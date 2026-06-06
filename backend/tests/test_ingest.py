"""
Request validation and /ingest abuse tests.

Covers /ingest filename validation, upload size limit, UTF-8 enforcement, the
happy-path metadata shape, and ChatRequest field validation.

The default `client` fixture runs with LOCAL_DEV=true, so the admin gate on
/ingest is open and these tests exercise the validation logic directly.
"""
import pytest


class TestIngestValidation:
    @pytest.mark.parametrize(
        "filename",
        ["notes.txt", "notes.md.exe", "weird name.md", "archive.tar.gz"],
    )
    def test_rejects_unsafe_filename(self, client, filename):
        resp = client.post(
            "/ingest",
            files={"file": (filename, b"# hi", "text/markdown")},
        )
        assert resp.status_code == 400

    def test_rejects_oversize_upload(self, client, server_module, monkeypatch):
        monkeypatch.setattr(server_module, "MAX_UPLOAD_BYTES", 10)
        resp = client.post(
            "/ingest",
            files={"file": ("big.md", b"a" * 50, "text/markdown")},
        )
        assert resp.status_code == 413

    def test_rejects_non_utf8(self, client):
        resp = client.post(
            "/ingest",
            files={"file": ("bad.md", b"\xff\xfe\x00bad", "text/markdown")},
        )
        assert resp.status_code == 400

    def test_happy_path_indexes_chunks_with_metadata(
        self, client, server_module, monkeypatch
    ):
        indexed = []

        def fake_index(text, vector_id, metadata):
            indexed.append((vector_id, metadata))
            return vector_id

        monkeypatch.setattr(server_module, "index_text_chunk", fake_index)

        resp = client.post(
            "/ingest",
            files={
                "file": ("notes.md", b"# Title\n\nSome content here.", "text/markdown")
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["filename"] == "notes.md"
        assert body["chunks_indexed"] == len(indexed) >= 1

        _, metadata = indexed[0]
        assert metadata["title"] == "notes"
        assert metadata["doc_type"] == ".md"
        assert metadata["source_path"] == "api_upload/notes.md"
        assert metadata["chunk_index"] == 0


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
