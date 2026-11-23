"""Raw uploaded bytes -> an ingestible document (ADR 0002, slice 3).

One code path for the ingestion worker, the local ``/ingest`` route and the
offline corpus validator. That is what makes "the corpus validated in CI" mean
exactly "the worker will accept it": key grammar, size, UTF-8, front matter and
chunking are checked by the same function.

No AWS and no environment reads: every limit is passed in. It uses the
project's chunking helper (``langchain-text-splitters``), like the worker.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from common import chunking
from common.doc_keys import (
    DocumentFormatError,
    DocumentKey,
    FrontMatter,
    build_document_metadata,
    parse_document_key,
    parse_front_matter,
)

# Mirror the defaults in common.config (MAX_UPLOAD_BYTES, CHUNK_SIZE, CHUNK_OVERLAP).
DEFAULT_MAX_BYTES = 1_048_576
DEFAULT_CHUNK_SIZE = 1500
DEFAULT_CHUNK_OVERLAP = 200


class InvalidDocumentContentError(DocumentFormatError):
    """The bytes are unacceptable (empty, too large, not UTF-8, nothing to index)."""


@dataclass(frozen=True)
class PreparedDocument:
    key: DocumentKey
    front_matter: FrontMatter
    body: str  # markdown after the front-matter block
    chunks: List[str]
    doc_metadata: Dict[str, Any]  # document-level vector metadata (doc_keys)


def prepare_document(
    source_key: str,
    body_bytes: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> PreparedDocument:
    """Validate and chunk one document.

    Raises :class:`~common.doc_keys.InvalidDocumentKeyError` for a bad key,
    :class:`InvalidDocumentContentError` for bad bytes and
    :class:`~common.doc_keys.InvalidFrontMatterError` for bad front matter (all
    :class:`~common.doc_keys.DocumentFormatError`, carrying every problem).
    The key is checked first, so nothing is read for a misplaced file.
    """
    key = parse_document_key(source_key)

    if not body_bytes:
        raise InvalidDocumentContentError(["file is empty"])
    if len(body_bytes) > max_bytes:
        raise InvalidDocumentContentError(
            [f"file is {len(body_bytes)} bytes, over the {max_bytes} byte limit"]
        )
    try:
        text = body_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidDocumentContentError(["file is not valid UTF-8"]) from None

    front_matter, body = parse_front_matter(text)

    chunks = list(chunking.chunk_text(body, size=chunk_size, overlap=chunk_overlap))
    if not chunks:
        raise InvalidDocumentContentError(["document produced no content chunks"])

    return PreparedDocument(
        key=key,
        front_matter=front_matter,
        body=body,
        chunks=chunks,
        doc_metadata=build_document_metadata(key, front_matter),
    )


def build_chunk_metadata(
    prepared: PreparedDocument,
    *,
    chunk_index: int,
    chunk_text: str,
    source_version: str,
    embedding_model: str,
    ingested_at: str,
    source_bucket: Optional[str] = None,
) -> Dict[str, Any]:
    """Full vector metadata for one chunk, identical for the worker and ``/ingest``.

    ``source_bucket`` is omitted for the local route, which has no bucket.
    """
    metadata = dict(prepared.doc_metadata)
    metadata.update(
        {
            "chunk_index": chunk_index,
            "chunk_text": chunk_text,
            "source_version": source_version,
            "embedding_model": embedding_model,
            "ingested_at": ingested_at,
        }
    )
    if source_bucket:
        metadata["source_bucket"] = source_bucket
    return metadata
