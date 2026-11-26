"""Name `Column N` table columns from the source table that the printed rows come from.

Printed-grid fallbacks label columns `Column 1..N` when no heading is printed on the page
(a table continued from an earlier page) or when the printed heading was clipped and no
longer matches exactly. Production restored such tables from the source and kept its
heading row, so every chunk of the table still said what its columns are.

Here a heading is carried only when the printed rows prove where they come from:
all data rows match a consecutive run of one source table in the same increasing
columns (a clipped cell may match by a prefix of three or more characters). Two
distinct records are needed, and competing column assignments remain unknown.
The source heading must precede every possible row owner without crossing a new
labelled section or an assignment row (`Location = | New York`); the records
below such a row stay unnamed. Data rows are never changed. A printed heading row that
was taken as the first data row becomes the heading again instead of being repeated.
"""
import re
from bisect import bisect_left
from .header_projection import SourceIndex as _SourceIndex, match as _project_match

_SEPARATOR = re.compile(r'[\s|:\-]+')


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith('|'):
        body = body[1:]
    if body.endswith('|') and not body.endswith('\\|'):
        body = body[:-1]
    return [cell.strip().replace('\\|', '|') for cell in re.split(r'(?<!\\)\|', body)]


def _synthetic(cells: list[str]) -> bool:
    return bool(cells) and all(cell == f'Column {i + 1}' for i, cell in enumerate(cells))


def _clipped(printed: str, source: str) -> bool:
    """A printed cell cut off by its column width: a word-like prefix of the source cell."""
    return (len(printed) >= 3 and len(source) > len(printed) and source.startswith(printed)
            and any(ch.isalpha() for ch in printed))


def _row_mapping(keys: list[str], source_keys: list[str]) -> tuple[dict[int, int], int] | None:
    """Return only column pairs shared by every best monotone row assignment.

    Repeated values can have several equally good owners. A left-to-right greedy
    match picks one arbitrarily; forward/backward alignment keeps that ambiguity
    instead of manufacturing evidence for a column name.
    """
    m, n = len(keys), len(source_keys)
    if not m or not n or m * n > 16384:
        return None
    weights = [[(m + 2 if a == b else m + 1 if _clipped(a, b) else 0) if a and b else 0
                for b in source_keys] for a in keys]
    before = [[0] * (n + 1) for _ in range(m + 1)]
    after = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m):
        for j in range(n):
            before[i + 1][j + 1] = max(before[i][j + 1], before[i + 1][j],
                                       before[i][j] + weights[i][j])
    for i in range(m - 1, -1, -1):
        for j in range(n - 1, -1, -1):
            after[i][j] = max(after[i + 1][j], after[i][j + 1],
                              after[i + 1][j + 1] + weights[i][j])
    best = after[0][0]
    mapping = {}
    for i in range(m):
        choices = [j for j in range(n) if weights[i][j]
                   and before[i][j] + weights[i][j] + after[i + 1][j + 1] == best]
        skipped = any(before[i][j] + after[i + 1][j] == best for j in range(n + 1))
        if len(choices) == 1 and not skipped:
            mapping[i] = choices[0]
    exact = sum(keys[i] == source_keys[j] for i, j in mapping.items())
    filled = sum(bool(k) for k in keys)
    if exact < 1 or len(mapping) < 2 or len(mapping) < .6 * filled:
        return None
    return mapping, exact


def _match(rows: list[list[str]], tables, index, key):
    """A complete proof or abstention; never fall back to row-vote guesses."""
    return _project_match(rows, index, key)


def _merged_spans(row) -> list[range]:
    """Heading cells merged across columns: a label and the blank cells after it."""
    spans, c = [], 0
    while c < len(row):
        if not row[c].strip():
            c += 1
            continue
        end = c + 1
        while end < len(row) and not row[end].strip():
            end += 1
        if end - c > 1:
            spans.append(range(c, end))
        c = end
    return spans


def _continues_heading(above, below, key) -> bool:
    """`below` names the columns under `above`'s merged cells (years over months).

    It gives new labels under at least two merged cells, filling a blank heading cell, and
    is blank (or repeats a vertically merged label) under every heading cell that is not
    merged. A record under a heading has values in ordinary columns or, when those are
    empty, sits under at most one merged cell (a label merged over its sub-column).
    """
    cell = lambda row, c: row[c].strip() if c < len(row) else ''
    spans = _merged_spans(above)
    merged = {c for span in spans for c in span}
    sub = [c for c in sorted(merged) if cell(below, c) and key(cell(below, c)) != key(cell(above, c))]
    return (sum(any(c in span for c in sub) for span in spans) >= 2
            and all(any(ch.isalpha() for ch in cell(below, c)) for c in sub)
            and any(not cell(above, c) for c in sub)
            and all(not cell(below, c) or key(cell(below, c)) == key(cell(above, c))
                    for c in range(max(len(above), len(below))) if c not in merged))


