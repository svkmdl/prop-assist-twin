"""Behaviour of the live eval runner, exercised offline with a mocked transport.

No AWS and no network: ``httpx.MockTransport`` plays the API. These tests prove
the runner reports service faults per case instead of crashing, isolates
sessions, keeps the admin key out of its output and writes a faithful record.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
KEY = "super-secret-eval-key"
REAL_CLIENT = httpx.Client  # captured before any test patches httpx.Client


@pytest.fixture
def eval_chat(monkeypatch):
    monkeypatch.syspath_prepend(str(BACKEND_DIR))
    sys.modules.pop("eval_chat", None)
    return importlib.import_module("eval_chat")


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_body(answer="Hallo, ich bin Marie Stein.", sources=None, tenant="T001"):
    return {
        "response": answer,
        "session_id": "x",
        "tenant_id": tenant,
        "sources": sources or [],
        "retrieval_used": bool(sources),
    }


CASE = {"id": "c1", "message": "Hallo", "must_contain_any": ["Marie Stein"]}


class TestRunCaseOutcomes:
    def test_pass_records_the_response_details(self, eval_chat):
        sources = [{"id": "S1", "title": "FAQ", "doc_type": ".md", "distance": 0.2, "snippet": "s"}]
        with make_client(lambda r: httpx.Response(200, json=ok_body(sources=sources))) as client:
            result = eval_chat.run_case(client, CASE, session_id="eval-1")
        assert result["status"] == "PASS"
        assert result["http_status"] == 200 and result["latency_ms"] is not None
        assert result["tenant_id_effective"] == "T001"
        assert result["retrieval_used"] is True and result["sources_count"] == 1
        assert result["sources"] == [{"title": "FAQ", "doc_type": ".md", "distance": 0.2}]
        assert result["error"] is None and result["failures"] == []

    def test_assertion_miss_is_a_fail_with_the_answer_kept(self, eval_chat):
        with make_client(lambda r: httpx.Response(200, json=ok_body("Ich bin ein Bot."))) as client:
            result = eval_chat.run_case(client, CASE, session_id="eval-1")
        assert result["status"] == "FAIL"
        assert result["failures"] and "Ich bin ein Bot." in result["response_excerpt"]

    @pytest.mark.parametrize(
        "exc",
        [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
        ids=["connect", "timeout"],
    )
    def test_transport_failure_is_an_error_not_a_crash(self, eval_chat, exc):
        def handler(request):
            raise exc

        with make_client(handler) as client:
            result = eval_chat.run_case(client, CASE, session_id="eval-1")
        assert result["status"] == "ERROR"
        assert type(exc).__name__ in result["error"]
        assert result["http_status"] is None and result["latency_ms"] is None

    @pytest.mark.parametrize("code", [500, 502, 503])
    def test_5xx_is_an_error_with_detail(self, eval_chat, code):
        handler = lambda r: httpx.Response(code, json={"detail": "Chat is not available"})  # noqa: E731
        with make_client(handler) as client:
            result = eval_chat.run_case(client, CASE, session_id="eval-1")
        assert result["status"] == "ERROR"
        assert f"HTTP {code}" in result["error"] and "Chat is not available" in result["error"]
        assert result["http_status"] == code

    @pytest.mark.parametrize("code", [403, 422])
    def test_unexpected_4xx_is_a_fail(self, eval_chat, code):
        handler = lambda r: httpx.Response(code, json={"detail": "nope"})  # noqa: E731
        with make_client(handler) as client:
            result = eval_chat.run_case(client, CASE, session_id="eval-1")
        assert result["status"] == "FAIL"
        assert f"expected HTTP 200, got {code}" in result["failures"][0]

    def test_non_json_200_is_an_error(self, eval_chat):
        with make_client(lambda r: httpx.Response(200, text="<html>gateway</html>")) as client:
            result = eval_chat.run_case(client, CASE, session_id="eval-1")
        assert result["status"] == "ERROR" and "not valid JSON" in result["error"]

    def test_json_array_200_is_an_error(self, eval_chat):
        with make_client(lambda r: httpx.Response(200, json=["x"])) as client:
            result = eval_chat.run_case(client, CASE, session_id="eval-1")
        assert result["status"] == "ERROR" and "not an object" in result["error"]


class TestExpectedRefusals:
    DENIED = {"id": "d", "message": "hi", "tenant_id": "admin", "expect_status": 403,
              "must_not_contain": ["T001", "admin"]}

    def test_expected_status_passes(self, eval_chat):
        handler = lambda r: httpx.Response(403, json={"detail": "Access to the requested scope is not permitted"})  # noqa: E731
        with make_client(handler) as client:
            result = eval_chat.run_case(client, self.DENIED, session_id="eval-1")
        assert result["status"] == "PASS" and result["expect_status"] == 403

    def test_leaky_refusal_fails(self, eval_chat):
        handler = lambda r: httpx.Response(403, json={"detail": "tenant T001 is the public scope"})  # noqa: E731
        with make_client(handler) as client:
            result = eval_chat.run_case(client, self.DENIED, session_id="eval-1")
        assert result["status"] == "FAIL" and "forbidden term" in result["failures"][0]

    def test_succeeding_where_a_refusal_was_expected_fails(self, eval_chat):
        """The containment gap itself: anonymous 'admin' must not get an answer."""
        with make_client(lambda r: httpx.Response(200, json=ok_body(tenant="admin"))) as client:
            result = eval_chat.run_case(client, self.DENIED, session_id="eval-1")
        assert result["status"] == "FAIL"
        assert "expected HTTP 403, got 200" in result["failures"][0]

    def test_an_expected_5xx_is_not_treated_as_an_error(self, eval_chat):
        case = {"id": "off", "message": "hi", "expect_status": 503}
        with make_client(lambda r: httpx.Response(503, json={"detail": "Chat is not available"})) as client:
            assert eval_chat.run_case(client, case, session_id="s")["status"] == "PASS"


class TestRequestShape:
    def test_payload_headers_and_session(self, eval_chat):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            seen["key"] = request.headers.get("x-api-key")
            return httpx.Response(200, json=ok_body())

        case = {**CASE, "tenant_id": "T002"}
        with make_client(handler) as client:
            eval_chat.run_case(client, case, session_id="eval-abc-001", headers={"x-api-key": KEY})
        assert seen["body"] == {"message": "Hallo", "session_id": "eval-abc-001", "tenant_id": "T002"}
        assert seen["key"] == KEY

    def test_tenant_is_omitted_when_the_case_has_none(self, eval_chat):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            seen["key"] = request.headers.get("x-api-key")
            return httpx.Response(200, json=ok_body())

        with make_client(handler) as client:
            eval_chat.run_case(client, CASE, session_id="s")
        assert "tenant_id" not in seen["body"] and seen["key"] is None


class TestStats:
    def test_percentile_nearest_rank(self, eval_chat):
        assert eval_chat.percentile([], 50) is None
        assert eval_chat.percentile([5], 95) == 5
        values = [10, 20, 30, 40]
        assert eval_chat.percentile(values, 50) == 20
        assert eval_chat.percentile(values, 95) == 40

    def test_summary_counts_and_latency_only_for_answered_requests(self, eval_chat):
        results = [
            {"status": "PASS", "http_status": 200, "latency_ms": 100.0},
            {"status": "FAIL", "http_status": 403, "latency_ms": 300.0},
            {"status": "ERROR", "http_status": None, "latency_ms": None},
        ]
        summary = eval_chat.summarize(results)
        assert (summary["total"], summary["passed"], summary["failed"], summary["errors"]) == (3, 1, 1, 1)
        assert summary["latency_ms"] == {"n": 2, "p50": 100.0, "p95": 300.0, "max": 300.0}

    def test_results_are_summarized_per_category(self, eval_chat):
        results = [
            {"status": "PASS", "http_status": 200, "latency_ms": 1.0, "category": "lease-fact"},
            {"status": "FAIL", "http_status": 200, "latency_ms": 2.0, "category": "lease-fact"},
            {"status": "ERROR", "http_status": None, "latency_ms": None, "category": "injection"},
            {"status": "PASS", "http_status": 200, "latency_ms": 3.0},  # an uncategorized (older) case
        ]
        assert eval_chat.summarize(results)["by_category"] == {
            "injection": {"total": 1, "passed": 0, "failed": 0, "errors": 1},
            "lease-fact": {"total": 2, "passed": 1, "failed": 1, "errors": 0},
            "uncategorized": {"total": 1, "passed": 1, "failed": 0, "errors": 0},
        }

    def test_summary_of_nothing_answered_has_no_latency(self, eval_chat):
        summary = eval_chat.summarize([{"status": "ERROR", "http_status": None, "latency_ms": None}])
        assert summary["latency_ms"]["p95"] is None and summary["latency_ms"]["n"] == 0


class TestKeyTransport:
    @pytest.mark.parametrize(
        "url",
        ["https://api.example.com", "http://localhost:8000", "http://127.0.0.1:8000"],
    )
    def test_safe_targets(self, eval_chat, url):
        assert eval_chat.key_transport_problem(url, KEY) is None

    def test_cleartext_remote_is_refused(self, eval_chat):
        assert "plain HTTP" in eval_chat.key_transport_problem("http://api.example.com", KEY)

    def test_no_key_never_a_problem(self, eval_chat):
        assert eval_chat.key_transport_problem("http://api.example.com", "") is None


def write_cases(tmp_path, *cases) -> Path:
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(json.dumps(c) for c in cases) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def run_main(eval_chat, monkeypatch, tmp_path):
    """Run main() against a handler; returns (exit_code, requests, report_or_None)."""

    def _run(handler, cases, *, key="", results=True, api_url="http://localhost:8000"):
        requests: list[httpx.Request] = []

        def recording(request):
            requests.append(request)
            return handler(request)

        monkeypatch.setattr(
            eval_chat.httpx,
            "Client",
            lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(recording), **kw),
        )
        cases_path = write_cases(tmp_path, *cases)
        out = tmp_path / "out" / "run.json"
        monkeypatch.setattr(eval_chat, "CASES_PATH", cases_path)
        monkeypatch.setattr(eval_chat, "API_URL", api_url)
        monkeypatch.setattr(eval_chat, "API_KEY", key)
        monkeypatch.setattr(eval_chat, "RESULTS_PATH", str(out) if results else "")
        code = eval_chat.main()
        report = json.loads(out.read_text(encoding="utf-8")) if results and out.exists() else None
        return code, requests, report, cases_path

    return _run


def api(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/health":
        return httpx.Response(
            200,
            json={"status": "healthy", "bedrock_model": "m", "rag_enabled": False,
                  "public_chat_enabled": True, "secret_ish": "dropped"},
        )
    return httpx.Response(200, json=ok_body())


TWO_CASES = [
    {"id": "a", "message": "Hallo", "must_contain_any": ["Marie Stein"]},
    {"id": "b", "message": "Hi", "must_contain_any": ["Marie Stein"]},
]


class TestCategorizedCases:
    def test_a_case_category_reaches_the_result_record(self, eval_chat):
        case = {**CASE, "category": "lease-fact"}
        with make_client(lambda r: httpx.Response(200, json=ok_body())) as client:
            assert eval_chat.run_case(client, case, session_id="s")["category"] == "lease-fact"

    def test_main_prints_and_records_the_per_category_breakdown(self, run_main, capsys):
        cases = [{**TWO_CASES[0], "category": "lease-fact"}, {**TWO_CASES[1], "category": "injection"}]
        code, _, report, _ = run_main(api, cases)
        assert code == 0
        assert report["summary"]["by_category"]["injection"] == {"total": 1, "passed": 1, "failed": 0, "errors": 0}
        out = capsys.readouterr().out
        assert "by category:" in out and "lease-fact" in out and "1/1 passed" in out

    def test_uncategorized_runs_stay_quiet(self, run_main, capsys):
        run_main(api, TWO_CASES)
        assert "by category:" not in capsys.readouterr().out


class TestMain:
    def test_all_pass_exits_0_and_writes_a_faithful_record(self, run_main, capsys):
        code, requests, report, cases_path = run_main(api, TWO_CASES)
        assert code == 0
        assert report["schema_version"] == 1
        assert report["summary"]["passed"] == 2 and report["summary"]["total"] == 2
        assert report["dataset"] == {
            "path": str(cases_path),
            "sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
            "cases": 2,
        }
        assert report["authenticated"] is False
        assert report["service"] == {
            "status": "healthy", "bedrock_model": "m", "rag_enabled": False, "public_chat_enabled": True,
        }
        assert [r["id"] for r in report["results"]] == ["a", "b"]
        out = capsys.readouterr().out
        assert "score: 2/2 passed" in out and "p95=" in out

    def test_sessions_are_unique_per_case_and_per_run(self, run_main):
        def sessions():
            _, requests, report, _ = run_main(api, TWO_CASES)
            return [json.loads(r.content)["session_id"] for r in requests if r.url.path == "/chat"]

        first, second = sessions(), sessions()
        assert len(set(first)) == 2 and len(set(second)) == 2
        assert not set(first) & set(second)
        for sid in first + second:
            assert sid.startswith("eval-") and len(sid) <= 64
            assert all(c.isalnum() or c in "-_" for c in sid)  # the API's session pattern

    def test_a_failing_case_exits_1_and_the_run_continues(self, run_main):
        def handler(request):
            if request.url.path == "/chat" and json.loads(request.content)["message"] == "Hallo":
                return httpx.Response(200, json=ok_body("nope"))
            return api(request)

        code, requests, report, _ = run_main(handler, TWO_CASES)
        assert code == 1
        assert [r["status"] for r in report["results"]] == ["FAIL", "PASS"]

    def test_a_dead_api_exits_1_with_per_case_errors_not_a_traceback(self, run_main):
        def handler(request):
            raise httpx.ConnectError("refused")

        code, _, report, _ = run_main(handler, TWO_CASES)
        assert code == 1
        assert [r["status"] for r in report["results"]] == ["ERROR", "ERROR"]
        assert report["service"]["error"].startswith("ConnectError")  # health failure is not fatal

    def test_503_everywhere_is_reported_as_errors(self, run_main):
        handler = lambda r: httpx.Response(503, json={"detail": "Chat is not available"})  # noqa: E731
        code, _, report, _ = run_main(handler, TWO_CASES)
        assert code == 1
        assert report["summary"]["errors"] == 2
        assert "Chat is not available" in report["results"][0]["error"]

    def test_key_is_sent_but_never_written_or_printed(self, run_main, capsys):
        code, requests, report, _ = run_main(api, TWO_CASES, key=KEY)
        assert code == 0
        chat = [r for r in requests if r.url.path == "/chat"]
        assert all(r.headers["x-api-key"] == KEY for r in chat)
        health = [r for r in requests if r.url.path == "/health"]
        assert all("x-api-key" not in r.headers for r in health)
        assert report["authenticated"] is True
        captured = capsys.readouterr()
        assert KEY not in json.dumps(report)
        assert KEY not in captured.out + captured.err

    def test_unsafe_key_transport_exits_2_before_any_request(self, run_main):
        code, requests, report, _ = run_main(api, TWO_CASES, key=KEY, api_url="http://api.example.com")
        assert code == 2 and requests == [] and report is None

    def test_invalid_fixture_exits_2_before_any_request(self, run_main):
        bad = [{"id": "a", "message": "x", ",min_sources": 0, "must_contain_any": ["y"]}]
        code, requests, report, _ = run_main(api, bad)
        assert code == 2 and requests == [] and report is None

    def test_unwritable_results_path_exits_2_but_still_prints_the_score(
        self, run_main, eval_chat, monkeypatch, tmp_path, capsys
    ):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        code, _, _, _ = run_main(api, TWO_CASES, results=False)
        assert code == 0  # no results path: nothing to write
        monkeypatch.setattr(eval_chat, "RESULTS_PATH", str(blocker / "sub" / "run.json"))
        assert eval_chat.main() == 2
        captured = capsys.readouterr()
        assert "score: 2/2 passed" in captured.out
        assert "could not write results" in captured.err

    def test_access_boundary_fixture_against_a_contained_api_passes(self, eval_chat, monkeypatch):
        """The shipped isolation fixture passes on the P0 behaviour and fails without it."""
        cases = eval_chat.load_cases(BACKEND_DIR / "evals" / "access_boundary_cases.jsonl")
        assert [c["id"] for c in cases] == ["anon_admin_scope_denied", "anon_unregistered_tenant_denied"]

        def contained(request):
            # Uniform refusal: a registered scope and an unregistered tenant look identical.
            return httpx.Response(403, json={"detail": "Access to the requested scope is not permitted"})

        def open_api(request):  # e.g. LOCAL_DEV: any registered principal, 422 for unknown ones
            tenant = json.loads(request.content).get("tenant_id")
            if tenant == "T999":
                return httpx.Response(422, json={"detail": "Unknown tenant_id"})
            return httpx.Response(200, json=ok_body(tenant="admin"))

        def leaky(request):  # contained, but tells the caller which tenants exist
            tenant = json.loads(request.content).get("tenant_id")
            detail = "Unknown tenant_id" if tenant == "T999" else "Access to the requested scope is not permitted"
            return httpx.Response(403, json={"detail": detail})

        def run(handler):
            with make_client(handler) as client:
                return [
                    eval_chat.run_case(client, c, session_id=f"eval-{i}")["status"]
                    for i, c in enumerate(cases)
                ]

        assert run(contained) == ["PASS", "PASS"]
        assert run(open_api) == ["FAIL", "FAIL"]
        assert run(leaky) == ["PASS", "FAIL"]
