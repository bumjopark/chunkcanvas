from copy import deepcopy

import pymupdf
import pytest

from app.models import ParsedDocument
from app.parsers import source_tables as s
from app.parsers.sheet_grids import recover_sheet_grids


def fixture(tmp_path, *, raster=False, rotated=False):
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    source = [['Field', 'Mandatory', 'Value'], ['Location', 'Y', '1.01'],
              ['Location', 'Y', '1.01'], ['Clipped', 'N', 'APP_COMPLETE']]
    for y in [80, 120, 160, 200, 240]:
        page.draw_line((40, y), (500, y))
    for x in [40, 220, 360, 500]:
        page.draw_line((x, 80), (x, 240))
    for i, row in enumerate(source):
        for x, text in zip([50, 230, 370], row):
            y = 100 + 40 * i
            if text == 'Mandatory':
                page.insert_text((x, y), 'Mandator', fontsize=9)
                page.insert_text((x, y + 12), 'y', fontsize=9)
            else:
                page.insert_text((x, y), 'APP_COMP' if text == 'APP_COMPLETE' else text, fontsize=9)
    page.insert_text((50, 280), 'Printed clock 14:27:17', fontsize=10)
    page.insert_text((50, 310), 'Independent annotation remains.', fontsize=10)
    if rotated:
        page.insert_text((540, 350), 'VERTICAL NOTE', fontsize=10, rotate=90)
    if raster:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100))
        pix.clear_with(180)
        page.insert_image(pymupdf.Rect(50, 350, 150, 450), pixmap=pix)
    path = tmp_path / 'grid.pdf'
    pdf.save(path)
    text = page.get_text()
    pdf.close()
    data = {'document': {'id': 'grid', 'filename': 'grid.xlsx', 'fileHash': 'h', 'pageCount': 1,
                         'sourceFormat': 'xlsx', 'parser': 'hybrid', 'parserVersion': 'test'},
            'pages': [{'index': 0, 'width': 600, 'height': 800}],
            'blocks': [{'id': 'b0', 'readingOrder': 0, 'type': 'table', 'text': text,
                        'regions': [{'pageIndex': 0, 'bbox': {'x0': .05, 'y0': .08, 'x1': .95, 'y1': .9}}],
                        'source': {'parser': 'test'}}], 'blocksFingerprint': 'f'}
    return ParsedDocument.model_validate(data), source, path


def run(doc, source, path, protected=()):
    return recover_sheet_grids(doc, path, [source], set(protected), s._printed_cells(path, doc))


def test_grid_keeps_only_printed_rows_and_preserves_unmatched_cells_and_notes(tmp_path):
    doc, rows, path = fixture(tmp_path)
    rows += [['Location', 'Y', '1.01']] * 30 + [['Unprinted', 'N', '999']]
    rows += [['Printed clock 14:27:19', '', '']]
    assert run(doc, rows, path) == 1
    table = next(b for b in doc.blocks if b.type == 'table')
    assert table.text == s.table_markdown([rows[0], rows[1], rows[2], ['Clipped', 'N', 'APP_COMP']])
    whole = '\n'.join(b.text for b in doc.blocks)
    assert whole.count('| Location | Y | 1.01 |') == 2
    assert 'APP_COMPLETE' not in whole and 'Unprinted' not in whole and '999' not in whole
    assert 'Printed clock 14:27:17' in whole and '14:27:19' not in whole
    assert 'Independent annotation remains.' in whole
    assert table.source.ref.startswith('printed-sheet-grid:')
    assert table.line_geometry == 'unavailable'
    assert len({b.id for b in doc.blocks}) == len(doc.blocks)


def test_overlapping_native_paragraph_does_not_duplicate_records(tmp_path):
    doc, rows, path = fixture(tmp_path)
    doc.blocks.append(doc.blocks[0].model_copy(update={'id': 'b1', 'type': 'paragraph', 'reading_order': 1}))
    assert run(doc, rows, path) == 1
    assert '\n'.join(b.text for b in doc.blocks).count('| Location | Y | 1.01 |') == 2


def test_grid_never_replaces_a_matched_source_table(tmp_path):
    doc, rows, path = fixture(tmp_path)
    before = deepcopy(doc.dump())
    assert run(doc, rows, path, ['b0']) == 0
    assert doc.dump() == before


