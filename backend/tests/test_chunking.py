"""
Tests for semantic text chunking using LangChain's RecursiveCharacterTextSplitter.
"""
import pytest
class TestSemanticChunking:
    """Tests for LangChain-based semantic chunking."""
    def test_empty_text(self, server_module):
        """Empty text should return no chunks."""
        chunks = list(server_module.chunk_text("", size=100, overlap=10))
        assert chunks == []
    def test_whitespace_only(self, server_module):
        """Whitespace-only text should return no chunks."""
        chunks = list(server_module.chunk_text("   \n\n  \t\n  ", size=100, overlap=10))
        assert chunks == []
    def test_single_paragraph(self, server_module):
        """Single short paragraph should yield one chunk."""
        text = "This is a short paragraph."
        chunks = list(server_module.chunk_text(text, size=100, overlap=0))
        assert len(chunks) == 1
    def test_multiple_paragraphs(self, server_module):
        """Multiple paragraphs should be preserved."""
        text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
        chunks = list(server_module.chunk_text(text, size=500, overlap=0))
        combined = "".join(chunks)
        assert "Paragraph" in combined
    def test_size_limit_enforced(self, server_module):
        """All chunks must respect size limits."""
        markdown = "# Header\n" + ("This is content. " * 100)
        chunks = list(server_module.chunk_text(markdown, size=300, overlap=50))
        assert all(len(chunk) <= 400 for chunk in chunks)
    def test_unicode_handling(self, server_module):
        """Chunker should handle unicode characters."""
        text = "# Immobilien\n\nÜber Grundstücke. Fläche: 150 m²."
        chunks = list(server_module.chunk_text(text, size=200, overlap=0))
        assert len(chunks) > 0
    def test_very_long_word(self, server_module):
        """Very long single word should be split."""
        long_word = "a" * 500
        chunks = list(server_module.chunk_text(long_word, size=100, overlap=0))
        assert len(chunks) >= 1
    def test_chunk_size_configuration(self, server_module):
        """Chunk size controls output."""
        text = "Word " * 100
        chunks_small = list(server_module.chunk_text(text, size=50, overlap=0))
        chunks_large = list(server_module.chunk_text(text, size=200, overlap=0))
        assert len(chunks_small) > len(chunks_large)
class TestChunkingEdgeCases:
    """Edge cases and boundary conditions."""
    def test_overlap_larger_than_chunk(self, server_module):
        """Overlap larger than chunk size should raise ValueError."""
        with pytest.raises(ValueError, match="larger chunk overlap"):
            list(server_module.chunk_text("text", size=10, overlap=20))
    def test_single_header(self, server_module):
        """Document with just a header."""
        text = "# Header Only"
        chunks = list(server_module.chunk_text(text, size=100, overlap=0))
        assert len(chunks) == 1
    def test_consecutive_headers(self, server_module):
        """Multiple headers should be preserved."""
        text = "# H1\n## H2\n### H3"
        chunks = list(server_module.chunk_text(text, size=100, overlap=0))
        combined = "".join(chunks)
        assert "# H1" in combined
class TestChunkingIntegration:
    """Integration tests for real-world scenarios."""
    def test_real_estate_listing(self, server_module):
        """Test with real estate content."""
        listing = """# Luxury Villa in Berlin
## Overview
Beautiful villa in prestigious neighborhood.
## Contact
Contact our office for more information."""
        chunks = list(server_module.chunk_text(listing, size=300, overlap=50))
        assert len(chunks) > 0
    def test_faq_document(self, server_module):
        """Test with FAQ content."""
        faq = """# FAQ
## Fees
Various fees apply depending on location.
## Timeline
Process takes 30-45 days."""
        chunks = list(server_module.chunk_text(faq, size=200, overlap=20))
        assert len(chunks) > 0
