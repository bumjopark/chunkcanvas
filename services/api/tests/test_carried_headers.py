"""`Column N` headings are replaced by the source heading only when printed rows prove the columns."""
from app.models import ParsedDocument
from app.parsers.carried_headers import carry_source_headers
from app.parsers.source_tables import table_markdown


def _doc(*tables_text):
    return ParsedDocument.model_validate({
        'document': {'id': 'd', 'filename': 'synthetic', 'fileHash': 'h', 'pageCount': 2,
                     'sourceFormat': 'xlsx', 'parser': 'hybrid', 'parserVersion': 'test'},
        'pages': [{'index': i, 'width': 600, 'height': 800} for i in range(2)],
        'blocks': [{'id': f'b{i}', 'readingOrder': i, 'type': 'table', 'text': text, 'markdown': text,
                    'regions': [{'pageIndex': 1, 'bbox': {'x0': .1, 'y0': .1 + .2 * i, 'x1': .9, 'y1': .25 + .2 * i}}],
                    'source': {'parser': 'test', 'ref': 'printed-sheet-grid:page:1;grid:0'}}
                   for i, text in enumerate(tables_text)],
        'blocksFingerprint': 'f'})


SOURCE = [['Region', 'Status', 'Owner'], *[[f'Region{i:02}', 'Active' if i % 2 else 'Paused', f'Owner{i:02}'] for i in range(1, 41)]]


def _numbered(rows):
    return table_markdown([[f'Column {i + 1}' for i in range(len(rows[0]))], *rows])


def test_continued_table_gets_the_heading_of_its_source_table():
    rows = SOURCE[21:26]
    doc = _doc(_numbered(rows))
    assert carry_source_headers(doc, [SOURCE]) == 1
    assert doc.blocks[0].text == table_markdown([SOURCE[0], *rows])
    assert 'carried-header:table:0:row:0' in doc.blocks[0].source.ref
    assert doc.blocks[0].lines is None


def test_clipped_heading_taken_as_data_becomes_the_heading_again():
    printed = [['Regi', 'Status', 'Own'], *SOURCE[1:4]]
    doc = _doc(_numbered(printed))
    assert carry_source_headers(doc, [SOURCE]) == 1
    assert doc.blocks[0].text == table_markdown([SOURCE[0], *SOURCE[1:4]])


def test_rows_found_in_two_source_tables_are_left_alone():
    other = [['Area', 'State', 'Lead'], *SOURCE[1:]]
    doc = _doc(_numbered(SOURCE[5:9]))
    assert carry_source_headers(doc, [SOURCE, other]) == 0
    assert doc.blocks[0].text.startswith('| Column 1 |')


def test_a_later_section_uses_its_own_heading():
    source = [['Region', 'Status', 'Owner'], ['North', 'Active', 'Kim'], ['South', 'Paused', 'Lee'],
              ['', '', ''],
              ['Project', 'Phase', 'Lead'], ['Atlas', 'Design', 'Park'], ['Boreal', 'Build', 'Choi'],
              ['Cobalt', 'Test', 'Jung']]
    doc = _doc(_numbered(source[5:8]))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text.splitlines()[0] == '| Project | Phase | Lead |'


def test_one_matching_row_is_not_proof():
    doc = _doc(_numbered([SOURCE[7], ['Unrelated', 'value', 'here']]))
    assert carry_source_headers(doc, [SOURCE]) == 0


def test_named_headings_and_unmatched_tables_are_unchanged():
    named = table_markdown([['Area', 'State', 'Lead'], *SOURCE[1:4]])
    unmatched = _numbered([['alpha', 'beta', 'gamma'], ['delta', 'epsilon', 'zeta']])
    doc = _doc(named, unmatched)
    assert carry_source_headers(doc, [SOURCE]) == 0
    assert [b.text for b in doc.blocks] == [named, unmatched]


def test_a_new_section_between_heading_and_rows_blocks_the_first_heading():
    # Text-only rows after a blank row start a new labelled section; without a usable
    # heading of their own the rows keep their numbered labels rather than borrow one.
    source = [['Region', 'Status', 'Owner'], ['North', 'Active', 'Kim'], ['', '', ''],
              ['Notes', 'Remarks', 'Extra'], ['1', '2', '3'], ['Atlas', 'Design', 'Park'], ['Boreal', 'Build', 'Choi']]
    doc = _doc(_numbered(source[5:7]))
    changed = carry_source_headers(doc, [source])
    heading = doc.blocks[0].text.splitlines()[0]
    assert changed == 0 or heading == '| Notes | Remarks | Extra |'
    assert 'Region' not in heading


