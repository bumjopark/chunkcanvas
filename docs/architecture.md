# Architecture

```text
browser                                    server (FastAPI, one process)
┌──────────────────────────────────┐       ┌────────────────────────────────────────────┐
│ apps/web (React)                 │  REST │ upload checks → SQLite queue → worker thread │
│  pdf.js canvas + block overlays  │◀─────▶│   └─ subprocess per parse (bwrap sandbox)    │
│  zustand store, undo history     │       │        Docling │ PyMuPDF │ Docling+RapidOCR  │
│ packages/core (pure TS)          │       │        → ParsedDocument (common model)       │
│  auto-split, edits, chunks,      │       │ split-plan storage with revisions            │
│  token counts, exports           │       │ expiry sweeper, rate limits                  │
└──────────────────────────────────┘       └────────────────────────────────────────────┘
```

## Data model

Original file, parsed blocks, the user's split and the resulting chunks are kept apart:

- **ParsedDocument**: pages (displayed size in points) and **DocumentBlocks** in reading order.
  A block has a stable id (`b0007`), a type (title, section_header, paragraph, list_item,
  table, picture, caption, formula, code, footnote, page_header, page_footer, other), plain text,
  optional Markdown, and zero or more **regions** `{pageIndex, bbox}`. Several regions mean the
  block continues on another page or column; zero regions mean the parser could not place it.
  Text blocks may also carry **lines**: `{pageIndex, bbox, start, end, xs}` where `start..end`
  are offsets into the block text and `xs` holds the left edge of each character, which locates
  any character offset on the page. `blocksFingerprint` hashes parser, version, ids, types and
  texts.
- **SplitPlan**: `boundaries`, each `{blockId}` (cut after the whole block) or
  `{blockId, offset}` (the next chunk starts at that character), plus chunk settings, the
  fingerprint it belongs to and `excluded`: character ranges of deleted chunks, kept whatever
  happens to the boundaries around them until restored. A chunk whose text is all excluded is
  shown hatched and left out of exports (which number the chunks kept); an excluded part of a
  chunk is left out of its text. This is the only thing the user edits, and the only thing saved
  besides the parse. Offsets are allowed only inside paragraphs, list items, captions and
  footnotes that have line positions; headings, tables, code and pictures stay atomic.
- **Chunk**: derived on demand as a list of **segments** `{blockId, start, end}` between two
  boundaries, with the joined Markdown text, token count and page range. A block split by an
  inner boundary appears in two consecutive chunks with adjoining ranges; its Markdown prefix
  (`- `, `## `) stays on the first part only.

Invariants (enforced by `packages/core`, covered by tests including random edit runs with block
and inner cuts): every character of every chunkable block is in exactly one chunk, in reading
order; no chunk is empty; no boundary after the last block; no duplicates. Running headers and
footers can be excluded (default) and are then outside every chunk.

## Coordinates

All coordinates are normalized to 0..1 with the origin at the top-left of the page **as
displayed**: the CropBox with `/Rotate` applied, which is what pdf.js renders. Normalization
happens in the backend adapters:

- Docling reports bottom-left-origin boxes already in the displayed frame; they are flipped and
  divided by the page size.
- PyMuPDF reports text in the unrotated frame; boxes go through `page.rotation_matrix` first.

Both were verified against pages with a CropBox offset and 90°/270° rotation. The frontend only
multiplies by the rendered page size, so zoom never changes alignment.

## Editing

Block boundaries are drawn in the gap after a block: between it and the next block when that
block sits directly below in the same column, otherwise just under the block. They are never
page-wide lines, so two-column pages work. Inner boundaries are drawn as a caret between two
characters with the grip in the left margin. Selecting a chunk shows its sentence starts as
split points. Dragging any boundary snaps to the positions it may legally occupy (block gaps and
sentence starts between its neighbouring boundaries), previews the result live, and commits one
undoable step on drop.

## Hybrid parsing

`hybrid_parser.py` runs Docling for layout, then reads the PDF text layer with PyMuPDF
(`rawdict`: every character with its box, font size and weight):

1. For each Docling block, the characters inside its regions are collected and rebuilt into text
   (visual lines merged by baseline, spaces collapsed, hyphenated words rejoined, no space between
   CJK lines). When that text agrees with Docling's (similarity ≥ 0.9), it replaces Docling's text
   and the block gets line positions; otherwise Docling's text is kept.
2. The adapter assigns provisional heading types and levels from typography. The shared heading
   pass below resolves the final tree after source-table and word restoration.
3. Text-layer lines covered by no Docling block (outside pictures) become `recovered` paragraphs,
   inserted after the nearest block above them in the same column.

