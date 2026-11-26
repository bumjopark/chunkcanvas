import pymupdf
import pytest

from tests.test_feedback_tables import document
from app.parsers import source_tables as s
from app.parsers.sheet_grids import recover_sheet_grids


@pytest.mark.parametrize('case', ['aligned', 'different_source', 'shifted', 'new_section'])
def test_printed_partial_header_keeps_only_aligned_column_names(tmp_path, case):
    names = ['Item', 'Current', 'Prior', 'Ratio']
    records = [['Alpha', '11', '10', '1.10', '1'],
               ['Bravo', '24', '20', '1.20', '1'],
               ['Cedar', '39', '30', '1.30', '1'],
               ['Delta', '56', '40', '1.40', '1']]
    source = [[*names, 'Unprinted label'], *records[:2]]
    if case == 'different_source':
        source[0][2] = 'Different'
    if case == 'new_section':
        source += [[''] * 5, ['Product', 'Next', 'Previous', 'Factor', 'Not shown']]
    source += records[2:]
    pdf = pymupdf.open()
    for i, data in enumerate([records[:2], records[2:]]):
        page = pdf.new_page(width=600, height=800)
        if i == 0:
            shift = 75 if case == 'shifted' else 0
            for x in (40, 140, 240, 340, 440):
                page.draw_line((x + shift, 80), (x + shift, 120))
            for y in (80, 120):
                page.draw_line((40 + shift, y), (440 + shift, y))
            for x, text in zip((50, 150, 250, 350), names):
                page.insert_text((x + shift, 105), text, fontsize=9)
        for ri, row in enumerate(data):
            for x, text in zip((50, 150, 250, 350, 460), row):
                page.insert_text((x, 150 + ri * 25), text, fontsize=9)
    path = tmp_path / 'partial-printed-header.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('table', ' '.join(names) + ' ' + ' '.join(' '.join(row) for row in records[:2]), 0),
                    ('table', ' '.join(' '.join(row) for row in records[2:]), 1)], pages=2)
    assert recover_sheet_grids(doc, path, [source], set(), s._printed_cells(path, doc)) == 2
    for pi, rows in enumerate([records[:2], records[2:]]):
        block = next(b for b in doc.blocks if b.type == 'table' and b.regions[0].page_index == pi)
        allowed = case in ('aligned', 'new_section') and (pi == 0 or case != 'new_section')
        header = [*names, 'Column 5'] if allowed else [f'Column {i}' for i in range(1, 6)]
        assert block.text == s.table_markdown([header, *rows])
        assert 'Unprinted label' not in block.text and 'Not shown' not in block.text
