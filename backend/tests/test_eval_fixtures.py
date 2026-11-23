"""Golden eval fixture validation (Milestone 1, P1 evaluation foundation).

Deterministic and offline: no AWS and no network. The live model evaluation
itself stays in ``eval_chat.py``; this only proves the *fixture* is read exactly
as written, so a typo cannot silently switch a check off.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
GOLDEN_PATH = BACKEND_DIR / "evals" / "golden_chat_cases.jsonl"


@pytest.fixture
def eval_chat(monkeypatch):
    monkeypatch.syspath_prepend(str(BACKEND_DIR))
    sys.modules.pop("eval_chat", None)
    return importlib.import_module("eval_chat")


def write_jsonl(tmp_path, *lines: str) -> Path:
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def case(**overrides) -> str:
    base = {"id": "c1", "message": "hi", "must_contain_any": ["hello"]}
    base.update(overrides)
    return json.dumps(base)


class TestGoldenFile:
    def test_every_line_parses_and_validates(self, eval_chat):
        cases = eval_chat.load_cases(GOLDEN_PATH)
        assert len(cases) >= 4
        assert len({c["id"] for c in cases}) == len(cases)

    @pytest.mark.parametrize(
        "path", sorted((BACKEND_DIR / "evals").glob("*.jsonl")), ids=lambda p: p.name
    )
    def test_every_fixture_in_evals_validates(self, eval_chat, path):
        assert eval_chat.load_cases(path)

    def test_no_key_has_stray_punctuation(self):
        """Regression: keys like ',min_sources' were silently ignored by the runner."""
        for line_no, line in enumerate(GOLDEN_PATH.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            for key in json.loads(line):
                assert key.isidentifier(), f"line {line_no}: suspicious key {key!r}"

    def test_language_case_forbidden_terms_are_enforced(self, eval_chat):
        """The previously ignored check now fails a German answer to an English question."""
        language_case = next(
            c for c in eval_chat.load_cases(GOLDEN_PATH) if c["id"] == "language_en"
        )
        assert language_case["must_not_contain"] == ["Ich antworte", "Deutsch"]

        def run(answer: str):
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={"response": answer, "sources": []})

            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                return eval_chat.run_case(client, language_case, session_id="eval-test")

        result = run("Ich antworte auf Deutsch in der Sprache des Nutzers.")
        assert result["status"] == "FAIL"
        assert any("forbidden" in failure for failure in result["failures"])

        result = run("I answer in the same language you write in, English.")
        assert result["status"] == "PASS" and result["failures"] == []


class TestValidateCase:
    def test_valid_minimal_and_full_cases(self, eval_chat):
        assert eval_chat.validate_case(json.loads(case())) == []
        full = json.loads(
            case(
                must_contain_all=["a"],
                must_not_contain=["b"],
                min_sources=1,
                requires_citation=True,
            )
        )
        assert eval_chat.validate_case(full) == []

    @pytest.mark.parametrize("bad_key", [",min_sources", "min_source", "must_not_contains"])
    def test_unknown_key_rejected_with_hint(self, eval_chat, bad_key):
        problems = eval_chat.validate_case({**json.loads(case()), bad_key: 0})
        assert len(problems) == 1
        assert "unknown key" in problems[0] and "did you mean" in problems[0]

    @pytest.mark.parametrize(
        "override",
        [
            {"id": ""},
            {"id": 7},
            {"message": "   "},
            {"must_contain_any": []},
            {"must_contain_any": "hello"},
            {"must_contain_any": ["ok", ""]},
            {"must_contain_any": ["ok", 3]},
            {"must_not_contain": [" "]},
            {"min_sources": -1},
            {"min_sources": "1"},
            {"min_sources": True},
            {"requires_citation": "yes"},
        ],
    )
    def test_invalid_values_rejected(self, eval_chat, override):
        assert eval_chat.validate_case({**json.loads(case()), **override})

    def test_missing_required_fields(self, eval_chat):
        problems = eval_chat.validate_case({"must_contain_any": ["x"]})
        assert any("'id'" in p for p in problems)
        assert any("'message'" in p for p in problems)

    def test_case_without_any_check_rejected(self, eval_chat):
        problems = eval_chat.validate_case({"id": "c", "message": "m", "min_sources": 0})
        assert problems == [
            "case defines no check (add a must_* list, min_sources > 0, requires_citation or expect_status)"
        ]

    def test_min_sources_alone_or_citation_alone_is_a_check(self, eval_chat):
        assert eval_chat.validate_case({"id": "c", "message": "m", "min_sources": 1}) == []
        assert eval_chat.validate_case({"id": "c", "message": "m", "requires_citation": True}) == []
        assert eval_chat.validate_case({"id": "c", "message": "m", "requires_citation": False})

    def test_expect_status_alone_is_a_check(self, eval_chat):
        assert eval_chat.validate_case({"id": "c", "message": "m", "expect_status": 403}) == []

    @pytest.mark.parametrize(
        "override",
        [
            {"tenant_id": ""},
            {"tenant_id": "a b"},
            {"tenant_id": "x" * 33},
            {"tenant_id": 1},
            {"expect_status": 99},
            {"expect_status": 600},
            {"expect_status": "403"},
            {"expect_status": True},
        ],
    )
    def test_invalid_tenant_or_status_rejected(self, eval_chat, override):
        assert eval_chat.validate_case({**json.loads(case()), **override})

    @pytest.mark.parametrize(
        "extra",
        [
            {"must_contain_any": ["x"]},
            {"must_contain_all": ["x"]},
            {"min_sources": 0},
            {"requires_citation": True},
        ],
    )
    def test_answer_checks_not_allowed_with_a_refusal_status(self, eval_chat, extra):
        problems = eval_chat.validate_case(
            {"id": "c", "message": "m", "expect_status": 403, **extra}
        )
        assert any("not allowed with expect_status 403" in p for p in problems)

    def test_must_not_contain_is_allowed_with_a_refusal_status(self, eval_chat):
        assert (
            eval_chat.validate_case(
                {"id": "c", "message": "m", "expect_status": 403, "must_not_contain": ["x"]}
            )
            == []
        )

    def test_non_object_rejected(self, eval_chat):
        assert eval_chat.validate_case(["not", "an", "object"])


class TestLoadCases:
    def test_blank_lines_are_ignored(self, eval_chat, tmp_path):
        path = write_jsonl(tmp_path, case(id="a"), "", "   ", case(id="b"))
        assert [c["id"] for c in eval_chat.load_cases(path)] == ["a", "b"]

    def test_reports_every_problem_with_line_numbers(self, eval_chat, tmp_path):
        path = write_jsonl(
            tmp_path,
            case(id="a"),
            "{not json",
            case(id="b", **{",min_sources": 0}),
            case(id="a"),
        )
        with pytest.raises(eval_chat.FixtureError) as err:
            eval_chat.load_cases(path)
        text = "\n".join(err.value.problems)
        assert "line 2: invalid JSON" in text
        assert "line 3: unknown key ',min_sources'" in text
        assert "line 4: duplicate id 'a' (first on line 1)" in text

    def test_empty_file_is_an_error(self, eval_chat, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("\n\n", encoding="utf-8")
        with pytest.raises(eval_chat.FixtureError, match="no cases found"):
            eval_chat.load_cases(path)


class TestMainExitCodes:
    def test_invalid_fixture_exits_2_without_calling_the_api(
        self, eval_chat, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            eval_chat, "CASES_PATH", write_jsonl(tmp_path, case(**{",min_sources": 0}))
        )

        def boom(*args, **kwargs):
            raise AssertionError("the API must not be called for an invalid fixture")

        monkeypatch.setattr(eval_chat.httpx, "Client", boom)
        assert eval_chat.main() == 2
        err = capsys.readouterr().err
        assert "INVALID FIXTURE" in err and "unknown key ',min_sources'" in err

    def test_missing_fixture_file_exits_2(self, eval_chat, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(eval_chat, "CASES_PATH", tmp_path / "nope.jsonl")
        assert eval_chat.main() == 2
        assert "INVALID FIXTURE" in capsys.readouterr().err
