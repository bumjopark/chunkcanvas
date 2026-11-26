"""Bounded, shared column evidence for printed continuation tables.

Every printed row must fit the same increasing source-column projection. Row
occurrences are bitmaps, built once for a document, rather than a new projected
copy of the entire source table for each hypothesis. An incomplete search never
supplies a heading.
"""
import logging
import re

_LOG = logging.getLogger(__name__)
_TABLE_BUDGET = 300_000
from bisect import bisect_left


class SourceIndex:
    def __init__(self, tables, key, *, keep_empty=False, allow_missing=False):
        self.allow_missing = allow_missing
        self.tables, self.key = tables, key
        self.keep_empty = keep_empty
        self.raw_index = None
        self.rows = []
        self.original_rows = []
        self.columns = []
        self.words = []
        self.ends = []
        self.rounded = []
        self.numeric = []
        self.empty = []
        self.results = {}
        self.identities = {}
        self.lookups = {}
        self.remaining = 20_000_000
        self.exhausted = False
        self.table_remaining = None
        self.limited_tables = 0
        normalized = {}
        for table in tables:
            width = min(map(len, table), default=0)
            self.spend(len(table) * width)
            rows, original_rows, columns = [], [], [{} for _ in range(width)]
            for ri, row in enumerate(table):
                values = []
                for cell in row[:width]:
                    if cell not in normalized:
                        normalized[cell] = key(cell)
                    values.append(normalized[cell])
                # Empty source rows contain no printed content. Keep their
                # original positions for section checks, but not as missing
                # records in a consecutive run of visible data.
                if not any(values) and not keep_empty:
                    continue
                position = len(rows)
                for c, value in enumerate(values):
                    columns[c][value] = columns[c].get(value, 0) | (1 << position)
                rows.append(tuple(values))
                original_rows.append(ri)
            self.original_rows.append(original_rows)
            self.rows.append(rows)
            self.columns.append(columns)
            self.words.append([sorted(v for v in column if v) for column in columns])
            # Only a genuinely empty body can stand for unextracted image or
            # formula text. A repeated value is still evidence, even if it is
            # the column's sole distinct value and its heading is blank.
            self.empty.append([all(not row[c] for row in rows[1:]) for c in range(width)])
            self.ends.append([None] * width)
            self.rounded.append([None] * width)
            numeric = []
            for column in columns:
                mask = 0
                for value, places in column.items():
                    if any(ch.isdigit() for ch in value) and not any(ch.isalpha() for ch in value):
                        mask |= places
                numeric.append(mask)
            self.numeric.append(numeric)

    def spend(self, units=1, *, shared=False):
        # Once-per-document indexes consume the document budget, not the
        # search allowance of whichever printed table happens to arrive first.
        if not shared and self.table_remaining is not None:
            self.table_remaining -= units
            if self.table_remaining < 0:
                raise OverflowError('table heading alignment budget')
        self.remaining -= units
        if self.remaining < 0:
            self.exhausted = True
            raise OverflowError('document heading alignment budget')

    def masks(self, ti, c, value, *, suffix=False):
        identity = ti, c, value, suffix
        if identity in self.lookups:
            return self.lookups[identity]
        self.spend()
        column = self.columns[ti][c]
        all_rows = (1 << len(self.rows[ti])) - 1
        if not value:
            result = all_rows, 0, 0
        else:
            exact = column.get(value, 0)
            strict = exact
            partial = 0
            if len(value) >= 3 and any(ch.isalpha() for ch in value):
                words = self.words[ti][c]
                at = bisect_left(words, value)
                while at < len(words) and words[at].startswith(value):
                    self.spend()
                    strict |= column[words[at]]
                    at += 1
                # A page may start in the middle of a vertically split cell.
                # Only the leading printed row may use a source word suffix.
                if suffix:
                    if self.ends[ti][c] is None:
                        self.spend(len(words), shared=True)
                        self.ends[ti][c] = sorted(word[::-1] for word in words)
                    endings = self.ends[ti][c]
                    needle = value[::-1]
                    at = bisect_left(endings, needle)
                    while at < len(endings) and endings[at].startswith(needle):
                        self.spend()
                        partial |= column[endings[at][::-1]]
                        at += 1
            # An image/formula may have no extracted source string. It cannot
            # anchor a row; two other complete records must prove the columns.
            supported = strict | partial
            compatible = supported
            if self.empty[ti][c] or self.allow_missing:
                compatible |= column.get('', 0)
            if len(value) >= 3 and set(value) == {'#'}:
                compatible |= self.numeric[ti][c]
            if re.fullmatch(r'[+\-\u2212]?\d+\.\d+', value):
                from .sheet_rows import _rounded_spellings
                if self.rounded[ti][c] is None:
                    variants = {}
                    for word in self.words[ti][c]:
                        self.spend(shared=True)
                        for spelling in _rounded_spellings(word):
                            self.spend(shared=True)
                            variants[spelling] = variants.get(spelling, 0) | column[word]
                    self.rounded[ti][c] = variants
                # A complete cell spelling may be rounded, but that supplies
                # no exact anchor and never substitutes the source number.
                partial = self.rounded[ti][c].get(value, 0)
                compatible |= partial
                supported |= partial
            result = compatible, strict, supported
        if len(self.lookups) >= 8192:
            self.lookups.clear()
        self.lookups[identity] = result
        return result


