"""Docling adapter: layout-model parsing with reading order, tables and page coordinates.

Docling reports boxes in the displayed page frame (CropBox origin, /Rotate applied), the same
frame PDF.js renders, so normalizing by the page size is enough. Verified against cropped and
90/270-degree rotated pages.
"""
import re
from importlib.metadata import version
from pathlib import Path

from .base import FLOW_FORMATS, SLIDE_FORMATS, ParseError, ProgressFn, clean_text, finish, normalized_bbox, table_markdown
from .formulas import read_formulas
from .spacing import restore_spacing

LABEL_TO_TYPE = {
    "title": "title",
    "section_header": "section_header",
    "text": "paragraph",
    "paragraph": "paragraph",
    "reference": "paragraph",
    "handwritten_text": "paragraph",
    "list_item": "list_item",
    "table": "table",
    "document_index": "table",
    "picture": "picture",
    "chart": "picture",
    "caption": "caption",
    "formula": "formula",
    "code": "code",
    "footnote": "footnote",
    "page_header": "page_header",
    "page_footer": "page_footer",
    # A check box with its label: an item of a list of options ("□" also bullets Korean reports).
    "checkbox_selected": "list_item",
    "checkbox_unselected": "list_item",
}
BOXES = "□☐☑☒■▣"


def _picture_text(item, doc, *, outside: list | None = None, pieces: list | None = None) -> str:
    """Text Docling found inside a picture (diagram labels, OCR of a screenshot), in order. Docling
    sometimes hangs an item lying outside the picture under it (a footnote under a QR code): that
    is no text of the picture, and goes to `outside` to be read as a block of its own."""
    parts = []
    for ref in getattr(item, "children", []) or []:
        try:
            child = ref.resolve(doc)
        except Exception:  # noqa: BLE001 - a dangling reference is not worth failing the parse
            continue
        if outside is not None and not _within(child, item, doc):
            outside.append(child)
            continue
        if getattr(child, "text", None):
            parts.append(clean_text(child.text))
            if pieces is not None:
                sizes = {no: (p.size.width, p.size.height) for no, p in doc.pages.items()}
                pieces.append(_block(child, "paragraph", clean_text(child.text), None, sizes, "docling"))
        parts += [p for p in _picture_text(child, doc, pieces=pieces).split("\n") if p] if getattr(child, "children", None) else []
    return "\n".join(p for p in parts if p.strip())


def _within(child, item, doc) -> bool:
    """Whether a child's centre lies in its picture's box (or either has no place on the page)."""
    inner, outer = getattr(child, "prov", None), getattr(item, "prov", None)
    if not inner or not outer:
        return True
    if inner[0].page_no != outer[0].page_no:
        return False
    page = doc.pages.get(outer[0].page_no)
    if page is None:
        return True
    a = inner[0].bbox.to_top_left_origin(page_height=page.size.height)
    b = outer[0].bbox.to_top_left_origin(page_height=page.size.height)
    cx, cy, pad = (a.l + a.r) / 2, (a.t + a.b) / 2, 2.0
    return b.l - pad <= cx <= b.r + pad and b.t - pad <= cy <= b.b + pad


def _table_text(item, *, once: bool = False) -> str:
    """Compact markdown from the cell grid; captions stay separate caption blocks. The grid holds
    a cell spanning columns in each of them: its text is said once, where it starts (a label
    spanning rows still applies to each row)."""
    grid = getattr(getattr(item, "data", None), "grid", None) or []
    return table_markdown([[cell.text if getattr(cell, "start_col_offset_idx", c) == c
                           and (not once or getattr(cell, "start_row_offset_idx", r) == r) else ""
                           for c, cell in enumerate(row)] for r, row in enumerate(grid)])


def _read_alike(text: str, other: str) -> bool:
    """Whether OCR text reads like (part of) another read of the same spot, a few letters apart."""
    keys = [re.sub(r"[\W_]+", "", t.lower()) for t in (text, other)]
    grams = [{k[i:i + 3] for i in range(len(k) - 2)} for k in keys]
    if not grams[0]:
        return bool(keys[0]) and keys[0] in keys[1]
    return len(grams[0] & grams[1]) >= 0.5 * len(grams[0])


