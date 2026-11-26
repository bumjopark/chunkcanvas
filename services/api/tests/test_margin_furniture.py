import copy
import pytest
from app.parsers.base import finish
from app.parsers.margin_furniture import mark_margin_furniture


def block(text, page, box, *, rotated=False, kind='paragraph'):
    return dict(type=kind, text=text, level=None, regions=[dict(pageIndex=page,
                bbox=dict(zip(('x0','y0','x1','y1'),box)))], source=dict(parser='test'),
                _rotated_text=rotated)


def margin_pages(count=3, right=False):
    return [b for page in range(count) for b in [
        block('Running side label',page,(.95,.3,.975,.5) if right else (.025,.3,.05,.5),rotated=True),
        block('A paragraph beside the margin label.',page,(.13,.3,.87,.5)),
        block('A late top heading',page,(.2,.12,.8,.17),kind='section_header')]]


@pytest.mark.parametrize('right',[False,True])
def test_margin_furniture_restores_top_heading_order_without_deleting_labels(right):
    blocks=margin_pages(right=right)
    mark_margin_furniture(blocks)
    parsed=finish(dict(id='test',filename='side.pdf',fileHash='h',pageCount=3,parser='test',parserVersion='1'),
                  [dict(index=p,width=600,height=800) for p in range(3)],blocks)
    for page in range(3):
        own=[b for b in parsed.blocks if b.regions[0].page_index==page]
        assert [b.text for b in own if b.type!='page_header']==['A late top heading','A paragraph beside the margin label.']
        assert sum(b.type=='page_header' and b.text=='Running side label' for b in own)==1


@pytest.mark.parametrize('case',['two-pages','different-y','same-page','horizontal','in-body','figure','table','no-body','close-body'])
def test_repeated_body_labels_are_preserved(case):
    bs=margin_pages()
    for i,b in enumerate(bs):
        if i%3!=0:continue
        if case=='two-pages':b['regions'][0]['pageIndex']%=2
        if case=='different-y':
            for k in ('y0','y1'):b['regions'][0]['bbox'][k]+=i*.01
        if case=='same-page':b['regions'][0]['pageIndex']=0
        if case=='horizontal':b['_rotated_text']=False
        if case=='in-body':
            b['regions'][0]['bbox']['x0']=.2;b['regions'][0]['bbox']['x1']=.225
    if case in ('figure','table'):
        bs += [block('',p,(.01,.2,.4,.8),kind='picture' if case=='figure' else case) for p in range(3)]
    if case=='no-body':bs=[b for i,b in enumerate(bs) if i%3!=1]
    if case=='close-body':
        for i,b in enumerate(bs):
            if i%3!=0:b['regions'][0]['bbox']['x0']=.07
    before=copy.deepcopy(bs)
    mark_margin_furniture(bs)
    assert bs==before


def test_same_words_in_body_remain_body_and_repeated_glyphs_need_distinct_pages():
    bs=margin_pages()
    bs.append(block('Running side label',0,(.2,.7,.8,.75)))
    mark_margin_furniture(bs)
    assert bs[-1]['type']=='paragraph'
    assert sum(b['type']=='page_header' for b in bs)==3


def test_native_vertical_running_head_does_not_turn_single_column_page_into_columns(tmp_path):
    import pymupdf
    from app.parsers.pymupdf_parser import parse
    pdf=pymupdf.open()
    for _ in range(3):
        p=pdf.new_page(width=600,height=800)
        p.insert_text((80,220),'A body paragraph spanning a substantial part of the page.',fontsize=12)
        p.insert_text((25,280),'Running side label',fontsize=10,rotate=90)
        p.insert_text((100,100),'Top heading',fontsize=20)
    path=tmp_path/'side.pdf';pdf.save(path);pdf.close()
    parsed=parse(path,file_meta=dict(id='test',filename='side.pdf',fileHash='h',pageCount=3),progress=lambda *args:None)
    for page in range(3):
        own=[b for b in parsed.blocks if b.regions[0].page_index==page]
        assert [b.text for b in own if b.type not in ('page_header','page_footer')][0]=='Top heading'
        assert any(b.type=='page_header' and b.text=='Running side label' for b in own)


def test_vertical_glyph_lines_become_one_label(tmp_path):
    # Vertically set CJK labels arrive one glyph per non-horizontal line.
    import pymupdf
    from app.parsers import pymupdf_parser
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    page.insert_text((60, 100), 'Body text across the page so the page has horizontal lines.', fontsize=11)
    for i, ch in enumerate('세로축제목'):
        page.insert_text((300, 200 + i * 12), ch, fontsize=10, fontname='korea', rotate=90)
    path = tmp_path / 'vertical.pdf'
    pdf.save(path)
    pdf.close()
    doc = pymupdf_parser.parse(path, file_meta={'sourceFormat': None, 'id': 'x', 'filename': 'f', 'fileHash': '0' * 64},
                               progress=lambda a, b: None)
    texts = [b.text for b in doc.blocks]
    assert not any(len(t.strip()) == 1 for t in texts)
    assert any(t.replace(' ', '') in ('세로축제목', '목제축로세') for t in texts)


def test_separate_vertical_labels_in_one_column_stay_separate(tmp_path):
    # Two vertically set labels in the same column, a few points apart, are two labels.
    import pymupdf
    from app.parsers import pymupdf_parser
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    page.insert_text((60, 100), 'Body text across the page so the page has horizontal lines.', fontsize=11)
    y = 200
    for label in ('매출', '비용'):
        for ch in label:
            page.insert_text((300, y), ch, fontsize=10, fontname='korea', rotate=90)
            y += 12
        y += 6
    path = tmp_path / 'labels.pdf'
    pdf.save(path)
    pdf.close()
    doc = pymupdf_parser.parse(path, file_meta={'sourceFormat': None, 'id': 'x', 'filename': 'f', 'fileHash': '0' * 64},
                               progress=lambda a, b: None)
    texts = [b.text.replace(' ', '') for b in doc.blocks]
    assert '매출' in texts and '비용' in texts
