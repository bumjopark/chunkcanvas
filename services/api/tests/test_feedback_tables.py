from collections import Counter
import re
import pytest
from app.models import ParsedDocument
from app.parsers import source_tables as s


def document(items, pages=1, fmt='xlsx'):
    return ParsedDocument.model_validate({
        'document': {'id': 'table-review', 'filename': 'synthetic', 'fileHash': 'h', 'pageCount': pages,
                     'sourceFormat': fmt, 'parser': 'hybrid', 'parserVersion': 'test'},
        'pages': [{'index': i, 'width': 600, 'height': 800} for i in range(pages)],
        'blocks': [{'id': f'b{i}', 'readingOrder': i, 'type': kind, 'text': text,
                    'regions': [{'pageIndex': page, 'bbox': {'x0': .05, 'y0': .08, 'x1': .95, 'y1': .9}}],
                    'source': {'parser': 'test'}} for i, (kind, text, page) in enumerate(items)],
        'blocksFingerprint': 'f'})


def test_single_letter_rows_are_not_consumed_by_fragments_of_header_words():
    rows = [['Category', 'Flagged'], ['A', 'Y'], ['C', 'N'], ['T', 'Y'], ['E', 'N'], ['B', 'Y']]
    doc = document([('paragraph', ' '.join(row), 0) for row in rows])
    s.apply_source_tables(doc, [rows], sheets=True)
    text = '\n'.join(b.text for b in doc.blocks)
    for a, b in rows[1:]:
        assert f'| {a} | {b} |' in text or f'{a} {b}' in text
    assert Counter(s._words(text)) == Counter(s._words(' '.join(' '.join(row) for row in rows)))


def test_horizontal_sheet_fragments_do_not_import_previous_page_columns(tmp_path):
    import pymupdf
    rows = [
        ['Double field1', 'Double field2', 'Double field3', 'Datas', '', 'Sum of Datas', '', '', '', 'Sum of Datas', '', '', 'Sum of Datas', ''],
        ['1', '1.00', '10,000.00', '22', '', 'Double field1', 'Total', '', '', 'Double field2', 'Total', '', 'Double field3', 'Total'],
        ['2', '2.00', '20,000.00', '12', '', '2', '12', '', '', '1.00', '22', '', '10,000.00', '22'],
        ['3', '3.00', '30,000.00', '32', '', '3', '32', '', '', 'Grand Total', '22', '', '30,000.00', '32'],
        ['', '', '', '', '', 'Grand Total', '44', '', '', '', '', '', 'Grand Total', '54'],
    ]
    fragments = [[row[:7] for row in rows], [row[9:] for row in rows]]
    pdf = pymupdf.open()
    for fragment in fragments:
        page = pdf.new_page(width=600, height=800)
        for r, row in enumerate(fragment):
            for c, text in enumerate(row):
                page.insert_text((35 + c * 75, 100 + r * 20), text, fontsize=7)
    path = tmp_path / 'horizontal.pdf'; pdf.save(path); pdf.close()
    doc = document([('table', s.table_markdown(fragment), i) for i, fragment in enumerate(fragments)], pages=2)
    s.apply_source_tables(doc, [rows], sheets=True, pdf=path)
    page2 = ' '.join(b.text for b in doc.blocks if any(r.page_index == 1 for r in b.regions))
    assert 'Double field1' not in page2
    assert '20,000.00' not in page2
    assert Counter(s._tokens(page2)) == Counter(s._tokens(' '.join(' '.join(row) for row in fragments[1])))


@pytest.mark.parametrize('original,replacement', [('A Y', 'Category Flagged'), ('15', '2015'), ('yes yes', 'yes')])
def test_preservation_requires_whole_words_and_occurrence_counts(original, replacement):
    assert not s._preserves_text(original, replacement)


def test_identical_source_rows_are_limited_to_the_printed_occurrences():
    rows = [['path', 'status']] + [['/srv/a.txt', 'ok']] * 40 + [['/srv/b.txt', 'fail']]
    shown = s.table_markdown(rows[:6])
    doc = document([('table', shown, 0)], fmt='csv')
    s.apply_source_tables(doc, [rows], columns_overflow=True, repeat_header=False)
    assert doc.blocks[0].text.count('a.txt') == 5
    assert 'b.txt' not in doc.blocks[0].text


def test_pdf_evidence_recovers_missing_text_inside_an_existing_source_cell(tmp_path):
    import pymupdf
    sentence = 'The field operator uses the mobile app to complete the task.'
    tail = 'More detail identifies the source cell and retains every printed word without repeating unseen text.'
    rows = [['Metric', 'Description'], ['Method', sentence + ' ' + tail]]
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    for y, row in [(100, rows[0]), (125, ['Method', sentence]), (140, ['', tail])]:
        page.insert_text((40, y), row[0], fontsize=8)
        page.insert_text((120, y), row[1], fontsize=8)
    path = tmp_path / 'source.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('table', '| Metric | Description |\n| --- | --- |\n| Method | The operator completes the task. ' + tail + ' |', 0)], fmt='docx')
    s.apply_source_tables(doc, [rows], pdf=path, columns_overflow=False)
    assert 'field operator uses the mobile app' in doc.blocks[0].text


def test_wide_csv_keeps_field_names_with_every_record():
    from app.convert import csv_body
    from lxml import html
    headers=[f'field{i}' for i in range(12)]
    text=','.join(headers)+'\n'+','.join(str(i) for i in range(12))+'\n'+','.join(str(i+20) for i in range(12))
    body,notices=csv_body(text)
    assert 'csv_header_inferred' in notices
    records=html.fromstring('<div>'+body+'</div>').xpath('.//p')
    assert len(records)==2
    for record in records:
        assert record.xpath('.//b/text()')==headers


def test_ambiguous_first_rows_keep_the_production_heading_default():
    # Without evidence in the file, production used row 1 as headings and most
    # CSV exports have one. Losing real column names is worse than labelling a
    # headerless list with its first record, which still stays in the output.
    from app.convert import csv_body
    from lxml import html
    for first in [['A','B'],['France','Paris']]:
        body,notices=csv_body(','.join(first)+'\nGermany,Berlin\nGreece,Athens')
        tree=html.fromstring(body)
        assert 'csv_header_inferred' in notices
        assert tree.xpath('.//th/text()') == first
        assert tree.xpath('.//tbody/tr[1]/td/text()') == ['Germany', 'Berlin']


@pytest.mark.parametrize('text', [
    'Seoul,Korea\nBusan,Korea\nTokyo,Japan',
    'Task A,open,Kim\nTask B,closed,Lee\nTask C,open,Park',
    'kim@example.com,Kim\nlee@example.com,Lee\npark@example.com,Park',
    '2024-01-01,10\n2024-01-02,12\n2024-01-03,15',
])
def test_first_rows_with_record_evidence_stay_as_data(text):
    from app.convert import csv_body
    from lxml import html
    body, notices = csv_body(text)
    tree = html.fromstring(body)
    assert 'csv_header_unverified' in notices
    assert tree.xpath('.//tbody/tr[1]/td/text()') == text.splitlines()[0].split(',')


def test_repeated_record_groups_share_one_printed_occurrence_budget():
    rows=s.SelectedRows([['Expense Row','Taxi']]*4+[['Expense Row','']]*90,range(94),[0,1])
    evidence='Expense Row Taxi '*4+'Expense Row '*8
    limited=s._limit_repeats(rows,evidence)
    assert len(limited)==12
    assert sum(row[1]=='Taxi' for row in limited)==4


def test_repeated_boolean_records_need_order_evidence_not_only_value_counts():
    a, b = ['FALSE', 'TRUE', 'FALSE'], ['TRUE', 'FALSE', 'FALSE']
    rows = s.SelectedRows([a, b, a, b], range(4), range(3), header_id=None)
    evidence = ' | '.join(' | '.join(row) for row in [a, b])
    limited = s._limit_repeats(rows, evidence)
    # Both row types consume identical TRUE/FALSE budgets. Keeping two copies
    # of a silently changes the first value of the second printed record.
    assert not limited or limited == [a, b]


