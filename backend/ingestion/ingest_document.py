"""Read, validate, chunk, embed, and index a single uploaded markdown document.

Keys follow the grammar in ``common.doc_keys`` (ADR 0002); everything that does
not need AWS (key, size, UTF-8, front matter, chunking, metadata) lives in
``common.prepare`` and is shared with the local ``/ingest`` route and the
offline corpus validator.
"""
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import unquote_plus

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from common import config, embeddings, vector_store
from common.doc_keys import (
    DocumentFormatError,
    DocumentKey,
    build_vector_id,
    parse_document_key,
)
from common.prepare import PreparedDocument, build_chunk_metadata, prepare_document
from ingestion import manifest

logger = logging.getLogger(__name__)

# Lazily-created, cached boto3 clients (one set per warm Lambda container).
_s3_client = None
_sagemaker_client = None
_s3vectors_client = None

# Manifest error messages list every problem found; keep the stored text bounded.
_MAX_ERROR_CHARS = 1000


class InvalidDocumentError(Exception):
    """A permanent validation failure.

    The triggering SQS message should be acknowledged (not retried), and the
    manifest should record a ``FAILED`` status. Retrying would not help because
    the document itself is unacceptable.
    """


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=config.DEFAULT_AWS_REGION)
    return _s3_client


def _sagemaker():
    global _sagemaker_client
    if _sagemaker_client is None:
        _sagemaker_client = boto3.client(
            "sagemaker-runtime",
            region_name=config.DEFAULT_AWS_REGION,
            config=Config(
                retries={
                    "max_attempts": config.EMBEDDING_MAX_ATTEMPTS,
                    "mode": "adaptive",
                }
            ),
        )
    return _sagemaker_client


def _s3vectors():
    global _s3vectors_client
    if _s3vectors_client is None:
        _s3vectors_client = boto3.client(
            "s3vectors", region_name=config.DEFAULT_AWS_REGION
        )
    return _s3vectors_client


def _parse_key(source_key: str) -> DocumentKey:
    """Parse a source key; a key outside the grammar is a permanent failure.

    Keys written under the former ``incoming/{tenant}/...`` layout land here and
    are acknowledged (never retried, never sent to the DLQ).
    """
    try:
        return parse_document_key(source_key)
    except DocumentFormatError as exc:
        raise InvalidDocumentError(str(exc)) from exc


def _index_chunk(
    *,
    prepared: PreparedDocument,
    chunk_index: int,
    chunk: str,
    source_bucket: str,
    source_version: str,
    ingested_at: str,
) -> str:
    """Embed a single chunk and write it to S3 Vectors with a deterministic id."""
    vector_id = build_vector_id(prepared.key, source_version, chunk_index)
    embedding = embeddings.embed_text(
        chunk, client=_sagemaker(), endpoint=config.SAGEMAKER_ENDPOINT
    )
    metadata = build_chunk_metadata(
        prepared,
        chunk_index=chunk_index,
        chunk_text=chunk,
        source_version=source_version,
        embedding_model=config.EMBEDDING_MODEL,
        ingested_at=ingested_at,
        source_bucket=source_bucket,
    )
    return vector_store.put_vector(
        client=_s3vectors(),
        bucket=config.VECTOR_BUCKET,
        index=config.VECTOR_INDEX,
        vector_id=vector_id,
        embedding=embedding,
        metadata=metadata,
    )


def process_deletion(
    *, bucket: str, key: str, version_id: Optional[str] = None
) -> Dict[str, Any]:
    """Remove S3-Vectors chunks for a permanently-deleted S3 object version.

    Returns a summary dict ``{"status", "scope_key", "vectors_deleted"}``.

    Raises:
        InvalidDocumentError: for permanent failures (ack the message).
        Exception: for transient failures (let SQS retry / route to the DLQ).
    """
    source_key = unquote_plus(key)
    doc_key = _parse_key(source_key)

    if not version_id:
        raise InvalidDocumentError(f"Delete event for {source_key} missing versionId")

    source_sk = manifest.build_sort_key(source_key, version_id)
    record = manifest.get_record(doc_key.scope_key, source_sk)
    if not record or record.get("status") not in (manifest.SUCCEEDED, manifest.SKIPPED):
        logger.info(
            "No indexed vectors for %s@%s; nothing to delete", source_key, version_id
        )
        return {"status": "noop", "scope_key": doc_key.scope_key, "vectors_deleted": 0}

    deleted = _delete_version_vectors(
        key=doc_key,
        source_version=version_id,
        chunk_count=record.get("chunk_count", 0),
    )
    manifest.mark_deleted(scope_key=doc_key.scope_key, source_sk=source_sk)

    logger.info("Deleted %d vectors for %s@%s", deleted, source_key, version_id)
    return {
        "status": "deleted",
        "scope_key": doc_key.scope_key,
        "vectors_deleted": deleted,
    }


