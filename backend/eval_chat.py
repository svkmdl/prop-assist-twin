"""Live chat evaluation runner (NOT a unit test: it calls a running API).

Exit codes: 0 = every case passed, 1 = at least one case FAILED or ERRORED,
2 = the run could not start or finish cleanly (invalid fixture, unsafe key
transport, unwritable results file).

Case status: PASS, FAIL (an assertion did not hold, including an unexpected
4xx) or ERROR (transport failure, 5xx or an unreadable response, i.e. the
service rather than the answer is at fault).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

import httpx


API_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
CASES_PATH = Path(os.getenv("EVAL_CASES", "evals/golden_chat_cases.jsonl"))
TIMEOUT_SECONDS = float(os.getenv("EVAL_TIMEOUT_SECONDS", "60"))
# Optional machine-readable run record. Opt-in so a plain run writes no files.
RESULTS_PATH = os.getenv("EVAL_RESULTS", "").strip()
# Optional admin key, sent as x-api-key. Environment only: never read from the
# fixture and never written to the results file or printed.
API_KEY = os.getenv("EVAL_API_KEY", "").strip()

RESULTS_SCHEMA_VERSION = 1
EXCERPT_CHARS = 2000
ERROR_DETAIL_CHARS = 300
TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
HEALTH_KEYS = (
    "status",
    "bedrock_model",
    "rag_enabled",
    "sagemaker_endpoint_configure",
    "s3vectors_configured",
    "public_chat_enabled",
    "use_s3",
)

# Every key the runner understands. A key outside this set is a fixture error,
# never silently ignored: a typo would otherwise disable that check.
STRING_LIST_FIELDS = ("must_contain_any", "must_contain_all", "must_not_contain")
# Checks that need an answer, so they only make sense when HTTP 200 is expected.
ANSWER_CHECK_FIELDS = ("must_contain_any", "must_contain_all", "min_sources", "requires_citation")
ALLOWED_CASE_KEYS = frozenset(
    {
        "id", "message", "min_sources", "requires_citation", "tenant_id", "expect_status",
        "category", "expected_docs", "language", *STRING_LIST_FIELDS,
    }
)

# What a case is testing; the closed vocabulary lets results be reported per failure mode
# (CLAUDE.md section 13.4: retrieval miss, stale evidence, unauthorized access, ...).
CATEGORIES = frozenset(
    {
        "lease-fact", "policy-fact", "property-fact", "precedence", "outdated-version",
        "co-tenant", "english", "triage", "escalation", "staff-fact", "staff-access",
        "unanswerable", "privacy", "isolation-lease", "isolation-property", "isolation-staff",
        "injection",
    }
)
LANGUAGES = frozenset({"de", "en"})
# An evidence reference is a document's scope-qualified key without ``incoming/`` and
# ``.md``, i.e. ``common.doc_keys.DocumentKey.relative_key``.
DOC_REF_PATTERN = re.compile(
    r"(?:staff|renters)/(?:company|properties/P-\d{3,12}|leases/L-\d{4,12})"
    r"/(?:contracts|policies|sops|statements)/[a-z0-9]+(?:-[a-z0-9]+)*/v[1-9][0-9]{0,5}"
)


class FixtureError(ValueError):
    """The golden fixture cannot be interpreted exactly as written."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def validate_case(case: Any) -> list[str]:
    """Return the problems with one case (empty list = valid)."""
    if not isinstance(case, dict):
        return [f"case must be a JSON object, got {type(case).__name__}"]

    problems: list[str] = []
    for key in sorted(case.keys() - ALLOWED_CASE_KEYS):
        hint = difflib.get_close_matches(key.strip(" ,"), ALLOWED_CASE_KEYS, n=1)
        suffix = f" (did you mean {hint[0]!r}?)" if hint else ""
        problems.append(f"unknown key {key!r}{suffix}")

    for key in ("id", "message"):
        value = case.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{key!r} is required and must be a non-empty string")

    for key in STRING_LIST_FIELDS:
        if key not in case:
            continue
        value = case[key]
        # An empty list or an empty needle would make the check vacuous
        # (every text contains ""), so both are rejected.
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(item, str) and item.strip() for item in value)
        ):
            problems.append(f"{key!r} must be a non-empty list of non-empty strings")

    if "min_sources" in case:
        value = case["min_sources"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append("'min_sources' must be an integer >= 0")

    if "requires_citation" in case and not isinstance(case["requires_citation"], bool):
        problems.append("'requires_citation' must be true or false")

    if "category" in case and case["category"] not in CATEGORIES:
        problems.append(f"'category' must be one of {sorted(CATEGORIES)}")

    if "language" in case and case["language"] not in LANGUAGES:
        problems.append(f"'language' must be one of {sorted(LANGUAGES)}")

    if "expected_docs" in case:
        value = case["expected_docs"]
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(item, str) and DOC_REF_PATTERN.fullmatch(item) for item in value)
            or len(set(value)) != len(value)
        ):
            problems.append(
                "'expected_docs' must be a non-empty list of distinct document references "
                "like 'renters/leases/L-0001/contracts/lease-contract/v2'"
            )

    if "tenant_id" in case:
        value = case["tenant_id"]
        if not isinstance(value, str) or not TENANT_ID_PATTERN.match(value):
            problems.append("'tenant_id' must match ^[A-Za-z0-9_-]{1,32}$")

    expected: int | None = 200
    if "expect_status" in case:
        value = case["expect_status"]
        if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
            problems.append("'expect_status' must be an integer HTTP status (100-599)")
            expected = None
        else:
            expected = value

    if expected is not None and expected != 200:
        for key in ANSWER_CHECK_FIELDS:
            if key in case:
                problems.append(
                    f"{key!r} is not allowed with expect_status {expected} (there is no answer to check)"
                )

    # A case that asserts nothing would pass vacuously.
    if not problems and not (
        any(key in case for key in STRING_LIST_FIELDS)
        or case.get("min_sources", 0) > 0
        or case.get("requires_citation") is True
        or case.get("expect_status", 200) != 200
    ):
        problems.append(
            "case defines no check (add a must_* list, min_sources > 0, requires_citation or expect_status)"
        )

    return problems


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Parse and validate every line; raise FixtureError listing all problems."""
    cases: list[dict[str, Any]] = []
    problems: list[str] = []
    first_seen: dict[str, int] = {}

    with path.open("r", encoding="utf-8") as file:
        for line_no, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                problems.append(f"line {line_no}: invalid JSON ({exc.msg})")
                continue

            case_problems = validate_case(case)
            problems.extend(f"line {line_no}: {problem}" for problem in case_problems)
            if case_problems:
                continue

            if case["id"] in first_seen:
                problems.append(
                    f"line {line_no}: duplicate id {case['id']!r} "
                    f"(first on line {first_seen[case['id']]})"
                )
                continue
            first_seen[case["id"]] = line_no
            cases.append(case)

    if not problems and not cases:
        problems.append(f"{path}: no cases found")
    if problems:
        raise FixtureError(problems)
    return cases


def contains_any(text: str, needles: list[str]) -> bool:
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles)


def contains_none(text: str, needles: list[str]) -> bool:
    lowered = text.lower()
    return all(needle.lower() not in lowered for needle in needles)


def has_inline_citation(text: str) -> bool:
    return re.search(r"\[S\d+\]", text) is not None


def check_answer(case: dict[str, Any], answer: str, sources: list[Any]) -> list[str]:
    """Assertion failures for a 200 response (empty list = all checks held)."""
    failures: list[str] = []
    if case.get("must_contain_any") and not contains_any(answer, case["must_contain_any"]):
        failures.append(f"missing any of: {case['must_contain_any']}")

    if case.get("must_contain_all"):
        missing = [term for term in case["must_contain_all"] if term.lower() not in answer.lower()]
        if missing:
            failures.append(f"missing required terms: {missing}")

    if case.get("must_not_contain") and not contains_none(answer, case["must_not_contain"]):
        failures.append(f"contains forbidden term from: {case['must_not_contain']}")

    min_sources = int(case.get("min_sources", 0))
    if len(sources) < min_sources:
        failures.append(f"expected at least {min_sources} sources, got {len(sources)}")

    if case.get("requires_citation") and not has_inline_citation(answer):
        failures.append("expected an inline [S#] citation")

    return failures


def run_case(
    client: httpx.Client,
    case: dict[str, Any],
    *,
    session_id: str,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run one case and return its result record. Never raises for service faults."""
    expected = int(case.get("expect_status", 200))
    payload: dict[str, Any] = {"message": case["message"], "session_id": session_id}
    if "tenant_id" in case:
        payload["tenant_id"] = case["tenant_id"]

    result: dict[str, Any] = {
        "id": case["id"],
        "status": "ERROR",
        "session_id": session_id,
        "category": case.get("category"),
        "tenant_id_requested": case.get("tenant_id"),
        "tenant_id_effective": None,
        "expect_status": expected,
        "http_status": None,
        "latency_ms": None,
        "retrieval_used": None,
        "sources_count": None,
        "sources": [],
        "failures": [],
        "error": None,
        "response_excerpt": None,
    }

    started = perf_counter()
    try:
        response = client.post(f"{API_URL}/chat", json=payload, headers=headers or {})
    except httpx.HTTPError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:ERROR_DETAIL_CHARS]
        return result
    result["latency_ms"] = round((perf_counter() - started) * 1000, 1)
    result["http_status"] = response.status_code
    text = response.text
    result["response_excerpt"] = text[:EXCERPT_CHARS]

    if response.status_code != expected:
        detail = text[:ERROR_DETAIL_CHARS]
        if response.status_code >= 500:
            result["error"] = f"HTTP {response.status_code}: {detail}"
        else:
            result["status"] = "FAIL"
            result["failures"] = [f"expected HTTP {expected}, got {response.status_code}: {detail}"]
        return result

    if expected != 200:
        # A refusal has no answer; the only text check is that it leaks nothing.
        if case.get("must_not_contain") and not contains_none(text, case["must_not_contain"]):
            result["failures"] = [f"response contains forbidden term from: {case['must_not_contain']}"]
        result["status"] = "FAIL" if result["failures"] else "PASS"
        return result

    try:
        body = response.json()
    except ValueError:
        result["error"] = "HTTP 200 but the body was not valid JSON"
        return result
    if not isinstance(body, dict):
        result["error"] = f"HTTP 200 but the body was {type(body).__name__}, not an object"
        return result

    answer = body.get("response", "")
    sources = body.get("sources", [])
    result["tenant_id_effective"] = body.get("tenant_id")
    result["retrieval_used"] = body.get("retrieval_used")
    result["sources_count"] = len(sources)
    result["sources"] = [
        {"title": s.get("title"), "doc_type": s.get("doc_type"), "distance": s.get("distance")}
        for s in sources
        if isinstance(s, dict)
    ]
    result["failures"] = check_answer(case, answer, sources)
    result["status"] = "FAIL" if result["failures"] else "PASS"
    return result


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    # Latency is only meaningful where the API actually answered.
    latencies = [r["latency_ms"] for r in results if r["http_status"] is not None]
    by_category: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = by_category.setdefault(
            r.get("category") or "uncategorized", {"total": 0, "passed": 0, "failed": 0, "errors": 0}
        )
        bucket["total"] += 1
        bucket[{"PASS": "passed", "FAIL": "failed", "ERROR": "errors"}[r["status"]]] += 1
    return {
        "by_category": dict(sorted(by_category.items())),
        "total": len(results),
        "passed": sum(r["status"] == "PASS" for r in results),
        "failed": sum(r["status"] == "FAIL" for r in results),
        "errors": sum(r["status"] == "ERROR" for r in results),
        "latency_ms": {
            "n": len(latencies),
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "max": max(latencies) if latencies else None,
        },
    }