@pytest.mark.parametrize('printed_note', ['THIS SHE', 'THIS SHEET IS USED ONLY FOR'])
def test_clipped_note_and_separate_title_keep_mixed_repeated_sheet_rows_once(tmp_path, printed_note):
    import pymupdf
    note = 'THIS SHEET IS USED ONLY FOR UPLOADING DATA TO A DIFFERENT SYSTEM'
    rows = [['Import Form', 'Version 1'], ['', note], ['Category', 'Value']]
    rows += [['Expense Row', 'Taxi']] * 4 + [['Expense Row', '']] * 30
    visible = [*rows[:1], ['', printed_note], *rows[2:10]]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    page.insert_text((200, 70), 'Import Form', fontsize=10)
    for i, row in enumerate(visible):
        for x, value in zip((50, 220), row):
            page.insert_text((x, 120+i*22), value, fontsize=10)
    path = tmp_path/'clipped-mixed-records.pdf'; pdf.save(path); pdf.close()
    doc = document([('section_header', 'Import Form', 0),
                    ('paragraph', ' '.join(' '.join(row) for row in visible), 0),
                    ('table', s.table_markdown(visible), 0)])
    doc.blocks[0].regions[0].bbox.y0 = .06
    doc.blocks[0].regions[0].bbox.y1 = .10
    for block in doc.blocks[1:]:
        block.regions[0].bbox.y0 = .13
        block.regions[0].bbox.y1 = .45
    s.apply_source_tables(doc, [rows], sheets=True, pdf=path)
    body = '\n'.join(b.text for b in doc.blocks)
    tables = [b for b in doc.blocks if b.type == 'table']
    assert body.count('Expense Row') == 7
    assert len(tables) == 1 and tables[0].text == s.table_markdown(visible)
    assert body.count('Import Form') == 2
    assert body.count(printed_note) == 1 and 'DIFFERENT SYSTEM' not in body


@pytest.mark.parametrize('column_major', [False, True])
def test_sheet_boolean_cells_keep_their_physical_row_order(tmp_path, column_major):
    import pymupdf
    a, b = ['FALSE', 'TRUE', 'FALSE'], ['TRUE', 'FALSE', 'FALSE']
    header = ['Billable', 'Research', 'Additional']
    rows = [header, *([a, b] * 8)]
    pages = [[header, *rows[1:9]], [header, *rows[9:17]]]
    pdf = pymupdf.open()
    for visible in pages:
        page = pdf.new_page(width=600, height=800)
        positions = [(r, c) for r in range(len(visible)) for c in range(3)]
        if column_major:
            positions.sort(key=lambda rc: (rc[1], rc[0]))
        for r, c in positions:
            page.insert_text((50 + c*160, 100 + r*24), visible[r][c], fontsize=10)
    path = tmp_path/'booleans.pdf'; pdf.save(path); pdf.close()
    doc = document([('table', s.table_markdown(visible), p) for p, visible in enumerate(pages)], pages=2)
    s.apply_source_tables(doc, [rows], sheets=True, pdf=path)
    for p, visible in enumerate(pages):
        actual = [v for block in doc.blocks if block.regions[0].page_index == p
                  for v in re.findall(r'\b(?:TRUE|FALSE)\b', block.text)]
        assert actual == [v for row in visible[1:] for v in row]


@pytest.mark.parametrize('heading_type', ['paragraph', 'caption'])
def test_sheet_title_and_cell_with_the_same_words_keep_both_occurrences(tmp_path, heading_type):
    import pymupdf
    rows = [['Department', 'Amount'], ['Engineering', '10'], ['Sales', '20']]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    page.insert_text((200, 80), 'Department', fontsize=10)
    for i, row in enumerate(rows):
        for x, value in zip((50, 250), row):
            page.insert_text((x, 130+i*20), value, fontsize=10)
    path = tmp_path/'title-and-cell.pdf'; pdf.save(path); pdf.close()
    doc = document([(heading_type, 'Department', 0), ('table', s.table_markdown(rows), 0)])
    doc.blocks[0].regions[0].bbox.y0 = .08
    doc.blocks[0].regions[0].bbox.y1 = .11
    doc.blocks[1].regions[0].bbox.y0 = .14
    doc.blocks[1].regions[0].bbox.y1 = .23
    s.apply_source_tables(doc, [rows], sheets=True, pdf=path)
    actual = '\n'.join(b.text for b in doc.blocks)
    assert actual.count('Department') == 2
    assert 'Engineering' in actual and '10' in actual
    assert 'Sales' in actual and '20' in actual


@pytest.mark.parametrize('heading_position', ['above', 'below', 'overlapping'])
def test_separate_sheet_label_does_not_prevent_recovering_the_complete_body(tmp_path, heading_position):
    import pymupdf
    rows = [['Department', 'Amount'], ['Engineering', '10'], ['Sales', '20'], ['Support', '30']]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    y = 260 if heading_position == 'below' else 80
    page.insert_text((200, y), 'Department', fontsize=10)
    for i, row in enumerate(rows):
        for x, value in zip((50, 250), row):
            page.insert_text((x, 130+i*20), value, fontsize=10)
    path = tmp_path/'separate-label.pdf'; pdf.save(path); pdf.close()
    doc = document([('paragraph', 'Department', 0),
                    ('paragraph', ' '.join(' '.join(row) for row in rows), 0)])
    doc.blocks[0].regions[0].bbox.y0 = (y-12)/800
    doc.blocks[0].regions[0].bbox.y1 = (y+2)/800
    doc.blocks[1].regions[0].bbox.y0 = .14
    doc.blocks[1].regions[0].bbox.y1 = .25
    if heading_position == 'overlapping':
        # A coarse region overlaps, but the printed label is outside the cells.
        # Physical row recovery can resolve this without duplicating the label.
        doc.blocks[0].regions[0].bbox.y1 = .15
    if heading_position == 'below':
        doc.blocks.reverse()
    s.apply_source_tables(doc, [rows], sheets=True, pdf=path)
    tables = [b for b in doc.blocks if b.type == 'table']
    assert '\n'.join(b.text for b in doc.blocks).count('Department') == 2
    assert len(tables) == 1
    assert tables[0].text == s.table_markdown(rows)
    labels = [b for b in doc.blocks if b.text == 'Department']
    assert len(labels) == 1
    label_before_table = doc.blocks.index(labels[0]) < doc.blocks.index(tables[0])
    assert label_before_table == (heading_position != 'below')


@pytest.mark.parametrize('heading_position', ['above', 'below', 'overlapping'])
def test_long_separate_sheet_label_does_not_count_against_complete_table_coverage(tmp_path, heading_position):
    import pymupdf
    label = 'The quick brown fox jumps over the lazy dog'
    rows = [[label, 'Amount'], ['Engineering', '10'], ['Sales', '20'], ['Support', '30']]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    y = 260 if heading_position == 'below' else 80
    page.insert_text((50, y), label, fontsize=10)
    for i, row in enumerate(rows):
        for x, value in zip((50, 400), row):
            page.insert_text((x, 130+i*20), value, fontsize=10)
    path = tmp_path/'long-separate-label.pdf'; pdf.save(path); pdf.close()
    doc = document([('paragraph', label, 0),
                    ('paragraph', ' '.join(' '.join(row) for row in rows), 0)])
    doc.blocks[0].regions[0].bbox.y0 = (y-12)/800
    doc.blocks[0].regions[0].bbox.y1 = (y+2)/800
    doc.blocks[1].regions[0].bbox.y0 = .14
    doc.blocks[1].regions[0].bbox.y1 = .25
    if heading_position == 'overlapping':
        doc.blocks[0].regions[0].bbox.y1 = .15
    if heading_position == 'below':
        doc.blocks.reverse()
    s.apply_source_tables(doc, [rows], sheets=True, pdf=path)
    tables = [b for b in doc.blocks if b.type == 'table']
    assert '\n'.join(b.text for b in doc.blocks).count(label) == 2
    assert len(tables) == 1 and tables[0].text == s.table_markdown(rows)
    outside = next(b for b in doc.blocks if b.text == label)
    assert (doc.blocks.index(outside) < doc.blocks.index(tables[0])) == (heading_position != 'below')


def test_overlapping_layout_blocks_cannot_duplicate_physical_sheet_rows(tmp_path):
    import pymupdf
    rows=[['Category','Flagged']]+[['A','Y']]*5+[['B','N']]*15
    pdf=pymupdf.open();page=pdf.new_page(width=600,height=800)
    for i,row in enumerate(rows[:6]):
        page.insert_text((50,100+i*20),row[0],fontsize=10)
        page.insert_text((150,100+i*20),row[1],fontsize=10)
    path=tmp_path/'rows.pdf';pdf.save(path);pdf.close()
    doc=document([('paragraph',' '.join(' '.join(row) for row in rows[:6]),0),('table',s.table_markdown(rows[:6]),0)])
    s.apply_source_tables(doc,[rows],sheets=True,pdf=path)
    tokens=Counter(s._words(' '.join(b.text for b in doc.blocks)))
    assert tokens['a']==5 and tokens['y']==5


def test_equal_rows_at_distinct_positions_are_not_absorbed_as_fragments():
    text='| Item | Count |\n| --- | --- |\n| Widget | 2 |'
    doc=document([('table',text,0),('table',text,1)],pages=2)
    s._absorb_fragments(doc,{'b1':0})
    assert len(doc.blocks)==2


