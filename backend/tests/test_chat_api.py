FIXED_PROMPTS = [
    {
        "session_id": "session-a",
        "message": "Hallo, wer bist du?",
        "expected_history": 0,
        "expected_sources": 0,
    },
    {
        "session_id": "session-a",
        "message": "Ich suche eine 3-Zimmer-Wohnung in Berlin mit Balkon.",
        "expected_history": 2,
        "expected_sources": 2,
    },
    {
        "session_id": "session-b",
        "message": "What can you help me with?",
        "expected_history": 0,
        "expected_sources": 0,
    },
    {
        "session_id": "session-b",
        "message": "Do you also support commercial real estate?",
        "expected_history": 2,
        "expected_sources": 1,
    },
]


def test_chat_fixed_prompts(client, server_module, monkeypatch):
    def fake_retrieve_sources(message: str):
        if message == "Ich suche eine 3-Zimmer-Wohnung in Berlin mit Balkon.":
            return [
                server_module.SourceItem(
                    id="listing-1",
                    title="Berlin Listing",
                    source_path="kb/listings/berlin.txt",
                    snippet="3 Zimmer, Balkon, Berlin",
                    doc_type="listing",
                    chunk_index=0,
                    distance=0.11,
                ),
                server_module.SourceItem(
                    id="listing-2",
                    title="Neighborhood Guide",
                    source_path="kb/guides/berlin.txt",
                    snippet="Mitte and Prenzlauer Berg",
                    doc_type="guide",
                    chunk_index=1,
                    distance=0.13,
                ),
            ]
        if message == "Do you also support commercial real estate?":
            return [
                server_module.SourceItem(
                    id="commercial-1",
                    title="Commercial FAQ",
                    source_path="kb/commercial/faq.txt",
                    snippet="Support for office and retail requests",
                    doc_type="faq",
                    chunk_index=0,
                    distance=0.09,
                )
            ]
        return []

    def fake_call_bedrock(conversation, user_message, sources=None):
        return f"stub::{user_message}::history={len(conversation)}::sources={len(sources or [])}"

    monkeypatch.setattr(server_module, "retrieve_sources", fake_retrieve_sources)
    monkeypatch.setattr(server_module, "call_bedrock", fake_call_bedrock)

    for case in FIXED_PROMPTS:
        response = client.post(
            "/chat",
            json={"message": case["message"], "session_id": case["session_id"]},
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["session_id"] == case["session_id"]
        assert body["response"] == (
            f"stub::{case['message']}::history={case['expected_history']}::sources={case['expected_sources']}"
        )
        assert body["retrieval_used"] is (case["expected_sources"] > 0)
        assert len(body["sources"]) == case["expected_sources"]

    conversation = client.get("/conversation/session-a")
    assert conversation.status_code == 200
    messages = conversation.json()["messages"]
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert messages[0]["content"] == "Hallo, wer bist du?"
    assert messages[2]["content"] == "Ich suche eine 3-Zimmer-Wohnung in Berlin mit Balkon."