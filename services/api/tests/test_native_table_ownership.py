"""Native cell geometry must not duplicate, scramble, or discard visible glyphs."""
from types import SimpleNamespace

import pymupdf
import pytest

from app.parsers.pymupdf_parser import extract, grounded_table, physical_table_rows, visible_table_rows
from app.parsers.textlayer import read_page


def grid(rows):
    cells = [cell for row in rows for cell in row if cell]
    return SimpleNamespace(rows=[SimpleNamespace(cells=row) for row in rows],
                           bbox=(min(c[0] for c in cells), min(c[1] for c in cells),
                                 max(c[2] for c in cells), max(c[3] for c in cells)))


@pytest.mark.parametrize('physical', [False, True])
def test_unfiltered_native_table_uses_the_same_overprint_rules_as_body(tmp_path, physical):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=400)
        for x in (40, 170, 300):
            page.draw_line((x, 100), (x, 190))
        for y in (100, 130, 160, 190):
            page.draw_line((40, y), (300, y))
        for y, row in zip((120, 150, 180), [('Name', 'Count'), ('Apple', '10'), ('Pear', '20')]):
            for x, value in zip((50, 180), row):
                page.insert_text((x, y), value, fontsize=10)
                page.insert_text((x + .15, y + .1), value, fontsize=10)
        assert not read_page(page, 0).visibility_filtered
        path = tmp_path / 'overprinted-table.pdf'
        pdf.save(path)
    _, blocks = extract(path, progress=lambda *_: None, physical_tables=physical)
    tables = [b['text'] for b in blocks if b['type'] == 'table']
    assert len(tables) == 1
    records = [[cell.strip() for cell in line.split('|') if cell.strip()]
               for line in tables[0].splitlines() if '---' not in line]
    assert records == [['Name', 'Count'], ['Apple', '10'], ['Pear', '20']]


@pytest.mark.parametrize('rotated', [False, True])
def test_overlapping_outer_cell_does_not_repeat_inner_grid_values(rotated):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=400)
        page.insert_text((50, 75), 'Note', fontsize=10)
        page.insert_text((70, 135), 'ALPHA', fontsize=10, rotate=90 if rotated else 0)
        page.insert_text((210, 135), '10', fontsize=10)
        page.insert_text((50, 170), 'BETA', fontsize=10)
        page.insert_text((210, 170), '20', fontsize=10)
        table = grid([[(40, 50, 300, 190), None],
                      [(40, 90, 170, 150), (170, 90, 300, 150)],
                      [(40, 150, 170, 190), (170, 150, 300, 190)]])
        assert visible_table_rows(table, read_page(page, 0), page) == [
            ['Note', ''], ['ALPHA', '10'], ['BETA', '20']]


@pytest.mark.parametrize('populated,lower_rows,repair', [(False, 2, True), (True, 2, False), (False, 1, False)])
def test_filter_inset_needs_empty_geometry_and_repeated_column(populated, lower_rows, repair):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=400)
        for x, word in ((50, 'Label'), (160, 'Date'), (270, 'Amount')):
            page.insert_text((x, 125), word, fontsize=10)
        if populated:
            page.insert_text((152, 107), 'X', fontsize=5)
        inset = (150, 90, 165, 110)
        rows = [[(40, 90, 150, 140), inset, (260, 90, 370, 140)]]
        for i in range(lower_rows):
            y = 140 + 30*i
            rows.append([(40, y, 150, y + 30), (150, y, 260, y + 30), (260, y, 370, y + 30)])
            page.insert_text((50, y + 20), f'Item{i}', fontsize=10)
            page.insert_text((270, y + 20), str(i + 10), fontsize=10)
        table = grid(rows)
        pt = read_page(page, 0)
        assert physical_table_rows(table, pt)[0][1] == ((150, 90, 260, 140) if repair else inset)
        if repair:
            assert visible_table_rows(table, pt, page) == [
                ['Label', 'Date', 'Amount'], ['Item0', '', '10'], ['Item1', '', '11']]


