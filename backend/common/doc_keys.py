"""Document key grammar, front matter and vector metadata (ADR 0002).

Pure and dependency-free: standard library only, no AWS clients and no
environment reads, so it is safe to import from the API, the ingestion worker
and offline tooling alike.

Key grammar (``audience`` is ``staff`` or ``renters``)::

    incoming/{audience}/company/{doc_type}/{doc_id}/v{n}.md
    incoming/{audience}/properties/{property_id}/{doc_type}/{doc_id}/v{n}.md
    incoming/renters/leases/{lease_id}/{doc_type}/{doc_id}/v{n}.md

The path owns audience, scope, IDs, ``doc_type``, ``doc_id`` and the business
version. The front matter owns title, language and the effective dates. Nothing
is stored twice, so the two cannot disagree.
"""
from __future__ import annotations

import difflib
import re
from urllib.parse import quote
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

KEY_ROOT = "incoming"
DOCUMENT_SUFFIX = ".md"

# Closed vocabulary. Adding a type is a deliberate, reviewed change.
DOC_TYPES = frozenset({"contracts", "policies", "sops", "statements"})
LANGUAGES = frozenset({"de", "en"})

# IDs are opaque and PII-free. The upper bounds keep every derived vector ID far
# below the S3 Vectors key limit (1,024 characters).
PROPERTY_ID_RE = re.compile(r"P-\d{3,12}")
LEASE_ID_RE = re.compile(r"L-\d{4,12}")
DOC_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_DOC_ID_CHARS = 64
VERSION_FILE_RE = re.compile(r"v([1-9][0-9]{0,5})" + re.escape(DOCUMENT_SUFFIX))

# Effective dates are stored as YYYYMMDD integers. An open-ended document uses
# the sentinel below, which keeps the filter free of ``$exists``.
OPEN_ENDED = 99991231
MIN_YEAR, MAX_YEAR = 2000, 2100

MAX_TITLE_CHARS = 200
MAX_ERROR_KEY_CHARS = 200


