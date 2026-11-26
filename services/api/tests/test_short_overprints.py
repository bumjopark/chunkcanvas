"""Partly covered text shadows cannot append stray letters to a title."""
import pymupdf
import pytest
from app.parsers.textlayer import read_page, build_text

@pytest.mark.parametrize('keep', [(2,), (2,3)])
@pytest.mark.parametrize('offset,duplicate', [(1.6,True),(4.0,False)])
def test_short_shadow_remnant_requires_nearly_coincident_glyphs(keep,offset,duplicate):
    with pymupdf.open() as pdf:
        p=pdf.new_page(width=600,height=400)
        text='Shift Title'
        p.insert_text((50,100),text,fontsize=40)
        chars=p.get_texttrace()[0]['chars']
        for i,(_,_,_,bbox) in enumerate(chars):
            if i not in keep:
                p.draw_rect(pymupdf.Rect(bbox)+(-.01,-.01,.01,.01),color=None,fill=(1,1,1))
        p.insert_text((50,100-offset),text,fontsize=40)
        pt=read_page(p,0)
        actual=build_text(pt.fragments,0,pt.width)[0]
        if duplicate:
            assert actual==text
        else:
            assert ''.join(text[i] for i in keep) in actual.replace(text,'')

@pytest.mark.parametrize('delta,size', [(0,40),(1.6,40),(0,32),(4,40)])
def test_nearby_short_label_is_kept_unless_glyphs_match_position_and_size(delta,size):
    with pymupdf.open() as pdf:
        p=pdf.new_page(width=600,height=400)
        p.insert_text((50,100),'Shift Title',fontsize=40)
        # A separate label at another horizontal position, even on the same baseline.
        p.insert_text((300,100-delta),'it',fontsize=size)
        pt=read_page(p,0)
        assert 'it' in build_text(pt.fragments,0,pt.width)[0].replace('Shift Title','')
