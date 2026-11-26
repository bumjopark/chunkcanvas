"""Tables of converted documents put back from the source.

The layout parser reads a table off the PDF page, where merged cells, multi-line cells, schedule
grids and sheets without grid lines come out shifted or split. The converter keeps each table of
the original as a grid of cell text (source-tables.json, for Hangul and office documents); every
table block gets the rows of the source table it shows, which also handles a table split over two
pages. A workbook's pages are prints of its sheets, so there the text of a page that comes from
one sheet becomes one table, wherever the layout parser saw paragraphs.
"""
import re
import unicodedata
import logging
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from ..models import ParsedDocument
from .base import blocks_fingerprint, table_markdown

# Letters and digits of every script are compared; spacing and punctuation differ between the
# page and the source.
KEEP = re.compile(r"[\W_]+")
# Workbook formats: their pages are prints of sheets.
# Blocks a sheet's cells are read as.
last_stats = {"uncertain_blocks": 0}

CELL_TYPES = {"paragraph", "table", "list_item", "section_header", "title", "caption", "code", "formula", "footnote"}


def _grams(text: str) -> set[str]:
    t = KEEP.sub("", text).lower()
    return {t[i:i + 3] for i in range(len(t) - 2)}


def _tokens(text: str) -> list[str]:
    # CJK line wrapping can insert spaces inside words. Alphabetic words and numbers,
    # including decimal amounts, remain whole: A must not match Category, or 15 match 2015.
    text = unicodedata.normalize("NFKC", text).lower().replace("\u00ad", "").replace("\u200b", "")
    return re.findall(r"[가-힣ぁ-ヿ\u4e00-\u9fff]|(?:[+\-−△▲]?\d+(?:[.,]\d+)*(?:%|‰)?|\(\d+(?:[.,]\d+)*\))|[^\W\d_가-힣ぁ-ヿ\u4e00-\u9fff][^\W_가-힣ぁ-ヿ\u4e00-\u9fff]*", text)


def _preserves_text(original: str, replacement: str) -> bool:
    """Require complete tokens and their occurrences, allowing only CJK layout spacing."""
    return not (Counter(_tokens(original)) - Counter(_tokens(replacement)))


def _preserves_table_text(original: str, rows: list[list[str]]) -> bool:
    """A layout parser may repeat a single label beside each wrapped line of its source cell.

    Only collapse those label copies when the source has exactly one such row and all the
    right-hand fragments fit inside that row's other cells. Independent repeated records do
    not qualify: their repeated right-hand values exceed the one cell's occurrence budget.
    """
    wanted = Counter(_tokens(original))
    available = Counter(_tokens(" ".join(" ".join(row) for row in rows)))
    if not wanted - available:
        return True
    lines = [re.split(r"(?<!\\)\|", line.strip().strip("|")) for line in original.splitlines() if line.strip().startswith("|")]
    labels = Counter(tuple(_tokens(row[0])) for row in rows if row and row[0])
    for row in rows:
        if len(row) < 2 or not row[0]:
            continue
        label = tuple(_tokens(row[0]))
        pieces = [line for line in lines if len(line) > 1 and tuple(_tokens(line[0])) == label]
        if labels[label] != 1 or len(pieces) < 2:
            continue
        body = " ".join(" ".join(line[1:]) for line in pieces)
        if _preserves_text(body, " ".join(row[1:])):
            wanted.subtract({token: n * (len(pieces) - 1) for token, n in Counter(label).items()})
    return not wanted - available


def _preserves_physical_table_text(original: str, rows: list[list[str]]) -> bool:
    """A complete native grid can correct OCR's repeated merged-cell labels.

    Layout extraction can attach a body fragment to the wrong repeated label.
    The ordinary source-table check intentionally refuses that reassignment.
    Here the caller has independently accounted for every native glyph in its
    physical cell. Only surplus first-column labels may be removed; every body
    occurrence and every other label must still fit the printed grid.
    """
    if _preserves_table_text(original, rows):
        return True
    lines = [line.strip() for line in original.splitlines() if line.strip()]
    if not lines or any(not line.startswith('|') or not line.endswith('|') for line in lines):
        return False
    cells = [re.split(r"(?<!\\)\|", line.strip("|")) for line in lines]
    # Unlike fuzzy source-row selection, the physical fallback has accounted
    # for every native glyph and positioned OCR cell. Equal character budgets
    # can therefore repair layout-inserted word breaks even across a wrongly
    # merged cell ("File information SummaryInfo rmation"). Keep punctuation,
    # signs and occurrence counts; do not use this shortcut for source matching.
    content = [row for i, row in enumerate(cells) if i != 1
               or not all(re.fullmatch(r'\s*:?-+:?\s*', cell) for cell in row)]
    original_chars = Counter(_cell_text_key(''.join(cell.replace('\\|', '|') for row in content for cell in row)))
    printed_chars = Counter(_cell_text_key(''.join(cell for row in rows for cell in row)))
    if original_chars == printed_chars:
        return True
    # A native cell can wrap in the middle of a number or identifier. Compare
    # its complete spelling (including signs/punctuation) before token counts:
    # "...#1 2#..." and "...#12#..." are the same physically verified cell.
    # Matching pieces across different cells would lose row/column evidence.
    spellings = {}
    for row in rows:
        for cell in row:
            spellings.setdefault(_cell_text_key(cell), set()).add(cell)
    for row in cells:
        for i, cell in enumerate(row):
            matches = spellings.get(_cell_text_key(cell.replace('\\|', '|')), set())
            if len(matches) == 1:
                row[i] = next(iter(matches))
    if _preserves_text(' '.join(cell for row in cells for cell in row),
                       ' '.join(cell for row in rows for cell in row)):
        return True
    key = lambda text: _cell_text_key(text).replace('|', '')
    printed_labels = Counter(key(row[0]) for row in rows if row and row[0].strip())
    original_labels = Counter(key(row[0]) for row in cells if row and row[0].strip())
    duplicates = {label for label, count in original_labels.items()
                  if count > 1 and printed_labels[label] == 1}
    if not duplicates:
        return False
    # A label-only row could be a genuine OCR-only occurrence, not a wrapped
    # piece of a merged cell. Do not remove it without its own body evidence.
    if any(key(row[0]) in duplicates and not any(cell.strip() for cell in row[1:]) for row in cells):
        return False
    seen = set()
    kept = []
    for row in cells:
        label = key(row[0])
        if label not in duplicates or label not in seen:
            kept.append(row[0])
        kept.extend(row[1:])
        seen.add(label)
    return _preserves_text(' '.join(kept), ' '.join(cell for row in rows for cell in row))


class SelectedRows(list):
    """Source row/column identities survive selection and occurrence limiting."""
    def __init__(self, rows, row_ids, columns, header_id=0):
        super().__init__(rows)
        self.row_ids = list(row_ids)
        self.columns = list(columns)
        self.header_id = header_id


def _header_row(table, columns=None, *, allow_indexed=False):
    """Find labels after optional titles/blank rows.

    Indexed labels are only candidates for physical matching. Source-only
    inference must not change body selection just because row zero looks like
    a header; the caller must prove its printed cells before using this option.
    """
    columns = list(columns) if columns is not None else range(len(table[0]))
    for index, row in enumerate(table):
        filled = [row[c] for c in columns if row[c].strip()]
        if len(filled) < min(len(columns), max(2, len(columns) / 2)):
            continue
        labels = [c for c in columns if row[c].strip() and any(ch.isalpha() for ch in row[c])]
        if len(labels) == len(filled):
            return index
        # Some exported sheets carry a constant numeric helper column beside
        # their real labels. It must not make the first record on every later
        # page become a Markdown header. Require both a constant helper and a
        # label-to-number transition in another column; a mostly textual data
        # record with an amount is not enough evidence for column names.
        following = [later for later in table[index + 1:] if any(later[c].strip() for c in columns)][:8]
        numeric = lambda value: bool(re.fullmatch(r"[+\-−]?\d+(?:[.,]\d+)*(?:%|‰)?", value.strip()))
        helpers = [c for c in columns if row[c].strip() and c not in labels]
        indexed = (allow_indexed and len(labels) >= 2 and helpers == [columns[0]]
                   and row[columns[0]].strip() == '0' and len(following) >= 4
                   and all(later[columns[0]].strip() == str(n)
                           for n, later in enumerate(following, 1)))
        if indexed and any(all(numeric(later[c]) for later in following) for c in labels):
            return index
        if (len(labels) >= 3 and len(labels) >= .75 * len(filled) and len(following) >= 2
                and all(numeric(row[c]) and all(later[c].strip() == row[c].strip() for later in following)
                        for c in helpers)
                and any(all(numeric(later[c]) for later in following) for c in labels)):
            return index
        return None
    return None


def _cells_are_printed(row, evidence):
    # CJK syllable budgets alone can assemble an unseen neighbouring row out of
    # unrelated words. Require each cell's ordered spelling as well. PDF line
    # wrapping may add spaces; it must not permute the cell's characters.
    flat = _cell_text_key(evidence).replace('|', '')
    return _preserves_text(' '.join(row), evidence) and all(
        _cell_text_key(cell).replace('|', '') in flat
        for cell in row if any(c.isalpha() for c in cell))


