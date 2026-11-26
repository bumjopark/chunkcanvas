"""Overlapping wrapped labels keep their matched PDF glyphs, not a rectangular crop."""
from collections import Counter

import pymupdf

from app.models import ParsedDocument
from app.parsers import source_tables as source_tables
from app.parsers.sheet_grids import recover_sheet_grids
from app.parsers.sheet_rows import PrintedRows
from app.parsers.textlayer import read_page


def _fixture(tmp_path):
    rows = [['Rounded sample', '9.876543210', '-9.876543210', 'Initial mode', '654321'],
            ['Upper label', '12', '-12', 'Upper mode', '1010'],
            ['Wrapped lower label', '13', '-13', 'Wrapped lower mode', '2020'],
            ['Final label', '14', '-14', 'Final mode', '3030']]
    printed = [list(row) for row in rows]
    printed[0][1:3] = ['9.876543', '-9.876543']
    pdf = pymupdf.open()
    page = pdf.new_page(width=700, height=500)
    for ri, row in enumerate(printed):
        for ci, text in enumerate(row):
            if ri == 2 and ci in (0, 3):
                continue
            x = [40, 210, 310, 410, 610][ci]
            page.insert_text((x, 100 + ri * 15), text, fontsize=9)
    # Wrapped labels are emitted separately, just as spreadsheet PDF exporters do.
    # Their first physical line overlaps the preceding logical row.
    for x, tail in [(40, 'lower label'), (410, 'lower mode')]:
        page.insert_text((x, 117), 'Wrapped', fontsize=9)
        page.insert_text((x, 130), tail, fontsize=9)
    path = tmp_path / 'ownership.pdf'
    pdf.save(path)
    text = page.get_text()
    doc = ParsedDocument.model_validate({
        'document': {'id': 'd', 'filename': 'synthetic.xlsx', 'fileHash': 'h', 'pageCount': 1,
                     'sourceFormat': 'xlsx', 'parser': 'hybrid', 'parserVersion': 'test'},
        'pages': [{'index': 0, 'width': 700, 'height': 500}],
        'blocks': [{'id': 'b0', 'readingOrder': 0, 'type': 'paragraph', 'text': text,
                    'regions': [{'pageIndex': 0, 'bbox': {'x0': 0, 'y0': .1, 'x1': 1, 'y1': .5}}],
                    'source': {'parser': 'test'}}], 'blocksFingerprint': 'f'})
    pdf.close()
    return rows, printed, path, doc


def _rows(doc):
    return [[c.strip() for c in line.strip().strip('|').split('|')]
            for b in doc.blocks if b.type == 'table' for line in b.text.splitlines()[2:]]


def test_wrapped_overlapping_labels_keep_their_row_and_every_visible_value(tmp_path):
    source, printed, path, doc = _fixture(tmp_path)
    assert recover_sheet_grids(doc, path, [source], set(), source_tables._printed_cells(path, doc)) > 0
    rows = _rows(doc)
    for row in printed:
        assert row in rows
    assert Counter(tuple(row) for row in rows) == Counter(tuple(row) for row in printed)
    assert all(not b.text.strip() for b in doc.blocks if b.type != 'table')
    assert source[0][1] not in '\n'.join(b.text for b in doc.blocks)


def test_source_matched_cells_keep_disjoint_character_identities(tmp_path):
    source, _, path, _ = _fixture(tmp_path)
    with pymupdf.open(path) as pdf:
        native = read_page(pdf[0], 0)
        grids = PrintedRows([source]).grids(native, [])
        owners = [id(ch) for g in grids for chars in g.glyph_owners.values() for ch in chars]
        assert len(owners) == len(set(owners))
        assert set(owners) == {id(ch) for f in native.fragments for ch in f.chars if ch.c.strip()}


def test_rounding_cannot_supply_a_complete_anchor_or_erase_a_sign():
    assert PrintedRows._match([(0, '9.876543210'), (1, '-9.876543210')], '9.876543-9.876543', {0, 8, 17})[0][1] == 0
    assert PrintedRows._match([(0, '-9.876543210')], '9.876543') is None
    assert PrintedRows._match([(0, '15%')], '15') is None


def test_superscript_normalization_keeps_the_original_pdf_glyph_owner():
    from app.parsers.textlayer import build_text
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 100), 'Area', fontsize=10)
        page.insert_text((62, 95), '2', fontsize=6)
        page.insert_text((140, 100), 'Mode', fontsize=10)
        page.insert_text((220, 100), '12', fontsize=10)
        native = read_page(page, 0)
        native_ids = {id(c) for f in native.fragments for c in f.chars if c.c.strip()}
        text, _, chars = build_text(native.fragments, 0, native.width)
        assert '\u00b2' in text
        assert any(id(c) not in native_ids for c in chars if c.c.strip())
        found = PrintedRows([[['Area2', 'Mode', '12']]]).locate(native.fragments, native)
        assert found is not None
        owners = [id(c) for glyphs in found[2].glyphs.values() for c in glyphs]
        assert len(owners) == len(set(owners))
        assert set(owners) == native_ids


