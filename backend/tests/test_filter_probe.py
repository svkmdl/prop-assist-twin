"""The live S3 Vectors filter probe (ADR 0002 verification plan), exercised with a fake client.

No AWS here: ``FakeVectors`` can be faithful to the assumed filter semantics, or deliberately
wrong (too lax, too strict, an unsupported operator, a store that cannot delete), to prove the
tool reports each failure instead of passing silently. The real run is owner-only.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from common import filter_probe
from common.access import FILTER_OPERATORS_USED, ROLE_STAFF, StaticScopeResolver
from common.filter_probe import KEY_PREFIX, probe_documents, run_probe
from common.registry import load_registry
from filter_eval import matches

BACKEND_DIR = Path(__file__).resolve().parents[1]
REGISTRY = load_registry(BACKEND_DIR / "data" / "registry.json")
DIM = 8


class FakeVectors:
    """In-memory stand-in for the boto3 ``s3vectors`` client."""

    def __init__(self, *, mode="faithful", unsupported=(), fail_put_on=None, fail_delete=False,
                 keep_on_delete=False, floats=False, arrays_match=True):
        self.mode, self.unsupported, self.arrays_match = mode, set(unsupported), arrays_match
        self.fail_put_on, self.fail_delete, self.keep_on_delete, self.floats = fail_put_on, fail_delete, keep_on_delete, floats
        self.store: dict = {}
        self.deleted: list = []

    def put_vectors(self, vectorBucketName, indexName, vectors):
        for v in vectors:
            if self.fail_put_on and v["key"].endswith(self.fail_put_on):
                raise RuntimeError("put refused")
            self.store[v["key"]] = dict(v["metadata"])

    def delete_vectors(self, vectorBucketName, indexName, keys):
        if self.fail_delete:
            raise RuntimeError("delete refused")
        self.deleted.extend(keys)
        if not self.keep_on_delete:
            for key in keys:
                self.store.pop(key, None)

    def query_vectors(self, vectorBucketName, indexName, queryVector, topK, returnDistance,
                      returnMetadata, filter=None):
        used = filter_probe._operators(filter)
        if used & self.unsupported:
            raise RuntimeError(f"ValidationException: unsupported operator {sorted(used & self.unsupported)}")
        hits = []
        for key, metadata in self.store.items():
            # An index that stopped matching arrays by element would see lists as plain mismatches.
            seen = metadata if self.arrays_match else {
                k: (object() if isinstance(v, list) else v) for k, v in metadata.items()
            }
            if self.mode == "faithful":
                ok = matches(filter, seen)
            elif self.mode == "lax":  # honours only the probe_run clause: ignores the scope
                ok = metadata.get("probe_run") == filter["$and"][-1]["probe_run"]["$eq"]
            else:  # "strict": drops everything that is not a company-wide document
                ok = matches(filter, metadata) and metadata.get("scope_type") == "company"
            if ok:
                md = dict(metadata)
                if self.floats:
                    md = {k: (float(v) if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in md.items()}
                hits.append({"key": key, "distance": 0.0, "metadata": md})
        return {"vectors": hits[:topK]}


def run(client, **kw):
    return run_probe(client, bucket="vb", index="ix", dimension=DIM, registry=REGISTRY, run_id="r1", **kw)


class TestProbeDocuments:
    def test_every_audience_and_scope_is_covered_and_edge_cases_are_malformed(self):
        probes = probe_documents()
        well_formed = {n for n in probes if not n.startswith("edge-")}
        assert {(probes[n]["audience"], probes[n]["scope_type"]) for n in well_formed} == {
            ("renter", "company"), ("staff", "company"), ("renter", "property"), ("staff", "property"), ("renter", "lease"),
        }
        assert {n for n in probes if n.startswith("edge-")} == {
            "edge-missing-effective-to", "edge-dates-as-strings", "edge-dates-as-bools",
            "edge-audience-as-list", "edge-audience-as-list-with-staff", "edge-scope-type-as-list",
            "edge-lease-id-as-number", "edge-lease-id-as-list", "edge-integral-float-dates",
        }
        assert filter_probe.ARRAY_VALUED_PROBES <= set(probes)

    def test_edge_cases_are_authorized_only_where_intended(self):
        probes = probe_documents()
        resolver = StaticScopeResolver(REGISTRY, today=lambda: date(2027, 3, 1))

        def authorized_edges(principal):
            scope = resolver.resolve(principal)
            return {n for n, m in probes.items() if n.startswith("edge-") and filter_probe.is_authorized(m, scope)}

        assert authorized_edges("T001") == {"edge-integral-float-dates"}
        # Staff see everything in a known schema, so also the lease document with a mistyped id.
        assert authorized_edges("admin") == {
            "edge-integral-float-dates", "edge-lease-id-as-number", "edge-lease-id-as-list",
        }


class TestRunProbe:
    def test_a_faithful_store_passes_and_leaves_nothing_behind(self):
        client = FakeVectors()
        report = run(client)
        assert report.problems == [] and report.setup_error is None and report.cleanup_error is None
        assert client.store == {}  # every probe deleted
        assert len(client.deleted) == len(probe_documents())
        assert all(k.startswith(f"{KEY_PREFIX}r1/") for k in client.deleted)
        assert report.operators_exercised == FILTER_OPERATORS_USED
        # 5 principals x 3 dates
        assert len(report.results) == (len(REGISTRY.staff) + len(REGISTRY.tenant_leases)) * 3

    def test_array_valued_vectors_admitted_by_the_filter_are_a_note_not_a_failure(self):
        """The live finding (dev, 2026-11-23): S3 Vectors matches arrays by any element."""
        report = run(FakeVectors())  # faithful, including array semantics
        assert report.problems == []
        names = {n.split(":")[0] for n in report.notes}
        assert names == {
            "edge-audience-as-list", "edge-audience-as-list-with-staff",
            "edge-scope-type-as-list", "edge-lease-id-as-list",
        }
        assert all("is_authorized drops it" in n and "cannot occur today" in n for n in report.notes)
        # They really were returned although outside the scope, for renters and for staff.
        extra = {(r.principal, n) for r in report.results for n in r.array_admitted}
        assert ("T001", "edge-audience-as-list") in extra and ("admin", "edge-audience-as-list") in extra
        assert ("T001", "edge-lease-id-as-list") in extra and ("T003", "edge-lease-id-as-list") not in extra

    def test_every_array_admission_is_one_the_per_hit_check_rejects(self):
        """The backstop the note relies on: is_authorized refuses all of them, for every principal."""
        probes = probe_documents()
        for principal in ("T001", "T002", "T003", "T004"):
            scope = StaticScopeResolver(REGISTRY, today=lambda: date(2027, 3, 1)).resolve(principal)
            for name in filter_probe.ARRAY_VALUED_PROBES:
                assert filter_probe.is_authorized(probes[name], scope) is False, (principal, name)

    def test_an_index_that_stops_matching_arrays_is_tolerated_too(self):
        report = run(FakeVectors(arrays_match=False))
        assert report.problems == [] and report.notes == []

    def test_a_real_leak_beside_the_array_cases_is_still_reported_and_named(self):
        report = run(FakeVectors(mode="lax"))
        lax = " ".join(p for p in report.problems if "FILTER TOO LAX" in p)
        assert "sop-water" in lax and "lease-l2" in lax
        for array_name in filter_probe.ARRAY_VALUED_PROBES:  # not blamed: they have their own note
            assert array_name not in lax

    def test_notes_and_array_admissions_never_include_a_real_leak(self):
        """Only list-valued probes may be called 'array-admitted'; a leak must not hide in a note."""
        report = run(FakeVectors(mode="lax"))
        for r in report.results:
            assert set(r.array_admitted) <= filter_probe.ARRAY_VALUED_PROBES
        assert {n.split(":")[0] for n in report.notes} <= filter_probe.ARRAY_VALUED_PROBES
        assert not any("sop-water" in n or "lease-l2" in n for n in report.notes)

    def test_the_expected_sets_are_what_the_scopes_allow(self):
        report = run(FakeVectors())
        by = {(r.principal, r.as_of): set(r.expected) for r in report.results}
        # T001 on 2027-03-01: company v2, own lease documents, P-001 house rules, never staff/other leases
        t001 = by[("T001", "2027-03-01")]
        assert {"repair-v2", "rules-p1", "lease-l1", "utility-l1", "edge-integral-float-dates"} == t001
        assert not t001 & {"sop-water", "contractor-policy", "vendor-p1", "lease-l2", "lease-l3", "rules-p2"}
        assert "repair-v1" in by[("T001", "2026-12-31")] and "repair-v2" not in by[("T001", "2026-12-31")]
        assert "sop-water" in by[("admin", "2027-03-01")]

    def test_a_store_that_ignores_the_filter_is_reported_as_a_leak(self):
        report = run(FakeVectors(mode="lax"))
        leaks = [p for p in report.problems if "FILTER TOO LAX" in p]
        assert leaks and any("T001" in p and "sop-water" in p for p in leaks)

    def test_a_store_that_over_filters_is_reported_as_too_strict(self):
        report = run(FakeVectors(mode="strict"))
        assert any("filter too strict" in p and "lease-l1" in p for p in report.problems)
        assert not any("FILTER TOO LAX" in p for p in report.problems)

    @pytest.mark.parametrize("operator", sorted(FILTER_OPERATORS_USED - {"$and"}))
    def test_an_unsupported_operator_is_reported_not_swallowed(self, operator):
        report = run(FakeVectors(unsupported={operator}))
        assert any("query failed" in p and operator in p for p in report.problems)

    def test_numeric_types_as_returned_are_reported(self):
        ints = run(FakeVectors()).number_types
        floats = run(FakeVectors(floats=True)).number_types
        assert {ints["repair-v2.effective_from"], ints["repair-v2.effective_to"]} == {"int"}
        assert floats["repair-v2.effective_from"] == "float"
        # integral floats must still be authorized and found, in either representation
        assert run(FakeVectors(floats=True)).problems == []

    def test_a_failed_write_stops_the_run_but_still_cleans_up(self):
        client = FakeVectors(fail_put_on="lease-l2")
        report = run(client)
        assert "put refused" in report.setup_error and report.results == []
        assert client.store == {}  # whatever was written before the failure is gone

    def test_a_failed_delete_is_reported_with_the_keys_to_remove(self):
        client = FakeVectors(fail_delete=True)
        report = run(client)
        assert report.cleanup_error.startswith("delete failed")
        assert len(report.leftover_keys) == len(probe_documents())
        assert any("delete these probe keys by hand" in p for p in report.problems)

    def test_a_delete_that_silently_keeps_the_vectors_is_caught(self):
        report = run(FakeVectors(keep_on_delete=True))
        assert report.cleanup_error == "probe vectors are still present after delete"
        assert report.leftover_keys and all(k.startswith(f"{KEY_PREFIX}r1/") for k in report.leftover_keys)

    def test_an_unverifiable_cleanup_is_reported_honestly(self):
        report = run(FakeVectors(unsupported={"$eq"}))
        assert "verification query failed" in report.cleanup_error
        assert any("check the index for keys starting" in p for p in report.problems)

    def test_runs_do_not_see_each_others_probes(self):
        client = FakeVectors()
        client.store[f"{KEY_PREFIX}other/lease-l2"] = {**probe_documents()["lease-l2"], "probe_run": "other"}
        report = run(client)
        assert report.problems == []
        assert f"{KEY_PREFIX}other/lease-l2" in client.store  # not touched, not returned

    def test_default_run_ids_are_unique(self):
        a = run_probe(FakeVectors(), bucket="b", index="i", dimension=DIM, registry=REGISTRY)
        b = run_probe(FakeVectors(), bucket="b", index="i", dimension=DIM, registry=REGISTRY)
        assert a.run_id != b.run_id and len(a.run_id) == 12


class TestCli:
    ARGS = ["--bucket", "vb", "--index", "ix", "--registry", str(BACKEND_DIR / "data" / "registry.json"), "--dimension", str(DIM)]

    def patch_boto(self, monkeypatch, client):
        import boto3

        made = []
        monkeypatch.setattr(boto3, "client", lambda *a, **k: made.append((a, k)) or client)
        return made

    def test_refuses_without_yes_and_touches_nothing(self, monkeypatch, capsys):
        client = FakeVectors()
        made = self.patch_boto(monkeypatch, client)
        assert filter_probe.main(self.ARGS) == 2
        assert made == [] and client.store == {} and client.deleted == []
        err = capsys.readouterr().err
        assert "Would write" in err and "owner's approval" in err and "vb" in err and "ix" in err

    def test_a_confirmed_run_passes_and_writes_the_json_report(self, monkeypatch, tmp_path, capsys):
        client = FakeVectors()
        made = self.patch_boto(monkeypatch, client)
        out = tmp_path / "probe.json"
        assert filter_probe.main([*self.ARGS, "--yes", "--output", str(out), "--region", "eu-west-1"]) == 0
        assert made[0][0] == ("s3vectors",) and made[0][1] == {"region_name": "eu-west-1"}
        report = json.loads(out.read_text())
        assert report["problems"] == [] and report["operators_exercised"] == sorted(FILTER_OPERATORS_USED)
        assert report["notes"] and any(n.startswith("edge-audience-as-list:") for n in report["notes"])
        shown = capsys.readouterr().out
        assert "filter probe: passed" in shown and "NOTE: edge-audience-as-list:" in shown
        assert " ok* " in shown and "MISMATCH" not in shown
        assert client.store == {}

    def test_a_leak_exits_1(self, monkeypatch, capsys):
        self.patch_boto(monkeypatch, FakeVectors(mode="lax"))
        assert filter_probe.main([*self.ARGS, "--yes"]) == 1
        captured = capsys.readouterr()
        assert "FILTER TOO LAX" in captured.err and "FAILED" in captured.out
        assert "MISMATCH" in captured.out and " ok* " not in captured.out  # a leak is never shown as "ok*"

    def test_a_write_failure_exits_2(self, monkeypatch):
        self.patch_boto(monkeypatch, FakeVectors(fail_put_on="rules-p1"))
        assert filter_probe.main([*self.ARGS, "--yes"]) == 2

    def test_a_bad_registry_exits_2_before_any_aws_call(self, monkeypatch, tmp_path, capsys):
        made = self.patch_boto(monkeypatch, FakeVectors())
        bad = tmp_path / "r.json"
        bad.write_text("{}")
        assert filter_probe.main(["--bucket", "vb", "--index", "ix", "--registry", str(bad), "--yes"]) == 2
        assert made == [] and "INVALID REGISTRY" in capsys.readouterr().err

    def test_the_staff_principal_is_part_of_the_matrix(self):
        assert REGISTRY.staff and any(r.principal in REGISTRY.staff for r in run(FakeVectors()).results)
        assert ROLE_STAFF == "staff"