@pytest.mark.parametrize('printed_note', ['THIS SHEET IS USED ONLY FOR', 'THIS SHE'])
def test_clipped_sheet_note_does_not_leave_two_copies_of_printed_records(tmp_path, printed_note):
    import pymupdf
    note = 'THIS SHEET IS USED ONLY FOR UPLOADING DATA TO A DIFFERENT SYSTEM'
    rows = [['Record import', 'Version 1'], ['', note], ['Category', 'Flagged']]
    rows += [['Entry Row', 'Yes']] * 5 + [['Entry Row', 'No']] * 15
    visible = [*rows[:1], ['', printed_note], *rows[2:8]]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    for i, row in enumerate(visible):
        for x, value in zip([50, 180], row):
            page.insert_text((x, 100 + i * 20), value, fontsize=10)
    path = tmp_path / 'clipped-note.pdf'; pdf.save(path); pdf.close()
    doc = document([('paragraph', ' '.join(' '.join(row) for row in visible), 0),
                    ('table', s.table_markdown(visible), 0)])
    s.apply_source_tables(doc, [rows], sheets=True, pdf=path)
    text = '\n'.join(b.text for b in doc.blocks)
    assert text.count('Entry Row') == 5
    assert text.count(printed_note) == 1
    assert 'DIFFERENT SYSTEM' not in text
    assert '| Entry Row | Yes |' in text


def test_partial_cell_keeps_only_its_printed_fragment():
    value=' '.join(f'word{i:03d}' for i in range(100))
    visible=' '.join(value.split()[30:60])
    rows=s.SelectedRows([['Description'],[value]],[0,1],[0])
    clipped=s._clip_long_cells(rows,'Description '+visible)
    assert clipped[1]==[visible]
    assert clipped.row_ids==[0,1]


def test_table_at_page_foot_keeps_target_columns_and_short_start_of_long_cell(tmp_path):
    import pymupdf
    visible = 'Field staff using the mobile application'
    unseen = ' '.join(f'laterword{i:03d}' for i in range(80))
    rows = [['Metric', 'Service usage', ''], ['Target', '2026', '2027'],
            ['Target', '50%', '60%'], ['Method', visible + ' ' + unseen, '']]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    # The last source cell continues on the next page. Only its short start is here.
    for y, cells in [(580, rows[0]), (610, rows[1]), (635, ['', '50%', '60%']),
                     (680, ['Method', visible, ''])]:
        for x, cell in zip([40, 140, 440], cells):
            if cell: page.insert_text((x, y), cell, fontsize=9)
    path = tmp_path / 'source.pdf'; pdf.save(path); pdf.close()
    doc = document([('table', '| Metric | Service usage |\n| --- | --- |\n'
                     '| Target | 2026 2027 |\n| Method | 50% 60% Field staff |', 0)], fmt='docx')
    assert s.apply_source_tables(doc, [rows], pdf=path, columns_overflow=False) == 1
    text = doc.blocks[0].text
    assert '| Target | 2026 | 2027 |' in text
    assert '| Target | 50% | 60% |' in text
    assert f'| Method | {visible} |  |' in text
    assert 'laterword' not in text


def test_partial_short_cell_does_not_repeat_previous_page_prefix():
    prefix = 'This text was printed on the preceding page. '
    visible = 'Only this distinct continuation is printed here.'
    rows = s.SelectedRows([['Description', prefix + visible]], [3], [0, 1])
    clipped = s._clip_long_cells(rows, 'Description ' + visible)
    assert clipped == [['Description', visible]]


def test_short_cjk_start_of_next_row_does_not_block_merged_label_restoration():
    visible = 'ㅇ 프로젝트참여현황'
    rows = [['구분', '사항', '내용'], ['기술 평가 (90)', '첫째', '기본 정보 확인'],
            ['기술 평가 (90)', '둘째', visible + ' ' + ' '.join(f'추가문구{i:03d}' for i in range(100))]]
    shown = '| 구분 | 사항 | 내용 |\n| --- | --- | --- |\n| 기술 평가 (90) | 첫째 | 기본 정보 확인 |\n| 둘째 | ' + visible + ' |'
    doc = document([('table', shown, 0)], fmt='hwpx')
    assert s.apply_source_tables(doc, [rows], columns_overflow=False) == 1
    assert f'| 기술 평가 (90) | 둘째 | {visible} |' in doc.blocks[0].text
    assert '추가문구' not in doc.blocks[0].text


def test_short_label_at_page_foot_preserves_only_its_printed_prefix(tmp_path):
    import pymupdf
    rows = [['구분', '평가사항', '세부내용'],
            ['기술 평가 (90)', '(1) 기획 분야 (15점)', '행사장 구성 및 진행 방안'],
            ['기술 평가 (90)', '(2) 프로그램 구성운영 분야 (15점)',
             'ㅇ 프로그램의 구성 및 진행 ㅇ 신규 프로그램의 적용 가능성 및 참여 이벤트의 우수성']]
    printed = [rows[0], rows[1], ['기술 평가 (90)', '(2) 프로그램', 'ㅇ 프로그램의 구성 및 진행']]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    page.insert_font(fontname='review', fontbuffer=pymupdf.Font('cjk').buffer)
    for y, row in zip((600, 640, 680), printed):
        for x, cell in zip((40, 180, 370), row):
            page.insert_text((x,y), cell, fontsize=7, fontname='review')
    path = tmp_path/'split-label.pdf'; pdf.save(path); pdf.close()
    # A merged-column layout parse must not prevent restoration of the other rows.
    shown = [[row[0]+' '+row[1], row[2]] for row in printed]
    doc = document([('table', s.table_markdown(shown), 0)], fmt='hwpx')
    assert s.apply_source_tables(doc, [rows], pdf=path, columns_overflow=False) == 1
    assert doc.blocks[0].text == s.table_markdown(printed)
    assert '신규 프로그램' not in doc.blocks[0].text


def test_short_label_prefix_is_limited_to_the_final_row_at_the_page_foot():
    rows = s.SelectedRows([['Program delivery'], ['Other field']], [1,2], [0], None)
    assert s._clip_long_cells(rows, 'Program Other field', at_foot=True) == rows
    final = s.SelectedRows([['Program delivery']], [1], [0], None)
    assert s._clip_long_cells(final, 'Program') == final
    assert s._clip_long_cells(final, 'Program', sheet=True, at_foot=True) == final
    assert s._clip_long_cells(final, 'Program', at_foot=True) == [['Program']]
    # A shared substring or a bare number cannot authorize a partial short label.
    assert s._clip_long_cells(final, 'Progra', at_foot=True) == final
    numeric = s.SelectedRows([['1000']], [1], [0], None)
    assert s._clip_long_cells(numeric, '100', at_foot=True) == numeric


def test_page_foot_fragment_cannot_reuse_paths_already_printed_in_other_rows(tmp_path):
    import pymupdf
    rows = [['Document', 'Count', 'Total', 'Missing'],
            *[[f'samples/basic/{name}.hwp', '2', '2', '0']
              for name in ('bookreview', 'english', 'interview')]]
    printed = rows[:3]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    for y, row in zip((600, 640, 680), printed):
        for x, cell in zip((40, 300, 400, 500), row):
            page.insert_text((x, y), cell, fontsize=8)
    path = tmp_path/'similar-paths.pdf'; pdf.save(path); pdf.close()
    doc = document([('table', s.table_markdown(printed), 0)], fmt='docx')
    s.apply_source_tables(doc, [rows], pdf=path, columns_overflow=False)
    assert doc.blocks[0].text == s.table_markdown(printed)


def test_table_at_page_head_preserves_short_tail_and_restores_reversed_columns():
    visible = '민간공동체협력구성'
    rows = [['항목', '설명'], ['첫째 항목', ' '.join(f'이전문구{i:03d}' for i in range(80)) + ' ' + visible],
            ['둘째 항목', '새로운 정책과제의 실행 방법'], ['셋째 항목', '후속 조치와 기관별 역할']]
    shown = f'| {visible} | |\n| --- | --- |\n| 새로운 정책과제의 실행 방법 | 둘째 항목 |\n| 후속 조치와 기관별 역할 | 셋째 항목 |'
    doc = document([('table', shown, 0)], fmt='hwpx')
    assert s.apply_source_tables(doc, [rows], columns_overflow=False) == 1
    assert f'| 첫째 항목 | {visible} |' in doc.blocks[0].text
    assert '| 둘째 항목 | 새로운 정책과제의 실행 방법 |' in doc.blocks[0].text
    assert '이전문구' not in doc.blocks[0].text


def test_discretionary_hyphens_do_not_split_source_cell_words():
    assert s._preserves_text('first-genera\u00adtion perfor\u00admance', 'first-generation performance')
    assert not s._preserves_text('performance performance', 'perfor\u00admance')