def _projection(printed, ti, columns, index, *, evidence=False, require_witnesses=True):
    """Full consecutive explanations and close competing explanations.

    One changed cell or exchanged neighbouring pair is never affirmative
    evidence. It is a reason to withhold a conflicting column label: a short
    count sequence can accidentally fit another source column exactly.
    """
    all_rows = (1 << len(index.rows[ti])) - 1
    cache, exact_masks, close_masks, strong_masks, possible_masks, evidenced_masks = {}, [], [], [], [], []
    for j, row in enumerate(printed):
        identity = row, j == 0
        if identity not in cache:
            allowed, one_error, one_anchor, two_anchors = all_rows, all_rows, 0, 0
            one_possible, two_possible, one_evidence, one_strict = 0, 0, 0, 0
            evidenced = all_rows
            for c, value in zip(columns, row):
                index.spend()
                compatible, strict, supported = index.masks(ti, c, value, suffix=j == 0)
                if require_witnesses and not any(row):
                    compatible = index.columns[ti][c].get('', 0)
                if value:
                    evidenced &= supported
                numeric = any(ch.isdigit() for ch in value) and not any(ch.isalpha() for ch in value)
                one_error = (one_error & compatible) | (allowed if numeric else 0)
                possible = strict | (index.numeric[ti][c] if numeric else 0)
                two_possible |= one_possible & possible
                one_possible |= possible
                one_evidence |= supported | (index.numeric[ti][c] if numeric else 0)
                allowed &= compatible
                two_anchors |= one_strict & strict
                one_strict |= strict
                one_anchor |= supported
            exact = allowed & one_anchor if any(row) else allowed
            close = one_error & one_evidence if any(row) else one_error
            cache[identity] = exact, close, allowed & two_anchors, two_possible, evidenced
        exact, close, strong, possible, evidenced = cache[identity]
        exact_masks.append(exact)
        close_masks.append(close)
        strong_masks.append(strong)
        possible_masks.append(possible)
        evidenced_masks.append(evidenced)
    full, one_error = all_rows, all_rows
    prefixes = [all_rows]
    for j, (exact, close) in enumerate(zip(exact_masks, close_masks)):
        index.spend(2)
        one_error = (one_error & (exact >> j)) | (full & (close >> j))
        full &= exact >> j
        prefixes.append(full)
    # Sparse/image-bearing records may be explained by one visible anchor,
    # but at least two distinct records must each prove two source cells.
    def witnesses(masks, swapped=None):
        patterns = {}
        for j, row in enumerate(printed):
            offset = (j + 1 if j == swapped else j - 1 if swapped is not None and j == swapped + 1 else j)
            patterns[row] = patterns.get(row, 0) | (masks[j] >> offset)
            index.spend()
        one_strong, two_strong = 0, 0
        for possible in patterns.values():
            two_strong |= one_strong & possible
            one_strong |= possible
        return two_strong
    if require_witnesses:
        full &= witnesses(strong_masks)
    # Blank transcriptions cannot establish identity among several source
    # tables: a similarly filled form could win by hiding disagreeing text.
    # Within one source table they may align the known columns; naming an
    # unknown column still requires its own evidence or fixed full width.
    raw_complete = bool(full)
    # An entirely evidenced alignment is a witness in its own right. A
    # different occurrence with a blank cell cannot invalidate that witness.
    # Intersect all columns/rows at the SAME start before pooling evidence.
    evidenced_full = full
    for j, supported in enumerate(evidenced_masks):
        evidenced_full &= supported >> j
        index.spend()
    if len(index.tables) != 1:
        full = evidenced_full
    if require_witnesses:
        one_error &= witnesses(possible_masks)
    suffix = [all_rows] * (len(printed) + 1)
    for j in range(len(printed) - 1, -1, -1):
        suffix[j] = suffix[j + 1] & (exact_masks[j] >> j)
    swaps = []
    for j in range(len(printed) - 1):
        index.spend()
        starts = (prefixes[j] & (exact_masks[j + 1] >> j)
                  & (exact_masks[j] >> (j + 1)) & suffix[j + 2])
        if starts:
            if require_witnesses:
                starts &= witnesses(strong_masks, j)
            if starts:
                swaps.append((j, starts))
    if not one_error and not swaps:
        return None
    # Retain all possible source sections as well as column projections. A
    # nearby alternative section must not be hidden behind the exact winner.
    owners = {}
    support, missing_support = 0, 0
    for j in range(len(printed)):
        possible = one_error << j
        for at, starts in swaps:
            offset = j + 1 if j == at else j - 1 if j == at + 1 else j
            possible |= starts << offset
        if evidence:
            for m, c in enumerate(columns):
                # Even one wildcard-filled cell is not proof of its column.
                # Matching some other rows must not legitimize a blank neighbour.
                if printed[j][m] and index.columns[ti][c].get('', 0) & possible:
                    missing_support |= 1 << m
                if printed[j][m] and index.masks(ti, c, printed[j][m], suffix=j == 0)[2] & possible:
                    support |= 1 << m
                index.spend()
        places = set()
        while possible:
            index.spend()
            bit = possible & -possible
            places.add(index.original_rows[ti][bit.bit_length() - 1])
            possible ^= bit
        owners[j] = places
    support &= ~missing_support
    if evidenced_full:
        support |= sum(1 << m for m in range(len(columns)) if any(row[m] for row in printed))
    return (bool(full), owners, support, raw_complete) if evidence else (bool(full), owners)