@pytest.mark.parametrize('rotated', [False, True])
def test_text_in_a_table_box_but_outside_its_cells_stays_in_body(tmp_path, monkeypatch, rotated):
    # A rectangular table outline can contain an undetected cell. Its outline
    # is not evidence that every enclosed glyph entered the table.
    table = grid([[(40, 90, 150, 140), None, (260, 90, 370, 140)],
                  [(40, 140, 150, 170), (150, 140, 260, 170), (260, 140, 370, 170)]])
    table.extract = lambda: [['Label', '', 'Amount'], ['Item', '', '10']]
    monkeypatch.setattr(pymupdf.Page, 'find_tables', lambda page, **kwargs: SimpleNamespace(tables=[table]))
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=400)
        for x, y, value in [(50, 120, 'Label'), (160, 120, 'RetainMe'), (270, 120, 'Amount'),
                            (50, 160, 'Item'), (270, 160, '10')]:
            page.insert_text((x, y), value, fontsize=10, rotate=90 if rotated and value == 'RetainMe' else 0)
        path = tmp_path / 'missing-cell.pdf'
        pdf.save(path)
    _, blocks = extract(path, progress=lambda *_: None)
    assert any(b['type'] != 'table' and b['text'] == 'RetainMe' for b in blocks)
    all_text = '\n'.join(b['text'] for b in blocks)
    for value in ('Label', 'RetainMe', 'Amount', 'Item', '10'):
        assert all_text.count(value) == 1


def test_blank_drawn_grid_does_not_invent_a_text_table(tmp_path):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=400)
        for x in (40, 170, 300):
            page.draw_line((x, 100), (x, 190))
        for y in (100, 130, 160, 190):
            page.draw_line((40, y), (300, y))
        path = tmp_path / 'blank-grid.pdf'
        pdf.save(path)
    _, blocks = extract(path, progress=lambda *_: None, physical_tables=True)
    assert not any(b['type'] == 'table' for b in blocks)


@pytest.mark.parametrize('shape', ['false_split', 'populated_columns', 'coarse_outline', 'neighbour_note'])
def test_only_physically_disproven_complementary_columns_replace_layout(shape):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=400)
        for x, y, word in [(110, 120, 'Name'), (170, 120, 'Kind'), (270, 120, 'Count'),
                            (50, 150, 'Apple'), (170, 150, 'Fruit'), (270, 150, '10'),
                            (50, 180, 'Pear'), (170, 180, 'Fruit'), (270, 180, '20')]:
            page.insert_text((x, y), word, fontsize=10)
        if shape == 'populated_columns':
            page.insert_text((110, 150), 'Separate', fontsize=8)
        model = grid([[(a, y, b, y + 30) for a, b in zip((40, 90, 160, 260), (90, 160, 260, 370))]
                      for y in (100, 130, 160)])
        native = grid([[(a, y, b, y + 30) for a, b in zip((40, 160, 260), (160, 260, 370))]
                       for y in (100, 130, 160)])
        if shape == 'neighbour_note':
            page.insert_text((375, 150), 'Note', fontsize=5)
            for row in native.rows:
                x0, y0, _, y1 = row.cells[-1]
                row.cells[-1] = (x0, y0, 400, y1)
            native.bbox = (40, 100, 400, 190)
        if shape == 'coarse_outline':
            native = grid([[(40, 100, 260, 190), (260, 100, 370, 190)]])
        pt = read_page(page, 0)
        selected = grounded_table(model, [native], pt, page)
        if shape in ('false_split', 'neighbour_note'):
            assert visible_table_rows(selected, pt, page) == [
                ['Name', 'Kind', 'Count'], ['Apple', 'Fruit', '10'], ['Pear', 'Fruit', '20']]
            assert selected.bbox == model.bbox
        else:
            assert selected is model