def test_table_evidence_counts_overprinted_glyphs_once_but_distinct_rows_twice(tmp_path):
    import pymupdf
    rows = [['Name', 'Role'], ['Morgan Lee', 'Member'], ['Morgan Lee', 'Member']]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    for i, row in enumerate(rows):
        for x, cell in zip([50, 250], row):
            for shadow in (0, 1):
                page.insert_text((x + shadow, 100 + i * 30 + shadow), cell, fontsize=10)
    path = tmp_path / 'overprint.pdf'; pdf.save(path); pdf.close()
    doc = document([('table', '| Name | Role |\n| --- | --- |\n| Morgan Lee Morgan Lee | Member Member |', 0)], fmt='docx')
    assert s.apply_source_tables(doc, [rows], pdf=path, columns_overflow=False) == 1
    assert doc.blocks[0].text == s.table_markdown(rows)


def test_explicit_slide_number_does_not_block_table_restoration_or_consume_a_cell(tmp_path):
    import pymupdf
    rows = [['Item', 'Count'], ['Widget', '15']]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    for i, row in enumerate(rows):
        for x, cell in zip([50, 250], row):
            page.insert_text((x, 100 + i * 30), cell, fontsize=10)
    page.insert_text((500, 710), '15', fontsize=10)
    path = tmp_path / 'number.pdf'; pdf.save(path); pdf.close()
    doc = document([('table', '| Count | Item |\n| --- | --- |\n| 15 | Widget |\n| 15 | |', 0)], fmt='pptx')
    templates = [{'kind': 'footer', 'text': '15', 'dynamic': False, 'pageIndex': 0,
                  'bbox': {'x0': .82, 'y0': .86, 'x1': .88, 'y1': .9}}]
    assert s.apply_source_tables(doc, [rows], pdf=path, columns_overflow=False,
                                 source_furniture=templates) == 1
    assert doc.blocks[0].text == s.table_markdown(rows)
    # An ordinary number at the same location has no source-level footer evidence.
    other = document([('table', '| Count | Item |\n| --- | --- |\n| 15 | Widget |\n| 15 | |', 0)], fmt='pptx')
    assert s.apply_source_tables(other, [rows], pdf=path, columns_overflow=False) == 0


def test_physical_table_evidence_keeps_rotated_cell_labels(tmp_path):
    import pymupdf
    rows = [['Category', 'Count'], ['Vertical', '25']]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    page.insert_text((50, 100), 'Category', fontsize=10)
    page.insert_text((250, 100), 'Count', fontsize=10)
    page.insert_text((50, 200), 'Vertical', fontsize=10, rotate=90)
    page.insert_text((250, 180), '25', fontsize=10)
    path = tmp_path / 'rotated-cell.pdf'; pdf.save(path); pdf.close()
    doc = document([('table', '| Category | Count |\n| --- | --- |\n| 25 | Vertical |', 0)], fmt='docx')
    assert s.apply_source_tables(doc, [rows], pdf=path, columns_overflow=False) == 1
    assert doc.blocks[0].text == s.table_markdown(rows)


def test_wrapped_number_and_word_are_matched_within_one_printed_cell(tmp_path):
    import pymupdf
    rows = [['ID', 'Location'], ['01', 'Alexandria'], ['02', 'Riverside']]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    for y in [80, 110, 160, 210]:
        page.draw_line((40, y), (400, y))
    for x in [40, 110, 400]:
        page.draw_line((x, 80), (x, 210))
    for x, y, text in [(50, 100, 'ID'), (120, 100, 'Location'),
                       (50, 130, '0'), (50, 145, '1'), (120, 130, 'Alexan'), (120, 145, 'dria'),
                       (50, 180, '0'), (50, 195, '2'), (120, 180, 'Riverside')]:
        page.insert_text((x, y), text, fontsize=10)
    path = tmp_path / 'wrapped-cells.pdf'; pdf.save(path); pdf.close()
    doc = document([('table', '| ID | Location |\n| --- | --- |\n| 0 | Alexan |\n| 1 0 | dria Riverside |\n| 2 | |', 0)], fmt='docx')
    assert s.apply_source_tables(doc, [rows], pdf=path, columns_overflow=False) == 1
    assert doc.blocks[0].text == s.table_markdown(rows)


def test_word_footer_template_does_not_remove_wrapped_numbers_inside_a_table(tmp_path):
    import pymupdf
    rows = [['ID', 'Location'], ['01', 'Alexandria'], ['02', 'Riverside']]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    for y in [640, 670, 720, 770]:
        page.draw_line((40, y), (400, y))
    for x in [40, 110, 400]:
        page.draw_line((x, 640), (x, 770))
    for x, y, text in [(50, 660, 'ID'), (120, 660, 'Location'),
                       (50, 690, '0'), (50, 705, '1'), (120, 690, 'Alexandria'),
                       (50, 740, '0'), (50, 755, '2'), (120, 740, 'Riverside')]:
        page.insert_text((x, y), text, fontsize=10)
    path = tmp_path / 'table-at-page-foot.pdf'; pdf.save(path); pdf.close()
    doc = document([('table', '| ID | Location |\n| --- | --- |\n| 0 | Alexandria |\n| 1 0 | Riverside |\n| 2 | |', 0)], fmt='docx')
    doc.blocks[0].regions[0].bbox.y1 = .98
    # A Word footer part has no rendered page/bbox. Its page field cannot
    # identify every matching digit near the bottom as a footer.
    templates = [{'kind': 'footer', 'text': '1 1', 'dynamic': True}]
    assert s.apply_source_tables(doc, [rows], pdf=path, columns_overflow=False,
                                 source_furniture=templates) == 1
    assert doc.blocks[0].text == s.table_markdown(rows)


@pytest.mark.parametrize('source_row,printed_row', [(['AY', 'N'], ['A', 'Y N']), (['101', 'N'], ['1.01', 'N'])])
def test_native_grid_does_not_join_values_from_different_cells_or_change_decimals(tmp_path, source_row, printed_row):
    import pymupdf
    rows = [['Category', 'Flagged'], source_row]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    for y in [80, 110, 150]:
        page.draw_line((40, y), (400, y))
    for x in [40, 210, 400]:
        page.draw_line((x, 80), (x, 150))
    for x, y, text in [(50, 100, 'Category'), (220, 100, 'Flagged'), (50, 130, printed_row[0]), (220, 130, printed_row[1])]:
        page.insert_text((x, y), text, fontsize=10)
    path = tmp_path / 'independent-cells.pdf'; pdf.save(path); pdf.close()
    doc = document([('table', s.table_markdown([rows[0], printed_row]), 0)], fmt='docx')
    original = doc.blocks[0].text
    assert s.apply_source_tables(doc, [rows], pdf=path, columns_overflow=False) == 0
    assert doc.blocks[0].text == original


def test_grid_restoration_does_not_copy_an_unprinted_row_sharing_a_time(tmp_path):
    import pymupdf
    rows = [['ID', 'City', 'Date', 'Time'], ['03', 'Unprinted', '2025-01-01', '13H00'],
            ['04', 'Clearwater', '2025-01-02', '10H00'], ['05', 'Riverbank', '2025-01-02', '13H00']]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    for y in [80, 110, 160, 210]:
        page.draw_line((40, y), (550, y))
    for x in [40, 100, 250, 400, 550]:
        page.draw_line((x, 80), (x, 210))
    for y, row in [(100, rows[0]), (130, rows[2]), (180, rows[3])]:
        for x, text in zip([50, 110, 260, 410], row):
            if text in ('04', '05'):
                page.insert_text((x, y), text[0], fontsize=10)
                page.insert_text((x, y + 15), text[1], fontsize=10)
            else:
                page.insert_text((x, y), text, fontsize=10)
    path = tmp_path / 'adjacent-source-row.pdf'; pdf.save(path); pdf.close()
    doc = document([('table', '| ID | City | Date | Time |\n| --- | --- | --- | --- |\n'
                    '| 0 | Clearwater | 2025-01-02 | 10H00 |\n| 4 0 | Riverbank | 2025-01-02 | 13H00 |\n| 5 | | | |', 0)], fmt='docx')
    assert s.apply_source_tables(doc, [rows], pdf=path, columns_overflow=False) == 1
    assert doc.blocks[0].text == s.table_markdown([rows[0], rows[2], rows[3]])


def test_optional_grid_failure_retains_the_existing_table_and_reports_uncertainty(tmp_path, monkeypatch):
    import pymupdf
    rows = [['Category', 'Flagged'], ['AY', 'N']]
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    page.insert_text((50, 100), 'Category Flagged A Y N', fontsize=10)
    path = tmp_path / 'grid-error.pdf'; pdf.save(path); pdf.close()
    doc = document([('table', '| Category | Flagged |\n| --- | --- |\n| A | Y N |', 0)], fmt='docx')
    original = doc.blocks[0].text
    def unavailable(*args, **kwargs):
        raise RuntimeError('synthetic grid failure')
    monkeypatch.setattr(pymupdf.Page, 'find_tables', unavailable)
    assert s.apply_source_tables(doc, [rows], pdf=path, columns_overflow=False) == 0
    assert doc.blocks[0].text == original
    assert s.last_stats['uncertain_blocks'] == 1


