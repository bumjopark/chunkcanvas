"""Carry a printed indexed header without changing source-body restoration."""
import pymupdf
import pytest
from app.parsers import source_tables as s
from app.parsers.sheet_grids import recover_sheet_grids
from tests.test_feedback_tables import document


@pytest.mark.parametrize('case', ['aligned', 'missing', 'shifted', 'different_source', 'not_indexed'])
def test_indexed_heading_requires_printed_cells_in_their_data_columns(tmp_path, case):
    heading = ['0', 'ReportYear', 'Region']
    data = [[str(i), '2013', 'Amber'] for i in range(1, 13)]
    if case == 'not_indexed':
        data = [[str(i + 20), year, region] for i, (_, year, region) in enumerate(data)]
    source = [[*heading, 'Hidden'], *[[*row, f'unprinted-{i}'] for i, row in enumerate(data)]]
    if case == 'different_source':
        source[0][1] = 'WrongSourceLabel'
    pdf = pymupdf.open()
    for pi, rows in enumerate([data[:6], data[6:]]):
        page = pdf.new_page(width=600, height=800)
        if pi == 0 and case != 'missing':
            shift = 100 if case == 'shifted' else 0
            for x, text in zip([51, 96, 161], heading):
                page.insert_text((x + shift, 100), text, fontsize=9)
        for i, row in enumerate(rows):
            for x, text in zip([89, 137, 161], row):
                page.insert_text((x, 120 + i * 20), text, fontsize=9)
    path = tmp_path / 'indexed.pdf'; pdf.save(path); pdf.close()
    first = ([heading] if case != 'missing' else []) + data[:6]
    coarse = [[row[0] if i == 0 and case != 'missing' else '',
               ' '.join(row[1:] if i == 0 and case != 'missing' else row)] for i, row in enumerate(first)]
    doc = document([('table', s.table_markdown(coarse), 0), ('table', s.table_markdown(data[6:]), 1)], pages=2)
    # With no physical proof this numeric index must not alter the earlier
    # source-body selection path, which can otherwise drop repeated records.
    assert s._header_row(source) is None
    assert recover_sheet_grids(doc, path, [source], set(), s._printed_cells(path, doc)) == 2
    expected = heading if case == 'aligned' else ['Column 1', 'Column 2', 'Column 3']
    for pi, rows in enumerate([data[:6], data[6:]]):
        tables = [b for b in doc.blocks if b.type == 'table' and b.regions[0].page_index == pi]
        assert len(tables) == 1
        assert tables[0].text == s.table_markdown([expected, *rows])
    whole = '\n'.join(b.text for b in doc.blocks)
    assert 'Hidden' not in whole and 'WrongSourceLabel' not in whole and 'unprinted-' not in whole
    assert whole.count('ReportYear') == (2 if case == 'aligned' else 0 if case == 'missing' else 1)
