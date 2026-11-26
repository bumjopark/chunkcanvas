"""Visible content and structure must survive independent PDF clipping/overpaint."""
import pytest
import pymupdf

from app.parsers.pymupdf_parser import extract
from app.parsers.textlayer import read_page


@pytest.mark.parametrize('section', ['merged', 'repeated', 'blank', 'numeric'])
def test_form_section_does_not_inherit_unrelated_personal_fields(section):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s

    first = ['Applicant', 'Position', 'Full name', 'Birth date']
    separator = {
        'merged': ['B. Qualifications', '', '', ''],
        'repeated': ['B. Qualifications'] * 4,
        'blank': ['', '', '', ''],
        'numeric': ['12', '', '', ''],
    }[section]
    records = [['Certificate', 'Subject', 'Issued', 'Issuer'],
               ['Professional', 'Engineering', '2023', 'Institute']]
    doc = document([('table', s.table_markdown(records), 0)])
    assert s.apply_source_tables(doc, [[first, separator, *records]],
                                 columns_overflow=False) == 1
    expected = records if section in ('merged', 'repeated') else [first, *records]
    assert doc.blocks[0].text == s.table_markdown(expected)


@pytest.mark.parametrize('ownership', ['overflow', 'neighbor', 'separate', 'ambiguous', 'spaced', 'truncated', 'incomplete_neighbor'])
def test_visible_word_overflow_keeps_its_source_cell(tmp_path, ownership):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids

    heading = ['Row', 'Field', 'Source', 'API']
    records = [['1', 'First', 'ALPHACODETAIL', ''],
               ['2', 'Second', 'BRAVOCODETAIL', '']]
    if ownership in ('neighbor', 'incomplete_neighbor'):
        for row in records:
            row[3] = 'OTHER'
    split = [[row[0], row[1], row[2][:9], row[2][9:]] for row in records]
    source = [heading, *(split if ownership in ('separate', 'ambiguous') else records)]
    if ownership == 'ambiguous':
        source += records
    if ownership == 'truncated':
        source = [heading, *[[r[0], r[1], r[2] + 'HIDDEN', r[3]] for r in records]]
    if ownership == 'incomplete_neighbor':
        source = [heading, *[[r[0], r[1], r[2], r[3] + 'HIDDEN'] for r in records]]
    pdf = pymupdf.open()
    page = pdf.new_page(width=500, height=300)
    for x in (40, 100, 240, 300, 440):
        page.draw_line((x, 80), (x, 170))
    for y in (80, 110, 140, 170):
        page.draw_line((40, y), (440, y))
    for i, row in enumerate([heading, *records]):
        if ownership == 'spaced' and i:
            row = split[i - 1]
        for x, value in zip((45, 105, 245, 345 if i else 305), row):
            page.insert_text((x, 100 + 30*i), value, fontsize=10, fontname='cour')
    path = tmp_path / 'visible-overflow.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('paragraph', ' '.join(' '.join(row) for row in [heading, *records]), 0)])
    assert recover_sheet_grids(doc, path, [source], set(), s._printed_cells(path, doc)) == 1
    expected = records if ownership in ('overflow', 'neighbor', 'truncated') else split
    if ownership == 'incomplete_neighbor':
        expected = [[r[0], r[1], r[2][:9], r[2][9:] + ' OTHER'] for r in records]
    assert [b.text for b in doc.blocks] == [s.table_markdown([heading, *expected])]


@pytest.mark.parametrize('ownership', ['wrapped', 'separate', 'ambiguous', 'different_block', 'incomplete'])
def test_wrapped_visible_tail_keeps_its_source_row(tmp_path, ownership):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids

    heading = ['Row', 'Label', 'Kind', 'Count']
    records = [['1', 'ALPHACODETAIL', 'Text', '10'], ['2', 'SECOND', 'Text', '20']]
    split = [['1', 'ALPHACODE', 'Text', '10'], ['2', 'TAIL SECOND', 'Text', '20']]
    source = [heading, *(split if ownership in ('separate', 'ambiguous') else records)]
    if ownership == 'ambiguous':
        source += records
    if ownership == 'incomplete':
        source = [heading, ['1', 'ALPHACODETAILHIDDEN', 'Text', '10'], records[1]]
    pdf = pymupdf.open()
    page = pdf.new_page(width=500, height=300)
    for x in (40, 100, 300, 380, 460):
        page.draw_line((x, 80), (x, 200))
    for y in (80, 110, 140, 200):
        page.draw_line((40, y), (460, y))
    for ri, row in enumerate([heading, *records]):
        for ci, (x, value) in enumerate(zip((45, 105, 305, 385), row)):
            if ri == 1 and ci == 1:
                if ownership == 'different_block':
                    page.insert_text((x, 137), 'ALPHACODE', fontsize=10, fontname='cour')
                    page.insert_text((x, 175), 'TAIL', fontsize=10, fontname='cour')
                else:
                    page.insert_text((x, 137), 'ALPHACODE\nTAIL', fontsize=10, fontname='cour')
            else:
                page.insert_text((x, [100, 137, 190][ri]), value, fontsize=10, fontname='cour')
    path = tmp_path / 'wrapped-cell.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('paragraph', ' '.join(' '.join(row) for row in [heading, *records]), 0)])
    assert recover_sheet_grids(doc, path, [source], set(), s._printed_cells(path, doc)) == 1
    expected = records if ownership == 'wrapped' else split
    assert [b.text for b in doc.blocks] == [s.table_markdown([heading, *expected])]


