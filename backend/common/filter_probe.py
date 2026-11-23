"""Live check of the S3 Vectors filter behaviour the access scopes rely on.

ADR 0002, verification plan item 3. The unit tests interpret filters with an
in-memory evaluator that *assumes* how S3 Vectors behaves; this tool confirms it
against a real index, because a wrong assumption silently changes who can see
what.

It answers four questions, for every principal in the registry on several dates:

1. Does the index accept every operator in ``FILTER_OPERATORS_USED``
   (``$and``, ``$or``, ``$eq``, ``$in``, ``$lte``, ``$gte``), nested as built?
2. Does each filter return exactly the documents ``is_authorized`` allows
   (no more: a leak; no fewer: documents wrongly hidden)?
3. Do malformed vectors (missing key, strings or booleans where numbers belong)
   match nothing? **Array-valued metadata is a known exception**: a live run on
   ``dev`` (2026-11-23) showed that S3 Vectors matches an array when ANY element
   matches, so ``audience: ["renter"]`` satisfies ``audience = renter``. The
   ingestion pipeline only writes plain strings, so such vectors cannot occur
   today, and the per-hit ``is_authorized`` check drops them (it rejects any
   non-string where a string belongs). The probe keeps testing for it and reports
   it as a NOTE (the filter admits it, the in-code check must drop it), not a
   failure; any *other* unexpected result is still ``FILTER TOO LAX``.
4. Are numbers returned as integers or floats?

**This WRITES.** It puts about twenty probe vectors into the named index and
deletes them again (also on failure). Probe keys start with ``__filter-probe__/``
and carry a unique ``probe_run`` value, so queries only ever see their own
probes. While they exist the probes look like ordinary documents (they must, to
test the real filters): run it on ``dev`` when nobody is chatting. It takes
seconds and changes nothing else. Run it only with the owner's approval::

    cd backend
    uv run python -m common.filter_probe --bucket <vector-bucket> --index <index> --yes

Exit codes: 0 every check passed, 1 a check failed (the report says which; a
"too lax" finding means documents outside a scope were returned), 2 could not
run (no ``--yes``, bad registry, AWS error while writing).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from common import vector_store
from common.access import FILTER_OPERATORS_USED, Scope, StaticScopeResolver, build_vector_filter, is_authorized
from common.doc_keys import FrontMatter, build_document_metadata, parse_document_key
from common.registry import Registry, RegistryError, load_registry

KEY_PREFIX = "__filter-probe__/"

# The same realistic universe the unit tests use: every audience and scope, and a
# business version that expires so the date window is exercised.
_DOCUMENTS = {
    "repair-v1": ("incoming/renters/company/policies/repair-policy/v1.md", "2026-01-01", "2026-12-31"),
    "repair-v2": ("incoming/renters/company/policies/repair-policy/v2.md", "2027-01-01", None),
    "sop-water": ("incoming/staff/company/sops/water-damage/v1.md", "2026-01-01", None),
    "contractor-policy": ("incoming/staff/company/policies/contractor-policy/v1.md", "2026-01-01", None),
    "rules-p1": ("incoming/renters/properties/P-001/policies/house-rules/v1.md", "2026-01-01", None),
    "rules-p2": ("incoming/renters/properties/P-002/policies/house-rules/v1.md", "2026-01-01", None),
    "vendor-p1": ("incoming/staff/properties/P-001/sops/heating-vendor/v1.md", "2026-01-01", None),
    "lease-l1": ("incoming/renters/leases/L-0001/contracts/lease-contract/v1.md", "2026-01-01", None),
    "utility-l1": ("incoming/renters/leases/L-0001/statements/utility-2026/v1.md", "2026-01-01", None),
    "lease-l2": ("incoming/renters/leases/L-0002/contracts/lease-contract/v1.md", "2026-01-01", None),
    "lease-l3": ("incoming/renters/leases/L-0003/contracts/lease-contract/v1.md", "2026-01-01", None),
}
PROBE_DATES = (date(2027, 3, 1), date(2027, 1, 1), date(2026, 12, 31))

# Probes whose list-valued field S3 Vectors may match by "any element" (see the module
# docstring). A filter returning one of these is expected and backstopped by
# is_authorized; returning anything else outside the scope is a failure.
ARRAY_VALUED_PROBES = frozenset(
    {
        "edge-audience-as-list",
        "edge-audience-as-list-with-staff",
        "edge-scope-type-as-list",
        "edge-lease-id-as-list",
    }
)


def probe_documents() -> Dict[str, Dict[str, Any]]:
    """Name -> vector metadata for every probe (well-formed first, then edge cases)."""
    probes: Dict[str, Dict[str, Any]] = {}
    for name, (key, frm, to) in _DOCUMENTS.items():
        front = FrontMatter("Probe", "de", date.fromisoformat(frm), date.fromisoformat(to) if to else None)
        probes[name] = build_document_metadata(parse_document_key(key), front)

    base = dict(probes["repair-v2"])
    lease = dict(probes["lease-l1"])
    # Edge cases. For renters only the integral-float one is authorized; staff also see the
    # documents whose scope id is mistyped or a list (a known audience/scope in its date
    # window). The rest must match nothing, except that list-valued fields may be ADMITTED
    # by the filter (ARRAY_VALUED_PROBES) and are then dropped by the per-hit check.
    probes["edge-missing-effective-to"] = {k: v for k, v in base.items() if k != "effective_to"}
    probes["edge-dates-as-strings"] = {**base, "effective_from": "20270101", "effective_to": "99991231"}
    probes["edge-dates-as-bools"] = {**base, "effective_from": True, "effective_to": True}
    probes["edge-audience-as-list"] = {**base, "audience": ["renter"]}
    probes["edge-audience-as-list-with-staff"] = {**base, "audience": ["renter", "staff"]}
    probes["edge-scope-type-as-list"] = {**base, "scope_type": ["company"]}
    probes["edge-lease-id-as-list"] = {**lease, "lease_id": ["L-0001", "L-0002"]}
    probes["edge-lease-id-as-number"] = {**lease, "lease_id": 1}
    probes["edge-integral-float-dates"] = {**base, "effective_from": 20260101.0, "effective_to": 99991231.0}
    return probes


def _operators(node: Any) -> Set[str]:
    found: Set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key.startswith("$"):
                found.add(key)
            found |= _operators(value)
    elif isinstance(node, list):
        for item in node:
            found |= _operators(item)
    return found


@dataclass
class QueryResult:
    principal: str
    as_of: str
    expected: List[str]
    returned: List[str]
    error: Optional[str] = None

    @property
    def array_admitted(self) -> List[str]:
        """Returned beyond the scope, but only list-valued probes the per-hit check drops."""
        return sorted((set(self.returned) - set(self.expected)) & ARRAY_VALUED_PROBES)


@dataclass
class ProbeReport:
    run_id: str
    results: List[QueryResult] = field(default_factory=list)
    operators_exercised: Set[str] = field(default_factory=set)
    number_types: Dict[str, str] = field(default_factory=dict)
    setup_error: Optional[str] = None
    cleanup_error: Optional[str] = None
    leftover_keys: List[str] = field(default_factory=list)

    @property
    def problems(self) -> List[str]:
        found: List[str] = []
        for r in self.results:
            label = f"{r.principal} on {r.as_of}"
            if r.error:
                found.append(f"{label}: query failed: {r.error}")
                continue
            lax = sorted((set(r.returned) - set(r.expected)) - ARRAY_VALUED_PROBES)
            strict = sorted(set(r.expected) - set(r.returned))
            if lax:
                found.append(f"{label}: FILTER TOO LAX, returned documents outside the scope: {lax}")
            if strict:
                found.append(f"{label}: filter too strict, authorized documents not returned: {strict}")
        if not self.setup_error:
            missing = sorted(FILTER_OPERATORS_USED - self.operators_exercised)
            if missing:
                found.append(f"operators never exercised by a successful query: {missing}")
        if self.cleanup_error:
            tail = (
                f"delete these probe keys by hand: {self.leftover_keys}"
                if self.leftover_keys
                else f"check the index for keys starting {KEY_PREFIX}{self.run_id}/"
            )
            found.append(f"cleanup problem ({self.cleanup_error}); {tail}")
        return found

    @property
    def notes(self) -> List[str]:
        """Informational findings that are not failures."""
        admitted: Dict[str, int] = {}
        for r in self.results:
            for name in r.array_admitted:
                admitted[name] = admitted.get(name, 0) + 1
        return [
            f"{name}: the filter returned it in {count} quer{'y' if count == 1 else 'ies'} although it is outside the scope "
            "(S3 Vectors matches array-valued metadata by any element); is_authorized drops it. "
            "Ingestion never writes lists, so this cannot occur today."
            for name, count in sorted(admitted.items())
        ]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "queries": [vars(r) for r in self.results],
            "operators_exercised": sorted(self.operators_exercised),
            "number_types_returned": self.number_types,
            "setup_error": self.setup_error,
            "cleanup_error": self.cleanup_error,
            "leftover_keys": self.leftover_keys,
            "problems": self.problems,
            "notes": self.notes,
        }


def _name(key: str, run_id: str) -> str:
    return key[len(f"{KEY_PREFIX}{run_id}/"):]


def run_probe(
    client: Any,
    *,
    bucket: str,
    index: str,
    dimension: int,
    registry: Registry,
    run_id: Optional[str] = None,
    dates: Sequence[date] = PROBE_DATES,
) -> ProbeReport:
    """Write the probes, query every scope, compare with ``is_authorized``, clean up."""
    report = ProbeReport(run_id=run_id or uuid.uuid4().hex[:12])
    probes = probe_documents()
    keys = {name: f"{KEY_PREFIX}{report.run_id}/{name}" for name in probes}
    vector = [1.0] + [0.0] * (dimension - 1)
    only_this_run = {"probe_run": {"$eq": report.run_id}}

    try:
        try:
            for name, metadata in probes.items():
                vector_store.put_vector(
                    client=client, bucket=bucket, index=index, vector_id=keys[name], embedding=vector,
                    metadata={**metadata, "probe_run": report.run_id, "chunk_text": "filter probe, ignore"},
                )
        except Exception as exc:  # noqa: BLE001 - reported, then cleaned up
            report.setup_error = f"{type(exc).__name__}: {exc}"
            return report

        principals = sorted(registry.staff) + sorted(registry.tenant_leases)
        for as_of in dates:
            resolver = StaticScopeResolver(registry, today=lambda day=as_of: day)
            for principal in principals:
                scope: Scope = resolver.resolve(principal)
                expected = sorted(n for n, m in probes.items() if is_authorized(m, scope))
                query_filter = {"$and": [build_vector_filter(scope), only_this_run]}
                result = QueryResult(principal, as_of.isoformat(), expected, [])
                try:
                    hits = vector_store.query_vectors(
                        client=client, bucket=bucket, index=index, embedding=vector,
                        top_k=len(probes), metadata_filter=query_filter,
                    )
                except Exception as exc:  # noqa: BLE001
                    result.error = f"{type(exc).__name__}: {exc}"
                else:
                    report.operators_exercised |= _operators(query_filter)
                    result.returned = sorted(_name(h["key"], report.run_id) for h in hits)
                    for hit in hits:
                        for fld in ("effective_from", "effective_to"):
                            value = (hit.get("metadata") or {}).get(fld)
                            if value is not None:
                                report.number_types[f"{_name(hit['key'], report.run_id)}.{fld}"] = type(value).__name__
                report.results.append(result)
    finally:
        _cleanup(client, bucket, index, list(keys.values()), only_this_run, vector, report)
    return report


def _cleanup(
    client: Any, bucket: str, index: str, keys: List[str], only_this_run: Dict[str, Any],
    vector: List[float], report: ProbeReport,
) -> None:
    """Delete the probes, then look for survivors (a failed delete must never go unnoticed)."""
    try:
        vector_store.delete_vectors(client=client, bucket=bucket, index=index, vector_ids=keys)
    except Exception as exc:  # noqa: BLE001
        report.cleanup_error = f"delete failed: {type(exc).__name__}: {exc}"
        report.leftover_keys = keys
        return
    try:
        survivors = vector_store.query_vectors(
            client=client, bucket=bucket, index=index, embedding=vector,
            top_k=len(keys), metadata_filter=only_this_run,
        )
    except Exception as exc:  # noqa: BLE001
        report.cleanup_error = f"deleted, but the verification query failed: {type(exc).__name__}: {exc}"
        return
    report.leftover_keys = sorted(h["key"] for h in survivors)
    if report.leftover_keys:
        report.cleanup_error = "probe vectors are still present after delete"


def _print(report: ProbeReport) -> None:
    print(f"filter probe run {report.run_id}")
    if report.setup_error:
        print(f"  could not write probes: {report.setup_error}")
    for r in report.results:
        if r.error:
            status = "ERROR"
        elif r.returned == r.expected:
            status = "ok"
        elif set(r.expected) <= set(r.returned) and set(r.returned) - set(r.expected) == set(r.array_admitted):
            status = "ok*"  # only list-valued probes beyond the scope: see the NOTE below
        else:
            status = "MISMATCH"
        print(f"  {status:8} {r.principal:6} {r.as_of}  expected {len(r.expected):2}  returned {len(r.returned):2}")
    print(f"  operators exercised: {sorted(report.operators_exercised)}")
    types = sorted(set(report.number_types.values()))
    print(f"  numbers returned as: {types or 'n/a'}")
    for note in report.notes:
        print(f"  NOTE: {note}")
    for problem in report.problems:
        print(f"  PROBLEM: {problem}", file=sys.stderr)
    print("filter probe: " + ("passed" if not report.problems and not report.setup_error else "FAILED"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verify S3 Vectors filter behaviour against a real index (writes probe vectors).")
    parser.add_argument("--bucket", required=True, help="vector bucket name")
    parser.add_argument("--index", required=True, help="vector index name")
    parser.add_argument("--dimension", type=int, default=384, help="index dimension (default 384)")
    parser.add_argument("--region", default=os.getenv("DEFAULT_AWS_REGION", "eu-central-1"))
    parser.add_argument("--registry", default="data/registry.json", type=Path)
    parser.add_argument("--output", type=Path, help="also write the report as JSON")
    parser.add_argument("--yes", action="store_true", help="confirm that probe vectors may be written to the index")
    args = parser.parse_args(argv)

    probes = probe_documents()
    if not args.yes:
        print(
            f"Would write {len(probes)} probe vectors (keys {KEY_PREFIX}<run>/...) to index "
            f"{args.index!r} in vector bucket {args.bucket!r} ({args.region}), query them, and delete them again.\n"
            "Re-run with --yes, with the owner's approval, when nobody is chatting.",
            file=sys.stderr,
        )
        return 2
    try:
        registry = load_registry(args.registry)
    except RegistryError as exc:
        print(f"INVALID REGISTRY {args.registry}: {'; '.join(exc.problems)}", file=sys.stderr)
        return 2

    import boto3  # imported late: nothing above needs AWS

    client = boto3.client("s3vectors", region_name=args.region)
    report = run_probe(client, bucket=args.bucket, index=args.index, dimension=args.dimension, registry=registry)
    _print(report)
    if args.output:
        args.output.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    if report.setup_error:
        return 2
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
