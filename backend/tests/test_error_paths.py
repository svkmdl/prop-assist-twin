"""
Error-path and resilience tests.

Covers Bedrock ClientError -> HTTP status mapping, the /chat top-level error
handler, retrieval/rewrite fallbacks, and the Bedrock request contract for the
success paths.
"""
import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, "Converse")


class TestCallBedrock:
    @pytest.mark.parametrize(
        "code,expected_status",
        [
            ("ValidationException", 400),
            ("AccessDeniedException", 403),
            ("ThrottlingException", 500),
        ],
    )
    def test_error_mapping(self, server_module, monkeypatch, code, expected_status):
        def fake_converse(**kwargs):
            raise _client_error(code)

        monkeypatch.setattr(
            server_module.bedrock_client, "converse", fake_converse, raising=False
        )

        with pytest.raises(HTTPException) as exc:
            server_module.call_bedrock([], "hi", sources=[])
        assert exc.value.status_code == expected_status

    def test_success_builds_expected_request(self, server_module, monkeypatch):
        captured = {}

        def fake_converse(**kwargs):
            captured.update(kwargs)
            return {"output": {"message": {"content": [{"text": "the answer"}]}}}

        monkeypatch.setattr(
            server_module.bedrock_client, "converse", fake_converse, raising=False
        )

        source = server_module.SourceItem(
            id="s1", title="T", source_path="kb/a.md", snippet="snippet"
        )
        out = server_module.call_bedrock(
            [{"role": "user", "content": "previous"}], "hello", sources=[source]
        )

        assert out == "the answer"
        # Retrieved knowledge is injected into the system prompt when sources exist.
        assert "RETRIEVED KNOWLEDGE" in captured["system"][0]["text"]
        # History is preserved and the current user message is appended last.
        roles = [m["role"] for m in captured["messages"]]
        assert roles[-1] == "user"
        assert captured["messages"][-1]["content"][0]["text"] == "hello"


class TestChatEndpointErrors:
    def test_generic_failure_returns_500(self, client, server_module, monkeypatch):
        def boom(session_id):
            raise RuntimeError("storage down")

        monkeypatch.setattr(server_module, "load_conversation", boom)
        resp = client.post("/chat", json={"message": "hello"})
        assert resp.status_code == 500


class TestRetrieveSourcesFallback:
    def test_disabled_returns_empty(self, server_module, monkeypatch):
        monkeypatch.setattr(server_module, "is_rag_enabled", lambda: False)
        assert server_module.retrieve_sources("query") == []

    def test_search_failure_returns_empty(self, server_module, monkeypatch):
        monkeypatch.setattr(server_module, "is_rag_enabled", lambda: True)

        def boom(query, top_k):
            raise RuntimeError("vector store down")

        monkeypatch.setattr(server_module, "search_text_chunks", boom)
        assert server_module.retrieve_sources("query") == []


class TestRewriteQuery:
    def test_empty_history_returns_message_unchanged(self, server_module):
        assert server_module.rewrite_query([], "standalone question") == (
            "standalone question"
        )

    def test_falls_back_to_original_on_error(self, server_module, monkeypatch):
        def boom(**kwargs):
            raise RuntimeError("llm down")

        monkeypatch.setattr(
            server_module.bedrock_client, "converse", boom, raising=False
        )
        out = server_module.rewrite_query(
            [{"role": "user", "content": "hi"}], "follow up"
        )
        assert out == "follow up"

    def test_returns_rewritten_query_on_success(self, server_module, monkeypatch):
        def fake_converse(**kwargs):
            return {"output": {"message": {"content": [{"text": "rewritten query"}]}}}

        monkeypatch.setattr(
            server_module.bedrock_client, "converse", fake_converse, raising=False
        )
        out = server_module.rewrite_query(
            [{"role": "user", "content": "hi"}], "it"
        )
        assert out == "rewritten query"
