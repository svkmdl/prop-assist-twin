"""S3 Vectors read/write helpers, decoupled from global client state.

Both the chat API (queries) and the ingestion worker (writes) share these
primitives. Clients, bucket, and index are passed explicitly so callers own
their own boto3 ``s3vectors`` client.
"""
from typing import Any, Dict, List, Optional

_MAX_DELETE_KEYS_PER_CALL = 500  # S3 Vectors DeleteVectors 'keys' shape max


def put_vector(
    *,
    client: Any,
    bucket: str,
    index: str,
    vector_id: str,
    embedding: List[float],
    metadata: Dict[str, Any],
) -> str:
    """Write a single vector to an S3 Vectors index.

    Args:
        client: A boto3 ``s3vectors`` client (or compatible stub).
        bucket: The vector bucket name.
        index: The vector index name.
        vector_id: Deterministic key for the vector (enables idempotent rewrite).
        embedding: The embedding vector.
        metadata: Metadata to store alongside the vector.

    Returns:
        The ``vector_id`` that was written.
    """
    client.put_vectors(
        vectorBucketName=bucket,
        indexName=index,
        vectors=[
            {
                "key": vector_id,
                "data": {"float32": [float(x) for x in embedding]},
                "metadata": metadata,
            }
        ],
    )
    return vector_id


def query_vectors(
    *,
    client: Any,
    bucket: str,
    index: str,
    embedding: List[float],
    top_k: int,
    metadata_filter: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Query an S3 Vectors index and return the matched vectors.

    Args:
        client: A boto3 ``s3vectors`` client (or compatible stub).
        bucket: The vector bucket name.
        index: The vector index name.
        embedding: The query embedding vector.
        top_k: Number of nearest neighbours to return.
        metadata_filter: Optional S3 Vectors metadata filter expression
            (e.g. ``{"tenant_id": {"$eq": "T001"}}``).  When *None* no
            filter is applied and all vectors are eligible.

    Returns:
        The list of matched vector dicts (possibly empty).
    """
    kwargs: Dict[str, Any] = dict(
        vectorBucketName=bucket,
        indexName=index,
        queryVector={"float32": [float(x) for x in embedding]},
        topK=top_k,
        returnDistance=True,
        returnMetadata=True,
    )
    if metadata_filter is not None:
        kwargs["filter"] = metadata_filter
    response = client.query_vectors(**kwargs)
    return response.get("vectors", [])


def delete_vectors(
    *,
    client: Any,
    bucket: str,
    index: str,
    vector_ids: List[str],
) -> List[str]:
    """Delete vectors by key from an S3 Vectors index.

    Args:
        client: A boto3 ``s3vectors`` client (or compatible stub).
        bucket: The vector bucket name.
        index: The vector index name.
        vector_ids: Deterministic keys to remove.

    Returns:
        The ``vector_ids`` that were submitted for deletion.
    """
    if not vector_ids:
        return []
    for start in range(0, len(vector_ids), _MAX_DELETE_KEYS_PER_CALL):
        batch = vector_ids[start : start + _MAX_DELETE_KEYS_PER_CALL]
        client.delete_vectors(vectorBucketName=bucket, indexName=index, keys=batch)
    return vector_ids
