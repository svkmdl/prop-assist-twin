"""
Local-only /ingest tests (ADR 0002).

/ingest takes a ``source_key`` in the same grammar as the S3 keys of the event-driven
worker and runs the same preparation code, so a document indexed here carries exactly
the metadata the worker would write. Also covers ChatRequest field validation.

The default `client` fixture runs with LOCAL_DEV=true, so the admin gate on
/ingest is open and these tests exercise the validation logic directly.
"""
import pytest
from fastapi import HTTPException

KEY = "incoming/renters/company/policies/repair-policy/v1.md"
LEASE_KEY = "incoming/renters/leases/L-0001/contracts/lease-contract/v2.md"


def doc(body="Reparaturen bitte schriftlich melden.", title="Reparaturrichtlinie",
        frm="2027-01-01", to=None, language="de", extra=""):
    lines = ["---", f"title: {title}", f"language: {language}", f"effective_from: {frm}"]
    if to:
        lines.append(f"effective_to: {to}")
    if extra:
        lines.append(extra)
    return ("\n".join(lines) + f"\n---\n{body}\n").encode("utf-8")


def post(client, source_key=KEY, content=None, filename="upload.md"):
    return client.post(
        "/ingest",
        data={"source_key": source_key},
        files={"file": (filename, doc() if content is None else content, "text/markdown")},
    )


@pytest.fixture
def indexed(server_module, monkeypatch):
    """Capture what /ingest would write to the vector index."""
    calls = {}

    def fake_index(text, vector_id, metadata):
        calls[vector_id] = (text, metadata)
        return vector_id

    monkeypatch.setattr(server_module, "index_text_chunk", fake_index)
    return calls


