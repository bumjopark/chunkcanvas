"""Text records must not become repeated column labels on later pages."""
import pymupdf
import pytest

from app.parsers import source_tables as s
from app.parsers.sheet_grids import recover_sheet_grids
from tests.test_feedback_tables import document


@pytest.mark.parametrize('damage', ['first_record', 'merged_record', 'missing_row'])
def test_text_only_records_do_not_pin_a_false_header_or_block_partial_recovery(tmp_path, damage):
    header = ['Category', 'Code', 'Person', 'Company']
    data = [['I', 'ALP', 'Alice', 'Amber'], ['I', 'BET', 'Brenda', 'Birch'],
            ['F', 'CED', 'Chloe', 'Cedar'], ['D', 'DEL', 'Doris', 'Delta']]
    source = [header, *data]
    # A printed exception cannot be assigned to a complete source record. It
    # must survive outside the grid without vetoing all other grounded rows.
    if damage == 'missing_row':
        data = [*data[:2], ['UNMATCHED', 'NOTE', '', ''], *data[2:]]
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    for i, row in enumerate(data):
        for x, text in zip([50, 140, 250, 380], row):
            page.insert_text((x, 100 + 20 * i), text, fontsize=9)
    path = tmp_path / 'records.pdf'
    pdf.save(path)
    pdf.close()
    original = data
    if damage == 'merged_record':
        original = [[a, b, c + ' ' + d] for a, b, c, d in data]
    doc = document([('table', s.table_markdown(original), 0)])
    assert recover_sheet_grids(doc, path, [source], set(), s._printed_cells(path, doc)) >= 1
    tables = [b for b in doc.blocks if b.type == 'table']
    assert tables
    for block in tables:
        # No header is printed on this page; generic labels are preferable to
        # repeating a person's record as column context in every chunk.
        assert block.text.splitlines()[0] == '| Column 1 | Column 2 | Column 3 | Column 4 |'
    whole = '\n'.join(b.text for b in doc.blocks)
    for row in source[1:]:
        assert whole.count('| ' + ' | '.join(row) + ' |') == 1
    if damage == 'missing_row':
        assert whole.count('UNMATCHED') == 1 and whole.count('NOTE') == 1
    assert 'Category' not in whole and 'Company' not in whole


def test_text_continuation_carries_observed_labels_instead_of_first_person(tmp_path):
    header = ['Category', 'Code', 'Person', 'Company']
    data = [['I', 'ALP', 'Alice', 'Amber'], ['F', 'BET', 'Brenda', 'Birch'],
            ['D', 'CED', 'Chloe', 'Cedar']]
    pdf = pymupdf.open()
    for rows in [[header], data]:
        page = pdf.new_page(width=600, height=800)
        for i, row in enumerate(rows):
            for x, text in zip([50, 140, 250, 380], row):
                page.insert_text((x, 100 + 20 * i), text, fontsize=9)
    path = tmp_path / 'continuation.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('table', s.table_markdown([header]), 0),
                    ('table', s.table_markdown(data), 1)], pages=2)
    assert recover_sheet_grids(doc, path, [[header, *data]], {'b0'}, s._printed_cells(path, doc)) == 1
    table = next(b for b in doc.blocks if b.type == 'table' and b.regions[0].page_index == 1)
    assert table.text == s.table_markdown([header, *data])


@pytest.mark.parametrize('protected', [False, True])
def test_complete_repeated_native_records_keep_their_table_and_occurrences(tmp_path, protected):
    # Exported row indices restart at zero for later groups. Identical visible
    # records can have distinct hidden cells and must stay separate occurrences.
    source = [['0', 'data_year', 'state', 'Hidden'], *[
        ['0', '2013', 'Arizona', f'unprinted-{i}'] for i in range(10)]]
    visible = [row[:3] for row in source[1:]]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    for i, row in enumerate(visible):
        for x, text in zip([50, 150, 300], row):
            page.insert_text((x, 100 + 20 * i), text, fontsize=9)
    path = tmp_path / 'repeated.pdf'; pdf.save(path); pdf.close()
    original = s.table_markdown(visible)
    doc = document([('table', original, 0)])
    assert recover_sheet_grids(doc, path, [source], {'b0'} if protected else set(), s._printed_cells(path, doc)) == 1
    assert len(doc.blocks) == 1 and doc.blocks[0].type == 'table'
    assert doc.blocks[0].text == s.table_markdown([['Column 1', 'Column 2', 'Column 3'], *visible])
    assert doc.blocks[0].text.count('| 0 | 2013 | Arizona |') == 10
    assert 'unprinted-' not in doc.blocks[0].text


def test_clipped_text_records_with_blank_columns_recover_without_hidden_suffixes(tmp_path):
    header = ['Type', 'Code', 'Person', 'Company', 'Address', 'City']
    source = [header, ['I', 'ALP', 'AnneLongName', '', 'FirstLongRoad', 'North'],
              ['I', 'BET', 'BertLongName', 'Co', 'SecondLongRoad', 'South'],
              ['I', 'CED', 'CoraLongName', '', 'ThirdLongRoad', 'East'],
              ['I', 'DEL', 'DanaLongName', '', 'FourthLongRoad', 'West']]
    visible = [[a, b, c[:4], d, e[:5], f] for a, b, c, d, e, f in source[1:]]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    for i, row in enumerate(visible):
        for x, text in zip([45, 100, 180, 270, 350, 460], row):
            page.insert_text((x, 100 + 20 * i + (20 if i >= 2 else 0)), text, fontsize=9)
    page.insert_text((45, 140), 'Independent unmatched annotation', fontsize=9)
    path = tmp_path / 'clipped.pdf'; pdf.save(path); pdf.close()
    coarse = [[a, b, c + ' ' + d, e + ' ' + f] for a, b, c, d, e, f in visible]
    coarse.insert(2, ['Independent unmatched annotation', '', '', ''])
    doc = document([('table', s.table_markdown(coarse), 0)])
    assert recover_sheet_grids(doc, path, [source], set(), s._printed_cells(path, doc)) == 2
    text = '\n'.join(b.text for b in doc.blocks)
    assert text.count('Independent unmatched annotation') == 1
    for i, row in enumerate(visible):
        # The later run has no company cells printed at all. Its five visible
        # columns remain distinct without importing the unused sixth column.
        printed = row if i < 2 else [value for value in row if value]
        assert text.count('| ' + ' | '.join(printed) + ' |') == 1
    for word in ['AnneLongName', 'BertLongName', 'FirstLongRoad', 'SecondLongRoad']:
        assert word not in text
    assert all(b.text.splitlines()[0].startswith('| Column 1 |') for b in doc.blocks if b.type == 'table')