def _column_windows(printed, ti, m, c, index, *, partial=False):
    """Possible source starts for one whole printed column, built once per block.

    Long sequences of small counts are informative together. Intersecting their
    shifted occurrence bitmaps before exploring column combinations avoids
    enumerating thousands of projections from one common-valued row.
    """
    size = len(index.rows[ti])
    if size < len(printed):
        return 0, 0, ()
    starts = (1 << (size - len(printed) + 1)) - 1
    exact, close, swapped = starts, starts, 0
    masks, prefixes = [], [starts]
    for j, row in enumerate(printed):
        value = row[m]
        compatible, _, _ = index.masks(ti, c, value, suffix=j == 0)
        if not partial and not any(row):
            compatible = index.columns[ti][c].get('', 0)
        mask = compatible >> j
        numeric = any(ch.isdigit() for ch in value) and not any(ch.isalpha() for ch in value)
        before = exact
        close = (close & mask) | (exact if numeric else 0)
        exact &= mask
        # A union is sufficient only for early rejection. The exact per-pair
        # swap masks below still require every column to exchange the same
        # two rows. Keep the unfinished last pair through `before`.
        swapped &= mask
        if j:
            swapped |= prefixes[j - 1] & (compatible >> (j - 1)) & (masks[-1] >> j)
        index.spend(7)
        if not (exact or close or swapped or before):
            return 0, 0, ()
        prefixes.append(exact)
        masks.append(compatible)
    suffixes = [starts] * (len(printed) + 1)
    for j in range(len(printed) - 1, -1, -1):
        suffixes[j] = suffixes[j + 1] & (masks[j] >> j)
        index.spend()
    swaps = tuple(prefixes[j] & (masks[j + 1] >> j) & (masks[j] >> (j + 1)) & suffixes[j + 2]
                  for j in range(len(printed) - 1))
    index.spend(len(swaps))
    return exact, close, swaps


