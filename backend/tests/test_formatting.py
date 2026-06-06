"""
Unit tests for text-formatting helpers.

Covers `shorten_snippet` (whitespace normalization + truncation) and
`build_retrieval_block` (citation block assembly).
"""


class TestShortenSnippet:
    def test_under_limit_unchanged(self, server_module):
        assert server_module.shorten_snippet("hello world", max_chars=50) == (
            "hello world"
        )

    def test_normalizes_whitespace(self, server_module):
        assert server_module.shorten_snippet("a\n\n  b\tc", max_chars=50) == "a b c"

    def test_truncates_with_ellipsis(self, server_module):
        out = server_module.shorten_snippet("word " * 100, max_chars=20)
        assert len(out) <= 20
        assert out.endswith("…")


class TestBuildRetrievalBlock:
    def test_empty_sources_returns_empty_string(self, server_module):
        assert server_module.build_retrieval_block([]) == ""

    def test_formats_sources_with_citation_markers(self, server_module):
        s1 = server_module.SourceItem(
            id="1", title="Title A", source_path="kb/a.md", snippet="snippet a"
        )
        s2 = server_module.SourceItem(id="2", snippet="snippet b")

        block = server_module.build_retrieval_block([s1, s2])

        assert "RETRIEVED KNOWLEDGE" in block
        assert "[S1] Title A" in block
        assert "Path: kb/a.md" in block
        # Falls back to the id as the header when title/path are absent.
        assert "[S2] 2" in block

    def test_prefers_context_over_snippet(self, server_module):
        source = server_module.SourceItem(
            id="1", snippet="short snippet", context="full context body"
        )
        block = server_module.build_retrieval_block([source])
        assert "full context body" in block
        assert "short snippet" not in block
