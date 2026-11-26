"""Printed sheet rows stay one table when an accounting-format number keeps room for ')'."""
import pymupdf

from app.parsers.sheet_rows import PrintedRows
from app.parsers.textlayer import read_page

ROWS = [('Integer', '13'), ('Float', '13.12'), ('Currency', '$3.03'), ('Percent', '20%'), ('Scaled', '13.1')]


def _page(padding):
    pdf = pymupdf.open()
    page = pdf.new_page(width=595, height=842)
    right = 300
    for i, (label, value) in enumerate(ROWS):
        y = 100 + 20 * i
        page.insert_text((50, y), label, fontsize=10)
        if value.startswith('$'):
            # Accounting format: currency sign at the cell's left, number short of the right edge.
            page.insert_text((200, y), '$', fontsize=10)
            text, end = value[1:], right - padding
        else:
            text, end = value, right
        page.insert_text((end - pymupdf.get_text_length(text, fontsize=10), y), text, fontsize=10)
    return pdf, page


def test_an_accounting_number_inside_the_right_edge_keeps_the_rows_together():
    pdf, page = _page(2.5)
    grids = PrintedRows([[['Format', 'Value'], *[list(row) for row in ROWS]]]).grids(read_page(page, 0), [])
    pdf.close()
    assert len(grids) == 1 and len(grids[0].rows) == len(ROWS)


def test_text_cells_keep_the_tight_alignment_rule():
    # The same offset on a text column is not an accounting format: the run splits as before.
    rows = [('Integer', 'alpha'), ('Float', 'beta'), ('Currency', 'gamma'), ('Percent', 'delta'), ('Scaled', 'omega')]
    pdf = pymupdf.open()
    page = pdf.new_page(width=595, height=842)
    for i, (label, value) in enumerate(rows):
        y = 100 + 20 * i
        page.insert_text((50, y), label, fontsize=10)
        page.insert_text((205 if label == 'Currency' else 200, y), value, fontsize=10)
    grids = PrintedRows([[['Format', 'Value'], *[list(row) for row in rows]]]).grids(read_page(page, 0), [])
    pdf.close()
    assert not any(len(g.rows) == len(rows) for g in grids)
