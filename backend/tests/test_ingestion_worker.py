"""
End-to-end unit tests for the event-driven RAG ingestion worker.

These exercise SQS/S3 event parsing, document validation, idempotency, vector
indexing, and the partial-batch failure contract, with all AWS clients stubbed
in-memory.
"""
from __future__ import annotations

import importlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]

INGEST_ENV = {
    "AWS_EC2_METADATA_DISABLED": "true",
    "DEFAULT_AWS_REGION": "eu-central-1",
    "SAGEMAKER_ENDPOINT": "embed-endpoint",
    "VECTOR_BUCKET": "vec-bucket",
    "VECTOR_INDEX": "vec-index",
    "EMBEDDING_MODEL": "test-model",
    "MANIFEST_TABLE": "manifest-table",
    "RAG_DOCS_BUCKET": "rag-docs",
    "CHUNK_SIZE": "1500",
    "CHUNK_OVERLAP": "200",
    "MAX_UPLOAD_BYTES": "1048576",
    "INGESTION_MAX_WORKERS": "2",
    "LOG_LEVEL": "INFO",
}


class FakeBody:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload


class FakeS3:
    """Minimal S3 stub backed by an in-memory ``key -> (bytes, version)`` map."""

    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key, VersionId=None):
        if Key not in self.objects:
            raise RuntimeError(f"NoSuchKey: {Key}")
        body, version = self.objects[Key]
        return {"Body": io.BytesIO(body), "VersionId": version}


class FakeDynamo:
    """In-memory DynamoDB stub storing attribute-value items."""

    def __init__(self):
        self.items = {}

    def get_item(self, TableName, Key, ConsistentRead=None):
        composite = (Key["tenant_id"]["S"], Key["source_sk"]["S"])
        item = self.items.get(composite)
        return {"Item": item} if item else {}

    def put_item(self, TableName, Item):
        composite = (Item["tenant_id"]["S"], Item["source_sk"]["S"])
        self.items[composite] = Item


class FakeSageMaker:
    def __init__(self, vector=None, fail=False):
        self.vector = vector or [0.1, 0.2, 0.3]
        self.fail = fail

    def invoke_endpoint(self, **kwargs):
        if self.fail:
            raise RuntimeError("sagemaker unavailable")
        return {"Body": FakeBody([self.vector])}


class FakeS3Vectors:
    def __init__(self, fail_on=None, fail_delete=False):
        self.fail_on = fail_on
        self.fail_delete = fail_delete
        self.puts = []
        self.deletes = []

    def put_vectors(self, **kwargs):
        vector_id = kwargs["vectors"][0]["key"]
        if self.fail_on is not None and vector_id == self.fail_on:
            raise RuntimeError("vector write failed")
        self.puts.append(kwargs)

    def delete_vectors(self, **kwargs):
        if self.fail_delete:
            raise RuntimeError("vector delete failed")
        self.deletes.append(kwargs)