def _distinct_reads(cells: list) -> list:
    """Docling OCRs each layout region over a bitmap, and two regions that overlap read what they
    share twice ("ATP ATP", "Modulation and Goding Schome Modulation and Coding Scheme"): a cell
    lying mostly inside a more confident one that reads alike is that text again. (On a turned
    scan the boxes of neighbouring lines overlap too: they read differently and stay.)"""
    if len(cells) < 2:
        return cells
    import numpy as np

    rects = [c.rect.to_bounding_box() for c in cells]
    box = np.array([[min(r.l, r.r), min(r.t, r.b), max(r.l, r.r), max(r.t, r.b)] for r in rects], dtype=float)
    area = np.maximum((box[:, 2] - box[:, 0]) * (box[:, 3] - box[:, 1]), 1e-6)
    kept: list[int] = []
    for i in sorted(range(len(cells)), key=lambda i: -(cells[i].confidence or 0.0)):
        if kept:
            k = box[kept]
            w = np.clip(np.minimum(k[:, 2], box[i, 2]) - np.maximum(k[:, 0], box[i, 0]), 0, None)
            h = np.clip(np.minimum(k[:, 3], box[i, 3]) - np.maximum(k[:, 1], box[i, 1]), 0, None)
            intersection = w * h
            # The higher-confidence read may have a tighter detector box. Comparing only
            # against the candidate's larger area misses the same physical line in that case.
            # Symmetric containment is safe only for identical text, similar line widths,
            # and aligned centres; repeated text at a different position remains distinct.
            widths = box[:, 2] - box[:, 0]
            heights = box[:, 3] - box[:, 1]
            same_line = (intersection >= 0.9 * np.minimum(area[i], area[kept])) \
                & (np.minimum(widths[i], widths[kept]) >= 0.8 * np.maximum(widths[i], widths[kept])) \
                & (np.abs((box[i, 1] + box[i, 3]) - (k[:, 1] + k[:, 3]))
                   <= 0.7 * np.maximum(heights[i], heights[kept]))
            key = re.sub(r"\s+", "", cells[i].text).casefold()
            if any(key == re.sub(r"\s+", "", cells[kept[j]].text).casefold()
                   for j in np.flatnonzero(same_line)) or any(
                _read_alike(cells[i].text, cells[kept[j]].text)
                for j in np.flatnonzero(intersection >= 0.6 * area[i])
            ):
                continue
        kept.append(i)
    if len(kept) == len(cells):
        return cells
    # Each region's cells come in its own order, one region after another: what is left of two
    # regions read over each other goes back in the order of the page (Docling joins a block's
    # cells in the order given), lines top to bottom and each line left to right.
    order = sorted(kept, key=lambda i: (box[i, 1] + box[i, 3]) / 2)
    lines: list[list[int]] = []
    for i in order:
        if lines:
            j = lines[-1][-1]
            shared = min(box[i, 3], box[j, 3]) - max(box[i, 1], box[j, 1])
            if shared >= 0.5 * min(box[i, 3] - box[i, 1], box[j, 3] - box[j, 1]):
                lines[-1].append(i)
                continue
        lines.append([i])
    return [cells[i] for line in lines for i in sorted(line, key=lambda i: box[i, 0])]


def _read_once() -> None:
    """OCR cells go through _distinct_reads before Docling merges them with the page's own text."""
    from docling.models.base_ocr_model import BaseOcrModel

    process = BaseOcrModel.post_process_cells
    if getattr(process, "distinct_reads", False):
        return

    def post_process_cells(self, ocr_cells, *args, **kwargs):
        return process(self, _distinct_reads(ocr_cells), *args, **kwargs)

    post_process_cells.distinct_reads = True
    BaseOcrModel.post_process_cells = post_process_cells


# Resolution OCR renders bitmap regions at, relative to 72 dpi. Docling's default 3 read no more
# words than 2 on our test scans and took 0.7 GB more memory on an image-heavy deck.
OCR_SCALE = 2.0


def parse(path: Path, *, file_meta: dict, models_dir: Path, threads: int, ocr: bool, progress: ProgressFn,
          table_mode: str = "fast", formula_seconds: float = 0):
    pages, blocks, parser_version = extract(path, models_dir=models_dir, threads=threads, ocr=ocr, progress=progress,
                                           table_mode=table_mode,
                                           retain_table_ocr=file_meta.get("sourceFormat") in FLOW_FORMATS | SLIDE_FORMATS)
    parser_id = "docling-ocr" if ocr else "docling"
    if ocr:
        for block in blocks:
            # Restore spacing; duplicate OCR reads are removed only with geometric evidence.
            block["text"] = restore_spacing(block["text"])
    read_formulas(path, blocks, models_dir, threads, formula_seconds)
    meta = {"pageCount": len(pages), **file_meta, "parser": parser_id, "parserVersion": parser_version}
    return finish(meta, pages, blocks)


