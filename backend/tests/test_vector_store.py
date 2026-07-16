"""Unit tests for the S3 Vectors client wrapper in common.vector_store."""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from common import vector_store  # noqa: E402


class FakeClient:
    def __init__(self):
        self.calls = []

    def delete_vectors(self, **kwargs):
        self.calls.append(kwargs)


def test_delete_vectors_empty_list_is_a_noop():
    client = FakeClient()
    result = vector_store.delete_vectors(
        client=client, bucket="b", index="i", vector_ids=[]
    )
    assert result == []
    assert client.calls == []


def test_delete_vectors_single_batch():
    client = FakeClient()
    ids = [f"tenant/doc/v1/{i}" for i in range(3)]
    result = vector_store.delete_vectors(
        client=client, bucket="b", index="i", vector_ids=ids
    )
    assert result == ids
    assert client.calls == [
        {"vectorBucketName": "b", "indexName": "i", "keys": ids}
    ]


def test_delete_vectors_splits_into_batches_of_500():
    client = FakeClient()
    ids = [f"tenant/doc/v1/{i}" for i in range(1200)]
    vector_store.delete_vectors(client=client, bucket="b", index="i", vector_ids=ids)

    assert len(client.calls) == 3
    assert [len(call["keys"]) for call in client.calls] == [500, 500, 200]
    assert client.calls[0]["keys"] == ids[:500]
    assert client.calls[-1]["keys"] == ids[1000:]
