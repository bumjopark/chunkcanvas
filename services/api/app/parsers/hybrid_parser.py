"""Hybrid adapter: Docling for structure, PyMuPDF for the text layer.

Docling decides what the blocks are (type, reading order, regions). PyMuPDF then contributes:

- the exact text-layer characters inside each region, with per-character positions, which
  enables splitting inside paragraphs (used only when it agrees with Docling's text);
- heading levels from font sizes, since Docling reports most headings at one level;
- a coverage check: text-layer lines that no Docling block covers become "recovered"
  paragraphs instead of silently disappearing (text inside pictures is ignored on purpose).
"""
import re
import sys
from collections import Counter
from importlib.metadata import version
from pathlib import Path

from ..models import BBox
from . import docling_parser, pymupdf_parser
from .base import FLOW_FORMATS, PICTURE_MIN_WORDS, PICTURE_WORD, SHEET_FORMATS, SLIDE_FORMATS, SPLITTABLE, ProgressFn, finish
from .garbled import read_garbled
from .formulas import read_formulas
from .spacing import restore_spacing
from .pymupdf_parser import GLYPHS, list_marker, open_pdf
from .textlayer import Fragment, PageText, build_text, code_text, dominant_size, read_page, similarity, strip_prefix, tex_accents

AGREEMENT = 0.9
REMAPPED_AGREEMENT = 0.6
# Extra space between two lines of one block, in line heights, that starts a new paragraph.
PARAGRAPH_GAP = 0.6
SENTENCE_END = re.compile(r"[.!?。！？…:\"'”’)\]]\s*$")
# Recovered text this close to the top or bottom edge is a running header or footer.
FURNITURE_MARGIN = 0.07
HEADINGS = {"title", "section_header"}
MAX_LEVEL = 4
# Blocks that take the text layer's text (no character positions): fixes missing word spaces.
TEXT_ONLY = {"page_header", "page_footer", "other"}
# A picture on a workbook's page holding this much text, in rows, is cells: a chart carries a
# few dozen labels (150 characters in our samples), a printed part of a sheet hundreds.
CELL_CHARS = 600
CELL_ROWS = 10


def parse(path: Path, *, file_meta: dict, models_dir: Path, threads: int, progress: ProgressFn, table_mode: str = "fast",
          ocr: bool = False, formula_seconds: float = 0):
    """ocr: also read pictures without a text layer (scanned pages inside a digital document).
    Docling then runs OCR on bitmap regions only; text-layer text is still taken as is."""
    pages, blocks, docling_version = docling_parser.extract(
        path, models_dir=models_dir, threads=threads, ocr=ocr, progress=progress, table_mode=table_mode,
        retain_table_ocr=file_meta.get("sourceFormat") in FLOW_FORMATS | SLIDE_FORMATS
    )
    pdf = open_pdf(path)
    texts = [read_page(page, i) for i, page in enumerate(pdf)]
    stats = {"replaced": 0, "kept": 0, "recovered": 0, "fallbackPages": 0, "split": 0, "images": 0}
    boxes = _region_boxes(blocks)
    for block in blocks:
        _attach_text_layer(block, texts, stats, boxes)
        if ocr and not block.get("_chars"):
            # Nothing under it in the text layer: Docling's text is OCR.
            block["text"] = restore_spacing(block["text"])
    if ocr:
        blocks = _without_native_ocr_twins(blocks, pdf)
    if file_meta.get("sourceFormat") in SHEET_FORMATS:
        _cells_taken_for_pictures(blocks, pdf)
    _assign_heading_levels(blocks)
    blocks = _split_at_blank_lines(blocks, stats)
    # Pages where Docling read little of the text layer (e.g. fonts it cannot decode) are taken
    # from the PyMuPDF parser instead, which keeps headings and lists rather than bare paragraphs.
    fallback = {pt.index for pt in texts if _coverage(pt) < 0.5}
    sheet = file_meta.get("sourceFormat") in SHEET_FORMATS
    # A worksheet page Docling read as a heap of one-word paragraphs has lost its rows and
    # columns. The PyMuPDF parser reads the drawn grid from visible glyphs; use its text
    # blocks there when it finds a table without losing text. Pictures stay Docling's.
    soup = _paragraph_soup_pages(blocks) - fallback if sheet else set()
    if fallback or soup:
        _, fast_blocks = pymupdf_parser.extract(path, progress=lambda done, total: None, physical_tables=sheet)
        blocks = _replace_pages(blocks, fast_blocks, fallback)
        soup = {page for page in soup if _grid_reads_better(blocks, fast_blocks, page)}
        if soup:
            blocks = _replace_page_text(blocks, fast_blocks, soup)
            stats["gridPages"] = len(soup)
        stats["fallbackPages"] = len(fallback)
    blocks = _recover_uncovered(blocks, texts, stats, skip=fallback | soup)
    _pictures_of_repeated_images(blocks, pdf)
    blocks = _images_as_pictures(blocks, pdf, stats)
    # Lines in a font with a broken Unicode map read as stray letters: OCR reads them instead.
    read_garbled(pdf, blocks, models_dir, threads, stats)
    read_formulas(path, blocks, models_dir, threads, formula_seconds)
    if file_meta.get("sourceFormat") in SLIDE_FORMATS:
        from .slide_diagrams import group_diagram_rows
        blocks = group_diagram_rows(blocks, pdf, texts)
    # Counts only (never text) go to the job's stderr log for diagnosis.
    print(f"hybrid: {stats}", file=sys.stderr)
    meta = {
        "pageCount": len(pages),
        **file_meta,
        "parser": "hybrid",
        "parserVersion": f"docling {docling_version}; pymupdf {version('pymupdf')}",
    }
    return finish(meta, pages, blocks)