def _printed_record_pdf(path, fragments):
    import pymupdf
    pdf = pymupdf.open()
    for rows in fragments:
        page = pdf.new_page(width=600, height=800)
        width = 510 / len(rows[0])
        for i in range(len(rows) + 1):
            page.draw_line((40, 80 + i * 30), (550, 80 + i * 30))
        for i in range(len(rows[0]) + 1):
            page.draw_line((40 + i * width, 80), (40 + i * width, 80 + len(rows) * 30))
        for i, row in enumerate(rows):
            for j, cell in enumerate(row):
                if cell:
                    page.insert_text((44 + j * width, 100 + i * 30), cell, fontsize=7)
    pdf.save(path)
    pdf.close()


def test_document_grid_retains_blank_continuation_cell_and_actual_columns(tmp_path):
    source = [['Category', 'Details'],
              ['Previous label', 'Previous-page text. Continuation tail'],
              ['Second', 'Complete second detail'], ['Third', 'Complete third detail']]
    printed = [['', 'Continuation tail'], *source[2:]]
    path = tmp_path/'continued-grid.pdf'
    _printed_record_pdf(path, [printed])
    reversed_columns = [[row[1], row[0]] for row in printed]
    doc = document([('table', s.table_markdown(reversed_columns), 0)], fmt='docx')
    before = doc.blocks_fingerprint
    assert s.apply_source_tables(doc, [source], pdf=path, columns_overflow=False) == 1
    # The printed rows prove the source table, so its heading is carried (claude/fix-20261106).
    assert doc.blocks[0].text == s.table_markdown([['Category','Details'], *printed])
    assert doc.blocks[0].source.ref.startswith('printed-document-grid:')
    assert 'Previous label' not in doc.blocks[0].text
    assert 'Previous-page text' not in doc.blocks[0].text
    assert doc.blocks_fingerprint != before
    assert s.last_stats['uncertain_blocks'] == 1


def test_printed_grid_does_not_split_a_complete_source_cell_overflowing_a_row_border(tmp_path):
    source = [['Field', 'Link'], ['First', 'example_hyperlink'], ['', '']]
    path = tmp_path/'overflowing-cell.pdf'
    _printed_record_pdf(path, [[source[0], ['First', 'example_hyperli'], ['', 'nk']]])
    original = s.table_markdown([source[0], ['First', 'example_hyperli nk']])
    doc = document([('table', original, 0)], fmt='pptx')
    assert s.apply_source_tables(doc, [source], pdf=path, columns_overflow=False) == 0
    assert doc.blocks[0].text == original
    assert s.last_stats['uncertain_blocks'] == 1


@pytest.mark.parametrize('positioned', [True, False])
def test_document_grid_keeps_scanned_formula_in_its_physical_column(tmp_path, positioned):
    source = [['Command', 'Explanation', 'Result'],
              ['Previous', 'Unprinted beginning. Continuation tail', ''],
              ['TIMES', 'Multiply two values', ''], ['SUM', 'Add the values', '']]
    printed = [['', 'Continuation tail', ''], *source[2:]]
    path = tmp_path / 'formula-cells.pdf'
    _printed_record_pdf(path, [printed])
    # The scanned result has no native glyphs; the layout model puts it under
    # the command instead of the rightmost printed cell.
    original = s.table_markdown([['', 'Continuation tail'],
        ['TIMES 2 x 5 = 10', 'Multiply two values'], ['SUM', 'Add the values']])
    doc = document([('table', original, 0)], fmt='hwp')
    if positioned:
        doc.blocks[0]._table_ocr_cells = [(0, '2 x 5 = 10', (.65, .15, .8, .17))]
    count = s.apply_source_tables(doc, [source], pdf=path, columns_overflow=False)
    if positioned:
        expected = [['Command', 'Explanation', 'Result'], printed[0],
                    ['TIMES', 'Multiply two values', '2 x 5 = 10'], printed[2]]
        assert count == 1
        assert doc.blocks[0].text == s.table_markdown(expected)
        assert doc.blocks[0].text.count('2 x 5 = 10') == 1
        assert '_table_ocr_cells' not in doc.model_dump_json()
    else:
        assert count == 0
        assert doc.blocks[0].text == original


def test_scanned_cell_fill_rejects_border_crossing_and_native_echoes():
    block = document([('table', '', 0)], fmt='hwp').blocks[0]
    block._table_ocr_cells = [(0, 'echo', (.1, .1, .2, .2)),
                             (0, 'crossing', (.4, .1, .6, .2)),
                             (0, 'second line', (.6, .3, .8, .4)),
                             (0, 'first line', (.6, .1, .8, .2)),
                             (0, 'first line', (.6, .1, .8, .2))]
    cells = {(0, 0): 'native text'}
    cache = {0: (None, [(0, 0, .5, .5), (.5, 0, 1, .5)], [], {})}
    assert sorted(s._fill_scanned_cells(block, cells, cache)) == ['first line', 'second line']
    assert cells == {(0, 0): 'native text', (0, 1): 'first line second line'}


def test_complete_source_match_cannot_replace_scanned_formula_with_blank(tmp_path):
    rows = [['Command', 'Explanation', 'Result'], ['TIMES', 'Multiply two values', ''],
            ['SUM', 'Add the values', '']]
    path = tmp_path / 'complete-source-with-scan.pdf'
    _printed_record_pdf(path, [rows])
    original = s.table_markdown([rows[0], ['TIMES', 'Multiply two values', '2 x 5 = 10'], rows[2]])
    doc = document([('table', original, 0)], fmt='hwp')
    doc.blocks[0]._table_ocr_cells = [(0, '2 x 5 = 10', (.65, .15, .8, .17))]
    s.apply_source_tables(doc, [rows], pdf=path, columns_overflow=False)
    assert doc.blocks[0].text == original


@pytest.mark.parametrize('case', ['aligned', 'missing', 'wrong_order', 'wrong_column',
                                  'different_band', 'distant_page', 'interrupted', 'punctuation'])
def test_scanned_grid_keeps_a_complete_printed_continuation_header(tmp_path, case):
    from app.models import BBox
    source = [['Measure', 'Vehicle A', 'Vehicle B'], ['Shape', '', ''],
              ['Height', '47.2 m', '25.8 m'], ['Diameter', '3.5 m', '2.6 m']]
    page = 2 if case == 'distant_page' else 1
    fragments = [source[:1]] + ([source[:1]] if page == 2 else []) + [source[1:]]
    path = tmp_path / 'scanned-continuation.pdf'; _printed_record_pdf(path, fragments)
    labels = list(source[0])
    if case == 'wrong_order':labels[0], labels[1] = labels[1], labels[0]
    if case == 'punctuation':labels[1] = 'Vehicle - A'
    if case == 'missing':labels.pop()
    original = s.table_markdown([['Shape', 'Badge A', 'Badge B'], *source[2:]])
    items = [('paragraph', label, 0) for label in labels]
    if case == 'interrupted':items.append(('paragraph', 'Unrelated explanation', 0))
    doc = document([*items, ('table', original, page)], pages=page + 1, fmt='hwp')
    for i, part in enumerate(doc.blocks[:-1]):
        x = .08 + i * (170 / 600)
        y = .3 if case == 'different_band' and i == 1 else .11
        if case == 'wrong_column' and i == 1:x = .75
        part.regions[0].bbox = BBox(x0=x, y0=y, x1=min(x + .1, .98), y1=y + .02)
    doc.blocks[-1]._table_ocr_cells = [(page, 'Badge A', (.4, .11, .5, .125)),
                                      (page, 'Badge B', (.7, .11, .8, .125))]
    before = [b.text for b in doc.blocks[:-1]]
    assert s.apply_source_tables(doc, [source], pdf=path, columns_overflow=False) == 1
    table = next(b for b in doc.blocks if b.type == 'table')
    # Printed labels that do not line up are not attached, but the continued rows still
    # prove their source table, whose own heading row is carried (claude/fix-20261106).
    assert table.text == s.table_markdown([source[0], ['Shape', 'Badge A', 'Badge B'], *source[2:]])
    assert table.text.count('Badge A') == table.text.count('Badge B') == 1
    assert s.last_stats['uncertain_blocks'] == 1
    if case == 'aligned':
        assert len(doc.blocks) == 1
        assert {r.page_index for r in table.regions} == {0, 1}
    else:
        assert [b.text for b in doc.blocks[:-1]] == before
        assert {r.page_index for r in table.regions} == {page}