def test_grid_preserves_rotated_note_as_separate_atomic_block(tmp_path):
    doc, rows, path = fixture(tmp_path, rotated=True)
    assert run(doc, rows, path) == 1
    note = next(b for b in doc.blocks if b.text == 'VERTICAL NOTE')
    assert note.lines is None and note.type == 'paragraph'
    assert 'Independent annotation' not in note.text


def test_rotated_shadow_is_one_occurrence_but_separate_note_stays(tmp_path):
    doc, rows, path = fixture(tmp_path, rotated=True)
    with pymupdf.open(path) as pdf:
        pdf[0].insert_text((541, 351), 'VERTICAL NOTE', fontsize=10, rotate=90)
        pdf[0].insert_text((540, 600), 'VERTICAL NOTE', fontsize=10, rotate=90)
        doc.blocks[0].text = pdf[0].get_text()
        pdf.saveIncr()
    assert run(doc, rows, path) == 1
    assert sum(b.text == 'VERTICAL NOTE' for b in doc.blocks) == 2


def test_raster_text_without_an_existing_picture_retains_original_parse(tmp_path):
    doc, rows, path = fixture(tmp_path, raster=True)
    before = deepcopy(doc.dump())
    assert run(doc, rows, path) == 0
    assert doc.dump() == before


def test_ocr_only_text_is_not_discarded_by_native_recovery(tmp_path):
    doc, rows, path = fixture(tmp_path)
    doc.blocks[0].text += '\nUnique content supplied only by OCR must remain intact.'
    before = deepcopy(doc.dump())
    assert run(doc, rows, path) == 0
    assert doc.dump() == before


@pytest.mark.parametrize('other_page', [False, True])
def test_unresolved_overlapping_text_cannot_claim_and_discard_table_glyphs(tmp_path, other_page):
    doc, rows, path = fixture(tmp_path)
    # A layout paragraph has an inaccurate box over the last printed cell.
    # Its unrelated text (or a continuation on another page) is not proof that
    # it owns those glyphs. Partial grid recovery used to drop APP_COMP here.
    regions = [{'pageIndex': 0, 'bbox': {'x0': .60, 'y0': .25, 'x1': .85, 'y1': .29}}]
    if other_page:
        regions.append({'pageIndex': 1, 'bbox': {'x0': .1, 'y0': .1, 'x1': .8, 'y1': .2}})
    unrelated = doc.blocks[0].model_copy(deep=True)
    unrelated = type(unrelated).model_validate({**unrelated.model_dump(by_alias=True),
        'id': 'unresolved', 'readingOrder': 1, 'type': 'paragraph',
        'text': 'Unrelated continuation with uncertain geometry.', 'regions': regions})
    doc.blocks.append(unrelated)
    before = deepcopy(doc.dump())
    assert run(doc, rows, path) == 0
    assert doc.dump() == before
    assert 'APP_COMP' in doc.blocks[0].text


@pytest.mark.parametrize('right_edge', [.86, .99])
def test_overlapping_annotation_can_keep_its_printed_text_without_blocking_grid(tmp_path, right_edge):
    doc, rows, path = fixture(tmp_path)
    with pymupdf.open(path) as pdf:
        pdf[0].insert_text((500, 400), 'Independent note', fontsize=10)
        pdf.saveIncr()
    # The coarse table box overlaps all or just a clipped prefix of a separate
    # annotation. Its native text is represented by that untouched paragraph.
    doc.blocks[0].regions[0].bbox.x1 = right_edge
    annotation = type(doc.blocks[0]).model_validate({**doc.blocks[0].model_dump(by_alias=True),
        'id': 'annotation', 'readingOrder': 1, 'type': 'paragraph',
        'text': 'Independent note with additional OCR-only content.',
        'regions': [{'pageIndex': 0, 'bbox': {'x0': .82, 'y0': .47, 'x1': .99, 'y1': .53}}]})
    doc.blocks.append(annotation)
    assert run(doc, rows, path) == 1
    table = next(b for b in doc.blocks if b.type == 'table')
    assert '| Location | Y | 1.01 |' in table.text
    assert 'APP_COMP' in table.text
    assert next(b for b in doc.blocks if b.id == 'annotation').text == annotation.text
    assert sum(b.text.count('Independent note') for b in doc.blocks) == 1


