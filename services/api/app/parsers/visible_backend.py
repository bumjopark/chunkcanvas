"""Use clipped, visible native cells on PDFs whose extraction includes stale text."""
from pathlib import Path

import pymupdf

from .visible_text import read_raw


def backend_for(path: Path):
    from docling.backend.docling_parse_backend import ThreadedDoclingParseDocumentBackend
    from docling_core.types.doc import BoundingBox, CoordOrigin
    from docling_core.types.doc.page import BoundingRectangle, TextCell

    replacements = {}
    flags = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    # This runs before Docling starts its page workers. No MuPDF objects cross
    # threads, and unaffected pages keep the original backend's segmentation.
    with pymupdf.open(path) as pdf:
        for index, page in enumerate(pdf):
            raw, changed = read_raw(page, flags, detect_clipping=True)
            if not changed:
                continue
            lines, words = [], []
            seen_lines, seen_words = set(), set()

            def add(chars, cells, seen):
                visible = [c for c in chars if c['c'].strip()]
                if not visible:
                    return
                box = pymupdf.Rect(visible[0]['bbox'])
                for char in visible[1:]:
                    box |= pymupdf.Rect(char['bbox'])
                box *= page.rotation_matrix
                text = ''.join(c['c'] for c in chars).strip()
                identity = text, tuple(round(v, 2) for v in box)
                if identity in seen:
                    return
                seen.add(identity)
                cells.append(TextCell(index=len(cells), text=text, orig=text, from_ocr=False,
                    rect=BoundingRectangle.from_bounding_box(BoundingBox(
                        l=box.x0, t=box.y0, r=box.x1, b=box.y1, coord_origin=CoordOrigin.TOPLEFT))))

            for block in raw['blocks']:
                for line in block.get('lines', []):
                    chars = [c for span in line['spans'] for c in span['chars']]
                    # Layout needs intact visual lines. Giving it individual
                    # words can interleave adjacent lines on dense forms. Table
                    # recognition separately consumes the native word cells.
                    add(chars, lines, seen_lines)
                    word = []
                    for char in chars + [{'c': ' '}]:
                        if char['c'].isspace():
                            add(word, words, seen_words)
                            word = []
                        else:
                            word.append(char)
            replacements[index + 1] = lines, words
    if not replacements:
        return ThreadedDoclingParseDocumentBackend

    class VisibleDocumentBackend(ThreadedDoclingParseDocumentBackend):
        visibility_filtered_pages = frozenset(replacements)

        def iter_pages(self):
            for page in super().iter_pages():
                if page.page_no in replacements and (segmented := page.get_segmented_page()) is not None:
                    segmented.textline_cells, segmented.word_cells = replacements[page.page_no]
                    segmented.char_cells = []
                    segmented.has_lines = segmented.has_words = True
                    segmented.has_chars = False
                yield page

    return VisibleDocumentBackend