def test_unused_cell_matches_preserve_native_line_order_with_overlapping_fonts():
    from app.parsers.sheet_grids import _remaining_fragments
    from app.parsers.textlayer import Char, Fragment, build_text
    # A clipped trailing glyph overlaps the next cell's slightly raised text.
    # Both were one native PDF line before tentative grid ownership split it.
    first = [Char(c, .1 + i * .01, .10, .11 + i * .01, .12, 10, False)
             for i, c in enumerate('Alpha ')]
    second = [Char(c, .145 + i * .01, .097, .155 + i * .01, .117, 10, False)
              for i, c in enumerate('beta')]
    following = [Char(c, .1 + i * .01, .15, .11 + i * .01, .17, 10, False)
                 for i, c in enumerate('next')]
    records = [(1, Fragment(first, block=7), 0), (2, Fragment(second, block=7), 0),
               (None, Fragment(following, block=7), 1)]
    assert build_text([part for _, part, _ in records], 0, 100)[0] != 'Alpha beta next'
    restored = _remaining_fragments(records, set())
    assert build_text(restored, 0, 100)[0] == 'Alpha beta next'
    assert build_text(_remaining_fragments(records, {2}), 0, 100)[0] == 'Alpha next'
    assert [id(c) for f in restored for c in f.chars] == [id(c) for c in first + second + following]


def _aligned_wrap(tmp_path, *, ambiguous=False):
    source = [['First record', 'A101', '12'], ['Wrapped record label', 'B202', '34'],
              ['Final record', 'C303', '56']]
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=500, height=300)
        for ri, row in enumerate(source):
            for ci, text in enumerate(row):
                if ri == 1 and ci == 0:
                    continue
                page.insert_text(([40, 250, 350][ci], [80, 105, 130][ri]), text, fontsize=9)
        page.insert_text((40, 105), 'Wrapped record', fontsize=9)
        page.insert_text((40, 118), 'label', fontsize=9)
        if ambiguous:
            for x, text in [(250, 'B202'), (350, '34')]:
                page.insert_text((x, 118), text, fontsize=9)
        path = tmp_path / 'aligned-wrap.pdf'
        pdf.save(path)
        doc = ParsedDocument.model_validate({
            'document': {'id': 'd', 'filename': 'synthetic.xlsx', 'fileHash': 'h', 'pageCount': 1,
                         'sourceFormat': 'xlsx', 'parser': 'hybrid', 'parserVersion': 'test'},
            'pages': [{'index': 0, 'width': 500, 'height': 300}],
            'blocks': [{'id': 'b0', 'readingOrder': 0, 'type': 'paragraph', 'text': page.get_text(),
                        'regions': [{'pageIndex': 0, 'bbox': {'x0': 0, 'y0': 0, 'x1': 1, 'y1': 1}}],
                        'source': {'parser': 'test'}}], 'blocksFingerprint': 'f'})
    return source, path, doc


def test_top_aligned_wrapped_label_stays_with_its_original_numeric_record(tmp_path):
    source, path, doc = _aligned_wrap(tmp_path)
    assert recover_sheet_grids(doc, path, [source], set(), source_tables._printed_cells(path, doc)) > 0
    assert source[1] in _rows(doc)
    assert all('Wrapped record' not in b.text for b in doc.blocks if b.type != 'table')


def test_wrapped_label_does_not_choose_between_equal_top_and_bottom_records(tmp_path):
    source, path, _ = _aligned_wrap(tmp_path, ambiguous=True)
    with pymupdf.open(path) as pdf:
        native = read_page(pdf[0], 0)
        parts = PrintedRows([source])._wrapped_parts(native)
        assert {id(p) for p in parts} == {id(p) for p in native.fragments}


def test_a_rounded_number_cannot_lend_its_suffix_to_an_unprinted_column():
    source = [['Alpha pump', 'A101', '12.345', '5'],
              ['Beta valve', 'B202', '7.512', '1'],
              ['Gamma seal', 'C303', '3.14159', '4'],
              ['Epsilon cap', 'E505', '0.125', '3']]
    printed = [row[:2] + [value] for row, value in zip(source, ['12.35', '7.51', '3.14', '0.13'])]
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=500, height=300)
        for ri, row in enumerate(printed):
            for x, value in zip([40, 200, 300], row):
                page.insert_text((x, 80 + 18 * ri), value, fontsize=9)
        native = read_page(page, 0)
        grids = PrintedRows([source]).grids(native, [])
        assert len(grids) == 1
        assert grids[0].source_identity == (0, (0, 1, 2))
        assert [[''.join(ch.c for ch in grids[0].glyph_owners[box]) for box in row.cells]
                for row in grids[0].rows] == [[v.replace(' ', '') for v in row] for row in printed]


def test_rounding_requires_both_physical_edges():
    parts = [(0, '12.345'), (1, '5')]
    assert PrintedRows._match(parts, '12.35', {0, 5})[1] == {((0, 0, 5),)}
    assert PrintedRows._match(parts, '12.35') is None
    assert PrintedRows._match([(0, '1'), (1, '2.345')], '12.35', {0, 5}) is None
    # Separate printed cells may still contain a rounded value beside its suffix.
    assert PrintedRows._match(parts, '12.35', {0, 4, 5})[1] == {((0, 0, 4), (1, 4, 5))}