def _section_heading(table, columns, source_rows, header_row, context_reaches, key=lambda v: v.strip()):
    """The nearest heading above the matched rows. The source table is read in sections
    separated by completely blank rows; each section's heading is found as `header_row`
    finds a table's (title rows before it are skipped). Its context must reach the rows.

    Looked for in the proven columns alone, a heading merged across them (blank there) is
    skipped and the first all-text record below it would be returned instead. Such a row
    does not count: the section's heading over all columns lies above it with nothing in
    the proven columns in between, and it does not continue that heading's merged cells.
    """
    rows = sorted(min(ris) for ris in source_rows.values())
    if len(rows) < 2 or not table:
        return None
    limit = rows[-2]                     # a heading needs two matched rows below it
    width = min(len(row) for row in table)
    filled = lambda row, cols: any(c < len(row) and row[c].strip() for c in cols)
    blank = [not filled(row, range(len(row))) for row in table]
    starts = [0] + [r for r in range(1, len(table)) if blank[r - 1] and not blank[r]]
    nonblank = [r for r, row in enumerate(table) if filled(row, columns)]
    candidates, barriers = [], []
    for n, start in enumerate(starts):
        if start >= limit:
            break
        stop = starts[n + 1] if n + 1 < len(starts) else len(table)
        window = [r for r in nonblank[bisect_left(nonblank, start):bisect_left(nonblank, stop)]]
        found = header_row([table[r] for r in window], columns) if window else None
        if found is None or window[found] >= limit:
            continue
        r = window[found]
        values = [cell.strip() for cell in table[r] if cell.strip()]
        if len(values) == 2 and values[0].endswith('='):
            # An explicit assignment such as "Location = | New York" is form
            # metadata, not a heading, and it ends every earlier heading's
            # context. The rows below it get no heading: the first all-text
            # row under it is as likely a record as a label, and a record
            # must never be taken out of the body as column names.
            barriers.append(r)
            continue
        whole = header_row(table[start:min(stop, r + 9)], range(width)) if width else None
        whole = None if whole is None else start + whole
        if (whole is not None and whole < r
                and not any(filled(table[i], columns) for i in range(whole + 1, r))
                and not (r == whole + 1 and _continues_heading(table[whole], table[r], key))):
            continue
        candidates.append(r)
    last = max(ri for ris in source_rows.values() for ri in ris)
    def numeric(row):
        values = [row[c].strip() for c in columns if c < len(row) and row[c].strip()]
        return sum(any(ch.isdigit() for ch in v) and not any(ch.isalpha() for ch in v)
                   for v in values) >= max(1, len(values) / 2)
    for h in sorted(candidates, reverse=True):
        if any(h < barrier <= last for barrier in barriers):
            continue
        later = [ri for ri in rows if ri > h]
        if len(later) < 2 or not context_reaches(table, columns, h, last):
            continue
        # A change from numeric records to labels and back can separate tables
        # without a blank source row. Do not carry an earlier heading across
        # that unresolved boundary, or promote the suspected labels as data.
        crossed = False
        for r in range(h + 2, min(last, len(table) - 1)):
            values = [table[r][c].strip() for c in columns if table[r][c].strip()]
            if (len(values) >= max(2, len(columns) / 2)
                    and all(any(ch.isalpha() for ch in v) for v in values)
                    and numeric(table[r - 1]) and numeric(table[r + 1])
                    and any(key(table[r][c]) != key(table[h][c]) for c in columns)):
                crossed = True
                break
        if not crossed:
            return h
    return None


