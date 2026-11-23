"""The labeled eval cases and the synthetic corpus must agree (ADR 0002 slice 5, CLAUDE.md 13.4).

Offline and deterministic: no AWS, no model. A live run can only be as good as its labels, so
these tests prove the labels are consistent with the corpus and with the access rules:

* every fact a case requires exists in the evidence documents it cites;
* every cited document is visible to the asking tenant (and stays so as the calendar moves);
* every "must not contain" marker of an isolation case exists only OUTSIDE the tenant's scope
  (otherwise a leak would be impossible, or a correct answer could trip it);
* outdated-version markers come from an expired version and from no document in force.

They do not say anything about answer quality: the cases still have to be run against a live,
ingested deployment (``python eval_chat.py`` with ``EVAL_CASES=evals/property_cases.jsonl``).
"""
from __future__ import annotations

import collections
import re
from datetime import date
from pathlib import Path

import pytest

import eval_chat
from common.access import ROLE_STAFF, StaticScopeResolver, is_authorized
from common.doc_keys import SCOPE_RULES
from common.prepare import prepare_document
from common.registry import load_registry
from common.corpus import validate_corpus

BACKEND_DIR = Path(__file__).resolve().parents[1]
CORPUS = BACKEND_DIR / "corpus"
CASES_PATH = BACKEND_DIR / "evals" / "property_cases.jsonl"
REGISTRY = load_registry(BACKEND_DIR / "data" / "registry.json")

# The corpus was authored on this day. Every document is either open-ended or already expired
# by then, so which documents are in force never depends on when the evals are run.
AUTHORED_ON = date(2026, 11, 23)
FAR_FUTURE = date(2035, 1, 1)

ANSWERABLE = {
    "lease-fact", "policy-fact", "property-fact", "precedence", "outdated-version", "co-tenant",
    "english", "triage", "escalation", "staff-fact", "staff-access",
}
LEAK_CHECKED = {"privacy", "isolation-lease", "isolation-property", "isolation-staff", "injection"}
NO_EVIDENCE = {"unanswerable"}


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


class Doc:
    def __init__(self, path: Path):
        relative = path.relative_to(CORPUS).as_posix()
        prepared = prepare_document(relative, path.read_bytes())
        self.ref = prepared.key.relative_key
        self.key = prepared.key
        self.front = prepared.front_matter
        self.text = norm(prepared.body)
        self.metadata = prepared.doc_metadata


DOCS = [Doc(p) for p in sorted((CORPUS / "incoming").rglob("*.md"))]
BY_REF = {d.ref: d for d in DOCS}


def scope_for(principal: str, as_of: date):
    return StaticScopeResolver(REGISTRY, today=lambda: as_of).resolve(principal)


def visible(principal: str, as_of: date = AUTHORED_ON) -> list[Doc]:
    scope = scope_for(principal, as_of)
    return [d for d in DOCS if is_authorized(d.metadata, scope)]


def in_force(as_of: date = AUTHORED_ON) -> list[Doc]:
    return [d for d in DOCS if d.front.effective_from <= as_of and (d.front.effective_to is None or as_of <= d.front.effective_to)]


CASES = eval_chat.load_cases(CASES_PATH)
CASE_IDS = [c["id"] for c in CASES]


def joined(refs) -> str:
    return " ".join(BY_REF[r].text for r in refs)


