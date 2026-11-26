"""Recover printed spreadsheet grids without importing unseen source rows.

Only complete, unambiguous source-cell matches normalize whitespace. Grid geometry
and all other values come from the PDF. This is a fallback after source restoration.
"""
from collections import Counter, defaultdict
from bisect import bisect_right
from itertools import groupby
from types import SimpleNamespace
import re
from statistics import median

import pymupdf

from pathlib import Path

from ..models import BBox, BlockRegion, ParsedDocument, TextLine
from .base import table_markdown
from .textlayer import Fragment, build_text, horizontal_line, read_page
from .source_tables import CELL_TYPES, _cell_text_key, _preserves_printed_text, _header_row


def _remaining_fragments(records, consumed):
    """Undo tentative cell cuts for text that was not actually made into a table.

    A cell match can split one PDF line into overlapping font boxes. Reflowing
    those pieces as separate lines changes word order even if no glyph is lost.
    Retain the original line identity and character order until a grid is used.
    """
    remaining = {}
    for cell, part, origin in records:
        if cell not in consumed:
            if origin not in remaining:
                remaining[origin] = Fragment([], block=part.block)
            remaining[origin].chars.extend(part.chars)
    return list(remaining.values())


def _inside(x, y, box):
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _overlap(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _overprinted(a, b):
    area = max((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    overlap = max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(0., min(a[3], b[3]) - max(a[1], b[1]))
    return area > 0 and overlap >= .8 * area


def _boxes(block, p):
    return [(r.bbox.x0, r.bbox.y0, r.bbox.x1, r.bbox.y1)
            for r in block.regions if r.page_index == p]


def _region(p, box):
    return BlockRegion(page_index=p, bbox=BBox(x0=max(0., box[0]), y0=max(0., box[1]),
                                             x1=min(1., box[2]), y1=min(1., box[3])))


def _bounds(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _represented_overlap(native, page, p, regions, blocks):
    """Every glyph excluded by an untouched box must already have an owner.

    Coarse table regions may overlap legitimate annotations. Check their actual
    printed text and occurrence counts rather than rejecting all box overlaps.
    Multi-page text has no reliable per-page budget here, so stays unresolved.
    """
    rotated = None
    for block in blocks:
        boxes = _boxes(block, p)
        if not any(_overlap(box, area) for box in boxes for area in regions):
            continue
        if {r.page_index for r in block.regions} != {p}:
            return False
        parts = []
        for fragment in native.fragments:
            chars = [ch for ch in fragment.chars if any(_inside(ch.cx, ch.cy, box) for box in boxes)
                     and any(_inside(ch.cx, ch.cy, area) for area in regions)]
            if chars:
                parts.append(build_text([Fragment(chars, block=fragment.block)], p, native.width)[0])
        if rotated is None:
            flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
            rotated = [line for raw in page.get_text('dict', flags=flags)['blocks']
                       for line in raw.get('lines', []) if not horizontal_line(line)]
        for line in rotated:
            x0, y0, x1, y1 = line['bbox']
            x, y = (x0 + x1) / (2 * native.width), (y0 + y1) / (2 * native.height)
            if any(_inside(x, y, box) for box in boxes) and any(_inside(x, y, area) for area in regions):
                parts.append(' '.join(span['text'] for span in line['spans']))
        excluded = ' '.join(parts)
        symbols = lambda text: Counter(ch for ch in _cell_text_key(text) if not ch.isalnum())
        if symbols(excluded) - symbols(block.text) or not _preserves_printed_text(excluded, block.text):
            return False
    return True


def _observed_headers(parsed, tables):
    """Reuse an already extracted header only when its cells match one source row.

    This supplies column context to a following printed fragment. It does not
    authorize data restoration, and an all-numeric first record is never a header.
    """
    observed = []
    for block in parsed.blocks:
        if block.type != 'table' or not block.text.lstrip().startswith('|'):
            continue
        cells = [v.strip().replace('\\|', '|') for v in re.split(r'(?<!\\)\|', block.text.splitlines()[0].strip().strip('|'))]
        keys = tuple(_cell_text_key(v) for v in cells if v)
        if len(keys) >= 3 and sum(any(ch.isalpha() for ch in key) for key in keys) >= max(2, len(keys) / 2):
            observed.append(keys)
    found = defaultdict(list)
    for ti, table in enumerate(tables):
        for ri, row in enumerate(table):
            columns = tuple(i for i, value in enumerate(row) if value.strip())
            keys = tuple(_cell_text_key(row[i]) for i in columns)
            for header in observed:
                for offset in range(len(keys) - len(header) + 1):
                    if keys[offset:offset + len(header)] != header:
                        continue
                    selected = columns[offset:offset + len(header)]
                    first = _header_row(table, selected)
                    if first is None or tuple(_cell_text_key(table[first][c]) for c in selected) != header:
                        continue
                    found[ti].append((ri, selected, tuple(row[c] for c in selected),
                                      tuple(_cell_text_key(value) for value in row)))
    result = {}
    for ti, matches in found.items():
        # A physically printed subset can name its own source columns even
        # when other columns are on another page. Repeated identical header
        # rows are the same context; differing source rows remain ambiguous.
        if len({keys for _, _, _, keys in matches}) != 1:
            continue
        names = {c: value for _, columns, values, _ in matches for c, value in zip(columns, values)}
        columns = tuple(sorted(names))
        result[ti] = (min(ri for ri, _, _, _ in matches), columns, [names[c] for c in columns])
    return result


def _complete_source_table(block, tables, headers, cache):
    """Preserve an existing table whose complete cell tuples match its source."""
    if block.type != 'table' or not re.search(r'(?m)^\|[ |:\-]+\|\s*$', block.text):
        return False
    rows = [tuple(_cell_text_key(value.strip().replace('\\|', '|'))
                  for value in re.split(r'(?<!\\)\|', line.strip().strip('|')))
            for line in block.text.strip().splitlines()
            if line.strip().startswith('|') and not re.fullmatch(r'[\s|:\-]+', line)]
    if len(rows) < 3 or len(rows[0]) < 3 or not all(rows[0]):
        return False
    for ti, (_, columns, names) in headers.items():
        keys = tuple(_cell_text_key(value) for value in names)
        for offset in range(len(keys) - len(rows[0]) + 1):
            if keys[offset:offset + len(rows[0])] != rows[0]:
                continue
            identity = ti, tuple(columns[offset:offset + len(rows[0])])
            if identity not in cache:
                cache[identity] = {tuple(_cell_text_key(row[c]) for c in identity[1]) for row in tables[ti]}
            if all(row in cache[identity] for row in rows):
                return True
    return False


def _restore_record_header(block, tables, headers, cache):
    """Keep a complete native table intact when its first row is actually data.

    Every cell tuple and its occurrence count must match one contiguous source
    projection. This changes column context only, preserving all native records;
    a partial reconstruction must not scatter their clipped neighbours as prose.
    """
    if block.type != 'table' or not re.search(r'(?m)^\|[ |:\-]+\|\s*$', block.text):
        return False
    lines = block.text.strip().splitlines()
    if len(lines) < 2 or not re.fullmatch(r'[\s|:\-]+', lines[1]):
        return False
    rows = [tuple(_cell_text_key(value.strip().replace('\\|', '|'))
                  for value in re.split(r'(?<!\\)\|', line.strip().strip('|')))
            for i, line in enumerate(lines) if i != 1 and line.strip().startswith('|')]
    if len(rows) < 3 or len(rows[0]) < 3 or not all(rows[0]) or any(len(row) != len(rows[0]) for row in rows):
        return False
    proposals = []
    width = len(rows[0])
    wanted = Counter(rows)
    for ti, table in enumerate(tables):
        if not table:
            continue
        for start in range(len(table[0]) - width + 1):
            identity = ti, start, width
            if identity not in cache:
                columns = tuple(range(start, start + width))
                projected = [tuple(_cell_text_key(row[c]) for c in columns) for row in table]
                header = _header_row(table, columns)
                first = next((row for row in projected if any(row)), ())
                positions = defaultdict(list)
                for ri, row in enumerate(projected):
                    positions[row].append(ri)
                cache[identity] = (columns, projected[header] if header is not None else first,
                                   Counter(projected[(header + 1) if header is not None else 0:]), positions)
            columns, heading, counts, positions = cache[identity]
            if rows[0] == heading or wanted - counts:
                continue
            proposals.append((ti, columns, positions[rows[0]]))
    if len(proposals) != 1:
        return False
    ti, columns, positions = proposals[0]
    observed = headers.get(ti)
    carry = observed and all(ri > observed[0] and _header_context_reaches(
        tables[ti], columns, observed[0], ri) for ri in positions)
    names = dict(zip(observed[1], observed[2])) if carry else {}
    labels = [names.get(c, f'Column {i + 1}') for i, c in enumerate(columns)]
    # Keep the original Markdown data rows byte-for-byte. The previous first
    # row is moved below a fresh separator and occurs exactly once as data.
    prefix = table_markdown([labels]).splitlines()
    text = '\n'.join([*prefix, lines[0], *lines[2:]])
    block.text = block.markdown = text
    block.lines = None
    block.line_geometry = 'unavailable'
    return True


def _header_context_reaches(table, columns, header_id, row_id, header_end=None):
    """A new labelled section after a source gap ends the earlier context.

    Text-only records are ordinary data. Their spelling alone cannot prove a
    section change; a blank source row must separate the later labels first.
    """
    keys = tuple(_cell_text_key(table[header_id][c]) for c in columns)
    after_gap = False
    for row in table[header_end or header_id + 1:row_id]:
        if not any(value.strip() for value in row):
            after_gap = True
            continue
        values = [row[c] for c in columns if row[c].strip()]
        if (after_gap and len(values) >= max(2, len(columns) / 2)
                and all(any(ch.isalpha() for ch in value) for value in values)
                and tuple(_cell_text_key(row[c]) for c in columns) != keys):
            return False
        if values:
            after_gap = False
    return True


def _attach_printed_headers(grids, row_grids, candidates, page, page_index, tables, native, headers):
    """Carry a visible stacked header to aligned fragments of the same table.

    Its printed cells must exactly match consecutive source header rows. Source
    values only establish column identity; the labels emitted are visible text.
    """
    if not row_grids:
        return
    physical = []
    cell_text = {}
    fragment_slots = defaultdict(set)
    for i, fragment in enumerate(native.fragments):
        for slot in range(int(fragment.y0 * 128), int(fragment.y1 * 128) + 1):
            fragment_slots[slot].add(i)
    for grid in grids:
        for row in grid.rows:
            cells = row.cells
            if len(cells) < 3 or None in cells or len(set(cells)) != len(cells):
                continue
            for cell in cells:
                if cell not in cell_text:
                    box = tuple(v / (native.width if i % 2 == 0 else native.height)
                                for i, v in enumerate(cell))
                    nearby = set().union(*(fragment_slots[slot] for slot in range(
                        int(box[1] * 128), int(box[3] * 128) + 1)))
                    parts = [Fragment(chars) for i in sorted(nearby) if (chars := [
                        ch for ch in native.fragments[i].chars if _inside(ch.cx, ch.cy, box)])]
                    cell_text[cell] = build_text(parts, native.index, native.width)[0]
            labels = [cell_text[c] for c in cells]
            if not all(label.strip() and any(ch.isalpha() for ch in label) for label in labels):
                continue
            physical.append((cells, labels))
    owners = [_boxes(b, page_index) for b in candidates if b.type == 'table']
    for grid in row_grids:
        ti, columns = grid.source_identity
        first = _header_row(tables[ti], columns)
        if first is None or first >= grid.source_min_row:
            continue
        proposals = {}
        for cells, labels in physical:
            if len(cells) < max(3, len(columns) / 2):
                continue
            # A printed table can have a narrow unlabelled extra column. Keep
            # every observed heading in its own x band without requiring that
            # unrelated column to have a heading too. Every labelled band must
            # identify exactly one source-grounded column across all records.
            mapped = []
            for head in cells:
                matches = [c for c in range(len(columns)) if all(
                    row.cells[c] is not None
                    and head[0] <= (row.cells[c][0] + row.cells[c][2]) / 2 <= head[2]
                    and row.cells[c][1] >= head[3] for row in grid.rows)]
                if len(matches) != 1:
                    break
                mapped.append(matches[0])
            if len(mapped) != len(cells) or mapped != sorted(set(mapped)):
                continue
            source_columns = tuple(columns[c] for c in mapped)
            keys = tuple(_cell_text_key(label) for label in labels)
            end = next((end for end in range(first + 1, min(first + 4, grid.source_min_row + 1))
                        if tuple(_cell_text_key(' '.join(tables[ti][i][c] for i in range(first, end)))
                                 for c in source_columns) == keys), None)
            if end is None or not _header_context_reaches(
                    tables[ti], columns, first, grid.source_min_row, end):
                continue
            header_box = _bounds(cells)
            data_box = _bounds([c for row in grid.rows for c in row.cells if c])
            # A coarse existing table must own both pieces. Do not propagate
            # a nearby caption/another table merely because columns align.
            width, height = page.rect.width, page.rect.height
            normalize = lambda box: tuple(v / (width if i % 2 == 0 else height) for i, v in enumerate(box))
            if not any(any(_overlap(normalize(header_box), box) for box in boxes)
                       and any(_overlap(normalize(data_box), box) for box in boxes) for boxes in owners):
                continue
            names = dict(zip(mapped, labels))
            value = tuple(names.get(c, f'Column {c + 1}') for c in range(len(columns)))
            proposals[value] = (first, source_columns, labels)
        if len(proposals) == 1:
            value, context = next(iter(proposals.items()))
            grid.observed_header = list(value)
            # Carry only observed labels to later source rows. Do not import
            # the unprinted header of the extra column from the workbook.
            prior = headers.get(ti)
            names = dict(zip(context[1], context[2]))
            if prior is None:
                headers[ti] = context
            elif prior[0] == first and all(c not in names or _cell_text_key(v) == _cell_text_key(names[c])
                                          for c, v in zip(prior[1], prior[2])):
                names.update(zip(prior[1], prior[2]))
                selected = tuple(sorted(names))
                headers[ti] = (first, selected, [names[c] for c in selected])


def _keep_overflow_words(grids, indices, fragments, values, matched, native, locator):
    """Keep a source-grounded visible word in its starting physical cell.

    A cell border can cut through text overflowing into the next column. Only
    move a suffix when the same native line prints it immediately after the
    left cell, and two other source columns establish an unambiguous partition.
    Merged cells, missing characters and competing source assignments stay put.
    """
    positions = {id(ch): (i, j) for i, fragment in enumerate(native.fragments)
                 for j, ch in enumerate(fragment.chars)}
    for grid in grids:
        if getattr(grid, 'source_identity', None) is not None:
            continue
        uses = Counter(cell for row in grid.rows for cell in row.cells if cell)
        for row in grid.rows:
            cells = [indices[cell] if cell else None for cell in row.cells]
            if None in cells or len(set(cells)) != len(cells):
                continue
            keys = [_cell_text_key(values.get(cell, '')) for cell in cells]
            for column, count in locator.overflow_boundaries(keys).items():
                if any(uses[row.cells[c]] != 1 for c in (column, column + 1)):
                    continue
                left, right = cells[column:column + 2]
                left_chars = [ch for f in fragments[left] for ch in f.chars if ch.c.strip()]
                right_chars = build_text(fragments[right], native.index, native.width)[2]
                moved, size = [], 0
                for ch in right_chars:
                    size += len(_cell_text_key(ch.c))
                    moved.append(ch)
                    if size >= count:
                        break
                if size != count or not left_chars or not moved:
                    continue
                locations = [positions.get(id(ch)) for ch in [*left_chars, *moved] if ch.c.strip()]
                if None in locations or len({pos[0] for pos in locations}) != 1:
                    continue
                last = positions[id(left_chars[-1])]
                first = positions.get(id(next((ch for ch in moved if ch.c.strip()), moved[0])))
                if first != (last[0], last[1] + 1):
                    continue
                a, b = left_chars[-1], next(ch for ch in moved if ch.c.strip())
                if (b.x0 - a.x1) * native.width > .2 * max(a.size, b.size):
                    continue
                moved_ids = {id(ch) for ch in moved}
                before = fragments[left] + [Fragment([ch for ch in f.chars if id(ch) in moved_ids], f.block)
                                            for f in fragments[right] if any(id(ch) in moved_ids for ch in f.chars)]
                after = [Fragment(chars, f.block) for f in fragments[right]
                         if (chars := [ch for ch in f.chars if id(ch) not in moved_ids])]
                a = build_text(before, native.index, native.width)[0]
                b = build_text(after, native.index, native.width)[0]
                if _cell_text_key(a) != keys[column] + keys[column + 1][:count] or _cell_text_key(b) != keys[column + 1][count:]:
                    continue
                values[left], values[right] = a, b
                matched.add(left)
                if b.strip():
                    matched.add(right)
                else:
                    matched.discard(right)


def _keep_row_overflows(grids, indices, fragments, values, matched, native, locator, source):
    """Keep a wrapped native continuation with its uniquely grounded source row."""
    positions = {id(ch): (i, j) for i, f in enumerate(native.fragments) for j, ch in enumerate(f.chars)}
    for grid in grids:
        if getattr(grid, 'source_identity', None) is not None:
            continue
        uses = Counter(cell for row in grid.rows for cell in row.cells if cell)
        rows = [[indices[cell] if cell else None for cell in row.cells] for row in grid.rows]
        keys = [[_cell_text_key(values.get(cell, '')) for cell in row] for row in rows]
        moved_cells = set()
        for ri, column, count in locator.row_overflow_boundaries(keys):
            upper, lower = rows[ri][column], rows[ri + 1][column]
            boxes = [grid.rows[i].cells[column] for i in (ri, ri + 1)]
            if (upper is None or lower is None or upper == lower or {upper, lower} & moved_cells
                    or any(uses[box] != 1 for box in boxes)):
                continue
            top = build_text(fragments[upper], native.index, native.width)[2]
            bottom = build_text(fragments[lower], native.index, native.width)[2]
            # Another repair may already have changed these values.
            if any(_cell_text_key(''.join(ch.c for ch in chars)) != keys[i][column]
                   for i, chars in ((ri, top), (ri + 1, bottom))):
                continue
            moved, size = [], 0
            for ch in bottom:
                size += len(_cell_text_key(ch.c))
                moved.append(ch)
                if size >= count:
                    break
            nonblank = [ch for ch in moved if ch.c.strip()]
            if not top or not nonblank or size != count:
                continue
            left = [ch for ch in top if ch.c.strip()]
            a, b = positions.get(id(left[-1])), positions.get(id(nonblank[0]))
            if a is None or b is None or b != (a[0] + 1, 0):
                continue
            fa, fb = native.fragments[a[0]], native.fragments[b[0]]
            if fa.block != fb.block or any(positions[id(ch)][0] != b[0] for ch in nonblank):
                continue
            size_pt = max(ch.size for ch in [*left, *nonblank])
            if (abs(fa.x0 - fb.x0) * native.width > .5 * size_pt
                    or not 0 < (fb.y0 - fa.y0) * native.height <= 1.6 * size_pt):
                continue
            ids = {id(ch) for ch in moved}
            before = fragments[upper] + [Fragment(chars, f.block) for f in fragments[lower]
                                        if (chars := [ch for ch in f.chars if id(ch) in ids])]
            after = [Fragment(chars, f.block) for f in fragments[lower]
                     if (chars := [ch for ch in f.chars if id(ch) not in ids])]
            texts = [build_text(parts, native.index, native.width)[0] for parts in (before, after)]
            wanted = [keys[ri][column] + keys[ri + 1][column][:count], keys[ri + 1][column][count:]]
            if [_cell_text_key(text) for text in texts] != wanted:
                continue
            for cell, text, key in zip((upper, lower), texts, wanted):
                spellings = source.get(key, set())
                values[cell] = next(iter(spellings)) if len(spellings) == 1 else text
                matched.add(cell)
            moved_cells.update((upper, lower))


def recover_sheet_grids(parsed: ParsedDocument, pdf: Path, tables: list[list[list[str]]],
                       protected, printed: dict[str, str]) -> int:
    """Replace unresolved, native-backed sheet areas; return partial grids made.

    A source match can repair whitespace inside a cell, but never supplies a row
    or a missing suffix. Unmatched native text stays outside the resulting table.
    """
    from .sheet_rows import PrintedRows

    row_locator = PrintedRows(tables)
    source = defaultdict(set)
    source_headers, source_records = [], set()
    for table in tables:
        header = _header_row(table) if table else None
        if header is not None:
            source_headers.append({_cell_text_key(cell) for cell in table[header] if cell.strip()})
        for ri, row in enumerate(table):
            keys = tuple(_cell_text_key(cell) for cell in row if cell.strip())
            if ri > (header if header is not None else -1) and len(keys) >= 3:
                source_records.add(keys)
            for i, cell in enumerate(row):
                if cell.strip():
                    source[_cell_text_key(cell)].add(cell)
                # Some printed grids merge the row number with its label.
                if cell.strip() and i + 1 < len(row) and row[i + 1].strip():
                    value = cell + ' ' + row[i + 1]
                    source[_cell_text_key(value)].add(value)
    headers = _observed_headers(parsed, tables)
    projected_source_records = {}
    complete_source_rows = {}
    record_header_cache = {}
    header_context = {}
    def covered(block):
        # Additional check against losing OCR-only text. Actual grid geometry and
        # exact per-cell matches, not this character budget, authorize restoration.
        letters = lambda text: Counter(ch for ch in _cell_text_key(text) if ch.isalnum())
        wanted = letters(block.text)
        return block.id in printed and sum((wanted - letters(printed[block.id])).values()) <= max(2, .01 * sum(wanted.values()))

    made = 0
    with pymupdf.open(pdf) as doc:
        for p in range(min(len(doc), len(parsed.pages))):
            corrected = [b for b in parsed.blocks
                         if {r.page_index for r in b.regions} == {p}
                         and (b.id in protected or covered(b))
                         and _restore_record_header(b, tables, headers, record_header_cache)]
            made += len(corrected)
            protected_boxes = [box for b in parsed.blocks if b.id in protected or b in corrected or b.type in ('page_header', 'page_footer')
                               for box in _boxes(b, p)]
            candidates = [b for b in parsed.blocks if b.type in CELL_TYPES and b.id not in protected and b not in corrected
                          and {r.page_index for r in b.regions} == {p} and covered(b)]
            # An intact table owns only its own rectangle. Preserve it before
            # grid selection so that a separate broken table on the same page
            # can still be recovered without dismantling wrapped cell records.
            intact = [b for b in candidates if _complete_source_table(b, tables, headers, complete_source_rows)]
            protected_boxes.extend(box for b in intact for box in _boxes(b, p))
            candidates = [b for b in candidates if b not in intact]
            untouched_blocks = [b for b in parsed.blocks if b.type in CELL_TYPES and b not in candidates]
            untouched = [box for b in untouched_blocks for box in _boxes(b, p)]
            if not candidates:
                continue
            page = doc[p]
            # The text layer uses the unrotated frame. Do not mix that frame
            # with table finder/display coordinates for rotated pages.
            if page.rotation:
                continue
            grids = list(page.find_tables(strategy='lines_strict', use_layout=False).tables)
            natives = getattr(printed, 'native', {})
            if p not in natives:
                natives[p] = read_page(page, p)
            native = natives[p]
            # A drawn outline may enclose many borderless records as one tall
            # "cell". It must not prevent individually source-grounded printed
            # rows inside it from retaining their structure.
            line_height = median(f.y1 - f.y0 for f in native.fragments) if native.fragments else 0
            # A neighbouring chart outline can touch a table border and become
            # one tall, row-spanning cell. Its axis text is not a table column.
            # Require an independently detected picture with the same box;
            # ordinary merged cells and tables inside coarse picture boxes stay.
            picture_boxes = [box for b in parsed.blocks if b.type == 'picture' for box in _boxes(b, p)]
            def picture_cell(cell):
                if not cell or (cell[3] - cell[1]) / native.height <= 2.5 * line_height:
                    return False
                box = tuple(v / (native.width if i % 2 == 0 else native.height)
                            for i, v in enumerate(cell))
                return any(_overprinted(box, picture) for picture in picture_boxes)

            physical_grids = []
            for grid in grids:
                rows = [[None if picture_cell(cell) else cell for cell in row.cells] for row in grid.rows]
                keep = [i for i in range(len(rows[0])) if any(row[i] is not None for row in rows)]
                if len(keep) >= 2:
                    physical_grids.append(SimpleNamespace(rows=[SimpleNamespace(cells=[row[i] for i in keep])
                                                               for row in rows]))
            grids = physical_grids
            occupied_cells = [tuple(v / (native.width if i % 2 == 0 else native.height)
                                    for i, v in enumerate(cell)) for grid in grids
                              for row in grid.rows for cell in row.cells if cell
                              and (cell[3] - cell[1]) / native.height <= 2.5 * line_height]
            row_grids = row_locator.grids(native, occupied_cells)
            header_grids = grids[:]
            finer_cells = [cell for grid in row_grids for row in grid.rows for cell in row.cells if cell]
            if finer_cells:
                # One owner per glyph: coarse cells superseded by finer printed
                # rows cannot remain in the geometry index as overlapping owners.
                grids = [SimpleNamespace(rows=[SimpleNamespace(cells=[
                    None if cell and any(_overlap(cell, finer) for finer in finer_cells) else cell
                    for cell in row.cells]) for row in grid.rows]) for grid in grids]
            # A completely matched printed source header also supplies context to
            # later runs separated by a clipped cell. Keep its source row identity;
            # the first data row of the later run is never promoted to a header.
            for grid in row_grids:
                ti, columns = grid.source_identity
                ri = _header_row(tables[ti], columns, allow_indexed=getattr(grid, 'printed_header', False))
                if ri is not None and ri == grid.source_min_row:
                    headers[ti] = (ri, columns, [tables[ti][ri][c] for c in columns])
            # A numeric cell near the page foot can be misclassified as furniture.
            # Reclaim it only when its exact position/text belongs to a complete
            # source-matched printed row, not merely because a value repeats.
            row_cells = [(tuple(v / (native.width if i % 2 == 0 else native.height)
                                for i, v in enumerate(cell)), cell)
                         for grid in row_grids for row in grid.rows for cell in row.cells if cell]
            reclaimed = []
            for b in parsed.blocks:
                if b.type not in ('page_header', 'page_footer') or b.id in protected:
                    continue
                boxes = _boxes(b, p)
                if not boxes or {r.page_index for r in b.regions} != {p}:
                    continue
                for box, _ in row_cells:
                    if all(_inside((area[0] + area[2]) / 2, (area[1] + area[3]) / 2, box) for area in boxes):
                        parts = [f for f in native.fragments if _inside((f.x0 + f.x1) / 2, (f.y0 + f.y1) / 2, box)]
                        if _cell_text_key(build_text(parts, p, native.width)[0]) == _cell_text_key(b.text):
                            reclaimed.append(b)
                            break
            if reclaimed:
                reclaimed_ids = {b.id for b in reclaimed}
                candidates.extend(reclaimed)
                protected_boxes = [box for b in parsed.blocks if b.id not in reclaimed_ids
                                   and (b.id in protected or b.type in ('page_header', 'page_footer'))
                                   for box in _boxes(b, p)]
            grids.extend(row_grids)
            cells = list(dict.fromkeys(cell for t in grids for row in t.rows for cell in row.cells if cell))
            if not cells or len(cells) > 4096:
                continue
            normalized = [tuple(v / (native.width if i % 2 == 0 else native.height)
                                for i, v in enumerate(cell)) for cell in cells]
            indices = {cell: i for i, cell in enumerate(cells)}
            fragments = defaultdict(list)
            records = []
            # Exactly one physical owner per glyph, including merged cells. Ambiguous
            # border glyphs stay outside the grid rather than being copied twice.
            slots = defaultdict(list)
            for i, box in enumerate(normalized):
                for slot in range(int(box[1] * 128), int(box[3] * 128) + 1):
                    slots[slot].append(i)
            proven = defaultdict(set)
            owned_cells = set()
            for grid in row_grids:
                for box, glyphs in getattr(grid, 'glyph_owners', {}).items():
                    i = indices[box]
                    owned_cells.add(i)
                    for ch in glyphs:
                        proven[id(ch)].add(i)
            def owner(ch):
                if id(ch) in proven:
                    choices = proven[id(ch)]
                    return next(iter(choices)) if len(choices) == 1 else None
                # Source-matched cells own precisely their matched characters. Their
                # overlapping rectangles must not consume a neighbouring row's label.
                matches = [i for i in slots[int(ch.cy * 128)] if i not in owned_cells
                           and _inside(ch.cx, ch.cy, normalized[i])]
                return matches[0] if len(matches) == 1 else None
            for origin, fragment in enumerate(native.fragments):
                for cell, chars in groupby(fragment.chars, key=owner):
                    part = Fragment(list(chars), block=fragment.block)
                    records.append((cell, part, origin))
                    if cell is not None:
                        fragments[cell].append(part)
            values, matched = {}, set()
            for cell, parts in fragments.items():
                text = build_text(parts, p, native.width)[0]
                matches = source.get(_cell_text_key(text), set())
                if len(matches) == 1:
                    text = next(iter(matches))
                    matched.add(cell)
                elif text in matches:
                    # "13" and "1 3" have the same whitespace key. An already
                    # exact printed spelling needs no ambiguous normalization.
                    matched.add(cell)
                values[cell] = text
            _keep_overflow_words(grids, indices, fragments, values, matched, native, row_locator)
            _keep_row_overflows(grids, indices, fragments, values, matched, native, row_locator, source)
            _attach_printed_headers(header_grids, row_grids, candidates, page, p, tables, native, headers)
            selected = []
            occupied = set()
            projected_records = set()
            for grid in grids:
                rows = [[indices[cell] if cell else None for cell in row.cells] for row in grid.rows]
                identity = getattr(grid, 'source_identity', None)
                if identity:
                    ti, columns = identity
                    if identity not in projected_source_records:
                        header_row = _header_row(tables[ti], columns)
                        projected_source_records[identity] = {
                            tuple(_cell_text_key(row[c]) for c in columns)
                            for ri, row in enumerate(tables[ti])
                            if ri > (header_row if header_row is not None else -1)
                            and all(c < len(row) and row[c].strip() for c in columns)
                        }
                    expected = projected_source_records[identity]
                    header_row = _header_row(tables[ti], columns)
                    heading = tuple(_cell_text_key(tables[ti][header_row][c]) for c in columns
                                    if tables[ti][header_row][c].strip()) if header_row is not None else ()
                    for row in rows:
                        present = tuple(c for c in row if c is not None and values.get(c, '').strip())
                        keys = tuple(_cell_text_key(values[c]) for c in present)
                        if (len(present) >= 3 and len(set(present)) == len(present)
                                and (keys in expected or sum(c in matched for c in present) >= 3 and keys != heading)):
                            # The locator already proved a unique source-column
                            # partition for every visible glyph. Three complete
                            # cells can ground that record even when another
                            # cell is blank or visibly clipped; a missing suffix
                            # is never imported from the workbook.
                            projected_records.add(present)
                qualifies = []
                for row in rows:
                    filled = list(dict.fromkeys(c for c in row if c is not None and values.get(c, '').strip()))
                    hits = [c for c in filled if c in matched]
                    qualifies.append(len(hits) >= 2 and len(hits) >= .5 * len(filled)
                                     and not any(_overlap(normalized[c], box) for c in row if c is not None
                                                 for box in [*protected_boxes, *untouched]))
                # A physical header may leave one separate column blank. Do not
                # mistake its following numeric record for column names. Merged
                # caption cells are not individual column headings.
                if getattr(grid, 'source_identity', None) is None:
                    for i in range(len(rows) - 1):
                        row = rows[i]
                        filled = [c for c in row if c is not None and values.get(c, '').strip()]
                        if (not qualifies[i] and qualifies[i + 1] and filled
                                and None not in row and len(set(row)) == len(row)
                                and all(any(ch.isalpha() for ch in values[c]) for c in filled)
                                and not any(_overlap(normalized[c], box) for c in row
                                            for box in [*protected_boxes, *untouched])):
                            qualifies[i] = True
                # Keep short exceptional rows *between* strongly grounded rows. They
                # still contain only their own printed glyphs, never source-row copies.
                for i in range(1, len(rows) - 1):
                    if qualifies[i - 1] and qualifies[i + 1] and not qualifies[i]:
                        filled = [c for c in rows[i] if c is not None and values.get(c, '').strip()]
                        if filled and any(c in matched for c in filled):
                            qualifies[i] = not any(_overlap(normalized[c], box) for c in rows[i]
                                                   if c is not None for box in [*protected_boxes, *untouched])
                start = None
                for i, ok in enumerate([*qualifies, False]):
                    if ok and start is None:
                        start = i
                    elif not ok and start is not None:
                        run = rows[start:i]
                        ids = {c for row in run for c in row if c is not None}
                        identity = getattr(grid, 'source_identity', None)
                        single_row = bool(identity and len(rows) == 1 and len(ids & matched) >= 3)
                        if (len(run) >= 2 and len(ids & matched) >= 4 or single_row) and not ids & occupied:
                            header = getattr(grid, 'observed_header', None)
                            if header is None and identity and identity[0] in headers:
                                ri, columns, values_header = headers[identity[0]]
                                names = dict(zip(columns, values_header))
                                context_key = identity, ri, grid.source_min_row
                                if context_key not in header_context:
                                    header_context[context_key] = _header_context_reaches(
                                        tables[identity[0]], identity[1], ri, grid.source_min_row)
                                known = set(identity[1]) & names.keys()
                                if ((set(identity[1]) <= names.keys() or len(known) >= max(3, len(identity[1]) / 2))
                                        and ri < grid.source_min_row
                                        and header_context[context_key]):
                                    header = [names.get(c, f'Column {i + 1}') for i, c in enumerate(identity[1])]
                            if identity and header is None and not (start == 0 and (
                                    getattr(grid, 'printed_header', False)
                                    or grid.source_min_row == _header_row(tables[identity[0]], identity[1]))):
                                header = [f'Column {i + 1}' for i in range(len(rows[0]))]
                            if identity is None and header is None and not any(
                                    ch.isalpha() for c in run[0] if c is not None for ch in values.get(c, '')):
                                header = [f'Column {i + 1}' for i in range(len(run[0]))]
                            if (header is not None and identity
                                    and any(value != f'Column {j + 1}' for j, value in enumerate(header))
                                    and len(header) == len(run[0])
                                    and all(_cell_text_key(values.get(c, '')) == _cell_text_key(value)
                                            for c, value in zip(run[0], header))):
                                # A repeated header actually printed on this page
                                # is already the run's first row; do not emit it twice.
                                header = None
                            selected.append((run, ids, _bounds([normalized[c] for c in ids]), header))
                            occupied.update(ids)
                        start = None
            if not selected:
                continue
            # A proven header-only run must not dismantle an existing table
            # whose sparse data rows cannot be assigned to the replacement.
            # A reconstruction with independently grounded data rows may split
            # a coarse layout table that wrongly absorbed neighbouring notes.
            # A headings-only reconstruction must keep the existing cell data.
            # Raw text merely tagged "table" still needs reconstruction too.
            selected_ids = {cell for run in selected for cell in run[1]}
            def grounded_record(row):
                cells = list(dict.fromkeys(cell for cell in row if cell is not None
                                           and values.get(cell, '').strip()))
                numeric = sum(cell in matched and any(ch.isdigit() for ch in values[cell])
                              and not any(ch.isalpha() for ch in values[cell]) for cell in cells)
                # A complete source record may contain only text. Its whole
                # ordered cell tuple, beyond the source header, establishes data
                # ownership independently of whether any cell is numeric.
                # A printed row may show only some source columns. Accept that
                # projection only from the locator's proven column identity,
                # with at least three complete, ordered visible cells. Hidden
                # source columns never supply text to the replacement.
                keys = tuple(_cell_text_key(values[cell]) for cell in cells)
                return numeric >= 2 or len(keys) >= 3 and (
                    keys in source_records or tuple(cells) in projected_records)
            would_detach = False
            for b in candidates:
                if b.type != 'table' or not re.search(r'(?m)^\|[ |:\-]+\|\s*$', b.text):
                    continue
                boxes = _boxes(b, p)
                if not any(_overlap(box, run[2]) for box in boxes for run in selected):
                    continue
                data_rows = sum(grounded_record(row)
                                for rows, _, box, _ in selected if any(_overlap(box, area) for area in boxes)
                                for row in rows)
                assigned = ''.join(ch.c for cell, fragment, _ in records if cell in selected_ids
                                   for ch in fragment.chars if any(_inside(ch.cx, ch.cy, box) for box in boxes))
                headings = [_cell_text_key(cell.replace('\\|', '|')) for cell in re.split(
                    r'(?<!\\)\|', b.text.strip().splitlines()[0].strip().strip('|')) if cell.strip()]
                # Adjacent source-cell combinations help match merged data, but
                # cannot certify a parser's accidentally merged column names.
                # A person's name/code is also a source cell. Membership in
                # arbitrary source cells must not certify the first data row
                # as a header and veto its recovery into ordinary records.
                known_headings = (len(headings) >= 2
                                  and any(set(headings) <= names for names in source_headers)
                                  and sum(any(ch.isalpha() for ch in key) for key in headings) >= max(2, len(headings) / 2))
                # Data text may contain all the same letters as a lost heading.
                # Require its complete cell in the replacement's header, not a
                # page-wide character budget, before discarding the old table.
                replacement_headings = Counter(
                    _cell_text_key(value) for rows, _, box, header in selected
                    if any(_overlap(box, area) for area in boxes)
                    for value in (header if header is not None else [values.get(c, '') for c in rows[0]])
                    if value.strip())
                if known_headings and Counter(headings) - replacement_headings:
                    would_detach = True
                    break
                if data_rows >= 2:
                    continue
                # Coarse layout boxes can also contain an independent note.
                # Compare the actual Markdown cell contents, not every glyph
                # in that oversized rectangle. Geometry already proves the new
                # cell assignments; this budget only prevents detaching values.
                content = ''.join(cell.replace('\\|', '|') for row, line in enumerate(b.text.strip().splitlines())
                                  if not (row == 1 and re.fullmatch(r'[\s|:\-]+', line))
                                  for cell in re.split(r'(?<!\\)\|', line.strip().strip('|')))
                if Counter(_cell_text_key(content)) - Counter(_cell_text_key(assigned)):
                    would_detach = True
                    break
            if would_detach:
                continue
            # Only replace connected layout blocks that actually overlap the grid.
            taken = [b for b in candidates if any(_overlap(box, run[2])
                     for box in _boxes(b, p) for run in selected)]
            if not taken:
                continue
            regions = [box for b in taken for box in _boxes(b, p)]
            for _ in range(len(candidates)):
                extra = [b for b in candidates if b not in taken and any(_overlap(box, other)
                         for box in _boxes(b, p) for other in regions)]
                if not extra:
                    break
                taken.extend(extra)
                regions.extend(box for b in extra for box in _boxes(b, p))
            if any(_overlap(box, other) for box in regions for other in protected_boxes):
                continue
            # The remainder below excludes untouched boxes. Their coordinates
            # alone cannot establish that those discarded glyphs are represented.
            if not _represented_overlap(native, page, p, regions, untouched_blocks):
                continue
            # A raster carrying otherwise unrepresented OCR text is not covered by a
            # native grid. Retain the existing parse in that case.
            picture_boxes = [box for b in parsed.blocks if b.type == 'picture' for box in _boxes(b, p)]
            unrepresented_image = False
            for info in page.get_image_info():
                raw = info['bbox']
                box = tuple(v / (native.width if i % 2 == 0 else native.height) for i, v in enumerate(raw))
                if any(_overlap(box, area) for area in regions) and not any(
                        _inside((box[0] + box[2]) / 2, (box[1] + box[3]) / 2, outer) for outer in picture_boxes):
                    if (box[2] - box[0]) * (box[3] - box[1]) > .002:
                        unrepresented_image = True
                        break
            if unrepresented_image:
                continue
            anchor = taken[0]
            output = []
            consumed = set()
            for n, (rows, ids, box, header) in enumerate(selected):
                keep = [i for i in range(len(rows[0])) if any(row[i] is not None
                        and values.get(row[i], '').strip() for row in rows)]
                seen = set()
                grid_values = []
                for row in rows:
                    output_row = []
                    for i in keep:
                        cell = row[i]
                        output_row.append(values.get(cell, '') if cell is not None and cell not in seen else '')
                        if cell is not None:
                            seen.add(cell)
                    grid_values.append(output_row)
                if len(keep) < 2:
                    continue
                if header:
                    grid_values.insert(0, [header[i] for i in keep])
                text = table_markdown(grid_values)
                output.append(anchor.model_copy(update={
                    'id': f'{anchor.id}-grid-{n}', 'type': 'table', 'level': None,
                    'text': text, 'markdown': text, 'lines': None, 'line_geometry': 'unavailable',
                    'regions': [_region(p, box)],
                    'source': anchor.source.model_copy(update={'ref': f'printed-sheet-grid:page:{p};grid:{n}'})}))
                consumed.update(ids)
            if not output:
                continue
            remainder = defaultdict(list)
            # One native PDF block may span several recovered tables. Keep its
            # remaining lines on their own side of each table, otherwise a note
            # below a table gets joined to a paragraph above it and read first.
            separators = sorted({y for block in output for y in (
                block.regions[0].bbox.y0, block.regions[0].bbox.y1)})
            for fragment in _remaining_fragments(records, consumed):
                chars = [ch for ch in fragment.chars if any(_inside(ch.cx, ch.cy, box) for box in regions)
                         and not any(_inside(ch.cx, ch.cy, box) for box in untouched)]
                if chars:
                    for band, group in groupby(chars, key=lambda ch: bisect_right(separators, ch.cy)):
                        remainder[fragment.block, band].append(Fragment(list(group), block=fragment.block))
            for parts in remainder.values():
                text, lines, _ = build_text(parts, p, native.width)
                if not text.strip():
                    continue
                box = _bounds([(f.x0, f.y0, f.x1, f.y1) for f in parts])
                output.append(anchor.model_copy(update={
                    'id': f'{anchor.id}-remainder-{len(output)}', 'type': 'paragraph', 'level': None,
                    'text': text, 'markdown': text,
                    'lines': [TextLine.model_validate(line) for line in lines],
                    'line_geometry': 'exact' if lines else 'unavailable',
                    'regions': [_region(p, box)],
                    'source': anchor.source.model_copy(update={'ref': f'printed-sheet-remainder:page:{p}'})}))
            # Horizontal glyph extraction intentionally excludes rotated text. Preserve
            # those atomic lines separately, including stamps/notes outside chosen rows.
            flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
            rotated_seen = defaultdict(list)
            for raw_block in page.get_text('dict', flags=flags)['blocks']:
                for line in raw_block.get('lines', []):
                    if horizontal_line(line):
                        continue
                    box = tuple(v / (native.width if i % 2 == 0 else native.height)
                                for i, v in enumerate(line['bbox']))
                    if not any(_inside((box[0]+box[2])/2, (box[1]+box[3])/2, area) for area in regions):
                        continue
                    if any(_inside((box[0]+box[2])/2, (box[1]+box[3])/2, area) for area in untouched):
                        continue
                    text = ' '.join(s['text'].strip() for s in line['spans'] if s['text'].strip())
                    if text:
                        identity = (text, tuple(line.get('dir', ())))
                        if any(_overprinted(box, other) for other in rotated_seen[identity]):
                            continue
                        rotated_seen[identity].append(box)
                        output.append(anchor.model_copy(update={
                            'id': f'{anchor.id}-rotated-{len(output)}', 'type': 'paragraph', 'level': None,
                            'text': text, 'markdown': text, 'lines': None, 'line_geometry': 'unavailable',
                            'regions': [_region(p, box)],
                            'source': anchor.source.model_copy(update={'ref': f'printed-sheet-rotated:page:{p}'})}))
            output.sort(key=lambda b: (b.regions[0].bbox.y0, b.regions[0].bbox.x0))
            recovered_tables = sum(b.type == 'table' for b in output)
            gone = {b.id for b in taken}
            positions = [i for i, b in enumerate(parsed.blocks) if b.id in gone]
            first, last = min(positions), max(positions)
            middle = parsed.blocks[first:last + 1]
            if all({r.page_index for r in b.regions} == {p} for b in middle):
                # A clipped row between two recovered grids may remain an original
                # paragraph. Insert it between the grids, not after both tables.
                output.extend(b for b in middle if b.id not in gone)
                output.sort(key=lambda b: (b.regions[0].bbox.y0, b.regions[0].bbox.x0))
                gone.update(b.id for b in middle)
            blocks = []
            for b in parsed.blocks:
                if b.id == anchor.id:
                    blocks.extend(output)
                if b.id not in gone:
                    blocks.append(b)
            parsed.blocks = blocks
            made += recovered_tables
    for i, b in enumerate(parsed.blocks):
        b.reading_order = i
    return made