def test_overlapping_annotation_cannot_claim_two_printed_occurrences_as_one(tmp_path):
    doc, rows, path = fixture(tmp_path)
    with pymupdf.open(path) as pdf:
        pdf[0].insert_text((50, 350), 'Distinct note', fontsize=10)
        pdf[0].insert_text((50, 370), 'Distinct note', fontsize=10)
        pdf.saveIncr()
    annotation = type(doc.blocks[0]).model_validate({**doc.blocks[0].model_dump(by_alias=True),
        'id': 'annotation', 'readingOrder': 1, 'type': 'paragraph',
        'text': 'Distinct note with additional OCR-only content.',
        'regions': [{'pageIndex': 0, 'bbox': {'x0': .05, 'y0': .41, 'x1': .5, 'y1': .48}}]})
    doc.blocks.append(annotation)
    before = deepcopy(doc.dump())
    assert run(doc, rows, path) == 0
    assert doc.dump() == before


def test_overlapping_annotation_cannot_discard_an_unrepresented_operator(tmp_path):
    doc, rows, path = fixture(tmp_path)
    with pymupdf.open(path) as pdf:
        pdf[0].insert_text((50, 400), '+', fontsize=10)
        pdf.saveIncr()
    doc.blocks[0].text += '\n+'
    annotation = type(doc.blocks[0]).model_validate({**doc.blocks[0].model_dump(by_alias=True),
        'id': 'annotation', 'readingOrder': 1, 'type': 'paragraph',
        'text': 'Unrelated OCR annotation.',
        'regions': [{'pageIndex': 0, 'bbox': {'x0': .05, 'y0': .47, 'x1': .2, 'y1': .53}}]})
    doc.blocks.append(annotation)
    before = deepcopy(doc.dump())
    assert run(doc, rows, path) == 0
    assert doc.dump() == before


@pytest.mark.parametrize('column,value', [(1, 'Category'), (2, '101')])
def test_cell_normalization_keeps_punctuation_and_unmatched_single_letters(tmp_path, column, value):
    doc, rows, path = fixture(tmp_path)
    rows[1][column] = value
    rows[2][column] = value
    assert run(doc, rows, path) == 1
    whole = '\n'.join(b.text for b in doc.blocks)
    assert whole.count('| Location | Y | 1.01 |') == 2
    assert 'Category' not in whole and '| 101 |' not in whole


def test_validated_document_serializes_all_rebuilt_line_geometry(tmp_path):
    doc, rows, path = fixture(tmp_path)
    assert run(doc, rows, path) == 1
    reloaded = ParsedDocument.model_validate(doc.dump())
    for b in reloaded.blocks:
        for line in b.lines or []:
            assert 0 <= line.start < line.end <= len(b.text)
            assert len(line.xs) == line.end - line.start


def test_source_restore_uses_printed_grid_after_whole_source_match_fails(tmp_path):
    doc, rows, path = fixture(tmp_path)
    previous = doc.blocks_fingerprint
    assert s.apply_source_tables(doc, [rows], sheets=True, pdf=path) == 1
    table = next(b for b in doc.blocks if b.type == 'table')
    assert table.source.ref.startswith('printed-sheet-grid:')
    assert table.text.count('| Location | Y | 1.01 |') == 2
    assert doc.blocks_fingerprint != previous
    assert s.last_stats['uncertain_blocks'] == 1


def test_optional_native_grid_failure_does_not_publish_partial_mutations(tmp_path, monkeypatch):
    from app.parsers import sheet_grids
    doc, rows, path = fixture(tmp_path)
    original = deepcopy(doc.dump())
    def fail(candidate, *args):
        candidate.blocks[0].text = 'partial mutation must not escape'
        raise RuntimeError('synthetic page-grid failure')
    monkeypatch.setattr(sheet_grids, 'recover_sheet_grids', fail)
    assert s.apply_source_tables(doc, [rows], sheets=True, pdf=path) == 0
    assert doc.dump() == original


def test_rotated_page_keeps_existing_coordinate_frame(tmp_path):
    doc, rows, path = fixture(tmp_path)
    with pymupdf.open(path) as pdf:
        pdf[0].set_rotation(90)
        pdf.saveIncr()
    before = deepcopy(doc.dump())
    assert run(doc, rows, path) == 0
    assert doc.dump() == before


