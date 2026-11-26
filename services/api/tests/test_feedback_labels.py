from app.parsers.base import finish, _join_line_pieces


def block(kind,text,box,**more):
    return {'type':kind,'text':text,'regions':[{'pageIndex':0,'bbox':dict(zip(('x0','y0','x1','y1'),box))}],
            'source':{'parser':'test'},**more}


def test_short_figure_labels_and_numeric_values_survive_cleanup_once():
    pic=block('picture','10\n20\nR',(.1,.2,.8,.5),_picture_parts=[block('paragraph','10',(.2,.3,.25,.32))])
    label=block('paragraph','10',(.2,.3,.25,.32))
    body=block('paragraph','R is a variable in the following sentence.',(.1,.6,.8,.65))
    doc=finish({'id':'labels','filename':'labels.pdf','fileHash':'h','parser':'test','parserVersion':'1','pageCount':1},
               [{'index':0,'width':600,'height':800}],[pic,label,body])
    assert [b.text for b in doc.blocks]==['10\n20\nR',body['text']]


def test_two_printed_words_side_by_side_are_not_an_animation_duplicate():
    words=[block('paragraph','very',(.1,.3,.15,.32)),block('paragraph','very',(.155,.3,.205,.32))]
    assert ' '.join(b['text'] for b in _join_line_pieces(words))=='very very'


def test_vector_diagram_labels_stay_with_their_row_and_body_is_preserved():
    import pymupdf
    from app.parsers.slide_diagrams import group_diagram_rows
    from app.parsers.textlayer import read_page
    pdf=pymupdf.open();p=pdf.new_page(width=600,height=400)
    blocks=[]
    for x in (100,130,160,190):
        p.draw_rect(pymupdf.Rect(x,100,x+20,120),fill=(.8,.4,.6))
        p.insert_text((x+5,115),'R',fontsize=12)
        blocks.append(block('paragraph','R',((x+5)/600,.25,(x+15)/600,.3)))
    p.insert_text((100,180),'R is a body variable.',fontsize=12)
    blocks.append(block('paragraph','R is a body variable.',(.16,.42,.5,.46)))
    group_diagram_rows(blocks,pdf,[read_page(p,0)])
    assert len(blocks)==2
    assert blocks[0]['type']=='picture' and blocks[0]['text'].split()==['R']*4
    assert blocks[1]['text']=='R is a body variable.'


def test_picture_cleanup_keeps_letters_and_text_matching_a_distant_caption():
    from app.parsers.base import _clean_picture_text
    pic=block('picture','oooo\nA separate chart title',(.1,.1,.4,.3))
    far=block('caption','A separate chart title',(.6,.8,.9,.85))
    _clean_picture_text([pic,far])
    assert pic['text']=='oooo\nA separate chart title'


def test_page_background_does_not_prevent_a_raised_exponent_join():
    raw=[block('picture','',(0,0,1,1)), block('paragraph','The floor area is 5 m',(.1,.300,.40,.322)),
         block('paragraph','2',(.401,.296,.41,.310))]
    doc=finish({'id':'exponent','filename':'slide.pptx','fileHash':'h','sourceFormat':'pptx',
                'parser':'hybrid','parserVersion':'test','pageCount':1},[{'index':0,'width':720,'height':540}],raw)
    assert [b.text for b in doc.blocks if b.type=='paragraph'] == ['The floor area is 5 m²']


def test_diagram_takes_held_heading_once_and_leaves_straddling_paragraph_intact():
    import pymupdf
    from app.parsers.slide_diagrams import group_diagram_rows
    from app.parsers.textlayer import read_page
    for partial in (False,True):
        pdf=pymupdf.open();p=pdf.new_page(width=600,height=400)
        blocks=[]
        for x,label in zip((60,100,300,340),'ABCD'):
            p.draw_rect(pymupdf.Rect(x,100,x+20,120),fill=(.8,.4,.6))
            p.insert_text((x+5,115),label,fontsize=12)
            blocks.append(block('paragraph',label,((x+5)/600,.25,(x+15)/600,.3)))
        p.insert_text((160,115),'versus',fontsize=10)
        if partial:
            p.insert_text((160,135),'another line',fontsize=10)
        blocks.append(block('paragraph' if partial else 'section_header','versus another line' if partial else 'versus',
                            (.26,.26,.4,.35 if partial else .30)))
        pt=read_page(p,0)
        blocks[-1]['_chars']=[c for f in pt.fragments for c in f.chars if .26<=c.cx<=.4]
        group_diagram_rows(blocks,pdf,[pt])
        assert ' '.join(b['text'] for b in blocks).count('versus') == 1
        assert any(b['type']=='picture' for b in blocks) is not partial
        if partial: assert blocks[-1]['text'] == 'versus another line'


def test_diagram_band_ignores_adjacent_native_text_box_without_owning_its_glyphs():
    import pymupdf
    from app.parsers.slide_diagrams import group_diagram_rows
    from app.parsers.textlayer import read_page
    pdf=pymupdf.open();p=pdf.new_page(width=600,height=400)
    blocks=[]
    for x,label in zip((60,100,300,340),'ABCD'):
        p.draw_rect(pymupdf.Rect(x,100,x+20,120),fill=(.8,.4,.6))
        p.insert_text((x+5,115),label,fontsize=12)
        blocks.append(block('paragraph',label,((x+5)/600,.25,(x+15)/600,.3)))
    p.insert_text((160,115),'versus',fontsize=10)
    blocks.append(block('section_header','versus',(.26,.25,.4,.30)))
    p.insert_text((160,140),'root canal note',fontsize=10)
    pt=read_page(p,0)
    chars=[c for f in pt.fragments for c in f.chars if c.cy>.31]
    # Font/layout boxes touch the band, but every actual native glyph is below it.
    body=block('paragraph','root canal note',(.26,.295,.5,.36),_chars=chars)
    blocks.append(body)
    group_diagram_rows(blocks,pdf,[pt])
    assert len(blocks)==2
    assert blocks[0]['type']=='picture' and blocks[0]['text']=='A B versus C D'
    assert blocks[1] is body and body['text']=='root canal note'


def test_diagram_band_keeps_crossing_ocr_text_when_native_glyphs_are_incomplete():
    import pymupdf
    from app.parsers.slide_diagrams import group_diagram_rows
    from app.parsers.textlayer import read_page
    pdf=pymupdf.open();p=pdf.new_page(width=600,height=400)
    blocks=[]
    for x in (60,100,300,340):
        p.draw_rect(pymupdf.Rect(x,100,x+20,120),fill=(.8,.4,.6))
        p.insert_text((x+5,115),'R',fontsize=12)
        blocks.append(block('paragraph','R',((x+5)/600,.25,(x+15)/600,.3)))
    p.insert_text((160,140),'native note',fontsize=10)
    pt=read_page(p,0)
    chars=[c for f in pt.fragments for c in f.chars if c.cy>.31]
    body=block('paragraph','OCR label native note',(.26,.27,.5,.36),_chars=chars)
    blocks.append(body)
    group_diagram_rows(blocks,pdf,[pt])
    assert all(b['type']!='picture' for b in blocks)
    assert body['text']=='OCR label native note'