Rotated pages and vertical text keep Docling's text and block-level editing only.

Lessons from real documents, now handled:

- Some producers (PowerPoint) position words without space characters; a gap wider than 0.12 em
  between two characters becomes a space.
- Text boxes overlapping at the same height are kept as separate lines instead of interleaving.
- Blocks with several regions (a paragraph continuing in the next column) are rebuilt region by
  region, so their lines stay in reading order.
- Code blocks are rebuilt with line breaks and indentation measured in character cells.
- Formulas Docling leaves empty are filled with the text layer's (plain, non-LaTeX) math.
  Glyphs of TeX's math extension font (cmex/Latin Modern, usually without a ToUnicode map) are
  mapped by font name to `∑ ∏ ∫ √` and delimiters; pieces of extensible delimiters and braces are
  dropped, and a stack of identical pieces reads as one. MSBM capitals become `𝔼 ℝ …`. No other
  font is touched.
- Docling sometimes returns a whole page of plain paragraphs as one text region (plain text and
  Markdown rendered by LibreOffice). Lines inside a paragraph are evenly spaced, so a paragraph
  block is split where the space between two lines exceeds its usual gap by 0.6 line heights.
  On the real PDFs used for testing this never fires.
- Docling's formula boxes are loose. A text-layer line reached by both a formula and a text
  region belongs to the one covering more of its height (text on a tie), so a formula never
  copies the first line of the next paragraph.
- When Docling covers less than half of a page's text layer (fonts it cannot decode), the page is
  taken from the PyMuPDF parser, which keeps headings and lists.
- After source restoration and the existing geometry finalization, `heading_recognition.py`
  can recover headings from printed lines or source outlines. It keeps table, picture and
  running-furniture content, identity and geometry intact. Splits/merges retain original IDs
  where possible and renumber reading-order indexes; only changed text/type/IDs invalidate
  the fingerprint. New body fragments reuse exact character positions or get separately
  labelled estimates from their printed lines. Unsupported fragment positions stay unavailable.
- `heading_structure.py` resolves the final heading levels for every parser. Source PDF outlines
  take precedence; typography distinguishes styles, and printed numbering refines headings at
  the same size. Interrupted counters can resume only within their still-active parent. A
  missing numbered parent must not attach a child to a contradictory chapter.
- Heading levels are tree depths: title 0, root section 1, otherwise parent + 1. There is no
  depth cap; only Markdown's `#` markers stop at six. Sheets and slides only have gaps in their
  existing levels compressed, so their headings never become deeper.
- Core export reads those levels in original document order. `sections` stores the nearest
  preceding smaller-level parent; chunk paths are derived from that tree, including headings
  excluded from chunk text. Core does not repeat the parser's numbering or font rules.

With OCR available, the hybrid parser also OCRs pictures (`CC_OCR_PICTURES`, on by default):
Docling OCRs only layout regions over bitmaps that have no PDF text, and a picture block takes
the text found inside it. On a figure-heavy paper this added about half the parse time and no
text (its figures carry real text); on an 18-page Korean deck made mostly of images it took the
text from 8k to 19k characters, at about five times the parse time.

OCR (RapidOCR through Docling) runs without the text-direction classifier: on our Korean test
pages it flipped about a quarter of the lines upside down, which then read as garbage (word recall
72 % with it, 98 % without). The Korean recognizer also drops many spaces between words; OCR'd
text (never text-layer text) goes through Kiwi's spacing model, which kept 65 % of the reference
word boundaries on noisy 150 dpi scans instead of 19 %. EasyOCR spaced better but read fewer
words (89 %) at six times the time; larger RapidOCR detectors were slower without gains.
Bitmaps are rendered for OCR at 2x (144 dpi) instead of Docling's 3x: the same word recall,
faster, and 0.7 GB less peak memory on an 18-page image-heavy deck (3.1 GB instead of 3.8 GB).
Image uploads become pages at the image's own resolution (or an A4 width when unknown), not 96
dpi, so a 200 dpi scan is not rendered at several times its pixels.

Uploads without a usable text layer (fewer than 30 % of the first pages with 30+ characters) are
routed to the OCR parser and cut to its page limit; the editor says so. A page counts as a picture
of text when it has under 30 characters and images cover half of it, or no text at all and images
cover a tenth of it. A digital document with such pages keeps the hybrid parser and runs Docling
with OCR (notice `mixed_ocr`): Docling then OCRs bitmap regions only, so text-layer pages come out
identical, and OCR'd blocks carry no character positions.

## Splitters

