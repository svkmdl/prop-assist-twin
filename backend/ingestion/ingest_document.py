"""Read, validate, chunk, embed, and index a single uploaded markdown document."""
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote_plus

import boto3
from botocore.config import Config

from common import chunking, config, embeddings, vector_store
from ingestion import manifest

logger = logging.getLogger(__name__)

# Lazily-created, cached boto3 clients (one set per warm Lambda container).
_s3_client = None
_sagemaker_client = None
_s3vectors_client = None


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


def parse_source_key(source_key: str) -> Tuple[str, str, str]:
    """Extract ``(tenant_id, doc_id, filename)`` from ``incoming/{tenant}/{doc}.md``.

    Raises:
        InvalidDocumentError: if the key does not match the expected layout.
    """
    parts = source_key.split("/")
    if len(parts) < 3 or parts[0] != "incoming":
        raise InvalidDocumentError(f"Unexpected source key layout: {source_key}")

    tenant_id = parts[1]
    filename = parts[-1]
    if not tenant_id or not filename:
        raise InvalidDocumentError(f"Unexpected source key layout: {source_key}")

    matched = next((s for s in config.SUPPORTED_SUFFIXES if filename.endswith(s)), None)
    doc_id = filename[: -len(matched)] if matched else filename
    return tenant_id, doc_id, filename


def _validate(source_key: str, body_bytes: bytes) -> str:
    """Validate extension/size/UTF-8 and return decoded content.

    Raises:
        InvalidDocumentError: on any validation failure.
    """
    if not any(source_key.endswith(s) for s in config.SUPPORTED_SUFFIXES):
        allowed = ", ".join(sorted(config.SUPPORTED_SUFFIXES))
        raise InvalidDocumentError(f"Unsupported extension; allowed: {allowed}")
    if len(body_bytes) == 0:
        raise InvalidDocumentError("File is empty")
    if len(body_bytes) > config.MAX_UPLOAD_BYTES:
        raise InvalidDocumentError("File too large")
    try:
        return body_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidDocumentError("File must be UTF-8 text") from exc


def _index_chunk(
    *,
    chunk_index: int,
    chunk: str,
    tenant_id: str,
    doc_id: str,
    doc_type: str,
    source_bucket: str,
    source_key: str,
    source_version: str,
    ingested_at: str,
) -> str:
    """Embed a single chunk and write it to S3 Vectors with a deterministic id."""
    vector_id = f"{tenant_id}/{doc_id}/{source_version}/{chunk_index}"
    embedding = embeddings.embed_text(
        chunk, client=_sagemaker(), endpoint=config.SAGEMAKER_ENDPOINT
    )
    metadata: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "source_bucket": source_bucket,
        "source_key": source_key,
        "source_version": source_version,
        "title": doc_id,
        "doc_type": doc_type,
        "chunk_index": chunk_index,
        "chunk_text": chunk,
        "embedding_model": config.EMBEDDING_MODEL,
        "ingested_at": ingested_at,
    }
    return vector_store.put_vector(
        client=_s3vectors(),
        bucket=config.VECTOR_BUCKET,
        index=config.VECTOR_INDEX,
        vector_id=vector_id,
        embedding=embedding,
        metadata=metadata,
    )


def process_document(
    *, bucket: str, key: str, version_id: Optional[str] = None
) -> Dict[str, Any]:
    """Ingest one markdown object from S3.

    Returns a summary dict ``{"status", "tenant_id", "chunk_count"}``.

    Raises:
        InvalidDocumentError: for permanent validation failures (ack the message).
        Exception: for transient failures (let SQS retry / route to the DLQ).
    """
    source_key = unquote_plus(key)
    tenant_id, doc_id, _filename = parse_source_key(source_key)

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
        content = _validate(source_key, body_bytes)
        chunks: List[str] = list(
            chunking.chunk_text(
                content, size=config.CHUNK_SIZE, overlap=config.CHUNK_OVERLAP
            )
        )
        if not chunks:
            raise InvalidDocumentError("Document produced no content chunks")
        doc_type = next(s for s in config.SUPPORTED_SUFFIXES if source_key.endswith(s))
    except InvalidDocumentError as exc:
        manifest.put_status(
            tenant_id=tenant_id,
            source_sk=source_sk,
            status=manifest.FAILED,
            source_bucket=bucket,
            source_key=source_key,
            source_version=source_version,
            sha256=sha256,
            error_message=str(exc),
        )
        logger.warning("Validation failed for %s: %s", source_key, exc)
        raise

    # --- Idempotency: skip if this exact version already succeeded ---
    existing = manifest.get_record(tenant_id, source_sk)
    if (
        existing
        and existing.get("status") == manifest.SUCCEEDED
        and existing.get("sha256") == sha256
    ):
        logger.info("Skipping already-ingested document %s", source_sk)
        manifest.put_status(
            tenant_id=tenant_id,
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
        return {
            "status": manifest.SKIPPED,
            "tenant_id": tenant_id,
            "chunk_count": existing.get("chunk_count", len(chunks)),
        }

    manifest.put_status(
        tenant_id=tenant_id,
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
                    chunk_index=idx,
                    chunk=chunk,
                    tenant_id=tenant_id,
                    doc_id=doc_id,
                    doc_type=doc_type,
                    source_bucket=bucket,
                    source_key=source_key,
                    source_version=source_version,
                    ingested_at=ingested_at,
                )
                for idx, chunk in enumerate(chunks)
            ]
            for future in as_completed(futures):
                future.result()
    except Exception as exc:
        manifest.put_status(
            tenant_id=tenant_id,
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
            error_message=str(exc),
        )
        logger.error("Indexing failed for %s: %s", source_key, exc)
        raise

    manifest.put_status(
        tenant_id=tenant_id,
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
    return {
        "status": manifest.SUCCEEDED,
        "tenant_id": tenant_id,
        "chunk_count": len(chunks),
    }