def test_physical_table_restoration_keeps_formula_without_copying_logo_block(tmp_path):
    from app.parsers.docling_parser import _keep_table_owned_ocr
    from test_feedback_labels import block
    source = [['Command', 'Explanation', 'Result'],
              ['Previous', 'Unprinted beginning. Continuation tail', ''],
              ['TIMES', 'Multiply two values', ''], ['SUM', 'Add the values', '']]
    printed = [['', 'Continuation tail', ''], *source[2:]]
    path = tmp_path / 'formula-and-logo.pdf'; _printed_record_pdf(path, [printed])
    original = s.table_markdown([['', 'Continuation tail'],
        ['TIMES 2 x 5 = 10', 'Multiply two values'], ['SUM', 'Add the values']])
    logo = block('picture', 'LOGO', (.05, .09, .2, .12))
    table = block('table', original, (.03, .07, .9, .3), _table_ocr_cells=[
        (0, 'LOGO', (.06, .095, .18, .115)),
        (0, '2 x 5 = 10', (.65, .15, .8, .17))])
    _keep_table_owned_ocr([table, logo])
    doc = document([('picture', 'LOGO', 0), ('table', original, 0)], fmt='hwp')
    doc.blocks[1]._table_ocr_cells = table['_table_ocr_cells']
    assert s.apply_source_tables(doc, [source], pdf=path, columns_overflow=False) == 1
    assert doc.blocks[0].text == 'LOGO'
    assert 'LOGO' not in doc.blocks[1].text
    assert '| TIMES | Multiply two values | 2 x 5 = 10 |' in doc.blocks[1].text


def test_scanned_annotation_shares_cell_with_spatially_separate_native_text(tmp_path):
    source = [['Command', 'Description', 'Result'],
              ['Previous', 'Unprinted beginning. Continuation tail', ''],
              ['TIMES', 'Multiply two values', ''], ['SUM', 'Add the values', '']]
    printed = [['', 'Continuation tail', ''], *source[2:]]
    path = tmp_path / 'mixed-cell.pdf'; _printed_record_pdf(path, [printed])
    shown = [printed[0], ['TIMES', 'Multiply two values delta', ''], printed[2]]
    doc = document([('table', s.table_markdown(shown), 0)], fmt='hwp')
    # Below the native line but still inside its physical row (110..140 pt).
    doc.blocks[0]._table_ocr_cells = [(0, 'delta', (.4, .168, .5, .173))]
    assert s.apply_source_tables(doc, [source], pdf=path, columns_overflow=False) == 1
    assert doc.blocks[0].text == s.table_markdown([['Column 1', 'Column 2'],
        ['', 'Continuation tail'], ['TIMES', 'Multiply two values delta'], ['SUM', 'Add the values']])


@pytest.mark.parametrize('hint', [True, False])
def test_physical_grid_removes_only_proven_layout_cell_references(tmp_path, hint):
    source = [['Category', 'Code', 'Amount'], ['Previous', 'Unprinted prefix. Tail', ''],
              ['Service', 'SER-000', '4'], ['Data', 'DAR-000', '8']]
    printed = [['', 'Tail', ''], *source[2:]]
    path = tmp_path / 'spanning-cell.pdf'; _printed_record_pdf(path, [printed])
    # The model extends the service code/amount over the first line of Data.
    full = s.table_markdown([printed[0], printed[1], ['Data', 'SER-000', '4'], ['', 'DAR-000', '8']])
    once = s.table_markdown([printed[0], printed[1], ['Data', '', ''], ['', 'DAR-000', '8']])
    doc = document([('table', full, 0)], fmt='hwpx')
    if hint:
        doc.blocks[0]._table_layout_text = (full, once)
    count = s.apply_source_tables(doc, [source], pdf=path, columns_overflow=False)
    if hint:
        assert count == 1
        assert doc.blocks[0].text == s.table_markdown([['Category', 'Code', 'Amount'], *printed])
    else:
        assert count == 0
        assert doc.blocks[0].text == full


def test_physical_grid_may_join_fragments_inside_one_cell_and_keep_header_labels(tmp_path):
    source = [['Category', '', 'ID', 'Count'], ['Previous', '', 'Unprinted prefix. Tail', ''],
              ['Data requirement', '', 'DAR-000', '8'], ['Service', '', 'SER-000', '4']]
    printed = [['Category', 'ID', 'Count'], ['', 'Tail', ''],
               ['Data requirement', 'DAR-000', '8'], ['Service', 'SER-000', '4']]
    path = tmp_path / 'joined-label.pdf'; _printed_record_pdf(path, [printed])
    shown = [printed[0], printed[1], ['Data', 'DAR-000', '8'], ['requirement', '', ''], printed[3]]
    doc = document([('table', s.table_markdown(shown), 0)], fmt='hwpx')
    # A shorter label is also a complete source value elsewhere. That must
    # not prevent rejoining the actual native cell on this page.
    unrelated = [['Kind', 'Meaning'], ['Data', 'An unrelated source table']]
    assert s.apply_source_tables(doc, [source, unrelated], pdf=path, columns_overflow=False) == 1
    assert doc.blocks[0].text == s.table_markdown(printed)


def test_physical_cell_wrapping_requires_a_complete_cell_match():
    rows = [['Command', 'Input', 'Result'], ['DIV', '{12#12#0}', '12 12 0']]
    wrapped = [['Command', 'Input', 'Result'], ['DIV', '{1 2#12#0}', '12 12 0']]
    assert s._preserves_physical_table_text(s.table_markdown(wrapped), rows)
    # With complete physical glyph evidence, a layout cell merged across a
    # column border may also be corrected. No character or sign may disappear.
    merged = [['Command', 'Input', 'Result'], ['DIV {1', '2#12#0}', '12 12 0']]
    assert s._preserves_physical_table_text(s.table_markdown(merged), rows)
    for changed in [
        [['Command', 'Input', 'Result'], ['DIV', '{1 2#12#0}', '12 12 0 1']],
        [['Command', 'Input', 'Result'], ['DIV', '{-1 2#12#0}', '12 12 0']],
    ]:
        assert not s._preserves_physical_table_text(s.table_markdown(changed), rows)


def test_finish_retains_table_ocr_only_until_internal_postprocessing():
    from app.parsers.base import finish
    doc = document([('table', '| Name | Value |\n| --- | --- |\n| A | 2 x 5 |', 0)], fmt='hwp')
    raw = doc.blocks[0].dump()
    raw.pop('id'); raw.pop('readingOrder')
    evidence = [(0, '2 x 5', (.6, .1, .8, .2))]
    raw['_table_ocr_cells'] = evidence
    raw['_table_layout_text'] = (raw['text'], raw['text'])
    parsed = finish(doc.document.dump(), [p.dump() for p in doc.pages], [raw])
    assert parsed.blocks[0]._table_ocr_cells == evidence
    assert parsed.model_copy(deep=True).blocks[0]._table_ocr_cells == evidence
    assert ParsedDocument.model_validate_json(parsed.model_dump_json()).blocks[0]._table_ocr_cells == []
    assert parsed.blocks[0]._table_layout_text == (raw['text'], raw['text'])
    assert ParsedDocument.model_validate_json(parsed.model_dump_json()).blocks[0]._table_layout_text is None


@pytest.mark.parametrize('extra', [None, 'OCR-only detail', 'beta detail', ''])
def test_physical_grid_reassigns_body_fragments_without_losing_ocr_occurrences(tmp_path, extra):
    source = [['Category', 'Details'], ['Previous', 'Unprinted beginning. Continuation tail'],
              ['Second', 'alpha detail beta detail'], ['Third', 'gamma detail delta detail']]
    printed = [['', 'Continuation tail'], *source[2:]]
    path = tmp_path/'merged-labels.pdf'; _printed_record_pdf(path, [printed])
    # The layout model repeats one merged label while putting the second
    # row's last fragment under that wrong label.
    shown = [['', 'Continuation tail'], ['Second', 'alpha detail'],
             ['Third', 'beta detail'], ['Third', 'gamma detail'], ['Third', 'delta detail']]
    if extra is not None:
        shown.append(['Third', extra])
    original = s.table_markdown(shown)
    doc = document([('table', original, 0)], fmt='docx')
    result = s.apply_source_tables(doc, [source], pdf=path, columns_overflow=False)
    if extra is None:
        assert result == 1
        assert doc.blocks[0].text == s.table_markdown([['Category', 'Details'], *printed])
        assert doc.blocks[0].source.ref.startswith('printed-document-grid:')
        assert s.last_stats['uncertain_blocks'] == 1
    else:
        assert result == 0
        assert doc.blocks[0].text == original


