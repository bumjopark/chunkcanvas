"""PyMuPDF adapter: fast text-layer parsing without ML models.

Block types come from simple typography heuristics (font size, weight, bullets, position), so
structure is rougher than Docling's. Reading order is the content-stream order, which follows
columns for most generated PDFs. Text and per-character positions come from the shared text
layer, so blocks on unrotated pages support splitting inside the block.
"""
import re
from collections import Counter, defaultdict
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

from .base import CAPTION, SHEET_FORMATS, SPLITTABLE, ParseError, ProgressFn, finish, normalized_bbox, table_markdown
from .textlayer import FLAGS, horizontal_line, Fragment, build_text, dominant_size, read_page, strip_prefix
from .visible_text import read_dict

# Bullet glyphs, including those of symbol fonts (❖ ➢ from Wingdings). Check marks and boxes
# say something (done, not done), so they stay.
GLYPHS = "•◦▪▫●○■□‣⁃∙·\\-–—*❖◆◇♦➢➤►▶▸❑◻◼→⇨➔"
# Korean documents also mark items with a lone "o" or "ㅇ" before Hangul text.
LETTER_BULLET = "o(?=\\s+[가-힣])|ㅇ(?=\\s)"
BULLET = re.compile(rf"^\s*(?:[{GLYPHS}]|{LETTER_BULLET}|\(?\d{{1,3}}[.)]|\(?[a-zA-Z][.)])\s+")
NUMBERED = re.compile(r"^\s*(\(?\w{1,3}[.)])\s+")
GLYPH_BULLET = re.compile(rf"^\s*(?:[{GLYPHS}]|{LETTER_BULLET})")
FURNITURE_MARGIN = 0.07
IMAGE_BLOCK = 1


def physical_table_rows(table, pt):
    """Repair an empty inset rectangle using an otherwise continuous column.

    Spreadsheet filter buttons can replace the detector's header-cell rectangle
    with their small outline. Two intact neighbours and repeated lower cells
    must agree on the missing rectangle; a populated inset is never expanded.
    """
    rows = [list(row.cells) for row in table.rows]
    columns = [Counter((cell[0], cell[2]) for row in rows if (cell := row[c]))
               for c in range(len(rows[0]))] if rows else []
    for row in rows:
        for c in range(1, len(row) - 1):
            left, cell, right = row[c - 1:c + 2]
            if not left or not right or abs(left[1] - right[1]) > .5 or abs(left[3] - right[3]) > .5:
                continue
            box = (left[2], left[1], right[0], left[3])
            if box[0] >= box[2] or columns[c][(box[0], box[2])] < 2:
                continue
            if cell and (not (box[0] <= cell[0] < cell[2] <= box[2]
                             and box[1] <= cell[1] < cell[3] <= box[3]) or cell == box):
                continue
            if cell and any(cell[0] <= ch.cx * pt.width < cell[2]
                            and cell[1] <= ch.cy * pt.height < cell[3]
                            for frag in pt.fragments for ch in frag.chars if ch.c.strip()):
                continue
            row[c] = box
    return rows


def visible_table_rows(table, pt, page, owned=None):
    """Use the detected cell boundaries, but only glyphs visible inside each cell.

    A clip or overpaint elsewhere on the page says nothing about the table's
    geometry. Raw Table.extract(), however, can bring back hidden cell values.
    """
    rotated = [line for block in read_dict(page, FLAGS)["blocks"]
               for line in block.get("lines", []) if not horizontal_line(line)]
    # Native detection can return a large cell overlapping a smaller grid.
    # Give each glyph one owner, preferring the more specific physical cell.
    physical_rows = physical_table_rows(table, pt)
    ordered_cells = sorted({rect for row in physical_rows for rect in row if rect},
                           key=lambda r: ((r[2] - r[0]) * (r[3] - r[1]), r))
    owners = {}
    for fragment in pt.fragments:
        for ch in fragment.chars:
            x, y = ch.cx * pt.width, ch.cy * pt.height
            cell = next((r for r in ordered_cells if r[0] <= x < r[2] and r[1] <= y < r[3]), None)
            if cell:
                owners[id(ch)] = cell
    if owned is not None:
        owned.update(owners)
    by_cell = defaultdict(list)
    for fragment in pt.fragments:
        groups = defaultdict(list)
        for ch in fragment.chars:
            if (cell := owners.get(id(ch))) is not None:
                groups[cell].append(ch)
        for cell, chars in groups.items():
            by_cell[cell].append(Fragment(chars))
    rows = []
    emitted = set()
    for row in physical_rows:
        cells = []
        for rect in row:
            if rect is None or rect in emitted:
                cells.append("")
                continue
            x0, y0, x1, y1 = rect
            box = normalized_bbox(x0, y0, x1, y1, pt.width, pt.height)
            if box is None:
                cells.append("")
                continue
            emitted.add(rect)
            text = build_text(by_cell[rect], pt.index, pt.width)[0]
            other = ["".join(span["text"] for span in line["spans"]).strip()
                     for line in rotated if next((r for r in ordered_cells
                         if r[0] <= (line['bbox'][0] + line['bbox'][2]) / 2 < r[2]
                         and r[1] <= (line['bbox'][1] + line['bbox'][3]) / 2 < r[3]), None) == rect]
            cells.append(" ".join(value for value in [text, *other] if value))
        rows.append(cells)
    return rows


