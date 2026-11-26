"""Separate physically printed header bands when one grid contains different tables.

This stage uses the PDF's merged-cell boundaries and source-row type transitions.
It never invents a heading on an empty continuation page or promotes an all-text
record merely because it is the first row. All output labels are printed cells;
source strings constrain roles and columns, not the emitted data.
"""
import re
from collections import Counter, defaultdict
from statistics import median
from types import SimpleNamespace

import pymupdf

from .carried_headers import _cells, _clipped, _merged_spans, _row_mapping
from .source_tables import _cell_text_key as key, table_markdown
from .textlayer import Fragment, build_text, read_page


def _numeric(value):
    value = value.strip()
    return bool(re.fullmatch(r'[+\-\u2212()$\u20ac\u00a3\u00a5\u20a9\d.,%/\s]+', value)
                and any(c.isdigit() for c in value))


def _labels(row):
    filled = [c for c in row if c.strip()]
    return len(filled) >= 2 and all(any(ch.isalpha() for ch in c) for c in filled)


def _candidate_rows(row, tables, index):
    values = [key(c) for c in row]
    hits = Counter(identity for value in set(values) if value for identity in index.get(value, ()))
    candidates = [identity for identity, count in hits.items() if count >= 2]
    if len(candidates) > 256:
        return []
    matches = []
    for ti, ri in candidates:
        source = [key(c) for c in tables[ti][ri]]
        found = _row_mapping(values, source)
        if not found or len(found[0]) != sum(bool(v) for v in values):
            continue
        offsets = {c - m for m, c in found[0].items()}
        if len(offsets) != 1:
            continue
        start = offsets.pop()
        columns = tuple(range(start, start + len(row)))
        if start >= 0 and columns[-1] < len(source):
            matches.append((ti, ri, columns))
    return matches


def _stacked_role(table, ri, columns, printed_below):
    if ri + 2 >= len(table):
        return False
    above, below, data = table[ri:ri + 3]
    if not _labels(below):
        return False
    for m, c in enumerate(columns):
        value = key(printed_below[m])
        if value and not (value == key(below[c]) or _clipped(value, key(below[c]))):
            return False
    spans = _merged_spans(above)
    merged = {c for span in spans for c in span}
    filled_groups = [span for span in spans if any(c < len(below) and below[c].strip()
                                                 and not above[c].strip() for c in span)]
    if len(filled_groups) < 2:
        return False
    # An unprinted ordinary column still participates: a person's/site's value
    # under that column proves this is a record, not a lower header row.
    for c, label in enumerate(below):
        if c not in merged and label.strip() and key(label) != key(above[c]):
            if c >= len(data) or not _numeric(data[c]):
                return False
    return sum(c < len(data) and bool(below[c].strip()) and _numeric(data[c]) for c in columns) >= 2


def _section_role(table, ri, columns):
    if ri + 2 >= len(table) or not _labels(table[ri]):
        return False
    # A change to a new column schema needs several subsequent source records
    # with independent numeric fields below the printed textual labels.
    later = [r for r in table[ri + 1:] if any(c.strip() for c in r)][:2]
    return len(later) == 2 and sum(bool(table[ri][c].strip()) and all(_numeric(r[c]) for r in later)
                                  for c in columns) >= 2


def _bounds(cells):
    return min(c[0] for c in cells), min(c[1] for c in cells), max(c[2] for c in cells), max(c[3] for c in cells)


def _partition(row):
    return tuple(cell is not None for cell in row.cells)


def _has_printed_merges(above, below):
    return sum(sum(cell is not None and outer[0] <= (cell[0] + cell[2]) / 2 < outer[2]
                   for cell in below.cells) >= 2 for outer in above.cells if outer is not None) >= 1


def _close_trailing_cells(grid):
    """Complete an open last column only from its other printed cell boundaries.

    A clipped outer rule can disappear on some rows. Interior missing cells may
    be merged, so they are never filled. The completed geometry is accepted only
    if every existing markdown cell matches its actual PDF glyphs afterwards.
    """
    ends = [row.cells[-1] for row in grid.rows if row.cells[-1] is not None]
    if not ends or any(abs(cell[0] - ends[0][0]) > .5 or abs(cell[2] - ends[0][2]) > .5 for cell in ends):
        return grid
    left, right = ends[0][0], ends[0][2]
    rows = []
    for row in grid.rows:
        cells = list(row.cells)
        prior = [cell for cell in cells[:-1] if cell is not None]
        if cells[-1] is None and prior and abs(prior[-1][2] - left) < .5:
            top, bottom = prior[-1][1], prior[-1][3]
            if all(abs(cell[1] - top) < .5 and abs(cell[3] - bottom) < .5 for cell in prior):
                cells[-1] = (left, top, right, bottom)
        rows.append(SimpleNamespace(cells=cells))
    return SimpleNamespace(rows=rows)