def _sheet_body_is_printed(rows: SelectedRows, evidence: str, original: str = '') -> bool:
    """A source match may repeat column labels, never unseen data cells.

    Similar values on another horizontal page can match the same words/pairs.
    Reserve complete token occurrences for every selected data cell as well as
    checking that the replacement preserves the PDF text. The identified header
    may supply continuation labels; other rows must fit the page's occurrence budget.
    A layout box can clip a leading cell, so retain tokens already present in the
    original extraction too. Take maximum counts, never add two copies of the
    same evidence. Other matching/preservation checks still decide restoration.
    This is an additional rejection guard, not authorization to restore a grid.
    """
    cells = [row for row, index in zip(rows, rows.row_ids) if index != rows.header_id]
    header = next((row for row, index in zip(rows, rows.row_ids) if index == rows.header_id), None)
    if header is not None:
        # An empty column on this fragment does not prove that its source header
        # was printed here. Otherwise a one-column tail of a wide form imports
        # every hidden field name merely because header rows are repeatable.
        for c, label in enumerate(header):
            if label.strip() and not any(row[c].strip() for row in cells) and not (
                    _cells_are_printed([label], evidence) or _cells_are_printed([label], original)):
                return False
    body = ' '.join(' '.join(row) for row in cells)
    budget = Counter(_tokens(evidence)) | Counter(_tokens(original))
    return not (Counter(_tokens(body)) - budget) and all(
        _cells_are_printed(row, evidence) or _cells_are_printed(row, original) for row in cells)