def _grouped_projections(printed, ti, index, *, partial=False):
    """Enumerate evidence classes, retaining every possible column in each class.

    Equal columns (often dozens of zero/empty form columns) give identical row
    evidence. Their increasing paths are merged by evidence prefix and last
    column. Prefix unions retain exactly the paths ending before the next
    column, without enumerating every combination or choosing one owner.
    """
    width, n = len(printed[0]), len(index.columns[ti])
    size = len(index.rows[ti])
    if width > n or size < len(printed):
        return
    starts = (1 << (size - len(printed) + 1)) - 1
    groups = {(): (starts, starts, (starts,) * (len(printed) - 1), {-1: ((), ())})}
    windows, identities = {}, {}
    for at in range(width):
        following = {}
        for prefix, (exact, close, swaps, paths) in groups.items():
            ordered = iter(sorted(paths.items()))
            pending = next(ordered, None)
            choices, representative = [0] * at, None
            for c in range(min(paths) + 1, n - (width - at) + 1):
                while pending is not None and pending[0] < c:
                    _, (chosen, alternatives) = pending
                    if representative is None:
                        representative = chosen
                    for m, options in enumerate(alternatives):
                        choices[m] |= options
                    index.spend(at + 1)
                    pending = next(ordered, None)
                if (at, c) not in windows:
                    window = _column_windows(printed, ti, at, c, index, partial=partial)
                    if not (window[0] or window[1] or any(window[2])):
                        windows[at, c] = None, window
                        continue
                    cells = []
                    for j, row in enumerate(printed):
                        compatible, strict, supported = index.masks(ti, c, row[at], suffix=j == 0)
                        if not partial and not any(row):
                            compatible = index.columns[ti][c].get('', 0)
                        numeric = index.numeric[ti][c] if (any(ch.isdigit() for ch in row[at])
                                  and not any(ch.isalpha() for ch in row[at])) else 0
                        # Overflow hashes can be compatible with either a number
                        # or a blank source cell. Only the latter invalidates
                        # name evidence, so its positions belong in the class.
                        cells.append((compatible, strict, supported, numeric, index.columns[ti][c].get('', 0)))
                    signature = tuple(cells)
                    index.spend(len(cells))
                    identity = identities.setdefault(signature, len(identities))
                    windows[at, c] = identity, window
                identity, (full, near, exchanged) = windows[at, c]
                full_next = exact & full
                close_next = (close & full) | (exact & near)
                swap_next = tuple(a & b for a, b in zip(swaps, exchanged))
                index.spend(3 + len(swaps))
                if full_next or close_next or any(swap_next):
                    group = following.setdefault((*prefix, identity), (full_next, close_next, swap_next, {}))
                    group[3][c] = ((*representative, c), (*choices, 1 << c))
        groups = following
        if not groups:
            return
    for _, _, _, paths in groups.values():
        options = [0] * width
        for _, alternatives in paths.values():
            for m, mask in enumerate(alternatives):
                options[m] |= mask
            index.spend(width)
        yield next(iter(paths.values()))[0], options


class _Ambiguous(Exception):
    pass


def _unique_source(printed, ti, support, index):
    """Unknown cells cannot select a source: compare only the supported columns.

    If another table can explain even this smaller view, including a nearby
    numeric/swap explanation, the missing cells cannot break that tie. This
    also preserves full-width image/formula tables identified by their text.
    """
    if support.bit_count() < 2:
        return False
    reduced = tuple(tuple(v for m, v in enumerate(row) if support & (1 << m)) for row in printed)
    identity = ti, reduced
    if identity in index.identities:
        return index.identities[identity]
    unique = True
    for other in range(len(index.tables)):
        if other == ti:
            continue
        for columns, _ in _grouped_projections(reduced, other, index, partial=True):
            if _projection(reduced, other, columns, index, require_witnesses=False) is not None:
                unique = False
                break
        if not unique:
            break
    if len(index.identities) >= 512:
        index.identities.clear()
    index.identities[identity] = unique
    return unique


