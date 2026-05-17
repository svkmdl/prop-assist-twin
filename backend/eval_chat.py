from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx


API_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
CASES_PATH = Path(os.getenv("EVAL_CASES", "evals/golden_chat_cases.jsonl"))
TIMEOUT_SECONDS = float(os.getenv("EVAL_TIMEOUT_SECONDS", "60"))


def load_cases(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def contains_any(text: str, needles: list[str]) -> bool:
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles)


def contains_none(text: str, needles: list[str]) -> bool:
    lowered = text.lower()
    return all(needle.lower() not in lowered for needle in needles)


def has_inline_citation(text: str) -> bool:
    return re.search(r"\[S\d+\]", text) is not None


def evaluate_case(client: httpx.Client, case: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    response = client.post(
        f"{API_URL}/chat",
        json={"message": case["message"], "session_id": f"eval-{case['id']}"},
    )
    response.raise_for_status()
    body = response.json()
    answer = body.get("response", "")
    sources = body.get("sources", [])

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

    return not failures, failures, body


def main() -> int:
    cases = load_cases(CASES_PATH)
    passed = 0

    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        for case in cases:
            ok, failures, body = evaluate_case(client, case)
            status = "PASS" if ok else "FAIL"
            print(f"{status} {case['id']}")
            if not ok:
                print(f"  message: {case['message']}")
                print(f"  failures: {failures}")
                print(f"  response: {body.get('response', '')[:500]}")
            passed += int(ok)

    total = len(cases)
    print(f"\nscore: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())