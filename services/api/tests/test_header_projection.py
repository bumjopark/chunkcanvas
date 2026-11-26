"""Column proofs agree with an independent exhaustive source-window oracle."""
import random

from app.parsers.header_projection import SourceIndex, _projection, match


def test_full_and_near_row_owners_match_exhaustive_small_windows():
    rng = random.Random(922813)
    for _ in range(300):
        source = [tuple(str(rng.randrange(3)) for _ in range(2)) for _ in range(rng.randrange(2, 9))]
        printed = [tuple(str(rng.randrange(3)) for _ in range(2)) for _ in range(rng.randrange(2, 5))]
        owners = {j: set() for j in range(len(printed))}
        full = False
        for start in range(len(source) - len(printed) + 1):
            window = source[start:start + len(printed)]
            differences = sum(a != b for actual, expected in zip(printed, window) for a, b in zip(actual, expected))
            full |= differences == 0
            if differences <= 1:
                for j in owners:
                    owners[j].add(start + j)
            for at in range(len(printed) - 1):
                swapped = list(window)
                swapped[at], swapped[at + 1] = swapped[at + 1], swapped[at]
                if swapped == printed:
                    for j in owners:
                        owners[j].add(start + (j + 1 if j == at else j - 1 if j == at + 1 else j))
        expected = (full, owners) if any(owners.values()) and len(set(printed)) >= 2 else None
        assert _projection(printed, 0, (0, 1), SourceIndex([source], lambda s: s)) == expected


def test_an_exhausted_search_is_cached_and_cannot_become_a_heading():
    source = [['A', 'B', 'C'], ['1', '2', '3'], ['4', '5', '6']]
    printed = [['1', '2'], ['4', '5']]
    index = SourceIndex([source], lambda s: s)
    index.remaining = 1
    assert match(printed, index, lambda s: s) is None
    spent = index.remaining
    assert match(printed, index, lambda s: s) is None
    assert index.remaining == spent


def test_source_index_construction_is_charged_to_the_document_budget():
    small = SourceIndex([[['a', 'b']]], lambda s: s)
    larger = SourceIndex([[['a', 'b']] * 100], lambda s: s)
    assert small.remaining - larger.remaining == 198


def test_empty_source_rows_do_not_break_a_printed_run_but_keep_original_positions():
    source = [['Name', 'Code', 'Count'], ['Alpha', 'A01', '1'], ['', '', ''], ['Beta', 'B02', '2']]
    rows = [source[1], ['', '', ''], source[3]]
    found = match(rows, SourceIndex([source], lambda s: s), lambda s: s)
    assert found is not None
    assert found[1] == {0: 0, 1: 1, 2: 2}
    assert found[2] == {0: {1}, 1: {2}, 2: {3}}


def test_nonempty_source_records_cannot_be_discarded_like_blank_rows():
    source = [['Name', 'Code', 'Count'], ['Alpha', 'A01', '1'], ['Other', 'X99', '9'], ['Beta', 'B02', '2']]
    assert match([source[1], source[3]], SourceIndex([source], lambda s: s), lambda s: s) is None


def test_a_leading_split_cell_suffix_is_proven_by_following_records():
    source = [['Field', 'Details'], ['Previous', 'Previous page. Continuation tail'],
              ['Second', 'Second detail'], ['Third', 'Third detail']]
    rows = [['', 'Continuation tail'], source[2], source[3]]
    found = match(rows, SourceIndex([source], lambda s: s), lambda s: s)
    assert found and found[1] == {0: 0, 1: 1}
    assert found[2] == {0: {1}, 1: {2}, 2: {3}}
    # A suffix in the middle of a record run is not a page continuation.
    assert match([source[2], rows[0], source[3]], SourceIndex([source], lambda s: s), lambda s: s) is None


def test_rounded_cells_are_compatible_but_need_two_other_exact_anchors():
    source = [['Item', 'Code', 'Rate'], ['Alpha', 'A01', '12.345'], ['Beta', 'B02', '7.512']]
    rows = [['Alpha', 'A01', '12.35'], ['Beta', 'B02', '7.51']]
    assert match(rows, SourceIndex([source], lambda s: s), lambda s: s)
    assert match([r[1:] for r in rows], SourceIndex([[r[1:] for r in source]], lambda s: s), lambda s: s) is None


def test_a_wide_source_does_not_hide_a_small_proven_printed_projection():
    source = [[f'Field {c}' for c in range(300)],
              *[[f'Record {r} cell {c}' for c in range(300)] for r in range(3)]]
    printed = [[row[287], row[299]] for row in source[1:]]
    found = match(printed, SourceIndex([source], lambda s: s), lambda s: s)
    assert found and found[1] == {0: 287, 1: 299}


def test_partial_source_gaps_require_the_document_cell_context():
    source = [['Item', 'Code', 'Value'], ['Alpha', 'A01', ''], ['Beta', 'B02', '7']]
    rows = [['Alpha', 'A01', '5'], ['Beta', 'B02', '7']]
    # A sheet's extracted numeric cell is not an unknown image transcription.
    assert match(rows, SourceIndex([source], lambda s: s), lambda s: s) is None
    assert match(rows, SourceIndex([source], lambda s: s, allow_missing=True), lambda s: s)


def test_a_leading_suffix_is_not_a_second_complete_record():
    source = [['Category', 'Description', 'Score'], ['Second', 'start tail', '20'], ['Total', 'Gamma', '30']]
    rows = [['', 'tail', '20'], ['Total', 'Gamma', '30']]
    assert match(rows, SourceIndex([source], lambda s: s), lambda s: s) is None
