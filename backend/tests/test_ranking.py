"""
Unit tests for retrieval ranking helpers.

Covers `get_lexical_score`, `is_rag_enabled`, the distance-threshold filter
inside `retrieve_sources`, and the embedding/index/search service helpers.
"""
import json

import pytest
from fastapi import HTTPException


class FakeBody:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload



class TestLexicalScore:
    def test_empty_inputs_score_zero(self, server_module):
        assert server_module.get_lexical_score("", "some document") == 0.0
        assert server_module.get_lexical_score("query", "") == 0.0

    def test_match_beats_no_match(self, server_module):
        matched = server_module.get_lexical_score(
            "balcony berlin", "spacious balcony in berlin"
        )
        unmatched = server_module.get_lexical_score(
            "balcony berlin", "totally unrelated content here"
        )
        assert matched > unmatched

    def test_case_insensitive(self, server_module):
        upper = server_module.get_lexical_score("BALCONY", "balcony")
        lower = server_module.get_lexical_score("balcony", "balcony")
        assert upper == lower

    def test_score_bounded_between_zero_and_one(self, server_module):
        score = server_module.get_lexical_score("balcony", "balcony " * 100)
        assert 0.0 <= score <= 1.0


class TestIsRagEnabled:
    def test_enabled_when_fully_configured(self, make_server):
        sm = make_server(
            RAG_ENABLED="true",
            SAGEMAKER_ENDPOINT="endpoint",
            VECTOR_BUCKET="bucket",
            VECTOR_INDEX="index",
        )
        assert sm.is_rag_enabled() is True

    def test_disabled_when_config_missing(self, make_server):
        sm = make_server(
            RAG_ENABLED="true",
            SAGEMAKER_ENDPOINT="",
            VECTOR_BUCKET="bucket",
            VECTOR_INDEX="index",
        )
        assert sm.is_rag_enabled() is False

    def test_disabled_when_flag_off(self, make_server):
        sm = make_server(
            RAG_ENABLED="false",
            SAGEMAKER_ENDPOINT="endpoint",
            VECTOR_BUCKET="bucket",
            VECTOR_INDEX="index",
        )
        assert sm.is_rag_enabled() is False


class TestRetrieveSourcesDistanceFilter:
    def test_drops_hits_beyond_threshold(self, server_module, monkeypatch):
        monkeypatch.setattr(server_module, "is_rag_enabled", lambda: True)
        monkeypatch.setattr(server_module, "MAX_RETRIEVAL_DISTANCE_VALUE", 0.3)

        hits = [
            {
                "key": "near",
                "distance": 0.1,
                "metadata": {
                    "title": "A",
                    "source_path": "kb/a.md",
                    "chunk_text": "balcony in berlin",
                },
            },
            {
                "key": "far",
                "distance": 0.9,
                "metadata": {
                    "title": "B",
                    "source_path": "kb/b.md",
                    "chunk_text": "balcony in berlin",
                },
            },
        ]
        monkeypatch.setattr(
            server_module, "search_text_chunks", lambda query, top_k, metadata_filter=None: hits
        )

        sources = server_module.retrieve_sources("balcony")
        ids = [s.id for s in sources]
        assert "near" in ids
        assert "far" not in ids


class TestServiceHelpersNotConfigured:
    """Guard branches raise 500 when the backing services are not configured."""

    def test_get_embedding_requires_endpoint(self, server_module):
        with pytest.raises(HTTPException) as exc:
            server_module.get_embedding("hi")
        assert exc.value.status_code == 500

    def test_index_text_chunk_requires_vector_store(self, server_module):
        with pytest.raises(HTTPException) as exc:
            server_module.index_text_chunk("text", "id", {})
        assert exc.value.status_code == 500

    def test_search_text_chunks_requires_vector_store(self, server_module):
        with pytest.raises(HTTPException) as exc:
            server_module.search_text_chunks("query")
        assert exc.value.status_code == 500


class TestServiceHelpersConfigured:
    """Happy paths for embedding, indexing, and search when fully configured."""

    def _configured(self, make_server):
        return make_server(
            SAGEMAKER_ENDPOINT="endpoint",
            VECTOR_BUCKET="bucket",
            VECTOR_INDEX="index",
        )

    @pytest.mark.parametrize(
        "payload,expected",
        [
            ([[[0.1, 0.2]]], [0.1, 0.2]),
            ([[0.3, 0.4]], [0.3, 0.4]),
        ],
    )
    def test_get_embedding_parses_nested_shapes(
        self, make_server, payload, expected
    ):
        sm = self._configured(make_server)

        class FakeSageMaker:
            def invoke_endpoint(self, **kwargs):
                return {"Body": FakeBody(payload)}

        sm.sagemaker_client = FakeSageMaker()
        assert sm.get_embedding("hi") == expected

    def test_index_text_chunk_puts_vector(self, make_server, monkeypatch):
        sm = self._configured(make_server)
        monkeypatch.setattr(sm, "get_embedding", lambda text: [0.1, 0.2])

        captured = {}

        class FakeS3Vectors:
            def put_vectors(self, **kwargs):
                captured.update(kwargs)

        sm.s3vectors_client = FakeS3Vectors()
        result = sm.index_text_chunk("body", "vec-1", {"title": "T"})

        assert result == "vec-1"
        assert captured["vectorBucketName"] == "bucket"
        assert captured["vectors"][0]["key"] == "vec-1"
        assert captured["vectors"][0]["metadata"]["chunk_text"] == "body"

    def test_search_text_chunks_returns_vectors(self, make_server, monkeypatch):
        sm = self._configured(make_server)
        monkeypatch.setattr(sm, "get_embedding", lambda text: [0.1, 0.2])

        class FakeS3Vectors:
            def query_vectors(self, **kwargs):
                return {"vectors": [{"key": "doc-1", "distance": 0.1}]}

        sm.s3vectors_client = FakeS3Vectors()
        results = sm.search_text_chunks("query", top_k=3)

        assert results == [{"key": "doc-1", "distance": 0.1}]

