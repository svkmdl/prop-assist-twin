"""Access scopes: filter builder, per-hit check and resolver (ADR 0002, slice 2).

Pure unit tests, no AWS. The in-memory ``matches`` evaluator interprets the
filter expression the way S3 Vectors is *assumed* to (a leaf on a missing key or
a type mismatch does not match). That assumption, and the operator list, must
still be confirmed against a real index in ``dev`` (ADR 0002 verification plan);
nothing here proves S3 Vectors behaviour.
"""
from __future__ import annotations

import copy
import itertools
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from common import access
from common.access import (
    FILTER_OPERATORS_USED,
    ROLE_RENTER,
    ROLE_STAFF,
    Scope,
    StaticScopeResolver,
    UnknownPrincipalError,
    berlin_today,
    build_vector_filter,
    is_authorized,
)
from common.doc_keys import FrontMatter, build_document_metadata, parse_document_key
from common.registry import load_registry, parse_registry
from filter_eval import SEEN_OPERATORS as _SEEN_OPERATORS, matches, walk

BACKEND_DIR = Path(__file__).resolve().parents[1]
REGISTRY = load_registry(BACKEND_DIR / "data" / "registry.json")
RESOLVER = StaticScopeResolver(REGISTRY, today=lambda: date(2027, 3, 1))

# --------------------------------------------------------------------------
# A realistic universe of document vectors, built through the real pipeline.
# --------------------------------------------------------------------------


def vec(key: str, frm: str = "2026-01-01", to: str | None = None) -> dict:
    fm = FrontMatter("Titel", "de", date.fromisoformat(frm), date.fromisoformat(to) if to else None)
    return build_document_metadata(parse_document_key(key), fm)


UNIVERSE = {
    "repair-v1": vec("incoming/renters/company/policies/repair-policy/v1.md", "2026-01-01", "2026-12-31"),
    "repair-v2": vec("incoming/renters/company/policies/repair-policy/v2.md", "2027-01-01"),
    "sop-water": vec("incoming/staff/company/sops/water-damage/v1.md"),
    "contractor-policy": vec("incoming/staff/company/policies/contractor-policy/v1.md"),
    "rules-p1": vec("incoming/renters/properties/P-001/policies/house-rules/v1.md"),
    "rules-p2": vec("incoming/renters/properties/P-002/policies/house-rules/v1.md"),
    "vendor-p1": vec("incoming/staff/properties/P-001/sops/heating-vendor/v1.md"),
    "lease-l1": vec("incoming/renters/leases/L-0001/contracts/lease-contract/v1.md"),
    "utility-l1": vec("incoming/renters/leases/L-0001/statements/utility-2026/v1.md"),
    "lease-l2": vec("incoming/renters/leases/L-0002/contracts/lease-contract/v1.md"),
    "lease-l3": vec("incoming/renters/leases/L-0003/contracts/lease-contract/v1.md"),
}
STAFF_ONLY = {"sop-water", "contractor-policy", "vendor-p1"}

# What each principal must see on 2027-03-01 (repair-v1 expired at the end of 2026).
EXPECTED_2027_03_01 = {
    "T001": {"repair-v2", "rules-p1", "lease-l1", "utility-l1"},
    "T002": {"repair-v2", "rules-p2", "lease-l2"},
    "T003": {"repair-v2", "rules-p1", "lease-l3"},
    "T004": {"repair-v2", "rules-p1", "lease-l3"},
    "admin": set(UNIVERSE) - {"repair-v1"},
}


def visible(scope: Scope) -> set:
    return {name for name, md in UNIVERSE.items() if is_authorized(md, scope)}


class TestScope:
    def test_value_object(self):
        scope = Scope(ROLE_RENTER, ["L-0001", "L-0001"], {"P-001"}, date(2027, 3, 1))
        assert scope.lease_ids == frozenset({"L-0001"}) and isinstance(scope.lease_ids, frozenset)
        assert scope.property_ids == frozenset({"P-001"})
        assert scope.as_of_int == 20270301 and scope.principal == ""

    def test_is_immutable(self):
        scope = Scope(ROLE_STAFF, [], [], date(2027, 3, 1))
        with pytest.raises(Exception):
            scope.role = ROLE_RENTER  # type: ignore[misc]

    @pytest.mark.parametrize("role", ["", "admin", "Staff", "tenant", None])
    def test_unknown_roles_are_rejected(self, role):
        with pytest.raises(ValueError):
            Scope(role, [], [], date(2027, 3, 1))

    @pytest.mark.parametrize("as_of", ["2027-03-01", 20270301, datetime(2027, 3, 1), None])
    def test_as_of_must_be_a_plain_date(self, as_of):
        with pytest.raises(TypeError):
            Scope(ROLE_STAFF, [], [], as_of)