def test_merged_physical_cell_is_emitted_only_once(tmp_path):
    doc, _, path = fixture(tmp_path)
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    for y in [80, 120, 200]:
        page.draw_line((40, y), (500, y))
    page.draw_line((220, 160), (500, 160))
    for x in [40, 220, 360, 500]:
        page.draw_line((x, 80), (x, 200))
    for x, y, value in [(50, 100, 'Name'), (230, 100, 'Type'), (370, 100, 'Count'),
                        (50, 140, 'Shared'), (230, 140, 'Alpha'), (370, 140, '1'),
                        (230, 180, 'Beta'), (370, 180, '2')]:
        page.insert_text((x, y), value, fontsize=10)
    text = page.get_text()
    path = tmp_path / 'merged.pdf'
    pdf.save(path)
    pdf.close()
    doc.blocks[0].text = text
    rows = [['Name', 'Type', 'Count'], ['Shared', 'Alpha', '1'], ['Shared', 'Beta', '2']]
    assert run(doc, rows, path) == 1
    whole = '\n'.join(b.text for b in doc.blocks)
    assert whole.count('Shared') == 1
    assert '| Shared | Alpha | 1 |' in whole and '|  | Beta | 2 |' in whole


def borderless_fixture(tmp_path, rows=None, *, centered=False):
    doc, _, _ = fixture(tmp_path)
    rows = rows or [['Name', 'Code', 'Total'], ['Alpha', '27', '4.50'],
                    ['Bravo', '35', '6.75'], ['Charlie', '18', '9.25']]
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    for i, row in enumerate(rows):
        for x, value in zip([50, 230, 370], row):
            if centered and x != 50:
                x -= pymupdf.get_text_length(value, fontsize=10) / 2
            page.insert_text((x, 100 + 20 * i), value, fontsize=10)
    page.insert_text((50, 240), 'Independent printed annotation.', fontsize=10)
    path = tmp_path / 'borderless.pdf'
    pdf.save(path)
    doc.blocks[0].text = page.get_text()
    pdf.close()
    return doc, rows, path


@pytest.mark.parametrize('absorbed_note', [False, True])
def test_borderless_rows_repair_shifted_cells_from_complete_physical_records(tmp_path, absorbed_note):
    doc, rows, path = borderless_fixture(tmp_path)
    # The layout parser assigned one row's amount to its neighbour. All glyphs
    # remain, so preservation counts alone cannot prove the cell relationship.
    wrong = [rows[0], ['Alpha', '35', '6.75'], ['Bravo', '27', '4.50'], rows[3]]
    if absorbed_note:
        wrong.append(['Independent printed annotation.', '', ''])
    doc.blocks[0].text = s.table_markdown(wrong)
    assert run(doc, rows, path) == 1
    assert next(b.text for b in doc.blocks if b.type == 'table') == s.table_markdown(rows)
    assert '\n'.join(b.text for b in doc.blocks).count('Independent printed annotation.') == 1


def test_borderless_rows_keep_physical_repeats_without_copying_unprinted_records(tmp_path):
    visible = [['Name', 'Code', 'Total'], ['Alpha', '27', '4.50'], ['Alpha', '27', '4.50']]
    doc, rows, path = borderless_fixture(tmp_path, visible)
    source = rows + [rows[1]] * 20 + [['Unprinted', '999', '800']]
    assert run(doc, source, path) == 1
    text = '\n'.join(b.text for b in doc.blocks)
    assert text.count('| Alpha | 27 | 4.50 |') == 2
    assert 'Unprinted' not in text and '999' not in text


def test_borderless_rows_require_the_whole_source_record_on_one_baseline(tmp_path):
    doc, rows, path = borderless_fixture(tmp_path)
    source = [rows[0], ['Alpha', '35', '6.75'], ['Bravo', '27', '4.50'], rows[3]]
    before = deepcopy(doc.dump())
    assert run(doc, source, path) == 0
    assert doc.dump() == before


def test_borderless_source_match_reclaims_a_data_cell_misclassified_as_footer(tmp_path):
    doc, rows, path = borderless_fixture(tmp_path)
    with pymupdf.open(path) as pdf:
        raw = pdf[0].search_for('9.25')[0]
    footer = doc.blocks[0].model_copy(update={
        'id': 'data-footer', 'type': 'page_footer', 'text': '9.25',
        'regions': [doc.blocks[0].regions[0].model_copy(update={'bbox': doc.blocks[0].regions[0].bbox.model_copy(
            update={'x0': raw.x0 / 600, 'y0': raw.y0 / 800, 'x1': raw.x1 / 600, 'y1': raw.y1 / 800})})]})
    doc.blocks.append(footer)
    assert run(doc, rows, path) == 1
    assert next(b.text for b in doc.blocks if b.type == 'table') == s.table_markdown(rows)
    assert not any(b.id == 'data-footer' for b in doc.blocks)
    assert sum(b.text.count('9.25') for b in doc.blocks) == 1