def test_an_edge_column_with_unmatched_values_takes_the_only_remaining_source_label():
    source = [['Command', 'Explanation', 'Result'], ['TIMES', 'Multiply two values', ''],
              ['SUM', 'Add the values', ''], ['MAX', 'Largest value', '']]
    printed = [['TIMES', 'Multiply two values', '2 x 5 = 10'], ['SUM', 'Add the values', '3 + 4 = 7']]
    doc = _doc(_numbered(printed))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text.splitlines()[0] == '| Command | Explanation | Result |'


def test_a_merged_leading_label_names_the_unproven_first_column():
    # '구 분' spans two source columns; the printed first column holds section labels only.
    source = [['Group', '', 'Code', 'Name'],
              ['Service', '', 'SER-001', 'Chat service'], ['Service', '', 'SER-002', 'Search'],
              ['Manage', '', 'PMR-001', 'Staffing'], ['Manage', '', 'PMR-002', 'Reporting'],
              ['Manage', '', 'PMR-003', 'Security plan']]
    printed = [['Manage', '', ''], ['', 'PMR-002', 'Reporting'], ['', 'PMR-003', 'Security plan']]
    doc = _doc(_numbered(printed))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text.splitlines()[0] == '| Group | Code | Name |'


def test_a_two_row_printed_heading_taken_as_data_becomes_one_stacked_heading():
    source = [['Item', '', 'FY2026', '', 'FY2027', ''],
              ['', '', 'Jul', 'Aug', 'Jan', 'Feb'],
              ['Build', 'Install', 'x', '', '', ''], ['Build', 'Train', '', 'x', '', ''],
              ['Run', 'Operate', '', '', 'x', 'x']]
    printed = [list(row) for row in source]
    doc = _doc(_numbered(printed))
    assert carry_source_headers(doc, [source]) == 1
    lines = doc.blocks[0].text.splitlines()
    assert lines[0] == '| Item |  | FY2026 Jul | Aug | FY2027 Jan | Feb |'
    assert lines[2:] == table_markdown([source[0], *source[2:]]).splitlines()[2:]


def test_a_vertically_merged_first_label_repeated_below_is_not_doubled():
    source = [['Item', '', 'FY2026', '', 'FY2027', ''],
              ['Item', '', 'Jul', 'Aug', 'Jan', 'Feb'],
              ['Build', 'Install', 'x', '', '', ''], ['Build', 'Train', '', 'x', '', ''],
              ['Run', 'Operate', '', '', 'x', 'x']]
    printed = [list(row) for row in source]
    printed[1][0] = ''
    doc = _doc(_numbered(printed))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text.splitlines()[0] == '| Item |  | FY2026 Jul | Aug | FY2027 Jan | Feb |'


def test_several_unproven_edge_columns_take_the_remaining_source_labels_in_order():
    source = [['Name', 'Kind', 'Size', 'Layout', 'Packing'],
              ['FileHeader', 'Header', '256', '', ''], ['DocInfo', 'Info', '512', '', ''],
              ['BodyText', 'Body', '1024', '', '']]
    printed = [['DocInfo', 'Info', '512', 'see 3.2', 'zip'], ['BodyText', 'Body', '1024', 'see 4.1', 'zip']]
    doc = _doc(_numbered(printed))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text.splitlines()[0] == '| Name | Kind | Size | Layout | Packing |'


def test_common_values_do_not_crowd_out_the_row_sharing_every_cell():
    # Hundreds of source rows share the value "0"; the printed rows still prove their source.
    source = [['Code', 'Owner', 'Open', 'Closed']] + [[f'C{i:03}', f'Team{i % 7}', '0', '0'] for i in range(600)]
    printed = [source[401], source[402], source[403]]
    doc = _doc(_numbered(printed))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text.splitlines()[0] == '| Code | Owner | Open | Closed |'


