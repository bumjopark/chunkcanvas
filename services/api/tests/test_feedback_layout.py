import pytest


def parse(path):
    from app.parsers.pymupdf_parser import parse
    return parse(path,file_meta={'id':'layout-review','filename':'synthetic.pdf','fileHash':'h'},progress=lambda *args:None)


def test_removed_overprint_does_not_return_through_rotated_text_fallback(tmp_path):
    import pymupdf
    pdf=pymupdf.open();p=pdf.new_page()
    p.insert_text((72,100),'Introduction to the system')
    p.insert_text((72,400),'Shadowed Title Words')
    p.insert_text((72,450),'Another normal line of body text after the title.')
    p.insert_text((72.6,400.6),'Shadowed Title Words')
    path=tmp_path/'shadow.pdf';pdf.save(path);pdf.close()
    doc=parse(path)
    assert ' '.join(b.text for b in doc.blocks).count('Shadowed Title Words')==1


def test_slide_shadow_at_six_points_is_read_once_without_losing_other_occurrences(tmp_path):
    import pymupdf
    pdf = pymupdf.open(); page = pdf.new_page(width=720, height=540)
    for origin in ((26, 278), (20, 272), (350, 400)):
        page.insert_text(origin, 'Transportation', fontsize=20)
    path = tmp_path / 'large-shadow.pdf'; pdf.save(path); pdf.close()
    doc = parse(path)
    assert ' '.join(b.text for b in doc.blocks).count('Transportation') == 2


def test_nearby_repeated_labels_with_different_glyph_sizes_are_kept():
    from app.parsers.textlayer import Char, Fragment, _without_repeats
    def line(size, dx):
        return Fragment([Char(c, (20 + dx + i * size * .5) / 720, .3,
            (20 + dx + (i + 1) * size * .5) / 720, .35, size, False)
            for i, c in enumerate('Retail')])
    assert len(_without_repeats([line(20, 0), line(16, 6)], 720, 540)) == 2


def test_close_overprint_with_a_different_font_keeps_legacy_deduplication(tmp_path):
    import pymupdf
    pdf = pymupdf.open(); page = pdf.new_page()
    page.insert_text((72, 100), 'IOWN1.0', fontsize=12)
    page.insert_text((72.4, 100), 'IOWN1.0', fontsize=15)
    page.insert_text((72, 160), 'IOWN1.0', fontsize=12)
    path = tmp_path / 'overprinted-label.pdf'; pdf.save(path); pdf.close()
    doc = parse(path)
    assert ' '.join(b.text for b in doc.blocks).count('IOWN1.0') == 2


def test_vertical_stamp_stays_separate_from_horizontal_body(tmp_path):
    import pymupdf
    pdf=pymupdf.open();p=pdf.new_page()
    p.insert_text((50,360),'arXiv vertical stamp',rotate=90)
    p.insert_text((72,400),'the following body paragraph remains editable.')
    path=tmp_path/'stamp.pdf';pdf.save(path);pdf.close()
    doc=parse(path)
    stamp=next(b for b in doc.blocks if 'arXiv' in b.text)
    body=next(b for b in doc.blocks if 'following body' in b.text)
    assert stamp is not body
    assert stamp.line_geometry=='unavailable' and not stamp.lines
    assert body.lines and body.line_geometry=='exact'


def test_vertical_edge_bands_are_furniture_but_repeated_body_illustrations_are_kept():
    from app.parsers.hybrid_parser import _repeated_image_decorations
    pages={p:[([0,.02,.08,.98],'band'),([.35,.3,.65,.65],'body')] for p in range(4)}
    assert _repeated_image_decorations(pages,{'band':set(range(4)),'body':set(range(4))})=={'band'}


def test_an_exact_geometry_label_cannot_survive_losing_its_lines():
    from app.parsers.base import finish
    doc=finish({'id':'layout-review','filename':'x.pdf','fileHash':'h','parser':'pymupdf','parserVersion':'test','pageCount':1},
        [{'index':0,'width':600,'height':800}],[{'type':'paragraph','text':'A paragraph.',
        'regions':[{'pageIndex':0,'bbox':{'x0':.1,'y0':.2,'x1':.9,'y1':.3}}],
        'lines':None,'lineGeometry':'exact','source':{'parser':'pymupdf'}}])
    assert doc.blocks[0].line_geometry=='unavailable'


def test_rotated_overprint_is_read_once_but_distinct_stamps_remain(tmp_path):
    import pymupdf
    pdf=pymupdf.open();page=pdf.new_page()
    for x,y in [(50,360),(50.3,360.3),(150,360)]:
        page.insert_text((x,y),'VERTICAL STAMP',fontsize=12,rotate=90)
    page.insert_text((72,450),'Horizontal body remains.')
    path=tmp_path/'rotated-overprint.pdf';pdf.save(path);pdf.close()
    doc=parse(path)
    assert ' '.join(b.text for b in doc.blocks).count('VERTICAL STAMP') == 2
    assert any(b.text == 'Horizontal body remains.' for b in doc.blocks)


def test_cover_logo_remains_content_while_mirrored_corner_copies_are_furniture():
    from types import SimpleNamespace
    from app.parsers.hybrid_parser import _pictures_of_repeated_images
    from test_feedback_labels import block
    rects=[(.2,.2,.8,.8),(.05,.05,.15,.15),(.075,.05,.175,.15),(.05,.05,.15,.15)]
    pages=[SimpleNamespace(rect=SimpleNamespace(width=600,height=800),get_image_info=lambda xrefs=True,r=r:[
        {'bbox':tuple(v*(600 if i%2==0 else 800) for i,v in enumerate(r)), 'xref':1}]) for r in rects]
    blocks=[block('picture','',r) for r in rects]
    for i,b in enumerate(blocks): b['regions'][0]['pageIndex']=i
    _pictures_of_repeated_images(blocks,pages)
    assert [b['type'] for b in blocks] == ['picture','page_header','page_header','page_header']
