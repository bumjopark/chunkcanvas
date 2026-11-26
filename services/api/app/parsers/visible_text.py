"""Native PDF text after clipping and provably opaque rectangular overpainting.

PDF text extraction can include an earlier version of a form beneath its visible
replacement. Keep uncertain cases (partial covers, translucent fills, curved
paths); remove only glyphs fully covered by a later opaque rectangle. Invisible
OCR layers are intentionally retained: invisibility alone is not stale content.
"""
from collections import Counter

import pymupdf


def _key(origin, char):
    return round(origin[0], 2), round(origin[1], 2), char


def _chars(raw):
    for block in raw['blocks']:
        for line in block.get('lines', []):
            for span in line['spans']:
                yield from span['chars']


def _line_clip_bounds(items):
    # Clip records lack the paint path's rect. A scissor is only a bounding
    # box: using it for a non-rectangular clip can discard visible text.
    points = [items[0][1]]
    directions = []
    for _, start, end in items:
        if start != points[-1]:
            return None
        dx, dy = end - start
        if dx and dy:
            return None
        if dx or dy:
            direction = (dx > 0) - (dx < 0), (dy > 0) - (dy < 0)
            if not directions or directions[-1] != direction:
                directions.append(direction)
        points.append(end)
    if points[-1] != points[0] or len(directions) < 4:
        return None
    # A path can start midway along a side. After joining that side, a
    # rectangle has four runs, with opposite sides two turns apart.
    if directions[0] == directions[-1]:
        directions.pop()
    if len(directions) != 4 or len(set(directions)) != 4:
        return None
    if directions[0][0] != -directions[2][0] or directions[0][1] != -directions[2][1]:
        return None
    return pymupdf.Rect(min(p.x for p in points), min(p.y for p in points),
                        max(p.x for p in points), max(p.y for p in points))


def _rectangle(path):
    items = path['items']
    if len(items) == 1 and items[0][0] == 're':
        return pymupdf.Rect(items[0][1])
    # Office often emits a rectangle as five lines, starting midway along an edge.
    if not items or any(item[0] != 'l' for item in items):
        return None
    points = [items[0][1]]
    for _, start, end in items:
        if abs(start - points[-1]) > .001 or (abs(start.x - end.x) > .001 and abs(start.y - end.y) > .001):
            return None
        points.append(end)
    if abs(points[0] - points[-1]) > .001:
        if not path.get('closePath'):
            return None
        points.append(points[0])
    if path['type'] == 'clip' and 'rect' not in path:
        return _line_clip_bounds(items)
    area = abs(sum(a.x * b.y - a.y * b.x for a, b in zip(points, points[1:]))) / 2
    rect = pymupdf.Rect(path['rect'])
    if rect.is_empty or abs(area - rect.get_area()) > .001 * rect.get_area():
        return None
    return rect


def _covers(page):
    ancestors = []
    out = []
    for path in page.get_drawings(extended=True):
        level = path['level']
        while ancestors and ancestors[-1]['level'] >= level:
            ancestors.pop()
        if path['type'] in ('clip', 'group'):
            ancestors.append(path)
            continue
        if 'f' not in path['type'] or path.get('fill_opacity') != 1:
            continue
        rect = _rectangle(path)
        if rect is None:
            continue
        for parent in ancestors:
            if parent['type'] == 'clip':
                clip = _rectangle(parent)
                if clip is None:
                    rect = None
                    break
                rect &= clip
            elif parent.get('opacity') != 1 or parent.get('blendmode') != 'Normal':
                rect = None
                break
        if rect is not None and not rect.is_empty:
            out.append((path['seqno'], rect))
    return out


def _covered(rect, bbox):
    glyph = pymupdf.Rect(bbox)
    # Trace boxes use font-wide ascent/descent, including unused descender space.
    # Permit at most 0.15pt at the vertical edge (under 0.2 screen pixels), never
    # a horizontally clipped letter or a materially exposed part of the glyph.
    inset = min(.15, .02 * glyph.height)
    glyph.y0 += inset
    glyph.y1 -= inset
    return rect.contains(glyph)


def read_raw(page, flags, *, detect_clipping=False):
    """Return (rawdict, changed). All coordinates retain MuPDF's unrotated frame."""
    raw = page.get_text('rawdict', flags=flags | pymupdf.TEXT_CLIP)
    changed = False
    if detect_clipping:
        unclipped = page.get_text('rawdict', flags=(flags & ~pymupdf.TEXT_CLIP) & ~pymupdf.TEXT_PRESERVE_IMAGES)
        # Fully off-page glyphs do not justify changing the layout backend.
        frame = page.rect * page.derotation_matrix
        before = Counter(_key(c['origin'], c['c']) for c in _chars(unclipped)
                         if c['c'].strip() and frame.intersects(c['bbox']))
        after = Counter(_key(c['origin'], c['c']) for c in _chars(raw) if c['c'].strip())
        changed = bool(before - after)
    covers = _covers(page)
    if not covers:
        return raw, changed
    hidden, visible = set(), set()
    for span in page.get_texttrace():
        later = [rect for seq, rect in covers if seq > span['seqno'] and rect.intersects(span['bbox'])]
        for code, _glyph, origin, bbox in span['chars']:
            key = _key(origin, chr(code))
            target = hidden if any(_covered(rect, bbox) for rect in later) else visible
            target.add(key)
    # An identical glyph can be painted again after its old copy was covered.
    hidden -= visible
    for block in raw['blocks']:
        for line in block.get('lines', []):
            for span in line['spans']:
                for char in span['chars']:
                    if char['c'].strip() and _key(char['origin'], char['c']) in hidden:
                        # Preserve separation on partially covered lines (12 [cover] 34).
                        char['c'] = ' '
                        changed = True
    return raw, changed


def read_dict(page, flags):
    """The same visibility rules for consumers needing span text, not raw glyphs."""
    raw, _ = read_raw(page, flags)
    for block in raw['blocks']:
        for line in block.get('lines', []):
            for span in line['spans']:
                span['text'] = ''.join(c['c'] for c in span.pop('chars'))
    return raw