class TestBuildVectorFilter:
    def test_renter_with_leases_and_properties(self):
        scope = Scope(ROLE_RENTER, {"L-0003", "L-0001"}, {"P-001"}, date(2027, 3, 1))
        assert build_vector_filter(scope) == {
            "$and": [
                {"audience": {"$eq": "renter"}},
                {
                    "$or": [
                        {"scope_type": {"$eq": "company"}},
                        {"$and": [{"scope_type": {"$eq": "property"}}, {"property_id": {"$in": ["P-001"]}}]},
                        {
                            "$and": [
                                {"scope_type": {"$eq": "lease"}},
                                {"lease_id": {"$in": ["L-0001", "L-0003"]}},  # sorted: deterministic
                            ]
                        },
                    ]
                },
                {"effective_from": {"$lte": 20270301}},
                {"effective_to": {"$gte": 20270301}},
            ]
        }

    def test_renter_without_leases_gets_only_company_documents_and_no_empty_in(self):
        flt = build_vector_filter(Scope(ROLE_RENTER, [], [], date(2027, 3, 1)))
        assert flt["$and"][1] == {"scope_type": {"$eq": "company"}}  # no one-element $or wrapper
        assert not any(k == "$in" and not v for k, v in walk(flt))
        assert not any(k in ("lease_id", "property_id") for k, _ in walk(flt))

    def test_each_branch_is_dropped_when_its_set_is_empty(self):
        only_lease = build_vector_filter(Scope(ROLE_RENTER, ["L-0001"], [], date(2027, 3, 1)))
        keys = [k for k, _ in walk(only_lease)]
        assert "lease_id" in keys and "property_id" not in keys
        only_property = build_vector_filter(Scope(ROLE_RENTER, [], ["P-001"], date(2027, 3, 1)))
        keys = [k for k, _ in walk(only_property)]
        assert "property_id" in keys and "lease_id" not in keys

    def test_staff_filter_has_no_audience_or_scope_restriction_but_keeps_the_date_window(self):
        flt = build_vector_filter(Scope(ROLE_STAFF, [], [], date(2027, 3, 1)))
        assert flt == {
            "$and": [
                {"audience": {"$in": ["renter", "staff"]}},
                {"scope_type": {"$in": ["company", "lease", "property"]}},
                {"effective_from": {"$lte": 20270301}},
                {"effective_to": {"$gte": 20270301}},
            ]
        }

    def test_staff_ids_are_ignored(self):
        a = build_vector_filter(Scope(ROLE_STAFF, ["L-0001"], ["P-001"], date(2027, 3, 1)))
        b = build_vector_filter(Scope(ROLE_STAFF, [], [], date(2027, 3, 1)))
        assert a == b

    @pytest.mark.parametrize("principal", ["T001", "T002", "T003", "T004"])
    def test_every_renter_filter_pins_the_renter_audience(self, principal):
        """The invariant that keeps staff documents away from tenants."""
        flt = build_vector_filter(RESOLVER.resolve(principal))
        assert flt["$and"][0] == {"audience": {"$eq": "renter"}}
        assert "staff" not in json.dumps(flt)

    def test_the_filter_never_refers_to_a_tenant_id(self):
        for principal in ("T001", "admin"):
            assert "tenant_id" not in json.dumps(build_vector_filter(RESOLVER.resolve(principal)))

    def test_only_the_documented_operators_are_used(self):
        used = set()
        for principal in ("T001", "T002", "admin"):
            flt = build_vector_filter(RESOLVER.resolve(principal))
            used |= {k for k, _ in walk(flt) if k.startswith("$")}
        assert used <= FILTER_OPERATORS_USED
        assert used == FILTER_OPERATORS_USED  # and none of them is dead

    def test_the_filter_does_not_mutate_or_share_state_between_calls(self):
        scope = RESOLVER.resolve("T001")
        first = build_vector_filter(scope)
        snapshot = copy.deepcopy(first)
        first["$and"].clear()
        assert build_vector_filter(scope) == snapshot


