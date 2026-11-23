"""
End-to-end unit tests for the event-driven RAG ingestion worker.

These exercise SQS/S3 event parsing, the document key grammar and front matter
(ADR 0002), idempotency, vector indexing, business versions, supersession of
overwritten S3 versions and the partial-batch failure contract, with all AWS
clients stubbed in-memory.

S3 version ids in these tests are ``ver1``/``ver2`` so they cannot be confused
with the business version in the key (``.../v1.md``).
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
from botocore.exceptions import ClientError

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
    """Minimal versioned S3 stub.

    ``objects`` maps ``key -> (bytes, current_version)``. Every version ever
    stored is kept in ``history`` so older versions stay readable by VersionId.
    """

    def __init__(self, objects):
        self.objects = objects
        self.history = {
            (key, version): body for key, (body, version) in objects.items()
        }

    def put(self, key, body, version):
        """Simulate an overwrite: ``version`` becomes the current version."""
        self.objects[key] = (body, version)
        self.history[(key, version)] = body

    def remove(self, key):
        """Simulate a delete marker: no current version, history retained."""
        self.objects.pop(key, None)

    def get_object(self, Bucket, Key, VersionId=None):
        if VersionId is not None and (Key, VersionId) in self.history:
            return {"Body": io.BytesIO(self.history[(Key, VersionId)]), "VersionId": VersionId}
        if Key not in self.objects:
            raise RuntimeError(f"NoSuchKey: {Key}")
        body, version = self.objects[Key]
        return {"Body": io.BytesIO(body), "VersionId": version}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
            )
        _body, version = self.objects[Key]
        return {"VersionId": version}


class FakeDynamo:
    """In-memory DynamoDB stub storing attribute-value items."""

    def __init__(self):
        self.items = {}

    def get_item(self, TableName, Key, ConsistentRead=None):
        composite = (Key["scope_key"]["S"], Key["source_sk"]["S"])
        item = self.items.get(composite)
        return {"Item": item} if item else {}

    def put_item(self, TableName, Item):
        composite = (Item["scope_key"]["S"], Item["source_sk"]["S"])
        self.items[composite] = Item

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeValues,
              ConsistentRead=None, ExclusiveStartKey=None):
        assert KeyConditionExpression == "scope_key = :t AND begins_with(source_sk, :p)"
        scope = ExpressionAttributeValues[":t"]["S"]
        prefix = ExpressionAttributeValues[":p"]["S"]
        matches = [
            item for (t, sk), item in sorted(self.items.items())
            if t == scope and sk.startswith(prefix)
        ]
        # Return one item per page to exercise pagination.
        start = int(ExclusiveStartKey["i"]["N"]) if ExclusiveStartKey else 0
        page = matches[start:start + 1]
        response = {"Items": page}
        if start + 1 < len(matches):
            response["LastEvaluatedKey"] = {"i": {"N": str(start + 1)}}
        return response


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


def make_event(bucket, key, version_id="ver1", message_id="m1", event_name=None):
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


def doc(body="Inhalt des Dokuments.", title="Mietvertrag", frm="2026-01-01", to=None,
        language="de", extra=""):
    """A valid document: front matter + markdown body, as bytes."""
    lines = ["---", f"title: {title}", f"language: {language}", f"effective_from: {frm}"]
    if to:
        lines.append(f"effective_to: {to}")
    if extra:
        lines.append(extra)
    return ("\n".join(lines) + f"\n---\n{body}\n").encode("utf-8")


LEASE_SCOPE = "renters/leases/L-0001"
DOC = f"incoming/{LEASE_SCOPE}/contracts/lease-contract/v1.md"


def vid(source_key, s3_version, chunk_index):
    """The expected deterministic vector id (kept literal on purpose, see test_doc_keys)."""
    from urllib.parse import quote

    relative = source_key[len("incoming/"):-len(".md")]
    return f"{relative}/{quote(s3_version, safe='')}/{chunk_index}"


def _vector_ids(mods):
    return {p["vectors"][0]["key"] for p in mods.vectors.puts}


def _deleted_ids(mods):
    return {key for d in mods.vectors.deletes for key in d["keys"]}


def _status(mods, s3_version, key=DOC, scope=LEASE_SCOPE):
    return mods.manifest.get_record(scope, f"{key}#{s3_version}")["status"]


class TestHappyPath:
    def test_indexes_chunks_with_deterministic_ids_and_metadata(self, ingestion):
        body = "# Mietvertrag\n\n" + ("Paragraph about housing. " * 200)
        mods = ingestion(objects={DOC: (doc(body=body, to="2027-12-31"), "ver1")})

        result = mods.worker.handler(make_event("rag-docs", DOC), None)

        assert result == {"batchItemFailures": []}
        assert len(mods.vectors.puts) >= 2
        by_id = {p["vectors"][0]["key"]: p["vectors"][0] for p in mods.vectors.puts}
        assert set(by_id) == {vid(DOC, "ver1", i) for i in range(len(mods.vectors.puts))}

        sample = by_id[vid(DOC, "ver1", 0)]["metadata"]
        assert sample == {
            "audience": "renter",
            "scope_type": "lease",
            "lease_id": "L-0001",
            "doc_type": "contracts",
            "doc_id": "lease-contract",
            "doc_version": 1,
            "language": "de",
            "effective_from": 20260101,
            "effective_to": 20271231,
            "title": "Mietvertrag",
            "source_key": DOC,
            "source_bucket": "rag-docs",
            "source_version": "ver1",
            "chunk_index": 0,
            "chunk_text": sample["chunk_text"],
            "embedding_model": "test-model",
            "ingested_at": sample["ingested_at"],
        }
        assert sample["chunk_text"] and "ingested_at" in sample
        # The front-matter block is not embedded, and no tenant partition is written.
        assert not any("effective_from" in v["metadata"]["chunk_text"] for v in by_id.values())
        assert "tenant_id" not in sample and "property_id" not in sample

        record = mods.manifest.get_record(LEASE_SCOPE, f"{DOC}#ver1")
        assert record["status"] == "SUCCEEDED"
        assert record["chunk_count"] == len(mods.vectors.puts)

    def test_open_ended_documents_use_the_sentinel(self, ingestion):
        mods = ingestion(objects={DOC: (doc(), "ver1")})
        mods.worker.handler(make_event("rag-docs", DOC), None)
        assert mods.vectors.puts[0]["vectors"][0]["metadata"]["effective_to"] == 99991231

    @pytest.mark.parametrize(
        "key,scope,audience,scope_type,property_id,lease_id",
        [
            ("incoming/staff/company/sops/water-damage/v1.md", "staff/company", "staff", "company", None, None),
            ("incoming/renters/company/policies/repair-policy/v1.md", "renters/company", "renter", "company", None, None),
            ("incoming/staff/properties/P-001/sops/heating-vendor/v1.md", "staff/properties/P-001", "staff", "property", "P-001", None),
            ("incoming/renters/properties/P-002/policies/house-rules/v1.md", "renters/properties/P-002", "renter", "property", "P-002", None),
            ("incoming/renters/leases/L-0003/statements/utility-2026/v1.md", "renters/leases/L-0003", "renter", "lease", None, "L-0003"),
        ],
    )
    def test_every_scope_row_is_indexed_with_its_own_partition_and_metadata(
        self, ingestion, key, scope, audience, scope_type, property_id, lease_id
    ):
        mods = ingestion(objects={key: (doc(), "ver1")})

        assert mods.worker.handler(make_event("rag-docs", key), None) == {"batchItemFailures": []}

        metadata = mods.vectors.puts[0]["vectors"][0]["metadata"]
        assert (metadata["audience"], metadata["scope_type"]) == (audience, scope_type)
        assert metadata.get("property_id") == property_id and metadata.get("lease_id") == lease_id
        assert mods.vectors.puts[0]["vectors"][0]["key"] == vid(key, "ver1", 0)
        assert mods.manifest.get_record(scope, f"{key}#ver1")["status"] == "SUCCEEDED"
        assert [k for k in mods.dynamo.items] == [(scope, f"{key}#ver1")]

    def test_percent_encoded_keys_are_decoded_before_parsing(self, ingestion):
        key = "incoming/renters/company/policies/repair-policy/v1.md"
        mods = ingestion(objects={key: (doc(), "ver1")})
        event = make_event("rag-docs", "incoming/renters/company/policies/repair%2Dpolicy/v1.md")

        assert mods.worker.handler(event, None) == {"batchItemFailures": []}
        assert mods.manifest.get_record("renters/company", f"{key}#ver1")["status"] == "SUCCEEDED"

    def test_equal_filenames_in_different_scopes_do_not_collide(self, ingestion):
        a = "incoming/renters/leases/L-0001/contracts/lease-contract/v1.md"
        b = "incoming/renters/leases/L-0002/contracts/lease-contract/v1.md"
        c = "incoming/staff/company/policies/lease-contract/v1.md"
        mods = ingestion(objects={a: (doc(), "ver1"), b: (doc(), "ver1"), c: (doc(), "ver1")})
        for index, key in enumerate((a, b, c)):
            mods.worker.handler(make_event("rag-docs", key, message_id=f"m{index}"), None)

        assert len(_vector_ids(mods)) == 3
        assert {scope for scope, _ in mods.dynamo.items} == {
            "renters/leases/L-0001", "renters/leases/L-0002", "staff/company",
        }


class TestBusinessVersions:
    V1 = "incoming/renters/company/policies/repair-policy/v1.md"
    V2 = "incoming/renters/company/policies/repair-policy/v2.md"

    def test_business_versions_are_separate_keys_and_both_stay_indexed(self, ingestion):
        mods = ingestion(
            objects={
                self.V1: (doc(body="Alte Regel.", frm="2026-01-01", to="2026-12-31"), "ver1"),
                self.V2: (doc(body="Neue Regel.", frm="2027-01-01"), "ver1"),
            }
        )
        mods.worker.handler(make_event("rag-docs", self.V1, message_id="a"), None)
        mods.worker.handler(make_event("rag-docs", self.V2, message_id="b"), None)

        assert mods.vectors.deletes == []  # nothing superseded: they are different documents
        metadata = {
            p["vectors"][0]["metadata"]["doc_version"]: p["vectors"][0]["metadata"]
            for p in mods.vectors.puts
        }
        assert metadata[1]["effective_to"] == 20261231
        assert metadata[2]["effective_from"] == 20270101 and metadata[2]["effective_to"] == 99991231
        assert _vector_ids(mods) == {vid(self.V1, "ver1", 0), vid(self.V2, "ver1", 0)}
        for key in (self.V1, self.V2):
            assert _status(mods, "ver1", key=key, scope="renters/company") == "SUCCEEDED"

    def test_overwriting_one_business_version_supersedes_only_that_keys_old_s3_version(self, ingestion):
        mods = ingestion(
            objects={
                self.V1: (doc(body="Alte Regel.", to="2026-12-31"), "ver1"),
                self.V2: (doc(body="Neue Regel.", frm="2027-01-01"), "ver1"),
            }
        )
        mods.worker.handler(make_event("rag-docs", self.V1, message_id="a"), None)
        mods.worker.handler(make_event("rag-docs", self.V2, message_id="b"), None)
        mods.s3.put(self.V2, doc(body="Korrigierte Regel.", frm="2027-01-01"), "ver2")
        mods.worker.handler(make_event("rag-docs", self.V2, version_id="ver2", message_id="c"), None)

        assert _deleted_ids(mods) == {vid(self.V2, "ver1", 0)}
        assert _status(mods, "ver1", key=self.V1, scope="renters/company") == "SUCCEEDED"
        assert _status(mods, "ver1", key=self.V2, scope="renters/company") == "SUPERSEDED"
        assert _status(mods, "ver2", key=self.V2, scope="renters/company") == "SUCCEEDED"


class TestRealisticS3VersionIds:
    """Review finding: S3 VersionIds may contain '/' and '+'; they used to end up in the DLQ."""

    OLD = "3/L4kqtJlcpXroDTDmJ+rmSp+XXXX"
    NEW = "Xu+1gQq/0fGvHNbY.Z_2"

    def test_indexing_supersession_and_deletion_work_with_them(self, ingestion):
        mods = ingestion(objects={DOC: (doc(body="Alt."), self.OLD)})
        assert mods.worker.handler(make_event("rag-docs", DOC, version_id=self.OLD), None) == {"batchItemFailures": []}
        old_ids = _vector_ids(mods)
        assert old_ids == {vid(DOC, self.OLD, 0)} and "%2F" in next(iter(old_ids))

        mods.s3.put(DOC, doc(body="Neu."), self.NEW)
        assert mods.worker.handler(make_event("rag-docs", DOC, version_id=self.NEW, message_id="m2"), None) == {"batchItemFailures": []}
        assert _deleted_ids(mods) == old_ids  # supersession built the same ids
        assert _status(mods, self.OLD) == "SUPERSEDED"

        delete = make_event("rag-docs", DOC, version_id=self.NEW, message_id="m3", event_name="ObjectRemoved:Delete")
        assert mods.worker.handler(delete, None) == {"batchItemFailures": []}
        assert vid(DOC, self.NEW, 0) in _deleted_ids(mods)
        assert _status(mods, self.NEW) == "DELETED"


class TestIdempotency:
    def test_duplicate_upload_is_skipped(self, ingestion):
        mods = ingestion(objects={DOC: (doc(), "ver1")})
        event = make_event("rag-docs", DOC)

        assert mods.worker.handler(event, None) == {"batchItemFailures": []}
        puts_after_first = len(mods.vectors.puts)

        assert mods.worker.handler(event, None) == {"batchItemFailures": []}

        assert len(mods.vectors.puts) == puts_after_first  # nothing written twice
        assert _status(mods, "ver1") == "SKIPPED"


class TestDeletion:
    def delete_event(self, key=DOC, message_id="del1", version_id="ver1"):
        return make_event(
            "rag-docs", key, version_id=version_id, message_id=message_id,
            event_name="ObjectRemoved:Delete",
        )

    def test_delete_event_removes_vectors_for_indexed_document(self, ingestion):
        mods = ingestion(objects={DOC: (doc(), "ver1")})
        mods.worker.handler(make_event("rag-docs", DOC), None)
        chunk_count = len(mods.vectors.puts)

        assert mods.worker.handler(self.delete_event(), None) == {"batchItemFailures": []}

        assert len(mods.vectors.deletes) == 1
        assert set(mods.vectors.deletes[0]["keys"]) == {vid(DOC, "ver1", i) for i in range(chunk_count)}
        assert _status(mods, "ver1") == "DELETED"

    def test_delete_event_with_no_manifest_record_is_a_noop(self, ingestion):
        mods = ingestion()
        key = "incoming/renters/leases/L-0001/contracts/never-ingested/v1.md"
        assert mods.worker.handler(self.delete_event(key=key), None) == {"batchItemFailures": []}
        assert mods.vectors.deletes == []

    def test_duplicate_delete_event_is_idempotent(self, ingestion):
        mods = ingestion(objects={DOC: (doc(), "ver1")})
        mods.worker.handler(make_event("rag-docs", DOC), None)
        mods.worker.handler(self.delete_event(), None)
        deletes_after_first = len(mods.vectors.deletes)

        assert mods.worker.handler(self.delete_event(), None) == {"batchItemFailures": []}
        assert len(mods.vectors.deletes) == deletes_after_first

    def test_delete_vector_failure_reports_batch_item_failure(self, ingestion):
        mods = ingestion(objects={DOC: (doc(), "ver1")})
        mods.worker.handler(make_event("rag-docs", DOC), None)
        mods.ingest_document._s3vectors_client.fail_delete = True

        result = mods.worker.handler(self.delete_event(message_id="xyz"), None)

        assert result == {"batchItemFailures": [{"itemIdentifier": "xyz"}]}
        assert _status(mods, "ver1") == "SUCCEEDED"

    def test_delete_without_a_version_id_is_acked(self, ingestion):
        mods = ingestion(objects={DOC: (doc(), "ver1")})
        mods.worker.handler(make_event("rag-docs", DOC), None)
        result = mods.worker.handler(self.delete_event(version_id=None), None)
        assert result == {"batchItemFailures": []} and mods.vectors.deletes == []

    def test_create_event_still_routes_to_indexing(self, ingestion):
        mods = ingestion(objects={DOC: (doc(), "ver1")})
        event = make_event("rag-docs", DOC, event_name="ObjectCreated:Put")
        assert mods.worker.handler(event, None) == {"batchItemFailures": []}
        assert len(mods.vectors.puts) >= 1


class TestOverwriteSupersedes:
    def test_new_version_removes_previous_version_vectors(self, ingestion):
        mods = ingestion(objects={DOC: (doc(body="Alt."), "ver1")})
        mods.worker.handler(make_event("rag-docs", DOC, version_id="ver1"), None)
        v1_ids = _vector_ids(mods)
        assert v1_ids and mods.vectors.deletes == []

        mods.s3.put(DOC, doc(body="Neu."), "ver2")
        result = mods.worker.handler(
            make_event("rag-docs", DOC, version_id="ver2", message_id="m2"), None
        )

        assert result == {"batchItemFailures": []}
        v2_ids = _vector_ids(mods) - v1_ids
        assert v2_ids and all(i.startswith(f"{LEASE_SCOPE}/contracts/lease-contract/v1/ver2/") for i in v2_ids)
        assert _deleted_ids(mods) == v1_ids
        assert _status(mods, "ver1") == "SUPERSEDED"
        assert _status(mods, "ver2") == "SUCCEEDED"

    def test_out_of_order_delivery_keeps_current_version(self, ingestion):
        """ver2 (current) is processed before the late ver1 event; ver1 must not win."""
        mods = ingestion(objects={DOC: (doc(body="Alt."), "ver1")})
        mods.s3.put(DOC, doc(body="Neu."), "ver2")

        mods.worker.handler(make_event("rag-docs", DOC, version_id="ver2"), None)
        assert mods.vectors.deletes == []

        result = mods.worker.handler(
            make_event("rag-docs", DOC, version_id="ver1", message_id="m-late"), None
        )

        assert result == {"batchItemFailures": []}
        deleted = _deleted_ids(mods)
        assert deleted and all("/ver1/" in i for i in deleted)
        assert _status(mods, "ver1") == "SUPERSEDED"
        assert _status(mods, "ver2") == "SUCCEEDED"

    def test_superseded_version_delete_event_is_noop(self, ingestion):
        mods = ingestion(objects={DOC: (doc(body="Alt."), "ver1")})
        mods.worker.handler(make_event("rag-docs", DOC, version_id="ver1"), None)
        mods.s3.put(DOC, doc(body="Neu."), "ver2")
        mods.worker.handler(make_event("rag-docs", DOC, version_id="ver2"), None)
        deletes_before = len(mods.vectors.deletes)

        result = mods.worker.handler(
            make_event("rag-docs", DOC, version_id="ver1", event_name="ObjectRemoved:Delete"),
            None,
        )

        assert result == {"batchItemFailures": []}
        assert len(mods.vectors.deletes) == deletes_before
        assert _status(mods, "ver1") == "SUPERSEDED"

    def test_failed_old_version_is_left_alone(self, ingestion):
        mods = ingestion(objects={DOC: (b"", "ver1")})  # empty -> FAILED
        mods.worker.handler(make_event("rag-docs", DOC, version_id="ver1"), None)
        assert _status(mods, "ver1") == "FAILED"

        mods.s3.put(DOC, doc(body="Neu."), "ver2")
        mods.worker.handler(make_event("rag-docs", DOC, version_id="ver2"), None)

        assert mods.vectors.deletes == []
        assert _status(mods, "ver1") == "FAILED"

    def test_object_gone_skips_cleanup(self, ingestion):
        """If the key has no current version (deleted), nothing is superseded."""
        mods = ingestion(objects={DOC: (doc(body="Alt."), "ver1")})
        mods.worker.handler(make_event("rag-docs", DOC, version_id="ver1"), None)
        mods.s3.put(DOC, doc(body="Neu."), "ver2")
        mods.s3.remove(DOC)

        result = mods.worker.handler(
            make_event("rag-docs", DOC, version_id="ver2", message_id="m2"), None
        )

        assert result == {"batchItemFailures": []}
        assert mods.vectors.deletes == []
        assert _status(mods, "ver1") == "SUCCEEDED"

    def test_unversioned_object_skips_cleanup(self, ingestion):
        mods = ingestion(objects={DOC: (doc(body="Alt."), None)})
        mods.worker.handler(make_event("rag-docs", DOC, version_id=None), None)
        mods.s3.put(DOC, doc(body="Neu."), None)
        result = mods.worker.handler(
            make_event("rag-docs", DOC, version_id=None, message_id="m2"), None
        )

        assert result == {"batchItemFailures": []}
        assert mods.vectors.deletes == []

    def test_head_object_error_other_than_404_is_retried(self, ingestion):
        mods = ingestion(objects={DOC: (doc(), "ver1")})

        def boom(**kwargs):
            raise ClientError(
                {"Error": {"Code": "SlowDown", "Message": "throttled"}}, "HeadObject"
            )

        mods.s3.head_object = boom
        result = mods.worker.handler(make_event("rag-docs", DOC, version_id="ver1"), None)

        assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}

    def test_cleanup_failure_is_retried_and_healed_on_redelivery(self, ingestion):
        mods = ingestion(objects={DOC: (doc(body="Alt."), "ver1")})
        mods.worker.handler(make_event("rag-docs", DOC, version_id="ver1"), None)
        v1_ids = _vector_ids(mods)
        mods.s3.put(DOC, doc(body="Neu."), "ver2")

        mods.vectors.fail_delete = True
        event = make_event("rag-docs", DOC, version_id="ver2", message_id="m2")
        assert mods.worker.handler(event, None) == {
            "batchItemFailures": [{"itemIdentifier": "m2"}]
        }
        assert _status(mods, "ver1") == "SUCCEEDED"

        # SQS redelivers; ver2 is already SUCCEEDED so it is skipped, but the
        # stale ver1 vectors are still cleaned up.
        mods.vectors.fail_delete = False
        assert mods.worker.handler(event, None) == {"batchItemFailures": []}
        assert _deleted_ids(mods) == v1_ids
        assert _status(mods, "ver1") == "SUPERSEDED"
        assert _status(mods, "ver2") == "SKIPPED"

    def test_supersession_stays_inside_the_documents_own_scope(self, ingestion):
        """The same file name in another lease is a different document and is never touched."""
        other = "incoming/renters/leases/L-0002/contracts/lease-contract/v1.md"
        mods = ingestion(objects={other: (doc(), "ver1"), DOC: (doc(body="Alt."), "ver1")})
        mods.worker.handler(make_event("rag-docs", other, version_id="ver1"), None)
        mods.worker.handler(make_event("rag-docs", DOC, version_id="ver1"), None)
        mods.s3.put(DOC, doc(body="Neu."), "ver2")
        mods.worker.handler(make_event("rag-docs", DOC, version_id="ver2"), None)

        assert _status(mods, "ver1", key=other, scope="renters/leases/L-0002") == "SUCCEEDED"
        assert all(i.startswith(f"{LEASE_SCOPE}/") for i in _deleted_ids(mods))
        assert _status(mods, "ver1") == "SUPERSEDED"


class TestValidationFailures:
    @pytest.mark.parametrize(
        "body,env,fragment",
        [
            (b"# no front matter\n\nbody", None, "front-matter"),
            (b"\xff\xfe\x00bad", None, "UTF-8"),
            (b"", None, "empty"),
            (doc(body="x" * 200), {"MAX_UPLOAD_BYTES": "50"}, "over the 50 byte limit"),
            (doc(language="fr"), None, "language must be one of"),
            (doc(extra="colour: blue"), None, "unknown key 'colour'"),
            (doc(frm="2027-1-1"), None, "effective_from"),
            (doc(frm="2027-06-01", to="2027-01-01"), None, "effective_to must not be before"),
            (doc(body="   "), None, "no content"),
        ],
        ids=["no-front-matter", "bad-utf8", "empty", "too-large", "bad-language",
             "unknown-key", "bad-date", "inverted-window", "blank-body"],
    )
    def test_invalid_documents_marked_failed_and_acked(self, ingestion, body, env, fragment):
        mods = ingestion(objects={DOC: (body, "ver1")}, env_overrides=env)

        result = mods.worker.handler(make_event("rag-docs", DOC), None)

        assert result == {"batchItemFailures": []}  # permanent failure: acked, not retried
        assert mods.vectors.puts == []
        record = mods.manifest.get_record(LEASE_SCOPE, f"{DOC}#ver1")
        assert record["status"] == "FAILED"
        assert fragment in record["error_message"]

    def test_every_problem_is_recorded_and_the_message_is_bounded(self, ingestion):
        bad = b"---\ntitel: x\nlanguage: fr\neffective_from: 2027-1-1\n---\n"
        mods = ingestion(objects={DOC: (bad, "ver1")})
        mods.worker.handler(make_event("rag-docs", DOC), None)
        message = mods.manifest.get_record(LEASE_SCOPE, f"{DOC}#ver1")["error_message"]
        for fragment in ("unknown key 'titel'", "language must be one of", "effective_from", "no content"):
            assert fragment in message
        assert len(message) <= 1000

    LEGACY_AND_INVALID_KEYS = [
        "incoming/T001/faq.md",  # the former tenant-folder layout
        "incoming/Tenants/T001/faq.md",
        "incoming/admin/notes.md",
        "handbook.md",
        "incoming/renters/company/policies/repair-policy/latest.md",
        "incoming/renters/company/policies/repair-policy/v1.txt",
        "incoming/renters/company/faq/repair-policy/v1.md",
        "incoming/staff/leases/L-0001/contracts/x/v1.md",
        "incoming/renters/leases/L-1/contracts/x/v1.md",
        "incoming/renters/properties/P-001/units/U-1/policies/x/v1.md",
        "incoming/renters/company/policies/repair-policy/v1.md#copy.md",
        "incoming/renters/company/policies/repair+policy/v1.md",  # '+' decodes to a space
    ]

    @pytest.mark.parametrize("key", LEGACY_AND_INVALID_KEYS)
    def test_keys_outside_the_grammar_are_acked_without_touching_s3_or_the_manifest(self, ingestion, key):
        mods = ingestion()  # S3 holds nothing: a read attempt would be a retried (transient) error

        result = mods.worker.handler(make_event("rag-docs", key), None)

        assert result == {"batchItemFailures": []}
        assert mods.vectors.puts == [] and mods.dynamo.items == {}

    @pytest.mark.parametrize("key", LEGACY_AND_INVALID_KEYS)
    def test_delete_events_for_such_keys_are_acked_and_delete_nothing(self, ingestion, key):
        """Cutover safety: deleting the old documents must not wedge the queue or the DLQ."""
        mods = ingestion()
        event = make_event("rag-docs", key, event_name="ObjectRemoved:Delete")

        assert mods.worker.handler(event, None) == {"batchItemFailures": []}
        assert mods.vectors.deletes == [] and mods.dynamo.items == {}


class TestTransientFailures:
    def test_embedding_failure_reports_batch_item_failure(self, ingestion):
        mods = ingestion(objects={DOC: (doc(), "ver1")}, sagemaker=FakeSageMaker(fail=True))

        result = mods.worker.handler(make_event("rag-docs", DOC, message_id="abc"), None)

        assert result == {"batchItemFailures": [{"itemIdentifier": "abc"}]}
        assert _status(mods, "ver1") == "FAILED"

    def test_vector_write_failure_reports_batch_item_failure(self, ingestion):
        mods = ingestion(
            objects={DOC: (doc(), "ver1")},
            vectors=FakeS3Vectors(fail_on=vid(DOC, "ver1", 0)),
        )

        result = mods.worker.handler(make_event("rag-docs", DOC, message_id="xyz"), None)

        assert result == {"batchItemFailures": [{"itemIdentifier": "xyz"}]}
        assert _status(mods, "ver1") == "FAILED"


class TestEventParsing:
    def test_s3_test_event_is_ignored(self, ingestion):
        mods = ingestion()
        event = {"Records": [{"messageId": "m1", "body": json.dumps({"Event": "s3:TestEvent"})}]}
        assert mods.worker.handler(event, None) == {"batchItemFailures": []}
        assert mods.vectors.puts == []

    def test_malformed_body_is_acked(self, ingestion):
        mods = ingestion()
        event = {"Records": [{"messageId": "m1", "body": "not-json"}]}
        assert mods.worker.handler(event, None) == {"batchItemFailures": []}

    def test_missing_body_is_acked(self, ingestion):
        mods = ingestion()
        assert mods.worker.handler({"Records": [{"messageId": "m1"}]}, None) == {"batchItemFailures": []}

    def test_missing_bucket_or_key_is_acked(self, ingestion):
        mods = ingestion()
        body = {"Records": [{"s3": {"bucket": {}, "object": {}}}]}
        event = {"Records": [{"messageId": "m1", "body": json.dumps(body)}]}
        assert mods.worker.handler(event, None) == {"batchItemFailures": []}

    def test_mixed_batch_only_reports_failed_message(self, ingestion):
        ok = "incoming/renters/company/policies/ok-policy/v1.md"
        gone = "incoming/renters/company/policies/missing-policy/v1.md"
        mods = ingestion(objects={ok: (doc(), "ver1")})
        # The second record points at a key the S3 stub does not have -> transient.
        good = make_event("rag-docs", ok, message_id="good")["Records"][0]
        bad = make_event("rag-docs", gone, message_id="bad")["Records"][0]

        result = mods.worker.handler({"Records": [good, bad]}, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


class TestManifestHelpers:
    def test_build_sort_key(self, ingestion):
        mods = ingestion()
        assert mods.manifest.build_sort_key("incoming/t/d.md", "ver9") == "incoming/t/d.md#ver9"

    def test_serialize_roundtrip(self, ingestion):
        mods = ingestion()
        mods.manifest.put_status(
            scope_key="renters/leases/L-0001",
            source_sk="incoming/x/v1.md#ver1",
            status="SUCCEEDED",
            source_bucket="rag-docs",
            source_key="incoming/x/v1.md",
            source_version="ver1",
            sha256="deadbeef",
            chunk_count=3,
        )
        record = mods.manifest.get_record("renters/leases/L-0001", "incoming/x/v1.md#ver1")
        assert record["status"] == "SUCCEEDED"
        assert record["chunk_count"] == 3
        assert record["sha256"] == "deadbeef"
        assert record["scope_key"] == "renters/leases/L-0001" and "tenant_id" not in record
        assert record["created_at"]
        assert record["updated_at"]

    def test_the_manifest_partition_is_the_scope_not_a_tenant(self, ingestion):
        mods = ingestion(objects={DOC: (doc(), "ver1")})
        mods.worker.handler(make_event("rag-docs", DOC), None)
        (item,) = mods.dynamo.items.values()
        assert item["scope_key"] == {"S": LEASE_SCOPE} and "tenant_id" not in item
