"""A PDF's hidden old values must not become visible form/table content."""
import pymupdf
import pytest

from app.parsers.textlayer import build_text, read_page
from app.parsers.visible_text import read_raw


def content(page):
    pt = read_page(page, 0)
    return build_text(pt.fragments, 0, pt.width)[0]


def test_opaque_cover_removes_old_value_but_retains_new_and_distinct_copies():
    with pymupdf.open() as pdf:
        p = pdf.new_page()
        p.insert_text((50, 70), 'OLD 100')
        p.insert_text((50, 120), 'OLD 100')
        p.draw_rect(pymupdf.Rect(40, 50, 150, 80), color=None, fill=(1, 1, 1))
        p.insert_text((50, 70), 'NEW 250')
        text = content(p)
        assert text.count('OLD 100') == 1
        assert text.count('NEW 250') == 1


@pytest.mark.parametrize('kind', ['transparent', 'outline', 'partial', 'ellipse'])
def test_uncertain_cover_does_not_delete_native_text(kind):
    with pymupdf.open() as pdf:
        p = pdf.new_page()
        p.insert_text((50, 70), 'Readable 250')
        box = pymupdf.Rect(40, 50, 160, 80)
        if kind == 'transparent':
            p.draw_rect(box, color=None, fill=(1, 1, 0), fill_opacity=.3)
        elif kind == 'outline':
            p.draw_rect(box, color=(0, 0, 0))
        elif kind == 'ellipse':
            p.draw_oval(box, color=None, fill=(1, 1, 1))
        else:
            p.draw_rect(pymupdf.Rect(40, 50, 160, 64), color=None, fill=(1, 1, 1))
        assert 'Readable 250' in content(p)


def test_five_segment_rect_and_identical_repaint():
    with pymupdf.open() as pdf:
        p = pdf.new_page()
        p.insert_text((50, 70), 'SAME')
        p.insert_text((50, 120), 'STALE')
        shape = p.new_shape()
        shape.draw_polyline([(95, 130), (40, 130), (40, 50), (150, 50), (150, 130), (95, 130)])
        shape.finish(fill=(1, 1, 1), color=None)
        shape.commit()
        p.insert_text((50, 70), 'SAME')
        text = content(p)
        assert text.count('SAME') == 1
        assert 'STALE' not in text


def clipped_document(path):
    with pymupdf.open() as pdf:
        p = pdf.new_page(width=300, height=300)
        p.insert_text((50, 70), 'CLIPPED OLD 100')
        stream = p.get_contents()[-1]
        # Clip to the bottom-left corner, excluding the earlier text entirely.
        pdf.update_stream(stream, b'q 0 0 10 10 re W n\n' + pdf.xref_stream(stream) + b'\nQ')
        p.insert_text((50, 70), 'VISIBLE 250')
        p.insert_text((50, 120), 'COVERED 900')
        p.draw_rect(pymupdf.Rect(40, 100, 180, 130), color=None, fill=(1, 1, 1))
        pdf.save(path)


def test_clip_and_opaque_cover_are_applied_before_docling_layout(tmp_path):
    from app.parsers.visible_backend import backend_for
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.document import InputDocument

    path = tmp_path / 'hidden.pdf'
    clipped_document(path)
    doc = InputDocument(path_or_stream=path, format=InputFormat.PDF, backend=backend_for(path))
    try:
        page = next(doc._backend.iter_pages())
        native = ' '.join(c.text for c in page.get_text_cells())
        assert native == 'VISIBLE 250'
        assert ' '.join(c.text for c in page.get_segmented_page().word_cells) == native
        assert ' '.join(c.text for c in page.get_visible_text_cells()) == native
        # OCR still receives the real rendered page, never the stale native cells.
        assert page.get_page_image().size == (300, 300)
    finally:
        doc._backend.unload()
    with pymupdf.open(path) as pdf:
        assert content(pdf[0]) == 'VISIBLE 250'


def test_invisible_searchable_ocr_text_is_not_discarded():
    with pymupdf.open() as pdf:
        p = pdf.new_page()
        p.insert_text((50, 70), 'SEARCHABLE OCR', render_mode=3)
        raw, changed = read_raw(p, pymupdf.TEXTFLAGS_RAWDICT, detect_clipping=True)
        assert not changed
        assert 'SEARCHABLE OCR' in content(p)


def test_clipped_fill_does_not_hide_text_outside_its_clip():
    with pymupdf.open() as pdf:
        p = pdf.new_page(width=300, height=300)
        p.insert_text((50, 70), 'VISIBLE 250')
        p.draw_rect(pymupdf.Rect(40, 50, 180, 80), color=None, fill=(1, 1, 1))
        stream = p.get_contents()[-1]
        pdf.update_stream(stream, b'q 0 0 10 10 re W n\n' + pdf.xref_stream(stream) + b'\nQ')
        assert content(p) == 'VISIBLE 250'


