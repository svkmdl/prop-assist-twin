import logging

import pytest

from common.access import build_vector_filter
from filter_eval import matches
from scope_helpers import RESOLVER, STAFF, T001, T002, md

# Visible to every principal on the test date: a company-wide renter document window.
AUTH = md()


def authorized(hits):
    """Give each raw hit the schema fields the per-hit authorization check requires."""
    for hit in hits:
        hit["metadata"] = {**AUTH, **hit["metadata"]}
    return hits


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

    authorized(raw_hits)

    # Monkeypatch settings for the test
    monkeypatch.setattr(server_module, "is_rag_enabled", lambda: True)
    monkeypatch.setattr(server_module, "search_text_chunks", lambda query, top_k, metadata_filter=None: raw_hits)

    # Set the new funnel constants
    monkeypatch.setattr(server_module, "RAW_FETCH_SIZE", 5)
    monkeypatch.setattr(server_module, "FINAL_TOP_K", 3)
    monkeypatch.setattr(server_module, "MAX_CHUNKS_PER_DOC", 1)  # Force diversity

    # Execute retrieval
    # Query has 'berlin' and 'balcony'
    sources = server_module.retrieve_sources("berlin balcony", fetch_n=5, return_n=3, scope=STAFF)

    # Behavior-based assertions (no reliance on exact score values, so tuning the
    # BM25 constants or distance weighting does not produce false failures).

    # Diversity: MAX_CHUNKS_PER_DOC=1 means at most one chunk per source_path.
    # doc-1 and doc-2 share "kb/listings/a.txt"; only the higher-scored doc-1
    # (strong lexical match) survives, doc-2 (weak lexical match) is dropped.
    ids = [s.vector_key for s in sources]
    assert "doc-1" in ids
    assert "doc-2" not in ids

    # The result is capped at return_n and every source comes from a distinct doc.
    assert len(sources) == 3
    paths = [s.source_path for s in sources]
    assert len(set(paths)) == len(paths)

    # Distances from the raw hits are preserved on the returned items.
    distance_by_id = {s.vector_key: s.distance for s in sources}
    assert distance_by_id["doc-1"] == 0.12

    # The over-long snippet (doc-4) is normalized and truncated with an ellipsis.
    doc4 = next(s for s in sources if s.vector_key == "doc-4")
    assert doc4.snippet.endswith("…")
    assert len(doc4.snippet) <= server_module.SOURCE_SNIPPET_CHARS


def test_retrieve_sources_sends_the_scope_filter(server_module, monkeypatch):
    """The S3 Vectors filter is exactly the one built from the caller's scope."""
    captured = []
    monkeypatch.setattr(server_module, "is_rag_enabled", lambda: True)
    monkeypatch.setattr(
        server_module,
        "search_text_chunks",
        lambda query, top_k, metadata_filter=None: captured.append(metadata_filter) or [],
    )

    server_module.retrieve_sources("query", scope=T001)
    server_module.retrieve_sources("query", scope=STAFF)

    assert captured == [build_vector_filter(T001), build_vector_filter(STAFF)]
    renter_filter, staff_filter = captured
    assert {"audience": {"$eq": "renter"}} in renter_filter["$and"]  # never staff documents
    assert "tenant_id" not in str(captured)  # the partition model is gone


def test_a_scope_is_required_there_is_no_unfiltered_retrieval(server_module, monkeypatch):
    called = []
    monkeypatch.setattr(server_module, "is_rag_enabled", lambda: True)
    monkeypatch.setattr(
        server_module, "search_text_chunks", lambda *a, **k: called.append(1) or []
    )
    with pytest.raises(TypeError):
        server_module.retrieve_sources("query")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        server_module.retrieve_sources("query", None, None, None)  # type: ignore[misc]
    assert called == []