@pytest.mark.parametrize('source_format', ['csv', 'pdf'])
def test_generated_csv_keeps_titles_and_edge_records_chunkable(source_format):
    from app.parsers.base import finish

    def block(text, kind, y):
        return {'type': kind, 'text': text, 'source': {'parser': 'test'},
                'regions': [{'pageIndex': 0, 'bbox': {
                    'x0': .1, 'y0': y, 'x1': .8, 'y1': y + .02}}]}

    raw = [block('Quarterly measurements.', 'page_header', .04),
           block('| Distance | Time |\n| --- | --- |\n| 10 | 2 |', 'table', .2),
           block('Written and saved in OpenOffice.', 'page_footer', .9),
           block('1', 'paragraph', .96)]
    parsed = finish({'id': 'csv-edges', 'filename': 'data.' + source_format,
                     'fileHash': 'test', 'sourceFormat': source_format,
                     'parser': 'test', 'parserVersion': '1', 'pageCount': 1},
                    [{'index': 0, 'width': 600, 'height': 800}], raw)
    body = '\n'.join(b.text for b in parsed.blocks
                     if b.type not in ('page_header', 'page_footer'))
    assert ('Quarterly measurements.' in body) == (source_format == 'csv')
    assert ('Written and saved in OpenOffice.' in body) == (source_format == 'csv')
    assert any(b.text == '1' and b.type == 'paragraph' for b in parsed.blocks) == (source_format == 'csv')
    assert '| Distance | Time |' in body


@pytest.mark.parametrize('damage', ['outside', 'cell', 'clipped'])
def test_light_parser_keeps_tables_using_only_visible_cell_values(tmp_path, damage):
    pdf = pymupdf.open()
    page = pdf.new_page(width=400, height=400)
    for x in (40, 170, 300):
        page.draw_line((x, 100), (x, 190))
    for y in (100, 130, 160, 190):
        page.draw_line((40, y), (300, y))
    rows = [('Name', 'Amount'), ('Apple', '10'), ('Pear', '20')]
    for y, row in zip((120, 150, 180), rows):
        for x, text in zip((50, 180), row):
            page.insert_text((x, y), text, fontsize=10)
    if damage == 'outside':
        page.insert_text((50, 60), 'STALE')
        page.draw_rect((45, 45, 100, 65), color=None, fill=(1, 1, 1))
    elif damage == 'cell':
        page.draw_rect((170, 130, 300, 160), color=None, fill=(1, 1, 1))
        page.insert_text((180, 150), '99', fontsize=10)
        page.draw_rect((170, 130, 300, 160))
    else:
        page.insert_text((50, 60), 'CLIPPED', fontsize=10)
        stream = page.get_contents()[-1]
        pdf.update_stream(stream, b'q 0 0 65 400 re W n\n' + pdf.xref_stream(stream) + b'\nQ')
    assert read_page(page, 0).visibility_filtered
    path = tmp_path / 'table.pdf'
    pdf.save(path)
    pdf.close()
    _, blocks = extract(path, progress=lambda *_: None)
    tables = [b for b in blocks if b['type'] == 'table']
    assert len(tables) == 1
    records = [[cell.strip() for cell in row.split('|') if cell.strip()]
               for row in tables[0]['text'].splitlines() if '---' not in row]
    assert records == [['Name', 'Amount'], ['Apple', '99' if damage == 'cell' else '10'], ['Pear', '20']]
    assert 'STALE' not in '\n'.join(b['text'] for b in blocks)


@pytest.mark.parametrize('text,header', [
    ('이름,부서,직급\n김가람,연구,대리\n이하늘,영업,과장', ['이름','부서','직급']),
    ('first,last\nAlice,Park\nBob,Kim', ['first','last']),
    ('name\nalpha\nbeta', ['name']),
    ('Item,2022,2023,2024\nSales,10,12,15\nCosts,7,8,9', ['Item','2022','2023','2024']),
    ('id,qty,price,\n1,2,3.5,\n2,4,1.25,', ['id','qty','price','']),
    ('구 분,5월,,6월,,비고\n판매,1,2,3,4,없음\n반품,0,0,0,0,없음', ['구 분','5월','','6월','','비고']),
    ('구 분,5월,세부사항,,6월,,,비고,검토란\n판매,진행,우선,,대기,,,없음,검토\n반품,완료,보통,,예정,,,없음,승인',
     ['구 분','5월','세부사항','','6월','','','비고','검토란']),
    # Years over measures are pivot headings (production behaviour).
    ('2022,2023,2024\n10,12,15\n7,8,9', ['2022','2023','2024']),
])
def test_csv_recognizes_text_year_and_empty_headings(text, header):
    from app.convert import csv_body
    from lxml import html
    body, notices = csv_body(text)
    tree = html.fromstring(body)
    assert 'csv_header_inferred' in notices
    assert [c.text_content() for c in tree.xpath('.//th')] == header
    assert len(tree.xpath('.//tbody/tr')) == len(text.splitlines()) - 1


