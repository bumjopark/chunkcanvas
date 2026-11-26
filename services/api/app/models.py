"""Common document model. Every parser converts its output into these types; the frontend
never sees parser-specific formats. Field names are camelCase on the wire to match
packages/core/src/types.ts."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from pydantic.alias_generators import to_camel

BlockType = Literal[
    "title", "section_header", "paragraph", "list_item", "table", "picture", "caption",
    "formula", "code", "footnote", "page_header", "page_footer", "other",
]
DocStatus = Literal["queued", "processing", "completed", "failed"]
ParserId = Literal["hybrid", "docling", "docling-ocr", "pymupdf"]


class Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    def dump(self) -> dict:
        return self.model_dump(by_alias=True, exclude_none=True)


class BBox(Model):
    x0: float = Field(ge=0, le=1)
    y0: float = Field(ge=0, le=1)
    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)


class BlockRegion(Model):
    page_index: int = Field(ge=0)
    bbox: BBox


class BlockSource(Model):
    parser: str
    ref: str | None = None


class TextLine(Model):
    """A visual line of a block: offsets into block.text and the left edge of each character."""

    page_index: int = Field(ge=0)
    bbox: BBox
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    xs: list[float]


class DocumentBlock(Model):
    # Ephemeral OCR evidence for restoring printed table cells. Never serialized
    # into the API or a saved document: (page index, text, normalized box).
    _table_ocr_cells: list[tuple[int, str, tuple[float, float, float, float]]] = PrivateAttr(default_factory=list)
    # Layout markdown and the same layout with each spanning cell emitted once.
    # This distinguishes a model's repeated cell reference from independent reads.
    _table_layout_text: tuple[str, str] | None = PrivateAttr(default=None)
    id: str
    reading_order: int
    type: BlockType
    text: str
    markdown: str | None = None
    level: int | None = None
    regions: list[BlockRegion]
    # Present when character positions are known; enables splitting inside the block.
    lines: list[TextLine] | None = None
    line_geometry: Literal["exact", "estimated", "unavailable", "unverified"] | None = None
    source: BlockSource


class PageInfo(Model):
    index: int
    width: float
    height: float


class DocumentMeta(Model):
    id: str
    filename: str
    # Hash of the file as uploaded, even when only its first pages were kept.
    file_hash: str
    page_count: int
    source_page_count: int | None = None
    # Original format when the upload was converted to PDF for display (e.g. "docx", "hwpx").
    source_format: str | None = None
    parser: str
    parser_version: str


class ParsedDocument(Model):
    schema_version: Literal[1] = 1
    # Absent in legacy files. Parser internals stay in code points; API output is UTF-16.
    offset_encoding: Literal["codepoint", "utf16"] = "codepoint"
    document: DocumentMeta
    pages: list[PageInfo]
    blocks: list[DocumentBlock]
    blocks_fingerprint: str


class ChunkSettings(Model):
    target_tokens: int = Field(ge=20, le=8000)
    max_tokens: int = Field(ge=20, le=16000)
    overlap_tokens: int = Field(default=0, ge=0, le=2000)
    structure_aware: bool
    keep_heading_with_body: bool
    exclude_furniture: bool

    @model_validator(mode="after")
    def _max_not_below_target(self) -> "ChunkSettings":
        if self.max_tokens < self.target_tokens:
            raise ValueError("maxTokens must be at least targetTokens")
        return self


class Boundary(Model):
    """A cut after a whole block, or before character `offset` of the block's text."""

    block_id: str
    offset: int | None = Field(default=None, gt=0)


class Segment(Model):
    """Characters [start, end) of a block's text."""

    block_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class SplitPlan(Model):
    document_id: str
    blocks_fingerprint: str
    revision: int = Field(ge=0)
    boundaries: list[Boundary] = Field(max_length=100_000)
    settings: ChunkSettings
    # Deleted text, left out of every chunk (plans saved before chunks could be deleted have none).
    excluded: list[Segment] = Field(default_factory=list, max_length=100_000)


class SaveSplitPlan(Model):
    base_revision: int = Field(ge=0)
    plan: SplitPlan
