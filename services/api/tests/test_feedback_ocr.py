from types import SimpleNamespace
import pytest
from app.parsers.docling_parser import _distinct_reads


def cell(text, box, confidence):
    return SimpleNamespace(text=text, confidence=confidence,
        rect=SimpleNamespace(to_bounding_box=lambda: SimpleNamespace(l=box[0],t=box[1],r=box[2],b=box[3])))


@pytest.mark.parametrize('confidence',[(.97,.99),(.99,.97)])
def test_same_printed_line_with_nested_detection_boxes_is_read_once(confidence):
    a=cell('the variance is',(59,448,121,467),confidence[0])
    b=cell('the variance is',(62,454,119,464),confidence[1])
    real_repeat=cell('the variance is',(60,165,121,180),.98)
    out=_distinct_reads([real_repeat,a,b])
    assert len(out)==2 and real_repeat in out


def test_adjacent_repeated_lines_and_additional_words_are_preserved():
    cells=[cell('the variance is',(60,448,121,467),.99),
           cell('the variance is',(60,466,121,480),.98),
           cell('a separate statement',(180,448,290,467),.97)]
    assert _distinct_reads(cells)==cells


def test_native_line_and_displaced_ocr_echo_have_one_owner_but_real_copies_survive():
    from app.parsers.hybrid_parser import _without_native_ocr_twins
    from test_feedback_labels import block
    native = block('paragraph', 'DEAL NAME:', (.05, .16, .105, .179), _chars=[SimpleNamespace(c=c) for c in 'DEAL NAME:'])
    echo = block('paragraph', 'DEAL NAME:', (.106, .161, .16, .180))
    another = block('paragraph', 'DEAL NAME:', (.106, .3, .16, .319))
    page = SimpleNamespace(rect=SimpleNamespace(width=600, height=800), get_image_info=lambda: [])
    assert _without_native_ocr_twins([native, echo, another], [page]) == [native, another]
    echo['_chars'] = [SimpleNamespace(c=c) for c in 'DEAL NAME:']
    assert _without_native_ocr_twins([native, echo], [page]) == [native, echo]
    del echo['_chars']
    page.get_image_info = lambda: [{'bbox': (60, 120, 100, 150)}]
    assert _without_native_ocr_twins([native, echo], [page]) == [native, echo]


def test_native_ocr_echo_tolerates_terminal_dash_but_not_a_changed_amount():
    from app.parsers.hybrid_parser import _without_native_ocr_twins
    from test_feedback_labels import block
    page = SimpleNamespace(rect=SimpleNamespace(width=600, height=800), get_image_info=lambda: [])
    native = block('paragraph', 'Feb. 27, 1999 –', (.4, .24, .47, .257), _chars=[SimpleNamespace(c=c) for c in 'DEAL NAME:'])
    echo = block('paragraph', 'Feb. 27, 1999 -', (.472, .241, .53, .258))
    assert _without_native_ocr_twins([native, echo], [page]) == [native]
    native['text'] = 'Amount -100'
    echo['text'] = 'Amount 100'
    assert _without_native_ocr_twins([native, echo], [page]) == [native, echo]


def test_removing_an_ocr_echo_keeps_its_real_terminal_glyph():
    from app.parsers.hybrid_parser import _without_native_ocr_twins
    from app.parsers.textlayer import Char
    from test_feedback_labels import block
    text = 'Feb. 27, 1999'
    chars = [Char(c, .4+i*.005, .24, .405+i*.005, .257, 10, False) for i,c in enumerate(text)]
    dash = Char('—', .472, .24, .48, .257, 10, False)
    native = block('paragraph', text, (.4,.24,.47,.257), _chars=chars)
    echo = block('paragraph', text+' -', (.472,.241,.53,.258), _chars=[dash])
    page = SimpleNamespace(rect=SimpleNamespace(width=600,height=800), get_image_info=lambda: [])
    out = _without_native_ocr_twins([native,echo],[page])
    assert len(out) == 1 and out[0]['text'].count('Feb.') == 1 and '—' in out[0]['text']
    assert dash in out[0]['_chars'] and out[0]['lines']


def test_table_cell_reference_repeats_are_distinct_from_independent_equal_cells():
    from app.parsers.docling_parser import _table_text
    def value(text, row, col):
        return SimpleNamespace(text=text, start_row_offset_idx=row, start_col_offset_idx=col)
    shared_code, shared_amount = value('SER-000', 1, 1), value('4', 1, 2)
    item = SimpleNamespace(data=SimpleNamespace(grid=[
        [value('Name', 0, 0), value('Code', 0, 1), value('Amount', 0, 2)],
        [value('Service', 1, 0), shared_code, shared_amount],
        [value('Data', 2, 0), shared_code, shared_amount],
        [value('Independent', 3, 0), value('SER-000', 3, 1), value('4', 3, 2)],
    ]))
    full, once = _table_text(item), _table_text(item, once=True)
    assert full.count('SER-000') == 3
    assert once.count('SER-000') == 2
    assert '| Data |  |  |' in once
    assert '| Independent | SER-000 | 4 |' in once


def test_overlapping_table_does_not_claim_separately_owned_logo_ocr():
    from copy import deepcopy
    from app.parsers.docling_parser import _keep_table_owned_ocr
    from test_feedback_labels import block
    table = block('table', '| Title | Result |', (.05, .05, .95, .5))
    logo = block('picture', 'Peace\nand prosperity', (.1, .1, .3, .2))
    formula = (0, '2 x 5 = 10', (.6, .3, .8, .32))
    table['_table_ocr_cells'] = [
        (0, 'Peace', (.11, .11, .2, .13)),
        (0, 'and prosperity', (.11, .15, .29, .18)), formula]
    original = deepcopy(logo)
    _keep_table_owned_ocr([table, logo])
    assert table['_table_ocr_cells'] == [formula]
    assert logo == original and table['text'] == '| Title | Result |'


@pytest.mark.parametrize('other', ['distant', 'another-page', 'different-text', 'unread', 'partial-overlap'])
def test_table_ocr_needs_matching_text_and_position_to_have_another_owner(other):
    from app.parsers.docling_parser import _keep_table_owned_ocr
    from test_feedback_labels import block
    cell = (0, 'Result -100', (.1, .1, .3, .2))
    table = block('table', '| Value | Result -100 |', (.05, .05, .95, .5), _table_ocr_cells=[cell])
    owner = block('picture', 'Result -100', (.1, .1, .3, .2))
    if other == 'distant': owner['regions'][0]['bbox']['y0'] = .4
    if other == 'another-page': owner['regions'][0]['pageIndex'] = 1
    if other == 'different-text': owner['text'] = 'Result 100'
    if other == 'unread': owner['text'] = ''
    if other == 'partial-overlap': owner['regions'][0]['bbox']['x1'] = .2
    _keep_table_owned_ocr([table, owner])
    assert table['_table_ocr_cells'] == [cell]