class DocumentFormatError(ValueError):
    """A document key or its front matter breaks the contract.

    ``problems`` lists every violation found, so authors fix them in one pass.
    """

    def __init__(self, problems: List[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


class InvalidDocumentKeyError(DocumentFormatError):
    """The S3 key does not match the grammar."""


class InvalidFrontMatterError(DocumentFormatError):
    """The front-matter block is missing or invalid."""


@dataclass(frozen=True)
class ScopeRule:
    """One accepted ``{audience}/{scope}`` shape. Table-driven on purpose."""

    path_audience: str  # first segment after ``incoming/``
    path_scope: str  # second segment
    audience: str  # metadata value: ``staff`` | ``renter``
    scope_type: str  # metadata value: ``company`` | ``property`` | ``lease``
    id_field: Optional[str] = None  # ``property_id`` | ``lease_id`` | None
    id_re: Optional["re.Pattern[str]"] = None


# The unit level is intentionally absent (ADR 0002 section 7): adding it later
# is one row here plus a metadata field and a filter branch, with no key moving.
SCOPE_RULES: Tuple[ScopeRule, ...] = (
    ScopeRule("staff", "company", "staff", "company"),
    ScopeRule("renters", "company", "renter", "company"),
    ScopeRule("staff", "properties", "staff", "property", "property_id", PROPERTY_ID_RE),
    ScopeRule("renters", "properties", "renter", "property", "property_id", PROPERTY_ID_RE),
    ScopeRule("renters", "leases", "renter", "lease", "lease_id", LEASE_ID_RE),
)
_RULES: Dict[Tuple[str, str], ScopeRule] = {
    (rule.path_audience, rule.path_scope): rule for rule in SCOPE_RULES
}


@dataclass(frozen=True)
class DocumentKey:
    """A parsed, validated source key."""

    source_key: str
    audience: str  # ``staff`` | ``renter``
    scope_type: str  # ``company`` | ``property`` | ``lease``
    property_id: Optional[str]
    lease_id: Optional[str]
    doc_type: str
    doc_id: str
    doc_version: int
    scope_key: str  # e.g. ``renters/leases/L-0001``
    relative_key: str  # key without ``incoming/`` and the suffix, incl. ``v{n}``

    @property
    def logical_doc(self) -> str:
        """Identity of the document across its business versions."""
        return f"{self.scope_key}/{self.doc_type}/{self.doc_id}"


def _shown(source_key: str) -> str:
    return source_key[:MAX_ERROR_KEY_CHARS]


def parse_document_key(source_key: str) -> DocumentKey:
    """Parse a key strictly; raise :class:`InvalidDocumentKeyError` otherwise."""

    def fail(reason: str) -> InvalidDocumentKeyError:
        return InvalidDocumentKeyError([f"{reason}: {_shown(source_key)!r}"])

    parts = source_key.split("/")
    if parts[0] != KEY_ROOT:
        raise fail(f"key must start with {KEY_ROOT}/")
    if len(parts) < 3:
        raise fail("key is too short")

    rule = _RULES.get((parts[1], parts[2]))
    if rule is None:
        accepted = ", ".join(f"{r.path_audience}/{r.path_scope}" for r in SCOPE_RULES)
        raise fail(f"unknown audience/scope (accepted: {accepted})")

    expected_parts = 7 if rule.id_field else 6
    if len(parts) != expected_parts:
        shape = (
            f"incoming/{rule.path_audience}/{rule.path_scope}/"
            + ("{" + rule.id_field + "}/" if rule.id_field else "")
            + "{doc_type}/{doc_id}/v{n}.md"
        )
        raise fail(f"expected {shape}")

    scope_id: Optional[str] = None
    if rule.id_field and rule.id_re is not None:
        scope_id = parts[3]
        if not rule.id_re.fullmatch(scope_id):
            raise fail(f"invalid {rule.id_field}")

    doc_type, doc_id, filename = parts[-3], parts[-2], parts[-1]
    if doc_type not in DOC_TYPES:
        raise fail(f"unknown doc_type (accepted: {', '.join(sorted(DOC_TYPES))})")
    if len(doc_id) > MAX_DOC_ID_CHARS or not DOC_ID_RE.fullmatch(doc_id):
        raise fail("invalid doc_id (lowercase letters, digits and single hyphens)")
    version_match = VERSION_FILE_RE.fullmatch(filename)
    if not version_match:
        raise fail(f"filename must be v<n>{DOCUMENT_SUFFIX} with n >= 1 and no leading zero")

    scope_key = f"{rule.path_audience}/{rule.path_scope}" + (f"/{scope_id}" if scope_id else "")
    doc_version = int(version_match.group(1))
    return DocumentKey(
        source_key=source_key,
        audience=rule.audience,
        scope_type=rule.scope_type,
        property_id=scope_id if rule.id_field == "property_id" else None,
        lease_id=scope_id if rule.id_field == "lease_id" else None,
        doc_type=doc_type,
        doc_id=doc_id,
        doc_version=doc_version,
        scope_key=scope_key,
        relative_key=f"{scope_key}/{doc_type}/{doc_id}/v{doc_version}",
    )


# --------------------------------------------------------------------------
# Front matter
# --------------------------------------------------------------------------

REQUIRED_FRONT_MATTER = ("title", "language", "effective_from")
ALLOWED_FRONT_MATTER = frozenset({*REQUIRED_FRONT_MATTER, "effective_to"})
_FM_KEY_RE = re.compile(r"[a-z_]+")
_FM_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_FENCE = "---"


@dataclass(frozen=True)
class FrontMatter:
    title: str
    language: str
    effective_from: date
    effective_to: Optional[date]


def _parse_date(name: str, value: str, problems: List[str]) -> Optional[date]:
    # Python 3.11+ ``fromisoformat`` also accepts ``20270101`` and week dates;
    # the strict pattern keeps the format unambiguous.
    if not _FM_DATE_RE.fullmatch(value):
        problems.append(f"{name} must be an ISO date YYYY-MM-DD, got {value!r}")
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        problems.append(f"{name} is not a real calendar date: {value!r}")
        return None
    if not MIN_YEAR <= parsed.year <= MAX_YEAR:
        problems.append(f"{name} year must be between {MIN_YEAR} and {MAX_YEAR}, got {parsed.year}")
        return None
    return parsed


def parse_front_matter(text: str) -> Tuple[FrontMatter, str]:
    """Split ``text`` into validated front matter and the markdown body.

    Only flat ``key: value`` lines are supported (no nesting, lists or inline
    comments, so a title may contain ``#``). Whole-line ``#`` comments and blank
    lines are ignored. Raises :class:`InvalidFrontMatterError` listing every
    problem found.
    """
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if lines[0].rstrip() != _FENCE:
        raise InvalidFrontMatterError(
            ["document must start with a front-matter block delimited by '---'"]
        )
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].rstrip() == _FENCE)
    except StopIteration:
        raise InvalidFrontMatterError(["front-matter block is not closed with '---'"]) from None

    problems: List[str] = []
    values: Dict[str, str] = {}
    emptied: set = set()  # required keys already reported as empty
    for line_no, raw in enumerate(lines[1:end], start=2):
        if not raw.strip() or raw.startswith("#"):
            continue
        if raw[0] in " \t" or raw.startswith("- "):
            problems.append(f"line {line_no}: only flat 'key: value' lines are supported")
            continue
        key, sep, value = raw.partition(":")
        if not sep or not _FM_KEY_RE.fullmatch(key):
            problems.append(f"line {line_no}: expected 'key: value' with a lowercase key")
            continue
        if key not in ALLOWED_FRONT_MATTER:
            hint = difflib.get_close_matches(key, ALLOWED_FRONT_MATTER, n=1)
            suffix = f" (did you mean {hint[0]!r}?)" if hint else ""
            problems.append(f"line {line_no}: unknown key {key!r}{suffix}")
            continue
        if key in values:
            problems.append(f"line {line_no}: duplicate key {key!r}")
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if not value:
            problems.append(f"line {line_no}: {key!r} is empty (omit an optional key instead)")
            emptied.add(key)
            continue
        values[key] = value

    for key in REQUIRED_FRONT_MATTER:
        if key not in values and key not in emptied:
            problems.append(f"missing required key {key!r}")

    title = values.get("title")
    if title is not None and (
        len(title) > MAX_TITLE_CHARS or any(ord(ch) < 32 for ch in title)
    ):
        problems.append(f"title must be at most {MAX_TITLE_CHARS} characters without control characters")

    language = values.get("language")
    if language is not None and language not in LANGUAGES:
        problems.append(f"language must be one of {sorted(LANGUAGES)}, got {language!r}")

    effective_from = (
        _parse_date("effective_from", values["effective_from"], problems)
        if "effective_from" in values
        else None
    )
    effective_to = (
        _parse_date("effective_to", values["effective_to"], problems)
        if "effective_to" in values
        else None
    )
    if effective_from and effective_to and effective_to < effective_from:
        problems.append("effective_to must not be before effective_from")

    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    if not body.strip():
        problems.append("document has no content after the front matter")

    if problems:
        raise InvalidFrontMatterError(problems)
    assert title is not None and language is not None and effective_from is not None
    return FrontMatter(title, language, effective_from, effective_to), body