class TestCategoriesAreFullyCovered:
    def test_every_category_has_exactly_one_rule(self):
        assert ANSWERABLE | LEAK_CHECKED | NO_EVIDENCE == eval_chat.CATEGORIES
        assert not (ANSWERABLE & LEAK_CHECKED or ANSWERABLE & NO_EVIDENCE or LEAK_CHECKED & NO_EVIDENCE)

    def test_the_planning_size_and_spread(self):
        assert 60 <= len(CASES) <= 100  # CLAUDE.md 13.4: roughly 60-100 labeled cases
        used = collections.Counter(c["category"] for c in CASES)
        assert set(used) == eval_chat.CATEGORIES  # every failure mode is represented
        assert sum(used[c] for c in LEAK_CHECKED) >= 15  # access boundaries carry real weight

    def test_every_case_declares_its_category_and_tenant(self):
        for case in CASES:
            assert "category" in case and "tenant_id" in case, case["id"]
            assert case["tenant_id"] in REGISTRY.tenant_leases or REGISTRY.is_staff(case["tenant_id"]), case["id"]

    def test_every_principal_is_exercised_and_every_renter_is_attacked(self):
        by_principal = collections.defaultdict(set)
        for case in CASES:
            by_principal[case["tenant_id"]].add(case["category"])
        assert set(by_principal) == set(REGISTRY.tenant_leases) | set(REGISTRY.staff)
        for tenant in REGISTRY.tenant_leases:
            assert by_principal[tenant] & LEAK_CHECKED, f"{tenant} has no isolation/injection case"


class TestEvidenceExistsAndIsVisible:
    @pytest.mark.parametrize("case", [c for c in CASES if c["category"] in ANSWERABLE], ids=lambda c: c["id"])
    def test_answerable_cases_cite_visible_evidence(self, case):
        refs = case["expected_docs"]
        assert case.get("requires_citation") is True and case.get("min_sources", 0) >= 1
        for ref in refs:
            assert ref in BY_REF, f"unknown document {ref}"
        for as_of in (AUTHORED_ON, FAR_FUTURE):
            seen = {d.ref for d in visible(case["tenant_id"], as_of)}
            assert set(refs) <= seen, f"{set(refs) - seen} not visible to {case['tenant_id']} on {as_of}"

    @pytest.mark.parametrize("case", [c for c in CASES if c["category"] not in ANSWERABLE], ids=lambda c: c["id"])
    def test_other_cases_cite_no_evidence(self, case):
        assert "expected_docs" not in case

    def test_every_document_in_force_is_the_evidence_of_some_case(self):
        cited = {ref for c in CASES for ref in c.get("expected_docs", [])}
        unused = {d.ref for d in in_force()} - cited
        assert not unused, f"documents no case exercises: {sorted(unused)}"

    def test_outdated_cases_cite_the_current_version_only(self):
        expired = {d.ref for d in DOCS} - {d.ref for d in in_force()}
        for case in CASES:
            if case["category"] == "outdated-version":
                assert not set(case["expected_docs"]) & expired, case["id"]


class TestRequiredFactsAreInTheEvidence:
    @pytest.mark.parametrize("case", [c for c in CASES if c["category"] in ANSWERABLE], ids=lambda c: c["id"])
    def test_required_terms_appear_in_the_cited_documents(self, case):
        text = joined(case["expected_docs"])
        for term in case.get("must_contain_all", []):
            assert norm(term) in text, f"required term {term!r} is not in {case['expected_docs']}"
        if case.get("must_contain_any"):
            # At least one accepted wording must be checkable against the evidence. English
            # variants may be absent from the German text, but a language-neutral one is not.
            assert any(norm(v) in text for v in case["must_contain_any"]), (
                f"none of {case['must_contain_any']} appears in {case['expected_docs']}"
            )
        assert case.get("must_contain_all") or case.get("must_contain_any"), "an answerable case must check a fact"

    def test_no_required_wording_is_also_forbidden(self):
        for case in CASES:
            required = [norm(t) for t in case.get("must_contain_all", []) + case.get("must_contain_any", [])]
            forbidden = [norm(t) for t in case.get("must_not_contain", [])]
            for r in required:
                for f in forbidden:
                    assert f not in r and r not in f, (case["id"], r, f)

    def test_unanswerable_cases_only_check_the_refusal_wording(self):
        for case in CASES:
            if case["category"] == "unanswerable":
                assert case.get("must_contain_any") and not case.get("must_contain_all")
                assert case.get("min_sources", 0) == 0