def _limit_repeats(rows: SelectedRows, evidence: str, *, sheet: bool = False) -> SelectedRows:
    """Do not copy an entire run of identical source records onto every printed fragment.

    Reserve evidence for the distinct records first. Repeated records share what remains;
    every occurrence needs its own complete tokens. Insufficient evidence leaves the page's
    original text intact at the caller's preservation check.
    """
    keys = [tuple(_cell_text_key(c) for c in row) for row in rows]
    counts = Counter(keys)
    budget = Counter(_tokens(evidence))
    for row, key in zip(rows, keys):
        if counts[key] == 1:
            budget.subtract(Counter(_tokens(" ".join(row))))
    allowance = {}
    repeated = {key: Counter(_tokens(" ".join(row))) for row, key in zip(rows, keys) if counts[key] > 1}
    # Specific records claim their distinct values before a repeated blank form row claims
    # its shared label. All groups draw from one budget, never one budget per value.
    for key, need in sorted(repeated.items(), key=lambda item: len(item[1]), reverse=True):
        n = max(0, min(counts[key], min((budget[t] // count for t, count in need.items()), default=0)))
        allowance[key] = n
        budget.subtract({token: count * n for token, count in need.items()})
    kept, ids = [], []
    for row, key, row_id in zip(rows, keys, rows.row_ids):
        if counts[key] > 1:
            if allowance[key] <= 0:
                continue
            allowance[key] -= 1
        kept.append(row)
        ids.append(row_id)
    if len(kept) < len(rows) and len(repeated) > 1:
        # Different repeated records can consume the same token budget (for
        # example FALSE/TRUE/FALSE and TRUE/FALSE/FALSE). Counts alone cannot
        # select their occurrences or their order. Require the proposed body
        # in the printed sequence; otherwise retain the original extraction
        # and let physical-grid recovery use actual cell positions. Column-
        # major or wrapped evidence may be inconclusive, never permission to
        # assemble a different sequence of equally numerous values.
        body = SelectedRows([row for row, index in zip(kept, ids) if index != rows.header_id],
                            [index for index in ids if index != rows.header_id], rows.columns, None)
        # Sheet printing may clip a note or a label beside otherwise complete
        # repeated records. Check the same visible fragments the caller will
        # restore, not their unprinted suffixes. Keep the full ordered body in
        # this proof so unique rows cannot lend tokens to reordered repeats.
        def in_printed_order(body):
            stream = iter(_tokens(evidence))
            wanted = _tokens(' '.join(' '.join(row) for row in body))
            return all(any(actual == token for actual in stream) for token in wanted)

        if not in_printed_order(body):
            if not sheet or (body := _clip_long_cells(body, evidence, sheet=True)) is None \
                    or not in_printed_order(body):
                return SelectedRows([], [], rows.columns, rows.header_id)
    return SelectedRows(kept, ids, rows.columns, rows.header_id)


def _cell_fragment(cell: str, evidence: str, minimum: int = 8, *, allow_word_edges: bool = False) -> str | None:
    """An exact, substantial source-cell fragment present in printed text.

    A row can start with just one line at a page foot. Its share of the full cell is
    irrelevant; requiring half of that cell loses the first printed line entirely.
    """
    flat_evidence = KEEP.sub("", unicodedata.normalize("NFKC", evidence)).lower()
    if len(cell) > 16000 or len(flat_evidence) > 32000:
        return None
    chars = [(i, norm.lower()) for i, ch in enumerate(cell)
             for norm in unicodedata.normalize("NFKC", ch) if norm.isalnum()]
    flat_cell = "".join(c for _, c in chars)
    match = SequenceMatcher(None, flat_cell, flat_evidence, autojunk=False).find_longest_match()
    if match.size < minimum:
        return None
    start, end = chars[match.a][0], chars[match.a + match.size - 1][0] + 1
    # Do not turn a matched substring inside an English word into a new word.
    def latin(ch):
        return ch.isascii() and ch.isalnum()
    if not allow_word_edges:
        while start < end and start > 0 and latin(cell[start - 1]) and latin(cell[start]):
            start += 1
        while end > start and end < len(cell) and latin(cell[end - 1]) and latin(cell[end]):
            end -= 1
    if len(KEEP.sub("", cell[start:end])) < minimum:
        return None
    # Keep a cell's initial bullet, not the previous sentence's closing period.
    if start == chars[0][0]:
        start = 0
    while end < len(cell) and cell[end] in '.,;:)]}%':
        end += 1
    return cell[start:end].strip()


def _clip_long_cells(rows: SelectedRows, evidence: str, *, sheet: bool = False,
                     at_foot: bool = False) -> SelectedRows | None:
    """Keep only printed cell fragments, including short cross-page continuations."""
    clipped = []
    for index, row in enumerate(rows):
        cells = []
        for column, cell in enumerate(row):
            short = len(cell) <= 32
            present = _cells_are_printed([cell], evidence) if sheet else _preserves_text(cell, evidence)
            if present:
                cells.append(cell)
                continue
            # Even a short label can straddle pages. Only the final row at the
            # page foot may use its printed prefix; retaining the full label
            # would import its next-page suffix (and can reject the whole table).
            # Sheets keep their stricter complete-record/cell checks below.
            if short and not sheet and at_foot and index == len(rows) - 1:
                prefix = _cell_fragment(cell, evidence, minimum=4)
                # A common path/label prefix already used by another cell is
                # not a separate printed occurrence of this last row.
                key = _cell_text_key(prefix or '').replace('|', '')
                others = [c for done in clipped for c in done] + cells + row[column + 1:]
                used = sum(_cell_text_key(c).replace('|', '').count(key) for c in others) if key else 0
                if prefix and any(ch.isalpha() for ch in prefix) and cell.startswith(prefix) \
                        and _cells_are_printed([prefix], evidence) \
                        and _cell_text_key(evidence).replace('|', '').count(key) > used:
                    cells.append(prefix)
                    continue
            if short and (not sheet or any(c.isdigit() for c in cell)):
                cells.append(cell)
                continue
            # Horizontal sheet printing can end in the middle of a word ("THIS
            # SHE"). Retain that visible spelling, never complete its unseen suffix.
            # The caller's whole-token occurrence budget must still accept it.
            fragment = _cell_fragment(cell, evidence, minimum=6 if sheet else 8, allow_word_edges=sheet)
            if short and (fragment is None or len(_cell_text_key(fragment)) < .9 * len(_cell_text_key(cell))):
                # Only a tiny clipped suffix of a short label can be retained.
                # Substantial missing codes keep the printed-grid fallback.
                cells.append(cell)
                continue
            if fragment is None:
                return None
            cells.append(fragment)
        clipped.append(cells)
    return SelectedRows(clipped, rows.row_ids, rows.columns, rows.header_id)


def _cell_edge_is_printed(cell: str, evidence: str, *, start: bool) -> bool:
    if len(cell) <= 32 or (fragment := _cell_fragment(cell, evidence)) is None:
        return False
    norm = lambda s: KEEP.sub("", unicodedata.normalize("NFKC", s)).lower()
    return norm(cell).startswith(norm(fragment)) if start else norm(cell).endswith(norm(fragment))


def _source_ref(table: int, rows: SelectedRows, pages) -> str:
    # Identical source rows are candidates, not distinguishable records. The printed page
    # and occurrence order identify the actual fragment; never assert an arbitrary match.
    return (f"source-table:{table};candidate-rows:{','.join(map(str, rows.row_ids))};"
            f"columns:{','.join(map(str, rows.columns))};pages:{','.join(map(str, sorted(set(pages))))}")


def _figure(key: str) -> bool:
    """A cell that is mostly digits (an amount, a date, a code like SFR-010), compared as a whole."""
    digits = sum(ch.isdigit() for ch in key)
    return digits >= 3 and digits >= 0.5 * len(key)


def _worded(key: str) -> bool:
    """A figure with words between its numbers ("OKLI March 2015 Put 1,800", an address with its
    phone number, a formula): it may be printed over lines the layout parser reads as rows apart,
    or over two pages, so not finding it whole says nothing."""
    return len(re.findall(r"[^\W\d_]+", key)) > 1


# A table block reaching this far down its page may end in a row that goes on over the page.
PAGE_FOOT = 0.85


def _rows(table: list[list[str]], rows_grams: list[set[str]], shown: set[str], text: str = "",
          at_foot: bool = False, overflow: bool = True, repeat_header: bool = True,
          at_head: bool = False) -> list[list[str]]:
    """The rows of a source table that a block showing `shown` (its `text`) holds, with only its
    columns. `at_foot`: the block reaches the foot of its page; `overflow`: columns may be printed
    on another page."""
    # A row is placed by what sets it apart: rows alike but for one cell ("DWORD | 4 | bit 3 | …")
    # match every page of the table by what they share. A figure or code found in a row or two of
    # the table ("3218419896.96", "SFR-010") says best where the row is: the 3-grams of a row of
    # figures are those of every other row.
    seen = Counter(g for grams in rows_grams for g in grams)
    rare = max(2, 0.02 * len(rows_grams))
    cells = [{k for c in row if len(k := KEEP.sub("", c).lower()) >= 3} for row in table]
    in_rows = Counter(k for row in cells for k in row)
    flat = KEEP.sub("", text).lower()

    def figures(r: int) -> list[str]:
        """The row's figures that say whether the block shows it: worded ones only where found."""
        own = [k for k in cells[r] if in_rows[k] <= rare and _figure(k)] if flat else []
        return [k for k in own if not _worded(k) or k in flat]

    def part(r: int) -> float:
        """How much of the row the block shows: of its figures when it has some, else of its rare 3-grams."""
        if own := figures(r):
            return sum(k in flat for k in own) / len(own)
        own = {g for g in rows_grams[r] if seen[g] <= rare} or rows_grams[r]
        return len(own & shown) / len(own)

    def shows(r: int) -> bool:
        return part(r) > 0.5 if figures(r) else part(r) >= 0.5

    inside = [r for r, g in enumerate(rows_grams) if g and shows(r)]
    if not inside:
        return []
    # Rows repeated within the table (a sub-header before each group) match where each copy is.
    # Rows too short to look for ("| 2 | 4 |") are no gap between the rows around them.
    placed = [r for r, g in enumerate(rows_grams) if g]
    start, end = _densest_run([int(r in inside) for r in placed], 3)
    first, last = placed[start], placed[end - 1]
    # A short group label immediately beside the distinctive rows belongs to the same
    # printed fragment when all its cells are present. It cannot place the fragment alone.
    while first > 0 and any(table[first - 1]) and _cells_are_printed(table[first - 1], text):
        first -= 1
    while last + 1 < len(table) and any(table[last + 1]) and _cells_are_printed(table[last + 1], text):
        last += 1
    # A short tail at the page top belongs to the preceding source row. Without it,
    # the preservation guard rejects otherwise recoverable reversed/merged columns.
    if at_head and first > 0 and any(_cell_edge_is_printed(cell, text, start=False) for cell in table[first - 1]):
        first -= 1
    # A row that starts at the foot of the page and goes on over it shows only part of itself
    # here: it is this page's (the next page shows the rest of it, no row of its own).
    if at_foot and end < len(placed) and (part(placed[end]) >= 0.3 or any(
            _cell_edge_is_printed(cell, text, start=True) for cell in table[placed[end]])):
        last = placed[end]
    # A row shown whole away from them (a note under the table) comes along unless it repeats one.
    run = table[first:last + 1]
    picked = sorted({*range(first, last + 1), *(r for r in inside if not first <= r <= last and table[r] not in run
                                                  and rows_grams[r] <= shown)})
    body = [table[r] for r in picked]
    # A continuation on the next page still says what its columns are.
    header_id = _header_row(table) if repeat_header else None
    if header_id is not None and not overflow:
        # Word/HWP forms can put several independently labelled sections in one
        # source table. A full-width section label ends the preceding column
        # context; it must not repeat that section's fields above later rows.
        for row in table[header_id + 1:picked[0]]:
            labels = {cell.strip() for cell in row if cell.strip()}
            if len(labels) == 1 and row[0].strip() and any(ch.isalpha() for ch in row[0]):
                header_id = None
                break
    added_header = header_id is not None and header_id < picked[0]
    row_ids = [header_id, *picked] if added_header else picked
    rows = [table[r] for r in row_ids]
    # Columns of empty cells (a grid drawn as layout, like an organization chart) go, and so do
    # columns printed on another page (a sheet wider than the paper), judged by the rows shown. A
    # document's table is printed whole across: a label merged over many rows is printed once,
    # maybe on another page, and its column stays.
    keep = []
    unprinted_header_columns = False
    for c in range(len(rows[0])):
        cells = set().union(*(_grams(row[c]) for row in body))
        if overflow and added_header and not any(row[c].strip() for row in body) and table[header_id][c].strip() \
                and not _cells_are_printed([table[header_id][c]], text):
            unprinted_header_columns = True
            continue
        if any(row[c] for row in rows) and (not overflow or not cells or len(cells & shown) >= 0.5 * len(cells)):
            keep.append(c)
    if unprinted_header_columns:
        # Only the printed records locate this fragment. The dense source row
        # was a different form section, not a proved header for these columns.
        # Retain the complete occurrence budget without injecting its labels.
        row_ids = picked
        header_id = None
    row_ids = [r for r in row_ids if any(table[r][c] for c in keep)]
    return _limit_repeats(SelectedRows([[table[r][c] for c in keep] for r in row_ids], row_ids, keep, header_id), text)


def apply_source_tables(parsed: ParsedDocument, tables: list[list[list[str]]], sheets: bool = False,
                        pdf: Path | None = None, columns_overflow: bool = True, repeat_header: bool = True,
                        source_furniture: list | None = None, strict_records: bool = False) -> int:
    """Replace table blocks with the rows of the matching source table (on the pages of a workbook,
    all text from one sheet; `pdf` gives the cells as printed); returns how many tables were put back.
    `columns_overflow`: tables may be wider than the paper (sheets, CSV), their columns printed on
    other pages. `repeat_header` permits a known rendered header on each fragment; it must
    not relax CSV/TSV record validation. `strict_records` requires complete printed rows."""
    last_stats["uncertain_blocks"] = 0
    if not tables:
        return 0
    strict_records = strict_records or parsed.document.source_format == "csv"
    rows_grams = [[_grams(" ".join(dict.fromkeys(row))) for row in table] for table in tables]
    table_grams = [set().union(*rows) if rows else set() for rows in rows_grams]
    source_of: dict[str, int] = {}
    grid_header_blocks: set[str] = set()
    document_grids = 0
    attempted = set()
    # A Word header/footer part has no rendered page coordinates. Its template
    # is useful for classifying paragraphs, but cannot identify digits or words
    # inside a table at the page edge. Only positioned source placeholders may
    # be removed from the physical evidence used to restore cells.
    source_furniture = [t for t in source_furniture or [] if isinstance(t, dict)
                        and 'pageIndex' in t and isinstance(t.get('bbox'), dict)]
    try:
        printed = _printed_cells(pdf, parsed, source_furniture) if pdf else {}
    except Exception as exc:  # optional restoration must leave the existing parse intact
        logging.getLogger(__name__).warning('Printed table evidence unavailable: %s', type(exc).__name__)
        last_stats['uncertain_blocks'] = max(1, sum(b.type in CELL_TYPES for b in parsed.blocks))
        return 0
    grid_cache = {'_native': getattr(printed, 'native', {})}
    source_cells = {}
    for table in tables:
        for row in table:
            for cell in row:
                if cell.strip():
                    source_cells.setdefault(_cell_text_key(cell), set()).add(cell)
    if sheets:
        candidate = parsed.model_copy(deep=True)
        try:
            _sheet_pages(candidate, tables, rows_grams, table_grams, source_of, printed, attempted)
        except Exception as exc:
            logging.getLogger(__name__).warning('Source sheet restoration unavailable: %s', type(exc).__name__)
            last_stats['uncertain_blocks'] = max(1, sum(b.type in CELL_TYPES for b in parsed.blocks))
            return 0
        parsed.blocks = candidate.blocks
    for block in parsed.blocks:
        if block.type != "table" or block.id in source_of:
            continue
        evidence = printed.get(block.id, block.text)
        best = None
        printed_rows = None
        if strict_records:
            # CSV/TSV conversion emits a grid with explicit cells, even when row one
            # is not known to be a header. Fuzzy cell/row similarity is not evidence
            # that a record was printed here. Match complete physical row tuples,
            # including empty columns and each separate repeated occurrence.
            attempted.add(block.id)
            alternative = None
            if pdf:
                try:
                    alternative = _grid_cell_evidence(pdf, block, evidence, source_cells, grid_cache,
                                                       source_furniture or [], include_unchanged=True)
                except (RuntimeError, ValueError, TypeError, IndexError) as exc:
                    logging.getLogger(__name__).warning("Printed record grid unavailable: %s", type(exc).__name__)
            if alternative is not None:
                evidence, printed_rows = alternative
                for t, table in enumerate(tables):
                    rows = _printed_record_rows(table, printed_rows)
                    if rows and _preserves_table_text(evidence, rows):
                        best = (1.0, t, rows, table_markdown(rows))
                        break
        for attempt in range(0 if strict_records else 2):
            shown = _grams(evidence)
            if len(shown) < 6:
                break
            for t, table in enumerate(tables):
                inside = [r for r, g in enumerate(rows_grams[t]) if g and len(g & shown) >= 0.5 * len(g)]
                if not inside:
                    continue
                attempted.add(block.id)
                rows = _rows(table, rows_grams[t], shown, evidence,
                             at_foot=bool(block.regions) and block.regions[-1].bbox.y1 >= PAGE_FOOT,
                             overflow=columns_overflow, repeat_header=repeat_header,
                             at_head=bool(block.regions) and block.regions[0].bbox.y0 <= 1 - PAGE_FOOT)
                rows = _clip_long_cells(rows, evidence, at_foot=repeat_header and not sheets and bool(block.regions)
                                        and block.regions[-1].bbox.y1 >= PAGE_FOOT) if rows else None
                if rows and printed and not sheets:
                    # Similar vocabulary on another page cannot authorize copying
                    # its rows. Keep only printed cell sequences (plus a known
                    # continuation header), including any clipped cell fragments.
                    keep = [i for i, row in enumerate(rows) if rows.row_ids[i] == rows.header_id
                            or _cells_are_printed(row, evidence)]
                    rows = SelectedRows([rows[i] for i in keep], [rows.row_ids[i] for i in keep], rows.columns, rows.header_id)
                if rows and sheets and printed and not _sheet_body_is_printed(rows, evidence, block.text):
                    continue
                if rows and printed_rows is not None:
                    # The grid also proves which complete rows are actually here.
                    # A rare time such as 13H00 must not import an unseen neighbouring
                    # source row that happens to share that time.
                    visible = {tuple(_cell_text_key(c) for c in row if c.strip()) for row in printed_rows}
                    keep = [i for i, (row, row_id) in enumerate(zip(rows, rows.row_ids))
                            if (repeat_header and row_id == rows.header_id)
                            or tuple(_cell_text_key(c) for c in row if c.strip()) in visible]
                    rows = SelectedRows([rows[i] for i in keep], [rows.row_ids[i] for i in keep], rows.columns, rows.header_id)
                if not rows or not _preserves_table_text(evidence, rows):
                    continue
                # Score the printed fragments, including a partial last row, rather than
                # only rows whose full source text was mostly visible on this page.
                replacement = table_markdown(rows)
                score = len(_grams(replacement) & shown) / len(shown)
                if best is None or score > best[0]:
                    best = (score, t, rows, replacement)
            if best is not None and best[0] >= 0.5:
                break
            # A complete token may wrap inside one drawn cell (01 -> 0 / 1).
            # Retry only with exact cell matches backed by physical grid lines.
            alternative = None
            if pdf and attempt == 0:
                try:
                    alternative = _grid_cell_evidence(pdf, block, evidence, source_cells, grid_cache,
                                                       source_furniture or [])
                except (RuntimeError, ValueError, TypeError, IndexError) as exc:
                    # Optional grid recovery must not discard a usable PDF parse.
                    logging.getLogger(__name__).warning("Native table grid unavailable: %s", type(exc).__name__)
            if alternative is None:
                break
            evidence, printed_rows = alternative
            best = None
        if best is not None and block._table_ocr_cells \
                and not _preserves_table_text(block.text, best[2]):
            # A source table can omit scanned formulas. Matching every native
            # glyph alone does not authorize replacing their OCR with blanks.
            best = None
        if best is None or best[0] < 0.5:
            if pdf and not sheets and not strict_records:
                try:
                    physical = _grid_cell_evidence(pdf, block, printed.get(block.id, block.text),
                        source_cells, grid_cache, source_furniture or [], include_unchanged=True,
                        preserve_columns=True, include_ocr=True)
                except (RuntimeError, ValueError, TypeError, IndexError) as exc:
                    logging.getLogger(__name__).warning('Printed document grid unavailable: %s', type(exc).__name__)
                    physical = None
                if physical is not None:
                    _, cells = physical
                    # Restore only actual grid cells. In particular, blank leading
                    # cells of a continued merged row do not authorize copying the
                    # previous page's label or values. OCR-only text must survive.
                    width = max(map(len, cells), default=0)
                    hits = sum(_cell_text_key(c) in source_cells for row in cells for c in row if c.strip())
                    printed_chars = Counter(_cell_text_key(physical[0].replace(' | ', '')))
                    cell_chars = Counter(_cell_text_key(''.join(c for row in cells for c in row)))
                    # Text can overflow a drawn row border during conversion.
                    # Keep an existing complete source cell together instead of
                    # promoting its wrapped suffix to a separate data row.
                    physical_keys = {_cell_text_key(c) for row in cells for c in row if c.strip()}
                    original_cells = [c.strip() for line in block.text.splitlines() if line.strip().startswith('|')
                                      for c in re.split(r"(?<!\\)\|", line.strip().strip('|')) if c.strip()]
                    divides_source_cell = any(_cell_text_key(c) in source_cells
                                              and not any(_cell_text_key(c) in key for key in physical_keys)
                                              for c in original_cells)
                    original = block.text
                    if block._table_layout_text and block._table_layout_text[0] == block.text:
                        # A spanning layout cell may appear in several markdown
                        # rows, but those are references to one read, not extra
                        # physical occurrences. Independent equal cells stay.
                        original = block._table_layout_text[1]
                    if len(cells) >= 2 and width >= 2 and hits >= 2 \
                            and all(len(row) == width for row in cells) \
                            and not divides_source_cell \
                            and printed_chars == cell_chars \
                            and _preserves_physical_table_text(original, cells):
                        header_keys = [_cell_text_key(c) for c in cells[0]]
                        known_header = any((ri := _header_row(table)) is not None and (
                            [_cell_text_key(c) for c in table[ri]] == header_keys
                            or (all(header_keys) and [_cell_text_key(c) for c in table[ri] if c.strip()] == header_keys))
                            for table in tables)
                        columns = [i for i in range(width) if any(row[i].strip() for row in cells)]
                        if len(columns) < 2:
                            continue
                        cells = [[row[i] for i in columns] for row in cells]
                        continuation = None
                        if not known_header:
                            if repeat_header:
                                continuation = _printed_grid_header(parsed, block, tables, width, columns, grid_cache)
                            header = continuation[0] if continuation else [f'Column {i + 1}' for i in range(len(columns))]
                            cells = [header, *cells]
                        replacement = table_markdown(cells)
                        if replacement == block.text:
                            continue
                        if continuation:
                            taken = continuation[1]
                            block.regions = [r for part in taken for r in part.regions] + block.regions
                            grid_header_blocks.update(part.id for part in taken)
                        block.text = block.markdown = replacement
                        block.lines = None
                        block.line_geometry = 'unavailable'
                        block.source.ref = ('printed-document-grid:pages:' + ','.join(
                            str(p) for p in sorted({r.page_index for r in block.regions}))
                            + ';columns:' + ','.join(map(str, columns)))
                        # This is a partial native fallback, not a complete source
                        # reconstruction. Keep it out of source-row absorption.
                        attempted.add(block.id)
                        document_grids += 1
            continue
        rows, replacement = best[2:]
        if not repeat_header:
            # CSV has no merged cells or implicit print headers. Similar paths
            # or labels can fuzzily match a neighbouring record on another page;
            # require each complete cell here before restoring that record.
            visible = KEEP.sub("", unicodedata.normalize("NFKC", evidence)).lower()
            if any(KEEP.sub("", unicodedata.normalize("NFKC", cell)).lower() not in visible
                   for row in rows for cell in row if cell.strip()):
                continue
        block.text = block.markdown = replacement
        block.lines = None
        block.line_geometry = "unavailable"
        block.source.ref = _source_ref(best[1], rows, [r.page_index for r in block.regions])
        source_of[block.id] = best[1]
    if grid_header_blocks:
        parsed.blocks = [b for b in parsed.blocks if b.id not in grid_header_blocks]
        for index, block in enumerate(parsed.blocks):
            block.reading_order = index
    _absorb_fragments(parsed, source_of, allow_headers=repeat_header)
    if repeat_header:
        _absorb_header_lines(parsed, source_of, tables)
    native_grids = 0
    if sheets and pdf:
        from .sheet_grids import recover_sheet_grids
        # A failed whole-source match can still contain a well-defined printed
        # sub-grid. Recover its actual cells while retaining surrounding native
        # text. Work on a copy so an optional PDF-grid failure is transactional.
        candidate = parsed.model_copy(deep=True)
        try:
            native_grids = recover_sheet_grids(candidate, pdf, tables, source_of, printed)
        except (RuntimeError, ValueError, TypeError, IndexError) as exc:
            logging.getLogger(__name__).warning("Printed sheet grid unavailable: %s", type(exc).__name__)
        else:
            if native_grids:
                parsed.blocks = candidate.blocks
    header_updates = 0
    if pdf and not sheets and not columns_overflow and repeat_header and not strict_records:
        from .document_headers import restore_printed_headers
        # A partial native grid may be sound even when clipped source rows could
        # not be restored. Carry only headings proved by their preceding printed
        # grid; keep every current body cell and the original page regions.
        candidate = parsed.model_copy(deep=True)
        try:
            header_updates = restore_printed_headers(candidate, tables, pdf, grid_cache['_native'])
        except (RuntimeError, ValueError, TypeError, IndexError) as exc:
            logging.getLogger(__name__).warning('Printed document headings unavailable: %s', type(exc).__name__)
        else:
            if header_updates:
                parsed.blocks = candidate.blocks
    if sheets and pdf and not strict_records:
        from .printed_header_bands import restore_printed_header_bands
        candidate = parsed.model_copy(deep=True)
        try:
            bands = restore_printed_header_bands(candidate, tables, pdf, getattr(printed, 'native', {}))
        except (RuntimeError, ValueError, TypeError, IndexError) as exc:
            logging.getLogger(__name__).warning('Printed table header bands unavailable: %s', type(exc).__name__)
        else:
            if bands:
                parsed.blocks = candidate.blocks
                native_grids += bands
    carried = 0
    if not strict_records:
        # Printed-grid fallbacks name unproven columns `Column N`. Where the printed rows
        # prove their source table and columns, carry that table's heading row instead.
        # Optional context: work on a copy so any failure keeps the parse as it is.
        candidate = parsed.model_copy(deep=True)
        try:
            from .carried_headers import carry_source_headers
            carried = carry_source_headers(candidate, tables, allow_missing_cells=not sheets)
        except Exception as exc:  # noqa: BLE001 - optional refinement must not fail a parse
            logging.getLogger(__name__).warning('Carried table headings unavailable: %s', type(exc).__name__)
        else:
            if carried:
                parsed.blocks = candidate.blocks
    if source_of or native_grids or document_grids or header_updates or carried:
        parsed.blocks_fingerprint = blocks_fingerprint(parsed.document.parser, parsed.document.parser_version, parsed.blocks)
    # Native fallback grids preserve clipped/unknown values as printed. They are
    # useful structure, but not a claim that every source cell was recovered.
    last_stats["uncertain_blocks"] = native_grids + sum(b.id in attempted and b.id not in source_of for b in parsed.blocks)
    return len(source_of) + native_grids + document_grids


def _densest_run(hits: list[int], gap: int) -> tuple[int, int] | None:
    """The run of positions with hits (allowing `gap` positions without between them) that has the
    most hits, as (start, end) with end exclusive."""
    best, run, total = None, None, 0
    for i, n in enumerate(hits):
        if not n:
            continue
        if run is None or i - run[1] > gap:
            run, total = [i, i + 1], 0
        run[1], total = i + 1, total + n
        if best is None or total > best[0]:
            best = (total, run[0], run[1])
    return (best[1], best[2]) if best else None


def _cell_key(text: str) -> tuple[str, set[str]] | None:
    """How a cell is looked for on a page: as whole words when short, by its three-character
    runs when long or CJK (line breaks inside a cell move the spaces). Single-letter values remain complete searchable tokens."""
    words = " ".join(KEEP.sub(" ", text).lower().split())
    if not words:
        return None
    if len(words) > 12 or re.search("[가-힣ぁ-ヿ\u4e00-\u9fff]", words):
        return "", _grams(words)
    return f" {words} ", set()


def _words(text: str) -> list[str]:
    return KEEP.sub(" ", text).lower().split()


def _sheet_part(table: list[list[str]], keys: list[list[tuple[str, set[str]] | None]], norm: list[list[str]],
                words: str, shown: set[str], by_pairs: bool = True, printed_text: str | None = None) -> list[list[str]]:
    """The part of a sheet a page shows. A sheet longer or wider than the paper is printed in
    pieces, so the page holds a run of rows and of columns. Two neighbouring cells of a row read
    one after the other on the page ("43 2013", "2013 alabama") place that row and those columns;
    a sheet of one column, or a page read column by column (by_pairs off), falls back to values
    found in only a few rows of their column."""
    width = len(table[0])
    # Each filled cell with the next filled one (empty columns between them are skipped):
    # (first column, last column, both texts) per row, and whether the page reads them in a row.
    links = [[(c, d, row[c], row[d]) for c, d in zip(cols, cols[1:])] for row in norm
             for cols in [[c for c, cell in enumerate(row) if cell]]]
    pairs = [[f" {a} {b} " in words for _, _, a, b in row] for row in links]
    hits = [[key is not None and (key[0] in words if key[0] else len(key[1] & shown) >= 0.7 * len(key[1]))
             for key in row] for row in keys]
    filled = [r for r, row in enumerate(keys) if any(row)]
    if by_pairs and any(any(p) for p in pairs):
        # A pair found in many rows of its columns ("2013 Alabama") places none of them.
        rare = max(2, 0.02 * len(table))
        seen = Counter(link for row in links for link in row)
        strong = [sum(p and seen[link] <= rare for p, link in zip(prow, row)) for prow, row in zip(pairs, links)]
        evidence = strong if any(strong) else [sum(p) for p in pairs]
        # The column names atop every printed page (print titles) place no page while other rows
        # can; the part gets them back as its header row.
        if filled and any(evidence[r] for r in filled[1:]):
            evidence = [0 if r == filled[0] else e for r, e in enumerate(evidence)]
        # Rows count by position among the filled ones, so empty rows between parts are no gap.
        run = _densest_run([evidence[r] for r in filled], 3)
        if run is None:
            return []  # No reliable placement: keep the text extracted from the PDF.
        lo, hi = run[0], run[1] - 1
        # A title or note row above or below (one cell, no pair) is on the page when all its cells are.
        # Cells from different visible rows can collectively match every value
        # of an unseen data row. Extend only standalone titles/notes here; data
        # rows need the neighbouring-cell evidence that placed the run itself.
        whole = [sum(bool(cell) for cell in norm[r]) == 1
                 and all(h for h, k in zip(hits[r], keys[r]) if k) for r in filled]
        while lo > 0 and whole[lo - 1]:
            lo -= 1
        while hi < len(filled) - 1 and whole[hi + 1]:
            hi += 1
        r0, r1 = filled[lo], filled[hi] + 1
        per_link = Counter((c, d) for r in range(r0, r1) for p, (c, d, _, _) in zip(pairs[r], links[r]) if p)
        top = max(per_link.values(), default=0)
        shown_columns = [0] * width
        for (c, d), n in per_link.items():
            if n >= 0.3 * top:
                shown_columns[c:d + 1] = [1] * (d + 1 - c)
        # A column of a title row stands alone: the first column comes along with the run.
        for r in range(r0, r1):
            for c in range(width):
                if hits[r][c] and keys[r][c] and not any(keys[r][:c]) and not any(keys[r][c + 1:]):
                    shown_columns[c] = 1
    else:
        # Counted within each column: a row number is rare in its column though small numbers fill others.
        count = Counter((c, cell) for row in norm for c, cell in enumerate(row) if cell)
        rare = max(2, 0.02 * len(table))
        evidence = [sum(hit and count[(c, cell)] <= rare for c, (hit, cell) in enumerate(zip(hrow, row)))
                    for hrow, row in zip(hits, norm)]
        if not any(evidence):
            evidence = [sum(row) for row in hits]
        run = _densest_run([evidence[r] for r in filled], 3)
        if not run:
            return []
        r0, r1 = filled[run[0]], filled[run[1] - 1] + 1
        shown_columns = []
        for c in range(width):
            keyed = sum(1 for r in range(r0, r1) if keys[r][c])
            shown_columns.append(int(keyed > 0 and sum(hits[r][c] for r in range(r0, r1)) >= 0.5 * keyed))
    columns = _densest_run(shown_columns, 0)
    if not columns:
        return []
    c0, c1 = columns
    # A column beside the part whose cells the page shows is on it too, though no pair placed it
    # (a column read on its own, cell by cell); a column of one-character codes ("F", "I") has
    # no cells to look for: it is on the page when the page shows those characters for its rows.
    tokens = Counter(words.split())

    def shown_column(c: int, beside: int) -> bool:
        # Cells that say what the cell beside them says are found through that one.
        own = [r for r in range(r0, r1) if norm[r][c] and norm[r][c] != norm[r][beside]]
        keyed = [r for r in own if keys[r][c]]
        if keyed:
            return sum(hits[r][c] for r in keyed) >= 0.8 * len(keyed)
        values = [norm[r][c] for r in own]
        return bool(values) and sum(min(n, tokens[v]) for v, n in Counter(values).items()) >= 0.8 * len(values)

    while c0 > 0 and shown_column(c0 - 1, c0):
        c0 -= 1
    while c1 < width and shown_column(c1, c1 - 1):
        c1 += 1
    # Repeated form records cannot supply rare pairs. Extend through those groups only;
    # a distinct unseen row may happen to share every cell with different visible rows.
    projected = Counter(tuple(row[c0:c1]) for row in table)
    def extends(r):
        row = table[r][c0:c1]
        return (projected[tuple(row)] > 1 or sum(bool(cell) for cell in row) == 1) and any(row) \
            and _cells_are_printed(row, printed_text or words)
    while r0 > 0 and extends(r0 - 1):
        r0 -= 1
    while r1 < len(table) and extends(r1):
        r1 += 1
    # A neighbouring row may share a seller/product pair with visible rows but
    # have a different amount/date. Never copy that row onto this page merely
    # because the weak pair matched. Check only the printed columns, since the
    # remaining columns may legitimately be on another page.
    def numeric_cells_visible(row: list[str]) -> bool:
        return all(_preserves_text(cell, printed_text or words) for cell in row[c0:c1]
                   if any(c.isdigit() for c in cell) and not any(c.isalpha() for c in cell))

    while r0 < r1 and not numeric_cells_visible(table[r0]):
        r0 += 1
    while r1 > r0 and not numeric_cells_visible(table[r1 - 1]):
        r1 -= 1
    if r0 == r1:
        return []
    row_ids = list(range(r0, r1))
    rows = [row[c0:c1] for row in table[r0:r1]]
    # A page further down still says what its columns are: the first row above it that fills most
    # of them (not the sheet's title).
    header_id = _header_row(table, range(c0, c1))
    if header_id is not None and header_id < r0:
        header = table[header_id][c0:c1]
        row_ids = [header_id, *row_ids]
        rows = [header, *rows]
    keep = [c for c in range(c1 - c0) if any(row[c] for row in rows)]
    columns = [c0 + c for c in keep]
    row_ids = [r for r in row_ids if any(table[r][c] for c in columns)]
    return _limit_repeats(SelectedRows([[table[r][c] for c in columns] for r in row_ids], row_ids, columns, header_id), printed_text or words, sheet=True)


def _within(box, outer) -> bool:
    w, h = max(box.x1 - box.x0, 1e-6), max(box.y1 - box.y0, 1e-6)
    ix = max(0.0, min(box.x1, outer.x1) - max(box.x0, outer.x0))
    iy = max(0.0, min(box.y1, outer.y1) - max(box.y0, outer.y0))
    return ix * iy >= 0.8 * w * h


def _pieces(block) -> list[tuple[int, str, list, list | None]] | None:
    """A block's text, regions and lines page by page; a paragraph run on over pages has lines that
    say which text is where. None for a block over pages without them."""
    pages = list(dict.fromkeys(r.page_index for r in block.regions))
    if len(pages) == 1:
        return [(pages[0], block.text, block.regions, block.lines)]
    if not block.lines:
        return None
    out = []
    for p in pages:
        text, lines = "", []
        for ln in (ln for ln in block.lines if ln.page_index == p):
            start = len(text) + (1 if text else 0)
            text = f"{text} {block.text[ln.start:ln.end]}" if text else block.text[ln.start:ln.end]
            lines.append(ln.model_copy(update={"start": start, "end": len(text)}))
        out.append((p, text, [r for r in block.regions if r.page_index == p], lines))
    return out


class PrintedEvidence(dict):
    def __init__(self):
        super().__init__()
        self.lines = {}
        self.native = {}

    def in_regions(self, regions) -> str:
        # A PDF line inside overlapping layout blocks is still one physical occurrence.
        pages = sorted({r.page_index for r in regions})
        return " | ".join(text for p in pages for x, y, text in self.lines.get(p, []) if text and any(
            r.page_index == p and r.bbox.x0 <= x <= r.bbox.x1 and r.bbox.y0 <= y <= r.bbox.y1 for r in regions))


def _cell_text_key(text: str) -> str:
    # Punctuation stays significant: 1.01 must not become 101. Only source
    # formatting characters and whitespace within a verified cell are ignored.
    text = unicodedata.normalize("NFKC", text).casefold().replace("\u00ad", "").replace("\u200b", "")
    return "".join(text.split())


def _printed_record_rows(table: list[list[str]], printed_rows: list[list[str]]) -> SelectedRows | None:
    """Match whole CSV records; values in different rows/columns cannot prove a row."""
    candidates = {}
    for i, row in enumerate(table):
        key = tuple(_cell_text_key(cell) for cell in row)
        candidates.setdefault(key, []).append(i)
    rows, identities = [], []
    for printed in printed_rows:
        choices = candidates.get(tuple(_cell_text_key(cell) for cell in printed), [])
        # Whitespace normalization can make distinct source strings equal. Do not
        # choose one spelling arbitrarily; leave the PDF parse intact in that case.
        if not choices or len({tuple(table[i]) for i in choices}) != 1:
            return None
        identities.append(choices[0])
        rows.append(table[choices[0]])
    return SelectedRows(rows, identities, range(len(table[0]))) if rows else None


def _grid_cell_evidence(pdf: Path, block, original: str, source_cells: dict, cache: dict,
                        source_furniture: list, *, include_unchanged: bool = False,
                        preserve_columns: bool = False, include_ocr: bool = False) -> tuple[str, list[list[str]]] | None:
    """Resolve wrapped tokens only within one cell bounded by actual PDF lines.

    Never concatenate separate cells or accept substrings. Each complete printed
    cell must match one unambiguous source value; every physical occurrence stays
    separate. Text outside matched cells stays unchanged.
    """
    import pymupdf
    from .source_furniture import _match
    from itertools import groupby
    from .textlayer import Fragment, build_text, read_page

    records = []
    with pymupdf.open(pdf) as doc:
        for p in sorted({r.page_index for r in block.regions}):
            if p not in cache:
                page = doc[p]
                grid_rows = [row.cells for table in page.find_tables(strategy="lines_strict", use_layout=False).tables
                             for row in table.rows]
                cells = list(dict.fromkeys(cell for row in grid_rows for cell in row if cell))
                if not cells or len(cells) > 4096:
                    cache[p] = None
                else:
                    natives = cache.setdefault('_native', {})
                    if p not in natives:
                        natives[p] = read_page(page, p)
                    native = natives[p]
                    indices = {cell: i for i, cell in enumerate(cells)}
                    normalized = [tuple(v / (native.width if i % 2 == 0 else native.height)
                                        for i, v in enumerate(cell)) for cell in cells]
                    slots = {}
                    for i, box in enumerate(normalized):
                        for slot in range(int(box[1] * 128), int(box[3] * 128) + 1):
                            slots.setdefault(slot, []).append(i)
                    cache[p] = (native, normalized, [[indices[cell] if cell else None for cell in row]
                                                    for row in grid_rows], slots)
            if cache[p] is None:
                return None
            native, cells, _, slots = cache[p]
            seen = set()
            for fragment in native.fragments:
                text, _, _ = build_text([fragment], p, native.width)
                x, y = (fragment.x0 + fragment.x1) / 2, (fragment.y0 + fragment.y1) / 2
                identity = (round(x, 4), round(y, 4), text)
                if not text or identity in seen or not any(r.page_index == p and
                        r.bbox.x0 <= x <= r.bbox.x1 and r.bbox.y0 <= y <= r.bbox.y1 for r in block.regions):
                    continue
                box = {'x0': fragment.x0, 'y0': fragment.y0, 'x1': fragment.x1, 'y1': fragment.y1}
                if _match(text, box, source_furniture, p):
                    continue
                seen.add(identity)
                def owner(ch):
                    owners = [i for i in slots.get(int(ch.cy * 128), [])
                              if cells[i][0] <= ch.cx <= cells[i][2] and cells[i][1] <= ch.cy <= cells[i][3]]
                    return (p, owners[0]) if len(owners) == 1 else None
                # PDF text runs can cross a column border. Partition actual glyphs,
                # keeping every piece, rather than assigning the whole run by its centre.
                for cell, chars in groupby(fragment.chars, key=owner):
                    piece = Fragment(list(chars))
                    text = build_text([piece], p, native.width)[0]
                    if text:
                        records.append((text, piece, cell))
    # Rotated or otherwise unmatched evidence must not silently disappear.
    if "".join(_cell_text_key(text) for text, _, _ in records) != _cell_text_key(original.replace(" | ", "")):
        return None
    groups = {}
    for i, (_, fragment, owner) in enumerate(records):
        if owner is not None:
            groups.setdefault(owner, []).append((i, fragment))
    replacements, consumed, cell_texts = {}, set(), {}
    for (page, cell), entries in groups.items():
        text, _, _ = build_text([fragment for _, fragment in entries], page, cache[page][0].width)
        cell_texts[(page, cell)] = text
        matches = source_cells.get(_cell_text_key(text), set())
        if len(matches) != 1:
            continue
        replacements[entries[0][0]] = next(iter(matches))
        cell_texts[(page, cell)] = replacements[entries[0][0]]
        consumed.update(i for i, _ in entries[1:])
    result = " | ".join(replacements.get(i, text) for i, (text, _, _) in enumerate(records) if i not in consumed)
    if include_ocr:
        additions = _fill_scanned_cells(block, cell_texts, cache, groups)
        if additions:
            result += " | " + " | ".join(additions)
    rows = [[cell_texts.get((p, i), '') for i in row if preserve_columns or i is not None]
            for p in sorted({r.page_index for r in block.regions})
            for row in cache[p][2]]
    return (result, [row for row in rows if any(row)]) if include_unchanged or result != original else None


def _fill_scanned_cells(block, cell_texts: dict, cache: dict, native_groups: dict | None = None) -> list[str]:
    """Place OCR in physical cells, keeping it separate from native glyphs.

    The layout model's row/column guesses are not evidence. An OCR line must
    lie almost entirely in exactly one drawn cell and must not overlap native
    glyphs. Never append an OCR echo or split a line across cells. Unplaced text remains protected
    by the caller's original-text preservation check.
    """
    from itertools import groupby
    from .textlayer import build_text
    native_groups = native_groups or {}
    groups = {}
    for p, text, box in dict.fromkeys(block._table_ocr_cells):
        if p not in cache or cache[p] is None or not text.strip():
            continue
        area = (box[2] - box[0]) * (box[3] - box[1])
        if area <= 0:
            continue
        owners = []
        for i, cell in enumerate(cache[p][1]):
            intersection = max(0, min(box[2], cell[2]) - max(box[0], cell[0])) \
                * max(0, min(box[3], cell[3]) - max(box[1], cell[1]))
            if intersection >= .9 * area:
                owners.append(i)
        if len(owners) != 1:
            continue
        owner = (p, owners[0])
        native = [fragment for _, fragment in native_groups.get(owner, [])]
        if cell_texts.get(owner, '').strip() and (not native or any(
                box[0] <= ch.cx <= box[2] and box[1] <= ch.cy <= box[3]
                for fragment in native for ch in fragment.chars)):
            continue
        groups.setdefault(owner, []).append((box, text))
    additions = []
    for owner, lines in groups.items():
        pieces = [((box[1] + box[3]) / 2, box[0], False, text) for box, text in lines]
        pieces += [((f.y0 + f.y1) / 2, f.x0, True, f) for _, f in native_groups.get(owner, [])]
        ordered = sorted(pieces, key=lambda item: item[:2])
        parts = []
        for is_native, run in groupby(ordered, key=lambda item: item[2]):
            values = [item[3] for item in run]
            parts.append(build_text(values, owner[0], cache[owner[0]][0].width)[0] if is_native else ' '.join(values))
        cell_texts[owner] = ' '.join(parts)
        additions.extend(text for _, text in lines)
    return additions


def _printed_cells(pdf: Path, parsed: ParsedDocument, source_furniture: list | None = None) -> PrintedEvidence:
    """Native PDF lines, each identified once by its page and location, for source-cell matching."""
    import pymupdf
    from .source_furniture import _match
    from .textlayer import build_text, horizontal_line, read_page

    out = PrintedEvidence()
    blocks = [b for b in parsed.blocks if b.type in CELL_TYPES and len({r.page_index for r in b.regions}) == 1]
    with pymupdf.open(pdf) as doc:
        for p in sorted({r.page_index for b in blocks for r in b.regions}):
            page = doc[p]
            native = read_page(page, p)
            out.native[p] = native
            w, h = native.width, native.height
            lines = []
            seen = set()
            def add(text, box):
                if _match(text, box, source_furniture or [], p):
                    return
                x, y = (box['x0'] + box['x1']) / 2, (box['y0'] + box['y1']) / 2
                identity = (round(x, 4), round(y, 4), text)
                if text and identity not in seen:
                    lines.append((x, y, text))
                    seen.add(identity)
            # Use the same glyph interpretation and spatial overprint handling as
            # the parser. Raw PDF lines count fake-bold/shadow copies twice and
            # soft hyphens as extra words, rejecting otherwise correct source grids.
            for fragment in native.fragments:
                text, _, _ = build_text([fragment], p, w)
                add(text, {'x0': fragment.x0, 'y0': fragment.y0, 'x1': fragment.x1, 'y1': fragment.y1})
            # read_page intentionally handles horizontal glyphs only. Meaningful
            # rotated cell labels still count as evidence for the source table.
            flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
            from .visible_text import read_dict
            for blk in read_dict(page, flags)["blocks"]:
                for ln in blk.get("lines", []):
                    if horizontal_line(ln):
                        continue
                    text = " ".join(span["text"].strip() for span in ln["spans"] if span["text"].strip())
                    x0, y0, x1, y1 = ln['bbox']
                    add(text, {'x0': x0 / w, 'y0': y0 / h, 'x1': x1 / w, 'y1': y1 / h})
            out.lines[p] = lines
        for b in blocks:
            captions = [other for other in blocks if b.type == 'table' and other.type == 'caption'
                        and other.id != b.id]
            # A coarse table box can include a caption already owned by a separate
            # block. Its exact printed line is not a source-table cell. Unowned
            # annotations stay in the evidence and must still be preserved.
            text = ' | '.join(value for p in sorted({r.page_index for r in b.regions})
                for x, y, value in out.lines.get(p, []) if value and any(
                    r.page_index == p and r.bbox.x0 <= x <= r.bbox.x1 and r.bbox.y0 <= y <= r.bbox.y1 for r in b.regions)
                and not any(_cell_text_key(value) == _cell_text_key(caption.text) and any(
                    r.page_index == p and r.bbox.x0 <= x <= r.bbox.x1 and r.bbox.y0 <= y <= r.bbox.y1
                    for r in caption.regions) for caption in captions))
            if text:
                out[b.id] = text
    return out


def _preserves_printed_text(original: str, replacement: str) -> bool:
    """Account for a few clipped word endings in a sheet's native text layer.

    This is only used with physical PDF evidence and a matched source grid. Numbers and
    single-letter values stay exact; whole occurrences are consumed even for clipped words.
    """
    wanted, have = Counter(_tokens(original)), Counter(_tokens(replacement))
    missing = wanted - have
    if not missing:
        return True
    if sum(missing.values()) > max(1, .05 * sum(wanted.values())):
        return False
    left = have - wanted
    for token, count in missing.items():
        if len(token) < 3 or not token.isalpha():
            return False
        for _ in range(count):
            full = next((word for word, n in left.items() if n > 0 and word.startswith(token)), None)
            if full is None:
                return False
            left[full] -= 1
    return True


def _sheet_pages(parsed: ParsedDocument, tables, rows_grams, table_grams, source_of: dict[str, int],
                 printed: dict[str, str] | None = None, attempted: set | None = None) -> None:
    """On each page of a workbook, the text that comes from one sheet becomes one table block
    (where the first of it was) with that part of the sheet; labels of a chart drawn from the
    sheet's cells go, as the chart's picture stands for them."""
    pictures: dict[int, list] = {}
    pieces: dict[str, list] = {}
    by_page: dict[int, list[tuple]] = {}
    for b in parsed.blocks:
        if b.type == "picture" and b.regions:
            pictures.setdefault(b.regions[0].page_index, []).append(b.regions[0].bbox)
        elif b.type in CELL_TYPES and b.regions and (cut := _pieces(b)):
            pieces[b.id] = cut
            for k, (page, text, regions, _) in enumerate(cut):
                if printed and b.id in printed:
                    text = printed[b.id]
                by_page.setdefault(page, []).append((b, k, _grams(text), regions, text))
    # Which sheet each page prints, and the pieces of text from it, judged by whole words: a run
    # of numbers ("1 2013 Alabama 2 2013 Alabama") reads as one string across the cells.
    table_words = [{w for row in table for cell in row for w in _words(cell)} for table in tables]
    sheet_of, members = {}, set()
    for page, entries in by_page.items():
        on_page = {w for e in entries for w in _words(e[4])}
        if len(on_page) < 3:
            continue
        t = sheet_of[page] = max(range(len(tables)), key=lambda t: len(on_page & table_words[t]))
        for b, k, _, _, text in entries:
            own = _words(text)
            if sum(w in table_words[t] for w in own) >= 0.8 * len(own):
                members.add((b.id, k))
    cell_keys: dict[int, list[list]] = {}
    made = {}
    for page, entries in by_page.items():
        if page not in sheet_of:
            continue
        use = [e for e in entries if (e[0].id, e[1]) in members]
        in_chart = [e for e in use if len(pieces[e[0].id]) == 1
                    and any(_within(e[3][0].bbox, p) for p in pictures.get(page, []))]
        cells = [e for e in use if e not in in_chart]
        shown = set().union(*(e[2] for e in cells)) if cells else set()
        t = sheet_of[page]
        if len(shown) < 6:
            continue
        if t not in cell_keys:
            cell_keys[t] = ([[_cell_key(cell) for cell in row] for row in tables[t]],
                            [[" ".join(_words(cell)) for cell in row] for row in tables[t]])
        physical = printed.in_regions([r for e in cells for r in e[3]]) if isinstance(printed, PrintedEvidence) else ""
        page_text = physical or " | ".join(e[4] for e in cells)
        original_text = ' '.join(pieces[e[0].id][e[1]][1] for e in cells)
        words = " " + " ".join(_words(page_text)) + " "
        # The part has to account for what the page shows (a sheet missing from the export
        # matches a column of years at best), and not hold far more than that. A page read
        # column by column has pairs only inside a column: its rows are found by their values,
        # which have to account for nearly all of the page (names alike in many rows match too).
        for by_pairs, cover, most in ((True, 0.6, 3.0), (False, 0.85, 1.2)):
            rows = _sheet_part(tables[t], *cell_keys[t], words, shown, by_pairs, page_text)
            # A wide sheet can clip a long note independently of its data rows.
            # Keep only its printed fragment before checking the occurrence budget;
            # otherwise the unseen suffix rejects a valid grid and leaves overlapping
            # native-text and layout-table copies of the same records in place.
            if rows and physical:
                rows = _clip_long_cells(rows, page_text, sheet=True)
            if not rows:
                continue
            if rows and physical and not _sheet_body_is_printed(rows, page_text, original_text):
                if attempted is not None:
                    attempted.update(e[0].id for e in cells)
                continue
            part = _grams(" ".join(" ".join(row) for row in rows))
            covered = len(part & shown) >= cover * len(shown)
            if rows and not by_pairs and not covered:
                # Read column by column, the page has its words in another order than the rows:
                # what runs across two words is no evidence then, only what is inside each.
                seen = set().union(*(_grams(w) for w in words.split()))
                got = set().union(*(_grams(w) for row in rows for cell in row for w in _words(cell)))
                covered = bool(seen) and len(got & seen) >= cover * len(seen)
            if rows and covered and len(part) <= most * len(shown):
                # Text of the page the part does not hold (cells of rows above it) stays as it is.
                # Numbers are held only as written in the part: "16" and "17" are anywhere in a
                # sheet of figures, "16.17" in one cell.
                said_in_order = " ".join(" ".join(row) for row in rows)

                def holds(text: str) -> bool:
                    return _preserves_printed_text(text, said_in_order) if physical else _preserves_text(text, said_in_order)

                held = [e for e in cells if holds(e[4])]
                # A header-only reconstruction must not be inserted beside an unchanged
                # overlapping body: that would duplicate rows and label the wrong area.
                held_text = printed.in_regions([r for e in held for r in e[3]]) if physical else " ".join(e[4] for e in held)
                coverage_text = page_text
                # Each block can independently match the same source word.
                # Replacing their union still needs every physical occurrence:
                # a sheet title "Department" and a column named "Department"
                # are two printed texts, even when either fits the source grid.
                # in_regions deduplicates overlapping boxes by position, so
                # duplicated layout ownership does not invent extra evidence.
                if physical and held and not _preserves_printed_text(held_text, said_in_order):
                    # A separate title/caption can reuse a word from a cell.
                    # Keep that block outside the replacement, rather than
                    # rejecting a fully evidenced table body alongside it.
                    # Prefer the largest body, then extend its physical budget.
                    selected = []
                    for entry in sorted(held, key=lambda e: len(_tokens(e[4])), reverse=True):
                        combined = printed.in_regions([r for e in selected + [entry] for r in e[3]])
                        if _preserves_printed_text(combined, said_in_order):
                            selected.append(entry)
                    remaining = [e for e in held if e not in selected]
                    # An unchanged block cannot keep ownership of text covered
                    # by the replacement. Ambiguous overlapping layouts stay
                    # on the conservative fallback path.
                    overlaps = any(a.page_index == b.page_index
                                   and min(a.bbox.x1, b.bbox.x1) > max(a.bbox.x0, b.bbox.x0)
                                   and min(a.bbox.y1, b.bbox.y1) > max(a.bbox.y0, b.bbox.y0)
                                   for e in remaining for a in e[3]
                                   for chosen in selected for b in chosen[3])
                    selected_text = printed.in_regions([r for e in selected for r in e[3]])
                    selected_original = ' '.join(pieces[e[0].id][e[1]][1] for e in selected)
                    if not overlaps and _sheet_body_is_printed(rows, selected_text, selected_original):
                        held = [e for e in held if e in selected]
                        held_text = selected_text
                        # These separate labels remain unchanged. Their size
                        # cannot count as missing table content after the whole
                        # selected body has been proved. Keep every other cell
                        # in the coverage budget, including unmatched pieces.
                        coverage_text = printed.in_regions([r for e in cells if e not in remaining for r in e[3]])
                if held and len(_tokens(held_text)) >= .8 * len(_tokens(coverage_text)) \
                        and (not physical or _preserves_printed_text(held_text, said_in_order)):
                    made[page] = (t, held, in_chart, rows)
                    break
    if not made:
        return
    used = {(e[0].id, e[1]) for page in made for e in made[page][1] + made[page][2]}
    after: dict[str, list] = {}
    gone: set[str] = set()
    for page in sorted(made):
        t, cells, in_chart, rows = made[page]
        anchor = cells[0][0]
        provenance = _source_ref(t, rows, [page])
        # Title rows above the column names ("All States Summary by year") are the sheet's heading,
        # not the table's header row, which a table cut in pieces repeats in each.
        titles = []
        # A title sits alone in the first column, above rows of several cells.
        while (len(rows) > 2 and len(rows[0]) >= 2 and not any(rows[0][1:])
               and any(sum(map(bool, row)) >= 2 for row in rows[1:])):
            titles.append(rows[0][0])
            rows = rows[1:]
        regions = [r for e in cells for r in e[3]]
        fields = {"type": "table", "level": None, "text": table_markdown(rows), "markdown": table_markdown(rows),
                  "lines": None, "line_geometry": "unavailable", "regions": regions,
                  "source": anchor.source.model_copy(update={"ref": provenance})}
        if len(pieces[anchor.id]) == 1:
            table = anchor.model_copy(update=fields)
        else:
            table = anchor.model_copy(update={**fields, "id": f"{anchor.id}-{page + 1}"})
        if titles := [x for x in titles if x]:
            heading = " · ".join(titles)
            after.setdefault(anchor.id, []).append((page, anchor.model_copy(update={
                "id": f"{table.id}-title", "type": "section_header", "level": 2, "text": heading,
                "markdown": f"### {heading}", "lines": None, "regions": regions[:1]})))
        after.setdefault(anchor.id, []).append((page, table))
        source_of[table.id] = t
        gone.update(e[0].id for e in cells + in_chart)
    # A block run on over pages keeps, as text of its own, the pages that did not become a table.
    byid = {b.id: b for b in parsed.blocks}
    for bid, cut in pieces.items():
        if len(cut) == 1 or not any((bid, k) in used for k in range(len(cut))):
            continue
        block = byid[bid]
        prefix = block.markdown[:len(block.markdown) - len(block.text)] if block.markdown and block.markdown.endswith(block.text) else ""
        for k, (page, text, regions, lines) in enumerate(cut):
            if (bid, k) not in used and text.strip():
                after.setdefault(bid, []).append((page, block.model_copy(update={
                    "id": f"{bid}-{page + 1}", "text": text, "markdown": f"{prefix}{text}" if prefix else None,
                    "regions": regions, "lines": lines})))
    blocks = []
    for b in parsed.blocks:
        if b.id in after:
            blocks += [x for _, x in sorted(after[b.id], key=lambda e: e[0])]
        elif b.id not in gone:
            blocks.append(b)
    parsed.blocks = blocks
    for k, b in enumerate(parsed.blocks):
        b.reading_order = k


def _printed_grid_header(parsed: ParsedDocument, block, tables, width, columns, cache):
    """A partial grid can still own a complete, visibly aligned preceding header.

    OCR-only cells can prevent full source restoration. That must not detach a
    known header that the layout parser left on the preceding page. Require all
    labels, in order, in one printed band and in the physical grid's columns;
    this authorizes only the header, never neighbouring source data rows.
    """
    if not block.regions:
        return None
    page = min(r.page_index for r in block.regions)
    cached = cache.get(page)
    if cached is None:
        return None
    _, boxes, grid_rows, _ = cached
    bounds = next(([boxes[cell] for cell in row] for row in grid_rows
        if len(row) == width and all(cell is not None and any(
            r.page_index == page and r.bbox.x0 <= (boxes[cell][0] + boxes[cell][2]) / 2 <= r.bbox.x1
            and r.bbox.y0 <= (boxes[cell][1] + boxes[cell][3]) / 2 <= r.bbox.y1
            for r in block.regions) for cell in row)), None)
    if bounds is None:
        return None
    at = next(i for i, candidate in enumerate(parsed.blocks) if candidate is block)
    preceding = []
    for part in reversed(parsed.blocks[:at]):
        if part.type in ('page_header', 'page_footer'):
            continue
        if part.type not in ('paragraph', 'section_header', 'list_item') or len(part.regions) != 1:
            break
        preceding.append(part)
        if len(preceding) == len(columns):
            break
    if len(preceding) != len(columns):
        return None
    taken = list(reversed(preceding))
    regions = [part.regions[0] for part in taken]
    header_pages = {r.page_index for r in regions}
    if len(header_pages) != 1 or next(iter(header_pages)) not in (page - 1, page):
        return None
    if max(r.bbox.y0 for r in regions) > min(r.bbox.y1 for r in regions) + .01:
        return None
    if page in header_pages and max(r.bbox.y1 for r in regions) > min(b[1] for b in bounds) + .01:
        return None
    if any(not bounds[column][0] - .01 <= (r.bbox.x0 + r.bbox.x1) / 2 <= bounds[column][2] + .01
           for column, r in zip(columns, regions)):
        return None
    printed_keys = [_cell_text_key(part.text) for part in taken]
    for table in tables:
        index = _header_row(table)
        if index is None or len(table[index]) != width:
            continue
        header = [table[index][column] for column in columns]
        if all(header) and [_cell_text_key(cell) for cell in header] == printed_keys:
            return header, taken
    return None


def _absorb_header_lines(parsed: ParsedDocument, source_of: dict[str, int], tables) -> None:
    """A table whose header row was left at the foot of the page before it comes with that row
    read as separate lines ("구 분", "한국형발사체", "시험발사체"); the restored table holds the row,
    so those lines join it (their place on the page is kept, their words are not said twice)."""
    def key(text: str) -> str:
        return KEEP.sub("", text).lower()

    gone: set[str] = set()
    for i, block in enumerate(parsed.blocks):
        if block.id not in source_of or not tables[source_of[block.id]]:
            continue
        # The restored table's own first row (a sheet's part starts lower than the sheet).
        header = {key(c) for c in block.text.split("\n", 1)[0].strip().strip("|").split("|") if key(c)}
        taken, j = [], i - 1
        while j >= 0 and len(taken) < len(header):
            prev = parsed.blocks[j]
            if prev.type in ("page_header", "page_footer"):
                j -= 1
                continue
            if prev.type not in ("paragraph", "section_header", "list_item") or key(prev.text) not in header:
                break
            taken.append(prev)
            j -= 1
        if len(taken) >= 2:
            block.regions = [r for b in reversed(taken) for r in b.regions] + block.regions
            gone.update(b.id for b in taken)
    if gone:
        parsed.blocks = [b for b in parsed.blocks if b.id not in gone]
        for k, b in enumerate(parsed.blocks):
            b.reading_order = k


def _absorb_fragments(parsed: ParsedDocument, source_of: dict[str, int], allow_headers: bool = True) -> None:
    """A piece the layout parser cut off a table (its header rows) that matched nothing on its own
    joins the neighbouring block that now holds its rows: on its page or the next one, saying
    what the piece says (a sheet's part holds some of the sheet, not each piece of it)."""
    tables = [b for b in parsed.blocks if b.type == "table"]
    gone = set()

    def pages(block) -> set[int]:
        return {r.page_index for r in block.regions}

    for i, block in enumerate(tables):
        if block.id in source_of:
            continue
        shown = _grams(block.text)
        for j in (i + 1, i - 1):
            if 0 <= j < len(tables) and tables[j].id in source_of and shown \
                    and any(abs(p - q) <= 1 for p in pages(block) for q in pages(tables[j])) \
                    and len(shown & _grams(tables[j].text)) >= 0.5 * len(shown):
                target = tables[j]
                overlap = bool(block.regions) and all(any(r.page_index == q.page_index and _within(r.bbox, q.bbox)
                    for q in target.regions) for r in block.regions)
                lines = [line for line in block.text.splitlines() if not re.match(r"\|\s*:?-{3}", line)]
                header_only = allow_headers and len(lines) == 1 and _preserves_text(block.text, target.text.split("\n", 1)[0])
                if not overlap and not header_only:
                    continue
                if not _preserves_text(block.text, tables[j].text):
                    continue
                target = tables[j]
                target.regions = block.regions + target.regions if j > i else target.regions + block.regions
                gone.add(block.id)
                break
    if gone:
        parsed.blocks = [b for b in parsed.blocks if b.id not in gone]
        for k, b in enumerate(parsed.blocks):
            b.reading_order = k
