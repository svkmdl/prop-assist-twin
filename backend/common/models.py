"""Shared pydantic models."""
from typing import Optional

from pydantic import BaseModel, Field


class SourceItem(BaseModel):
    # Public, positional id ("S1", "S2", ...), matching the [S#] citation labels. The
    # vector key reveals scope, lease/property ids, bucket layout and S3 version ids,
    # so it is kept in ``vector_key``, which is never serialized.
    id: str
    vector_key: Optional[str] = Field(default=None, exclude=True)
    title: Optional[str] = None
    source_path: Optional[str] = None
    snippet: str
    context: Optional[str] = Field(
        default=None, exclude=True
    )  # Internal model context, excluded from API response
    doc_type: Optional[str] = None
    chunk_index: Optional[int] = None
    distance: Optional[float] = None