def grounded_table(table, physical, pt, page):
    """Replace a spurious column split only when the drawn grid disproves it.

    A model sometimes separates centred headings from left-aligned values in
    the same printed column. Keep its useful borderless/merged-cell structure
    elsewhere: a different physical grid alone is not grounds for replacement.
    """
    rows = visible_table_rows(table, pt, page)
    splits = defaultdict(list)
    for row, values in zip(table.rows, rows):
        for i, (left, right) in enumerate(zip(row.cells, row.cells[1:])):
            if left and right and abs(left[2] - right[0]) < 2:
                splits[right[0]].append((bool(values[i].strip()), bool(values[i + 1].strip())))
    complementary = [x for x, pairs in splits.items()
                     if not any(a and b for a, b in pairs)
                     and min(sum(a for a, _ in pairs), sum(b for _, b in pairs)) >= 1
                     and max(sum(a for a, _ in pairs), sum(b for _, b in pairs)) >= 2]
    if not complementary:
        return table
    x0, y0, x1, y1 = table.bbox
    for candidate in physical:
        a, b, c, d = candidate.bbox
        overlap = max(0, min(c, x1) - max(a, x0)) * max(0, min(d, y1) - max(b, y0))
        if overlap < .9 * (x1 - x0) * (y1 - y0):
            continue
        native_rows = [row for row in physical_table_rows(candidate, pt)
                       if any(cell and cell[1] < y1 and cell[3] > y0 for cell in row)]
        edges = Counter(x for row in native_rows for x in {
            cell[0] for cell in row if cell and cell[3] > cell[1]})
        stable = [x for x, count in edges.items() if count >= 2 and x0 + 2 < x < x1 - 2]
        if len(stable) < 2 or not any(all(abs(x - edge) > 2 for edge in stable) for x in complementary):
            continue
        # Only replace the existing table area. A larger drawn grid may also
        # enclose an independent neighbouring note or another table.
        def clipped(cell):
            if cell is None:
                return None
            rect = (max(x0, cell[0]), max(y0, cell[1]), min(x1, cell[2]), min(y1, cell[3]))
            return rect if rect[0] < rect[2] and rect[1] < rect[3] else None
        native_rows = [[clipped(cell) for cell in row] for row in native_rows]
        keep = [i for i in range(len(native_rows[0])) if any(row[i] for row in native_rows)]
        native_rows = [[row[i] for i in keep] for row in native_rows if any(row)]
        proposal = SimpleNamespace(rows=[SimpleNamespace(cells=row) for row in native_rows],
                                   bbox=table.bbox)
        # Geometry must not erase a value merely because another grid is neater.
        before, after = set(), set()
        visible_table_rows(table, pt, page, before)
        visible_table_rows(proposal, pt, page, after)
        ink = {id(ch) for f in pt.fragments for ch in f.chars if ch.c.strip()}
        if before & ink <= after:
            return proposal
    return table


def _stack_vertical_glyphs(lines):
    """Join vertically written text that PyMuPDF reports one glyph per line.

    Chart axis titles and side labels in CJK documents are set top to bottom; each
    glyph arrives as its own short non-horizontal line. Consecutive short lines in the
    same direction, in one column and touching vertically, form one label. Text in
    scripts without word spaces is joined directly; other glyphs are space-separated.
    """
    import unicodedata

    def short(text):
        return len(text) <= 2

    def spaced(a, b):
        wide = lambda ch: unicodedata.east_asian_width(ch) in ("W", "F")
        return not (wide(a[-1]) and wide(b[0]))

    out = []
    for box, text, direction in lines:
        if out and short(text) and out[-1][3]:
            last, joined, last_direction, _ = out[-1]
            height = min(box.height, last.height)
            overlap = min(box.x1, last.x1) - max(box.x0, last.x0)
            if (direction == last_direction and overlap >= .5 * min(box.width, last.width)
                    and -.25 * height <= box.y0 - last.y1 <= .3 * height):
                out[-1] = (last | box, joined + (" " if spaced(joined, text) else "") + text, direction, True)
                continue
        out.append((box, text, direction, short(text)))
    return [(box, text, direction) for box, text, direction, _ in out]