def extract(path: Path, *, models_dir: Path, threads: int, ocr: bool, progress: ProgressFn,
            table_mode: str = "fast", retain_table_ocr: bool = False) -> tuple[list, list, str]:
    """Pages and raw blocks (see base.finish) in Docling's reading order."""
    from docling.datamodel.base_models import ConversionStatus, InputFormat
    from docling.datamodel.pipeline_options import AcceleratorOptions, PdfPipelineOptions, RapidOcrOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling_core.types.doc import ContentLayer

    opts = PdfPipelineOptions(artifacts_path=str(models_dir))
    opts.do_table_structure = True
    opts.table_structure_options.mode = TableFormerMode.FAST if table_mode == "fast" else TableFormerMode.ACCURATE
    opts.do_ocr = ocr
    # The layout model can put native text and scanned formulas in the wrong
    # columns. Keep the OCR's own positions until physical table restoration.
    opts.generate_parsed_pages = ocr and retain_table_ocr
    if ocr:
        # The Korean PP-OCRv5 recognizer also reads Latin text, so prefer it when installed.
        korean = models_dir / "RapidOcr-korean"
        extra = {}
        if (korean / "korean_PP-OCRv5_rec_mobile.onnx").exists():
            extra = {"rec_model_path": str(korean / "korean_PP-OCRv5_rec_mobile.onnx"),
                     "rec_keys_path": str(korean / "ppocrv5_korean_dict.txt")}
        # No text-direction classifier: it flipped about a quarter of Korean lines upside down,
        # which then read as garbage (Korean word recall 72% with it, 98% without).
        opts.ocr_options = RapidOcrOptions(force_full_page_ocr=False, use_cls=False, scale=OCR_SCALE, **extra)
        _read_once()
    opts.accelerator_options = AcceleratorOptions(num_threads=threads, device="cpu")
    from .visible_backend import backend_for

    backend = backend_for(path)
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(
        pipeline_options=opts, backend=backend)})

    def on_progress(event) -> None:
        if getattr(event, "kind", None) == "page_completed":
            progress(event.completed_pages, event.total_pages)

    result = converter.convert(path, raises_on_error=False, progress_callback=on_progress)
    if result.status not in (ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS):
        raise ParseError("parse_failed", "Docling could not parse this PDF.")
    doc = result.document

    sizes = {no: (p.size.width, p.size.height) for no, p in doc.pages.items()}
    pages = [{"index": no - 1, "width": w, "height": h} for no, (w, h) in sorted(sizes.items())]
    parser_id = "docling-ocr" if ocr else "docling"
    blocks: list[dict] = []
    table_ocr = {}
    if opts.generate_parsed_pages:
        for page in result.pages:
            if page.parsed_page is None or page.page_no not in sizes:
                continue
            w, h = sizes[page.page_no]
            cells = []
            for cell in page.parsed_page.textline_cells:
                if not cell.from_ocr or not cell.text.strip():
                    continue
                bb = cell.rect.to_bounding_box().to_top_left_origin(page_height=h)
                box = normalized_bbox(min(bb.l, bb.r), min(bb.t, bb.b), max(bb.l, bb.r), max(bb.t, bb.b), w, h)
                if box:
                    cells.append((page.page_no - 1, clean_text(cell.text), (box.x0, box.y0, box.x1, box.y1)))
            table_ocr[page.page_no - 1] = cells

    def add(item) -> None:
        label = getattr(getattr(item, "label", None), "value", None)
        if label is None:
            return
        block_type = LABEL_TO_TYPE.get(label, "other")
        text = clean_text(getattr(item, "text", "") or "")
        # A drawn check box is no text: its state goes in front of the item as its marker.
        ticked = None
        if label.startswith("checkbox") and not text.startswith(tuple(BOXES)):
            ticked = "☑" if label == "checkbox_selected" else "☐"
        outside: list = []
        pieces: list = []
        if block_type == "table":
            text = _table_text(item)
        elif block_type == "picture":
            text = _picture_text(item, doc, outside=outside, pieces=pieces)
        # Formulas keep their region even without text (Docling only reads them with enrichment
        # enabled), so the hybrid parser can fill them from the text layer.
        if text or block_type in ("picture", "table", "formula"):
            block = _block(item, block_type, text, ticked, sizes, parser_id)
            if block_type == "table":
                once = _table_text(item, once=True)
                if once != text:
                    block["_table_layout_text"] = (text, once)
                block["_table_ocr_cells"] = [cell for r in block["regions"]
                    for cell in table_ocr.get(r["pageIndex"], [])
                    if r["bbox"]["x0"] <= (cell[2][0] + cell[2][2]) / 2 <= r["bbox"]["x1"]
                    and r["bbox"]["y0"] <= (cell[2][1] + cell[2][3]) / 2 <= r["bbox"]["y1"]]
            if pieces:
                block["_picture_parts"] = pieces
            blocks.append(block)
        # Only what the walk below does not reach on its own (a caption also hangs in the body).
        for child in outside:
            if getattr(child, "self_ref", None) not in walked:
                walked.add(getattr(child, "self_ref", None))
                add(child)

    items = [item for item, _depth in doc.iterate_items(included_content_layers={ContentLayer.BODY, ContentLayer.FURNITURE})]
    walked = {getattr(item, "self_ref", None) for item in items}
    for item in items:
        add(item)
    _keep_table_owned_ocr(blocks)
    _order_visible_page_headings(blocks, {p - 1 for p in getattr(backend, 'visibility_filtered_pages', ())})
    return pages, blocks, version("docling")


