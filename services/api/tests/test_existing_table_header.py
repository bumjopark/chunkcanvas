"""A table title does not mean the printed column labels are absent."""
import pytest
import pymupdf
from app.parsers.document_headers import restore_printed_headers
from app.parsers.source_tables import table_markdown
from tests.test_feedback_tables import document


@pytest.mark.parametrize('prefix', [['Separate table title','',''],['','','']])
def test_existing_labels_below_a_table_title_are_not_carried_again(tmp_path,prefix):
    header=['Strategy','Method','Outcome']
    records=[['First','Alpha','Success'],['Second','Beta','Pending']]
    printed=[prefix,header,*records]
    pdf=pymupdf.open()
    for page_number,rows in enumerate([[header],printed]):
        p=pdf.new_page(width=600,height=800);top=600 if page_number==0 else 40
        for x in [40,200,380,560]:p.draw_line((x,top),(x,top+40*len(rows)))
        for y in range(top,top+40*len(rows)+1,40):p.draw_line((40,y),(560,y))
        for i,row in enumerate(rows):
            for x,text in zip([45,205,385],row):p.insert_text((x,top+25+i*40),text,fontsize=9)
    path=tmp_path/'headings.pdf';pdf.save(path);pdf.close()
    doc=document([('paragraph',' '.join(header),0),('table',table_markdown(printed),1)],pages=2,fmt='pptx')
    doc.blocks[1].regions[0].bbox.y0=.04;doc.blocks[1].regions[0].bbox.y1=.26
    before=doc.model_dump()
    assert restore_printed_headers(doc,[[header,*records]],path,{})==0
    assert doc.model_dump()==before