@pytest.mark.parametrize('extra', ['OCR-only annotation', 'Outside physical grid', '%'])
def test_document_grid_fallback_cannot_drop_text_outside_its_cells(tmp_path, extra):
    import pymupdf
    source = [['Category','Details'], ['Previous label','Previous-page text. Continuation tail'],
              ['Second','Complete second detail'], ['Third','Complete third detail']]
    printed = [['','Continuation tail'], *source[2:]]
    path = tmp_path/'annotation.pdf'; _printed_record_pdf(path,[printed])
    if extra != 'OCR-only annotation':
        pdf=pymupdf.open(path)
        pdf[0].insert_text((45, 210), extra, fontsize=8)
        pdf.saveIncr(); pdf.close()
    shown = s.table_markdown([[row[1],row[0]] for row in printed]) + '\n' + extra
    doc = document([('table',shown,0)],fmt='docx')
    assert s.apply_source_tables(doc,[source],pdf=path,columns_overflow=False) == 0
    assert doc.blocks[0].text == shown


def test_csv_repeated_header_does_not_import_similar_records_from_the_next_page(tmp_path):
    rows = [['Field A', 'Field B', 'Field C']] + [[f'cell{i}_{c}' for c in range(3)] for i in range(1, 20)]
    fragments = [rows[:18], [rows[0], *rows[18:]]]
    path = tmp_path / 'records.pdf'
    _printed_record_pdf(path, fragments)
    doc = document([('table', s.table_markdown(fragment), i) for i, fragment in enumerate(fragments)],
                   pages=2, fmt='csv')
    s.apply_source_tables(doc, [rows], pdf=path, repeat_header=True)
    assert [b.text for b in doc.blocks] == [s.table_markdown(fragment) for fragment in fragments]


@pytest.mark.parametrize('fmt,strict', [('csv', False), ('txt', True)])
def test_delimited_records_keep_printed_duplicates_and_empty_column_positions(tmp_path, fmt, strict):
    rows = [['Item', 'Left', 'Right'], ['Widget', '', 'Y'], ['Widget', 'Y', ''],
            *[['Widget', '', 'Y']] * 10]
    shown = [rows[0], rows[1], rows[2], rows[1]]
    path = tmp_path / 'duplicates.pdf'
    _printed_record_pdf(path, [shown])
    doc = document([('table', '| Item | Left | Right |\n| --- | --- | --- |\n| Widget | Y | Y |', 0)], fmt=fmt)
    assert s.apply_source_tables(doc, [rows], pdf=path, strict_records=strict) == 1
    assert doc.blocks[0].text == s.table_markdown(shown)


def test_csv_does_not_combine_cells_from_distinct_printed_records(tmp_path):
    rows = [['Item', 'Color'], ['A', 'red'], ['B', 'blue'], ['A', 'blue']]
    shown = rows[:3]
    path = tmp_path / 'combinations.pdf'
    _printed_record_pdf(path, [shown])
    doc = document([('table', s.table_markdown(shown), 0)], fmt='csv')
    assert s.apply_source_tables(doc, [rows], pdf=path) == 1
    assert doc.blocks[0].text == s.table_markdown(shown)


def test_csv_keeps_decimal_punctuation_and_wrapped_complete_cells(tmp_path):
    rows = [['Description', 'Value'], ['North Bay', '1.01'], ['North Bay', '101']]
    path = tmp_path / 'wrapped.pdf'
    _printed_record_pdf(path, [[rows[0], ['North\nBay', '1.01']]])
    doc = document([('table', '| Description | Value |\n| --- | --- |\n| North | 1.01 |\n| Bay | |', 0)], fmt='csv')
    assert s.apply_source_tables(doc, [rows], pdf=path) == 1
    assert doc.blocks[0].text == s.table_markdown(rows[:2])


@pytest.mark.parametrize('source,printed', [
    ([['Code', 'Value'], ['AB', '1'], ['A B', '1']], [['Code', 'Value'], ['AB', '1']]),
    ([['Code', 'Value'], ['A', '1']], [['Code', 'Value'], ['A', '1'], ['Unknown', '2']]),
])
def test_csv_ambiguous_or_unmatched_grid_keeps_original_text(tmp_path, source, printed):
    path = tmp_path / 'uncertain.pdf'
    _printed_record_pdf(path, [printed])
    original = s.table_markdown(printed)
    doc = document([('table', original, 0)], fmt='csv')
    assert s.apply_source_tables(doc, [source], pdf=path) == 0
    assert doc.blocks[0].text == original
    assert s.last_stats['uncertain_blocks'] == 1


def test_csv_without_physical_grid_does_not_guess_unprinted_records(tmp_path):
    import pymupdf
    rows = [['Code', 'Value'], ['A', 'red'], ['B', 'blue'], ['A', 'blue']]
    path = tmp_path / 'no-grid.pdf'
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    page.insert_text((50, 100), 'Code Value A red B blue')
    pdf.save(path)
    pdf.close()
    original = s.table_markdown(rows[:3])
    doc = document([('table', original, 0)], fmt='csv')
    assert s.apply_source_tables(doc, [rows], pdf=path) == 0
    assert doc.blocks[0].text == original
    assert s.last_stats['uncertain_blocks'] == 1


@pytest.mark.parametrize('width', [2, 12])
def test_long_first_csv_record_is_not_repeated_as_a_column_label(width):
    import csv
    import io
    from lxml import html
    from app.convert import csv_body
    prose = 'A long first data record must remain data. ' * 50
    rows = [['en', prose, *['field'] * (width - 2)], ['de', 'Second record', *['value'] * (width - 2)]]
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    body, notices = csv_body(buffer.getvalue())
    tree = html.fromstring('<div>' + body + '</div>')
    assert 'csv_header_unverified' in notices
    assert tree.text_content().count(prose) == 1
    assert 'Column 2' in tree.text_content()


def test_long_csv_cells_use_labelled_records_without_splitting_source_rows():
    import csv
    import io
    from lxml import html
    from app.convert import csv_body
    text = 'Long prose with <tags> and & punctuation. ' * 30
    rows = [['en', text], ['de', 'A short second record.']]
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    body, notices = csv_body(buffer.getvalue())
    tree = html.fromstring('<div>' + body + '</div>')
    assert 'csv_records' in notices
    assert not tree.xpath('.//table')
    records = tree.findall('p')
    assert len(records) == 2
    assert records[0].text_content() == f'Column 1: en · Column 2: {text}'
    assert records[1].text_content() == 'Column 1: de · Column 2: A short second record.'


@pytest.mark.parametrize('early,late', [('-100','100'), ('100','-100'), ('15%','15'), ('(250)','250')])
def test_sheet_page_break_preserves_different_signed_records(tmp_path, early, late):
    import pymupdf
    rows = [['Account', 'Amount', 'Memo']] + [[f'Acct{i:02}', str(i*37+1000), f'memo{i:02}'] for i in range(1,41)]
    rows[20:22] = [['Adj', early, 'reversal'], ['Adj', late, 'reversal']]
    pages = [[rows[0], *rows[1:21]], [rows[0], *rows[21:41]]]
    pdf = pymupdf.open()
    for pg in pages:
        page = pdf.new_page(width=600, height=800)
        for i, row in enumerate(pg):
            for x, cell in zip((50,250,420), row):
                page.insert_text((x,80+20*i), cell, fontsize=9)
    path = tmp_path/'signs.pdf'; pdf.save(path); pdf.close()
    doc = document([('paragraph', ' '.join(' '.join(row) for row in pg), i) for i,pg in enumerate(pages)], pages=2)
    s.apply_source_tables(doc, [rows], sheets=True, pdf=path)
    for pn, value in enumerate((early,late)):
        actual = '\n'.join(b.text for b in doc.blocks if b.regions[0].page_index == pn)
        assert [line for line in actual.splitlines() if 'Adj' in line] == [f'| Adj | {value} | reversal |']


@pytest.mark.parametrize('value', ['−100', '△100', '-100', '(250)', '15%'])
def test_value_evidence_distinguishes_signs_and_units(value):
    unsigned = ''.join(c for c in value if c.isdigit())
    assert not s._preserves_text(value, unsigned)
    assert not s._preserves_text(unsigned, value)


def test_sheet_continuation_uses_column_names_after_title_and_blank_row(tmp_path):
    import pymupdf
    rows = [['Quarterly expense report','',''], ['','',''], ['Employee','Category','Amount']]
    rows += [[f'Employee {i:02}', 'Travel', str(i*41+300)] for i in range(1,41)]
    pages = [rows[:23], rows[23:]]
    pdf = pymupdf.open()
    for pg in pages:
        page = pdf.new_page(width=600,height=800)
        for i,row in enumerate(pg):
            for x,cell in zip((50,250,420),row):
                if cell: page.insert_text((x,80+25*i),cell,fontsize=9)
    path = tmp_path/'title.pdf'; pdf.save(path); pdf.close()
    doc = document([('table',s.table_markdown([r for r in pg if any(r)]),p) for p,pg in enumerate(pages)], pages=2)
    s.apply_source_tables(doc,[rows],sheets=True,pdf=path)
    tables = [b for b in doc.blocks if b.type == 'table']
    assert len(tables) == 2
    assert all(b.text.splitlines()[0] == '| Employee | Category | Amount |' for b in tables)
    assert sum(b.text.count('Quarterly expense report') for b in doc.blocks) == 1