def test_borderless_recovery_preserves_protected_furniture_and_tables(tmp_path):
    doc, rows, path = borderless_fixture(tmp_path)
    before = deepcopy(doc.dump())
    assert run(doc, rows, path, ['b0']) == 0
    assert doc.dump() == before


def test_center_aligned_numbers_keep_rows_when_their_digit_width_changes(tmp_path):
    rows = [['Name', 'Code', 'Total'], ['Alpha', '123456', '4.50'],
            ['Bravo', '12', '6.75'], ['Charlie', '5555555', '9.25']]
    doc, rows, path = borderless_fixture(tmp_path, rows, centered=True)
    assert run(doc, rows, path) == 1
    assert next(b.text for b in doc.blocks if b.type == 'table') == s.table_markdown(rows)


def test_borderless_continuation_uses_observed_source_headers_without_previous_data(tmp_path):
    doc, _, _ = fixture(tmp_path)
    rows = [['Name', 'Code', 'Total'], ['Alpha', '27', '4.50'], ['Bravo', '35', '6.75'],
            ['Charlie', '18', '9.25'], ['Delta', '44', '8.00']]
    pdf = pymupdf.open()
    fragments = [rows[:3], rows[3:]]
    for fragment in fragments:
        page = pdf.new_page(width=600, height=800)
        for i, row in enumerate(fragment):
            for x, value in zip([50, 230, 370], row):
                page.insert_text((x, 100 + 20 * i), value, fontsize=10)
    path = tmp_path / 'continuation.pdf'
    pdf.save(path)
    pdf.close()
    doc.document.page_count = 2
    doc.pages.append(doc.pages[0].model_copy(update={'index': 1}))
    doc.blocks[0].text = s.table_markdown(rows[:3])
    doc.blocks.append(doc.blocks[0].model_copy(update={
        'id': 'page2', 'text': s.table_markdown(rows[3:]),
        'regions': [doc.blocks[0].regions[0].model_copy(update={'page_index': 1})]}))
    assert run(doc, rows, path, ['b0']) == 1
    page2 = next(b for b in doc.blocks if b.regions[0].page_index == 1 and b.type == 'table')
    assert page2.text == s.table_markdown([rows[0], *rows[3:]])
    assert 'Alpha' not in page2.text and 'Bravo' not in page2.text


@pytest.mark.parametrize('fragment', ['Amber', 'Ambe', '#####'])
def test_large_borderless_sheet_retains_visible_columns_with_repeated_values(tmp_path, fragment):
    # Most source rows have the same year/name. Their record numbers still
    # distinguish the few actually printed records from more than 64 matches.
    source_value = '12345678' if fragment == '#####' else 'Amber'
    source = [['Record', 'Year', 'State', 'Hidden'], *[
        [str(i), '2018', source_value, 'Not printed'] for i in range(1, 151)]]
    visible = [['Record', 'Year', 'State'], *[
        [str(i), '2018', fragment] for i in (31, 32, 33)]]
    doc, _, path = borderless_fixture(tmp_path, visible)
    assert run(doc, source, path) == 1
    whole = '\n'.join(b.text for b in doc.blocks)
    for row in visible[1:]:
        assert '| ' + ' | '.join(row) + ' |' in whole
    assert 'Not printed' not in whole
    assert whole.count('Independent printed annotation.') == 1
    if fragment == 'Ambe':
        assert 'Amber' not in whole
    if fragment == '#####':
        assert '12345678' not in whole


def test_large_sheet_filter_keeps_competing_visible_column_assignments(tmp_path):
    from app.parsers.sheet_rows import PrintedRows
    from app.parsers.textlayer import read_page
    visible = [['Record', 'Year', 'Value'], ['31', '2018', 'Amber'], ['32', '2018', 'Amber']]
    doc, _, path = borderless_fixture(tmp_path, visible)
    source = [[str(i), '2018', 'Amber', 'Not printed'] for i in range(1, 151)]
    # Both partitions spell the exact printed glyphs. A cheap order filter
    # cannot decide that the first table owns these rows.
    competing = [['31', '2018', 'Am', 'ber'], ['32', '2018', 'Am', 'ber']]
    with pymupdf.open(path) as pdf:
        native = read_page(pdf[0], 0)
        assert PrintedRows([source, competing]).grids(native, []) == []