@pytest.mark.parametrize('text', [
    'apple,red,fruit\ncarrot,orange,vegetable\nbanana,yellow,fruit',
    '1,2,3\n4,5,6\n7,8,9',
    'Seoul,Korea\nBusan,Korea\nTokyo,Japan',
    'kim@example.com,Kim\nlee@example.com,Lee',
])
def test_csv_does_not_consume_headerless_first_record(text):
    from app.convert import csv_body
    from lxml import html
    body, notices = csv_body(text)
    tree = html.fromstring(body)
    assert 'csv_header_unverified' in notices
    assert [c.text_content() for c in tree.xpath('.//tbody/tr[1]/td')] == text.splitlines()[0].split(',')


@pytest.mark.parametrize('clip_header', [False, True])
def test_fifty_borderless_rows_keep_printed_cell_fragments_and_columns(tmp_path, clip_header):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    rows = [['Index', 'Code', 'Description', 'Amount']] + [
        [str(i), f'Record{i:02}', f'CompleteLabel{i:02}', str(i * 10)] for i in range(1, 51)]
    printed = [list(row) for row in rows]
    printed[25][2] = 'Complete'
    if clip_header:
        printed[0][2] = 'Descr'
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=1000)
    for i, row in enumerate(printed):
        for x, value in zip((40, 110, 230, 450), row):
            page.insert_text((x, 100 + 15*i), value, fontsize=8)
    path = tmp_path / 'fifty-rows.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('paragraph', ' '.join(' '.join(row) for row in printed), 0)])
    s.apply_source_tables(doc, [rows], sheets=True, pdf=path)
    assert [b.type for b in doc.blocks] == ['table']
    assert doc.blocks[0].text == s.table_markdown(printed)
    assert 'CompleteLabel25' not in doc.blocks[0].text
    assert s.last_stats['uncertain_blocks'] > 0  # partial source spelling remains explicit


def test_numeric_physical_grid_does_not_need_a_word_in_every_row(tmp_path):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids
    rows = [['Pressure', 'Rate']] + [[str(i), str(i * 3)] for i in range(1, 21)]
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    for x in (40, 200, 400):
        page.draw_line((x, 80), (x, 500))
    for i in range(22):
        y = 80 + 20*i
        page.draw_line((40, y), (400, y))
    for i, row in enumerate(rows):
        for x, value in zip((50, 210), row):
            page.insert_text((x, 95 + 20*i), value, fontsize=9)
    path = tmp_path / 'numbers.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('paragraph', ' '.join(' '.join(row) for row in rows), 0)])
    printed = s._printed_cells(path, doc)
    assert recover_sheet_grids(doc, path, [rows], set(), printed) == 1
    assert [b.type for b in doc.blocks] == ['table']
    assert doc.blocks[0].text == s.table_markdown(rows)


def test_unrelated_numeric_rows_do_not_exhaust_printed_record_candidates(tmp_path):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids
    rows = [['Record alpha', '12', '34'], ['Record beta', '12', '34']]
    # These rows share both numbers, but cannot possibly supply the printed
    # letters. Reject them before applying the bounded ambiguous-row search.
    source = rows + [[str(i), '12', '34'] for i in range(100, 180)]
    pdf = pymupdf.open()
    page = pdf.new_page(width=500, height=400)
    for i, row in enumerate(rows):
        for x, value in zip((40, 200, 320), row):
            page.insert_text((x, 100 + 20 * i), value, fontsize=10)
    path = tmp_path / 'many-unrelated-rows.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('paragraph', ' '.join(' '.join(row) for row in rows), 0)])
    assert recover_sheet_grids(doc, path, [source], set(), s._printed_cells(path, doc)) == 1
    assert [b.type for b in doc.blocks] == ['table']
    assert doc.blocks[0].text == s.table_markdown([['Column 1', 'Column 2', 'Column 3'], *rows])


def test_remaining_lines_keep_their_order_between_recovered_tables(tmp_path):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids
    rows = [['alpha', '10'], ['bravo', '20'], ['gamma', '30'], ['delta', '40']]
    lines = ['Before', 'alpha 10', 'bravo 20', 'Between', 'gamma 30', 'delta 40', 'After']
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    page.insert_text((50, 100), lines, fontsize=10, fontname='cour', lineheight=1.4)
    assert len(page.get_text('blocks')) == 1
    path = tmp_path / 'interleaved.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('paragraph', ' '.join(lines), 0)])
    assert recover_sheet_grids(doc, path, [rows], set(), s._printed_cells(path, doc)) == 2
    assert [b.type for b in doc.blocks] == ['paragraph', 'table', 'paragraph', 'table', 'paragraph']
    assert [b.text for b in doc.blocks if b.type == 'paragraph'] == ['Before', 'Between', 'After']
    assert all(word in doc.blocks[1].text for word in ['alpha', 'bravo'])
    assert all(word in doc.blocks[3].text for word in ['gamma', 'delta'])


@pytest.mark.parametrize('amounts', [['1.00', '2.00', '3.00'], ['-', '--', '---']])
def test_recovering_only_headings_does_not_detach_existing_sparse_table_data(tmp_path, amounts):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids
    rows = [['Purpose', 'Amount', 'Currency'], ['Description', 'Value', 'Code']]
    rows.extend(['', value, ''] for value in amounts)
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    for i, row in enumerate(rows):
        for x, value in zip((50, 200, 350), row):
            page.insert_text((x, 100 + i * 16), value, fontsize=9)
    path = tmp_path / 'sparse-body.pdf'
    pdf.save(path)
    pdf.close()
    original = s.table_markdown(rows)
    doc = document([('table', original, 0)])
    assert recover_sheet_grids(doc, path, [rows], set(), s._printed_cells(path, doc)) == 0
    assert [(b.type, b.text) for b in doc.blocks] == [('table', original)]