def test_a_record_below_a_heading_merged_over_the_proven_columns_is_never_the_heading():
    # Only the item columns are proven on a continuation page; the heading is merged (blank)
    # in them, and the first all-text record must not be read as column names.
    source = [['Item', '', 'FY2026', '', 'FY2027', ''], ['', '', 'Jul', 'Aug', 'Jan', 'Feb'],
              *[[f'Task{i}', f'Step{i}', *['x' if i % 4 == c else '' for c in range(4)]] for i in range(40)]]
    doc = _doc(_numbered(source[30:34]))
    assert carry_source_headers(doc, [source]) == 0
    assert doc.blocks[0].text.startswith('| Column 1 |')


def test_a_first_record_with_a_blank_lead_is_not_stacked_into_the_heading():
    # The heading has no merged cell, so the record below it cannot continue the heading.
    source = [['Group', 'Item', 'Code', 'Name'], ['', 'Chat', 'SER-001', 'Service'],
              ['Manage', 'Staffing', 'PMR-001', 'Plan'], ['Manage', 'Report', 'PMR-002', 'Monthly']]
    doc = _doc(_numbered([list(row) for row in source]))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text == table_markdown(source)


def test_rows_made_only_of_very_common_values_prove_nothing():
    # "0" fills hundreds of source cells; such rows cannot single out a source row.
    source = [['Code', 'Jan', 'Feb', 'Mar']] + [[f'C{i:03}', '0', '0', '0'] for i in range(500)]
    doc = _doc(_numbered([['', '0', '0', '0'], ['', '0', '0', '0'], ['', '0', '0', '0']]))
    assert carry_source_headers(doc, [source]) == 0


def test_a_gantt_record_below_a_two_row_heading_stays_a_record():
    # Years over months, the first label merged down; the first task row leaves every month
    # blank and must not become a third heading row.
    source = [['Area', '', 'FY2026', '', '', 'FY2027', '', ''], ['Area', '', 'Jul', 'Aug', 'Sep', 'Jan', 'Feb', 'Mar'],
              ['Build platform', 'Install servers', *[''] * 6], ['Build platform', 'Train staff', *[''] * 6],
              ['Run service', 'Operate', *[''] * 6], ['Run service', 'Monitor', *[''] * 6]]
    doc = _doc(_numbered([list(row) for row in source]))
    assert carry_source_headers(doc, [source]) == 1
    lines = doc.blocks[0].text.splitlines()
    assert lines[0] == '| Area |  | FY2026 Jul | Aug | Sep | FY2027 Jan | Feb | Mar |'
    assert lines[2:] == table_markdown([source[0], *source[2:]]).splitlines()[2:]


def test_a_table_nested_in_two_columns_uses_its_own_heading():
    # A record of the outer table opens a two-column inner table; a continuation page shows
    # only inner rows, which take the inner heading rather than the outer one.
    source = [['Type', 'Length', 'Description', ''], ['BYTE array', '32', 'signature', ''],
              ['DWORD', '4', 'version', ''], ['UINT32', '4', 'Range', 'Effect'],
              ['', '', 'bit 0', 'compressed'], ['', '', 'bit 1', 'encrypted'],
              ['', '', 'bit 2', 'distributed'], ['', '', 'bit 3', 'script']]
    doc = _doc(_numbered([row[2:] for row in source[5:8]]))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text.splitlines()[0] == '| Range | Effect |'


def test_sub_headings_under_group_headings_are_a_heading():
    source = [['North', '', 'South', ''], ['Owner', 'Status', 'Owner', 'Status'],
              ['Kim', 'Active', 'Lee', 'Paused'], ['Park', 'Paused', 'Choi', 'Active'], ['Jung', 'Active', 'Han', 'Active']]
    doc = _doc(_numbered([row[:2] for row in source[3:5]]))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text.splitlines()[0] == '| Owner | Status |'


def test_a_row_repeating_one_printed_value_does_not_outrank_the_matching_row():
    # "n/a" fills nine cells of a totals row; each printed row's own record holds it once.
    heading = ['Item', 'Plan', 'Actual', 'Note', *[''] * 6]
    totals = ['Totals', *['n/a'] * 9]
    records = [['A-100', '12,345', 'n/a', '1,234'], ['A-200', '23,456', 'n/a', '2,345'],
               ['A-300', '34,567', '4,567', '3,456']]
    source = [heading, totals, *[row + [''] * 6 for row in records]]
    doc = _doc(_numbered([row[1:4] for row in records]))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text.splitlines()[0] == '| Plan | Actual | Note |'


