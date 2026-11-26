"""Locate borderless sheet cells using printed rows and source column identities.

Source strings constrain the column assignment; they never supply output text.
A clipped suffix may be absent, but every visible glyph must have an owner, two
complete cells must anchor the row, and competing column assignments are refused.
"""
import re
from collections import Counter, defaultdict
from functools import lru_cache
from types import SimpleNamespace
from statistics import median
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .source_tables import _cell_text_key, _header_row
from .textlayer import Fragment, build_text


# A spreadsheet number as printed: optional sign, currency or parenthesis, digits with
# separators, optional percent or closing parenthesis.
_NUMBER = re.compile(r'[+\-\u2212(]?[$\u20ac\u00a3\u00a5\u20a9]?\s*\d[\d,.\s]*%?\)?')


class OwnedBoxes(dict):
    """Cell geometry with the actual PDF characters that proved each assignment.

    A rectangle cannot identify a glyph when wrapped labels overlap another row.
    Consumers must keep these identities instead of cropping the geometry again.
    """
    def __init__(self, boxes, glyphs, parts):
        super().__init__(boxes)
        self.glyphs = glyphs
        self.row_y = median(getattr(p, '_anchor_y', (p.y0 + p.y1) / 2) for p in parts)
        self.line_height = median(getattr(p, '_line_height', p.y1 - p.y0) for p in parts)


@lru_cache(maxsize=8192)
def _rounded_spellings(key):
    """Possible finite decimal renderings; always emit the printed glyphs themselves.

    Rounding is not a complete-cell anchor. Other exact cells must prove the row,
    and competing numeric/column assignments remain ambiguous in the matcher.
    """
    if not re.fullmatch(r'[+\-\u2212]?\d+\.\d{2,}', key):
        return ()
    try:
        value = Decimal(key.replace('\u2212', '-'))
        places = len(key.rsplit('.', 1)[1])
        if places > 32 or len(key) > 64:
            return ()
        values = tuple(str(value.quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP))
                       for n in range(places - 1, 0, -1))
        if key.startswith('\u2212'):
            return tuple(text.replace('-', '\u2212', 1) for text in values)
        if key.startswith('+'):
            return tuple('+' + text for text in values)
        return values
    except InvalidOperation:
        return ()


