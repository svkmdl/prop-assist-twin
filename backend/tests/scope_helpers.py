"""Shared test fixtures for retrieval tests: realistic metadata and ready-made scopes."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from common.access import ROLE_STAFF, StaticScopeResolver
from common.doc_keys import FrontMatter, build_document_metadata, parse_document_key
from common.registry import load_registry

REGISTRY = load_registry(Path(__file__).resolve().parents[1] / "data" / "registry.json")
TODAY = date(2027, 3, 1)
RESOLVER = StaticScopeResolver(REGISTRY, today=lambda: TODAY)
STAFF = RESOLVER.resolve("admin")
T001 = RESOLVER.resolve("T001")
T002 = RESOLVER.resolve("T002")
assert STAFF.role == ROLE_STAFF


def md(key: str = "incoming/renters/company/policies/repair-policy/v1.md",
       frm: str = "2026-01-01", to: str | None = None, **extra) -> dict:
    """Authorized-looking vector metadata for ``key``, plus any extra fields."""
    front = FrontMatter("Titel", "de", date.fromisoformat(frm), date.fromisoformat(to) if to else None)
    return {**build_document_metadata(parse_document_key(key), front), **extra}