class TestLeakMarkersAreMeaningful:
    @pytest.mark.parametrize("case", [c for c in CASES if c["category"] in LEAK_CHECKED], ids=lambda c: c["id"])
    def test_markers_exist_only_outside_the_askers_scope(self, case):
        markers = [norm(m) for m in case["must_not_contain"]]
        mine = {d.ref for d in visible(case["tenant_id"])}
        hidden = " ".join(d.text for d in DOCS if d.ref not in mine)
        shown = " ".join(d.text for d in DOCS if d.ref in mine)
        assert any(m in hidden for m in markers), f"{case['id']}: no marker occurs in a document the tenant cannot see"
        for marker in markers:
            assert marker not in shown, f"{case['id']}: {marker!r} occurs in a document the tenant is allowed to see"

    @pytest.mark.parametrize("case", [c for c in CASES if c["category"] == "outdated-version"], ids=lambda c: c["id"])
    def test_outdated_markers_come_from_an_expired_version_only(self, case):
        markers = [norm(m) for m in case["must_not_contain"]]
        live = {d.ref for d in in_force()}
        expired_text = " ".join(d.text for d in DOCS if d.ref not in live)
        current_text = " ".join(d.text for d in visible(case["tenant_id"]) if d.ref in live)
        assert any(m in expired_text for m in markers), f"{case['id']}: no marker occurs in an expired document"
        for marker in markers:
            assert marker not in current_text, f"{case['id']}: {marker!r} appears in a current document"


class TestCorpusShapeAndStability:
    def test_the_shipped_corpus_passes_the_validator_the_worker_logic_shares(self):
        report = validate_corpus(CORPUS, REGISTRY)
        assert report.problems == [] and len(report.documents) == len(DOCS)

    def test_size_matches_the_planning_target(self):
        logical = {d.key.logical_doc for d in DOCS}
        assert 10 <= len(logical) <= 20 and len(DOCS) <= 25  # CLAUDE.md 13.4: roughly 10-20 documents

    def test_every_scope_row_and_every_lease_and_property_is_represented(self):
        assert {(d.key.audience, d.key.scope_type) for d in DOCS} == {(r.audience, r.scope_type) for r in SCOPE_RULES}
        assert {d.key.lease_id for d in DOCS if d.key.lease_id} == set(REGISTRY.lease_property)
        assert {d.key.property_id for d in DOCS if d.key.property_id} == set(REGISTRY.properties)

    def test_results_never_depend_on_the_calendar(self):
        for d in DOCS:
            assert d.front.effective_from <= AUTHORED_ON, f"{d.ref} starts in the future"
            assert d.front.effective_to is None or d.front.effective_to < AUTHORED_ON, f"{d.ref} ends in the future"
        for principal in sorted(REGISTRY.tenant_leases) + sorted(REGISTRY.staff):
            assert {d.ref for d in visible(principal, AUTHORED_ON)} == {d.ref for d in visible(principal, FAR_FUTURE)}

    def test_renters_never_see_staff_documents_and_staff_see_everything_in_force(self):
        for tenant in REGISTRY.tenant_leases:
            assert all(d.key.audience == "renter" for d in visible(tenant)), tenant
        staff = {d.ref for d in visible("admin")}
        assert staff == {d.ref for d in in_force()}
        assert scope_for("admin", AUTHORED_ON).role == ROLE_STAFF

    def test_the_isolation_scenario_the_cases_rely_on_holds(self):
        refs = {t: {d.ref for d in visible(t)} for t in REGISTRY.tenant_leases}
        assert refs["T003"] == refs["T004"]  # co-tenants
        assert not (refs["T001"] & {r for r in refs["T002"] if "/leases/" in r or "/properties/" in r})
        own_lease = lambda t: {r for r in refs[t] if "/leases/" in r}
        assert own_lease("T001") != own_lease("T003")  # same property, different lease
        assert {r for r in refs["T001"] if "/properties/" in r} == {r for r in refs["T003"] if "/properties/" in r}

    def test_every_version_chain_is_current_for_exactly_one_version(self):
        chains = collections.defaultdict(list)
        for d in in_force():
            chains[d.key.logical_doc].append(d.key.doc_version)
        assert all(len(v) == 1 for v in chains.values())

    def test_documents_are_marked_synthetic(self):
        for path in (CORPUS / "incoming").rglob("*.md"):
            assert "synthetisches testdokument" in path.read_text(encoding="utf-8").lower(), path.name