# --------------------------------------------------------------------------
# Vector metadata and IDs
# --------------------------------------------------------------------------


def date_to_int(value: date) -> int:
    """``2027-01-31`` -> ``20270131`` (sortable and filterable as a number)."""
    return value.year * 10000 + value.month * 100 + value.day


def build_document_metadata(key: DocumentKey, front_matter: FrontMatter) -> Dict[str, Any]:
    """Document-level vector metadata, shared by the worker and ``/ingest``.

    Per-chunk fields (``chunk_index``, ``chunk_text``, ``source_version`` and so
    on) are added by the caller. ``property_id``/``lease_id`` are present only
    for their scope. A lease document deliberately carries no ``property_id``:
    the lease-to-property link is registry data and would go stale in a vector.
    """
    metadata: Dict[str, Any] = {
        "audience": key.audience,
        "scope_type": key.scope_type,
        "doc_type": key.doc_type,
        "doc_id": key.doc_id,
        "doc_version": key.doc_version,
        "language": front_matter.language,
        "effective_from": date_to_int(front_matter.effective_from),
        "effective_to": (
            date_to_int(front_matter.effective_to)
            if front_matter.effective_to is not None
            else OPEN_ENDED
        ),
        "title": front_matter.title,
        "source_key": key.source_key,
    }
    if key.property_id is not None:
        metadata["property_id"] = key.property_id
    if key.lease_id is not None:
        metadata["lease_id"] = key.lease_id
    return metadata


def build_vector_id(key: DocumentKey, source_version: str, chunk_index: int) -> str:
    """Deterministic, scope-qualified vector ID (idempotent across redelivery).

    S3 VersionIds are opaque and may contain ``/`` or ``+`` (AWS's own examples
    do), so the version is percent-encoded: deterministic, reversible, and it can
    never add a path segment. Indexing, deletion and supersession all build IDs
    here, so they always agree.
    """
    if not source_version:
        raise ValueError("source_version must be non-empty")
    if isinstance(chunk_index, bool) or not isinstance(chunk_index, int) or chunk_index < 0:
        raise ValueError("chunk_index must be an integer >= 0")
    return f"{key.relative_key}/{quote(source_version, safe='')}/{chunk_index}"
