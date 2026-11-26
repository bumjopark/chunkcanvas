/** Normalized page coordinates: 0..1, origin at the top-left of the page as displayed. */
export interface BBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/** One visible piece of a block. A block that spans pages or columns has several. */
export interface BlockRegion {
  pageIndex: number;
  bbox: BBox;
}

/** A visual line of a block: offsets into block.text and the left edge (normalized x) of each character. */
export interface TextLine {
  pageIndex: number;
  bbox: BBox;
  start: number;
  end: number;
  xs: number[];
}

export type BlockType =
  | 'title'
  | 'section_header'
  | 'paragraph'
  | 'list_item'
  | 'table'
  | 'picture'
  | 'caption'
  | 'formula'
  | 'code'
  | 'footnote'
  | 'page_header'
  | 'page_footer'
  | 'other';

export interface DocumentBlock {
  id: string;
  readingOrder: number;
  type: BlockType;
  /** Plain text as extracted. */
  text: string;
  /** Markdown serialization used in chunk text (headings, lists, tables). Falls back to `text`. */
  markdown?: string;
  /** Heading tree depth: title = 0, root section = 1, otherwise parent + 1 in new parses. */
  level?: number;
  /** Empty when the parser could not locate the block on the page. Never invented. */
  regions: BlockRegion[];
  /** Character positions, when known. Required for splitting inside the block. */
  lines?: TextLine[];
  lineGeometry?: 'exact' | 'estimated' | 'unavailable' | 'unverified' | null;
  source: { parser: string; ref?: string };
}

export interface PageInfo {
  index: number;
  /** Displayed size in PDF points (CropBox with /Rotate applied). */
  width: number;
  height: number;
}

export interface DocumentMeta {
  id: string;
  filename: string;
  /** Hash of the file as uploaded, even when only its first pages were kept. */
  fileHash: string;
  pageCount: number;
  /** Pages in the uploaded file when it was cut to the first `pageCount` pages. */
  sourcePageCount?: number;
  /** Original format when the upload was converted to PDF for display (e.g. "docx", "hwpx"). */
  sourceFormat?: string;
  parser: string;
  parserVersion: string;
}

export interface ParsedDocument {
  schemaVersion: 1;
  /** API geometry and all SplitPlan offsets use UTF-16 code units. */
  offsetEncoding?: 'codepoint' | 'utf16';
  document: DocumentMeta;
  pages: PageInfo[];
  blocks: DocumentBlock[];
  /** Hash of block ids and texts; split plans are only valid for the same fingerprint. */
  blocksFingerprint: string;
}

export interface ChunkSettings {
  targetTokens: number;
  maxTokens: number;
  /** Reserved for export-time derived chunks. Editing always works on non-overlapping splits. */
  overlapTokens: number;
  /** Start a new chunk at headings when the current chunk is already substantial. */
  structureAware: boolean;
  /** Never leave a heading as the last block of a chunk when a split happens right after it. */
  keepHeadingWithBody: boolean;
  /** Leave running page headers and footers out of chunks. */
  excludeFurniture: boolean;
}

/**
 * Where one chunk ends and the next begins. Without `offset` the cut is after the whole block;
 * with it, the next chunk starts at that character of `block.text` (0 < offset < text length).
 */
export interface Boundary {
  blockId: string;
  offset?: number;
}

export interface SplitPlan {
  documentId: string;
  blocksFingerprint: string;
  revision: number;
  /** In reading order, never after the last block. */
  boundaries: Boundary[];
  settings: ChunkSettings;
  /**
   * Text left out of every chunk and export (deleted chunks): character ranges of block.text.
   * Kept whatever happens to the boundaries around it, until restored. A block without text is
   * left out by the range [0, 0].
   */
  excluded?: Segment[];
}

/** The part of a block that belongs to a chunk: characters [start, end) of block.text. */
export interface Segment {
  blockId: string;
  start: number;
  end: number;
}

export interface Chunk {
  id: string;
  index: number;
  segments: Segment[];
  /** Ids of the blocks the chunk touches, in order (a block split in two appears in both chunks). */
  blockIds: string[];
  text: string;
  tokenCount: number;
  /** 1-based inclusive page range; null when no block has a known location. */
  pageRange: [number, number] | null;
  overTarget: boolean;
  overMax: boolean;
  /** Every part of the chunk is excluded: shown so it can be restored, never exported. */
  deleted: boolean;
  /** Parts of `segments` excluded from the chunk; `text`, `tokenCount` and `pageRange` leave them out. */
  excluded: Segment[];
}

export type TokenCounter = (text: string) => number;

/** Full tokenizer, needed by token-window splitters and overlap. */
export interface Tokenizer {
  name: string;
  count: TokenCounter;
  encode: (text: string) => number[];
  decode: (tokens: number[]) => string;
}
