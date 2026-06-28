"""Shared pydantic models."""
from typing import Optional

from pydantic import BaseModel, Field


class SourceItem(BaseModel):
    id: str
    title: Optional[str] = None
    source_path: Optional[str] = None
    snippet: str
    context: Optional[str] = Field(
        default=None, exclude=True
    )  # Internal model context, excluded from API response
    doc_type: Optional[str] = None
    chunk_index: Optional[int] = None
    distance: Optional[float] = None