class TestVisibility:
    @pytest.mark.parametrize("principal", sorted(EXPECTED_2027_03_01))
    def test_each_principal_sees_exactly_the_expected_documents(self, principal):
        scope = RESOLVER.resolve(principal)
        assert visible(scope) == EXPECTED_2027_03_01[principal]
        assert {n for n, md in UNIVERSE.items() if matches(build_vector_filter(scope), md)} == EXPECTED_2027_03_01[principal]

    @pytest.mark.parametrize("tenant", ["T001", "T002", "T003", "T004"])
    def test_no_renter_ever_sees_staff_documents(self, tenant):
        assert not visible(RESOLVER.resolve(tenant)) & STAFF_ONLY

    def test_cross_lease_isolation(self):
        assert "lease-l2" not in visible(RESOLVER.resolve("T001"))
        assert "lease-l1" not in visible(RESOLVER.resolve("T002"))
        assert "lease-l3" not in visible(RESOLVER.resolve("T001"))  # same property, other lease
        assert "utility-l1" not in visible(RESOLVER.resolve("T003"))

    def test_cross_property_isolation(self):
        assert "rules-p2" not in visible(RESOLVER.resolve("T001"))
        assert "rules-p1" not in visible(RESOLVER.resolve("T002"))

    def test_co_tenants_see_the_same_documents(self):
        assert visible(RESOLVER.resolve("T003")) == visible(RESOLVER.resolve("T004"))

    def test_staff_see_renter_documents_too(self):
        seen = visible(RESOLVER.resolve("admin"))
        assert {"lease-l1", "lease-l2", "lease-l3", "rules-p1", "rules-p2"} <= seen and STAFF_ONLY <= seen

    @pytest.mark.parametrize(
        "day,expected",
        [
            (date(2025, 12, 31), set()),  # before anything takes effect
            (date(2026, 1, 1), {"repair-v1"}),  # effective_from is inclusive
            (date(2026, 12, 31), {"repair-v1"}),  # effective_to is inclusive
            (date(2027, 1, 1), {"repair-v2"}),  # the next version starts the very next day
            (date(2099, 1, 1), {"repair-v2"}),  # open-ended stays in force
        ],
    )
    def test_effective_date_boundaries(self, day, expected):
        scope = Scope(ROLE_RENTER, [], [], day)
        got = {n for n, md in UNIVERSE.items() if n.startswith("repair") and is_authorized(md, scope)}
        assert got == expected
        in_filter = {n for n, md in UNIVERSE.items() if n.startswith("repair") and matches(build_vector_filter(scope), md)}
        assert in_filter == expected

    def test_exactly_one_version_is_in_force_on_any_day_when_windows_are_adjacent(self):
        for day in (date(2026, 6, 15), date(2026, 12, 31), date(2027, 1, 1), date(2030, 5, 5)):
            scope = Scope(ROLE_RENTER, [], [], day)
            assert sum(is_authorized(UNIVERSE[n], scope) for n in ("repair-v1", "repair-v2")) == 1


# Defects that hide a vector from EVERYONE, staff included: the schema itself is broken.
MALFORMED = {
    "empty": {},
    "none": None,
    "string": "not metadata",
    "list": [],
    "legacy-tenant-vector": {"tenant_id": "T001", "title": "FAQ", "chunk_text": "x", "source_key": "incoming/T001/faq.md"},
    "no-dates": {k: v for k, v in UNIVERSE["repair-v2"].items() if not k.startswith("effective")},
    "no-effective-to": {k: v for k, v in UNIVERSE["repair-v2"].items() if k != "effective_to"},
    "dates-as-strings": {**UNIVERSE["repair-v2"], "effective_from": "20270101", "effective_to": "99991231"},
    "dates-as-bools": {**UNIVERSE["repair-v2"], "effective_from": True, "effective_to": True},
    "dates-as-none": {**UNIVERSE["repair-v2"], "effective_from": None},
    "nan-date": {**UNIVERSE["repair-v2"], "effective_to": float("nan")},
    "inverted-window": {**UNIVERSE["repair-v2"], "effective_from": 20271231, "effective_to": 20270101},
    "no-audience": {k: v for k, v in UNIVERSE["repair-v2"].items() if k != "audience"},
    "unknown-audience": {**UNIVERSE["repair-v2"], "audience": "public"},
    "audience-case": {**UNIVERSE["repair-v2"], "audience": "Renter"},
    "unknown-scope": {**UNIVERSE["repair-v2"], "scope_type": "unit"},
}