# Behaviour locks from an independent review of attempts that were reverted: each case
# carried a wrong heading or moved a record into the heading. `Column N` is the right answer
# whenever the printed rows do not prove the columns.

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def test_a_right_hand_page_of_a_count_matrix_never_gets_wrong_month_labels():
    # Small counts repeat across columns; a page showing Jul..Dec must not be labelled Jan..
    source = [['Area', *MONTHS]] + [[f'Area{i:04}', *[str((i * 31 + j * 17 + (i * j) % 7) % 10) for j in range(12)]]
                                    for i in range(700)]
    doc = _doc(_numbered([row[7:] for row in source[300:340]]))
    changed = carry_source_headers(doc, [source])
    heading = doc.blocks[0].text.splitlines()[0]
    assert changed == 0 or heading == '| ' + ' | '.join(MONTHS[6:]) + ' |'


def test_rows_of_zeros_with_one_identifiable_row_do_not_pick_the_columns():
    def sheet(prefix):
        rows = [['Area', *[f'{prefix}{i}' for i in range(1, 7)]]]
        rows += [[f'{prefix}Area{i}', *['0'] * 6] for i in range(150)]
        rows += [[f'{prefix}Row{i}', *[str(1 + (i * 7 + j) % 9) for j in range(6)]] for i in range(300)]
        return rows
    a, b = sheet('A'), sheet('B')
    a[200] = ['ARowX', '5', '6', '1234', '5678', '4321', '8765']
    doc = _doc(_numbered([['0'] * 4 for _ in range(10)] + [a[200][3:]]))
    changed = carry_source_headers(doc, [a, b])
    assert changed == 0 or doc.blocks[0].text.splitlines()[0] == '| A3 | A4 | A5 | A6 |'


def test_a_record_under_merged_groups_stays_in_the_body():
    names = ['Kim', 'Lee', 'Park', 'Choi', 'Jung', 'Kang', 'Cho', 'Yoon', 'Jang', 'Lim', 'Han', 'Oh',
             'Seo', 'Shin', 'Kwon', 'Hwang', 'Ahn', 'Song', 'Ryu', 'Jeon', 'Hong', 'Ko', 'Moon', 'Yang']
    source = [['Site', 'Morning', '', 'Evening', '']] + [[f'Site{i}', *names[4 * i:4 * i + 4]] for i in range(6)]
    printed = [row[1:] for row in source[:5]]
    doc = _doc(_numbered(printed))
    carry_source_headers(doc, [source])
    lines = doc.blocks[0].text.splitlines()
    assert lines[0] != '| Morning Kim | Lee | Evening Park | Choi |'
    if lines[0] == '| Morning |  | Evening |  |':
        assert lines[2:] == table_markdown([printed[0], *printed[1:]]).splitlines()[2:]


def test_a_page_showing_one_merged_group_keeps_its_second_heading_row():
    source = [['Item', 'Plan', '', '', 'Actual', '', ''], ['', 'Q1', 'Q2', 'Q3', 'Q1', 'Q2', 'Q3']]
    source += [[f'Item{i:02}', *[f'{i * 7 + q}.{q}' for q in range(1, 7)]] for i in range(1, 30)]
    doc = _doc(_numbered([row[:4] for row in source[:7]]))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text.splitlines()[0] == '| Item | Plan Q1 | Q2 | Q3 |'


def test_rows_of_zeros_shared_by_two_tables_stay_unproven():
    first = [['Area', 'Jan', 'Feb']] + [['0', '0', '0'] for _ in range(300)]
    second = [['Unit', 'Q1', 'Q2']] + [['0', '0', '0'] for _ in range(300)]
    doc = _doc(_numbered([['0', '0', '0'], ['0', '0', '0'], ['0', '0', '0']]))
    assert carry_source_headers(doc, [first, second]) == 0


def test_an_empty_heading_on_a_continuation_page_is_left_alone():
    source = [['Air', '', 'Argon', ''], ['CFM', 'Cost', 'CFM', 'Cost'],
              *[[f'{i}.5', f'${i}.10', f'{i + 40}.5', f'${i + 40}.20'] for i in range(1, 30)]]
    rows = source[10:15]
    text = table_markdown([[''] * 4, *rows])
    doc = _doc(text)
    assert carry_source_headers(doc, [source]) == 0
    assert doc.blocks[0].text == text