def _physical_rows(grid, native):
    values = []
    cache = {}
    seen = set()
    for row in grid.rows:
        out = []
        for box in row.cells:
            if box is None or box in seen:
                out.append('')
                continue
            seen.add(box)
            if box not in cache:
                parts = [Fragment(chars) for f in native.fragments if (chars := [ch for ch in f.chars
                         if box[0] <= ch.cx * native.width < box[2]
                         and box[1] <= ch.cy * native.height < box[3]])]
                cache[box] = build_text(parts, native.index, native.width)[0] if parts else ''
            out.append(cache[box])
        values.append(out)
    return values


def restore_printed_header_bands(parsed, tables, pdf, native_cache=None):
    if not tables:
        return 0
    index = defaultdict(set)
    for ti, table in enumerate(tables):
        for ri, row in enumerate(table):
            for value in set(key(c) for c in row if c.strip()):
                index[value].add((ti, ri))
    natives = native_cache if native_cache is not None else {}
    replacements = {}
    with pymupdf.open(pdf) as document:
        page_grids = {}
        for block in parsed.blocks:
            if block.type != 'table' or len(block.regions) != 1:
                continue
            lines = block.text.splitlines()
            if len(lines) < 6 or not re.fullmatch(r'[\s|:\-]+', lines[1]):
                continue
            head = _cells(lines[0])
            if any(head):
                continue
            rows = [_cells(line) for line in lines[2:]]
            if any(len(row) != len(head) for row in rows) or not _labels(rows[0]) or not _labels(rows[1]):
                continue
            page_index = block.regions[0].page_index
            page = document[page_index]
            if page.rotation:
                continue
            if page_index not in natives:
                natives[page_index] = read_page(page, page_index)
            native = natives[page_index]
            if page_index not in page_grids:
                page_grids[page_index] = [_close_trailing_cells(g) for g in page.find_tables(strategy='lines_strict', use_layout=False).tables]
            wanted = [[key(c) for c in row] for row in rows]
            matching = [grid for grid in page_grids[page_index]
                        if len(grid.rows) == len(rows) and len(grid.rows[0].cells) == len(head)
                        and [[key(c) for c in row] for row in _physical_rows(grid, native)] == wanted]
            if len(matching) != 1:
                continue
            grid = matching[0]
            if not _has_printed_merges(grid.rows[0], grid.rows[1]):
                continue
            candidates = _candidate_rows(rows[0], tables, index)
            if not candidates or not all(_stacked_role(tables[ti], ri, cols, rows[1]) for ti, ri, cols in candidates):
                continue
            bands = {0: 2}
            valid = True
            for i in range(2, len(rows)):
                if not _labels(rows[i]):
                    continue
                candidates = _candidate_rows(rows[i], tables, index)
                following = grid.rows[i + 1:i + 3]
                height = max(c[3] for c in grid.rows[i].cells if c) - min(c[1] for c in grid.rows[i].cells if c)
                heights = [max(c[3] for c in row.cells if c) - min(c[1] for c in row.cells if c) for row in following]
                if (len(following) != 2 or not candidates
                        or not all(_section_role(tables[ti], ri, cols) for ti, ri, cols in candidates)
                        or height < 1.5 * median(heights)
                        or _partition(grid.rows[i]) == _partition(grid.rows[i - 1])
                        or any(_partition(row) != _partition(grid.rows[i]) for row in following)):
                    valid = False
                    break
                bands[i] = 1
            if not valid:
                continue
            starts = list(bands)
            output = []
            for n, start in enumerate(starts):
                end = starts[n + 1] if n + 1 < len(starts) else len(rows)
                count = bands[start]
                if start + count >= end:
                    valid = False
                    break
                labels = [' '.join(rows[r][c] for r in range(start, start + count) if rows[r][c])
                          for c in range(len(head))]
                text = '\n'.join([*table_markdown([labels]).splitlines(), *lines[2 + start + count:2 + end]])
                cells = [c for row in grid.rows[start:end] for c in row.cells if c]
                box = _bounds(cells)
                region = block.regions[0].model_copy(update={'bbox': block.regions[0].bbox.model_copy(update={
                    'x0': box[0] / native.width, 'y0': box[1] / native.height,
                    'x1': box[2] / native.width, 'y1': box[3] / native.height})})
                output.append(block.model_copy(update={'id': f'{block.id}-section-{n}', 'text': text, 'markdown': text,
                    'regions': [region], 'lines': None, 'line_geometry': 'unavailable',
                    'source': block.source.model_copy(update={'ref': (block.source.ref or '') + f';printed-header-band:{start}:{count}'})
                              if block.source else None}))
            if valid:
                replacements[block.id] = output
    if replacements:
        parsed.blocks = [child for block in parsed.blocks for child in replacements.get(block.id, [block])]
        for i, block in enumerate(parsed.blocks):
            block.reading_order = i
    return len(replacements)
