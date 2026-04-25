def test_retrieve_sources_reranks_and_diversifies(server_module, monkeypatch):
    # Setup a long snippet for truncation testing
    long_snippet = " ".join(["Spacious balcony near park"] * 20)

    # 2. Mock raw hits from the Vector DB
    raw_hits = [
        {
            "key": "doc-1",
            "distance": 0.12,  # Good distance
            "metadata": {
                "title": "Listing A",
                "source_path": "kb/listings/a.txt",
                "chunk_text": "Spacious balcony in Berlin Mitte",  # High Lexical match
            },
        },
        {
            "key": "doc-2",
            "distance": 0.05,  # Better distance than doc-1
            "metadata": {
                "title": "Listing A - Part 2",
                "source_path": "kb/listings/a.txt",  # SAME PATH as doc-1
                "chunk_text": "More info about the same apartment",  # Low Lexical match
            },
        },
        {
            "key": "doc-3",
            "distance": 0.20,
            "metadata": {
                "title": "Listing B",
                "source_path": "kb/listings/b.txt",  # Different Path
                "chunk_text": "Another balcony in Berlin",  # High Lexical match
            },
        },
        {
            "key": "doc-4",
            "distance": 0.15,
            "metadata": {
                "title": "Guide C",
                "source_path": "kb/guides/c.txt",
                "chunk_text": long_snippet,  # Will be truncated
            },
        },
    ]

    # Monkeypatch settings for the test
    monkeypatch.setattr(server_module, "is_rag_enabled", lambda: True)
    monkeypatch.setattr(server_module, "search_text_chunks", lambda query, top_k: raw_hits)

    # Set the new funnel constants
    monkeypatch.setattr(server_module, "RAW_FETCH_SIZE", 5)
    monkeypatch.setattr(server_module, "FINAL_TOP_K", 3)
    monkeypatch.setattr(server_module, "MAX_CHUNKS_PER_DOC", 1)  # Force diversity

    # Execute retrieval
    # Query has 'berlin' and 'balcony'
    sources = server_module.retrieve_sources("berlin balcony", fetch_n=5, return_n=3)

    # Assertions

    # Check Diversity: Even though doc-2 had a better 'distance' than doc-3,
    # doc-2 should be dropped because doc-1 (same path) was already picked.
    # Expected order based on combined score: doc-1, doc-3, doc-4
    assert [s.id for s in sources] == ["doc-1", "doc-3", "doc-4"]

    # Verify doc-1 (The winner)
    assert sources[0].title == "Listing A"
    assert sources[0].distance == 0.12

    # Verify truncation logic still works on the last item
    assert sources[2].snippet.endswith("…")
    assert len(sources[2].snippet) <= server_module.SOURCE_SNIPPET_CHARS

    # Verify document counts in results
    paths = [s.source_path for s in sources]
    assert len(set(paths)) == len(paths)  # All paths must be unique due to MAX_CHUNKS_PER_DOC=1