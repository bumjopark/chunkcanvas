"""Recognize repeated native vertical text outside the body column."""
from collections import defaultdict


def mark_margin_furniture(blocks: list[dict]) -> None:
    """Keep side running heads as furniture without dropping rotated body labels.

    Repetition alone is insufficient: the text must be native rotated text in a
    narrow outer margin, separated from horizontal body text, outside figures
    and tables, and repeated at the same position on three distinct pages.
    """
    by_page = defaultdict(list)
    for block in blocks:
        for region in block['regions']:
            by_page[region['pageIndex']].append((block, region['bbox']))
    candidates = defaultdict(list)
    for block in blocks:
        if not block.get('_rotated_text') or block['type'] != 'paragraph' or len(block['regions']) != 1:
            continue
        text = ' '.join(block['text'].split())
        if not 0 < len(text) <= 100:
            continue
        region = block['regions'][0]
        box = region['bbox']
        side = 'left' if box['x1'] <= .06 else 'right' if box['x0'] >= .94 else None
        if side is None or box['x1'] - box['x0'] > .035:
            continue
        peers = by_page[region['pageIndex']]
        def overlap(other):
            return min(box['x1'], other['x1']) > max(box['x0'], other['x0']) and min(box['y1'], other['y1']) > max(box['y0'], other['y0'])
        if any(b['type'] in ('picture', 'table') and overlap(bb) for b, bb in peers):
            continue
        def separated_body(b, bb):
            if b.get('_rotated_text') or b['type'] not in ('paragraph', 'list_item'):
                return False
            if bb['x1'] - bb['x0'] < .2:
                return False
            return bb['x0'] - box['x1'] >= .03 if side == 'left' else box['x0'] - bb['x1'] >= .03
        if any(separated_body(b, bb) for b, bb in peers):
            candidates[(side, text)].append(block)
    for peers in candidates.values():
        for block in peers:
            box = block['regions'][0]['bbox']
            pages = {other['regions'][0]['pageIndex'] for other in peers
                     if all(abs(box[k] - other['regions'][0]['bbox'][k]) <= .005 for k in box)}
            if len(pages) >= 3:
                block['type'], block['level'] = 'page_header', None
    _absorb_split_glyphs(blocks)


def _absorb_split_glyphs(blocks: list[dict]) -> None:
    """A glyph or two split off a side running head belong to that head.

    Docling sometimes splits a vertical side head (a chapter mark) into pieces on one page. The
    larger pieces repeat elsewhere and are marked furniture; a lone chapter glyph left as a paragraph
    would otherwise run into the body text across the page break. Only paragraphs of at most
    two characters in the same narrow outer margin, touching a head in that margin column,
    are taken.
    """
    margin = lambda box: box['x1'] <= .06 or box['x0'] >= .94
    heads = [(head, region) for head in blocks if head['type'] in ('page_header', 'page_footer')
             for region in head['regions'] if margin(region['bbox'])]
    for block in blocks:
        if block['type'] != 'paragraph' or len(block['regions']) != 1:
            continue
        if not 0 < len(''.join(block['text'].split())) <= 2:
            continue
        region = block['regions'][0]
        box = region['bbox']
        if not margin(box) or box['x1'] - box['x0'] > .035:
            continue
        line = box['y1'] - box['y0']
        for head, other in heads:
            near = other['bbox']
            if (other['pageIndex'] == region['pageIndex']
                    and min(box['x1'], near['x1']) > max(box['x0'], near['x0'])
                    and max(near['y0'] - box['y1'], box['y0'] - near['y1']) <= 1.5 * line):
                block['type'], block['level'] = head['type'], None
                break
