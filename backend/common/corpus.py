"""Offline validator for the synthetic document corpus (ADR 0002 section 6).

The corpus tree mirrors the RAG source bucket, so a validated tree can be
uploaded as is::

    corpus/incoming/renters/leases/L-0001/contracts/lease-contract/v1.md

Checks, per file (through ``common.prepare.prepare_document``, the very function
the ingestion worker runs, so a valid tree is an ingestible tree): key grammar,
size, UTF-8, front matter, non-empty body and chunking; plus that every
property/lease exists in the registry. Across files: one document
type per ``doc_id`` within a scope, versions that take effect in order, and
**no overlapping effective windows** between the versions of a document.

Run from ``backend/``::

    uv run python -m common.corpus [--corpus corpus] [--registry data/registry.json]
                                   [--require-documents]

Exit codes: 0 valid, 1 problems found, 2 could not run (bad paths or registry).
Makes no network or AWS calls; needs the project's chunking dependency.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from common.doc_keys import (
    KEY_ROOT,
    DocumentFormatError,
    DocumentKey,
    FrontMatter,
    parse_document_key,
)
from common.prepare import DEFAULT_MAX_BYTES, prepare_document
from common.registry import Registry, RegistryError, load_registry


@dataclass(frozen=True)
class CorpusDocument:
    path: str  # relative to the corpus directory, POSIX separators
    key: DocumentKey
    front_matter: FrontMatter


@dataclass
class CorpusReport:
    documents: List[CorpusDocument] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _iter_files(incoming: Path) -> List[Path]:
    # Hidden files (``.DS_Store``) are never ingested: the S3 suffix filter skips them.
    return sorted(
        p
        for p in incoming.rglob("*")
        if (p.is_file() or p.is_symlink())
        and not any(part.startswith(".") for part in p.relative_to(incoming).parts)
    )


def _check_registry(key: DocumentKey, registry: Registry) -> Optional[str]:
    if key.lease_id is not None and key.lease_id not in registry.lease_property:
        return f"lease {key.lease_id!r} is not in the registry"
    if key.property_id is not None and key.property_id not in registry.properties:
        return f"property {key.property_id!r} is not in the registry"
    return None


def _validate_file(
    path: Path, corpus_dir: Path, registry: Registry, max_bytes: int, report: CorpusReport
) -> None:
    relative = path.relative_to(corpus_dir).as_posix()

    def problem(message: str) -> None:
        report.problems.append(f"{relative}: {message}")

    if path.is_symlink():
        return problem("symbolic links are not allowed")
    # Cheap grammar check first so a stray file is never read.
    try:
        parse_document_key(relative)
    except DocumentFormatError as exc:
        return problem("; ".join(exc.problems))

    try:
        prepared = prepare_document(relative, path.read_bytes(), max_bytes=max_bytes)
    except DocumentFormatError as exc:
        for item in exc.problems:
            problem(item)
        return

    registry_problem = _check_registry(prepared.key, registry)
    if registry_problem:
        return problem(registry_problem)
    report.documents.append(CorpusDocument(relative, prepared.key, prepared.front_matter))


def _check_versions(documents: Sequence[CorpusDocument], report: CorpusReport) -> None:
    by_doc: Dict[str, List[CorpusDocument]] = defaultdict(list)
    types_by_scope_id: Dict[Tuple[str, str], set] = defaultdict(set)
    for doc in documents:
        by_doc[doc.key.logical_doc].append(doc)
        types_by_scope_id[(doc.key.scope_key, doc.key.doc_id)].add(doc.key.doc_type)

    for (scope_key, doc_id), types in sorted(types_by_scope_id.items()):
        if len(types) > 1:
            report.problems.append(
                f"{scope_key}: doc_id {doc_id!r} is used under several doc_types {sorted(types)}"
            )

    for logical_doc, versions in sorted(by_doc.items()):
        versions.sort(key=lambda d: d.key.doc_version)
        for earlier, later in zip(versions, versions[1:]):
            label = f"{logical_doc}: v{later.key.doc_version} ({later.path})"
            earlier_from: date = earlier.front_matter.effective_from
            later_from: date = later.front_matter.effective_from
            if later_from <= earlier_from:
                report.problems.append(
                    f"{label} must take effect after v{earlier.key.doc_version} "
                    f"(effective_from {later_from} <= {earlier_from})"
                )
                continue
            earlier_to = earlier.front_matter.effective_to
            if earlier_to is None:
                report.problems.append(
                    f"{label} overlaps v{earlier.key.doc_version}, which is open-ended; "
                    f"set its effective_to before {later_from}"
                )
            elif earlier_to >= later_from:
                report.problems.append(
                    f"{label} overlaps v{earlier.key.doc_version} "
                    f"(its effective_to {earlier_to} >= {later_from})"
                )


def validate_corpus(
    corpus_dir: Path, registry: Registry, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> CorpusReport:
    """Validate ``corpus_dir/incoming``; a missing ``incoming`` means no documents."""
    report = CorpusReport()
    incoming = corpus_dir / KEY_ROOT
    if incoming.is_dir():
        for path in _iter_files(incoming):
            _validate_file(path, corpus_dir, registry, max_bytes, report)
    _check_versions(report.documents, report)
    report.problems.sort()
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the synthetic document corpus.")
    parser.add_argument("--corpus", default="corpus", type=Path)
    parser.add_argument("--registry", default="data/registry.json", type=Path)
    parser.add_argument(
        "--require-documents",
        action="store_true",
        help="fail when the corpus contains no valid documents",
    )
    args = parser.parse_args(argv)

    if not args.corpus.is_dir():
        print(f"corpus directory not found: {args.corpus}", file=sys.stderr)
        return 2
    try:
        registry = load_registry(args.registry)
    except RegistryError as exc:
        print(f"INVALID REGISTRY {args.registry}", file=sys.stderr)
        for item in exc.problems:
            print(f"  {item}", file=sys.stderr)
        return 2

    report = validate_corpus(args.corpus, registry)
    for item in report.problems:
        print(item, file=sys.stderr)
    print(f"{len(report.documents)} valid document(s), {len(report.problems)} problem(s)")
    if report.problems:
        return 1
    if args.require_documents and not report.documents:
        print("no documents found but --require-documents was given", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
