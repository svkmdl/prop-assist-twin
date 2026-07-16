"""DynamoDB ingestion-manifest access.

Tracks ingestion status per source document version to provide idempotency and
operational visibility. Table schema:

    PK (tenant_id):  S
    SK (source_sk):  S   ->  "{source_key}#{source_version_or_sha256}"
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3

from common import config

# Allowed manifest statuses.
PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
SKIPPED = "SKIPPED"
DELETED = "DELETED"

_dynamodb_client = None


def _client():
    """Lazily create (and cache) the boto3 DynamoDB client."""
    global _dynamodb_client
    if _dynamodb_client is None:
        _dynamodb_client = boto3.client(
            "dynamodb", region_name=config.DEFAULT_AWS_REGION
        )
    return _dynamodb_client


def build_sort_key(source_key: str, source_version: str) -> str:
    """Compose the manifest sort key from the source key and version/sha."""
    return f"{source_key}#{source_version}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_record(tenant_id: str, source_sk: str) -> Optional[Dict[str, Any]]:
    """Return the deserialized manifest item, or ``None`` if absent."""
    response = _client().get_item(
        TableName=config.MANIFEST_TABLE,
        Key={"tenant_id": {"S": tenant_id}, "source_sk": {"S": source_sk}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not item:
        return None
    return _deserialize(item)


def put_status(
    *,
    tenant_id: str,
    source_sk: str,
    status: str,
    source_bucket: str,
    source_key: str,
    source_version: str,
    sha256: str,
    vector_bucket: str = "",
    vector_index: str = "",
    embedding_model: str = "",
    chunk_count: Optional[int] = None,
    error_message: str = "",
) -> None:
    """Create or overwrite the manifest record with the given status."""
    now = _now_iso()
    existing = get_record(tenant_id, source_sk)
    created_at = existing.get("created_at") if existing else now

    item: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "source_sk": source_sk,
        "source_bucket": source_bucket,
        "source_key": source_key,
        "source_version": source_version,
        "sha256": sha256,
        "status": status,
        "vector_bucket": vector_bucket,
        "vector_index": vector_index,
        "embedding_model": embedding_model,
        "error_message": error_message,
        "created_at": created_at,
        "updated_at": now,
    }
    if chunk_count is not None:
        item["chunk_count"] = chunk_count

    _client().put_item(TableName=config.MANIFEST_TABLE, Item=_serialize(item))


def mark_deleted(*, tenant_id: str, source_sk: str) -> None:
    """Flip an existing manifest record to ``DELETED``, preserving its history."""
    existing = get_record(tenant_id, source_sk)
    if not existing:
        return
    put_status(
        tenant_id=tenant_id,
        source_sk=source_sk,
        status=DELETED,
        source_bucket=existing.get("source_bucket", ""),
        source_key=existing.get("source_key", ""),
        source_version=existing.get("source_version", ""),
        sha256=existing.get("sha256", ""),
        vector_bucket=existing.get("vector_bucket", ""),
        vector_index=existing.get("vector_index", ""),
        embedding_model=existing.get("embedding_model", ""),
        chunk_count=existing.get("chunk_count"),
    )


def _serialize(item: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a plain dict into DynamoDB attribute-value form."""
    serialized: Dict[str, Any] = {}
    for key, value in item.items():
        if value is None:
            continue
        if isinstance(value, bool):
            serialized[key] = {"BOOL": value}
        elif isinstance(value, int):
            serialized[key] = {"N": str(value)}
        else:
            serialized[key] = {"S": str(value)}
    return serialized


def _deserialize(item: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a DynamoDB item back into a plain dict."""
    result: Dict[str, Any] = {}
    for key, value in item.items():
        if "S" in value:
            result[key] = value["S"]
        elif "N" in value:
            result[key] = int(value["N"])
        elif "BOOL" in value:
            result[key] = value["BOOL"]
    return result