def test_hits_outside_the_scope_are_dropped_even_if_the_vector_store_returns_them(
    server_module, monkeypatch, caplog
):
    """Defence in depth: a filter that is ignored or mis-built cannot leak a document."""
    hits = [
        {"key": "company", "distance": 0.2, "metadata": md("incoming/renters/company/policies/repair-policy/v1.md", chunk_text="repair rules")},
        {"key": "own-lease", "distance": 0.2, "metadata": md("incoming/renters/leases/L-0001/contracts/lease-contract/v1.md", chunk_text="repair own lease")},
        {"key": "other-lease", "distance": 0.1, "metadata": md("incoming/renters/leases/L-0002/contracts/lease-contract/v1.md", chunk_text="SECRET-OTHER-LEASE repair")},
        {"key": "staff-sop", "distance": 0.05, "metadata": md("incoming/staff/company/sops/water-damage/v1.md", chunk_text="SECRET-STAFF repair")},
        {"key": "other-property", "distance": 0.05, "metadata": md("incoming/renters/properties/P-002/policies/house-rules/v1.md", chunk_text="SECRET-OTHER-PROPERTY repair")},
        {"key": "legacy", "distance": 0.01, "metadata": {"tenant_id": "T001", "title": "FAQ", "chunk_text": "SECRET-LEGACY repair"}},
        {"key": "expired", "distance": 0.01, "metadata": md(frm="2020-01-01", to="2020-12-31", chunk_text="SECRET-EXPIRED repair")},
        {"key": "no-metadata", "distance": 0.01},
    ]
    monkeypatch.setattr(server_module, "is_rag_enabled", lambda: True)
    monkeypatch.setattr(server_module, "MAX_CHUNKS_PER_DOC", 5)
    monkeypatch.setattr(
        server_module, "search_text_chunks", lambda query, top_k, metadata_filter=None: hits
    )

    with caplog.at_level(logging.WARNING, logger="server"):
        sources = server_module.retrieve_sources("repair", fetch_n=20, return_n=10, scope=T001)

    assert sorted(s.vector_key for s in sources) == ["company", "own-lease"]
    assert "SECRET" not in str([s.model_dump() for s in sources]) + server_module.build_retrieval_block(sources)
    # The drop is logged as a security signal, without any document content.
    assert "Dropped 6 retrieved vector(s) outside the caller's scope" in caplog.text
    assert "SECRET" not in caplog.text and "role=renter" in caplog.text


def test_staff_scope_sees_every_audience_but_still_not_expired_or_legacy_vectors(server_module, monkeypatch):
    hits = [
        {"key": "company", "distance": 0.2, "metadata": md(chunk_text="repair rules")},
        {"key": "staff-sop", "distance": 0.2, "metadata": md("incoming/staff/company/sops/water-damage/v1.md", chunk_text="repair sop")},
        {"key": "other-lease", "distance": 0.2, "metadata": md("incoming/renters/leases/L-0002/contracts/lease-contract/v1.md", chunk_text="repair lease")},
        {"key": "legacy", "distance": 0.01, "metadata": {"tenant_id": "T001", "chunk_text": "repair legacy"}},
        {"key": "expired", "distance": 0.01, "metadata": md(frm="2020-01-01", to="2020-12-31", chunk_text="repair expired")},
    ]
    monkeypatch.setattr(server_module, "is_rag_enabled", lambda: True)
    monkeypatch.setattr(server_module, "MAX_CHUNKS_PER_DOC", 5)
    monkeypatch.setattr(
        server_module, "search_text_chunks", lambda query, top_k, metadata_filter=None: hits
    )
    sources = server_module.retrieve_sources("repair", fetch_n=20, return_n=10, scope=STAFF)
    assert sorted(s.vector_key for s in sources) == ["company", "other-lease", "staff-sop"]


def test_the_filter_and_the_per_hit_check_agree_through_retrieve_sources(server_module, monkeypatch):
    """A fake store that honours the filter returns exactly what retrieval then keeps."""
    universe = {
        "company": md(chunk_text="repair"),
        "own-lease": md("incoming/renters/leases/L-0001/contracts/lease-contract/v1.md", chunk_text="repair"),
        "other-lease": md("incoming/renters/leases/L-0002/contracts/lease-contract/v1.md", chunk_text="repair"),
        "staff": md("incoming/staff/company/sops/water-damage/v1.md", chunk_text="repair"),
        # S3 Vectors matches arrays by any element, so this reaches the result set and only the
        # per-hit check keeps it out (verified live; ingestion never writes lists).
        "array-audience": md(chunk_text="SECRET repair", audience=["renter", "staff"]),
        "array-lease": md("incoming/renters/leases/L-0001/contracts/lease-contract/v1.md", chunk_text="SECRET repair", lease_id=["L-0001", "L-0002"]),
    }

    def honouring_store(query, top_k, metadata_filter=None):
        return [{"key": k, "distance": 0.2, "metadata": m} for k, m in universe.items() if matches(metadata_filter, m)]

    monkeypatch.setattr(server_module, "is_rag_enabled", lambda: True)
    monkeypatch.setattr(server_module, "MAX_CHUNKS_PER_DOC", 5)
    monkeypatch.setattr(server_module, "search_text_chunks", honouring_store)
    for principal, expected in (
        ("T001", {"company", "own-lease"}),
        ("T002", {"company", "other-lease"}),
        # staff are shown a list-valued *scope id* (a known audience and scope), never a list-valued audience
        ("admin", {"company", "own-lease", "other-lease", "staff", "array-lease"}),
    ):
        scope = RESOLVER.resolve(principal)
        reached = {k for k, m in universe.items() if matches(build_vector_filter(scope), m)}
        assert "array-audience" in reached  # the filter really does admit it
        got = {s.vector_key for s in server_module.retrieve_sources("repair", return_n=10, scope=scope)}
        assert got == expected, principal
        assert "array-audience" not in got and (principal == "admin" or "array-lease" not in got)


