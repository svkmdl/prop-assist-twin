"""Synthetic scope registry: who may see which leases and properties (ADR 0002).

Interim data source. Until the planned SQL database exists, this file answers
"which leases does tenant T001 participate in" and "which property is lease
L-0001 in". The same IDs will be the primary keys of the future tables, and a
SQL-backed resolver replaces :func:`load_registry` without touching documents.

The registry is administrative data owned by the operator, never request input.
Pure and dependency-free (standard library only).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Union

from common.doc_keys import LEASE_ID_RE, PROPERTY_ID_RE

# Same shape the API already enforces for tenant IDs (``T001``, ``admin``).
PRINCIPAL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_TOP_LEVEL_KEYS = frozenset({"properties", "leases", "tenants", "staff"})


class RegistryError(ValueError):
    """The registry file is unusable; ``problems`` lists every violation."""

    def __init__(self, problems: List[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class Registry:
    properties: FrozenSet[str]
    lease_property: Mapping[str, str]  # lease_id -> property_id
    tenant_leases: Mapping[str, FrozenSet[str]]  # tenant_id -> lease_ids
    staff: FrozenSet[str]

    def is_staff(self, principal: str) -> bool:
        return principal in self.staff

    def lease_ids_for(self, tenant_id: str) -> FrozenSet[str]:
        return self.tenant_leases.get(tenant_id, frozenset())

    def property_ids_for(self, tenant_id: str) -> FrozenSet[str]:
        return frozenset(self.lease_property[lease] for lease in self.lease_ids_for(tenant_id))


def parse_registry(data: Any) -> Registry:
    """Validate a decoded registry document and build a :class:`Registry`."""
    if not isinstance(data, dict):
        raise RegistryError([f"registry must be a JSON object, got {type(data).__name__}"])

    problems: List[str] = []
    for key in sorted(data.keys() - _TOP_LEVEL_KEYS):
        problems.append(f"unknown top-level key {key!r}")
    for key in sorted(_TOP_LEVEL_KEYS - data.keys()):
        problems.append(f"missing top-level key {key!r}")
    if problems:
        raise RegistryError(problems)

    def id_list(name: str, pattern: "re.Pattern[str]") -> FrozenSet[str]:
        raw = data[name]
        if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
            problems.append(f"{name!r} must be a list of strings")
            return frozenset()
        for value in raw:
            if not pattern.fullmatch(value):
                problems.append(f"{name!r}: invalid id {value!r}")
        if len(set(raw)) != len(raw):
            problems.append(f"{name!r} contains duplicates")
        return frozenset(raw)

    properties = id_list("properties", PROPERTY_ID_RE)
    staff = id_list("staff", PRINCIPAL_ID_RE)

    lease_property: Dict[str, str] = {}
    leases = data["leases"]
    if not isinstance(leases, dict):
        problems.append("'leases' must be an object mapping lease id to {\"property_id\": ...}")
    else:
        for lease_id, entry in leases.items():
            if not LEASE_ID_RE.fullmatch(lease_id):
                problems.append(f"'leases': invalid lease id {lease_id!r}")
                continue
            # An object (not a bare string) so a unit_id can be added later.
            if not isinstance(entry, dict) or set(entry) != {"property_id"}:
                problems.append(f"'leases'[{lease_id!r}] must be exactly {{\"property_id\": ...}}")
                continue
            property_id = entry["property_id"]
            if property_id not in properties:
                problems.append(f"lease {lease_id!r} refers to unknown property {property_id!r}")
                continue
            lease_property[lease_id] = property_id

    tenant_leases: Dict[str, FrozenSet[str]] = {}
    tenants = data["tenants"]
    if not isinstance(tenants, dict):
        problems.append("'tenants' must be an object mapping tenant id to a list of lease ids")
    else:
        for tenant_id, lease_ids in tenants.items():
            if not PRINCIPAL_ID_RE.fullmatch(tenant_id):
                problems.append(f"'tenants': invalid tenant id {tenant_id!r}")
                continue
            if tenant_id in staff:
                problems.append(f"{tenant_id!r} is listed as both tenant and staff")
                continue
            if not isinstance(lease_ids, list) or not all(isinstance(v, str) for v in lease_ids):
                problems.append(f"tenant {tenant_id!r} must map to a list of lease ids")
                continue
            unknown = [lease for lease in lease_ids if lease not in lease_property]
            if unknown:
                problems.append(f"tenant {tenant_id!r} refers to unknown leases {unknown}")
                continue
            tenant_leases[tenant_id] = frozenset(lease_ids)

    if problems:
        raise RegistryError(problems)
    return Registry(
        properties=properties,
        lease_property=lease_property,
        tenant_leases=tenant_leases,
        staff=staff,
    )


def load_registry(path: Union[str, Path]) -> Registry:
    """Read and validate a registry JSON file."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise RegistryError([f"cannot read registry {path}: {exc.strerror or exc}"]) from exc
    except json.JSONDecodeError as exc:
        raise RegistryError([f"registry {path} is not valid JSON: {exc.msg} (line {exc.lineno})"]) from exc
    return parse_registry(data)
