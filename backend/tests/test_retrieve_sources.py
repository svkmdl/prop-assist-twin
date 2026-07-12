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
    monkeypatch.setattr(server_module, "search_text_chunks", lambda query, top_k, metadata_filter=None: raw_hits)

    # Set the new funnel constants
    monkeypatch.setattr(server_module, "RAW_FETCH_SIZE", 5)
    monkeypatch.setattr(server_module, "FINAL_TOP_K", 3)
    monkeypatch.setattr(server_module, "MAX_CHUNKS_PER_DOC", 1)  # Force diversity

    # Execute retrieval
    # Query has 'berlin' and 'balcony'
    sources = server_module.retrieve_sources("berlin balcony", fetch_n=5, return_n=3)

    # Behavior-based assertions (no reliance on exact score values, so tuning the
    # BM25 constants or distance weighting does not produce false failures).

    # Diversity: MAX_CHUNKS_PER_DOC=1 means at most one chunk per source_path.
    # doc-1 and doc-2 share "kb/listings/a.txt"; only the higher-scored doc-1
    # (strong lexical match) survives, doc-2 (weak lexical match) is dropped.
    ids = [s.id for s in sources]
    assert "doc-1" in ids
    assert "doc-2" not in ids

    # The result is capped at return_n and every source comes from a distinct doc.
    assert len(sources) == 3
    paths = [s.source_path for s in sources]
    assert len(set(paths)) == len(paths)

    # Distances from the raw hits are preserved on the returned items.
    distance_by_id = {s.id: s.distance for s in sources}
    assert distance_by_id["doc-1"] == 0.12

    # The over-long snippet (doc-4) is normalized and truncated with an ellipsis.
    doc4 = next(s for s in sources if s.id == "doc-4")
    assert doc4.snippet.endswith("…")
    assert len(doc4.snippet) <= server_module.SOURCE_SNIPPET_CHARS


def test_retrieve_sources_applies_tenant_filter(server_module, monkeypatch):
    """retrieve_sources forwards a tenant filter when tenant_id is a real tenant."""
    captured_filter = {}

    def fake_search(query, top_k, metadata_filter=None):
        captured_filter["value"] = metadata_filter
        return []

    monkeypatch.setattr(server_module, "is_rag_enabled", lambda: True)
    monkeypatch.setattr(server_module, "search_text_chunks", fake_search)

    server_module.retrieve_sources("query", tenant_id="T001")
    assert captured_filter["value"] == {"tenant_id": {"$eq": "T001"}}


def test_retrieve_sources_no_filter_for_admin(server_module, monkeypatch):
    """retrieve_sources passes no filter for the admin (default) tenant."""
    captured_filter = {}

    def fake_search(query, top_k, metadata_filter=None):
        captured_filter["value"] = metadata_filter
        return []

    monkeypatch.setattr(server_module, "is_rag_enabled", lambda: True)
    monkeypatch.setattr(server_module, "search_text_chunks", fake_search)

    server_module.retrieve_sources("query", tenant_id="admin")
    assert captured_filter["value"] is None

    # Passing None explicitly should also result in no filter.
    server_module.retrieve_sources("query", tenant_id=None)
    assert captured_filter["value"] is None