def test_borderless_matching_refuses_competing_column_assignments():
    from app.parsers.sheet_rows import PrintedRows
    from app.parsers.textlayer import read_page
    pdf = pymupdf.open()
    page = pdf.new_page()
    for x, value in zip((50, 200, 350), ('Record01', 'Abbrevi', '10')):
        page.insert_text((x, 100), value)
    native = read_page(page, 0)
    locator = PrintedRows([[['Record01', 'Abbreviated', '', '10'],
                           ['Record01', '', 'Abbreviation', '10']]])
    assert locator.locate(native.fragments, native) is None
    pdf.close()


@pytest.mark.parametrize('show_header', [False, True])
def test_borderless_grid_keeps_wrapped_header_and_literal_overflow_hashes(tmp_path, show_header):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    rows = [['Diameter of Leak', '150 PSI', '', '125 PSI', ''],
            ['PipeA', '1.20', '$15.00', '3.40', '$45.00'],
            ['PipeB', '2.20', '$25.00', '4.40', '$55.00'],
            ['PipeC', '3.20', '$35.00', '5.40', '$65.00']]
    printed = [row[:] for row in rows]
    printed[2][1] = '###'
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    if show_header:
        for x, y, value in [(50, 83, 'Diameter of'), (50, 95, 'Leak'),
                            (200, 95, '150 PSI'), (370, 95, '125 PSI')]:
            page.insert_text((x, y), value, fontsize=9)
    for i, row in enumerate(printed[1:]):
        for x, value in zip((50, 200, 280, 370, 450), row):
            page.insert_text((x, 115 + i*15), value, fontsize=9)
    path = tmp_path / 'wrapped-header.pdf'
    pdf.save(path)
    pdf.close()
    native_text = ' '.join(' '.join(row) for row in (printed if show_header else printed[1:]))
    doc = document([('paragraph', native_text, 0)])
    s.apply_source_tables(doc, [rows], sheets=True, pdf=path)
    assert [b.type for b in doc.blocks] == ['table']
    # Without a printed heading the proven source heading row is carried (claude/fix-20261106).
    expected = printed
    assert doc.blocks[0].text == s.table_markdown(expected)
    assert '2.20' not in doc.blocks[0].text


def test_two_column_clipped_label_keeps_separate_side_annotation(tmp_path):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    rows = [['Parameter', 'Value'], ['Metric01', '101'], ['Metric02', '102'],
            ['VERY_LONG_LABEL', '103'], ['Metric04', '104'], ['Metric05', '105']]
    printed = [r[:] for r in rows]
    printed[3][0] = 'VERY_LONG'
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    for i, row in enumerate(printed):
        for x, value in zip((50, 200), row):
            page.insert_text((x, 100 + 20*i), value, fontsize=9)
    page.insert_text((400, 160), 'Separate annotation', fontsize=9)
    path = tmp_path / 'side-note.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('paragraph', ' '.join(' '.join(row) for row in printed) + ' Separate annotation', 0)])
    s.apply_source_tables(doc, [rows], sheets=True, pdf=path)
    tables = [b for b in doc.blocks if b.type == 'table']
    assert len(tables) == 1 and tables[0].text == s.table_markdown(printed)
    assert [b.text for b in doc.blocks if b.type != 'table'] == ['Separate annotation']


def test_light_table_keeps_caption_from_the_same_pdf_text_block(tmp_path, monkeypatch):
    # Pin detection to this fixture's drawn cells. The optional layout model can
    # classify the caption itself as a heading; this tests glyph ownership after
    # the table geometry has been found, independently of that classification.
    find_tables = pymupdf.Page.find_tables
    monkeypatch.setattr(pymupdf.Page, 'find_tables',
                        lambda page, **kwargs: find_tables(page, strategy='lines_strict', use_layout=False))
    pdf = pymupdf.open()
    page = pdf.new_page(width=400, height=400)
    for x in (40, 170, 300):
        page.draw_line((x, 94), (x, 130))
    for y in (94, 106, 118, 130):
        page.draw_line((40, y), (300, y))
    page.insert_text((50, 88), ['Caption', 'Name', 'Apple', 'Pear'], fontsize=10, lineheight=1.2)
    page.insert_text((180, 100), ['Amount', '10', '20'], fontsize=10, lineheight=1.2)
    page.insert_text((50, 50), 'HIDDEN')
    page.draw_rect((45, 35, 100, 55), color=None, fill=(1, 1, 1))
    path = tmp_path / 'caption.pdf'
    pdf.save(path)
    pdf.close()
    _, blocks = extract(path, progress=lambda *_: None)
    assert len([b for b in blocks if b['type'] == 'table']) == 1
    assert any(b['type'] != 'table' and b['text'] == 'Caption' for b in blocks)
    text = '\n'.join(b['text'] for b in blocks)
    assert text.count('Caption') == text.count('Apple') == text.count('Pear') == 1
    assert 'HIDDEN' not in text


