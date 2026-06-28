"""Markdown-aware text chunking shared by chat ingestion and the worker."""
from typing import Iterator

from langchain_text_splitters import RecursiveCharacterTextSplitter


def chunk_text(text: str, size: int, overlap: int) -> Iterator[str]:
    """Yield text chunks using LangChain's RecursiveCharacterTextSplitter.

    The splitter respects markdown structure (paragraphs, lines, words) before
    falling back to character-based splitting. Empty/whitespace-only chunks are
    skipped.

    Args:
        text: The input text to chunk.
        size: Target chunk size in characters.
        overlap: Number of characters to overlap between chunks.

    Yields:
        Non-empty text chunks respecting semantic boundaries.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=size,
        chunk_overlap=overlap,
        separators=["\n\n", "\n", " ", ""],
        is_separator_regex=False,
    )

    for chunk in splitter.split_text(text):
        if chunk.strip():
            yield chunk
