"""Picture regions cut out as PNG files for the vision-language model step.

Runs inside the sandboxed parse job, so the API process never renders an uploaded PDF itself; it
only sends these images (with the caption and a little text before each) to the model.
"""
import json
from pathlib import Path

import pymupdf

from ..models import ParsedDocument

# Pictures smaller than this share of the page are logos, icons and bullets.
MIN_AREA = 0.015
MAX_SIDE_PX = 1280
CONTEXT_CHARS = 400
# The same picture at the same spot on this many pages (a logo, a slide's decoration, an agenda
# shown again before each part) is described on its first page only.
REPEATS = 3


def language(text: str) -> str:
    """The document's main script, for the language of the descriptions."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "en"
    share = lambda lo, hi: sum(lo <= c <= hi for c in letters) / len(letters)  # noqa: E731
    if share("가", "힣") > 0.2:
        return "ko"
    if share("぀", "ヿ") > 0.05:
        return "ja"
    if share("\u4e00", "\u9fff") > 0.3:
        return "zh"
    return "en"


def _clip(pdf, block):
    r = block.regions[0]
    page = pdf[r.page_index]
    W, H = page.rect.width, page.rect.height
    return page, pymupdf.Rect(r.bbox.x0 * W, r.bbox.y0 * H, r.bbox.x1 * W, r.bbox.y1 * H) * ~page.rotation_matrix


def _repeated(pdf, pictures: list) -> set[str]:
    """Ids of pictures whose spot looks the same, pixel for pixel, on REPEATS pages or more, but
    for the first of them."""
    if len({b.regions[0].page_index for b in pictures}) < REPEATS:
        return set()
    renders: dict[int, pymupdf.Pixmap] = {}

    def spot(page_index: int, box) -> list[bytes]:
        if page_index not in renders:
            renders[page_index] = pdf[page_index].get_pixmap(dpi=40, colorspace=pymupdf.csGRAY)
        pix = renders[page_index]
        x0, x1 = int(box.x0 * pix.width), max(int(box.x0 * pix.width) + 1, int(box.x1 * pix.width))
        y0, y1 = int(box.y0 * pix.height), max(int(box.y0 * pix.height) + 1, int(box.y1 * pix.height))
        rows = range(max(0, y0), min(pix.height, y1))
        return [pix.samples[y * pix.stride + max(0, x0): y * pix.stride + min(pix.width, x1)] for y in rows]

    out = set()
    for a in pictures:
        box, page = a.regions[0].bbox, a.regions[0].page_index
        mine = None
        same = {page}
        for b in pictures:
            other = b.regions[0].bbox
            if b.regions[0].page_index in same or max(abs(box.x0 - other.x0), abs(box.y0 - other.y0),
                                                      abs(box.x1 - other.x1), abs(box.y1 - other.y1)) > 0.015:
                continue
            # The same rectangle on both pages, so a box drawn a little differently still matches.
            mine = mine or spot(page, box)
            theirs = spot(b.regions[0].page_index, box)
            if all(max((abs(p - q) for p, q in zip(r, t)), default=0) <= 16 for r, t in zip(mine, theirs)):
                same.add(b.regions[0].page_index)
        if len(same) >= REPEATS and page != min(same):
            out.add(a.id)
    return out


def write_figures(path: Path, parsed: ParsedDocument, out_dir: Path, limit: int) -> int:
    """Crop the largest pictures (at most `limit`) to out_dir; returns how many were written."""
    # Empty new results must also invalidate a previous parse's manifest.
    (out_dir / "figures.json").unlink(missing_ok=True)
    blocks = [b for b in parsed.blocks if b.type not in ("page_header", "page_footer")]
    pictures = []
    for i, b in enumerate(blocks):
        if b.type != "picture" or not b.regions:
            continue
        box = b.regions[0].bbox
        area = (box.x1 - box.x0) * (box.y1 - box.y0)
        if area >= MIN_AREA:
            pictures.append((area, i, b))
    if not pictures:
        return 0
    pdf = pymupdf.open(path)
    template = _repeated(pdf, [b for _, _, b in pictures])
    pictures = [p for p in pictures if p[2].id not in template]
    chosen = sorted(sorted(pictures, key=lambda p: -p[0])[:limit], key=lambda p: p[1])
    if not chosen:
        return 0
    out_dir.mkdir(exist_ok=True)
    figures = []
    for _, i, b in chosen:
        page, clip = _clip(pdf, b)
        dpi = int(min(200, MAX_SIDE_PX * 72 / max(clip.width, clip.height, 1)))
        name = f"{b.id}.png"
        page.get_pixmap(clip=clip, dpi=dpi).save(out_dir / name)
        near = [blocks[j] for j in (i - 1, i + 1, i - 2, i + 2) if 0 <= j < len(blocks)]
        caption = next((o.text for o in near if o.type == "caption"), "")
        before = next((blocks[j].text for j in range(i - 1, -1, -1) if blocks[j].type in ("paragraph", "list_item")), "")
        figures.append({"blockId": b.id, "file": name, "page": b.regions[0].page_index + 1, "caption": caption[:CONTEXT_CHARS],
                        "context": before[-CONTEXT_CHARS:]})
    doc_text = " ".join(b.text for b in parsed.blocks[:400])
    (out_dir / "figures.json").write_text(json.dumps({"language": language(doc_text), "figures": figures,
                                                     "blocksFingerprint": parsed.blocks_fingerprint}, ensure_ascii=False))
    return len(figures)