def test_csv_title_and_blank_rows_do_not_hide_the_actual_headings():
    from app.convert import csv_body
    from lxml import html
    body, notices = csv_body('Quarterly measurements,,\n,,\n,,\n,Distance,Time\n,10,2\n,20,4')
    tree = html.fromstring('<div>' + body + '</div>')
    assert 'csv_header_inferred' in notices
    assert tree.xpath('./p/text()') == ['Quarterly measurements']
    assert [cell.text_content() for cell in tree.xpath('.//th')] == ['', 'Distance', 'Time']
    assert [row.xpath('./td/text()') for row in tree.xpath('.//tbody/tr')] == [['10','2'], ['20','4']]
    assert body.count('Quarterly measurements') == 1


def test_source_column_match_cannot_split_one_expanded_unicode_glyph():
    from app.parsers.sheet_rows import PrintedRows
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((50, 100), 'alpha aßb omega')
    native = read_page(page, 0)
    locator = PrintedRows([[['alpha', 'as', 'sb', 'omega']]])
    assert locator.locate(native.fragments, native) is None
    pdf.close()


def test_wrapped_header_proof_cannot_take_a_suffix_of_another_label():
    from app.parsers.sheet_rows import PrintedRows
    pdf = pymupdf.open()
    page = pdf.new_page()
    for y, values in [(80, ['Item', 'Gross Amount']), (100, ['alpha', '100']), (120, ['beta', '200'])]:
        for x, value in zip((50, 250), values):
            page.insert_text((x, y), value, fontsize=10)
    native = read_page(page, 0)
    locator = PrintedRows([[['Item', 'Amount'], ['alpha', '100'], ['beta', '200']]])
    grids = locator.grids(native, [])
    assert len(grids) == 1 and not grids[0].printed_header
    pdf.close()


def test_complete_source_matches_do_not_prefer_more_columns():
    from app.parsers.sheet_rows import PrintedRows
    pdf = pymupdf.open()
    page = pdf.new_page()
    for x, value in zip((50, 200, 350), ('alpha', 'ab', 'omega')):
        page.insert_text((x, 100), value)
    native = read_page(page, 0)
    locator = PrintedRows([[['alpha', 'ab', '', 'omega'], ['alpha', 'a', 'b', 'omega']]])
    assert locator.locate(native.fragments, native) is None
    pdf.close()


@pytest.mark.parametrize('heading, source_heading', [(None, False), (['', 'Category'], True), (['', 'Category'], False)])
def test_numeric_grid_keeps_first_record_out_of_column_headings(tmp_path, heading, source_heading):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids
    body = [['2000', '14'], ['2001', '18'], ['2002', '27']]
    rows = ([heading] if heading else []) + body
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    for x in (40, 200, 400):
        page.draw_line((x, 80), (x, 80 + 20 * len(rows)))
    for i in range(len(rows) + 1):
        page.draw_line((40, 80 + 20*i), (400, 80 + 20*i))
    for i, row in enumerate(rows):
        for x, value in zip((50, 210), row):
            page.insert_text((x, 95 + 20*i), value, fontsize=9)
    path = tmp_path / 'numeric-grid.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('paragraph', ' '.join(' '.join(row) for row in rows), 0)])
    assert recover_sheet_grids(doc, path, [rows if source_heading else body], set(), s._printed_cells(path, doc)) == 1
    assert [b.type for b in doc.blocks] == ['table']
    assert doc.blocks[0].text == s.table_markdown([heading or ['Column 1', 'Column 2'], *body])


@pytest.mark.parametrize('wordy_data', [False, True])
def test_data_only_reconstruction_cannot_detach_existing_wrapped_column_names(tmp_path, wordy_data):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids
    headers = ['Name', 'Monthly Count', 'Net Value']
    body = [['100', '10', '2'], ['200', '20', '3'], ['300', '30', '4']]
    if wordy_data:
        body = [['NameMonthlyCountNetValue' + row[0], *row[1:]] for row in body]
    rows = [['', 'Monthly', 'Net'], ['Name', 'Count', 'Value'], *body]
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    for x in (40, 200, 360, 520):
        page.draw_line((x, 80), (x, 80 + 20 * len(rows)))
    for i in range(len(rows) + 1):
        page.draw_line((40, 80 + 20*i), (520, 80 + 20*i))
    for i, row in enumerate(rows):
        for x, value in zip((50, 210, 370), row):
            page.insert_text((x, 95 + 20*i), value, fontsize=9)
    path = tmp_path / 'wrapped-headings.pdf'
    pdf.save(path)
    pdf.close()
    source = [headers, *body]
    original = s.table_markdown(source)
    doc = document([('table', original, 0)])
    assert recover_sheet_grids(doc, path, [source], set(), s._printed_cells(path, doc)) == 0
    assert [(b.type, b.text) for b in doc.blocks] == [('table', original)]


