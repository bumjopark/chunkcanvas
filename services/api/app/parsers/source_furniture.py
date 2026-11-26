"""Running text explicitly stored in Word parts or presentation placeholders.

Templates are evidence only at the matching page edge. Matching words in the body are retained.
"""
from pathlib import Path
import posixpath
import re
import zipfile
from defusedxml import ElementTree as ET

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
WORDML='http://schemas.microsoft.com/office/word/2003/wordml'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG='http://schemas.openxmlformats.org/package/2006/relationships'
STYLE='urn:oasis:names:tc:opendocument:xmlns:style:1.0'
TEXT='urn:oasis:names:tc:opendocument:xmlns:text:1.0'


def _xml(z, name, limit=4*1024*1024):
    with z.open(name) as f:
        data=f.read(limit+1)
    if len(data)>limit:
        raise ValueError('Header/footer source exceeds the metadata limit')
    return ET.fromstring(data)


def docx_templates(path: Path):
    out=[]
    with zipfile.ZipFile(path) as z:
        doc=_xml(z,'word/document.xml')
        rels=_xml(z,'word/_rels/document.xml.rels')
        targets={r.get('Id'):r for r in rels if r.tag==f'{{{PKG}}}Relationship' and r.get('TargetMode')!='External'}
        seen=set()
        for kind in ('header','footer'):
            for ref in doc.iter(f'{{{W}}}{kind}Reference'):
                rel=targets.get(ref.get(f'{{{R}}}id'))
                if rel is None or rel.get('Type')!=f'{R}/{kind}':
                    continue
                name=posixpath.normpath(posixpath.join('word',rel.get('Target','')))
                if not name.startswith('word/') or name in seen or len(seen)>=128:
                    continue
                seen.add(name)
                root=_xml(z,name,1024*1024)
                texts=[''.join(t.text or '' for t in p.iter(f'{{{W}}}t')) for p in root.iter(f'{{{W}}}p')]
                field=' '.join((el.text or '') if el.tag==f'{{{W}}}instrText' else el.get(f'{{{W}}}instr','') for el in root.iter())
                text=' '.join(' '.join(texts).split())
                if text and len(text)<=1000:
                    out.append({'kind':kind,'text':text,'dynamic':bool(re.search(r'\b(?:PAGE|NUMPAGES)\b',field))})
    return out


def fodt_templates(path: Path):
    if path.stat().st_size>64*1024*1024:
        return []
    root=ET.fromstring(path.read_bytes());out=[]
    for master in root.iter(f'{{{STYLE}}}master-page'):
        for item in master:
            kind=item.tag.rsplit('}',1)[-1]
            if kind not in ('header','header-left','footer','footer-left','header-first','footer-first'):
                continue
            text=' '.join(' '.join(''.join(p.itertext()) for p in item.iter(f'{{{TEXT}}}p')).split())
            if text and len(text)<=1000:
                out.append({'kind':kind.split('-')[0],'text':text,
                            'dynamic':any(el.tag in (f'{{{TEXT}}}page-number',f'{{{TEXT}}}page-count') for el in item.iter())})
    return out[:256]


def wordml_templates(path: Path):
    """Legacy Word XML keeps first/even/odd running text inside section parts."""
    if path.stat().st_size > 64 * 1024 * 1024:
        return []
    root = ET.fromstring(path.read_bytes())
    if root.tag != f'{{{WORDML}}}wordDocument':
        return []
    out = []
    for section in root.iter(f'{{{WORDML}}}sectPr'):
        for part in section:
            kind = {f'{{{WORDML}}}hdr': 'header', f'{{{WORDML}}}ftr': 'footer'}.get(part.tag)
            if kind is None:
                continue
            paragraphs = [''.join(t.text or '' for t in p.iter(f'{{{WORDML}}}t'))
                          for p in part.iter(f'{{{WORDML}}}p')]
            text = ' '.join(' '.join(paragraphs).split())
            fields = ' '.join(t.text or '' for t in part.iter(f'{{{WORDML}}}instrText'))
            if text and len(text) <= 1000:
                out.append({'kind': kind, 'text': text,
                            'dynamic': bool(re.search(r'\b(?:PAGE|NUMPAGES)\b', fields))})
            if len(out) >= 256:
                return out
    return out


def pptx_templates(path: Path):
    """Only explicit slide-number/footer placeholders, with their page and position.

    A numeric table cell near the bottom is not evidence of a slide number. The
    presentation's placeholder type supplies that distinction even when a PDF
    layout model includes the number inside a table's bounding box.
    """
    from pptx import Presentation
    from pptx.enum.shapes import PP_PLACEHOLDER

    deck = Presentation(str(path)); out = []
    for page, slide in enumerate(deck.slides):
        for shape in slide.placeholders:
            if shape.placeholder_format.type not in (PP_PLACEHOLDER.SLIDE_NUMBER, PP_PLACEHOLDER.FOOTER):
                continue
            text = ' '.join(shape.text.split()) if shape.has_text_frame else ''
            if not text or len(text) > 1000 or not shape.width or not shape.height:
                continue
            out.append({'kind': 'footer', 'text': text, 'dynamic': False, 'pageIndex': page,
                        'bbox': {'x0': shape.left / deck.slide_width, 'y0': shape.top / deck.slide_height,
                                 'x1': (shape.left + shape.width) / deck.slide_width,
                                 'y1': (shape.top + shape.height) / deck.slide_height}})
            if len(out) >= 256:
                return out
    return out


