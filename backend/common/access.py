"""Access scopes: who may see which documents, and as of when (ADR 0002 section 5).

Pure and dependency-free (standard library only; no AWS, no environment reads).

Authorization is enforced in code at the data-access layer, never by prompt
wording or by a classifier (CLAUDE.md section 9.9). This module gives retrieval
two independent implementations of the same rule:

* :func:`build_vector_filter` turns a :class:`Scope` into the S3 Vectors
  metadata filter, so unauthorized vectors are never returned, and
* :func:`is_authorized` re-checks every returned hit, so a mistake in query
  construction cannot leak a document.

The tests prove the two agree on every well-formed vector. Where they differ on
malformed metadata, :func:`is_authorized` is the stricter one and fails closed.
That is not hypothetical: a live run against the ``dev`` index (2026-11-23)
showed that S3 Vectors matches **array-valued** metadata when any element
matches, so a vector with ``audience: ["renter", "staff"]`` or
``lease_id: ["L-0001", "L-0002"]`` is *returned* by a renter's filter. The
ingestion pipeline only writes plain strings, so this cannot occur today, but it
makes the in-code check the part that actually keeps such a vector out, not a
formality: it must keep rejecting every non-string where a string belongs.

A :class:`Scope` is derived server-side from a principal through a
:class:`ScopeResolver`. Today that is :class:`StaticScopeResolver` over the
synthetic registry; the planned SQL database adds a second resolver and nothing
else changes. Which principal a caller *is* remains the identity workstream:
until verified identity exists (Month 3) the request body is still unverified.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from common.doc_keys import date_to_int
from common.registry import Registry

logger = logging.getLogger(__name__)

ROLE_STAFF = "staff"
ROLE_RENTER = "renter"
_ROLES = frozenset({ROLE_STAFF, ROLE_RENTER})

# The metadata vocabulary written by common.doc_keys.build_document_metadata.
AUDIENCES = frozenset({"staff", "renter"})
SCOPE_TYPES = frozenset({"company", "property", "lease"})

# Every operator the filter builder may emit. Deliberately a short, closed list:
# the dev-environment check against the real S3 Vectors index (ADR 0002,
# verification plan item 3) must confirm exactly these, and a test fails if the
# builder ever uses another.
FILTER_OPERATORS_USED = frozenset({"$and", "$or", "$eq", "$in", "$lte", "$gte"})

BERLIN = "Europe/Berlin"


class UnknownPrincipalError(LookupError):
    """The principal is neither a registered tenant nor staff. Callers refuse (403)."""


@dataclass(frozen=True)
class Scope:
    """What one principal may see, as of one day."""

    role: str
    lease_ids: FrozenSet[str]
    property_ids: FrozenSet[str]
    as_of: date
    principal: str = ""  # for audit/logging only; never used in a decision

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ValueError(f"role must be one of {sorted(_ROLES)}, got {self.role!r}")
        if not isinstance(self.as_of, date) or isinstance(self.as_of, datetime):
            raise TypeError("as_of must be a date (not a datetime)")
        # Accept any iterable of ids but always hold an immutable set.
        object.__setattr__(self, "lease_ids", frozenset(self.lease_ids))
        object.__setattr__(self, "property_ids", frozenset(self.property_ids))

    @property
    def as_of_int(self) -> int:
        return date_to_int(self.as_of)


def berlin_today(now: Optional[datetime] = None) -> date:
    """Today's calendar date in Europe/Berlin (the day a policy is "in force").

    If the system has no tz database the date falls back to UTC with a warning:
    a skew of at most two hours around midnight, instead of failing the request.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    try:
        return moment.astimezone(ZoneInfo(BERLIN)).date()
    except ZoneInfoNotFoundError:
        logger.warning("tz database unavailable; using the UTC date for effective-date checks")
        return moment.astimezone(timezone.utc).date()


