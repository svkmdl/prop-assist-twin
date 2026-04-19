# backend/tests/test_retrieve_sources.py
def test_retrieve_sources_dedupes_filters_and_shortens(server_module, monkeypatch):
    long_snippet = " ".join(["Spacious balcony near park"] * 20)

    raw_hits = [
        {
            "key": "doc-1",
            "distance": 0.12,
            "metadata": {
                "title": "Listing A",
                "source_path": "kb/listings/a.txt",
                "doc_type": "listing",
                "chunk_index": 0,
                "chunk_text": "  Spacious   balcony in Berlin Mitte  ",
            },
        },
        {
            "key": "doc-2",
            "distance": 0.18,
            "metadata": {
                "title": "Listing A duplicate",
                "source_path": "kb/listings/a-copy.txt",
                "doc_type": "listing",
                "chunk_index": 1,
                "chunk_text": "spacious balcony in berlin mitte",
            },
        },
        {
            "key": "doc-3",
            "distance": 0.99,
            "metadata": {
                "title": "Too Far",
                "source_path": "kb/listings/far.txt",
                "doc_type": "listing",
                "chunk_index": 2,
                "chunk_text": "Far away result",
            },
        },
        {
            "key": "doc-4",
            "distance": 0.20,
            "metadata": {
                "title": "Guide B",
                "source_path": "kb/guides/b.txt",
                "doc_type": "guide",
                "chunk_index": 3,
                "chunk_text": long_snippet,
            },
        },
        {
            "key": "doc-5",
            "distance": 0.10,
            "metadata": {
                "title": "Blank",
                "source_path": "kb/blank.txt",
                "doc_type": "guide",
                "chunk_index": 4,
                "chunk_text": "   ",
            },
        },
    ]

    monkeypatch.setattr(server_module, "is_rag_enabled", lambda: True)
    monkeypatch.setattr(server_module, "search_text_chunks", lambda query, top_k: raw_hits)
    monkeypatch.setattr(server_module, "MAX_RETRIEVAL_DISTANCE", "0.25")

    sources = server_module.retrieve_sources("berlin balcony", top_k=5)

    assert [source.id for source in sources] == ["doc-1", "doc-4"]
    assert sources[0].snippet == "Spacious balcony in Berlin Mitte"
    assert sources[0].source_path == "kb/listings/a.txt"
    assert sources[0].distance == 0.12
    assert sources[1].snippet.endswith("…")
    assert len(sources[1].snippet) <= server_module.SOURCE_SNIPPET_CHARS