def key_transport_problem(api_url: str, api_key: str) -> str | None:
    """Refuse to send the admin key in clear text to a non-local host."""
    if not api_key:
        return None
    parsed = urlparse(api_url)
    if parsed.scheme == "https" or parsed.hostname in LOCAL_HOSTS:
        return None
    return "EVAL_API_KEY would be sent over plain HTTP to a non-local host; use https"


def fetch_service_info(client: httpx.Client) -> dict[str, Any]:
    """Record the target's public configuration flags; a failure is not fatal."""
    try:
        response = client.get(f"{API_URL}/health")
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:ERROR_DETAIL_CHARS]}
    if not isinstance(body, dict):
        return {"error": "health response was not an object"}
    return {key: body[key] for key in HEALTH_KEYS if key in body}


def build_report(
    *,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    dataset_path: Path,
    dataset_sha256: str,
    service: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": RESULTS_SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "api_url": API_URL,
        "authenticated": bool(API_KEY),  # whether a key was used, never its value
        "timeout_seconds": TIMEOUT_SECONDS,
        "dataset": {
            "path": str(dataset_path),
            "sha256": dataset_sha256,
            "cases": len(results),
        },
        "service": service,
        "summary": summarize(results),
        "results": results,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _print_result(case: dict[str, Any], result: dict[str, Any]) -> None:
    latency = f" ({result['latency_ms']:.0f} ms)" if result["latency_ms"] is not None else ""
    print(f"{result['status']} {result['id']}{latency}")
    if result["status"] == "PASS":
        return
    print(f"  message: {case['message']}")
    if result["error"]:
        print(f"  error: {result['error']}")
    if result["failures"]:
        print(f"  failures: {result['failures']}")
    if result["response_excerpt"]:
        print(f"  response: {result['response_excerpt'][:500]}")


def main() -> int:
    problem = key_transport_problem(API_URL, API_KEY)
    if problem:
        print(problem, file=sys.stderr)
        return 2

    try:
        cases = load_cases(CASES_PATH)
        dataset_sha256 = hashlib.sha256(CASES_PATH.read_bytes()).hexdigest()
    except (OSError, FixtureError) as exc:
        # Exit 2 = the fixture is unusable, distinct from 1 = a case failed.
        print(f"INVALID FIXTURE {CASES_PATH}", file=sys.stderr)
        for fixture_problem in getattr(exc, "problems", [str(exc)]):
            print(f"  {fixture_problem}", file=sys.stderr)
        return 2

    headers = {"x-api-key": API_KEY} if API_KEY else {}
    started_at = datetime.now(timezone.utc)
    run_uid = uuid.uuid4().hex[:8]
    run_id = f"{started_at:%Y%m%dT%H%M%SZ}-{run_uid}"
    print(f"run {run_id}: {len(cases)} cases against {API_URL} ({'admin key' if API_KEY else 'anonymous'})")

    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        service = fetch_service_info(client)
        for index, case in enumerate(cases, start=1):
            # A fresh session per case and per run: no history carries over, so
            # case order and reruns cannot influence each other.
            result = run_case(client, case, session_id=f"eval-{run_uid}-{index:03d}", headers=headers)
            results.append(result)
            _print_result(case, result)

    report = build_report(
        run_id=run_id,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        dataset_path=CASES_PATH,
        dataset_sha256=dataset_sha256,
        service=service,
        results=results,
    )
    summary = report["summary"]
    latency = summary["latency_ms"]
    categories = summary["by_category"]
    if len(categories) > 1 or "uncategorized" not in categories:
        print("\nby category:")
        for name, counts in categories.items():
            print(f"  {name:18} {counts['passed']}/{counts['total']} passed")
    print(
        f"\nscore: {summary['passed']}/{summary['total']} passed, "
        f"{summary['failed']} failed, {summary['errors']} errors; "
        f"latency p50={latency['p50']} p95={latency['p95']} ms (n={latency['n']})"
    )

    exit_code = 0 if summary["passed"] == summary["total"] else 1
    if RESULTS_PATH:
        try:
            write_report(Path(RESULTS_PATH), report)
        except OSError as exc:
            print(f"could not write results to {RESULTS_PATH}: {exc}", file=sys.stderr)
            return 2
        print(f"results: {RESULTS_PATH}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