def _load_ingestion(monkeypatch, env_overrides=None):
    """Import a fresh ingestion stack with a controlled environment."""
    monkeypatch.syspath_prepend(str(BACKEND_DIR))

    env = dict(INGEST_ENV)
    if env_overrides:
        env.update(env_overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    monkeypatch.setattr(boto3, "client", lambda *a, **k: SimpleNamespace())

    stale = [
        name
        for name in list(sys.modules)
        if name in ("server", "context", "lambda_handler")
        or name == "common"
        or name.startswith("common.")
        or name == "ingestion"
        or name.startswith("ingestion.")
    ]
    for name in stale:
        sys.modules.pop(name, None)

    worker = importlib.import_module("ingestion.worker")
    ingest_document = importlib.import_module("ingestion.ingest_document")
    manifest = importlib.import_module("ingestion.manifest")
    return SimpleNamespace(
        worker=worker, ingest_document=ingest_document, manifest=manifest
    )


@pytest.fixture
def ingestion(monkeypatch):
    """A loaded ingestion stack with default fakes wired in."""
    def _factory(objects=None, sagemaker=None, vectors=None, env_overrides=None):
        mods = _load_ingestion(monkeypatch, env_overrides)
        fake_s3 = FakeS3(objects or {})
        fake_dynamo = FakeDynamo()
        fake_sm = sagemaker or FakeSageMaker()
        fake_vec = vectors or FakeS3Vectors()

        mods.ingest_document._s3_client = fake_s3
        mods.ingest_document._sagemaker_client = fake_sm
        mods.ingest_document._s3vectors_client = fake_vec
        mods.manifest._dynamodb_client = fake_dynamo

        mods.s3 = fake_s3
        mods.dynamo = fake_dynamo
        mods.sagemaker = fake_sm
        mods.vectors = fake_vec
        return mods

    return _factory


def make_event(bucket, key, version_id="v1", message_id="m1", event_name=None):
    s3_record = {
        "s3": {
            "bucket": {"name": bucket},
            "object": {"key": key, "versionId": version_id},
        }
    }
    if event_name is not None:
        s3_record["eventName"] = event_name
    s3_body = {"Records": [s3_record]}
    return {"Records": [{"messageId": message_id, "body": json.dumps(s3_body)}]}


class TestHappyPath:
    def test_indexes_chunks_with_deterministic_ids_and_metadata(self, ingestion):
        content = "# Handbook\n\n" + ("Paragraph about housing. " * 200)
        mods = ingestion(
            objects={"incoming/tenant-a/handbook.md": (content.encode("utf-8"), "v1")}
        )

        event = make_event("rag-docs", "incoming/tenant-a/handbook.md")
        result = mods.worker.handler(event, None)

        assert result == {"batchItemFailures": []}
        assert len(mods.vectors.puts) >= 1

        by_id = {p["vectors"][0]["key"]: p["vectors"][0] for p in mods.vectors.puts}
        expected_ids = {
            f"tenant-a/handbook/v1/{i}" for i in range(len(mods.vectors.puts))
        }
        assert set(by_id) == expected_ids

        sample = by_id["tenant-a/handbook/v1/0"]["metadata"]
        assert sample["tenant_id"] == "tenant-a"
        assert sample["source_bucket"] == "rag-docs"
        assert sample["source_key"] == "incoming/tenant-a/handbook.md"
        assert sample["source_version"] == "v1"
        assert sample["title"] == "handbook"
        assert sample["doc_type"] == ".md"
        assert sample["embedding_model"] == "test-model"
        assert "ingested_at" in sample
        assert sample["chunk_text"]

        record = mods.manifest.get_record(
            "tenant-a", "incoming/tenant-a/handbook.md#v1"
        )
        assert record["status"] == "SUCCEEDED"
        assert record["chunk_count"] == len(mods.vectors.puts)

    def test_url_encoded_key_is_decoded(self, ingestion):
        mods = ingestion(
            objects={"incoming/tenant-a/my doc.md": (b"# Hello world", "v1")}
        )
        event = make_event("rag-docs", "incoming/tenant-a/my+doc.md")
        result = mods.worker.handler(event, None)

        assert result == {"batchItemFailures": []}
        assert mods.manifest.get_record(
            "tenant-a", "incoming/tenant-a/my doc.md#v1"
        )["status"] == "SUCCEEDED"

    def test_tenant_id_comes_from_parent_folder(self, ingestion):
        """Nested source layouts use the file's parent folder as tenant_id."""
        key = "incoming/Tenants/T001/T001.md"
        mods = ingestion(objects={key: (b"# Tenant T001\n\nbody", "v1")})

        result = mods.worker.handler(make_event("rag-docs", key), None)

        assert result == {"batchItemFailures": []}
        vector = mods.vectors.puts[0]["vectors"][0]
        assert vector["key"] == "T001/T001/v1/0"
        assert vector["metadata"]["tenant_id"] == "T001"
        assert vector["metadata"]["source_key"] == key
        assert mods.manifest.get_record("T001", f"{key}#v1")["status"] == "SUCCEEDED"


class TestIdempotency:
    def test_duplicate_upload_is_skipped(self, ingestion):
        mods = ingestion(
            objects={"incoming/tenant-a/doc.md": (b"# Title\n\nbody text", "v1")}
        )
        event = make_event("rag-docs", "incoming/tenant-a/doc.md")

        first = mods.worker.handler(event, None)
        assert first == {"batchItemFailures": []}
        puts_after_first = len(mods.vectors.puts)

        second = mods.worker.handler(event, None)
        assert second == {"batchItemFailures": []}

        # No additional vectors written on the duplicate run.
        assert len(mods.vectors.puts) == puts_after_first
        record = mods.manifest.get_record("tenant-a", "incoming/tenant-a/doc.md#v1")
        assert record["status"] == "SKIPPED"


class TestDeletion:
    def test_delete_event_removes_vectors_for_indexed_document(self, ingestion):
        content = "# Title\n\nbody text"
        mods = ingestion(
            objects={"incoming/tenant-a/doc.md": (content.encode("utf-8"), "v1")}
        )
        create_event = make_event("rag-docs", "incoming/tenant-a/doc.md")
        mods.worker.handler(create_event, None)
        chunk_count = len(mods.vectors.puts)

        delete_event = make_event(
            "rag-docs",
            "incoming/tenant-a/doc.md",
            message_id="del1",
            event_name="ObjectRemoved:Delete",
        )
        result = mods.worker.handler(delete_event, None)

        assert result == {"batchItemFailures": []}
        assert len(mods.vectors.deletes) == 1
        deleted_keys = set(mods.vectors.deletes[0]["keys"])
        assert deleted_keys == {
            f"tenant-a/doc/v1/{i}" for i in range(chunk_count)
        }

        record = mods.manifest.get_record("tenant-a", "incoming/tenant-a/doc.md#v1")
        assert record["status"] == "DELETED"

    def test_delete_event_with_no_manifest_record_is_a_noop(self, ingestion):
        mods = ingestion()
        delete_event = make_event(
            "rag-docs",
            "incoming/tenant-a/never-ingested.md",
            event_name="ObjectRemoved:Delete",
        )
        result = mods.worker.handler(delete_event, None)

        assert result == {"batchItemFailures": []}
        assert mods.vectors.deletes == []

    def test_duplicate_delete_event_is_idempotent(self, ingestion):
        mods = ingestion(
            objects={"incoming/tenant-a/doc.md": (b"# Title\n\nbody", "v1")}
        )
        mods.worker.handler(make_event("rag-docs", "incoming/tenant-a/doc.md"), None)

        delete_event = make_event(
            "rag-docs",
            "incoming/tenant-a/doc.md",
            message_id="del1",
            event_name="ObjectRemoved:Delete",
        )
        mods.worker.handler(delete_event, None)
        deletes_after_first = len(mods.vectors.deletes)

        second = mods.worker.handler(delete_event, None)

        assert second == {"batchItemFailures": []}
        assert len(mods.vectors.deletes) == deletes_after_first

    def test_delete_vector_failure_reports_batch_item_failure(self, ingestion):
        mods = ingestion(
            objects={"incoming/tenant-a/doc.md": (b"# Title\n\nbody", "v1")},
        )
        mods.worker.handler(make_event("rag-docs", "incoming/tenant-a/doc.md"), None)
        mods.ingest_document._s3vectors_client.fail_delete = True

        delete_event = make_event(
            "rag-docs",
            "incoming/tenant-a/doc.md",
            message_id="xyz",
            event_name="ObjectRemoved:Delete",
        )
        result = mods.worker.handler(delete_event, None)

        assert result == {"batchItemFailures": [{"itemIdentifier": "xyz"}]}
        record = mods.manifest.get_record("tenant-a", "incoming/tenant-a/doc.md#v1")
        assert record["status"] == "SUCCEEDED"

    def test_create_event_still_routes_to_indexing(self, ingestion):
        mods = ingestion(
            objects={"incoming/tenant-a/doc.md": (b"# Title\n\nbody", "v1")}
        )
        event = make_event(
            "rag-docs", "incoming/tenant-a/doc.md", event_name="ObjectCreated:Put"
        )
        result = mods.worker.handler(event, None)

        assert result == {"batchItemFailures": []}
        assert len(mods.vectors.puts) >= 1


class TestValidationFailures:
    @pytest.mark.parametrize(
        "key,body,env",
        [
            ("incoming/tenant-a/notes.txt", b"# hi", None),
            ("incoming/tenant-a/bad.md", b"\xff\xfe\x00bad", None),
            ("incoming/tenant-a/empty.md", b"", None),
            ("incoming/tenant-a/big.md", b"a" * 50, {"MAX_UPLOAD_BYTES": "10"}),
        ],
        ids=["bad-extension", "bad-utf8", "empty", "too-large"],
    )
    def test_invalid_documents_marked_failed_and_acked(self, ingestion, key, body, env):
        mods = ingestion(objects={key: (body, "v1")}, env_overrides=env)
        event = make_event("rag-docs", key)

        result = mods.worker.handler(event, None)

        # Permanent failure -> acknowledged, not retried.
        assert result == {"batchItemFailures": []}
        assert mods.vectors.puts == []

        source_sk = f"{key}#v1"
        record = mods.manifest.get_record("tenant-a", source_sk)
        assert record["status"] == "FAILED"
        assert record["error_message"]

    def test_unparseable_key_is_acked_without_manifest(self, ingestion):
        mods = ingestion(objects={"handbook.md": (b"# hi", "v1")})
        event = make_event("rag-docs", "handbook.md")

        result = mods.worker.handler(event, None)
        assert result == {"batchItemFailures": []}
        assert mods.vectors.puts == []


class TestTransientFailures:
    def test_embedding_failure_reports_batch_item_failure(self, ingestion):
        mods = ingestion(
            objects={"incoming/tenant-a/doc.md": (b"# Title\n\nbody", "v1")},
            sagemaker=FakeSageMaker(fail=True),
        )
        event = make_event("rag-docs", "incoming/tenant-a/doc.md", message_id="abc")

        result = mods.worker.handler(event, None)

        assert result == {"batchItemFailures": [{"itemIdentifier": "abc"}]}
        record = mods.manifest.get_record("tenant-a", "incoming/tenant-a/doc.md#v1")
        assert record["status"] == "FAILED"

    def test_vector_write_failure_reports_batch_item_failure(self, ingestion):
        mods = ingestion(
            objects={"incoming/tenant-a/doc.md": (b"# Title\n\nbody", "v1")},
            vectors=FakeS3Vectors(fail_on="tenant-a/doc/v1/0"),
        )
        event = make_event("rag-docs", "incoming/tenant-a/doc.md", message_id="xyz")

        result = mods.worker.handler(event, None)

        assert result == {"batchItemFailures": [{"itemIdentifier": "xyz"}]}
        record = mods.manifest.get_record("tenant-a", "incoming/tenant-a/doc.md#v1")
        assert record["status"] == "FAILED"


class TestEventParsing:
    def test_s3_test_event_is_ignored(self, ingestion):
        mods = ingestion()
        event = {
            "Records": [
                {"messageId": "m1", "body": json.dumps({"Event": "s3:TestEvent"})}
            ]
        }
        result = mods.worker.handler(event, None)
        assert result == {"batchItemFailures": []}
        assert mods.vectors.puts == []

    def test_malformed_body_is_acked(self, ingestion):
        mods = ingestion()
        event = {"Records": [{"messageId": "m1", "body": "not-json"}]}
        result = mods.worker.handler(event, None)
        assert result == {"batchItemFailures": []}

    def test_missing_body_is_acked(self, ingestion):
        mods = ingestion()
        event = {"Records": [{"messageId": "m1"}]}
        result = mods.worker.handler(event, None)
        assert result == {"batchItemFailures": []}

    def test_missing_bucket_or_key_is_acked(self, ingestion):
        mods = ingestion()
        body = {"Records": [{"s3": {"bucket": {}, "object": {}}}]}
        event = {"Records": [{"messageId": "m1", "body": json.dumps(body)}]}
        result = mods.worker.handler(event, None)
        assert result == {"batchItemFailures": []}

    def test_mixed_batch_only_reports_failed_message(self, ingestion):
        mods = ingestion(
            objects={
                "incoming/tenant-a/ok.md": (b"# ok\n\nbody", "v1"),
                "incoming/tenant-a/missing.md": (b"# x", "v1"),
            }
        )
        # Second record points at a key the S3 stub does not have -> transient.
        good = make_event("rag-docs", "incoming/tenant-a/ok.md", message_id="good")[
            "Records"
        ][0]
        bad = make_event(
            "rag-docs", "incoming/tenant-a/nope.md", message_id="bad"
        )["Records"][0]
        event = {"Records": [good, bad]}

        result = mods.worker.handler(event, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


class TestManifestHelpers:
    def test_build_sort_key(self, ingestion):
        mods = ingestion()
        assert (
            mods.manifest.build_sort_key("incoming/t/d.md", "v9")
            == "incoming/t/d.md#v9"
        )

    def test_serialize_roundtrip(self, ingestion):
        mods = ingestion()
        mods.manifest.put_status(
            tenant_id="tenant-a",
            source_sk="incoming/tenant-a/d.md#v1",
            status="SUCCEEDED",
            source_bucket="rag-docs",
            source_key="incoming/tenant-a/d.md",
            source_version="v1",
            sha256="deadbeef",
            chunk_count=3,
        )
        record = mods.manifest.get_record("tenant-a", "incoming/tenant-a/d.md#v1")
        assert record["status"] == "SUCCEEDED"
        assert record["chunk_count"] == 3
        assert record["sha256"] == "deadbeef"
        assert record["created_at"]
        assert record["updated_at"]
