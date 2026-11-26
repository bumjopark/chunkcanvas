"""A glyph split off a side running head goes with that head, not into the body text."""
from app.parsers.margin_furniture import mark_margin_furniture


def _block(kind, text, x0, y0, x1, y1, page=10):
    return {'type': kind, 'level': None, 'text': text, 'source': {'parser': 'test'},
            'regions': [{'pageIndex': page, 'bbox': {'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1}}]}


def test_a_lone_glyph_touching_a_side_head_in_the_same_margin_joins_it():
    head = _block('page_header', '\u7b2c 1', .022, .191, .046, .220)
    glyph = _block('paragraph', '\u7ae0', .022, .219, .040, .232)
    body = _block('paragraph', 'Body text set across the page, continuing from the page before', .105, .091, .891, .344)
    blocks = [head, glyph, body]
    mark_margin_furniture(blocks)
    assert [b['type'] for b in blocks] == ['page_header', 'page_header', 'paragraph']


def test_glyphs_away_from_a_head_or_longer_text_stay_paragraphs():
    head = _block('page_header', '\u7b2c 1', .022, .191, .046, .220)
    far = _block('paragraph', 'I', .024, .600, .038, .612)                 # same margin, far below
    longer = _block('paragraph', 'Note', .022, .221, .040, .234)           # touches, but four letters
    other_side = _block('paragraph', 'I', .960, .221, .976, .234)         # opposite margin
    blocks = [head, far, longer, other_side]
    mark_margin_furniture(blocks)
    assert [b['type'] for b in blocks] == ['page_header', 'paragraph', 'paragraph', 'paragraph']