def match(rows, index, key):
    printed = tuple(tuple(key(c) for c in row) for row in rows)
    if printed in index.results:
        return index.results[printed]
    if index.exhausted or not printed or not 2 <= len(printed[0]) <= 128:
        return None
    cache_key = printed
    patterns = {row for row in printed if sum(bool(v) for v in row) >= 2}
    if len(patterns) < 2:
        index.results[cache_key] = None
        return None
    if not index.keep_empty and any(not any(row) for row in printed):
        # Empty printed grid rows can project nonempty source rows whose other
        # columns are on another page. Never silently remove those rows.
        try:
            if index.raw_index is None:
                index.raw_index = SourceIndex(index.tables, index.key, keep_empty=True, allow_missing=index.allow_missing)
                index.spend(20_000_000 - index.raw_index.remaining)
            index.raw_index.remaining = index.remaining
            found = match(rows, index.raw_index, key)
            index.remaining = index.raw_index.remaining
            index.exhausted |= index.raw_index.exhausted
        except OverflowError as error:
            _LOG.warning('Heading alignment stopped: %s', error)
            found = None
        index.results[cache_key] = found
        return found
    found, explanations, supported_anywhere = None, [], 0
    all_supported = sum(1 << m for m in range(len(printed[0])) if any(row[m] for row in printed))
    certain_ti, certain_columns = None, None
    index.table_remaining = _TABLE_BUDGET
    try:
        for ti in range(len(index.columns)):
            for columns, alternatives in _grouped_projections(printed, ti, index):
                proof = _projection(printed, ti, columns, index, evidence=True)
                if proof is None:
                    continue
                complete, owners, support, raw_complete = proof
                if not complete and raw_complete:
                    complete = _unique_source(printed, ti, support, index)
                # Fully supported competing paths can never be removed by
                # blank-cell dominance. Stop once their disagreement proves
                # that fewer than two columns could ever be named.
                if support & all_supported == all_supported:
                    fixed = {m: mask.bit_length() - 1 for m, mask in enumerate(alternatives) if mask.bit_count() == 1}
                    if certain_ti is not None and certain_ti != ti:
                        raise _Ambiguous()
                    certain_ti = ti
                    certain_columns = fixed if certain_columns is None else {m: c for m, c in certain_columns.items() if fixed.get(m) == c}
                    if len(certain_columns) < 2:
                        raise _Ambiguous()
                supported_anywhere |= support
                explanations.append((ti, alternatives, complete, owners, support))
        # Blank source cells are permission to continue an alignment, not
        # evidence for a label. A blank-only explanation cannot veto an actual
        # supported pairing. Keep all supported disagreements, including near
        # numeric/swap explanations, rather than voting for the best fit.
        kept = [p for p in explanations if p[4] & supported_anywhere == supported_anywhere]
        if kept and any(p[2] for p in kept) and len({p[0] for p in kept}) == 1:
            ti = kept[0][0]
            common, owners = None, {j: set() for j in range(len(printed))}
            for _, alternatives, _, owned, support in kept:
                mapping = {m: mask.bit_length() - 1 for m, mask in enumerate(alternatives)
                           if mask.bit_count() == 1 and (support & (1 << m)
                              or len(printed[0]) == len(index.columns[ti]))}
                common = mapping if common is None else {m: c for m, c in common.items() if mapping.get(m) == c}
                for j, places in owned.items():
                    owners[j].update(places)
            if len(common) >= 2:
                found = ti, common, owners, {(j, ri): common for j, ris in owners.items() for ri in ris}
    except _Ambiguous:
        found = None
    except OverflowError as error:
        index.limited_tables += 1
        _LOG.warning('Heading alignment stopped: %s (printed rows=%d, columns=%d)', error, len(printed), len(printed[0]))
        found = None
    finally:
        index.table_remaining = None
    if len(index.results) >= 512:
        index.results.clear()
    index.results[cache_key] = found
    return found