def test_sheet_continuation_keeps_labels_beside_constant_numeric_helper(tmp_path):
    import pymupdf
    header = ['Name', 'Amount', 'Percent', '1']
    rows = [['Matching list', '', '', ''], ['', '', '', ''], header]
    rows += [[f'Name{i:02}', str(i * 37 + 1000), f'{i}%', '1'] for i in range(1, 31)]
    pages = [rows[:18], rows[18:]]
    pdf = pymupdf.open()
    for records in pages:
        page = pdf.new_page(width=600, height=800)
        for i, row in enumerate(records):
            for x, cell in zip((50, 240, 360, 480), row):
                if cell:
                    page.insert_text((x, 80 + 25 * i), cell, fontsize=9)
    path = tmp_path / 'numeric-helper.pdf'
    pdf.save(path); pdf.close()
    doc = document([('table', s.table_markdown([r for r in records if any(r)]), p)
                    for p, records in enumerate(pages)], pages=2)
    s.apply_source_tables(doc, [rows], sheets=True, pdf=path)
    tables = [b for b in doc.blocks if b.type == 'table']
    assert len(tables) == 2
    assert all(b.text.splitlines()[0] == '| Name | Amount | Percent | 1 |' for b in tables)
    actual = [line for b in tables for line in b.text.splitlines() if re.match(r'\| Name\d', line)]
    assert actual == ['| ' + ' | '.join(row) + ' |' for row in rows[3:]]


def test_numeric_cells_do_not_turn_a_text_record_into_column_labels():
    assert s._header_row([['Alice', 'Ops', 'London', '1'], ['Bob', 'Sales', 'Paris', '1'],
                          ['Carol', 'Finance', 'Seoul', '1']]) is None
    assert s._header_row([['Alice', 'Ops', 'London', '100'], ['Bob', 'Sales', 'Paris', '200'],
                          ['Carol', 'Finance', 'Seoul', '300']]) is None


def test_short_clipped_sheet_label_does_not_leave_two_copies_of_records(tmp_path):
    import pymupdf
    rows = [['Field', 'Value', 'Name'], ['Report', 'Expense report name', 'Note'],
            ['Entries', 'Expense type', 'Transaction']] + [['Expense Row', 'Taxi', '']] * 5
    shown = [row[:] for row in rows]
    shown[1][1] = 'Expense report nam'
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    for i, row in enumerate(shown):
        for x, cell in zip((50, 220, 420), row):
            if cell: page.insert_text((x, 90+22*i), cell, fontsize=9)
    path = tmp_path/'clipped-label.pdf'; pdf.save(path); pdf.close()
    doc = document([('paragraph', ' '.join(' '.join(row) for row in shown), 0),
                    ('table', s.table_markdown(shown), 0)])
    s.apply_source_tables(doc, [rows], sheets=True, pdf=path)
    body = '\n'.join(b.text for b in doc.blocks)
    assert body.count('Expense Row') == 5
    assert body.count('Expense report nam') == 1
    assert 'Expense report name' not in body


def test_blank_tail_of_wide_sheet_does_not_import_unprinted_field_names(tmp_path):
    import pymupdf
    rows = [['Form', '', '', '', '', '', '', 'Login id'],
            ['XML Element', 'Name', 'Date', 'OrgUnit1', 'OrgUnit2', 'Custom1', 'Custom2', 'Custom3']]
    rows += [['Expense Row', '', '', '', '', '', '', '']] * 40
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    for i in range(4): page.insert_text((60, 100+20*i), 'Expense Row', fontsize=9)
    path = tmp_path/'blank-columns.pdf'; pdf.save(path); pdf.close()
    # The layout parser collapsed two equal rows. Physical PDF evidence must
    # restore the missing record without importing the wide source header.
    shown = [['Expense Row']] * 3
    doc = document([('table', s.table_markdown(shown), 0)])
    s.apply_source_tables(doc, [rows], sheets=True, pdf=path)
    body = '\n'.join(b.text for b in doc.blocks)
    assert body.count('Expense Row') == 4
    assert all(label not in body for label in rows[1])


def test_cjk_neighbour_requires_ordered_cell_not_shared_syllables():
    assert not s._cells_are_printed(['나. 기술 평가'], '기술 평가 | 가용성 및 기술 수준 | 나머지 일반 항목 평가')
    assert s._cells_are_printed(['나. 기술 평가'], '나. 기 술 평 가')


def test_csv_header_with_one_typed_column_or_email_and_headerless_text():
    from app.convert import csv_body
    from lxml import html
    for text, headers in [('name,value,description\nalpha,1,first\nbeta,2,second', ['name','value','description']),
                          ('name,email,city\nAlice,a@example.org,Berlin', ['name','email','city'])]:
        body, notices = csv_body(text)
        assert 'csv_header_inferred' in notices
        assert html.fromstring(body).xpath('.//th/text()') == headers
    # A row-1 value recurring in its own column ('da') is record evidence.
    body, notices = csv_body('da,Første tekst\nen,Second text\nda,Dritter Text')
    assert 'csv_header_unverified' in notices
    tree = html.fromstring(body)
    assert tree.xpath('.//th/text()') == ['Column 1','Column 2']
    assert tree.xpath('.//tbody/tr[1]/td/text()') == ['da','Første tekst']


def test_caption_inside_coarse_table_box_has_its_own_owner(tmp_path):
    import pymupdf
    rows = [['Step','Owner','Description'], ['Install','Field team','Install the mobile app'],
            ['Report','Citizen','Send a pothole report']]
    pdf = pymupdf.open(); page = pdf.new_page(width=600,height=800)
    page.insert_text((50,80),'Table 3. Work steps',fontsize=9)
    for i,row in enumerate(rows):
        for x,cell in zip((50,200,330),row): page.insert_text((x,110+30*i),cell,fontsize=9)
    path = tmp_path/'caption.pdf'; pdf.save(path); pdf.close()
    merged = [[row[0],row[1]+' '+row[2]] for row in rows]
    doc = document([('caption','Table 3. Work steps',0),('table',s.table_markdown(merged),0)],fmt='docx')
    doc.blocks[0].regions[0].bbox.y1 = .11
    assert s.apply_source_tables(doc,[rows],pdf=path,columns_overflow=False) == 1
    assert doc.blocks[1].text == s.table_markdown(rows)
    assert doc.blocks[0].text == 'Table 3. Work steps'


@pytest.mark.parametrize('korean',[False,True])
def test_clipped_sheet_row_keeps_its_printed_cells_in_the_same_grid(tmp_path,korean):
    import pymupdf
    header = ['지역','상태','담당'] if korean else ['Region','Status','Owner']
    rows = [header] + [[f'Region{i:02}', 'APP_COMPLETE' if i==5 else 'Active', f'Owner{i:02}'] for i in range(1,11)]
    printed = [[c.replace('APP_COMPLETE','APP_COMP') for c in row] for row in rows]
    pdf = pymupdf.open(); page = pdf.new_page(width=600,height=800)
    page.insert_font(fontname='review',fontbuffer=pymupdf.Font('cjk').buffer)
    for i,row in enumerate(printed):
        for x,cell in zip((50,230,370),row): page.insert_text((x,80+20*i),cell,fontsize=10,fontname='review')
    path = tmp_path/'clipped.pdf'; pdf.save(path); pdf.close()
    doc = document([('paragraph',' '.join(row),0) for row in printed])
    for i,b in enumerate(doc.blocks):
        b.regions[0].bbox.y0 = (80+20*i-12)/800
        b.regions[0].bbox.y1 = (80+20*i+4)/800
    s.apply_source_tables(doc,[rows],sheets=True,pdf=path)
    assert [b.type for b in doc.blocks] == ['table']
    assert doc.blocks[0].text == s.table_markdown(printed)
    assert 'APP_COMPLETE' not in '\n'.join(b.text for b in doc.blocks)


def test_optional_sheet_recovery_failure_keeps_the_original_parse(monkeypatch):
    rows=[['Code','Value'],['A','First value']]
    doc=document([('table',s.table_markdown(rows),0)])
    original=doc.model_dump()
    def broken(candidate,*args):
        candidate.blocks[0].text='partial replacement'
        raise KeyError('synthetic corrupt grid')
    monkeypatch.setattr(s,'_sheet_pages',broken)
    assert s.apply_source_tables(doc,[rows],sheets=True) == 0
    assert doc.model_dump() == original
    assert s.last_stats['uncertain_blocks'] > 0