class TestIngestValidation:
    @pytest.mark.parametrize(
        "source_key,fragment",
        [
            ("incoming/T001/faq.md", "unknown audience/scope"),  # the former layout
            ("incoming/renters/company/policies/repair-policy/latest.md", "filename must be"),
            ("incoming/renters/company/policies/repair-policy/v1.txt", "filename must be"),
            ("incoming/renters/company/faq/repair-policy/v1.md", "unknown doc_type"),
            ("incoming/renters/leases/L-1/contracts/x/v1.md", "invalid lease_id"),
            ("incoming/staff/leases/L-0001/contracts/x/v1.md", "unknown audience/scope"),
            ("../../etc/passwd", "start with incoming/"),
            ("notes.md", "start with incoming/"),
        ],
    )
    def test_rejects_keys_outside_the_grammar_before_reading_or_indexing(
        self, client, indexed, source_key, fragment
    ):
        resp = post(client, source_key)
        assert resp.status_code == 422
        assert fragment in resp.json()["detail"]
        assert indexed == {}

    def test_missing_source_key_rejected(self, client, indexed):
        resp = client.post("/ingest", files={"file": ("n.md", doc(), "text/markdown")})
        assert resp.status_code == 422 and indexed == {}

    def test_overlong_source_key_rejected(self, client, indexed):
        assert post(client, "incoming/" + "x" * 400).status_code == 422
        assert indexed == {}

    def test_the_upload_filename_is_irrelevant(self, client, indexed):
        """Only source_key decides the target; a hostile filename cannot move the document."""
        assert post(client, KEY, filename="../../evil.exe").status_code == 200
        assert {vid.split("/v1/")[0] for vid in indexed} == {"renters/company/policies/repair-policy"}

    def test_rejects_oversize_upload(self, client, server_module, monkeypatch, indexed):
        monkeypatch.setattr(server_module, "MAX_UPLOAD_BYTES", 10)
        assert post(client, content=b"a" * 50).status_code == 413
        assert indexed == {}

    @pytest.mark.parametrize(
        "content,fragment",
        [
            (b"\xff\xfe\x00bad", "not valid UTF-8"),
            (b"", "file is empty"),
            (b"# no front matter\n\nbody", "front-matter"),
            (doc(language="fr"), "language must be one of"),
            (doc(extra="colour: blue"), "unknown key 'colour'"),
            (doc(frm="2027-06-01", to="2027-01-01"), "effective_to must not be before"),
            (doc(body="  "), "no content"),
        ],
    )
    def test_rejects_invalid_content_with_every_reason(self, client, indexed, content, fragment):
        resp = post(client, content=content)
        assert resp.status_code == 400
        assert fragment in resp.json()["detail"]
        assert indexed == {}

    def test_happy_path_indexes_chunks_with_worker_identical_metadata(self, client, indexed):
        long_body = "\n\n".join(f"Absatz {i}. " + "Wort " * 300 for i in range(4))

        resp = post(client, LEASE_KEY, doc(body=long_body, to="2027-12-31"))

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["source_key"] == LEASE_KEY
        assert (body["doc_id"], body["doc_version"]) == ("lease-contract", 2)
        assert body["chunks_indexed"] == len(indexed) >= 2

        assert set(indexed) == {
            f"renters/leases/L-0001/contracts/lease-contract/v2/local/{i}" for i in range(len(indexed))
        }
        text, metadata = indexed["renters/leases/L-0001/contracts/lease-contract/v2/local/0"]
        assert text == metadata["chunk_text"] and "effective_from" not in text
        # Exactly the worker's key set, minus source_bucket (the local route has no bucket).
        assert set(metadata) == {
            "audience", "scope_type", "lease_id", "doc_type", "doc_id", "doc_version",
            "language", "effective_from", "effective_to", "title", "source_key",
            "chunk_index", "chunk_text", "source_version", "embedding_model", "ingested_at",
        }
        assert metadata["audience"] == "renter" and metadata["scope_type"] == "lease"
        assert metadata["lease_id"] == "L-0001" and "property_id" not in metadata
        assert metadata["doc_type"] == "contracts" and metadata["doc_version"] == 2
        assert (metadata["effective_from"], metadata["effective_to"]) == (20270101, 20271231)
        assert metadata["source_version"] == "local" and metadata["source_key"] == LEASE_KEY
        assert "tenant_id" not in metadata and "source_path" not in metadata
        assert sorted(m["chunk_index"] for _, m in indexed.values()) == list(range(len(indexed)))

    def test_re_uploading_a_key_overwrites_its_own_vector_ids(self, client, indexed):
        assert post(client).status_code == 200
        first = set(indexed)
        assert post(client, content=doc(body="Geänderter Text.")).status_code == 200
        assert set(indexed) == first  # same deterministic ids, no pile-up of copies
        assert "Geänderter Text." in next(iter(indexed.values()))[0]

    def test_same_file_name_in_two_scopes_gets_distinct_vector_ids(self, client, indexed):
        for key in (
            "incoming/renters/leases/L-0001/contracts/lease-contract/v1.md",
            "incoming/renters/leases/L-0002/contracts/lease-contract/v1.md",
            "incoming/staff/company/policies/lease-contract/v1.md",
        ):
            assert post(client, key).status_code == 200
        assert len(indexed) == 3

    def test_business_versions_are_separate_documents(self, client, indexed):
        v1 = "incoming/renters/company/policies/repair-policy/v1.md"
        v2 = "incoming/renters/company/policies/repair-policy/v2.md"
        assert post(client, v1, doc(to="2026-12-31", frm="2026-01-01")).status_code == 200
        assert post(client, v2).status_code == 200
        assert {m["doc_version"] for _, m in indexed.values()} == {1, 2}
        assert len(indexed) == 2

    def test_worker_http_exception_propagates(self, client, server_module, monkeypatch):
        def fake_index(text, vector_id, metadata):
            raise HTTPException(status_code=503, detail="vector store unavailable")

        monkeypatch.setattr(server_module, "index_text_chunk", fake_index)
        resp = post(client)
        assert resp.status_code == 503
        assert resp.json()["detail"] == "vector store unavailable"

    def test_worker_generic_exception_returns_500(self, client, server_module, monkeypatch):
        def fake_index(text, vector_id, metadata):
            raise RuntimeError("indexing failed")

        monkeypatch.setattr(server_module, "index_text_chunk", fake_index)
        resp = post(client)
        assert resp.status_code == 500
        assert resp.json()["detail"] == "Ingestion error: indexing failed"


class TestChatRequestValidation:
    def test_empty_message_rejected(self, client):
        resp = client.post("/chat", json={"message": ""})
        assert resp.status_code == 422

    def test_overlong_message_rejected(self, client):
        resp = client.post("/chat", json={"message": "a" * 3001})
        assert resp.status_code == 422

    def test_invalid_session_id_pattern_rejected(self, client):
        resp = client.post("/chat", json={"message": "hi", "session_id": "bad id!"})
        assert resp.status_code == 422
