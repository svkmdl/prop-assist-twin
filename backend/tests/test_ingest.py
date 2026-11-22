"""
Request validation and /ingest abuse tests.

Covers /ingest filename validation, upload size limit, UTF-8 enforcement, the
happy-path metadata shape, and ChatRequest field validation.

The default `client` fixture runs with LOCAL_DEV=true, so the admin gate on
/ingest is open and these tests exercise the validation logic directly.
"""
import pytest
from fastapi import HTTPException


class TestIngestValidation:
    @pytest.mark.parametrize(
        "filename",
        ["notes.txt", "notes.md.exe", "weird name.md", "archive.tar.gz"],
    )
    def test_rejects_unsafe_filename(self, client, filename):
        resp = client.post(
            "/ingest",
            data={"tenant_id": "T001"},
            files={"file": (filename, b"# hi", "text/markdown")},
        )
        assert resp.status_code == 400

    def test_rejects_oversize_upload(self, client, server_module, monkeypatch):
        monkeypatch.setattr(server_module, "MAX_UPLOAD_BYTES", 10)
        resp = client.post(
            "/ingest",
            data={"tenant_id": "T001"},
            files={"file": ("big.md", b"a" * 50, "text/markdown")},
        )
        assert resp.status_code == 413

    def test_rejects_non_utf8(self, client):
        resp = client.post(
            "/ingest",
            data={"tenant_id": "T001"},
            files={"file": ("bad.md", b"\xff\xfe\x00bad", "text/markdown")},
        )
        assert resp.status_code == 400

    def test_happy_path_indexes_chunks_with_metadata(
        self, client, server_module, monkeypatch
    ):
        indexed = []

        monkeypatch.setattr(
            server_module,
            "chunk_text",
            lambda content: iter(["first chunk", "second chunk", "third chunk"]),
        )

        def fake_index(text, vector_id, metadata):
            indexed.append((text, vector_id, metadata))
            return vector_id

        monkeypatch.setattr(server_module, "index_text_chunk", fake_index)

        resp = client.post(
            "/ingest",
            data={"tenant_id": "T001"},
            files={
                "file": ("notes.md", b"# Title\n\nSome content here.", "text/markdown")
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["filename"] == "notes.md"
        assert body["chunks_indexed"] == len(indexed) >= 1

        indexed_by_id = {vector_id: (text, metadata) for text, vector_id, metadata in indexed}
        assert set(indexed_by_id) == {"T001/notes.md_0", "T001/notes.md_1", "T001/notes.md_2"}

        for chunk_index in range(3):
            text, metadata = indexed_by_id[f"T001/notes.md_{chunk_index}"]
            assert text == ["first chunk", "second chunk", "third chunk"][chunk_index]
            assert metadata["title"] == "notes"
            assert metadata["doc_type"] == ".md"
            assert metadata["source_path"] == "api_upload/notes.md"
            assert metadata["tenant_id"] == "T001"
            assert metadata["chunk_index"] == chunk_index

    def test_worker_http_exception_propagates(self, client, server_module, monkeypatch):
        monkeypatch.setattr(
            server_module,
            "chunk_text",
            lambda content: iter(["first chunk", "second chunk"]),
        )

        def fake_index(text, vector_id, metadata):
            if vector_id == "T001/notes.md_1":
                raise HTTPException(status_code=503, detail="vector store unavailable")
            return vector_id

        monkeypatch.setattr(server_module, "index_text_chunk", fake_index)

        resp = client.post(
            "/ingest",
            data={"tenant_id": "T001"},
            files={"file": ("notes.md", b"# Title", "text/markdown")},
        )

        assert resp.status_code == 503
        assert resp.json()["detail"] == "vector store unavailable"

    def test_worker_generic_exception_returns_500(self, client, server_module, monkeypatch):
        monkeypatch.setattr(
            server_module,
            "chunk_text",
            lambda content: iter(["first chunk", "second chunk"]),
        )

        def fake_index(text, vector_id, metadata):
            if vector_id == "T001/notes.md_1":
                raise RuntimeError("indexing failed")
            return vector_id

        monkeypatch.setattr(server_module, "index_text_chunk", fake_index)

        resp = client.post(
            "/ingest",
            data={"tenant_id": "T001"},
            files={"file": ("notes.md", b"# Title", "text/markdown")},
        )

        assert resp.status_code == 500
        assert resp.json()["detail"] == "Ingestion error: indexing failed"


class TestIngestTenant:
    def test_missing_tenant_id_rejected(self, client):
        resp = client.post(
            "/ingest",
            files={"file": ("notes.md", b"# hi", "text/markdown")},
        )
        assert resp.status_code == 422

    def test_malformed_tenant_id_rejected(self, client):
        resp = client.post(
            "/ingest",
            data={"tenant_id": "../T001"},
            files={"file": ("notes.md", b"# hi", "text/markdown")},
        )
        assert resp.status_code == 422

    def test_unknown_tenant_id_rejected_before_indexing(
        self, client, server_module, monkeypatch
    ):
        indexed = []
        monkeypatch.setattr(
            server_module,
            "index_text_chunk",
            lambda text, vector_id, metadata: indexed.append(vector_id),
        )
        resp = client.post(
            "/ingest",
            data={"tenant_id": "T999"},
            files={"file": ("notes.md", b"# hi", "text/markdown")},
        )
        # Same status as /chat's ChatRequest for an unknown tenant.
        assert resp.status_code == 422
        assert "tenant_id must be one of" in resp.text
        assert indexed == []

    def test_same_filename_for_two_tenants_gets_distinct_vector_ids(
        self, client, server_module, monkeypatch
    ):
        monkeypatch.setattr(server_module, "chunk_text", lambda content: iter(["only"]))
        indexed = {}

        def fake_index(text, vector_id, metadata):
            indexed[vector_id] = metadata
            return vector_id

        monkeypatch.setattr(server_module, "index_text_chunk", fake_index)

        for tenant in ("T001", "T002"):
            resp = client.post(
                "/ingest",
                data={"tenant_id": tenant},
                files={"file": ("faq.md", b"# FAQ", "text/markdown")},
            )
            assert resp.status_code == 200

        assert {vid: md["tenant_id"] for vid, md in indexed.items()} == {
            "T001/faq.md_0": "T001",
            "T002/faq.md_0": "T002",
        }


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