def _key(text, dynamic):
    value=''.join(text.casefold().split())
    return re.sub(r'\d+','#',value) if dynamic else value


def _position_templates(blocks, templates):
    """Locate unpositioned Word templates at the outermost printed occurrence.

    A source header part proves the words, not that every matching title near the
    top is a header. Use the outer occurrence across pages as an anchor, then
    require the same vertical extent and no text farther towards that edge.
    Explicit presentation placeholders already supply their own position.
    """
    units = []
    candidates = []
    for b in blocks:
        lines = b.get('lines') or []
        valid = lines and all(0 <= ln['start'] < ln['end'] <= len(b['text']) for ln in lines)
        parts = [(b['text'][ln['start']:ln['end']], ln, b['type']) for ln in lines] if valid else [
            (b['text'], r, b['type']) for r in b['regions']]
        units.extend((text, r, kind) for text, r, kind in parts if text.strip())
        candidates.extend(parts)
        if len(b['regions']) == 1:
            candidates.append((b['text'], b['regions'][0], b['type']))
    out = []
    for template in templates:
        if not isinstance(template, dict) or template.get('kind') not in ('header', 'footer'):
            continue
        if 'pageIndex' in template:
            out.append(template)
            continue
        header = template['kind'] == 'header'
        matches = [(r, kind) for text, r, kind in candidates
                   if kind in ('paragraph', 'section_header', 'title', 'caption', 'page_header', 'page_footer')
                   and isinstance(template.get('text'), str)
                   and _key(text, template.get('dynamic')) == _key(template['text'], template.get('dynamic'))
                   and (r['bbox']['y1'] <= .16 if header else r['bbox']['y0'] >= .84)]
        if not matches:
            continue
        anchor = min(matches, key=lambda v: v[0]['bbox']['y0'])[0]['bbox'] if header else max(
            matches, key=lambda v: v[0]['bbox']['y1'])[0]['bbox']
        for region, kind in matches:
            box = region['bbox']
            if any(abs(box[k] - anchor[k]) > .012 for k in ('y0', 'y1')):
                continue
            # A lone body heading is not enough to locate an unused header part.
            if kind in ('title', 'section_header') and all(k in ('title', 'section_header') for _, k in matches):
                continue
            if any(r['pageIndex'] == region['pageIndex'] and (
                    r['bbox']['y1'] < box['y0'] - .002 if header else r['bbox']['y0'] > box['y1'] + .002)
                   for _, r, _ in units):
                continue
            positioned = {**template, **region, '_located': True}
            if positioned not in out:
                out.append(positioned)
    return out


def _match(text, box, templates, page_index=None):
    for t in templates:
        if not isinstance(t,dict) or t.get('kind') not in ('header','footer') or not isinstance(t.get('text'),str):
            continue
        if 'pageIndex' in t:
            if t['pageIndex'] != page_index or not isinstance(t.get('bbox'), dict):
                continue
            position = t['bbox']
            x, y = (box['x0'] + box['x1']) / 2, (box['y0'] + box['y1']) / 2
            tolerance = .002 if t.get('_located') else .02
            edge = (position['x0'] - tolerance <= x <= position['x1'] + tolerance
                    and position['y0'] - tolerance <= y <= position['y1'] + tolerance)
        else:
            edge=box['y1']<=.16 if t['kind']=='header' else box['y0']>=.84
        if edge and _key(text,t.get('dynamic'))==_key(t['text'],t.get('dynamic')):
            return 'page_'+t['kind']
    return None


def mark_source_furniture(blocks, templates):
    templates = _position_templates(blocks, templates)
    out=[]
    for b in blocks:
        if b['type'] not in ('paragraph','section_header','title','caption','page_header','page_footer'):
            out.append(b)
            continue
        if len(b['regions'])==1 and (kind:=_match(b['text'],b['regions'][0]['bbox'],templates,b['regions'][0]['pageIndex'])):
            b['type']=kind;b['level']=None;b['_source_furniture']=True
            out.append(b)
            continue
        # Layout models can put a footer and a distant text box into one item. Native line
        # offsets let us separate the template from that item without slicing by a guessed
        # character count or dropping the text box along with the footer.
        lines=b.get('lines') or []
        valid=all(0<=ln['start']<ln['end']<=len(b['text']) for ln in lines) and all(
            a['end']<=c['start'] for a,c in zip(lines,lines[1:]))
        matches=[(ln,kind) for ln in lines if (kind:=_match(b['text'][ln['start']:ln['end']],ln['bbox'],templates,ln['pageIndex']))] if valid else []
        if not matches:
            out.append(b)
            continue
        def piece(start,end,kind=None):
            text=b['text'][start:end]
            left=len(text)-len(text.lstrip());text=text.strip();start+=left
            if not text:
                return
            own=[{**ln,'start':ln['start']-start,'end':ln['end']-start} for ln in lines
                 if start<=ln['start'] and ln['end']<=start+len(text)]
            part={k:v for k,v in b.items() if k not in ('_chars','_source_furniture')}
            part.update(text=text,lines=own or None,
                regions=[{'pageIndex':ln['pageIndex'],'bbox':ln['bbox']} for ln in own] or b['regions'])
            if kind:
                part.update(type=kind,level=None,_source_furniture=True)
            elif part['type'] in ('page_header','page_footer'):
                part.update(type='paragraph',level=None)
            out.append(part)
        at=0
        for ln,kind in matches:
            piece(at,ln['start'])
            piece(ln['start'],ln['end'],kind)
            at=ln['end']
        piece(at,len(b['text']))
    blocks[:]=out
