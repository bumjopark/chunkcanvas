"""Carry column labels across pages only when their printed grid proves ownership."""
import re
from collections import Counter
from itertools import groupby


def restore_printed_headers(parsed, tables, pdf, native_cache):
    from .source_tables import _header_row, _cell_text_key as key, table_markdown
    from .textlayer import Fragment, read_page, build_text
    import pymupdf

    candidates = []
    for table in tables:
        h = _header_row(table)
        if h is None or not 2 <= len(table[h]) <= 20:
            continue
        filled = sum(bool(c.strip()) for c in table[h])
        if filled < min(3, len(table[h])) or filled < len(table[h]) / 2:
            continue
        counts = Counter((ci, key(cell)) for row in table[h + 1:] for ci, cell in enumerate(row) if cell.strip())
        anchors = {(ci, key(cell)): ri for ri, row in enumerate(table) if ri > h
                   for ci, cell in enumerate(row) if cell.strip() and counts[ci, key(cell)] == 1}
        candidates.append((table, h, anchors))
    if not candidates:
        return 0

    physical = {}
    def aligned(a, b):
        return len(a) == len(b) and all(abs(x[0] - y[0]) < .012 and abs(x[2] - y[2]) < .012
                                       for x, y in zip(a, b))

    changed = 0
    with pymupdf.open(pdf) as doc:
        def page_rows(p):
            if p not in physical:
                page = doc[p]
                native = native_cache.get(p)
                if native is None:
                    native = native_cache[p] = read_page(page, p)
                grids = [row.cells for grid in page.find_tables(strategy='lines_strict', use_layout=False).tables
                         for row in grid.rows]
                cells = list(dict.fromkeys(cell for row in grids for cell in row if cell))
                if len(cells) > 4096:
                    physical[p] = []
                    return []
                boxes = {cell: tuple(v / (native.width if i % 2 == 0 else native.height)
                                    for i, v in enumerate(cell)) for cell in cells}
                slots = {}
                fragments = {cell: [] for cell in cells}
                for cell, box in boxes.items():
                    for slot in range(int(box[1] * 128), int(box[3] * 128) + 1):
                        slots.setdefault(slot, []).append(cell)
                def owner(ch):
                    found = [cell for cell in slots.get(int(ch.cy * 128), []) if
                             boxes[cell][0] <= ch.cx <= boxes[cell][2] and boxes[cell][1] <= ch.cy <= boxes[cell][3]]
                    return found[0] if len(found) == 1 else None
                for fragment in native.fragments:
                    for cell, chars in groupby(fragment.chars, key=owner):
                        if cell is not None:
                            fragments[cell].append(Fragment(list(chars)))
                values = {cell: build_text(parts, p, native.width)[0] if parts else ''
                          for cell, parts in fragments.items()}
                rows = [([boxes[cell] for cell in row if cell], [values[cell] for cell in row if cell])
                        for row in grids if any(row)]
                physical[p] = rows
            return physical[p]

        for block in parsed.blocks:
            if block.type != 'table' or len({r.page_index for r in block.regions}) != 1:
                continue
            # OCR-backed cells can refer to a different visual table than the
            # native text grid. Their header ownership remains governed by the
            # existing aligned parsed-fragment check in source_tables.
            if block._table_ocr_cells:
                continue
            page = block.regions[0].page_index
            if page == 0:
                continue
            lines = block.text.splitlines()
            if len(lines) < 3:
                continue
            cells = [[c.strip().replace('\\|', '|') for c in re.split(r'(?<!\\)\|', line.strip().strip('|'))]
                     for i, line in enumerate(lines) if i != 1 and line.strip().startswith('|')]
            if not cells or len({len(row) for row in cells}) != 1:
                continue
            matches = []
            for table, h, anchors in candidates:
                width = len(table[h])
                columns = list(range(width))
                if width != len(cells[0]):
                    projection = re.search(r'(?:^|;)columns:([0-9,]+)(?:;|$)', block.source.ref or '')
                    if projection is None:
                        continue
                    columns = [int(c) for c in projection[1].split(',')]
                    if len(columns) != len(cells[0]) or columns != sorted(set(columns)) or max(columns) >= width:
                        continue
                header = [table[h][c] for c in columns]
                # A title/blank row can precede the actual labels. Do not
                # add another copy when this table already carries them.
                if not all(header) or any(list(map(key, header)) == list(map(key, row)) for row in cells):
                    continue
                found = [(ci, anchors[columns[ci], key(cell)]) for row in cells for ci, cell in enumerate(row)
                         if (columns[ci], key(cell)) in anchors]
                row_ids = {ri for _, ri in found}
                whole_row = any(list(map(key, row)) == [key(table[ri][c]) for c in columns]
                                for row in cells for ri in row_ids)
                if len(row_ids) < 2 and not whole_row:
                    continue
                # Source vocabulary locates a table, but only drawn columns locate
                # a header. Do not borrow headings from a different horizontal grid.
                current = [([boxes[c] for c in columns], [values[c] for c in columns])
                           for boxes, values in page_rows(page)
                           if len(boxes) == width and any(
                               all(r.bbox.x0 - .015 < box[2] and box[0] < r.bbox.x1 + .015
                                   and r.bbox.y0 - .015 < box[3] and box[1] < r.bbox.y1 + .015
                                   for box in [boxes[c] for c in columns]) for r in block.regions)]
                if not current or not all(aligned(current[0][0], boxes) for boxes, _ in current):
                    continue
                bounds = current[0][0]
                observed = None
                # Stop at a page without this grid; an unrelated later table may
                # reuse both column positions and values from an earlier section.
                for previous in range(page - 1, -1, -1):
                    rows = page_rows(previous)
                    same_grid = [([boxes[c] for c in columns], [values[c] for c in columns])
                                 for boxes, values in rows if len(boxes) == width
                                 and aligned(bounds, [boxes[c] for c in columns])]
                    if not same_grid:
                        break
                    headings = list(same_grid)
                    if columns == [i for i, c in enumerate(table[h]) if c.strip()]:
                        # A printed heading can span adjacent empty source slots.
                        # Only an omitted, unlabelled slot may widen its box; the
                        # next populated column must still start at the same edge.
                        for boxes, values in rows:
                            if len(boxes) != len(columns):
                                continue
                            fits = True
                            for i, (a, b) in enumerate(zip(bounds, boxes)):
                                next_c = columns[i + 1] if i + 1 < len(columns) else width
                                merged = next_c > columns[i] + 1
                                far_edge = bounds[i + 1][0] if i + 1 < len(bounds) else a[2]
                                if abs(a[0] - b[0]) >= .012 or not (
                                        abs(a[2] - b[2]) < .012 or merged and abs(far_edge - b[2]) < .012):
                                    fits = False
                                    break
                            if fits:
                                headings.append((boxes, values))
                    hits = [(boxes, values) for boxes, values in headings
                            if list(map(key, values)) == list(map(key, header))]
                    if hits:
                        observed = previous, hits[-1][0]
                        break
                if observed is None:
                    continue
                first = min(row_ids)
                safe = True
                for source_row in table[h + 1:first]:
                    labels = {key(c) for c in source_row if c.strip()}
                    if len(labels) != 1 or not source_row[0].strip() or not any(c.isalpha() for c in source_row[0]):
                        continue
                    # A picture row has a narrow label cell and separate picture
                    # cells. A full-width section title ends the column context.
                    occurrences = [(boxes, values) for p in range(observed[0], page + 1)
                                   for boxes, values in page_rows(p)
                                   if (p > observed[0] or boxes[0][1] >= observed[1][0][3] - .01)
                                   and values and key(values[0]) == key(source_row[0])]
                    if not occurrences or any(len(boxes) != width
                                              or not aligned(bounds, [boxes[c] for c in columns])
                                              or any(v.strip() for v in values[1:])
                                              for boxes, values in occurrences):
                        safe = False
                        break
                if safe:
                    matches.append((header, observed[0]))
            if len(matches) != 1:
                continue
            generic = (block.source.ref or '').startswith('printed-document-grid:') and cells[0] == [
                f'Column {i + 1}' for i in range(len(cells[0]))]
            rows = [matches[0][0], *(cells[1:] if generic else cells)]
            block.text = block.markdown = table_markdown(rows)
            block.lines = None
            block.line_geometry = 'unavailable'
            block.source.ref = (block.source.ref or '') + f';printed-header-page:{matches[0][1]}'
            changed += 1
    return changed
