"""Registry loading and the offline corpus validator (ADR 0002, slice 1).

Filesystem tests use ``tmp_path`` only. No AWS, no network.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from common import corpus
from common.corpus import validate_corpus
from common.registry import RegistryError, load_registry, parse_registry

BACKEND_DIR = Path(__file__).resolve().parents[1]
SHIPPED_REGISTRY = BACKEND_DIR / "data" / "registry.json"
SHIPPED_CORPUS = BACKEND_DIR / "corpus"

VALID_REGISTRY = {
    "properties": ["P-001", "P-002"],
    "leases": {
        "L-0001": {"property_id": "P-001"},
        "L-0002": {"property_id": "P-002"},
        "L-0003": {"property_id": "P-001"},
    },
    "tenants": {"T001": ["L-0001"], "T002": ["L-0002"], "T003": ["L-0003"], "T004": ["L-0003"]},
    "staff": ["admin"],
}


def registry_problems(data) -> list[str]:
    with pytest.raises(RegistryError) as err:
        parse_registry(data)
    return err.value.problems


class TestShippedRegistry:
    def test_loads_and_encodes_the_isolation_scenario(self):
        reg = load_registry(SHIPPED_REGISTRY)
        assert reg.is_staff("admin") and not reg.is_staff("T001")
        # T001 (L-0001) and T003 (L-0003) share property P-001 but not a lease.
        assert reg.property_ids_for("T001") == reg.property_ids_for("T003") == {"P-001"}
        assert reg.lease_ids_for("T001") == {"L-0001"} and reg.lease_ids_for("T003") == {"L-0003"}
        # T003 and T004 are co-tenants of one lease.
        assert reg.lease_ids_for("T003") == reg.lease_ids_for("T004") == {"L-0003"}
        # T002 sees nothing of P-001.
        assert reg.property_ids_for("T002") == {"P-002"} and reg.lease_ids_for("T002") == {"L-0002"}

    def test_unknown_principals_have_no_scope(self):
        reg = load_registry(SHIPPED_REGISTRY)
        assert reg.lease_ids_for("T999") == frozenset() and reg.property_ids_for("T999") == frozenset()
        assert not reg.is_staff("T999")

    def test_matches_the_adr_document(self):
        assert json.loads(SHIPPED_REGISTRY.read_text()) == VALID_REGISTRY


class TestParseRegistry:
    def test_valid(self):
        reg = parse_registry(copy.deepcopy(VALID_REGISTRY))
        assert reg.lease_property["L-0003"] == "P-001"

    def test_a_tenant_may_hold_leases_in_several_properties(self):
        data = copy.deepcopy(VALID_REGISTRY)
        data["tenants"]["T001"] = ["L-0001", "L-0002"]
        reg = parse_registry(data)
        assert reg.property_ids_for("T001") == {"P-001", "P-002"}

    def test_a_tenant_without_leases_is_allowed(self):
        data = copy.deepcopy(VALID_REGISTRY)
        data["tenants"]["T005"] = []
        assert parse_registry(data).lease_ids_for("T005") == frozenset()

    @pytest.mark.parametrize("data", [[], "x", None, 3])
    def test_must_be_an_object(self, data):
        assert "must be a JSON object" in registry_problems(data)[0]

    def test_unknown_and_missing_top_level_keys(self):
        data = copy.deepcopy(VALID_REGISTRY)
        data["units"] = {}
        del data["staff"]
        problems = registry_problems(data)
        assert "unknown top-level key 'units'" in problems and "missing top-level key 'staff'" in problems

    @pytest.mark.parametrize(
        "mutate,fragment",
        [
            (lambda d: d["properties"].append("P-1"), "invalid id 'P-1'"),
            (lambda d: d["properties"].append("P-001"), "duplicates"),
            (lambda d: d.update(properties="P-001"), "must be a list of strings"),
            (lambda d: d["leases"].update({"L-9": {"property_id": "P-001"}}), "invalid lease id"),
            (lambda d: d["leases"].update({"L-0009": {"property_id": "P-404"}}), "unknown property 'P-404'"),
            (lambda d: d["leases"].update({"L-0009": "P-001"}), "must be exactly"),
            (lambda d: d["leases"].update({"L-0009": {"property_id": "P-001", "unit_id": "U-1"}}), "must be exactly"),
            (lambda d: d.update(leases=[]), "'leases' must be an object"),
            (lambda d: d["tenants"].update({"bad id": ["L-0001"]}), "invalid tenant id"),
            (lambda d: d["tenants"].update({"T009": ["L-9999"]}), "unknown leases ['L-9999']"),
            (lambda d: d["tenants"].update({"T009": "L-0001"}), "list of lease ids"),
            (lambda d: d["tenants"].update({"admin": ["L-0001"]}), "both tenant and staff"),
            (lambda d: d.update(tenants=[]), "'tenants' must be an object"),
            (lambda d: d["staff"].append("bad id"), "invalid id 'bad id'"),
        ],
    )
    def test_invalid_entries(self, mutate, fragment):
        data = copy.deepcopy(VALID_REGISTRY)
        mutate(data)
        assert any(fragment in p for p in registry_problems(data))

    def test_every_problem_is_reported_at_once(self):
        data = copy.deepcopy(VALID_REGISTRY)
        data["leases"]["L-0001"] = {"property_id": "P-404"}
        data["tenants"]["T009"] = ["L-9999"]
        assert len(registry_problems(data)) >= 2


class TestLoadRegistry:
    def test_missing_and_malformed_files(self, tmp_path):
        with pytest.raises(RegistryError, match="cannot read registry"):
            load_registry(tmp_path / "nope.json")
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        with pytest.raises(RegistryError, match="not valid JSON"):
            load_registry(bad)


# ----------------------------------------------------------------------------
# Corpus validator
# ----------------------------------------------------------------------------

REGISTRY = parse_registry(copy.deepcopy(VALID_REGISTRY))


def fm(frm="2027-01-01", to=None, title="Titel", language="de") -> str:
    lines = ["---", f"title: {title}", f"language: {language}", f"effective_from: {frm}"]
    if to:
        lines.append(f"effective_to: {to}")
    return "\n".join(lines) + "\n---\nInhalt des Dokuments.\n"


def put(root: Path, key: str, text: str | bytes | None = None) -> Path:
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    data = fm() if text is None else text
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


LEASE_DOC = "incoming/renters/leases/L-0001/contracts/lease-contract/v1.md"
COMPANY_DOC = "incoming/renters/company/policies/repair-policy/v1.md"
STAFF_DOC = "incoming/staff/company/sops/water-damage/v1.md"
PROPERTY_DOC = "incoming/renters/properties/P-001/policies/house-rules/v1.md"


class TestValidateCorpus:
    def test_a_valid_tree(self, tmp_path):
        for key in (LEASE_DOC, COMPANY_DOC, STAFF_DOC, PROPERTY_DOC):
            put(tmp_path, key)
        report = validate_corpus(tmp_path, REGISTRY)
        assert report.ok and report.problems == []
        assert sorted(d.path for d in report.documents) == sorted([LEASE_DOC, COMPANY_DOC, STAFF_DOC, PROPERTY_DOC])

    def test_missing_incoming_directory_means_no_documents(self, tmp_path):
        report = validate_corpus(tmp_path, REGISTRY)
        assert report.ok and report.documents == []

    def test_hidden_files_are_ignored(self, tmp_path):
        put(tmp_path, LEASE_DOC)
        put(tmp_path, "incoming/.DS_Store", b"\x00")
        put(tmp_path, "incoming/renters/.hidden/notes.txt", b"x")
        assert validate_corpus(tmp_path, REGISTRY).ok

    @pytest.mark.parametrize(
        "key,fragment",
        [
            ("incoming/T001/faq.md", "unknown audience/scope"),
            ("incoming/renters/company/policies/repair-policy/latest.md", "filename must be"),
            ("incoming/renters/company/policies/repair-policy/v1.txt", "filename must be"),
            ("incoming/renters/company/faq/repair-policy/v1.md", "unknown doc_type"),
            ("incoming/renters/leases/L-0099/contracts/x/v1.md", "lease 'L-0099' is not in the registry"),
            ("incoming/renters/properties/P-099/policies/x/v1.md", "property 'P-099' is not in the registry"),
            ("incoming/staff/properties/P-099/sops/x/v1.md", "property 'P-099' is not in the registry"),
        ],
    )
    def test_each_file_level_problem_names_the_file(self, tmp_path, key, fragment):
        put(tmp_path, key)
        report = validate_corpus(tmp_path, REGISTRY)
        assert report.documents == []
        assert len(report.problems) == 1
        assert report.problems[0].startswith(key + ": ") and fragment in report.problems[0]

    def test_content_problems(self, tmp_path):
        put(tmp_path, "incoming/renters/company/policies/empty/v1.md", b"")
        put(tmp_path, "incoming/renters/company/policies/binary/v1.md", b"---\n\xff\xfe\n---\nx")
        put(tmp_path, "incoming/renters/company/policies/nofm/v1.md", "# no front matter\n")
        put(tmp_path, "incoming/renters/company/policies/huge/v1.md", fm() + "x" * 200)
        report = validate_corpus(tmp_path, REGISTRY, max_bytes=150)
        text = "\n".join(report.problems)
        assert "empty/v1.md: file is empty" in text
        assert "binary/v1.md: file is not valid UTF-8" in text
        assert "nofm/v1.md: document must start with a front-matter block" in text
        assert "huge/v1.md: file is" in text and "over the 150 byte limit" in text
        assert report.documents == []

    def test_front_matter_problems_are_listed_individually(self, tmp_path):
        put(tmp_path, COMPANY_DOC, "---\ntitel: x\nlanguage: fr\n---\n")
        problems = validate_corpus(tmp_path, REGISTRY).problems
        assert len(problems) >= 3 and all(p.startswith(COMPANY_DOC + ": ") for p in problems)

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink support")
    def test_symlinks_are_rejected(self, tmp_path):
        target = put(tmp_path, "outside/real.md")
        link = tmp_path / "incoming/renters/company/policies/linked/v1.md"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        assert any("symbolic links are not allowed" in p for p in validate_corpus(tmp_path, REGISTRY).problems)


KEY = "incoming/renters/company/policies/repair-policy/v{n}.md"


class TestVersionWindows:
    def test_adjacent_windows_are_valid(self, tmp_path):
        put(tmp_path, KEY.format(n=1), fm("2026-01-01", "2026-12-31"))
        put(tmp_path, KEY.format(n=2), fm("2027-01-01", "2027-06-30"))
        put(tmp_path, KEY.format(n=3), fm("2027-07-01"))  # latest version may stay open-ended
        assert validate_corpus(tmp_path, REGISTRY).ok

    def test_a_gap_between_versions_is_allowed(self, tmp_path):
        put(tmp_path, KEY.format(n=1), fm("2026-01-01", "2026-03-31"))
        put(tmp_path, KEY.format(n=2), fm("2027-01-01"))
        assert validate_corpus(tmp_path, REGISTRY).ok

    def test_overlap_is_reported(self, tmp_path):
        put(tmp_path, KEY.format(n=1), fm("2026-01-01", "2027-01-31"))
        put(tmp_path, KEY.format(n=2), fm("2027-01-01"))
        problems = validate_corpus(tmp_path, REGISTRY).problems
        assert len(problems) == 1 and "v2" in problems[0] and "overlaps v1" in problems[0]

    def test_the_boundary_day_overlaps(self, tmp_path):
        put(tmp_path, KEY.format(n=1), fm("2026-01-01", "2027-01-01"))
        put(tmp_path, KEY.format(n=2), fm("2027-01-01"))
        assert any("overlaps v1" in p for p in validate_corpus(tmp_path, REGISTRY).problems)

    def test_an_open_ended_predecessor_must_be_closed(self, tmp_path):
        put(tmp_path, KEY.format(n=1), fm("2026-01-01"))
        put(tmp_path, KEY.format(n=2), fm("2027-01-01"))
        problems = validate_corpus(tmp_path, REGISTRY).problems
        assert len(problems) == 1 and "open-ended" in problems[0] and "set its effective_to" in problems[0]

    def test_versions_must_take_effect_in_order(self, tmp_path):
        put(tmp_path, KEY.format(n=1), fm("2027-06-01", "2027-12-31"))
        put(tmp_path, KEY.format(n=2), fm("2026-01-01", "2026-12-31"))
        problems = validate_corpus(tmp_path, REGISTRY).problems
        assert len(problems) == 1 and "must take effect after v1" in problems[0]

    def test_equal_start_dates_are_an_order_problem_not_a_silent_tie(self, tmp_path):
        put(tmp_path, KEY.format(n=1), fm("2027-01-01", "2027-03-31"))
        put(tmp_path, KEY.format(n=2), fm("2027-01-01"))
        assert any("must take effect after v1" in p for p in validate_corpus(tmp_path, REGISTRY).problems)

    def test_versions_are_ordered_numerically_not_lexically(self, tmp_path):
        for n, (a, b) in enumerate([("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-28"), ("2026-03-01", None)], 1):
            put(tmp_path, KEY.format(n=n), fm(a, b))
        put(tmp_path, KEY.format(n=10), fm("2026-04-01"))
        # v3 is open-ended and v10 follows: a string sort would put v10 before v2.
        problems = validate_corpus(tmp_path, REGISTRY).problems
        assert len(problems) == 1 and "v10" in problems[0] and "overlaps v3" in problems[0]

    def test_windows_are_independent_per_scope_and_document(self, tmp_path):
        put(tmp_path, "incoming/renters/leases/L-0001/contracts/lease-contract/v1.md", fm("2026-01-01"))
        put(tmp_path, "incoming/renters/leases/L-0002/contracts/lease-contract/v1.md", fm("2026-01-01"))
        put(tmp_path, "incoming/renters/company/policies/repair-policy/v1.md", fm("2026-01-01"))
        put(tmp_path, "incoming/staff/company/policies/repair-policy/v1.md", fm("2026-01-01"))
        assert validate_corpus(tmp_path, REGISTRY).ok

    def test_one_doc_id_under_two_doc_types_in_a_scope_is_rejected(self, tmp_path):
        put(tmp_path, "incoming/renters/company/policies/handbook/v1.md")
        put(tmp_path, "incoming/renters/company/sops/handbook/v1.md")
        problems = validate_corpus(tmp_path, REGISTRY).problems
        assert len(problems) == 1 and "several doc_types" in problems[0]

    def test_the_same_doc_id_in_different_scopes_is_fine(self, tmp_path):
        put(tmp_path, "incoming/renters/company/policies/handbook/v1.md")
        put(tmp_path, "incoming/staff/company/sops/handbook/v1.md")
        assert validate_corpus(tmp_path, REGISTRY).ok


class TestCli:
    def write_registry(self, tmp_path) -> Path:
        path = tmp_path / "registry.json"
        path.write_text(json.dumps(VALID_REGISTRY))
        return path

    def run(self, tmp_path, *extra, corpus_dir=None):
        return corpus.main(
            ["--corpus", str(corpus_dir or tmp_path / "c"), "--registry", str(self.write_registry(tmp_path), ), *extra]
        )

    def test_valid_corpus_exits_0(self, tmp_path, capsys):
        put(tmp_path / "c", LEASE_DOC)
        assert self.run(tmp_path) == 0
        assert "1 valid document(s), 0 problem(s)" in capsys.readouterr().out

    def test_problems_exit_1_and_go_to_stderr(self, tmp_path, capsys):
        put(tmp_path / "c", "incoming/T001/faq.md")
        assert self.run(tmp_path) == 1
        captured = capsys.readouterr()
        assert "incoming/T001/faq.md" in captured.err and "1 problem(s)" in captured.out

    def test_empty_corpus_passes_unless_documents_are_required(self, tmp_path, capsys):
        (tmp_path / "c").mkdir()
        assert self.run(tmp_path) == 0
        assert self.run(tmp_path, "--require-documents") == 1
        assert "--require-documents" in capsys.readouterr().err

    def test_a_wrong_corpus_path_is_not_silently_valid(self, tmp_path, capsys):
        assert self.run(tmp_path, corpus_dir=tmp_path / "typo") == 2
        assert "corpus directory not found" in capsys.readouterr().err

    def test_a_bad_registry_exits_2_with_the_reasons(self, tmp_path, capsys):
        (tmp_path / "c").mkdir()
        bad = tmp_path / "registry.json"
        bad.write_text(json.dumps({**VALID_REGISTRY, "leases": {"L-0001": {"property_id": "P-404"}}}))
        assert corpus.main(["--corpus", str(tmp_path / "c"), "--registry", str(bad)]) == 2
        err = capsys.readouterr().err
        assert "INVALID REGISTRY" in err and "unknown property 'P-404'" in err

    def test_the_shipped_corpus_is_valid(self):
        """Gate for slice 5: today the tree is empty; once documents exist this validates them."""
        report = validate_corpus(SHIPPED_CORPUS, load_registry(SHIPPED_REGISTRY))
        assert report.problems == []