def _delete_version_vectors(
    *, key: DocumentKey, source_version: str, chunk_count: int
) -> int:
    """Delete the deterministic vector ids of one indexed document version."""
    vector_ids = [
        build_vector_id(key, source_version, i) for i in range(chunk_count)
    ]
    vector_store.delete_vectors(
        client=_s3vectors(),
        bucket=config.VECTOR_BUCKET,
        index=config.VECTOR_INDEX,
        vector_ids=vector_ids,
    )
    return len(vector_ids)


def _current_s3_version(bucket: str, source_key: str) -> Optional[str]:
    """Return the key's current S3 VersionId, or ``None`` if it cannot be known.

    ``None`` means: object gone (404 / delete marker) or bucket unversioned.
    Other errors propagate so SQS retries.
    """
    try:
        head = _s3().head_object(Bucket=bucket, Key=source_key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    version = head.get("VersionId")
    return version if version and version != "null" else None


def _supersede_stale_versions(*, bucket: str, key: DocumentKey) -> int:
    """Remove vectors of every indexed S3 version that is not S3's current version.

    S3 overwrites of the *same key* create a new version with new vector ids;
    without this the old version's chunks stay searchable and compete with the
    new content. Comparing against S3's *current* version (rather than "the
    version just processed") keeps this correct when SQS delivers versions out
    of order: a stale version that finishes last removes its own vectors.

    Business versions (``v1.md``, ``v2.md``) are different keys and are never
    touched here: both stay indexed, and retrieval picks the one in force.
    """
    current = _current_s3_version(bucket, key.source_key)
    if current is None:
        logger.info(
            "Current version of %s unknown; skipping stale-version cleanup",
            key.source_key,
        )
        return 0

    superseded = 0
    for record in manifest.list_versions(key.scope_key, key.source_key):
        version = record.get("source_version", "")
        if version == current or record.get("status") not in (
            manifest.SUCCEEDED,
            manifest.SKIPPED,
        ):
            continue
        deleted = _delete_version_vectors(
            key=key,
            source_version=version,
            chunk_count=record.get("chunk_count", 0),
        )
        manifest.mark_status(
            scope_key=key.scope_key,
            source_sk=record["source_sk"],
            status=manifest.SUPERSEDED,
        )
        superseded += 1
        logger.info(
            "Superseded %s@%s (%d vectors removed)", key.source_key, version, deleted
        )
    return superseded


def process_document(
    *, bucket: str, key: str, version_id: Optional[str] = None
) -> Dict[str, Any]:
    """Ingest one markdown object from S3.

    Returns a summary dict ``{"status", "scope_key", "chunk_count", ...}``.

    Raises:
        InvalidDocumentError: for permanent validation failures (ack the message).
        Exception: for transient failures (let SQS retry / route to the DLQ).
    """
    source_key = unquote_plus(key)
    doc_key = _parse_key(source_key)
    scope_key = doc_key.scope_key

    # --- Transient: read object from S3 (failures here should be retried) ---
    get_kwargs: Dict[str, Any] = {"Bucket": bucket, "Key": source_key}
    if version_id:
        get_kwargs["VersionId"] = version_id
    obj = _s3().get_object(**get_kwargs)
    body_bytes = obj["Body"].read()

    resolved_version = version_id or obj.get("VersionId") or ""
    sha256 = hashlib.sha256(body_bytes).hexdigest()
    source_version = resolved_version or sha256
    source_sk = manifest.build_sort_key(source_key, source_version)

    # --- Permanent: validate + chunk (failures here mark FAILED and ack) ---
    try:
        prepared = prepare_document(
            source_key,
            body_bytes,
            max_bytes=config.MAX_UPLOAD_BYTES,
            chunk_size=config.CHUNK_SIZE,
            chunk_overlap=config.CHUNK_OVERLAP,
        )
    except DocumentFormatError as exc:
        message = str(exc)
        manifest.put_status(
            scope_key=scope_key,
            source_sk=source_sk,
            status=manifest.FAILED,
            source_bucket=bucket,
            source_key=source_key,
            source_version=source_version,
            sha256=sha256,
            error_message=message[:_MAX_ERROR_CHARS],
        )
        logger.warning("Validation failed for %s: %s", source_key, message)
        raise InvalidDocumentError(message) from exc
    chunks = prepared.chunks

    # --- Idempotency: skip if this exact version already succeeded ---
    existing = manifest.get_record(scope_key, source_sk)
    if (
        existing
        and existing.get("status") == manifest.SUCCEEDED
        and existing.get("sha256") == sha256
    ):
        logger.info("Skipping already-ingested document %s", source_sk)
        manifest.put_status(
            scope_key=scope_key,
            source_sk=source_sk,
            status=manifest.SKIPPED,
            source_bucket=bucket,
            source_key=source_key,
            source_version=source_version,
            sha256=sha256,
            vector_bucket=config.VECTOR_BUCKET,
            vector_index=config.VECTOR_INDEX,
            embedding_model=config.EMBEDDING_MODEL,
            chunk_count=existing.get("chunk_count", len(chunks)),
        )
        # Re-run cleanup on redelivery so a cleanup failure on the first
        # delivery (which raised and triggered the retry) is eventually healed.
        superseded = (
            _supersede_stale_versions(bucket=bucket, key=doc_key)
            if resolved_version
            else 0
        )
        return {
            "status": manifest.SKIPPED,
            "scope_key": scope_key,
            "chunk_count": existing.get("chunk_count", len(chunks)),
            "versions_superseded": superseded,
        }

    manifest.put_status(
        scope_key=scope_key,
        source_sk=source_sk,
        status=manifest.RUNNING,
        source_bucket=bucket,
        source_key=source_key,
        source_version=source_version,
        sha256=sha256,
        vector_bucket=config.VECTOR_BUCKET,
        vector_index=config.VECTOR_INDEX,
        embedding_model=config.EMBEDDING_MODEL,
        chunk_count=len(chunks),
    )

    ingested_at = datetime.now(timezone.utc).isoformat()

    # --- Transient: embed + index chunks (failures here mark FAILED and retry) ---
    try:
        with ThreadPoolExecutor(max_workers=config.INGESTION_MAX_WORKERS) as executor:
            futures = [
                executor.submit(
                    _index_chunk,
                    prepared=prepared,
                    chunk_index=idx,
                    chunk=chunk,
                    source_bucket=bucket,
                    source_version=source_version,
                    ingested_at=ingested_at,
                )
                for idx, chunk in enumerate(chunks)
            ]
            for future in as_completed(futures):
                future.result()
    except Exception as exc:
        manifest.put_status(
            scope_key=scope_key,
            source_sk=source_sk,
            status=manifest.FAILED,
            source_bucket=bucket,
            source_key=source_key,
            source_version=source_version,
            sha256=sha256,
            vector_bucket=config.VECTOR_BUCKET,
            vector_index=config.VECTOR_INDEX,
            embedding_model=config.EMBEDDING_MODEL,
            chunk_count=len(chunks),
            error_message=str(exc)[:_MAX_ERROR_CHARS],
        )
        logger.error("Indexing failed for %s: %s", source_key, exc)
        raise

    manifest.put_status(
        scope_key=scope_key,
        source_sk=source_sk,
        status=manifest.SUCCEEDED,
        source_bucket=bucket,
        source_key=source_key,
        source_version=source_version,
        sha256=sha256,
        vector_bucket=config.VECTOR_BUCKET,
        vector_index=config.VECTOR_INDEX,
        embedding_model=config.EMBEDDING_MODEL,
        chunk_count=len(chunks),
    )
    logger.info("Ingested %s (%d chunks)", source_key, len(chunks))

    # Only meaningful with S3 versioning: without a VersionId the content hash
    # is the version and there is no notion of which one is current.
    superseded = (
        _supersede_stale_versions(bucket=bucket, key=doc_key)
        if resolved_version
        else 0
    )
    return {
        "status": manifest.SUCCEEDED,
        "scope_key": scope_key,
        "chunk_count": len(chunks),
        "versions_superseded": superseded,
    }