def _keep_table_owned_ocr(blocks: list[dict]) -> None:
    """Do not inject a separate picture/caption's OCR into an overlapping table.

    A table's coarse layout box can contain a logo that already has its own
    block. Its page OCR is useful for that block, but is not additional table
    text. Require both matching text and almost complete spatial containment;
    the same words elsewhere on the page or an unread picture do not qualify.
    This filters restoration hints only, leaving the extracted blocks intact.
    """
    owners = {}
    key = lambda text: re.sub(r"\s+", "", text)
    for block in blocks:
        text = key(block['text'])
        if block['type'] == 'table' or not text:
            continue
        for region in block['regions']:
            owners.setdefault(region['pageIndex'], []).append((text, region['bbox']))
    for block in blocks:
        if not block.get('_table_ocr_cells'):
            continue
        kept = []
        for cell in block['_table_ocr_cells']:
            page, text, box = cell
            text = key(text)
            area = max(0, box[2] - box[0]) * max(0, box[3] - box[1])
            owned = False
            for other_text, other in owners.get(page, []):
                if not text or text not in other_text or area <= 0:
                    continue
                intersection = max(0, min(box[2], other['x1']) - max(box[0], other['x0'])) \
                    * max(0, min(box[3], other['y1']) - max(box[1], other['y0']))
                if intersection >= .9 * area:
                    owned = True
                    break
            if not owned:
                kept.append(cell)
        block['_table_ocr_cells'] = kept


def _order_visible_page_headings(blocks: list[dict], filtered_pages: set[int]) -> None:
    """A heading visibly above all body text precedes that text after filtering.

    Removing hidden cells can make Docling put a wide top title between the two
    columns of a form. Do not sort columns or narrow local headings: move only
    a page-wide heading above every other body-text box on an affected page.
    """
    furniture = {'page_header', 'page_footer', 'picture'}
    for heading in list(blocks):
        if heading['type'] not in ('title', 'section_header') or len(heading['regions']) != 1:
            continue
        region = heading['regions'][0]
        page, box = region['pageIndex'], region['bbox']
        if page not in filtered_pages or box['x1'] - box['x0'] < .5 or box['y1'] > .15:
            continue
        body = [(i, b) for i, b in enumerate(blocks) if b is not heading and b['text'].strip()
                and b['type'] not in furniture and any(r['pageIndex'] == page for r in b['regions'])]
        if not body or any(r['bbox']['y0'] < box['y1'] for _, b in body
                           for r in b['regions'] if r['pageIndex'] == page):
            continue
        at, current = body[0][0], next(i for i, b in enumerate(blocks) if b is heading)
        if at < current:
            blocks.insert(at, blocks.pop(current))


def _block(item, block_type: str, text: str, ticked: str | None, sizes: dict, parser_id: str) -> dict:
    level = getattr(item, "level", None) if block_type == "section_header" else (0 if block_type == "title" else None)
    regions = []
    for prov in getattr(item, "prov", []) or []:
        size = sizes.get(prov.page_no)
        if not size:
            continue
        bb = prov.bbox.to_top_left_origin(page_height=size[1])
        box = normalized_bbox(bb.l, bb.t, bb.r, bb.b, *size)
        if box:
            regions.append({"pageIndex": prov.page_no - 1, "bbox": box.model_dump()})
    marker = (getattr(item, "marker", "") or "").strip()
    return {
        "type": block_type,
        "text": text,
        "level": level,
        "regions": regions,
        "source": {"parser": parser_id, "ref": getattr(item, "self_ref", None)},
        "_marker": ticked or (marker if block_type == "list_item" and getattr(item, "enumerated", False) and marker else None),
        "_bullet": True if ticked else None,
        # Docling formula text is LaTeX (from enrichment); text-layer math is not.
        "_latex": block_type == "formula" and bool(text),
    }