def test_same_named_documents_in_different_scopes_are_distinct_sources(server_module, monkeypatch):
    """Vectors carry ``source_key`` (scope-qualified); it groups chunks per document and
    must never leak through the public JSON or the model prompt."""
    keys = {
        "l1": "incoming/renters/leases/L-0001/contracts/lease-contract/v1.md",
        "l2": "incoming/renters/leases/L-0002/contracts/lease-contract/v1.md",
    }
    raw_hits = [
        {
            "key": f"{name}/{i}",
            "distance": 0.1 + 0.01 * n,
            "metadata": md(key, chunk_text=f"lease answer {name} {i}"),
        }
        for n, (name, key, i) in enumerate(
            [("l1", keys["l1"], 0), ("l1", keys["l1"], 1), ("l2", keys["l2"], 0), ("l2", keys["l2"], 1)]
        )
    ]
    monkeypatch.setattr(server_module, "is_rag_enabled", lambda: True)
    monkeypatch.setattr(
        server_module, "search_text_chunks", lambda query, top_k, metadata_filter=None: raw_hits
    )
    monkeypatch.setattr(server_module, "MAX_CHUNKS_PER_DOC", 1)

    sources = server_module.retrieve_sources("lease answer", fetch_n=5, return_n=3, scope=STAFF)

    # One chunk per document, from both leases (grouping by file name alone would yield 1).
    assert {s.vector_key.split("/")[0] for s in sources} == {"l1", "l2"}
    assert len(sources) == 2
    assert all(s.source_path is None for s in sources)
    payload = str([s.model_dump() for s in sources])
    prompt_block = server_module.build_retrieval_block(sources)
    for leaked in ("incoming/", "renters/leases", "L-0001"):
        assert leaked not in payload
        assert leaked not in prompt_block


def test_public_source_ids_are_positional_and_the_vector_key_never_leaves(server_module, monkeypatch):
    """Review finding: the raw vector key encodes scope, lease ids and S3 versions."""
    key = "renters/leases/L-0001/contracts/lease-contract/v1/3%2FL4kq/0"
    hits = [
        {"key": key, "distance": 0.1, "metadata": md("incoming/renters/leases/L-0001/contracts/lease-contract/v1.md", chunk_text="repair a")},
        {"key": "company/0", "distance": 0.2, "metadata": md(chunk_text="repair b")},
    ]
    monkeypatch.setattr(server_module, "is_rag_enabled", lambda: True)
    monkeypatch.setattr(server_module, "search_text_chunks", lambda query, top_k, metadata_filter=None: hits)
    sources = server_module.retrieve_sources("repair", scope=STAFF)
    assert [s.id for s in sources] == ["S1", "S2"]  # the [S#] citation labels
    assert sources[0].vector_key == key  # still available internally
    public = str([s.model_dump() for s in sources]) + server_module.build_retrieval_block(sources)
    for leaked in ("vector_key", "renters/", "L-0001", "3%2FL4kq", "lease-contract"):
        assert leaked not in public


def test_business_versions_are_distinct_documents(server_module, monkeypatch):
    v1 = "incoming/renters/company/policies/repair-policy/v1.md"
    v2 = "incoming/renters/company/policies/repair-policy/v2.md"
    raw_hits = [
        {"key": "v1", "distance": 0.1, "metadata": md(v1, "2020-01-01", "2099-12-31", chunk_text="repair rule one")},
        {"key": "v2", "distance": 0.2, "metadata": md(v2, "2020-01-01", "2099-12-31", chunk_text="repair rule two")},
    ]
    monkeypatch.setattr(server_module, "is_rag_enabled", lambda: True)
    monkeypatch.setattr(
        server_module, "search_text_chunks", lambda query, top_k, metadata_filter=None: raw_hits
    )
    monkeypatch.setattr(server_module, "MAX_CHUNKS_PER_DOC", 1)
    assert len(server_module.retrieve_sources("repair", scope=STAFF)) == 2
