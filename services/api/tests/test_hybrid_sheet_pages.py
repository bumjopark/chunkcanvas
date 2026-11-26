"""Worksheet pages that Docling reads as a heap of one-word paragraphs use the PyMuPDF grid."""
from app.parsers import hybrid_parser as h


def _block(kind, text, page=0, y=.1):
    return {'type': kind, 'text': text, 'regions': [{'pageIndex': page, 'bbox': {'x0': .1, 'y0': y, 'x1': .9, 'y1': y + .01}}],
            'source': {'parser': 'docling'}}


def test_one_word_heaps_without_tables_are_candidates():
    heap = [_block('paragraph', f'v{i}', 0, .01 * i) for i in range(30)]
    tabled = [_block('table', '| a | b |\n| --- | --- |\n| 1 | 2 |', 1)] + [_block('paragraph', f'w{i}', 1) for i in range(30)]
    prose = [_block('paragraph', 'A sentence with several words in it.', 2) for _ in range(30)]
    assert h._paragraph_soup_pages(heap + tabled + prose) == {0}


def test_grid_page_must_keep_the_text_and_have_a_table():
    ours = [_block('paragraph', f'value{i}') for i in range(30)]
    grid = [_block('table', '| ' + ' | '.join(f'value{i}' for i in range(30)) + ' |')]
    assert h._grid_reads_better(ours, grid, 0)
    assert not h._grid_reads_better(ours, [_block('table', '| value1 |')], 0)          # loses text
    assert not h._grid_reads_better(ours, [_block('paragraph', b['text']) for b in ours], 0)  # no table


def test_replacing_page_text_keeps_docling_pictures_and_other_pages():
    blocks = [_block('paragraph', 'p0'), _block('picture', 'chart text', 0), _block('paragraph', 'x', 0), _block('paragraph', 'next', 1)]
    fast = [_block('table', '| p0 | x |'), _block('picture', '', 0), _block('paragraph', 'other', 1)]
    out = h._replace_page_text(blocks, fast, {0})
    assert [(b['type'], b['text']) for b in out] == [('table', '| p0 | x |'), ('picture', 'chart text'), ('paragraph', 'next')]
    assert out[0]['source']['ref'] == 'sheet-grid-page'


def test_swapped_page_keeps_docling_role_for_a_repeated_sheet_title():
    blocks = [_block('section_header', 'Quarterly volumes', 0, .04), *[_block('paragraph', f'v{i}', 0) for i in range(30)]]
    fast = [_block('page_header', 'Quarterly volumes', 0, .04), _block('table', '| v0 | v1 |', 0)]
    out = h._replace_page_text(blocks, fast, {0})
    assert [(b['type'], b['text']) for b in out] == [('section_header', 'Quarterly volumes'), ('table', '| v0 | v1 |')]


def _box(kind, text, page, x0, y0, x1, y1):
    return {'type': kind, 'text': text, 'regions': [{'pageIndex': page, 'bbox': {'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1}}],
            'source': {'parser': 'docling'}}


def test_table_syntax_is_not_counted_as_kept_text():
    # Eight columns add 24 separator dashes; they must not hide that 16 of 24 values are gone.
    ours = [_block('paragraph', str(i % 10), 0, .01 * i) for i in range(24)]
    grid = [_block('table', '| ' + ' | '.join(str(i) for i in range(8)) + ' |\n|' + ' --- |' * 8)]
    assert not h._grid_reads_better(ours, grid, 0)


def test_labels_a_kept_picture_holds_are_not_added_again():
    chart = _box('picture', 'Sales Jan Feb Mar', 0, .1, .5, .9, .8)
    blocks = [*[_block('paragraph', f'v{i}', 0, .01 * i) for i in range(30)], chart]
    fast = [_box('table', '| v0 | v1 |\n| --- | --- |', 0, .1, .05, .9, .3),
            _box('paragraph', 'Sales', 0, .4, .52, .6, .54), _box('paragraph', 'Jan Feb Mar', 0, .2, .75, .8, .77),
            _box('paragraph', 'Notes below', 0, .1, .85, .5, .87)]
    out = h._replace_page_text(blocks, fast, {0})
    assert [(b['type'], b['text']) for b in out] == [
        ('table', '| v0 | v1 |\n| --- | --- |'), ('paragraph', 'Notes below'), ('picture', 'Sales Jan Feb Mar')]


def test_a_page_with_a_block_reaching_another_page_keeps_docling():
    heap = [_block('paragraph', f'v{i}', 0, .01 * i) for i in range(30)]
    spread = {'type': 'paragraph', 'text': 'Total carried over', 'source': {'parser': 'docling'},
              'regions': [{'pageIndex': 0, 'bbox': {'x0': .1, 'y0': .9, 'x1': .9, 'y1': .95}},
                          {'pageIndex': 1, 'bbox': {'x0': .1, 'y0': .05, 'x1': .9, 'y1': .1}}]}
    grid = [_block('table', '| ' + ' | '.join(f'v{i}' for i in range(30)) + ' |\n|' + ' --- |' * 30 + '\n| Total carried over |')]
    assert h._grid_reads_better(heap, grid, 0)
    assert not h._grid_reads_better([*heap, spread], grid, 0)