class PrintedRows:
    def __init__(self, tables):
        self.tables = tables
        self.rows = {}
        self.letters = {}
        self.row_counts = [sum(any(cell.strip() for cell in row) for row in table) for table in tables]
        self.index = defaultdict(set)
        self.prefixes = defaultdict(set)
        self.cell_keys = {}
        self.cell_index = defaultdict(set)
        for ti, table in enumerate(tables):
            for ri, row in enumerate(table):
                parts = [(c, _cell_text_key(value)) for c, value in enumerate(row) if value.strip()]
                if len(parts) > 128 or sum(len(key) for _, key in parts) > 8192:
                    continue
                self.rows[ti, ri] = parts
                keys = [''] * len(row)
                for column, key in parts:
                    keys[column] = key
                self.cell_keys[ti, ri] = tuple(keys)
                for column, key in parts:
                    if len(key) >= 2 or key.isdecimal():
                        self.cell_index[len(row), column, key].add((ti, ri))
                self.letters[ti, ri] = frozenset(ch for _, key in parts for ch in key if ch.isalpha())
                for _, key in parts:
                    if len(key) >= 2:
                        self.index[key].add((ti, ri))
                        self.prefixes[key[:2]].add(key)
        self.complete_rows = set(self.cell_keys.values())

    def overflow_boundaries(self, keys):
        """Propose source-grounded partitions of existing printed text.

        Two other columns must identify the record. A source row agreeing with
        the physical split is competing evidence, so ambiguous ownership stays
        unchanged. The left word may be clipped; the right value must be complete.
        Geometry must separately prove the moved letters continue the same
        printed word; no source string is emitted here.
        """
        if tuple(keys) in self.complete_rows:
            return {}
        votes = Counter(identity for column, key in enumerate(keys)
                        for identity in self.cell_index.get((len(keys), column, key), ()))
        candidates = [identity for identity, count in votes.items() if count >= 2]
        if len(candidates) > 64:
            return {}
        proposals = defaultdict(set)
        for identity in candidates:
            source = self.cell_keys[identity]
            for column in range(len(keys) - 1):
                left, right = keys[column:column + 2]
                if len(left) < 3 or not right or not any(ch.isalpha() for ch in left):
                    continue
                anchors = [key for i, key in enumerate(keys) if i not in (column, column + 1)
                           and key == source[i] and (len(key) >= 2 or key.isdecimal())]
                if len(anchors) < 2 or not any(any(ch.isalpha() for ch in key) for key in anchors):
                    continue
                a, b = source[column:column + 2]
                if a == left and b == right:
                    proposals[column].add(0)
                elif len(right) > len(b) and right.endswith(b) and a.startswith(left + right[:len(right) - len(b)]):
                    # A neighbouring value may clip the tail of the overflowing
                    # word. Reassign only its existing prefix, never the unseen
                    # source suffix; the neighbour must itself be fully printed.
                    proposals[column].add(len(right) - len(b))
        result = {c: next(iter(values)) for c, values in proposals.items()
                  if len(values) == 1 and 0 not in values}
        return {c: n for c, n in result.items() if c - 1 not in result and c + 1 not in result}

    def row_overflow_boundaries(self, rows):
        """Find complete source cells whose wrapped tail spills into the next row."""
        result = []
        for ri, (upper, lower) in enumerate(zip(rows, rows[1:])):
            if len(upper) != len(lower):
                continue
            votes = Counter(identity for column, key in enumerate(upper)
                            for identity in self.cell_index.get((len(upper), column, key), ()))
            candidates = [identity for identity, count in votes.items() if count >= 2]
            if len(candidates) > 64:
                continue
            proposals = defaultdict(set)
            for ti, si in candidates:
                a = self.cell_keys[ti, si]
                b = self.cell_keys.get((ti, si + 1))
                if b is None or len(b) != len(lower):
                    continue
                for column, (left, right) in enumerate(zip(upper, lower)):
                    if len(left) < 3 or not right or not any(ch.isalpha() for ch in left):
                        continue
                    anchors = [[key for i, key in enumerate(actual) if i != column and key == source[i]
                                and (len(key) >= 2 or key.isdecimal())]
                               for actual, source in ((upper, a), (lower, b))]
                    if any(len(keys) < 2 or not any(any(ch.isalpha() for ch in k) for k in keys)
                           for keys in anchors):
                        continue
                    if a[column] == left and b[column] == right:
                        proposals[column].add(0)
                    elif (b[column] and a[column].startswith(left) and len(a[column]) > len(left)
                          and a[column] + b[column] == left + right):
                        proposals[column].add(len(a[column]) - len(left))
            result.extend((ri, c, next(iter(values))) for c, values in proposals.items()
                          if len(values) == 1 and 0 not in values)
        return result

    @staticmethod
    def _match(parts, line, boundaries=None):
        calls = 0

        @lru_cache(None)
        def walk(i, start):
            nonlocal calls
            calls += 1
            if calls > 20000:
                raise ValueError('printed row match budget')
            if start == len(line):
                return (0, 0, 0), {()}
            if i == len(parts):
                return None
            best = walk(i + 1, start)
            column, key = parts[i]
            end = start
            while end < len(line) and end - start < len(key) and line[end] == key[end - start]:
                end += 1
            lengths = [len(key)] if end - start == len(key) else []
            # Only word-like cells may be visibly truncated. Numeric signs,
            # decimal points and percentages must match as complete values.
            if end - start >= 3 and any(c.isalpha() for c in key):
                lengths.extend(range(min(end - start, len(key) - 1), 2, -1))
            # Spreadsheet renderers print hashes when a numeric value does not
            # fit its column. Keep those literal glyphs; never substitute the
            # hidden number. Other complete cells still have to anchor this row.
            hashes = len(line[start:]) - len(line[start:].lstrip('#'))
            if hashes >= 3 and any(c.isdigit() for c in key):
                lengths.append(hashes)
            lengths.extend(len(value) for value in _rounded_spellings(key)
                           if line.startswith(value, start)
                           and boundaries is not None
                           and start in boundaries and start + len(value) in boundaries)
            for count in lengths:
                child = walk(i + 1, start + count)
                if child is None:
                    continue
                full = line[start:start + count] == key
                score = (child[0][0] + (count if full else 0), child[0][1] + full,
                         child[0][2] - (not full))
                paths = {((column, start, start + count),) + path for path in child[1]}
                rank = score[0], score[2]
                if best is None or rank > (best[0][0], best[0][2]):
                    best = score, paths
                elif rank == (best[0][0], best[0][2]):
                    best = score, set(sorted(best[1] | paths)[:2])
            return best

        try:
            return walk(0, 0)
        except ValueError:
            return None

    def locate(self, parts, native, *, minimum_anchors=2, ordered_cells=False):
        if ordered_cells:
            chars = [ch for part in sorted(parts, key=lambda p: p.x0) for ch in part.chars]
        else:
            _, _, chars = build_text(parts, native.index, native.width)
        # build_text may replace a raised digit with its Unicode superscript.
        # Match the normalized spelling but preserve the native glyph identity:
        # downstream cell ownership must refer to the original page characters.
        originals = defaultdict(list)
        for part in parts:
            for ch in part.chars:
                originals[ch.x0, ch.y0, ch.x1, ch.y1, _cell_text_key(ch.c)].append(ch)
        native_ids = {id(ch) for part in parts for ch in part.chars}
        indexed = []
        for ch in chars:
            letters = _cell_text_key(ch.c)
            if not letters:
                continue
            owner = ch
            if id(ch) not in native_ids:
                choices = originals[ch.x0, ch.y0, ch.x1, ch.y1, letters]
                if len(choices) != 1:
                    return None
                owner = choices[0]
            indexed.extend((letter, owner) for letter in letters)
        line = ''.join(letter for letter, _ in indexed)
        if not line or len(line) > 2048:
            return None
        hits = {key for i in range(len(line) - 1) for key in self.prefixes.get(line[i:i + 2], ())
                if line.startswith(key, i)}
        votes = Counter(identity for key in hits for identity in self.index[key])
        letters = {ch for ch in line if ch.isalpha()}
        # Every printed letter must come from a source cell (possibly its
        # clipped prefix). Unrelated numeric rows often share short values;
        # they must not exhaust the search budget before a real record is tried.
        candidates = [identity for identity, count in votes.items() if count >= minimum_anchors
                      and letters <= self.letters[identity]]
        if len(candidates) > 64:
            # Repeated years/names can nominate hundreds of records. Before
            # spending the bounded partition search, reject rows that cannot
            # spell the visible glyphs in source-column order at all. This is
            # only a necessary condition: exact cell boundaries, signs, and
            # competing assignments are still checked below. Printed overflow
            # hashes may stand for an unseen number, so do not constrain them.
            visible = line.replace('#', '')
            def possible(identity):
                source = ''.join(key for _, key in self.rows[identity])
                offset = 0
                for letter in visible:
                    found = source.find(letter, offset)
                    if found < 0:
                        return False
                    offset = found + 1
                return True
            candidates = [identity for identity in candidates if possible(identity)]
            if len(candidates) > 64:
                return None
        # A rounded number is one physical glyph run. Source columns omitted by
        # printing cannot claim its trailing digits as separate exact values.
        boundaries = {0, len(indexed)}
        for at in range(1, len(indexed)):
            left, right = indexed[at - 1][1], indexed[at][1]
            if left is right:
                continue
            gap = right.x0 - left.x1
            if (gap > .15 * min(left.size, right.size) / native.width
                    or abs(left.cy - right.cy) > .5 * min(left.y1 - left.y0, right.y1 - right.y0)):
                boundaries.add(at)
        best, assignments = None, {}
        for ti, ri in candidates:
            result = self._match(self.rows[ti, ri], line, boundaries)
            if result is None or result[0][1] < minimum_anchors:
                continue
            score, paths = result
            # NFKC/case folding can expand one glyph (for example ß -> ss).
            # Two source cells must never share that one printed character.
            paths = {path for path in paths if all(end == len(indexed)
                     or indexed[end - 1][1] is not indexed[end][1] for _, _, end in path)}
            if not paths:
                continue
            # More cells are not stronger evidence: "ab" in one cell and
            # "a" / "b" in two cells must remain competing assignments.
            rank = score[0], score[2]
            if best is None or rank > best:
                best, assignments = rank, {}
            if rank == best:
                for path in paths:
                    assignments.setdefault((ti, path), ri)
        if len(assignments) != 1:
            return None
        (ti, path), ri = next(iter(assignments.items()))
        boxes, centers, owners = {}, {}, {}
        for column, start, end in path:
            glyphs = [ch for _, ch in indexed[start:end]]
            owners[column] = tuple({id(ch): ch for ch in glyphs}.values())
            centers[column] = (min(ch.cx for ch in glyphs), max(ch.cx for ch in glyphs))
            boxes[column] = (min(ch.x0 for ch in glyphs), min(ch.y0 for ch in glyphs),
                             max(ch.x1 for ch in glyphs), max(ch.y1 for ch in glyphs))
        # Printed glyph extents can overlap across adjacent source cells.
        # The uniquely matched character sequence still owns each glyph. Split
        # at the gap between their centers, or refuse physically interleaved
        # assignments instead of ejecting letters into detached paragraphs.
        for (left, _, _), (right, _, _) in zip(path, path[1:]):
            a, b = boxes[left], boxes[right]
            if a[2] <= b[0]:
                continue
            if centers[left][1] >= centers[right][0]:
                return None
            cut = (centers[left][1] + centers[right][0]) / 2
            boxes[left] = (a[0], a[1], min(a[2], cut), a[3])
            boxes[right] = (max(b[0], cut), b[1], b[2], b[3])
        return ti, ri, OwnedBoxes(boxes, owners, parts)

    def _wrapped_parts(self, native):
        """Recognize complete source labels split into PDF lines before grouping rows.

        Only aligned lines from the same PDF block may join, and their full spelling
        must be one source cell. Independently complete cells cannot be swallowed.
        Source-matched companion cells choose the physical line that anchors the
        record. Top, middle and bottom alignment are possible; ambiguous anchors
        leave the original fragments intact. Glyph positions never change.
        """
        parts = native.fragments
        bins = defaultdict(list)
        for part in parts:
            bins[part.block, round(part.x0 * native.width / 2)].append(part)
        proposals = []
        for first in parts:
            text = _cell_text_key(''.join(ch.c for ch in first.chars))
            if len(text) < 3 or text in self.index or not any(c.isalpha() for c in text):
                continue
            possible = [k for k in self.prefixes.get(text[:2], ()) if k.startswith(text)]
            if not possible:
                continue
            chain = [first]
            for _ in range(3):
                last = chain[-1]
                slot = round(first.x0 * native.width / 2)
                nearby = [p for b in range(slot - 1, slot + 2) for p in bins[first.block, b]
                          if abs(p.x0 - first.x0) * native.width <= 2
                          and .5 * (last.y1 - last.y0) <= p.y0 - last.y0 <= 1.5 * (last.y1 - last.y0)]
                if not nearby:
                    break
                nearest = min(p.y0 for p in nearby)
                nearby = [p for p in nearby if abs(p.y0 - nearest) * native.height < 1]
                if len(nearby) != 1:
                    break
                part = nearby[0]
                tail = _cell_text_key(''.join(ch.c for ch in part.chars))
                if not tail or tail in self.index:
                    break
                text += tail
                possible = [k for k in possible if k.startswith(text)]
                if not possible:
                    break
                chain.append(part)
                if text in self.index:
                    proposals.append(chain)
                    break
        uses = Counter(id(p) for chain in proposals for p in chain)
        chains = [chain for chain in proposals if all(uses[id(p)] == 1 for p in chain)]
        proposed = {id(p) for chain in chains for p in chain}
        companions = [p for p in parts if id(p) not in proposed]
        joined, consumed = [], set()
        for chain in chains:
            part = Fragment([ch for p in chain for ch in p.chars], block=chain[0].block)
            anchors = []
            for physical in chain:
                y, height = (physical.y0 + physical.y1) / 2, physical.y1 - physical.y0
                aligned = [other for other in companions
                           if abs((other.y0 + other.y1) / 2 - y)
                           <= .2 * min(height, other.y1 - other.y0)]
                part._anchor_y, part._line_height = y, height
                # The label alone cannot establish which line its numeric record
                # belongs to. At least one other exact cell must prove the row.
                if aligned and self.locate([part, *aligned], native, ordered_cells=True):
                    anchors.append((y, height))
            if len(anchors) != 1:
                continue
            part._anchor_y, part._line_height = anchors[0]
            joined.append(part)
            consumed.update(id(p) for p in chain)
        return [p for p in parts if id(p) not in consumed] + joined

    def _number(self, ti, ri, c):
        row = self.tables[ti][ri] if ti < len(self.tables) and ri < len(self.tables[ti]) else ()
        return c < len(row) and bool(_NUMBER.fullmatch(row[c].strip()))

    def grids(self, native, occupied):
        lines = []
        anchor = lambda f: getattr(f, '_anchor_y', (f.y0 + f.y1) / 2)
        height = lambda f: getattr(f, '_line_height', f.y1 - f.y0)
        for part in sorted(self._wrapped_parts(native), key=lambda f: (anchor(f), f.x0)):
            prev = lines[-1][0] if lines else None
            if prev and abs(anchor(prev) - anchor(part)) <= .2 * min(height(prev), height(part)):
                lines[-1].append(part)
            else:
                lines.append([part])
        runs, run = [], []
        def flush():
            if len(run) >= 2 or (len(run) == 1 and self.row_counts[run[0][0]] == 1 and len(run[0][2]) >= 3):
                runs.append(list(run))
            run.clear()
        for parts in lines:
            located = self.locate(parts, native, ordered_cells=True)
            if located is None and len(run) >= 2:
                # A side note at the same baseline must not detach a table row.
                # Two preceding grounded rows establish this table's extent;
                # a unique clipped label plus its complete value may then stay
                # in those same aligned columns. The excluded note stays native.
                left = min(b[0] for _, _, boxes in run for b in boxes.values()) - 1 / native.width
                right = max(b[2] for _, _, boxes in run for b in boxes.values()) + 1 / native.width
                cropped = [Fragment(chars) for part in parts
                           if (chars := [ch for ch in part.chars if left <= ch.cx <= right])]
                candidate = self.locate(cropped, native, minimum_anchors=1)
                if candidate and candidate[0] == run[-1][0] and candidate[2].keys() == run[-1][2].keys():
                    located = candidate
            if located and any(min(box[2], other[2]) > max(box[0], other[0])
                               and min(box[3], other[3]) > max(box[1], other[1])
                               for box in located[2].values() for other in occupied):
                located = None
            if located and run:
                ti, ri, boxes = located
                last_ti, last_ri, previous = run[-1]
                shared = boxes.keys() & previous.keys()
                y = boxes.row_y
                last_y = previous.row_y
                height = boxes.line_height
                # Right-aligned numbers in an accounting format keep room for a closing
                # parenthesis, so their right edge sits a few points inside the column's
                # other numbers. Allow that much (a third of a row height) only when both
                # source cells are numbers; text and left edges keep the 2pt rule.
                padding = max(2, .3 * height * native.height)
                aligned = sum(abs(boxes[c][0] - previous[c][0]) * native.width <= 2
                              or abs(boxes[c][2] - previous[c][2]) * native.width
                              <= (padding if self._number(ti, ri, c) and self._number(last_ti, last_ri, c) else 2)
                              for c in shared)
                if ti != last_ti or aligned < 2 or not 0 < y - last_y <= 2.5 * height:
                    flush()
            if located:
                run.append(located)
            else:
                flush()
        flush()
        grids = []
        for run in runs:
            columns = tuple(sorted({c for _, _, boxes in run for c in boxes}))
            header = self._header(run, columns, native)
            if header:
                run = [header, *run]
            centers = [sum((b[1] + b[3]) / 2 for b in boxes.values()) / len(boxes)
                       for _, _, boxes in run]
            cells = []
            for row, (_, _, boxes) in enumerate(run):
                # Font ascent/descent boxes can overlap tightly printed rows.
                # Divide at the intervening baselines' midpoints, so every
                # printed glyph has exactly one physical row owner.
                bottom = (centers[row] + centers[row + 1]) / 2 if row + 1 < len(run) else 1.
                top = (centers[row - 1] + centers[row]) / 2 if row else 0.
                cells.append([None if c not in boxes else (boxes[c][0], max(top, boxes[c][1]),
                                                          boxes[c][2], min(bottom, boxes[c][3])) for c in columns])
            ownership = {}
            for row_index, (_, _, boxes) in enumerate(run):
                for column, glyphs in getattr(boxes, 'glyphs', {}).items():
                    ci = columns.index(column)
                    cells[row_index][ci] = boxes[column]
                    ownership[tuple(v * (native.width if i % 2 == 0 else native.height)
                                    for i, v in enumerate(boxes[column]))] = glyphs
            grids.append(SimpleNamespace(source_identity=(run[0][0], columns), glyph_owners=ownership,
                printed_header=bool(header),
                source_min_row=min(ri for _, ri, _ in run), rows=[SimpleNamespace(cells=[
                    tuple(v * (native.width if i % 2 == 0 else native.height) for i, v in enumerate(box))
                    if box else None for box in row]) for row in cells]))
        return grids

    def _header(self, run, columns, native):
        """Attach a wrapped, physically present header immediately above a run.

        Each header cell must end its own printed column band. A merged heading
        may span following empty source columns; no source-only label is added.
        """
        ti = run[0][0]
        first = min(ri for _, ri, _ in run)
        bounds = {c: (min(boxes[c][0] for _, _, boxes in run if c in boxes),
                      max(boxes[c][2] for _, _, boxes in run if c in boxes)) for c in columns}
        top = min(b[1] for b in run[0][2].values())
        height = max(b[3] - b[1] for b in run[0][2].values())
        for ri in range(first - 1, max(-1, first - 4), -1):
            fields = [(c, key) for c, key in self.rows.get((ti, ri), ()) if c in columns]
            textual = all(any(ch.isalpha() for ch in key) for _, key in fields)
            indexed = (not textual and _header_row(self.tables[ti], columns) is None
                       and _header_row(self.tables[ti], columns, allow_indexed=True) == ri)
            if len(fields) < 2 or not (textual or indexed):
                continue
            if indexed:
                # Right-aligned numbers make data glyphs narrower than their
                # cells. A left-aligned heading can cross the midpoint between
                # those glyphs. Match its whole printed row first, then require
                # each identified label's centre to stay in its data column.
                parts = [Fragment(chars) for fragment in native.fragments
                         if (chars := [ch for ch in fragment.chars if top - 4 * height <= ch.cy < top])]
                candidate = self.locate(parts, native)
                if candidate and candidate[:2] == (ti, ri) and set(candidate[2]) == {c for c, _ in fields}:
                    aligned = True
                    for c, box in candidate[2].items():
                        i = columns.index(c)
                        left = (bounds[columns[i - 1]][1] + bounds[c][0]) / 2 if i else 0.
                        right = (bounds[c][1] + bounds[columns[i + 1]][0]) / 2 if i + 1 < len(columns) else 1.
                        if not left <= (box[0] + box[2]) / 2 < right:
                            aligned = False
                            break
                    if aligned:
                        return candidate
            boxes = {}
            for i, (c, key) in enumerate(fields):
                left_index = columns.index(c)
                right_index = columns.index(fields[i + 1][0]) if i + 1 < len(fields) else len(columns)
                left = (bounds[columns[left_index - 1]][1] + bounds[c][0]) / 2 if left_index else 0.
                right = (bounds[columns[right_index - 1]][1] + bounds[columns[right_index]][0]) / 2 \
                    if right_index < len(columns) else 1.
                fragments = []
                for fragment in native.fragments:
                    chars = [ch for ch in fragment.chars if left <= ch.cx < right
                             and top - 4 * height <= ch.cy < top]
                    if chars:
                        fragments.append(Fragment(chars))
                _, _, chars = build_text(fragments, native.index, native.width)
                indexed = [(letter, ch) for ch in chars for letter in _cell_text_key(ch.c)]
                text = ''.join(letter for letter, _ in indexed)
                if not text.endswith(key):
                    break
                start = len(indexed) - len(key)
                if start:
                    previous, first_glyph = indexed[start - 1][1], indexed[start][1]
                    # A matching suffix of "Gross Amount" is not a separately
                    # printed "Amount" heading. Earlier title lines may remain
                    # above the header, but its own line must start here.
                    if previous is first_glyph or abs(previous.cy - first_glyph.cy) <= .25 * max(
                            previous.y1 - previous.y0, first_glyph.y1 - first_glyph.y0):
                        break
                glyphs = [ch for _, ch in indexed[-len(key):]]
                boxes[c] = (min(ch.x0 for ch in glyphs), min(ch.y0 for ch in glyphs),
                            max(ch.x1 for ch in glyphs), max(ch.y1 for ch in glyphs))
            if len(boxes) == len(fields):
                return ti, ri, boxes
        return None
