"""
Chat API behavior, plus smoke tests for unauthenticated endpoints and the S3
storage branch of the conversation persistence layer.
"""
import io

import pytest
from botocore.exceptions import ClientError

# Sources returned by the stubbed retriever, keyed by user message.
STUBBED_SOURCES = {
    "Ich suche eine 3-Zimmer-Wohnung in Berlin mit Balkon.": [
        {
            "id": "listing-1",
            "title": "Berlin Listing",
            "source_path": "kb/listings/berlin.txt",
            "snippet": "3 Zimmer, Balkon, Berlin",
            "doc_type": "listing",
            "chunk_index": 0,
            "distance": 0.11,
        },
        {
            "id": "listing-2",
            "title": "Neighborhood Guide",
            "source_path": "kb/guides/berlin.txt",
            "snippet": "Mitte and Prenzlauer Berg",
            "doc_type": "guide",
            "chunk_index": 1,
            "distance": 0.13,
        },
    ],
    "Do you also support commercial real estate?": [
        {
            "id": "commercial-1",
            "title": "Commercial FAQ",
            "source_path": "kb/commercial/faq.txt",
            "snippet": "Support for office and retail requests",
            "doc_type": "faq",
            "chunk_index": 0,
            "distance": 0.09,
        }
    ],
}

# Ordered conversation scenarios. Each scenario runs its turns sequentially in a
# single session so the accumulated history is exercised end to end.
CONVERSATION_SCENARIOS = [
    (
        "session-a",
        [
            {
                "message": "Hallo, wer bist du?",
                "expected_history": 0,
                "expected_sources": 0,
            },
            {
                "message": "Ich suche eine 3-Zimmer-Wohnung in Berlin mit Balkon.",
                "expected_history": 2,
                "expected_sources": 2,
            },
        ],
    ),
    (
        "session-b",
        [
            {
                "message": "What can you help me with?",
                "expected_history": 0,
                "expected_sources": 0,
            },
            {
                "message": "Do you also support commercial real estate?",
                "expected_history": 2,
                "expected_sources": 1,
            },
        ],
    ),
]


@pytest.mark.parametrize(
    "session_id,turns", CONVERSATION_SCENARIOS, ids=["session-a", "session-b"]
)
def test_chat_conversation_scenarios(
    client, server_module, monkeypatch, session_id, turns
):
    def fake_retrieve_sources(message: str):
        return [
            server_module.SourceItem(**data)
            for data in STUBBED_SOURCES.get(message, [])
        ]

    def fake_call_bedrock(conversation, user_message, sources=None):
        return (
            f"stub::{user_message}::history={len(conversation)}"
            f"::sources={len(sources or [])}"
        )

    monkeypatch.setattr(server_module, "retrieve_sources", fake_retrieve_sources)
    monkeypatch.setattr(server_module, "call_bedrock", fake_call_bedrock)

    for turn in turns:
        response = client.post(
            "/chat",
            json={"message": turn["message"], "session_id": session_id},
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["session_id"] == session_id
        assert body["response"] == (
            f"stub::{turn['message']}::history={turn['expected_history']}"
            f"::sources={turn['expected_sources']}"
        )
        assert body["retrieval_used"] is (turn["expected_sources"] > 0)
        assert len(body["sources"]) == turn["expected_sources"]

    # Persisted history alternates user/assistant and preserves message content.
    conversation = client.get(f"/conversation/{session_id}")
    assert conversation.status_code == 200
    messages = conversation.json()["messages"]

    expected_roles = ["user", "assistant"] * len(turns)
    assert [message["role"] for message in messages] == expected_roles
    for index, turn in enumerate(turns):
        assert messages[index * 2]["content"] == turn["message"]


class TestSmokeEndpoints:
    """Unauthenticated informational endpoints."""

    def test_root(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["memory_enabled"] is True
        assert "ai_model" in body

    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        # Default test env has no vector store configured.
        assert body["rag_enabled"] is False


class TestS3StorageBranch:
    """The USE_S3 code path of load/save_conversation."""

    def test_load_returns_empty_on_no_such_key(self, make_server):
        sm = make_server(USE_S3="true", S3_BUCKET="bucket")

        class FakeS3:
            def get_object(self, Bucket, Key):
                raise ClientError(
                    {"Error": {"Code": "NoSuchKey", "Message": "missing"}},
                    "GetObject",
                )

        sm.s3_client = FakeS3()
        assert sm.load_conversation("sess") == []

    def test_save_then_load_roundtrip(self, make_server):
        sm = make_server(USE_S3="true", S3_BUCKET="bucket")
        store = {}

        class FakeS3:
            def put_object(self, Bucket, Key, Body, ContentType):
                store[Key] = Body

            def get_object(self, Bucket, Key):
                return {"Body": io.BytesIO(store[Key].encode("utf-8"))}

        sm.s3_client = FakeS3()
        messages = [{"role": "user", "content": "hi", "timestamp": "t"}]
        sm.save_conversation("sess", messages)
        assert sm.load_conversation("sess") == messages