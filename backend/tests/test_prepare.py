"""The shared preparation step used by the worker, /ingest and the corpus validator.

Pure unit tests: no AWS, no filesystem.
"""
from __future__ import annotations

from datetime import date

import pytest

from common import prepare
from common.doc_keys import DocumentFormatError, InvalidDocumentKeyError, InvalidFrontMatterError
from common.prepare import (
    InvalidDocumentContentError,
    PreparedDocument,
    build_chunk_metadata,
    prepare_document,
)

KEY = "incoming/renters/leases/L-0001/contracts/lease-contract/v1.md"


def doc(body="Inhalt des Vertrags.", **fm) -> bytes:
    fields = {"title": "Mietvertrag", "language": "de", "effective_from": "2026-01-01", **fm}
    head = "\n".join(f"{k}: {v}" for k, v in fields.items())
    return f"---\n{head}\n---\n{body}\n".encode("utf-8")


class TestPrepareDocument:
    def test_happy_path(self):
        prepared = prepare_document(KEY, doc(effective_to="2026-12-31"))
        assert isinstance(prepared, PreparedDocument)
        assert prepared.key.lease_id == "L-0001" and prepared.key.doc_version == 1
        assert prepared.front_matter.title == "Mietvertrag"
        assert prepared.front_matter.effective_to == date(2026, 12, 31)
        assert prepared.body.strip() == "Inhalt des Vertrags."
        assert prepared.chunks == ["Inhalt des Vertrags."]
        assert prepared.doc_metadata["effective_to"] == 20261231
        assert "effective_from" not in prepared.chunks[0]  # the front matter is not indexed

    def test_chunking_follows_the_given_sizes(self):
        body = "\n\n".join(f"Absatz {i}. " + "Wort " * 100 for i in range(6))
        small = prepare_document(KEY, doc(body=body), chunk_size=300, chunk_overlap=20)
        large = prepare_document(KEY, doc(body=body), chunk_size=5000, chunk_overlap=0)
        assert len(small.chunks) > len(large.chunks) == 1

    def test_the_defaults_mirror_the_deployed_configuration(self):
        assert (prepare.DEFAULT_MAX_BYTES, prepare.DEFAULT_CHUNK_SIZE, prepare.DEFAULT_CHUNK_OVERLAP) == (
            1_048_576, 1500, 200,
        )

    def test_the_size_limit_is_inclusive(self):
        data = doc()
        assert prepare_document(KEY, data, max_bytes=len(data)).chunks
        with pytest.raises(InvalidDocumentContentError, match=f"over the {len(data) - 1} byte limit"):
            prepare_document(KEY, data, max_bytes=len(data) - 1)

    @pytest.mark.parametrize(
        "payload,fragment",
        [(b"", "file is empty"), (b"\xff\xfe\x00bad", "not valid UTF-8"), (b"\x80", "not valid UTF-8")],
    )
    def test_content_errors(self, payload, fragment):
        with pytest.raises(InvalidDocumentContentError, match=fragment):
            prepare_document(KEY, payload)

    def test_front_matter_errors_keep_their_own_class_and_list_every_problem(self):
        with pytest.raises(InvalidFrontMatterError) as err:
            prepare_document(KEY, b"---\ntitel: x\nlanguage: fr\n---\n")
        assert len(err.value.problems) >= 3

    def test_key_errors_win_and_nothing_is_inspected_for_a_misplaced_file(self):
        """A bad key is reported even if the bytes are also bad (the file is never examined)."""
        with pytest.raises(InvalidDocumentKeyError):
            prepare_document("incoming/T001/faq.md", b"\xff")

    def test_all_errors_share_one_catchable_base(self):
        for key, payload in ((KEY, b""), ("nope", doc()), (KEY, b"# plain")):
            with pytest.raises(DocumentFormatError):
                prepare_document(key, payload)

    def test_a_document_that_yields_no_chunks_is_rejected(self, monkeypatch):
        monkeypatch.setattr(prepare.chunking, "chunk_text", lambda *a, **k: iter(()))
        with pytest.raises(InvalidDocumentContentError, match="no content chunks"):
            prepare_document(KEY, doc())

    def test_prepared_documents_are_immutable(self):
        prepared = prepare_document(KEY, doc())
        with pytest.raises(Exception):
            prepared.body = "x"  # type: ignore[misc]


class TestBuildChunkMetadata:
    def prepared(self):
        return prepare_document(KEY, doc())

    def test_adds_the_per_chunk_fields_to_the_document_metadata(self):
        prepared = self.prepared()
        md = build_chunk_metadata(
            prepared, chunk_index=3, chunk_text="text", source_version="ver1",
            embedding_model="m", ingested_at="2027-01-01T00:00:00+00:00", source_bucket="rag-docs",
        )
        assert md == {
            **prepared.doc_metadata,
            "chunk_index": 3, "chunk_text": "text", "source_version": "ver1",
            "embedding_model": "m", "ingested_at": "2027-01-01T00:00:00+00:00",
            "source_bucket": "rag-docs",
        }

    def test_the_bucket_is_omitted_for_the_local_route(self):
        md = build_chunk_metadata(
            self.prepared(), chunk_index=0, chunk_text="t", source_version="local",
            embedding_model="m", ingested_at="x",
        )
        assert "source_bucket" not in md

    def test_does_not_mutate_the_shared_document_metadata(self):
        prepared = self.prepared()
        before = dict(prepared.doc_metadata)
        build_chunk_metadata(
            prepared, chunk_index=1, chunk_text="t", source_version="v", embedding_model="m", ingested_at="x"
        )
        assert prepared.doc_metadata == before and "chunk_index" not in prepared.doc_metadata

    def test_no_legacy_fields(self):
        md = build_chunk_metadata(
            self.prepared(), chunk_index=0, chunk_text="t", source_version="v", embedding_model="m", ingested_at="x"
        )
        assert not {"tenant_id", "source_path"} & set(md)