# Defects only in the scope id. The document still has a known audience, scope and a valid
# window, so staff (who see everything) still get it, but no renter can: a renter needs a
# matching property_id or lease_id.
SCOPE_ID_DEFECTS = {
    "lease-without-id": {k: v for k, v in UNIVERSE["lease-l1"].items() if k != "lease_id"},
    "lease-id-wrong-type": {**UNIVERSE["lease-l1"], "lease_id": 1},
    "property-without-id": {k: v for k, v in UNIVERSE["rules-p1"].items() if k != "property_id"},
    "property-doc-with-only-a-lease-id": {
        **{k: v for k, v in UNIVERSE["rules-p1"].items() if k != "property_id"},
        "lease_id": "L-0001",
    },
}


# ARRAY-VALUED metadata. Verified live against the dev index (2026-11-23, `common.filter_probe`):
# S3 Vectors matches an array when ANY element matches, so ``audience: ["renter"]`` satisfies
# ``audience = renter``. Ingestion only ever writes plain strings, so these cannot occur
# today; they are why the per-hit check is load-bearing rather than belt and braces: the
# filter ADMITS them, `is_authorized` must DROP them.
#
# Lists where the audience or scope type belongs: rejected for everyone, staff included.
ARRAY_AUDIENCE_OR_SCOPE = {
    "audience-list": {**UNIVERSE["repair-v2"], "audience": ["renter"]},
    "audience-list-hiding-staff": {**UNIVERSE["repair-v2"], "audience": ["renter", "staff"]},
    "scope-list": {**UNIVERSE["repair-v2"], "scope_type": ["company"]},
}
# Lists where a scope id belongs: dropped for every renter (a list could span several
# leases or properties and cross a boundary), still visible to staff like any scope-id defect.
ARRAY_SCOPE_ID = {
    "lease-id-list": {**UNIVERSE["lease-l1"], "lease_id": ["L-0001"]},
    "lease-id-list-spanning-leases": {**UNIVERSE["lease-l1"], "lease_id": ["L-0001", "L-0002"]},
    "property-id-list-spanning-properties": {**UNIVERSE["rules-p1"], "property_id": ["P-001", "P-002"]},
}


class TestArrayValuedMetadata:
    @pytest.mark.parametrize("name", sorted(ARRAY_AUDIENCE_OR_SCOPE))
    @pytest.mark.parametrize("principal", ["T001", "T002", "T003", "T004", "admin"])
    def test_the_filter_admits_them_and_the_check_drops_them(self, name, principal):
        scope = RESOLVER.resolve(principal)
        md = ARRAY_AUDIENCE_OR_SCOPE[name]
        assert matches(build_vector_filter(scope), md) is True  # S3 Vectors any-element semantics
        assert is_authorized(md, scope) is False  # the in-code check is what keeps them out

    @pytest.mark.parametrize("name", sorted(ARRAY_SCOPE_ID))
    @pytest.mark.parametrize("tenant", ["T001", "T002", "T003", "T004"])
    def test_a_list_valued_scope_id_is_dropped_for_every_renter(self, name, tenant):
        assert is_authorized(ARRAY_SCOPE_ID[name], RESOLVER.resolve(tenant)) is False

    @pytest.mark.parametrize("name", sorted(ARRAY_SCOPE_ID))
    def test_staff_still_see_them(self, name):
        scope = RESOLVER.resolve("admin")
        assert is_authorized(ARRAY_SCOPE_ID[name], scope) is True and matches(build_vector_filter(scope), ARRAY_SCOPE_ID[name])

    def test_a_list_spanning_leases_is_admitted_by_the_filter_for_both_leases_and_dropped_by_both_checks(self):
        md = ARRAY_SCOPE_ID["lease-id-list-spanning-leases"]
        for tenant in ("T001", "T002"):  # the list names L-0001 and L-0002
            scope = RESOLVER.resolve(tenant)
            assert matches(build_vector_filter(scope), md) is True
            assert is_authorized(md, scope) is False
        assert matches(build_vector_filter(RESOLVER.resolve("T003")), md) is False  # no overlap with L-0003

    def test_documents_built_by_the_ingestion_pipeline_never_contain_lists(self):
        """The guard that makes the cases above unreachable in practice."""
        for name, md in UNIVERSE.items():
            for field in ("audience", "scope_type", "property_id", "lease_id", "doc_id", "doc_type"):
                assert not isinstance(md.get(field), (list, tuple, set, dict)), (name, field)
            for field in ("effective_from", "effective_to", "doc_version"):
                assert isinstance(md[field], int) and not isinstance(md[field], bool), (name, field)