class ScopeResolver(Protocol):
    """Maps a principal to its :class:`Scope`. The seam for the SQL database."""

    def resolve(self, principal: str, *, as_of: Optional[date] = None) -> Scope: ...


class StaticScopeResolver:
    """Resolver over the synthetic registry (interim, until the SQL database)."""

    def __init__(self, registry: Registry, *, today: Callable[[], date] = berlin_today) -> None:
        self._registry = registry
        self._today = today

    def resolve(self, principal: str, *, as_of: Optional[date] = None) -> Scope:
        day = as_of if as_of is not None else self._today()
        if self._registry.is_staff(principal):
            return Scope(ROLE_STAFF, frozenset(), frozenset(), day, principal)
        if principal in self._registry.tenant_leases:
            return Scope(
                ROLE_RENTER,
                self._registry.lease_ids_for(principal),
                self._registry.property_ids_for(principal),
                day,
                principal,
            )
        # Exact match only: no trimming or case folding, and no default scope.
        raise UnknownPrincipalError("principal is not registered")


def _eq(field: str, value: Any) -> Dict[str, Any]:
    return {field: {"$eq": value}}


def build_vector_filter(scope: Scope) -> Dict[str, Any]:
    """The S3 Vectors metadata filter for ``scope``.

    Renter: ``audience = renter`` and (company, or one of their properties, or
    one of their leases) and in force on ``as_of``. Staff: any known audience and
    scope, in force on ``as_of``. An empty set drops its branch instead of
    emitting an empty ``$in``. Metadata without the new schema (for example
    vectors from before ADR 0002) matches nothing.
    """
    now = scope.as_of_int
    in_force: List[Dict[str, Any]] = [
        {"effective_from": {"$lte": now}},
        {"effective_to": {"$gte": now}},
    ]

    if scope.role == ROLE_STAFF:
        return {
            "$and": [
                {"audience": {"$in": sorted(AUDIENCES)}},
                {"scope_type": {"$in": sorted(SCOPE_TYPES)}},
                *in_force,
            ]
        }

    branches: List[Dict[str, Any]] = [_eq("scope_type", "company")]
    if scope.property_ids:
        branches.append(
            {"$and": [_eq("scope_type", "property"), {"property_id": {"$in": sorted(scope.property_ids)}}]}
        )
    if scope.lease_ids:
        branches.append(
            {"$and": [_eq("scope_type", "lease"), {"lease_id": {"$in": sorted(scope.lease_ids)}}]}
        )
    where = branches[0] if len(branches) == 1 else {"$or": branches}
    return {"$and": [_eq("audience", "renter"), where, *in_force]}


def _as_int(value: Any) -> Optional[int]:
    """An exact integer, or None. Vector stores may hand integers back as floats."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _text_in(value: Any, allowed: FrozenSet[str]) -> bool:
    return isinstance(value, str) and value in allowed


def is_authorized(metadata: Any, scope: Scope) -> bool:
    """Whether a retrieved vector's metadata is visible to ``scope``.

    Written directly, not derived from :func:`build_vector_filter`, so the two
    can catch each other's mistakes. Fails closed on anything unexpected:
    missing or mistyped fields, unknown audience or scope, dates outside the
    window.
    """
    if not isinstance(metadata, Mapping):
        return False

    start = _as_int(metadata.get("effective_from"))
    end = _as_int(metadata.get("effective_to"))
    if start is None or end is None or not start <= scope.as_of_int <= end:
        return False

    audience = metadata.get("audience")
    scope_type = metadata.get("scope_type")
    if not _text_in(audience, AUDIENCES) or not _text_in(scope_type, SCOPE_TYPES):
        return False

    if scope.role == ROLE_STAFF:
        return True
    if audience != "renter":
        return False
    if scope_type == "company":
        return True
    if scope_type == "property":
        return _text_in(metadata.get("property_id"), scope.property_ids)
    return _text_in(metadata.get("lease_id"), scope.lease_ids)  # scope_type == "lease"