def _without_native_ocr_twins(blocks, pdf):
    """Remove a misplaced OCR echo beside its native PDF line, before line merging.

    Both native copies and separate scanned copies are content. Only an adjacent
    equal read with no native characters and no raster under it can be an echo.
    Dash variants at a line end are a common OCR difference in slide forms.
    """
    def key(text):
        return re.sub(r'\s+', '', text).translate(str.maketrans({'–': '-', '—': '-'})).rstrip('-')
    natives = {}
    images = {}
    for b in blocks:
        if b['type'] == 'paragraph' and len(b['regions']) == 1 and b.get('_chars') and any(c.c.isalnum() for c in b['_chars']):
            natives.setdefault(b['regions'][0]['pageIndex'], []).append(b)
    out = []
    for b in blocks:
        chars = b.get('_chars', [])
        if b['type'] != 'paragraph' or len(b['regions']) != 1 or any(c.c.isalnum() for c in chars) or len(key(b['text'])) < 6:
            out.append(b)
            continue
        region = b['regions'][0]; pn = region['pageIndex']; box = region['bbox']
        if pn not in images:
            p = pdf[pn]
            images[pn] = [dict(zip(('x0', 'y0', 'x1', 'y1'),
                                  (v / (p.rect.width if i % 2 == 0 else p.rect.height)
                                   for i, v in enumerate(item['bbox'])))) for item in p.get_image_info()]
        raster = any(_overlap(box, image) > 0 for image in images[pn])
        echo = False
        if not raster and box['y1'] - box['y0'] <= .025:
            for native in natives.get(pn, []):
                nb = native['regions'][0]['bbox']
                if (key(native['text']) == key(b['text'])
                        and -.004 <= box['x0'] - nb['x1'] <= .015
                        and max(abs(box[y] - nb[y]) for y in ('y0', 'y1')) <= .003):
                    echo = True
                    if chars:
                        # A real terminal dash can lie inside the echoed box.
                        # Keep that glyph and its geometry with the native line.
                        text, lines, merged_chars = build_text([Fragment(native['_chars']), Fragment(chars)], pn, pdf[pn].rect.width)
                        native.update(text=text, lines=lines, _chars=merged_chars,
                                      regions=[{'pageIndex': pn, 'bbox': ln['bbox']} for ln in lines])
                    break
        if not echo:
            out.append(b)
    return out


def _image_pages(pdf) -> tuple[dict, dict]:
    """Each page's images (normalized box, identity) and the pages each image is drawn on."""
    per_page: dict[int, list[tuple[list[float], object]]] = {}
    pages_of: dict = {}
    for pn, page in enumerate(pdf):
        w, h = page.rect.width, page.rect.height
        per_page[pn] = []
        for n, info in enumerate(page.get_image_info(xrefs=True)):
            x0, y0, x1, y1 = info["bbox"]
            r = [max(0.0, x0 / w), max(0.0, y0 / h), min(1.0, x1 / w), min(1.0, y1 / h)]
            key = info.get("xref") or info.get("digest") or (pn, n)
            per_page[pn].append((r, key))
            pages_of.setdefault(key, set()).add(pn)
    return per_page, pages_of


