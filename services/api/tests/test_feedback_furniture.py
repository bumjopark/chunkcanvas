from pathlib import Path
import zipfile
from app.parsers.source_furniture import docx_templates, mark_source_furniture
from app.parsers.base import finish, _repeated_running_text


def test_wordml_section_parts_mark_running_text_but_preserve_same_words_in_body(tmp_path):
    from app.parsers.source_furniture import WORDML, wordml_templates
    path = tmp_path / 'word.xml'
    path.write_text(f'''<w:wordDocument xmlns:w="{WORDML}"><w:body>
      <w:p><w:r><w:t>Odd page footer</w:t></w:r></w:p>
      <w:sectPr>
        <w:hdr w:type="first"><w:p><w:r><w:t>First page header</w:t></w:r></w:p></w:hdr>
        <w:ftr w:type="odd"><w:p><w:r><w:t>Odd page footer</w:t></w:r></w:p></w:ftr>
        <w:ftr w:type="even"><w:p><w:r><w:t>Even page footer</w:t></w:r></w:p></w:ftr>
      </w:sectPr></w:body></w:wordDocument>''')
    templates = wordml_templates(path)
    assert [(t['kind'], t['text']) for t in templates] == [
        ('header', 'First page header'), ('footer', 'Odd page footer'), ('footer', 'Even page footer')]
    first = block('First page header', 0, .06); first['type'] = 'caption'
    body = block('Odd page footer', 0, .4)
    odd = block('Odd page footer', 0, .94)
    even = block('Even page footer', 1, .94)
    mark_source_furniture([first, body, odd, even], templates)
    assert [b['type'] for b in [first, body, odd, even]] == ['page_header', 'paragraph', 'page_footer', 'page_footer']


def test_wordml_templates_ignore_similarly_named_foreign_elements_and_track_page_fields(tmp_path):
    from app.parsers.source_furniture import WORDML, wordml_templates
    path = tmp_path / 'word.xml'
    path.write_text(f'''<w:wordDocument xmlns:w="{WORDML}"><w:body><w:sectPr>
      <ftr><w:p><w:r><w:t>Not a Word footer</w:t></w:r></w:p></ftr>
      <w:ftr><w:p><w:r><w:instrText> PAGE </w:instrText><w:t>Page 1</w:t></w:r></w:p></w:ftr>
    </w:sectPr></w:body></w:wordDocument>''')
    assert wordml_templates(path) == [{'kind': 'footer', 'text': 'Page 1', 'dynamic': True}]


def test_slide_number_template_requires_placeholder_and_exact_page_and_position(tmp_path):
    from pptx import Presentation
    from pptx.oxml.xmlchemy import OxmlElement
    from pptx.util import Inches
    from app.parsers.source_furniture import pptx_templates, _match

    deck = Presentation(); slide = deck.slides.add_slide(deck.slide_layouts[6])
    number = slide.shapes.add_textbox(Inches(8), Inches(6.8), Inches(.4), Inches(.3))
    number.text = '15'
    ph = OxmlElement('p:ph'); ph.set('type', 'sldNum')
    number._element.nvSpPr.nvPr.append(ph)
    body = slide.shapes.add_textbox(Inches(2), Inches(2), Inches(1), Inches(.3))
    body.text = '15'
    path = tmp_path / 'numbers.pptx'; deck.save(path)
    templates = pptx_templates(path)
    assert len(templates) == 1
    position = templates[0]['bbox']
    assert _match('15', position, templates, 0) == 'page_footer'
    assert _match('15', position, templates, 1) is None
    assert _match('15', {'x0': .2, 'y0': .2, 'x1': .3, 'y1': .3}, templates, 0) is None


def block(text,page,y):
    return {'type':'paragraph','text':text,'source':{'parser':'test'},
            'regions':[{'pageIndex':page,'bbox':{'x0':.1,'y0':y,'x1':.5,'y1':y+.02}}]}


