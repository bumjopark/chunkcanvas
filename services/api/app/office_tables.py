"""Tables of office documents as grids of cell text, read from the original file.

The layout parser reads tables off the converted PDF, where a sheet without grid lines, merged
cells and multi-line cells come out with columns run together or rows split. LibreOffice's HTML
export keeps each table of a text document and each sheet of a workbook (with values formatted as
shown), and presentations keep theirs in the PowerPoint file; the parse job puts these rows back
(parsers/source_tables.py).
"""
import re
import unicodedata
from pathlib import Path

# Beyond this the export is too large to be worth reading (it is read as a stream, row by row).
MAX_HTML_BYTES = 400_000_000
MAX_ROWS = 5000
MAX_CELLS = 300_000
# Symbol-font characters (form check boxes and the like) with no meaning outside their font.
_SYMBOLS = re.compile("[-]")


def clean_cell(text: str) -> str:
    # Zero-width spaces (where a long figure may wrap on the page written here) and soft hyphens
    # are no part of the cell's text.
    text = _SYMBOLS.sub("", text).replace("\u200b", "").replace("\u00ad", "")
    text = "".join(c for c in text if unicodedata.category(c) != "Cc" or c in "\t\n")
    return " ".join(text.split())


def fill_grid(rows: list[list[tuple[int, int, str]]]) -> list[list[str]]:
    """Rows of (colspan, rowspan, text) cells as a rectangular grid. A label spanning rows applies
    to each of them; text spanning columns is said once."""
    filled: dict[tuple[int, int], str] = {}
    origins: dict[tuple[int, int], tuple[int, int]] = {}
    for r, row in enumerate(rows):
        c = 0
        for colspan, rowspan, text in row:
            while (r, c) in filled:
                c += 1
            for dr in range(max(1, rowspan)):
                for dc in range(max(1, colspan)):
                    filled[(r + dr, c + dc)] = text if dc == 0 else ""
                    origins[(r + dr, c + dc)] = (r, c)
            c += max(1, colspan)
    if not filled:
        return []
    height, width = max(r for r, _ in filled) + 1, max(c for _, c in filled) + 1
    result, previous = [], None
    for r in range(height):
        row = [filled.get((r, c), "") for c in range(width)]
        source_cells = tuple(origins.get((r, c)) if row[c] else None for c in range(width))
        # Collapse only a row made entirely from the same spanning source cells.
        # Independent rows with identical text have different origins and survive.
        if not result or source_cells != previous or row != result[-1]:
            result.append(row)
        previous = source_cells
    return result


def _span(cell, name: str) -> int:
    try:
        return max(1, min(int(cell.get(name) or 1), 1000))
    except ValueError:
        return 1


# Elements that start a new line; inline ones (font, span, b) join their text to their neighbours.
_BREAKS = {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th", "ul", "ol", "table"}


def _own_text(cell) -> str:
    """A cell's text, with that of a table nested in it (left as its text when it was read: see
    html_tables) and without the note attached to it (a spreadsheet exports it as <comment>; it is
    not printed)."""
    parts = [cell.text or ""]
    for child in cell:
        # Comments have a function for a tag.
        if isinstance(child.tag, str) and child.tag != "comment":
            gap = " " if child.tag in _BREAKS else ""
            parts.append(gap + _own_text(child) + gap)
        parts.append(child.tail or "")
    return "".join(parts)


def html_tables(path: Path) -> list[list[list[str]]]:
    """The tables of an HTML export in document order. A workbook's sheet can make an export of
    tens of megabytes: rows are read one at a time and let go, and a table keeps MAX_ROWS."""
    from lxml import etree

    if path.stat().st_size > MAX_HTML_BYTES:
        return []
    grids: list[list[list[str]] | None] = []
    # The open tables, innermost last: (its place in `grids`, its rows).
    open_tables: list[tuple[int, list]] = []
    cells = 0
    try:
        for event, el in etree.iterparse(str(path), events=("start", "end"), html=True, recover=True, huge_tree=True):
            tag = el.tag.lower() if isinstance(el.tag, str) else ""
            if event == "start":
                if tag == "table":
                    grids.append(None)
                    open_tables.append((len(grids) - 1, []))
                elif tag == "tr" and open_tables:
                    open_tables[-1][1].append([])
                continue
            if tag in ("td", "th") and open_tables and open_tables[-1][1]:
                # Tables nested in the cell have ended already: this cell is the innermost table's.
                open_tables[-1][1][-1].append((_span(el, "colspan"), _span(el, "rowspan"), clean_cell(_own_text(el))))
            elif tag == "tr":
                if open_tables and len(open_tables[-1][1]) > MAX_ROWS:
                    open_tables[-1][1].pop()
                # Rows read are let go.
                el.clear(keep_tail=True)
                while el.getprevious() is not None:
                    del el.getparent()[0]
            elif tag == "table" and open_tables:
                place, rows = open_tables.pop()
                grid = fill_grid(rows)
                if open_tables:
                    # A table nested in a cell is a table of its own and part of that cell's text:
                    # its rows are let go already, so its text stays in its place.
                    el.clear(keep_tail=True)
                    el.text = " ".join(" ".join(dict.fromkeys(c for c in row if c)) for row in grid)
                if any(any(row) for row in grid):
                    grids[place] = grid
                    cells += sum(len(row) for row in grid)
                    if cells > MAX_CELLS:
                        break
    except etree.LxmlError:
        pass
    return [g for g in grids if g]


def pptx_tables(path: Path) -> list[list[list[str]]]:
    from pptx import Presentation

    out = []

    def shapes(group):
        for shape in group:
            if shape.shape_type == 6:  # a group: its shapes may hold tables
                yield from shapes(shape.shapes)
            else:
                yield shape

    for slide in Presentation(str(path)).slides:
        for shape in shapes(slide.shapes):
            if not getattr(shape, "has_table", False) or not shape.has_table:
                continue
            table = shape.table
            rows = []
            for r, row in enumerate(table.rows):
                cells = []
                for c, cell in enumerate(row.cells):
                    if cell.is_spanned:
                        continue
                    span = (cell.span_width, cell.span_height) if cell.is_merge_origin else (1, 1)
                    cells.append((*span, clean_cell(cell.text)))
                rows.append(cells)
            grid = fill_grid(rows)
            if any(any(row) for row in grid):
                out.append(grid)
    return out