def _edge_decoration(rect) -> bool:
    x0, y0, x1, y1 = rect
    width, height = x1 - x0, y1 - y0
    return ((width * height <= .08 or (width >= .5 and height <= .2)) and (y1 <= .25 or y0 >= .75)
            or height >= .5 and width <= .1 and (x1 <= .15 or x0 >= .85))


def _repeated_image_decorations(per_page: dict, pages_of: dict) -> set:
    """Identity alone cannot distinguish a running logo from a reused illustration.

    Only edge occurrences repeated across three pages qualify. A large cover copy
    of that same image remains content; mirrored margins can shift the edge band.
    """
    positions: dict = {}
    for page, images in per_page.items():
        for rect, key in images:
            if _edge_decoration(rect):
                positions.setdefault(key, []).append((page, rect))
    repeated = set()
    for key, occurrences in positions.items():
        if len(pages_of[key]) < 3:
            continue
        if any(len({page for page, other in occurrences
                    if max(abs(a-b) for a,b in zip(rect,other)) <= .03}) >= 3 for _,rect in occurrences):
            repeated.add(key)
    return repeated


def _pictures_of_repeated_images(blocks: list[dict], pdf) -> None:
    """Mark repeated edge decorations, preserving reused pictures in the body."""
    per_page, pages_of = _image_pages(pdf)
    repeated = _repeated_image_decorations(per_page, pages_of)
    for b in blocks:
        if b["type"] != "picture" or len(b["regions"]) != 1:
            continue
        reg = b["regions"][0]
        box, area = reg["bbox"], (reg["bbox"]["x1"] - reg["bbox"]["x0"]) * (reg["bbox"]["y1"] - reg["bbox"]["y0"])
        if area <= 0:
            continue
        under = [(r, k) for r, k in per_page.get(reg["pageIndex"], [])
                 if _overlap({"x0": r[0], "y0": r[1], "x1": r[2], "y1": r[3]}, box) >= 0.5 * area]
        if under and _edge_decoration(tuple(box[k] for k in ('x0','y0','x1','y1'))) and all(k in repeated and _edge_decoration(r) for r,k in under):
            b["type"] = "page_header" if (box["y0"] + box["y1"]) / 2 <= 0.5 else "page_footer"


def _images_as_pictures(blocks: list[dict], pdf, stats: dict) -> list[dict]:
    """Images the layout model found no picture for (a patent's drawing alone on its page, a photo
    in a converted document) become pictures, so they are in a chunk and the vision model reads
    them. Images drawn next to each other (a figure in tiles, layers of a cover's artwork) are one
    picture; a page-sized one is the page itself (a scan). Repeated edge decorations are skipped,
    but reused body illustrations are retained."""
    found: dict[int, list[tuple[list[float], set]]] = {}
    per_page, pages_of = _image_pages(pdf)
    for pn, images in per_page.items():
        rects = []
        for r, key in images:
            if r[2] - r[0] < 0.02 or r[3] - r[1] < 0.02:
                continue
            rects.append((r, {key}))
        found[pn] = _join_touching(rects)
    repeated = _repeated_image_decorations(per_page, pages_of)
    for pn, joined in found.items():
        for r, keys in joined:
            area = (r[2] - r[0]) * (r[3] - r[1])
            # (An image within the top or bottom band is a letterhead's logo or a running mark.)
            if area < 0.02 or area > 0.85 or (keys <= repeated and _edge_decoration(r)) or r[3] <= 0.1 or r[1] >= 0.9:
                continue
            box = {"x0": r[0], "y0": r[1], "x1": r[2], "y1": r[3]}
            figure = text = 0.0
            inside = False
            words = 0
            for b in blocks:
                for reg in b["regions"]:
                    if reg["pageIndex"] != pn:
                        continue
                    rb = reg["bbox"]
                    if b["type"] not in ("picture", "table", "page_header", "page_footer") \
                            and box["x0"] <= (rb["x0"] + rb["x1"]) / 2 <= box["x1"] and box["y0"] <= (rb["y0"] + rb["y1"]) / 2 <= box["y1"]:
                        words += len(PICTURE_WORD.findall(b["text"]))
                    shared = _overlap(reg["bbox"], box)
                    if b["type"] in ("picture", "table"):
                        figure += shared / area
                        # A picture found inside the image (its drawing without the margin) is it.
                        own = (reg["bbox"]["x1"] - reg["bbox"]["x0"]) * (reg["bbox"]["y1"] - reg["bbox"]["y0"])
                        inside = inside or (own > 0 and shared >= 0.5 * own)
                    elif b["type"] not in ("page_header", "page_footer"):
                        text += shared / area
            # Text set on the image (a diagram's boxes drawn as images under their words, a banner
            # behind a title) is what it says: the image is its background. A drawing's reference
            # numerals are no words.
            if inside or figure >= 0.5 or text >= 0.5 or words >= 3:
                continue
            at = _insert_position(blocks, pn, box)
            blocks.insert(at, {"type": "picture", "text": "", "level": None,
                               "regions": [{"pageIndex": pn, "bbox": box}],
                               "source": {"parser": "pymupdf", "ref": "image"}})
            stats["images"] += 1
    return blocks