def test_continuation_retains_names_for_only_the_printed_source_columns(tmp_path):
    from tests.test_sheet_grids import fixture, run
    from app.parsers import source_tables as s
    doc, _, _ = fixture(tmp_path)
    rows = [['Name', 'Code', 'Total'], ['Alpha', '27', '4.50'], ['Bravo', '35', '6.75'],
            ['Charlie', '18', '9.25'], ['Delta', '44', '8.00']]
    pdf = pymupdf.open()
    for fragment in (rows[:3], [[row[0], row[2]] for row in rows[3:]]):
        page = pdf.new_page(width=600, height=800)
        for i, row in enumerate(fragment):
            for x, value in zip([50, 230, 370], row):
                page.insert_text((x, 100 + 20 * i), value, fontsize=10)
    path = tmp_path / 'narrow-continuation.pdf'
    pdf.save(path)
    pdf.close()
    doc.document.page_count = 2
    doc.pages.append(doc.pages[0].model_copy(update={'index': 1}))
    doc.blocks[0].text = s.table_markdown(rows[:3])
    doc.blocks.append(doc.blocks[0].model_copy(update={
        'id': 'page2', 'type': 'paragraph', 'text': 'Charlie 9.25 Delta 8.00',
        'regions': [doc.blocks[0].regions[0].model_copy(update={'page_index': 1})]}))
    assert run(doc, rows, path, ['b0']) == 1
    page2 = next(b for b in doc.blocks if b.regions[0].page_index == 1 and b.type == 'table')
    assert page2.text == s.table_markdown([['Name', 'Total'], ['Charlie', '9.25'], ['Delta', '8.00']])
    assert 'Code' not in page2.text and '18' not in page2.text and '44' not in page2.text


@pytest.mark.parametrize('picture_kind', ['none', 'chart', 'coarse'])
def test_touching_chart_border_does_not_become_a_table_column(tmp_path, picture_kind):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids
    from app.models import BlockRegion, BBox

    rows = [['', 'Category'], ['2000', '14'], ['2001', '18'], ['2002', '27']]
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    for x in (40, 120, 240):
        page.draw_line((x, 80), (x, 160))
    for i in range(5):
        page.draw_line((40, 80 + 20*i), (240, 80 + 20*i))
    # The touching outline is detected as a third, row-spanning cell.
    page.draw_rect((240, 80, 500, 160))
    for i, row in enumerate(rows):
        for x, value in zip((50, 130), row):
            page.insert_text((x, 95 + 20*i), value, fontsize=9)
    page.insert_text((260, 100), 'Category trend', fontsize=9)
    page.insert_text((260, 125), '100 50 0', fontsize=9)
    path = tmp_path / 'touching-chart.pdf'
    pdf.save(path)
    text = page.get_text()
    pdf.close()
    doc = document([('paragraph', text, 0)])
    if picture_kind != 'none':
        box = BBox(x0=.4, y0=.1, x1=5/6, y1=.2) if picture_kind == 'chart' else BBox(x0=.05, y0=.08, x1=.95, y1=.9)
        picture = doc.blocks[0].model_copy(deep=True, update={
            'id': 'chart', 'type': 'picture', 'text': 'Category trend 100 50 0',
            'regions': [BlockRegion(page_index=0, bbox=box)]})
        doc.blocks.append(picture)
    assert recover_sheet_grids(doc, path, [rows], set(), s._printed_cells(path, doc)) == 1
    table = next(b for b in doc.blocks if b.type == 'table')
    if picture_kind == 'chart':
        assert table.text == s.table_markdown(rows)
        assert 'Category trend' not in table.text
        assert any(b.id == 'chart' and b.text == 'Category trend 100 50 0' for b in doc.blocks)
    else:
        # A tall cell alone is not evidence of a chart: keep a genuine merged
        # text column when no independent picture owns its box, including an
        # imprecise picture region that covers the entire table.
        assert 'Category trend' in table.text
        assert len(table.text.splitlines()[0].strip('|').split('|')) == 3
    assert all(value in '\n'.join(b.text for b in doc.blocks) for value in ('2000', '2001', '2002', '14', '18', '27'))


@pytest.mark.parametrize('damage', ['merged_headings', 'attached_note'])
def test_printed_source_records_can_repair_a_coarse_existing_table(tmp_path, damage):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids
    rows = [['Code', 'Item', 'Status', 'Quantity'], ['1', 'Alpha', 'Ready', '10'],
            ['2', 'Bravo', 'Done', '20']]
    if damage == 'attached_note':
        rows = [row[:3] for row in rows]  # Exactly one numeric identifier per data row.
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    edges = [40 + i * 100 for i in range(len(rows[0]) + 1)]
    for x in edges:
        page.draw_line((x, 80), (x, 140))
    for y in (80, 100, 120, 140):
        page.draw_line((edges[0], y), (edges[-1], y))
    for i, row in enumerate(rows):
        for x, value in zip(edges, row):
            page.insert_text((x + 5, 95 + i * 20), value, fontsize=9)
    if damage == 'merged_headings':
        coarse = [[row[0] + ' ' + row[1], row[2] + ' ' + row[3]] for row in rows]
    else:
        page.insert_text((450, 115), 'Separate note', fontsize=9)
        coarse = [row[:] for row in rows]
        coarse[1][-1] += ' Separate note'
    path = tmp_path / 'coarse-table.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('table', s.table_markdown(coarse), 0)])
    assert recover_sheet_grids(doc, path, [rows], set(), s._printed_cells(path, doc)) == 1
    assert [b.text for b in doc.blocks if b.type == 'table'] == [s.table_markdown(rows)]
    if damage == 'attached_note':
        assert [b.text for b in doc.blocks if b.type == 'paragraph'] == ['Separate note']


