"""Shared values need one column projection supported by several complete records."""
import random

from app.parsers.carried_headers import _row_mapping, carry_source_headers
from app.parsers.source_tables import table_markdown
from test_carried_headers import _doc, _numbered


def test_common_count_rows_resolve_the_right_hand_columns_together():
    rng = random.Random(74192)
    names = [f'Period {i + 1}' for i in range(14)]
    source = [['Zone', *names]] + [[f'Zone {i}', *[str(rng.randrange(5)) for _ in names]] for i in range(900)]
    rows = [row[8:15] for row in source[401:429]]
    doc = _doc(_numbered(rows))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text == table_markdown([names[7:], *rows])


def test_joint_column_evidence_is_independent_of_page_visit_order():
    rng = random.Random(8813)
    labels = [f'Bucket {i}' for i in range(8)]
    source = [['Group', *labels]] + [[f'Group {i}', *[str(rng.randrange(4)) for _ in labels]] for i in range(600)]
    a, b = [r[1:5] for r in source[50:70]], [r[5:] for r in source[400:420]]
    forward, backward = _doc(_numbered(a), _numbered(b)), _doc(_numbered(b), _numbered(a))
    assert carry_source_headers(forward, [source]) == carry_source_headers(backward, [source]) == 2
    assert [x.text for x in forward.blocks] == [x.text for x in reversed(backward.blocks)]
    assert forward.blocks[0].text == table_markdown([labels[:4], *a])
    assert forward.blocks[1].text == table_markdown([labels[4:], *b])


def test_equal_column_projections_stay_unknown_even_with_distinct_records():
    source = [['A1', 'A2', 'B1', 'B2'], ['1', '2', '1', '2'], ['3', '4', '3', '4']]
    rows = [['1', '2'], ['3', '4']]
    doc = _doc(_numbered(rows))
    assert carry_source_headers(doc, [source]) == 0


def test_repeated_values_only_name_columns_shared_by_all_explanations():
    source = [['Code', 'Value', 'First', 'Last'], ['tag', '0', 'tag', 'tag'], ['tip', '1', 'tip', 'tip']]
    printed = [['tag', '0', 'tag'], ['tip', '1', 'tip']]
    doc = _doc(_numbered(printed))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text == table_markdown([['Code', 'Value', 'Column 3'], *printed])


def test_joint_column_alignment_keeps_a_clipped_cell_before_an_exact_duplicate():
    source = [['Prefix', 'Value', 'Note'], ['alphabet', 'alpha', 'tail'], ['betamax', 'beta', 'last']]
    printed = [['alpha', 'alpha', 'tail'], ['beta', 'beta', 'last']]
    doc = _doc(_numbered(printed))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text == table_markdown([source[0], *printed])


def test_omitted_middle_columns_are_solved_as_one_projection():
    rng = random.Random(3310)
    names = [f'Field {i}' for i in range(12)]
    source = [names] + [[str(rng.randrange(7)) for _ in names] for _ in range(500)]
    columns = [2, 5, 8, 9]
    rows = [[row[c] for c in columns] for row in source[100:130]]
    doc = _doc(_numbered(rows))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text == table_markdown([[names[c] for c in columns], *rows])


def test_identical_records_in_differently_named_sections_do_not_choose_the_first():
    records = [['1', '2', '3'], ['4', '5', '6'], ['7', '8', '9']]
    source = [['Count', 'Value', 'Rate'], *records, ['', '', ''],
              ['Weight', 'Length', 'Amount'], *records]
    doc = _doc(_numbered(records))
    assert carry_source_headers(doc, [source]) == 0


def test_sampled_rare_rows_cannot_hide_a_better_common_row_projection():
    rare = [[f'{100 + i}', f'{200 + i}'] for i in range(8)]
    common = [['10', '20'], ['30', '40']] * 46
    source = [[['Wrong A', 'Wrong B'], *rare, *common[:72]],
              [['Actual A', 'Actual B'], *common]]
    doc = _doc(_numbered([*rare, *common]))
    assert carry_source_headers(doc, source) == 0


def test_joint_sequence_cannot_discard_an_out_of_order_record_to_name_columns():
    rows = [[str(100 + i), str(200 + i)] for i in range(10)]
    tables = [[['Wrong A', 'Wrong B'], *rows[:8]],
              [['Actual A', 'Actual B'], *rows[1:], rows[0]]]
    doc = _doc(_numbered(rows))
    assert carry_source_headers(doc, tables) == 0
    assert doc.blocks[0].text == _numbered(rows)


def test_equal_row_paths_cannot_choose_which_printed_record_to_ignore():
    rows = [[str(100 + i), str(200 + i)] for i in range(5)]
    source = [['Field A', 'Field B'], rows[1], rows[0], *rows[2:]]
    doc = _doc(_numbered(rows))
    assert carry_source_headers(doc, [source]) == 0


def test_row_alignment_does_not_claim_an_ambiguous_duplicate_value():
    mapping, exact = _row_mapping(['tag', '0', 'tag'], ['tag', '0', 'tag', 'tag'])
    assert mapping == {0: 0, 1: 1} and exact == 2


def test_joint_row_alignment_keeps_a_clipped_cell_before_an_exact_duplicate():
    mapping, exact = _row_mapping(['alpha', 'alpha', 'tail'], ['alphabet', 'alpha', 'tail'])
    assert mapping == {0: 0, 1: 1, 2: 2} and exact == 2