def _join_touching(rects: list[tuple[list[float], set]], gap: float = 0.005) -> list[tuple[list[float], set]]:
    """Rectangles that overlap or touch, joined into their bounding boxes (with the images in each)."""
    out = [(list(r), set(k)) for r, k in rects]
    merged = True
    while merged:
        merged = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                (a, ka), (b, kb) = out[i], out[j]
                if a[0] <= b[2] + gap and b[0] <= a[2] + gap and a[1] <= b[3] + gap and b[1] <= a[3] + gap:
                    out[i] = ([min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])], ka | kb)
                    del out[j]
                    merged = True
                    break
            if merged:
                break
    return out


def _overlap(a: dict, b: dict) -> float:
    return max(0.0, min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])) * max(0.0, min(a["y1"], b["y1"]) - max(a["y0"], b["y0"]))


def _cells_taken_for_pictures(blocks: list[dict], pdf) -> None:
    """On a workbook's page the layout model takes a sheet printed small for one picture, whose
    hundreds of cells a vision model then reads in part: a picture with no image in it and the
    text of many rows is that text, line by line, for the sheet's part to take its place."""
    for b in blocks:
        lines = b.get("_lines") or []
        if b["type"] != "picture" or len(b["regions"]) != 1 or len(b["text"]) < CELL_CHARS or len(lines) < CELL_ROWS:
            continue
        region = b["regions"][0]
        page = pdf[region["pageIndex"]]
        w, h, box = page.rect.width, page.rect.height, region["bbox"]
        if any(i["bbox"][0] / w < box["x1"] and i["bbox"][2] / w > box["x0"] and i["bbox"][1] / h < box["y1"]
               and i["bbox"][3] / h > box["y0"] for i in page.get_image_info()):
            continue
        b["type"], b["lines"] = "paragraph", lines


def _region_boxes(blocks: list[dict]) -> dict[tuple[str, int], list[BBox]]:
    """Formula and text regions per page, for settling lines both of them reach."""
    boxes: dict[tuple[str, int], list[BBox]] = {}
    for block in blocks:
        kind = "formula" if block["type"] == "formula" else "text" if block["type"] in SPLITTABLE | HEADINGS else None
        if kind:
            for region in block["regions"]:
                boxes.setdefault((kind, region["pageIndex"]), []).append(BBox.model_validate(region["bbox"]))
    return boxes


def _vertical_share(frag: Fragment, box: BBox) -> float:
    if min(frag.x1, box.x1) <= max(frag.x0, box.x0):
        return 0.0
    return max(0.0, min(frag.y1, box.y1) - max(frag.y0, box.y0))


def _owner_filter(block: dict, page_index: int, box: BBox, boxes: dict):
    """Display formulas sit close to the text around them and Docling's formula boxes are loose:
    a line reached by both a formula and a text region belongs to the one that covers more of
    its height (text on a tie), so no line is read twice or lost."""
    if block["type"] == "formula":
        rivals, wins_tie = boxes.get(("text", page_index), []), False
    elif block["type"] in SPLITTABLE | HEADINGS:
        rivals, wins_tie = boxes.get(("formula", page_index), []), True
    else:
        return None
    if not rivals:
        return None

    def keep(frag: Fragment) -> bool:
        mine = _vertical_share(frag, box)
        best = max(_vertical_share(frag, other) for other in rivals)
        return mine >= best if wins_tie else mine > best

    return keep