def test_pymupdf_fallback_does_not_reintroduce_hidden_table_values(tmp_path):
    from app.parsers.pymupdf_parser import extract
    path = tmp_path / 'hidden.pdf'
    clipped_document(path)
    _, blocks = extract(path, progress=lambda *_: None)
    assert '\n'.join(b['text'] for b in blocks) == 'VISIBLE 250'


def test_repainted_word_has_one_layout_owner_but_distinct_copy_remains(tmp_path):
    from app.parsers.visible_backend import backend_for
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.document import InputDocument

    path = tmp_path / 'repaint.pdf'
    with pymupdf.open() as pdf:
        p = pdf.new_page()
        p.insert_text((50, 70), 'SAME')
        p.insert_text((50, 120), 'STALE')
        p.draw_rect(pymupdf.Rect(40, 50, 150, 130), color=None, fill=(1, 1, 1))
        p.insert_text((50, 70), 'SAME')
        p.insert_text((50, 170), 'SAME')
        pdf.save(path)
    doc = InputDocument(path_or_stream=path, format=InputFormat.PDF, backend=backend_for(path))
    try:
        cells = list(next(doc._backend.iter_pages()).get_text_cells())
        assert [c.text for c in cells] == ['SAME', 'SAME']
        assert cells[0].rect != cells[1].rect
    finally:
        doc._backend.unload()


def test_dense_form_lines_remain_intact_for_layout(tmp_path):
    from app.parsers.visible_backend import backend_for
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.document import InputDocument

    path = tmp_path / 'form.pdf'
    texts = ['Immediately available at current price', 'Not available at current price']
    with pymupdf.open() as pdf:
        p = pdf.new_page()
        p.insert_text((50, 40), 'HIDDEN')
        p.draw_rect(pymupdf.Rect(40, 20, 150, 50), color=None, fill=(1, 1, 1))
        for i, text in enumerate(texts):
            p.insert_text((50, 70 + i * 8), text, fontsize=6)
        pdf.save(path)
    doc = InputDocument(path_or_stream=path, format=InputFormat.PDF, backend=backend_for(path))
    try:
        page = next(doc._backend.iter_pages())
        assert [c.text for c in page.get_text_cells()] == texts
        assert [c.text for c in page.get_segmented_page().word_cells] == ' '.join(texts).split()
    finally:
        doc._backend.unload()


def test_wide_heading_above_both_columns_precedes_form_content_only_on_filtered_pages():
    from app.parsers.docling_parser import _order_visible_page_headings

    def block(text, kind, x0, y0, x1, y1):
        return {'text': text, 'type': kind, 'regions': [{'pageIndex': 0,
            'bbox': dict(x0=x0, y0=y0, x1=x1, y1=y1)}]}
    left = block('Left body', 'paragraph', .05, .2, .45, .8)
    right = block('Right body', 'paragraph', .55, .2, .95, .8)
    title = block('Document title', 'title', .1, .02, .8, .08)
    local = block('Local heading', 'section_header', .55, .12, .85, .16)
    blocks = [left, title, local, right]
    _order_visible_page_headings(blocks, set())
    assert blocks == [left, title, local, right]
    _order_visible_page_headings(blocks, {0})
    assert blocks == [title, left, local, right]
    # A heading below earlier body text is not a page title, even when wide.
    title['regions'][0]['bbox']['y0'] = .11
    title['regions'][0]['bbox']['y1'] = .14
    left['regions'][0]['bbox']['y0'] = .1
    blocks = [left, title, right]
    _order_visible_page_headings(blocks, {0})
    assert blocks == [left, title, right]


@pytest.mark.parametrize(('clip', 'second_clip', 'hidden'), [
    # Counterclockwise and clockwise, with a split bottom edge.
    ('150 60 m 240 60 l 240 180 l 40 180 l 40 60 l 150 60 l h', '', True),
    ('150 60 m 40 60 l 40 180 l 240 180 l 240 60 l 150 60 l h', '', True),
    # The right-hand text is outside the inner clip despite being inside its parent.
    ('150 60 m 240 60 l 240 180 l 40 180 l 40 60 l 150 60 l h', '40 60 70 120 re W n', False),
    # Concave clip: the bounding box would incorrectly hide the right-hand text.
    ('150 60 m 240 60 l 240 90 l 110 90 l 110 180 l 40 180 l 40 60 l 150 60 l h', '', False),
])
def test_line_clip_limits_later_opaque_paint(clip, second_clip, hidden):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=300, height=300)
        page.insert_text((55, 70), 'Outside clip')
        page.insert_text((55, 150), 'Inside above paint')
        page.insert_text((55, 190), 'Left value')
        page.insert_text((150, 190), 'Right value')
        stream = page.get_contents()[-1]
        paint = f'\nq {clip} W n {second_clip} 0 g 40 95 200 40 re f Q\n'
        pdf.update_stream(stream, pdf.xref_stream(stream) + paint.encode('ascii'))
        text = content(page)
        assert 'Outside clip' in text and 'Inside above paint' in text
        assert ('Right value' not in text) == hidden
        if second_clip:
            assert 'Left value' not in text