@pytest.mark.parametrize('program_x, recover', [(126, True), (120, False)])
def test_overlapping_glyph_extents_keep_printed_cells_intact(tmp_path, program_x, recover):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids
    rows = [['10', 'NameA', 'IDT', '300'], ['20', 'NameB', 'IDT', '400']]
    pdf = pymupdf.open()
    page = pdf.new_page(width=500, height=400)
    for i, row in enumerate(rows):
        for x, value in zip((40, 100, program_x, 220), row):
            page.insert_text((x, 100 + 20 * i), value, fontsize=10, fontname='cour')
    path = tmp_path / 'overlapping-cell-glyphs.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('paragraph', ' '.join(' '.join(row) for row in rows), 0)])
    if not recover:
        original = [(b.type, b.text) for b in doc.blocks]
        assert recover_sheet_grids(doc, path, [rows], set(), s._printed_cells(path, doc)) == 0
        assert [(b.type, b.text) for b in doc.blocks] == original
        return
    assert recover_sheet_grids(doc, path, [rows], set(), s._printed_cells(path, doc)) == 1
    assert [b.type for b in doc.blocks] == ['table']
    assert doc.blocks[0].text == s.table_markdown([['Column 1', 'Column 2', 'Column 3', 'Column 4'], *rows])


@pytest.mark.parametrize('hidden_column', [1, 3])
@pytest.mark.parametrize('record_count', [1, 3])
def test_existing_merged_grid_can_recover_only_printed_source_columns(tmp_path, hidden_column, record_count):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids
    visible = [['1', 'Alice', 'Acme'], ['2', 'Brenda', 'Beacon'], ['3', 'Clara', 'Cedar']][:record_count]
    source = [['RecordSpec', 'Name', 'Company'], *[row[:] for row in visible]]
    for i, row in enumerate(source):
        row.insert(hidden_column, 'Comment' if i == 0 else f'Unprinted confidential note {i}')
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    page.insert_text((50, 85), 'Unclear', fontsize=9)
    page.insert_text((250, 85), 'Company', fontsize=9)
    for i, row in enumerate(visible):
        for x, value in zip((50, 100, 250), row):
            page.insert_text((x, 105 + 16 * i), value, fontsize=9)
    path = tmp_path / 'projected-records.pdf'
    pdf.save(path)
    pdf.close()
    original = s.table_markdown([['Unclear', 'Company'], *[[f'{a} {b}', c] for a, b, c in visible]])
    doc = document([('table', original, 0)])
    made = recover_sheet_grids(doc, path, [source], set(), s._printed_cells(path, doc))
    if record_count == 1:
        assert made == 0
        assert [(b.type, b.text) for b in doc.blocks] == [('table', original)]
    else:
        assert made == 1
        table = next(b for b in doc.blocks if b.type == 'table')
        for row in visible:
            assert '| ' + ' | '.join(row) + ' |' in table.text
        assert 'Unclear Company' in '\n'.join(b.text for b in doc.blocks)
        assert 'Unprinted' not in '\n'.join(b.text for b in doc.blocks)
        assert table.text.startswith('| Column 1 | Column 2 | Column 3 |')


@pytest.mark.parametrize('repeat', [False, True])
@pytest.mark.parametrize('printed_header', [False, True])
def test_printed_subset_header_names_continuation_columns(tmp_path, repeat, printed_header):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids

    names = ['Asset', 'Currency', 'Quantity', 'Price']
    first = ['Alpha', 'USD', '10', '1.25']
    records = [['Bravo', 'EUR', '20', '2.50'], ['Cedar', 'USD', '30', '3.75']]
    full_header = [*names, 'Unprinted note', 'Hidden code']
    source = [full_header, [*first, 'hidden A', 'private A']]
    if repeat:
        source.append(full_header[:])
    source.extend([*row, 'not visible', 'not printed'] for row in records)
    pdf = pymupdf.open()
    for rows in ([names, first], [*([names] if printed_header else []), *records]):
        page = pdf.new_page(width=600, height=800)
        for i, row in enumerate(rows):
            for x, value in zip((50, 180, 290, 400), row):
                page.insert_text((x, 100 + 20*i), value, fontsize=9)
    path = tmp_path / 'partial-header.pdf'
    pdf.save(path)
    pdf.close()
    second = [*([names] if printed_header else []), *records]
    doc = document([('table', s.table_markdown([names, first]), 0),
                    ('paragraph', ' '.join(' '.join(row) for row in second), 1)], pages=2)
    assert recover_sheet_grids(doc, path, [source], {'b0'}, s._printed_cells(path, doc)) == 1
    table = next(b for b in doc.blocks if b.type == 'table' and b.regions[0].page_index == 1)
    assert table.text == s.table_markdown([names, *records])
    assert table.text.count('| Asset |') == 1
    assert 'Unprinted' not in table.text and 'not visible' not in table.text