def _attach_text_layer(block: dict, texts: list[PageText], stats: dict, boxes: dict | None = None) -> None:
    """Mark the characters under the block as covered and, when the extractions agree, take the
    text-layer text and its line geometry."""
    # Docling keeps the codes of symbol fonts ("\uf070"); the page's text layer knows what they show.
    pages = {region["pageIndex"] for region in block["regions"] if region["pageIndex"] < len(texts)}
    if symbols := {ord(code): c for page in sorted(pages) for code, c in texts[page].symbols.items()}:
        block["text"] = block["text"].translate(symbols)
    # Accents TeX draws over letters ("Fr´echet") are put onto them, as in the text layer.
    if any(texts[page].accents for page in pages):
        block["text"] = tex_accents(block["text"])
    # One group of fragments per region, in the block's region order (column, then next column/page).
    found: list[tuple[int, list[Fragment]]] = []
    for region in block["regions"]:
        page_index = region["pageIndex"]
        if page_index >= len(texts):
            continue
        box = BBox.model_validate(region["bbox"])
        frags = texts[page_index].chars_in(box, keep=_owner_filter(block, page_index, box, boxes or {}))
        for frag in frags:
            for ch in frag.chars:
                ch.used = True
        if frags:
            found.append((page_index, frags))
    block["_chars"] = [ch for _, frags in found for frag in frags for ch in frag.chars]
    if not found or any(texts[page_index].rotated for page_index, _ in found):
        return
    width = texts[found[0][0]].width
    if block["type"] == "code":
        # Docling flattens code onto one line; the text layer still knows the line breaks.
        code = code_text([f for _, frags in found for f in frags], width)
        if code and similarity(code, block["text"]) >= AGREEMENT:
            block["text"] = code
            stats["replaced"] += 1
        return
    if block["type"] == "picture":
        # Text drawn inside a picture (diagram labels, slide graphics) is content too. The text
        # layer is exact; without one, Docling's text for the picture (OCR in mixed mode) stays.
        inner, inner_lines, _ = build_text([f for _, frags in found for f in frags], found[0][0], width)
        if len(PICTURE_WORD.findall(inner)) >= PICTURE_MIN_WORDS:
            block["text"] = inner
            block["_lines"] = inner_lines
        return
    if block["type"] == "formula" and not block["text"]:
        # Plain text-layer math is better for search than nothing; it is not LaTeX.
        block["text"] = build_text([f for _, frags in found for f in frags], found[0][0], width)[0]
        block["_latex"] = False
        return
    if block["type"] not in SPLITTABLE | HEADINGS | TEXT_ONLY:
        return

    # Rebuild text region by region, so a paragraph continuing in the next column keeps its order.
    text, lines = "", []
    for page_index, frags in found:
        part, part_lines, _ = build_text(frags, page_index, texts[page_index].width)
        if not part:
            continue
        shift = len(text) + (1 if text else 0)
        text = f"{text} {part}" if text else part
        lines += [{**ln, "start": ln["start"] + shift, "end": ln["end"] + shift} for ln in part_lines]
    if block["type"] == "section_header" and re.match(rf"[{GLYPHS}]\s+\S", text):
        # A bulleted line set in bold ("● Statistical profilers") is an item of a list, not a heading.
        block["type"], block["level"] = "list_item", None
    if block["type"] == "list_item" and text:
        # An item with no bullet or number on the page may go on from the one before it.
        block["_bullet"] = bool(bullet := list_marker(text)) or bool(block.get("_bullet"))
        if bullet:
            cut, marker = bullet
            text, lines = strip_prefix(text, lines, cut)
            block["_marker"] = block.get("_marker") or marker

    # Docling reads math fonts without a Unicode map as letters ("x P U" for "x ∈ U"); text with
    # glyphs mapped back cannot agree with that, so it only has to be roughly the same text.
    remapped = any(ch.remapped for _, frags in found for f in frags for ch in f.chars)
    agreement = REMAPPED_AGREEMENT if remapped else AGREEMENT
    # Text drawn twice in one place (a shadow, an animation copy) is read once from the text
    # layer; Docling reads it twice, line after line or word after word ("LIGUE LIGUE DE DE",
    # "LIBREOFFICE 6.2 LIBREOFFICE 6.2").
    once = re.sub(r"(?<!\S)(\S+(?: \S+){0,11}?)(?: \1(?!\S))+", r"\1", block["text"])
    if text and (similarity(text, block["text"]) >= agreement or similarity(f"{text} {text}", block["text"]) >= AGREEMENT
                 or (once != block["text"] and similarity(text, once) >= AGREEMENT)):
        block["text"] = text
        block["lines"] = lines if block["type"] in SPLITTABLE else None
        stats["replaced"] += 1
    else:
        stats["kept"] += 1