def test_referenced_word_footer_is_furniture_on_first_occurrence_but_body_stays(tmp_path):
    from app.parsers.source_furniture import W,R,PKG
    path=tmp_path/'source.docx'
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('word/document.xml',f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body><w:sectPr><w:footerReference w:type="even" r:id="r1"/></w:sectPr></w:body></w:document>')
        z.writestr('word/_rels/document.xml.rels',f'<Relationships xmlns="{PKG}"><Relationship Id="r1" Type="{R}/footer" Target="footer1.xml"/><Relationship Id="unused" Type="{R}/footer" Target="footer2.xml"/></Relationships>')
        z.writestr('word/footer1.xml',f'<w:ftr xmlns:w="{W}"><w:p><w:r><w:t>Section footer</w:t></w:r></w:p></w:ftr>')
        z.writestr('word/footer2.xml',f'<w:ftr xmlns:w="{W}"><w:p><w:r><w:t>Unused footer</w:t></w:r></w:p></w:ftr>')
    templates=docx_templates(path)
    assert len(templates)==1
    blocks=[block('Section footer',0,.93),block('Section footer',0,.4),block('continues with a new paragraph',1,.1)]
    doc=finish({'id':'foot','filename':'foot.docx','fileHash':'h','parser':'test','parserVersion':'1',
        'pageCount':2,'sourceFormat':'docx','_sourceFurniture':templates},
        [{'index':i,'width':600,'height':800} for i in range(2)],blocks)
    assert any(b.type=='page_footer' and b.text=='Section footer' for b in doc.blocks)
    assert any(b.type=='paragraph' and b.text.startswith('Section footer') for b in doc.blocks)


def test_alternating_footers_need_repeated_positions_and_both_parities():
    blocks=[block('Left footer' if p%2==0 else 'Right footer',p,.94) for p in range(4)]
    blocks += [block('Left footer',0,.6)]
    _repeated_running_text(blocks)
    assert [b['type'] for b in blocks]==['page_footer']*4+['paragraph']
    short=[block('Left footer',0,.94),block('Right footer',1,.94)]
    _repeated_running_text(short)
    assert all(b['type']=='paragraph' for b in short)


def test_source_template_does_not_match_a_longer_body_paragraph():
    b=block('Section footer is explained in this paragraph.',0,.9)
    mark_source_furniture([b],[{'kind':'footer','text':'Section footer','dynamic':False}])
    assert b['type']=='paragraph'


def test_word_footer_inside_a_layout_merged_block_is_split_without_losing_body():
    footer='Even page footer';body='Text inside a shape remains body.'
    b=block(footer+' '+body,0,.94)
    b['regions'] += block(body,0,.3)['regions']
    b['lines']=[{'pageIndex':r['pageIndex'],'bbox':r['bbox'],'start':start,'end':end,'xs':[.1]*(end-start)}
                for r,start,end in [(b['regions'][0],0,len(footer)),(b['regions'][1],len(footer)+1,len(b['text']))]]
    blocks=[b]
    mark_source_furniture(blocks,[{'kind':'footer','text':footer,'dynamic':False}])
    assert [(x['type'],x['text']) for x in blocks]==[('page_footer',footer),('paragraph',body)]
    assert blocks[1]['lines'][0]['start']==0


def test_source_header_keeps_identical_title_and_subheading_near_page_top():
    blocks = [block('Quarterly report', 0, .045), block('Quarterly report', 0, .09),
              block('Quarterly report', 1, .045), block('Quarterly report', 1, .09),
              block('Quarterly report', 2, .09)]
    blocks[1]['type'] = 'title'
    blocks[3]['type'] = blocks[4]['type'] = 'section_header'
    mark_source_furniture(blocks, [{'kind': 'header', 'text': 'Quarterly report'}])
    assert [b['type'] for b in blocks] == ['page_header', 'title', 'page_header', 'section_header', 'section_header']


def test_source_footer_keeps_signature_and_merged_body_at_bottom():
    body = block('Signed by\nContoso Ltd.', 0, .84)
    body['lines'] = [dict(r, start=a, end=z) for r, a, z in [
        (block('', 0, .84)['regions'][0], 0, 9),
        (block('', 0, .87)['regions'][0], 10, 22)]]
    blocks = [body, block('Contoso Ltd.', 0, .94), block('Contoso Ltd.', 1, .94)]
    mark_source_furniture(blocks, [{'kind': 'footer', 'text': 'Contoso Ltd.'}])
    assert [(b['type'], b['text']) for b in blocks] == [
        ('paragraph', 'Signed by\nContoso Ltd.'), ('page_footer', 'Contoso Ltd.'), ('page_footer', 'Contoso Ltd.')]


def test_unused_source_header_does_not_claim_a_matching_body_heading():
    blocks = [block('Report', 0, .08)]
    blocks[0]['type'] = 'title'
    mark_source_furniture(blocks, [{'kind': 'header', 'text': 'Report'}])
    assert blocks[0]['type'] == 'title'


def test_repeated_small_body_photos_remain_pictures_on_slides():
    from app.parsers.base import _decorations_that_repeat
    blocks = [block('', p, .67) for p in range(4)]
    for b in blocks:
        b['type'] = 'picture'
        b['regions'][0]['bbox'].update(x0=.16, x1=.22, y1=.75)
    _decorations_that_repeat(blocks, slides=True)
    assert all(b['type'] == 'picture' for b in blocks)