@pytest.mark.parametrize('matching_source', [True, False])
def test_stacked_printed_headers_follow_aligned_table_fragments(tmp_path, matching_source):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids

    top = ['Group', 'Sales', 'Stock', 'Price']
    lower = ['code', 'total', 'total', 'net']
    names = [a + ' ' + b for a, b in zip(top, lower)]
    numbers = ['(1)', '(2)', '(3)', '(4)']
    records = [['10', '110', '210', '310'], ['20', '120', '220', '320'], ['30', '130', '230', '330']]
    source = [['All results', '', '', ''], top[:], lower[:], numbers, *records]
    if not matching_source:
        source[2][0] = 'category'
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    page.insert_text((50, 85), 'All results', fontsize=9)
    for x in (40, 170, 280, 390, 500):
        page.draw_line((x, 100), (x, 350))
    for y in (100, 150, 170, 200, 350):
        page.draw_line((40, y), (500, y))
    for x, a, b in zip((50, 180, 290, 400), top, lower):
        page.insert_text((x, 120), a, fontsize=9)
        page.insert_text((x, 137), b, fontsize=9)
    for y, row in zip((165, 190, 300, 320), [numbers, *records]):
        for x, value in zip((50, 180, 290, 400), row):
            page.insert_text((x, y), value, fontsize=9)
    path = tmp_path / 'stacked-header.pdf'
    pdf.save(path)
    pdf.close()
    coarse = [['All results', '', '', ''], names, numbers, *records]
    doc = document([('table', s.table_markdown(coarse), 0)])
    assert recover_sheet_grids(doc, path, [source], set(), s._printed_cells(path, doc)) >= 2
    fragments = [b for b in doc.blocks if b.type == 'table' and '| 20 | 120 |' in b.text]
    assert len(fragments) == 1
    expected = names if matching_source else ['Column 1', 'Column 2', 'Column 3', 'Column 4']
    assert fragments[0].text == s.table_markdown([expected, *records[1:]])


@pytest.mark.parametrize('with_neighbor', [False, True])
def test_continuation_context_does_not_dismantle_correct_wrapped_records(tmp_path, with_neighbor):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids

    names = ['Asset', 'Currency', 'Quantity', 'Price']
    records = [['Alpha Long Company', 'USD', '10', '1.25'],
               ['Bravo', 'EUR', '20', '2.50'], ['Cedar', 'USD', '30', '3.75']]
    source = [[*names, 'Hidden note', 'Hidden code'],
              *[[*row, 'Unprinted', 'Not visible'] for row in records]]
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    for y, row in zip((100, 130, 170, 190), [names, *records]):
        for x, value in zip((50, 210, 320, 430), row):
            page.insert_text((x, y), value.replace('Long Company', 'Long\nCompany'), fontsize=9)
    neighbor = [['Code', 'Units', 'Value'], ['Beta', '4', '5.00'], ['Gamma', '6', '7.00']]
    if with_neighbor:
        for i, row in enumerate(neighbor):
            for x, value in zip((50, 210, 320), row):
                page.insert_text((x, 400 + 20*i), value, fontsize=9)
    path = tmp_path / 'already-correct.pdf'
    pdf.save(path)
    pdf.close()
    original = s.table_markdown([names, *records])
    doc = document([('table', original, 0)])
    sources = [source]
    if with_neighbor:
        from app.models import BBox
        doc.blocks[0].regions[0].bbox = BBox(x0=.05, y0=.1, x1=.95, y1=.3)
        other = document([('paragraph', ' '.join(' '.join(row) for row in neighbor), 0)]).blocks[0]
        other.id = 'neighbor'
        other.regions[0].bbox = BBox(x0=.05, y0=.45, x1=.95, y1=.6)
        doc.blocks.append(other)
        sources.append(neighbor)
    recover_sheet_grids(doc, path, sources, set(), s._printed_cells(path, doc))
    assert doc.blocks[0].type == 'table' and doc.blocks[0].text == original
    if with_neighbor:
        assert any(b.type == 'table' and '| Beta | 4 | 5.00 |' in b.text for b in doc.blocks[1:])
    else:
        assert len(doc.blocks) == 1


@pytest.mark.parametrize('changed_column', [0, 3])
def test_observed_header_does_not_cross_a_different_source_section(tmp_path, changed_column):
    from tests.test_feedback_tables import document
    from app.parsers import source_tables as s
    from app.parsers.sheet_grids import recover_sheet_grids

    names = ['Asset', 'Currency', 'Quantity', 'Price']
    first = ['Alpha', 'USD', '10', '1.25']
    later_names = names[:]
    later_names[changed_column] = 'Different field'
    records = [['Bravo', 'EUR', '20', '2.50'], ['Cedar', 'USD', '30', '3.75']]
    source = [names, first, ['', '', '', ''], later_names, *records]
    pdf = pymupdf.open()
    for rows in ([names, first], records):
        page = pdf.new_page(width=600, height=800)
        for i, row in enumerate(rows):
            for x, value in zip((50, 180, 290, 400), row):
                page.insert_text((x, 100 + 20*i), value, fontsize=9)
    path = tmp_path / 'section-change.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('table', s.table_markdown([names, first]), 0),
                    ('paragraph', ' '.join(' '.join(row) for row in records), 1)], pages=2)
    assert recover_sheet_grids(doc, path, [source], {'b0'}, s._printed_cells(path, doc)) == 1
    table = next(b for b in doc.blocks if b.type == 'table' and b.regions[0].page_index == 1)
    assert table.text == s.table_markdown([['Column 1', 'Column 2', 'Column 3', 'Column 4'], *records])


def test_text_only_records_do_not_end_column_header_context():
    from app.parsers.sheet_grids import _header_context_reaches

    rows = [['Type', 'Code', 'Name', 'Address'],
            ['F', '12', 'Alice', 'First Street'],
            ['I', 'AB', 'Bravo Company', 'Second Street'],
            ['I', 'CD', 'Cedar Company', 'Third Street'],
            ['F', '34', 'Dana', 'Fourth Street']]
    assert _header_context_reaches(rows, (0, 1, 2, 3), 0, 4)
