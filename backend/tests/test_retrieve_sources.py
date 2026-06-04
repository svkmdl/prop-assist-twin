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

    # BM25-based ranking with diversity filter:
    # doc-4 has repeated "balcony" term, giving it highest BM25 score
    # Combined scores: doc-4 (1.0826), doc-1 (1.0800), doc-3 (1.0000), doc-2 (0.9500)
    # With diversity (all different paths): doc-4, doc-1, doc-3
    assert [s.id for s in sources] == ["doc-4", "doc-1", "doc-3"]

    # Verify doc-4 (The winner due to repeated terms in BM25)
    assert sources[0].title == "Guide C"
    assert sources[0].distance == 0.15

    # Verify doc-1 (Second due to higher combined score)
    assert sources[1].title == "Listing A"
    assert sources[1].distance == 0.12

    # Verify truncation logic still works on one of the items
    assert sources[0].snippet.endswith("…")
    assert len(sources[0].snippet) <= server_module.SOURCE_SNIPPET_CHARS

    # Verify document counts in results
    paths = [s.source_path for s in sources]
    assert len(set(paths)) == len(paths)  # All paths must be unique due to MAX_CHUNKS_PER_DOC=1