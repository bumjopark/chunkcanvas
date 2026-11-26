"""A physically merged summary and a taller detail heading are different schemas."""
from collections import Counter

import pymupdf
import pytest

from app.models import ParsedDocument
from app.parsers.carried_headers import _cells
from app.parsers.printed_header_bands import restore_printed_header_bands
from app.parsers.source_tables import table_markdown, _cell_text_key


def _fixture(tmp_path, rows, merges, heights=None, open_last=False):
    width = len(rows[0])
    pdf = pymupdf.open()
    page = pdf.new_page(width=60 + width * 90, height=600)
    y = 50
    for ri, row in enumerate(rows):
        height = (heights or {}).get(ri, 20)
        c = 0
        while c < width:
            span = merges.get((ri, c), 1)
            x = 30 + c * 90
            if open_last and c == width - 1 and ri in (0, 4, 5):
                page.draw_line((x, y), (x + span * 90, y))
                page.draw_line((x, y + height), (x + span * 90, y + height))
            else:
                page.draw_rect(pymupdf.Rect(x, y, x + span * 90, y + height))
            if row[c]:
                page.insert_text((x + 3, y + height - 5), row[c], fontsize=8)
            c += span
        y += height
    path = tmp_path / 'bands.pdf'
    pdf.save(path)
    pdf.close()
    text = table_markdown([[''] * width, *rows])
    doc = ParsedDocument.model_validate({
        'document': {'id': 'd', 'filename': 'synthetic.xlsx', 'fileHash': 'h', 'pageCount': 1,
                     'sourceFormat': 'xlsx', 'parser': 'pymupdf', 'parserVersion': 'test'},
        'pages': [{'index': 0, 'width': 60 + width * 90, 'height': 600}],
        'blocks': [{'id': 'b0', 'readingOrder': 0, 'type': 'table', 'text': text, 'markdown': text,
                    'regions': [{'pageIndex': 0, 'bbox': {'x0': 0, 'y0': 0, 'x1': 1, 'y1': 1}}],
                    'source': {'parser': 'pymupdf', 'ref': 'page1'}}], 'blocksFingerprint': 'f'})
    return doc, path


def _content(doc):
    return Counter(''.join(_cell_text_key(c) for b in doc.blocks for i, line in enumerate(b.text.splitlines())
                           if i != 1 for c in _cells(line)))


@pytest.mark.parametrize('open_last', [False, True])
def test_summary_and_detail_headers_use_their_own_columns_and_keep_all_records(tmp_path, open_last):
    source = [['North', '', 'South', '', 'Balance'], ['Units', 'Cost', 'Units', 'Cost', 'Amount'],
              ['1', '$2', '3', '$4', '$6'], ['Item', 'Note', '', 'Count', 'Rate'],
              ['R1', 'memo', '', '7', '8'], ['R1', 'memo', '', '7', '8']]
    merges = {(0, 0): 2, (0, 2): 2, (3, 1): 2, (4, 1): 2, (5, 1): 2}
    doc, path = _fixture(tmp_path, source, merges, {3: 45}, open_last=open_last)
    before = _content(doc)
    assert restore_printed_header_bands(doc, [source], path) == 1
    assert len(doc.blocks) == 2
    assert doc.blocks[0].text == table_markdown([
        ['North Units', 'Cost', 'South Units', 'Cost', 'Balance Amount'], source[2]])
    assert doc.blocks[1].text == table_markdown(source[3:])
    assert _content(doc) == before
    assert [b.reading_order for b in doc.blocks] == [0, 1]
    assert len({b.id for b in doc.blocks}) == 2
    assert doc.blocks[0].regions[0].bbox.y1 <= doc.blocks[1].regions[0].bbox.y0


def test_a_projected_record_under_groups_is_not_a_subheading(tmp_path):
    source = [['Site', 'Morning', '', 'Evening', ''],
              ['SiteA', 'Kim', 'Lee', 'Park', 'Choi'], ['SiteB', 'Jung', 'Kang', 'Cho', 'Yoon'],
              ['SiteC', 'Jang', 'Lim', 'Han', 'Oh']]
    printed = [row[1:] for row in source]
    doc, path = _fixture(tmp_path, printed, {(0, 0): 2, (0, 2): 2})
    before = doc.dump()
    assert restore_printed_header_bands(doc, [source], path) == 0
    assert doc.dump() == before


def test_an_empty_continuation_without_printed_labels_stays_unchanged(tmp_path):
    source = [['North', '', 'South', ''], ['Units', 'Cost', 'Units', 'Cost'],
              ['1', '$2', '3', '$4'], ['5', '$6', '7', '$8'], ['9', '$10', '11', '$12']]
    doc, path = _fixture(tmp_path, source[2:], {})
    before = doc.dump()
    assert restore_printed_header_bands(doc, [source], path) == 0
    assert doc.dump() == before


def test_an_unproven_later_text_row_is_not_silently_given_the_summary_heading(tmp_path):
    source = [['North', '', 'South', ''], ['Units', 'Cost', 'Units', 'Cost'],
              ['1', '$2', '3', '$4'], ['Alpha', 'Beta', 'Gamma', 'Delta'],
              ['7', '$8', '9', '$10'], ['11', '$12', '13', '$14']]
    doc, path = _fixture(tmp_path, source, {(0, 0): 2, (0, 2): 2})
    before = doc.dump()
    assert restore_printed_header_bands(doc, [source], path) == 0
    assert doc.dump() == before