def _split_at_blank_lines(blocks: list[dict], stats: dict) -> list[dict]:
    """Docling sometimes returns a whole page of plain paragraphs as one text region (seen on
    plain text rendered by LibreOffice). Lines of one paragraph are evenly spaced; where the
    space between two lines exceeds the block's usual gap by more than PARAGRAPH_GAP line
    heights, a new paragraph starts."""
    out: list[dict] = []
    for block in blocks:
        lines = block.get("lines")
        if block["type"] != "paragraph" or not lines or len(lines) < 3:
            out.append(block)
            continue

        def gap(i: int) -> float | None:
            a, b = lines[i], lines[i + 1]
            if a["pageIndex"] != b["pageIndex"] or b["bbox"]["y0"] < a["bbox"]["y1"] - 0.002:
                return None  # next column or page: not a gap
            return b["bbox"]["y0"] - a["bbox"]["y1"]

        gaps = [g for g in map(gap, range(len(lines) - 1)) if g is not None]
        if not gaps:
            out.append(block)
            continue
        typical = sorted(gaps)[len(gaps) // 2]
        height = sorted(ln["bbox"]["y1"] - ln["bbox"]["y0"] for ln in lines)[len(lines) // 2]
        # Only after a sentence ends: a line taken by an inline formula also leaves a gap.
        cuts = [i + 1 for i in range(len(lines) - 1) if (g := gap(i)) is not None and g > typical + PARAGRAPH_GAP * height
                and SENTENCE_END.search(block["text"][lines[i]["start"]:lines[i]["end"]])]
        if not cuts:
            out.append(block)
            continue
        for a, b in zip([0, *cuts], [*cuts, len(lines)]):
            part = lines[a:b]
            shift = part[0]["start"]
            regions = []
            for page in dict.fromkeys(ln["pageIndex"] for ln in part):
                on_page = [ln["bbox"] for ln in part if ln["pageIndex"] == page]
                regions.append({"pageIndex": page, "bbox": {
                    "x0": min(bb["x0"] for bb in on_page), "y0": min(bb["y0"] for bb in on_page),
                    "x1": max(bb["x1"] for bb in on_page), "y1": max(bb["y1"] for bb in on_page)}})
            out.append({
                **{k: v for k, v in block.items() if not k.startswith("_")},
                "text": block["text"][shift:part[-1]["end"]],
                "regions": regions,
                "lines": [{**ln, "start": ln["start"] - shift, "end": ln["end"] - shift} for ln in part],
            })
        stats["split"] += len(cuts)
    return out


def _assign_heading_levels(blocks: list[dict]) -> None:
    """Rank heading font sizes: the largest becomes level 1 (or the title), and so on.

    Slide decks use many sizes, so sizes used by a single heading join the nearest larger level
    and levels stop at MAX_LEVEL; otherwise real slide titles end up five levels deep.
    """
    headings = [b for b in blocks if b["type"] in HEADINGS and b.get("_chars")]
    sizes = {id(b): dominant_size(b["_chars"])[0] for b in headings}
    clusters: list[float] = []
    for size in sorted(set(sizes.values()), reverse=True):
        if size and (not clusters or clusters[-1] - size > 0.6):
            clusters.append(size)
    if not clusters:
        return

    def nearest(size: float) -> int:
        return min(range(len(clusters)), key=lambda i: abs(clusters[i] - size))

    counts = [0] * len(clusters)
    for b in headings:
        counts[nearest(sizes[id(b)])] += 1

    # One heading alone in the biggest size, clearly above the next size, on the first page: title.
    top = [b for b in headings if nearest(sizes[id(b)]) == 0]
    has_title = any(b["type"] == "title" for b in blocks)
    if not has_title and len(top) == 1 and len(clusters) > 1 and clusters[0] >= clusters[1] * 1.25:
        if top[0]["regions"] and top[0]["regions"][0]["pageIndex"] == 0:
            top[0]["type"], top[0]["level"] = "title", 0
            has_title = True
    first = 1 if has_title and top and top[0]["type"] == "title" else 0

    # Level per cluster: frequent sizes count as levels; one-off sizes share the level above them.
    level_of, level = {}, 0
    for i in range(first, len(clusters)):
        if counts[i] > 1 or level == 0:
            level += 1
        level_of[i] = min(MAX_LEVEL, level)
    for b in headings:
        if b["type"] == "section_header":
            b["level"] = level_of.get(nearest(sizes[id(b)]), 1)


def _coverage(pt: PageText) -> float:
    """Share of the page's text-layer characters inside some Docling block (1.0 without text)."""
    chars = [ch for f in pt.fragments for ch in f.chars if ch.c != " "]
    if pt.rotated or len(chars) < 30:
        return 1.0
    return sum(ch.used for ch in chars) / len(chars)


def _page_of(block: dict) -> int | None:
    return block["regions"][0]["pageIndex"] if block["regions"] else None


def _replace_pages(blocks: list[dict], fast_blocks: list[dict], pages: set[int]) -> list[dict]:
    """Swap every block on the given pages for the PyMuPDF parser's blocks of those pages."""
    out = [b for b in blocks if _page_of(b) not in pages]
    for page in sorted(pages):
        replacement = [{**b, "source": {"parser": "pymupdf", "ref": "fallback"}} for b in fast_blocks if _page_of(b) == page]
        at = len(out)
        for i, b in enumerate(out):
            p = _page_of(b)
            if p is not None and p > page:
                at = i
                break
        out[at:at] = replacement
    return out


def _short_paragraph(block: dict) -> bool:
    return block["type"] in ("paragraph", "list_item") and len(block["text"].split()) <= 2


def _paragraph_soup_pages(blocks: list[dict]) -> set[int]:
    """Worksheet pages read as many one- or two-word paragraphs and no table."""
    by_page: dict[int, list[dict]] = {}
    for b in blocks:
        if (page := _page_of(b)) is not None:
            by_page.setdefault(page, []).append(b)
    return {page for page, items in by_page.items()
            if not any(b["type"] == "table" for b in items)
            and (short := sum(_short_paragraph(b) for b in items)) >= 20 and short >= .6 * len(items)}


def _content_chars(items: list[dict]) -> Counter:
    """Visible characters of blocks, without whitespace or Markdown table syntax."""
    out: Counter = Counter()
    for b in items:
        for line in b["text"].splitlines():
            if b["type"] == "table":
                if set(line) <= set("|-: "):
                    continue
                line = line.replace("|", "")
            out.update(ch for ch in line if not ch.isspace())
    return out


def _held_by_pictures(items: list[dict], pictures: list[dict]) -> list[dict]:
    """The items inside a picture whose characters that picture's own text already holds
    (chart labels Docling attached to the picture and PyMuPDF read as loose text)."""
    pools = []
    for p in pictures:
        if p["regions"] and p["text"].strip():
            pools.append((p["regions"][0]["bbox"], Counter(ch for ch in p["text"] if not ch.isspace())))
    held = []
    for b in items:
        if not b["regions"] or not b["text"].strip():
            continue
        box = b["regions"][0]["bbox"]
        cx, cy = (box["x0"] + box["x1"]) / 2, (box["y0"] + box["y1"]) / 2
        chars = Counter(ch for ch in b["text"] if not ch.isspace())
        for area, pool in pools:
            if area["x0"] <= cx <= area["x1"] and area["y0"] <= cy <= area["y1"] and not chars - pool:
                pool.subtract(chars)
                held.append(b)
                break
    return held


def _page_replacement(blocks: list[dict], fast_blocks: list[dict], page: int) -> list[dict]:
    """The PyMuPDF blocks that would replace this page's non-picture Docling blocks."""
    pictures = [b for b in blocks if _page_of(b) == page and b["type"] == "picture"]
    theirs = [b for b in fast_blocks if _page_of(b) == page and b["type"] != "picture"]
    held = _held_by_pictures(theirs, pictures)
    return [b for b in theirs if not any(b is x for x in held)]


def _grid_reads_better(blocks: list[dict], fast_blocks: list[dict], page: int) -> bool:
    """The PyMuPDF page has a table, keeps the text and leaves far fewer stray words.

    Text is compared character by character (table pipes and separator rows are not text).
    A Docling block reaching another page cannot be swapped without losing that part, so
    such a page keeps Docling's reading.
    """
    if any(len(spread := {r["pageIndex"] for r in b["regions"]}) > 1 and page in spread for b in blocks):
        return False
    pictures = [b for b in blocks if _page_of(b) == page and b["type"] == "picture"]
    ours = [b for b in blocks if _page_of(b) == page and b["type"] != "picture"]
    ours = [b for b in ours if not any(b is x for x in _held_by_pictures(ours, pictures))]
    theirs = _page_replacement(blocks, fast_blocks, page)
    wanted, kept = _content_chars(ours), _content_chars(theirs)
    return (any(b["type"] == "table" for b in theirs) and sum((wanted & kept).values()) >= .95 * sum(wanted.values())
            and sum(_short_paragraph(b) for b in theirs) <= sum(_short_paragraph(b) for b in ours) / 4)


def _replace_page_text(blocks: list[dict], fast_blocks: list[dict], pages: set[int]) -> list[dict]:
    """Swap the non-picture blocks of these pages for the PyMuPDF parser's, keeping pictures.

    A heading or running line that Docling also read keeps Docling's role, so a sheet title
    repeated at the top of every page stays a heading on swapped and unswapped pages alike.
    Loose text that a kept picture already holds (its chart labels) is not added again.
    """
    roles = {}
    for b in blocks:
        if _page_of(b) in pages and b["type"] in ("title", "section_header", "page_header", "page_footer"):
            roles.setdefault((_page_of(b), " ".join(b["text"].split())), (b["type"], b.get("level")))
    out, inserted = [], set()
    for b in blocks:
        page = _page_of(b)
        if page in pages and b["type"] != "picture":
            if page not in inserted:
                inserted.add(page)
                for f in _page_replacement(blocks, fast_blocks, page):
                    f = {**f, "source": {"parser": "pymupdf", "ref": "sheet-grid-page"}}
                    role = roles.get((page, " ".join(f["text"].split())))
                    if role and f["type"] in ("title", "section_header", "page_header", "page_footer", "paragraph"):
                        f["type"], f["level"] = role
                    out.append(f)
            continue
        out.append(b)
    return out


def _recover_uncovered(blocks: list[dict], texts: list[PageText], stats: dict, skip: set[int] = frozenset()) -> list[dict]:
    """Turn text-layer lines no block covers into paragraphs placed in reading order."""
    out = list(blocks)
    for pt in texts:
        if pt.rotated or pt.index in skip:
            continue
        # Only lines no block touched at all: a partly covered line is a box-edge effect, not lost
        # text. Tiny fragments such as equation numbers "(1)" are layout noise.
        leftovers = [
            f for f in pt.fragments
            if not any(ch.used for ch in f.chars) and sum(ch.c.isalnum() for ch in f.chars) >= 3
        ]
        for group in _group_lines(leftovers):
            text, lines, _ = build_text(group, pt.index, pt.width)
            if not text:
                continue
            box = {
                "x0": min(ln["bbox"]["x0"] for ln in lines), "y0": min(ln["bbox"]["y0"] for ln in lines),
                "x1": max(ln["bbox"]["x1"] for ln in lines), "y1": max(ln["bbox"]["y1"] for ln in lines),
            }
            margin = FURNITURE_MARGIN
            kind = "page_header" if box["y1"] < margin else "page_footer" if box["y0"] > 1 - margin else "paragraph"
            block = {
                "type": kind,
                "text": text,
                "level": None,
                "regions": [{"pageIndex": pt.index, "bbox": box}],
                "lines": lines if kind == "paragraph" else None,
                "source": {"parser": "pymupdf", "ref": "recovered"},
            }
            out.insert(_insert_position(out, pt.index, box), block)
            stats["recovered"] += 1
    return out


def _group_lines(fragments: list[Fragment]) -> list[list[Fragment]]:
    """Consecutive, horizontally overlapping lines with small vertical gaps form one paragraph."""
    groups: list[list[Fragment]] = []
    for frag in sorted(fragments, key=lambda f: (f.y0, min(c.x0 for c in f.chars))):
        x0, x1 = min(c.x0 for c in frag.chars), max(c.x1 for c in frag.chars)
        if groups:
            last = groups[-1][-1]
            lx0, lx1 = min(c.x0 for c in last.chars), max(c.x1 for c in last.chars)
            height = last.y1 - last.y0
            if frag.y0 - last.y1 < height * 1.2 and min(x1, lx1) - max(x0, lx0) > 0:
                groups[-1].append(frag)
                continue
        groups.append([frag])
    return groups


def _insert_position(blocks: list[dict], page_index: int, box: dict) -> int:
    """After the nearest block above in the same column on that page, else before the page's first."""
    best, best_y = None, -1.0
    first_on_page, last_before = None, -1
    for i, b in enumerate(blocks):
        for r in b["regions"]:
            if r["pageIndex"] < page_index:
                last_before = i
            if r["pageIndex"] != page_index:
                continue
            first_on_page = i if first_on_page is None else first_on_page
            rb = r["bbox"]
            overlaps = min(rb["x1"], box["x1"]) - max(rb["x0"], box["x0"]) > 0
            if overlaps and rb["y1"] <= box["y0"] + 0.005 and rb["y1"] > best_y:
                best, best_y = i, rb["y1"]
    if best is not None:
        return best + 1
    if first_on_page is not None:
        return first_on_page
    return last_before + 1