class TestMalformedMetadataFailsClosed:
    @pytest.mark.parametrize("name", sorted(MALFORMED))
    @pytest.mark.parametrize("principal", ["T001", "admin"])
    def test_is_authorized_rejects_it_for_everyone(self, name, principal):
        assert is_authorized(MALFORMED[name], RESOLVER.resolve(principal)) is False

    @pytest.mark.parametrize("name", sorted(SCOPE_ID_DEFECTS))
    @pytest.mark.parametrize("tenant", ["T001", "T002", "T003", "T004"])
    def test_a_missing_or_mistyped_scope_id_is_never_visible_to_a_renter(self, name, tenant):
        assert is_authorized(SCOPE_ID_DEFECTS[name], RESOLVER.resolve(tenant)) is False

    @pytest.mark.parametrize("name", sorted(SCOPE_ID_DEFECTS))
    def test_staff_still_see_them_so_a_broken_ingest_stays_discoverable(self, name):
        assert is_authorized(SCOPE_ID_DEFECTS[name], RESOLVER.resolve("admin")) is True

    def test_a_non_integral_float_date_is_rejected_by_the_check(self):
        """Stricter than the filter on purpose: the per-hit check is the last line."""
        md = {**UNIVERSE["repair-v2"], "effective_from": 20270101.5}
        assert is_authorized(md, RESOLVER.resolve("T001")) is False

    def test_integral_floats_are_accepted_because_stores_may_return_floats(self):
        md = {**UNIVERSE["repair-v2"], "effective_from": 20270101.0, "effective_to": 99991231.0}
        for principal in ("T001", "admin"):
            scope = RESOLVER.resolve(principal)
            assert is_authorized(md, scope) is True and matches(build_vector_filter(scope), md) is True


class TestFilterAndCheckAgree:
    """Two independent implementations of one rule must not diverge."""

    DAYS = [date(2025, 12, 31), date(2026, 1, 1), date(2026, 12, 31), date(2027, 1, 1), date(2027, 3, 1), date(2099, 1, 1)]
    SCOPES = [
        *(RESOLVER.resolve(p) for p in ("T001", "T002", "T003", "T004", "admin")),
        Scope(ROLE_RENTER, [], [], date(2027, 3, 1), "tenant-without-leases"),
        Scope(ROLE_RENTER, ["L-0001", "L-0002"], ["P-001", "P-002"], date(2027, 3, 1), "two-properties"),
        Scope(ROLE_RENTER, ["L-0001"], [], date(2027, 3, 1), "lease-only"),
        Scope(ROLE_RENTER, [], ["P-001"], date(2027, 3, 1), "property-only"),
    ]

    def test_equal_on_every_well_formed_vector_scope_and_day(self):
        checked = 0
        for scope, day in itertools.product(self.SCOPES, self.DAYS):
            scope = Scope(scope.role, scope.lease_ids, scope.property_ids, day, scope.principal)
            flt = build_vector_filter(scope)
            for name, md in UNIVERSE.items():
                assert matches(flt, md) == is_authorized(md, scope), (scope.principal, day, name)
                checked += 1
        assert checked == len(self.SCOPES) * len(self.DAYS) * len(UNIVERSE)

    @pytest.mark.parametrize("name", sorted(MALFORMED))
    def test_the_filter_never_admits_malformed_vectors_either(self, name):
        for scope in self.SCOPES:
            assert matches(build_vector_filter(scope), MALFORMED[name]) is False, (scope.principal, name)

    @pytest.mark.parametrize("name", sorted(SCOPE_ID_DEFECTS))
    def test_scope_id_defects_agree_too_and_only_staff_are_admitted(self, name):
        for scope in self.SCOPES:
            md = SCOPE_ID_DEFECTS[name]
            in_filter = matches(build_vector_filter(scope), md)
            assert in_filter == is_authorized(md, scope), (scope.principal, name)
            assert in_filter == (scope.role == ROLE_STAFF), (scope.principal, name)

    def test_only_documented_operators_were_ever_evaluated(self):
        assert _SEEN_OPERATORS <= FILTER_OPERATORS_USED