def test_small_zero_rows_do_not_fall_back_to_column_votes():
    source = [['Item', 'Mon', 'Tue', 'Wed', 'Thu'],
              ['Bolts', '0', '1', '0', '0'], ['Nuts', '1', '0', '0', '0']]
    doc = _doc(_numbered([['0', '0'], ['0', '0']]))
    assert carry_source_headers(doc, [source]) == 0


def test_clipped_names_share_the_same_column_proof_as_complete_cells():
    source = [['Name', 'Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'Q6'],
              ['Station North complete', '3', '4', '7', '8', '5', '9'],
              ['Station South complete', '5', '6', '9', '1', '2', '2'],
              ['Station East complete', '7', '8', '1', '3', '4', '8']]
    printed = [['Station North', '3', '4'], ['Station South', '5', '6'], ['Station East', '7', '8']]
    doc = _doc(_numbered(printed))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text == table_markdown([source[0][:3], *printed])


def test_two_small_zero_sections_do_not_borrow_the_lower_heading():
    source = [['', 'Type', 'Complex', 'Medium', 'Simple', 'None'],
              ['', 'Word', '0', '0', '0', '0'], ['', 'Excel', '0', '0', '0', '0'],
              ['', 'PowerPoint', '0', '0', '0', '0'], ['', 'Total', '0', '0', '0', '0'],
              ['', '', '', '', '', ''],
              ['', 'Type', '', 'Complex', 'Minor', 'None'],
              ['', 'Word', '', '0', '0', '0'], ['', 'Excel', '', '0', '0', '0'],
              ['', 'PowerPoint', '', '0', '0', '0'], ['', 'Total', '', '0', '0', '0']]
    printed = [['', '0', '0', '0']] * 3 + [['Total', '0', '0', '0']]
    doc = _doc(_numbered(printed))
    assert carry_source_headers(doc, [source]) == 0
    assert doc.blocks[0].text == _numbered(printed)


def test_numeric_sections_without_a_blank_gap_do_not_borrow_the_first_heading():
    source = [['Count', 'Rate'], ['1', '2'], ['3', '4'],
              ['Weight', 'Height'], ['5', '6'], ['7', '8'], ['9', '10']]
    doc = _doc(_numbered(source[4:]))
    assert carry_source_headers(doc, [source]) == 0
    assert doc.blocks[0].text == _numbered(source[4:])


def test_an_unproven_empty_column_does_not_borrow_a_hidden_column_heading():
    source = [['Name', 'Units', '', 'Date'],
              ['Alpha', '3', '', '2026'], ['Bravo', '4', '', '2027']]
    rows = [[row[0], row[2], row[3]] for row in source[1:]]
    doc = _doc(_numbered(rows))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text.splitlines()[0] == '| Name | Column 2 | Date |'
    assert doc.blocks[0].text.splitlines()[2:] == _numbered(rows).splitlines()[2:]


def test_a_proven_empty_column_keeps_its_source_heading():
    source = [['Name', 'Units', 'Date'],
              ['Alpha', '', '2026'], ['Bravo', '', '2027']]
    doc = _doc(_numbered(source[1:]))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text == table_markdown(source)


def test_unknown_image_cells_do_not_borrow_an_ambiguous_source_heading():
    source = [['Name', 'Units', '', 'Date'],
              ['Alpha', '', '', '2026'], ['Bravo', '', '', '2027']]
    rows = [['Alpha', 'Picture A', '2026'], ['Bravo', 'Picture B', '2027']]
    doc = _doc(_numbered(rows))
    assert carry_source_headers(doc, [source]) == 1
    assert doc.blocks[0].text == table_markdown([['Name', 'Column 2', 'Date'], *rows])


def test_promoted_printed_labels_survive_missing_or_ambiguous_source_headings():
    for hidden in [False, True]:
        source = ([['Name', 'Units', '', 'Date'],
                   ['Alpha', '', '', '2026'], ['Bravo', '', '', '2027']] if hidden else
                  [['Name', '', 'Date'], ['Alpha', '', '2026'], ['Bravo', '', '2027']])
        for label in ['Units', 'Uni', 'Un', 'U', 'Visible field', '12.3',
                      '\U0001d44b', '+', '-', '|', '\U0001f60a']:
            rows = [['Name', label, 'Date'],
                    ['Alpha', 'Picture A', '2026'], ['Bravo', 'Picture B', '2027']]
            doc = _doc(_numbered(rows))
            assert carry_source_headers(doc, [source]) == 1
            assert doc.blocks[0].text == table_markdown(rows)