def parse(path: Path, *, file_meta: dict, progress: ProgressFn):
    pages, blocks = extract(path, progress=progress, physical_tables=file_meta.get("sourceFormat") in SHEET_FORMATS)
    meta = {"pageCount": len(pages), **file_meta, "parser": "pymupdf", "parserVersion": version("pymupdf")}
    return finish(meta, pages, blocks)


def open_pdf(path: Path):
    import pymupdf

    try:
        pdf = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001 - PyMuPDF raises several types for broken files
        raise ParseError("corrupt_pdf", "The PDF could not be opened.") from exc
    if pdf.needs_pass:
        raise ParseError("encrypted_pdf", "The PDF is password-protected.")
    return pdf


def list_marker(text: str) -> tuple[int, str | None] | None:
    """Length of a leading bullet or number and the number to keep (None for bullet glyphs)."""
    m = BULLET.match(text)
    if not m:
        return None
    numbered = NUMBERED.match(text)
    return m.end(), (numbered.group(1) if numbered and not GLYPH_BULLET.match(text) else None)


def extract(path: Path, *, progress: ProgressFn, physical_tables: bool = False) -> tuple[list, list]:
    import pymupdf

    pdf = open_pdf(path)
    texts = [read_page(page, i) for i, page in enumerate(pdf)]

    # Body font size: the most common size across the document, weighted by characters.
    size_weights: dict[float, int] = {}
    for pt in texts:
        for frag in pt.fragments:
            for ch in frag.chars:
                if ch.c != " ":
                    key = round(ch.size, 1)
                    size_weights[key] = size_weights.get(key, 0) + 1
    body_size = max(size_weights, key=size_weights.get) if size_weights else 10.0
    heading_sizes = sorted({s for s in size_weights if s >= body_size * 1.15}, reverse=True)

    pages, blocks = [], []
    total = pdf.page_count
    for page_no, page in enumerate(pdf):
        width, height = page.rect.width, page.rect.height
        pages.append({"index": page_no, "width": width, "height": height})
        pt = texts[page_no]
        matrix = page.rotation_matrix

        def region(rect) -> list[dict]:
            r = pymupdf.Rect(rect) * matrix
            box = normalized_bbox(r.x0, r.y0, r.x1, r.y1, width, height)
            return [{"pageIndex": page_no, "bbox": box.model_dump()}] if box else []

        tables = []
        table_chars = set()
        table_cells = []
        try:
            if page.rotation:
                # Table detection is unreliable on rotated pages; their text still becomes blocks.
                raise ValueError("rotated page")
            detected = page.find_tables().tables
            physical = page.find_tables(use_layout=False).tables if physical_tables and detected else []
            for t in detected:
                if physical:
                    t = grounded_table(t, physical, pt, page)
                owned = set()
                rows = visible_table_rows(t, pt, page, owned)
                if not any(cell.strip() for row in rows for cell in row):
                    continue
                md = table_markdown([[c or "" for c in row] for row in rows])
                if md:
                    tables.append((pymupdf.Rect(t.bbox), md))
                    table_chars.update(owned)
                    table_cells.extend(pymupdf.Rect(cell) for row in physical_table_rows(t, pt)
                                       for cell in row if cell)
        except Exception:  # noqa: BLE001 - table detection is best effort
            tables = []
            table_chars.clear()
            table_cells.clear()

        by_block: dict[int, list[Fragment]] = {}
        for frag in pt.fragments:
            by_block.setdefault(frag.block, []).append(frag)
        entries: list[tuple[str, dict]] = []
        placed: set[int] = set()
        raw_text = None
        rotated_seen = {}
        for block_no, (block_type_no, bbox) in enumerate(pt.blocks):
            rect = pymupdf.Rect(bbox)
            inside = next((i for i, (tr, _) in enumerate(tables) if tr.contains(rect.tl + (rect.br - rect.tl) * 0.5)), None)
            if inside is not None:
                if inside not in placed:
                    placed.add(inside)
                    entries.append(("table", {"rect": tables[inside][0], "text": tables[inside][1]}))
                if block_type_no == IMAGE_BLOCK:
                    continue
            if block_type_no == IMAGE_BLOCK:
                if rect.width * rect.height >= 0.01 * width * height:
                    entries.append(("picture", {"rect": rect, "text": ""}))
                continue
            # Use the same extraction flags and orientation rule as the text layer. An
            # absent fragment may have been intentionally removed as an overprint; only
            # genuinely non-horizontal lines need an atomic fallback.
            if raw_text is None:
                raw_text = read_dict(page, FLAGS)["blocks"]
            raw = raw_text[block_no] if block_no < len(raw_text) else {}
            vertical = []
            for line in raw.get("lines", []):
                if horizontal_line(line):
                    continue
                text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                if text:
                    box = pymupdf.Rect(line['bbox'])
                    if any(cell.contains(box.tl + (box.br - box.tl) * .5) for cell in table_cells):
                        continue
                    vertical.append((box, text, tuple(line.get('dir', ()))))
            for box, text, direction in _stack_vertical_glyphs(vertical):
                identity = (text, direction)
                previous = rotated_seen.setdefault(identity, [])
                if any((box & old).get_area() >= .8 * max(box.get_area(), old.get_area()) for old in previous):
                    continue
                previous.append(box)
                entries.append(("text", {"rect": box, "text": text,
                    "lines": None, "unavailable": True, "rotated": True,
                    "size": body_size, "bold": False, "nlines": 1}))
            frags = by_block.get(block_no)
            if not frags:
                continue
            if tables:
                # A PDF text block can contain both a table column and its
                # neighbouring caption. Consume only actual cell-area glyphs,
                # never the whole block just because its centre lies in a table.
                frags = [Fragment(chars, block=frag.block) for frag in frags if (chars := [
                    ch for ch in frag.chars if id(ch) not in table_chars])]
                if not frags:
                    continue
                rect = pymupdf.Rect(min(f.x0 for f in frags) * pt.width, min(f.y0 for f in frags) * pt.height,
                                    max(f.x1 for f in frags) * pt.width, max(f.y1 for f in frags) * pt.height)
            text, lines, chars = build_text(frags, page_no, pt.width)
            if not text:
                continue
            size, bold = dominant_size(chars)
            entries.append(("text", {"rect": rect, "text": text, "lines": None if pt.rotated else lines,
                                     "size": size, "bold": bold, "nlines": len(lines), "unavailable": pt.rotated, "rotated": pt.rotated}))
        for i, (tr, md) in enumerate(tables):
            if i not in placed:
                entries.append(("table", {"rect": tr, "text": md}))

        for kind, e in entries:
            text, level, marker, lines = e["text"], None, None, e.get("lines")
            shown = pymupdf.Rect(e["rect"]) * matrix
            if kind != "text":
                block_type, lines = kind, None
            elif (shown.y1 < height * FURNITURE_MARGIN or shown.y0 > height * (1 - FURNITURE_MARGIN)) and e["nlines"] <= 2 and len(text) < 80:
                block_type = "page_header" if shown.y1 < height / 2 else "page_footer"
            elif len(text) < 120 and e["size"] >= body_size * 1.15:
                size_key = round(e["size"], 1)
                if page_no == 0 and heading_sizes and size_key == heading_sizes[0] and e["size"] >= body_size * 1.6:
                    block_type, level = "title", 0
                else:
                    level = heading_sizes.index(size_key) + 1 if size_key in heading_sizes else len(heading_sizes) + 1
                    block_type = "section_header"
            elif CAPTION.match(text) and len(text) < 300:
                block_type = "caption"
            elif found := list_marker(text):
                block_type, (cut, marker) = "list_item", found
                if lines is not None:
                    text, lines = strip_prefix(text, lines, cut)
                else:
                    text = text[cut:].lstrip()
            elif e["bold"] and e["nlines"] <= 2 and len(text) < 100:
                block_type, level = "section_header", len(heading_sizes) + 1
            else:
                block_type = "paragraph"
            blocks.append(
                {
                    "type": block_type,
                    "text": text,
                    "level": level,
                    "regions": region(e["rect"]),
                    "lines": lines if block_type in SPLITTABLE else None,
                    "lineGeometry": "unavailable" if e.get("unavailable") else ("exact" if lines else None),
                    "source": {"parser": "pymupdf", "ref": f"page{page_no + 1}"},
                    "_marker": marker,
                    "_rotated_text": bool(e.get("rotated")),
                }
            )
        progress(page_no + 1, total)
    return pages, blocks