class TestStaticScopeResolver:
    def test_tenant(self):
        scope = RESOLVER.resolve("T003")
        assert (scope.role, scope.principal) == (ROLE_RENTER, "T003")
        assert scope.lease_ids == {"L-0003"} and scope.property_ids == {"P-001"} and scope.as_of == date(2027, 3, 1)

    def test_staff(self):
        scope = RESOLVER.resolve("admin")
        assert scope.role == ROLE_STAFF and not scope.lease_ids and not scope.property_ids

    def test_tenant_with_no_leases_is_a_renter_with_only_company_scope(self):
        data = {
            "properties": ["P-001"],
            "leases": {"L-0001": {"property_id": "P-001"}},
            "tenants": {"T009": []},
            "staff": ["admin"],
        }
        scope = StaticScopeResolver(parse_registry(data), today=lambda: date(2027, 3, 1)).resolve("T009")
        assert scope.role == ROLE_RENTER and not scope.lease_ids and not scope.property_ids

    @pytest.mark.parametrize("principal", ["", "T999", "t001", " T001", "T001 ", "T001\n", "ADMIN", "Admin", "*", "null"])
    def test_unknown_principals_are_refused_with_no_default_scope(self, principal):
        with pytest.raises(UnknownPrincipalError):
            RESOLVER.resolve(principal)

    def test_error_does_not_echo_the_principal_or_registry(self):
        with pytest.raises(UnknownPrincipalError) as err:
            RESOLVER.resolve("T999")
        assert "T999" not in str(err.value) and "L-0001" not in str(err.value)

    def test_as_of_can_be_overridden_per_call(self):
        assert RESOLVER.resolve("T001", as_of=date(2026, 6, 1)).as_of == date(2026, 6, 1)

    def test_the_clock_is_read_on_every_call(self):
        days = iter([date(2027, 1, 1), date(2027, 1, 2)])
        resolver = StaticScopeResolver(REGISTRY, today=lambda: next(days))
        assert resolver.resolve("T001").as_of == date(2027, 1, 1)
        assert resolver.resolve("T001").as_of == date(2027, 1, 2)

    def test_the_default_clock_is_berlin_today(self):
        assert StaticScopeResolver(REGISTRY)._today is berlin_today

    def test_resolution_follows_the_registry_not_the_documents(self):
        """Moving a lease to another property changes access without touching any vector."""
        data = {
            "properties": ["P-001", "P-002"],
            "leases": {"L-0001": {"property_id": "P-002"}},
            "tenants": {"T001": ["L-0001"]},
            "staff": ["admin"],
        }
        moved = StaticScopeResolver(parse_registry(data), today=lambda: date(2027, 3, 1)).resolve("T001")
        seen = visible(moved)
        assert "rules-p2" in seen and "rules-p1" not in seen and "lease-l1" in seen


class TestBerlinToday:
    def test_summer_time_rolls_the_date_forward_two_hours_early(self):
        assert berlin_today(datetime(2027, 6, 30, 22, 30, tzinfo=timezone.utc)) == date(2027, 7, 1)  # CEST, UTC+2

    def test_winter_time_is_one_hour(self):
        assert berlin_today(datetime(2027, 1, 1, 22, 59, tzinfo=timezone.utc)) == date(2027, 1, 1)
        assert berlin_today(datetime(2027, 1, 1, 23, 0, tzinfo=timezone.utc)) == date(2027, 1, 2)

    def test_a_policy_effective_on_new_year_is_visible_from_berlin_midnight(self):
        md = UNIVERSE["repair-v2"]  # effective_from 2027-01-01
        before = berlin_today(datetime(2026, 12, 31, 22, 59, tzinfo=timezone.utc))
        after = berlin_today(datetime(2026, 12, 31, 23, 0, tzinfo=timezone.utc))
        assert not is_authorized(md, Scope(ROLE_RENTER, [], [], before))
        assert is_authorized(md, Scope(ROLE_RENTER, [], [], after))

    def test_naive_datetimes_are_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            berlin_today(datetime(2027, 1, 1))

    def test_defaults_to_the_current_date(self):
        assert abs((berlin_today() - datetime.now(timezone.utc).date()).days) <= 1

    def test_falls_back_to_utc_with_a_warning_when_the_tz_database_is_missing(self, monkeypatch, caplog):
        def missing(name):
            raise access.ZoneInfoNotFoundError(name)

        monkeypatch.setattr(access, "ZoneInfo", missing)
        with caplog.at_level(logging.WARNING, logger="common.access"):
            day = berlin_today(datetime(2027, 6, 30, 22, 30, tzinfo=timezone.utc))
        assert day == date(2027, 6, 30)  # UTC date: the documented, bounded skew
        assert "tz database unavailable" in caplog.text
