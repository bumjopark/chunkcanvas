"""Keep labels in repeated vector cells with their diagram, before paragraph ordering.

This narrow recognizer handles rows of small, filled rectangular cells on slides. It requires
actual drawing geometry and native text; a short word alone never identifies a diagram.
"""
from collections import Counter
import re

from .textlayer import Fragment, build_text


def _rectangle(path, width, height):
    r = path['rect']
    if not path.get('fill') or not (.008 <= r.width / width <= .08 and .012 <= r.height / height <= .12):
        return None
    if not .5 <= r.width / r.height <= 2:
        return None
    items = path.get('items', [])
    if len(items) == 1 and items[0][0] == 're':
        return r
    # Writer may start in the middle of an edge, yielding five segments.
    if not 3 <= len(items) <= 8 or any(i[0] != 'l' for i in items):
        return None
    def on_edge(a,b):
        return (abs(a.x-b.x)<.2 and min(abs(a.x-r.x0),abs(a.x-r.x1))<.2) or \
               (abs(a.y-b.y)<.2 and min(abs(a.y-r.y0),abs(a.y-r.y1))<.2)
    points = [point for item in items for point in item[1:]]
    corners = [(r.x0,r.y0),(r.x0,r.y1),(r.x1,r.y0),(r.x1,r.y1)]
    if all(on_edge(item[1],item[2]) for item in items) and all(
            any(abs(p.x-x)<.2 and abs(p.y-y)<.2 for p in points) for x,y in corners):
        return r
    return None


def _tokens(text):
    return Counter(re.findall(r'\w+', text.casefold()))


def _owns_band_text(block, bbox, page_index, width):
    """Loose font/layout boxes can touch a band while all their glyphs lie outside it.

    Only a complete native transcription proves that overlap harmless. An OCR word
    without glyphs, or a mixed block whose native text is incomplete, still owns its box.
    """
    chars = block.get('_chars', [])
    if not chars or len(block['regions']) != 1:
        return True
    if any(bbox['x0'] <= c.cx <= bbox['x1'] and bbox['y0'] <= c.cy <= bbox['y1']
           for c in chars if c.c.strip()):
        return True
    native = build_text([Fragment(chars)], page_index, width)[0]
    return ''.join(native.split()) != ''.join(block['text'].split())


def group_diagram_rows(blocks, pdf, texts):
    for pn, page in enumerate(pdf):
        pt = texts[pn]
        if pt.rotated:
            continue
        rects = [r for path in page.get_drawings() if (r := _rectangle(path, pt.width, pt.height))]
        groups = []
        for r in sorted(rects, key=lambda r: (r.y0,r.x0)):
            group = next((g for g in groups if abs((g[0].y0+g[0].y1)-(r.y0+r.y1)) < .5*r.height
                          and .8 < g[0].height/r.height < 1.25), None)
            if group is None:
                groups.append([r])
            else:
                # Overprinted copies are one cell; distinct repeated cells remain distinct.
                if not any((r & other).get_area() > .8*r.get_area() for other in group):
                    group.append(r)
        for group in groups:
            if len(group) < 4:
                continue
            x0=min(r.x0 for r in group)/pt.width; x1=max(r.x1 for r in group)/pt.width
            y0=min(r.y0 for r in group)/pt.height; y1=max(r.y1 for r in group)/pt.height
            fragments=[]
            for f in pt.fragments:
                chars=[c for c in f.chars if x0<=c.cx<=x1 and y0<=c.cy<=y1]
                if chars:
                    fragments.append(Fragment(chars, f.block))
            labels = [f for f in fragments if len(''.join(c.c for c in f.chars).strip()) <= 3
                      and any(r.x0/pt.width <= f.chars[0].cx <= r.x1/pt.width for r in group)]
            if len(labels) < 2:
                continue
            text, _, chars = build_text(fragments, pn, pt.width)
            if not text:
                continue
            bbox={'x0':min(x0,min(c.x0 for c in chars)), 'x1':max(x1,max(c.x1 for c in chars)),
                  'y0':min(y0,min(c.y0 for c in chars)), 'y1':max(y1,max(c.y1 for c in chars))}
            held=[]
            for b in blocks:
                if b['type'] not in ('picture','paragraph','caption','section_header','title','list_item') or len(b['regions']) != 1:
                    continue
                reg=b['regions'][0]; box=reg['bbox']
                pad=.015 if b['type']=='picture' else .005
                if reg['pageIndex']==pn and box['x0']>=bbox['x0']-pad and box['x1']<=bbox['x1']+pad \
                        and box['y0']>=bbox['y0']-pad and box['y1']<=bbox['y1']+pad:
                    held.append(b)
            # A detected band must preserve each held block's words. Do not erase an OCR
            # annotation that the native text layer cannot substantiate.
            if not held or any(_tokens(b['text'])-_tokens(text) for b in held):
                continue
            # A paragraph crossing the band still owns its text. Without a safe
            # split, keep the original layout rather than duplicating its first line
            # inside the new picture. Fully held headings/list labels transfer too.
            if any(b not in held and b['text'].strip() and any(
                    r['pageIndex'] == pn and min(r['bbox']['x1'],bbox['x1']) > max(r['bbox']['x0'],bbox['x0'])
                    and min(r['bbox']['y1'],bbox['y1']) > max(r['bbox']['y0'],bbox['y0']) for r in b['regions'])
                   and _owns_band_text(b, bbox, pn, pt.width)
                   for b in blocks if b['type'] not in ('page_header','page_footer','picture')):
                continue
            for b in held:
                box=b['regions'][0]['bbox']
                for key in ('x0','y0'):
                    bbox[key]=min(bbox[key],box[key])
                for key in ('x1','y1'):
                    bbox[key]=max(bbox[key],box[key])
            at=min(blocks.index(b) for b in held)
            ids={id(b) for b in held}
            blocks[:]=[b for b in blocks if id(b) not in ids]
            blocks.insert(at, {'type':'picture','text':text,'level':None,
                'regions':[{'pageIndex':pn,'bbox':bbox}], 'lines':None,'lineGeometry':'unavailable',
                'source':{'parser':'pymupdf','ref':'vector-cell-row'},'_chars':chars})
    return blocks