`packages/core/src/splitters.ts` re-implements LangChain's RecursiveCharacterTextSplitter,
CharacterTextSplitter and TokenTextSplitter line by line; `test/splitters.test.ts` compares them
with fixtures produced by the real `langchain-text-splitters` (`scripts/gen_splitter_fixtures.py`).
Splitters run on the document text: chunkable blocks' Markdown joined by blank lines, with a map
from text offsets back to blocks. Their (possibly overlapping) ranges are drawn on the page for
preview; "Edit from this split" turns them into boundaries, cutting each chunk where its new text
begins and snapping cuts inside atomic blocks to the nearer edge. Average overlap becomes the
export overlap setting.

## Auto-split

Greedy over chunkable blocks, using per-block token estimates:

1. Split before a block when adding it would exceed the target (or must, above the maximum).
2. With *start chunks at headings*, also split before a heading once the chunk has at least a
   quarter of the target.
3. A caption and its adjacent table or picture stay together unless that exceeds the maximum.
4. With *keep headings with their text*, trailing headings move to the next chunk.

A single block larger than the maximum becomes its own chunk; if it is a paragraph with line
positions, it is then split at sentence ends into pieces of at most the target size.

## Other formats

Every upload is identified by content (`formats.py`: magic bytes, OLE stream names, the
`mimetype` entry of zip packages, `[Content_Types].xml`), never by its name. Non-PDF uploads are
stored as `original.<ext>` and converted in the background job (`convert.py`, stage
`converting`) before the usual inspection, truncation and parsing:

- Office formats go to headless LibreOffice with a per-job profile, after OOXML/ODF packages are
  rewritten without external relationships or INCLUDETEXT/INCLUDEPICTURE/LINK/DDE fields.
- HWPX is read from its XML (`hwpx.py`): section order from `content.hpf`, outline levels and
  character sizes from `header.xml`, tables with `cellSpan`, nested tables, pictures from `BinData`,
  footnotes. Font sizes are kept in the HTML so the PDF parser can still find headings in documents
  that use custom styles instead of outline levels. If the XML cannot be read, the preview text
  (`Preview/PrvText.txt`) is used.
- Formats LibreOffice opens directly: legacy and macro-enabled Office files, flat ODF, Word/Excel
  2003 XML, WordPerfect, StarOffice, HWP 3.0, and Apple iWork (whose '09 packages usually carry
  Apple's own `QuickLook/Preview.pdf`, used instead when present). Macro parts and the references
  to them are removed first. XPS goes through MuPDF; EPUB chapters are joined in spine order and
  sanitized like HTML. Password protection is recognized per format (OOXML EncryptedPackage, ODF
  manifest encryption data, Word's FIB flag, Excel's FILEPASS record, PowerPoint's
  EncryptedSummary, scrambled zip entries) and reported instead of failing in LibreOffice.
- HWP 5.x is read from its binary records (`hwp.py`, record models from pyhwp) into the same HTML
  as HWPX. pyhwp's own XHTML export ran an XSLT pass that took minutes on 1 MB documents; the
  records take seconds. Its text export is the fallback; a truncated file is reported as damaged.
- Hangul documents use tables for layout. A 1x1 table (a frame) becomes a box; a table with a
  cell over 100 characters (a form) is drawn without lines, so the layout parser keeps its text
  as paragraphs; only tables of short cells get borders and become table blocks. With borders
  everywhere, TableFormer turned forms into table blocks of 3,000+ characters with mixed-up cells.
- Long unbroken runs (codes, numbers, long compounds) get zero-width spaces every 6 characters:
  LibreOffice otherwise widens a column to its longest word and cuts the table off at the margin.
- Tables and pictures carry their sizes from the document (tables and cells as percentages of
  the text width, pictures with both width and height, shrunk to their cell). LibreOffice
  otherwise lays a picture out at its pixel size and cuts off whatever the wider table pushes
  past the right margin.
- HTML, Markdown, text and CSV become sanitized HTML; images are wrapped into a PDF and routed to
  OCR like any PDF without a text layer.
- With page truncation on, LibreOffice exports only the pages that will be kept, so a
  5,000-row spreadsheet converts in seconds; the editor then says the document may be longer.

## Parsing jobs

Uploads are validated in a short-lived subprocess (pypdfium2) before queuing. A worker thread
claims queued jobs from SQLite and runs each parse as a separate process with a timeout, a raised
OOM score and a file-size limit. With `CC_PARSER_SANDBOX=bwrap` the process runs in bubblewrap
with no network and write access only to its own document directory. Interrupted jobs are
re-queued on restart. Expired documents are deleted every 30 seconds.