def carry_source_headers(parsed, tables, *, allow_missing_cells=False) -> int:
    from .sheet_grids import _header_context_reaches
    from .source_tables import _cell_text_key as key, _header_row, table_markdown

    if not tables:
        return 0
    try:
        index = _SourceIndex(tables, key, allow_missing=allow_missing_cells)
    except OverflowError:
        return 0
    changed = 0
    for block in parsed.blocks:
        if block.type != 'table':
            continue
        lines = block.text.splitlines()
        if len(lines) < 3 or not _SEPARATOR.fullmatch(lines[1]):
            continue
        head = _cells(lines[0])
        if not _synthetic(head):
            continue
        data_lines = lines[2:]
        rows = [_cells(line) for line in data_lines]
        if any(len(row) != len(head) for row in rows):
            continue
        found = _match(rows, tables, index, key)
        if found is None:
            continue
        ti, mapping, source_rows, row_maps = found
        mapping = dict(mapping)
        table = tables[ti]
        columns = [mapping[m] for m in sorted(mapping)]
        h = _section_heading(table, columns, source_rows, _header_row, _header_context_reaches, key)
        if h is None:
            continue
        if (h > 0 and h - 1 in source_rows.get(0, set())
                and _continues_heading(table[h - 1], table[h], key)):
            h -= 1
        heading = list(table[h])
        # A clipped printed heading becomes the first data row; take it back as the heading.
        first_keys = [key(cell) for cell in rows[0]]
        promoted = source_rows.get(0, set()) == {h} and sum(
            1 for m, c in mapping.items()
            if first_keys[m] and (first_keys[m] == key(heading[c]) or _clipped(first_keys[m], key(heading[c])))) >= 2
        if (not promoted and h + 1 < len(table)
                and _continues_heading(table[h], table[h + 1], key)):
            continue  # a sparse parent heading alone does not name its subcolumns
        stacked = 0
        if promoted:
            # A merged heading continues on the next source row (years over months). That row
            # names the columns under the merged label, filling at least one blank heading
            # cell, and is blank (or repeats a vertically merged label) under every heading
            # cell that is not merged. An ordinary first record fails the second test.
            while stacked < 2 and len(rows) > stacked + 2 and source_rows.get(stacked + 1, set()) == {h + stacked + 1}:
                below = table[h + stacked + 1]
                if _continues_heading(table[h + stacked], below, key):
                    stacked += 1
                    heading = [a.strip() if key(b) == key(a) else ' '.join(part for part in (a.strip(), b.strip()) if part)
                               for a, b in zip(heading, [*below, *[''] * (len(heading) - len(below))])]
                else:
                    break
        if promoted:
            # The printed heading rows matched the source heading rows cell by cell, which
            # places their columns too (month columns may hold no text in any data row).
            for k in range(stacked + 1):
                for m, c in sorted(row_maps.get((k, h + k), {}).items()):
                    if m in mapping:
                        continue
                    left = max((mapping[x] for x in mapping if x < m), default=-1)
                    right = min((mapping[x] for x in mapping if x > m), default=len(heading))
                    if left < c < right:
                        mapping[m] = c
        data = {j: ris for j, ris in source_rows.items() if not (promoted and j <= stacked)}
        below_rows = [min(ri for ri in ris if ri > h + stacked) for ris in data.values() if any(ri > h + stacked for ri in ris)]
        if len(below_rows) < 2 or len(below_rows) < len(data):
            continue                         # some matched rows are not under this heading
        labels = []
        for m in range(len(head)):
            c = mapping.get(m)
            source_label = heading[c].strip() if c is not None and c < len(heading) else None
            observed = []
            if promoted:
                for row in rows[:stacked + 1]:
                    value = row[m].strip()
                    if value and (not observed or key(value) != key(observed[-1])):
                        observed.append(value)
            literal = ' '.join(observed)
            label = source_label if source_label is not None else head[m]
            # The source may omit a printed label or extract it differently.
            # Promote observed text without loss; only a compatible proved
            # source label can expand a clipped printed heading.
            # A printed prefix alone must not select a hidden empty column
            # and expand its name. Preserve the observed heading unless the
            # data also supports that column (exact/prefix cell evidence).
            body_support = c is not None and any(
                key(rows[j][m]) and any(key(rows[j][m]) == key(table[ri][c])
                    or _clipped(key(rows[j][m]), key(table[ri][c])) for ri in ris)
                for j, ris in data.items()) if literal else False
            if literal and not (source_label is not None and (literal == source_label or (
                    bool(key(literal)) and (key(literal) == key(source_label)
                                           or (body_support and _clipped(key(literal), key(source_label))))))):
                label = literal
            labels.append(label)
        named = sum(bool(label) and not re.fullmatch(r'Column \d+', label) for label in labels)
        if named < max(2, len(labels) / 2):
            continue
        prefix = table_markdown([labels]).splitlines()
        kept = data_lines[1 + stacked:] if promoted else data_lines
        if not kept:
            continue
        block.text = block.markdown = '\n'.join([*prefix, *kept])
        block.lines = None
        block.line_geometry = 'unavailable'
        if block.source is not None:
            block.source.ref = (block.source.ref or '') + f';carried-header:table:{ti}:row:{h}'
        changed += 1
    return changed
