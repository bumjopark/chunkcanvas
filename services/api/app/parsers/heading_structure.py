"""A heading tree from source outlines, typography and printed numbering.

This pass changes only a heading's level and Markdown. Classification, text,
coordinates, IDs and fingerprints belong to the preceding parser stages.
Levels describe the resulting tree depth; they are not font-size ranks.
"""
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..models import DocumentBlock, ParsedDocument
from .base import SHEET_FORMATS, SLIDE_FORMATS

HEADINGS = frozenset({"title", "section_header"})
_HANGUL = "\uac00\ub098\ub2e4\ub77c\ub9c8\ubc14\uc0ac\uc544\uc790\ucc28\uce74\ud0c0\ud30c\ud558"
_ROMAN = re.compile(r"M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})$")


@dataclass(frozen=True)
class Marker:
    kind: str
    depth: int
    parts: tuple[int, ...]


@dataclass(frozen=True)
class Style:
    size: float = 0
    bold: float = 0
    horizontal: float = 1


@dataclass
class Evidence:
    styles: dict[str, Style]
    # Keep unmatched entries too: an absent source ancestor must not make its
    # descendants children of the previous, unrelated chapter.
    outline: list[tuple[int, str | None]]


def horizontal_direction(direction: tuple[float, float], rotation) -> bool:
    """Text extraction directions are unrotated; classify the displayed line."""
    return abs(direction[0] * rotation.b + direction[1] * rotation.d) < .1


def normalized(text: str) -> str:
    return re.sub(r"[^\w]+", "", unicodedata.normalize("NFKC", text).replace("\u200b", "")).casefold()


def printed_marker(text: str) -> Marker | None:
    text = unicodedata.normalize("NFKC", text).strip()
    # Some text layers place a Roman marker's dot before its numeral, or at the
    # end of the line. A bare capital letter in ordinary prose is not a marker.
    match = re.match(r"^\.?\s*([IVXLCDM]+)(?:\.\s*|\s+)(?=\S)", text)
    if match and _ROMAN.fullmatch(match[1]):
        values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
        digits = [values[c] for c in match[1]]
        value = sum(-n if i + 1 < len(digits) and n < digits[i + 1] else n for i, n in enumerate(digits))
        return Marker("roman", 0, (value,))
    match = re.match(r"^(\d{1,3}(?:\.\d{1,3})+)(?:[.)]\s*(?=[^\d\s])|\s+(?=\S))", text)
    if match:
        # A decimal measurement is not an outline counter. An explicit dot or
        # parenthesis after the number still marks a section ("3.5. Details").
        units = r"(?:inch(?:es)?|mm|cm|km|m|kg|mg|g|lb|lbs|oz|ml|l|hz|khz|mhz|ghz|kb|mb|gb|tb)"
        tail = text[len(match[1]):]
        if len(match[1].split(".")) == 2 and re.match(r"\s+" + units + r"(?=\s|$)", tail, re.I):
            return None
        parts = tuple(map(int, match[1].split(".")))
        return Marker("dotted", len(parts), parts)
    match = re.match(r"^(\d{1,3})[.)]\s*(?=[^\d\s])", text)
    if match:
        return Marker("dotted", 1, (int(match[1]),))
    match = re.match(r"^\((\d{1,3})\)\s*(?=\S)", text)
    if match:
        return Marker("paren", 4, (int(match[1]),))
    match = re.match(rf"^([{_HANGUL}])[.)]\s*(?=\S)", text)
    if match:
        return Marker("hangul", 3, (_HANGUL.index(match[1]) + 1,))
    match = re.match(r"^\uc81c\s*(\d+)\s*([\uc7a5\uc808\uc870])(?=\s|\()", text)
    if match:
        return Marker("legal", {"\uc7a5": 0, "\uc808": 1, "\uc870": 2}[match[2]], (int(match[1]),))
    return None


def collect_evidence(parsed: ParsedDocument, path: Path, *, block_types=HEADINGS) -> Evidence:
    """Use the already converted PDF; never rerun a converter/OCR or fetch URLs."""
    import pymupdf

    headings = [b for b in parsed.blocks if b.type in block_types]
    styles: dict[str, Style] = {}
    outlines: list[tuple[int, str | None]] = []
    by_page: dict[int, list] = {}
    with pymupdf.open(path) as pdf:
        for block in headings:
            sizes: Counter = Counter()
            bold = total = horizontal = 0
            for region in block.regions:
                pi = region.page_index
                if pi >= len(pdf):
                    continue
                page = pdf[pi]
                if pi not in by_page:
                    # Dict extraction otherwise includes image bytes. Font
                    # evidence needs only text, including on image-heavy pages.
                    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
                    by_page[pi] = [(s, horizontal_direction(line["dir"], page.rotation_matrix)) for item in page.get_text("dict", flags=flags)["blocks"]
                                   for line in item.get("lines", []) for s in line["spans"]]
                box = region.bbox
                for span, is_horizontal in by_page[pi]:
                    x0, y0, x1, y1 = pymupdf.Rect(span["bbox"]) * page.rotation_matrix
                    x, y = (x0 + x1) / 2 / page.rect.width, (y0 + y1) / 2 / page.rect.height
                    if box.x0 - .002 <= x <= box.x1 + .002 and box.y0 - .003 <= y <= box.y1 + .003:
                        weight = len(span["text"].strip())
                        sizes[round(span["size"], 1)] += weight
                        total += weight
                        if is_horizontal:
                            horizontal += weight
                        if span["flags"] & 16 or "bold" in span["font"].lower():
                            bold += weight
            styles[block.id] = Style(sizes.most_common(1)[0][0] if sizes else 0, bold / max(1, total), horizontal / max(1, total))

        matches: dict[tuple[int, str], list[DocumentBlock]] = {}
        for block in headings:
            if block.regions:
                names = {normalized(block.text)}
                if block.type == "list_item" and block.markdown and block.markdown.endswith(block.text):
                    # The text layer can retain the printed marker only in
                    # Markdown. Match the original outline before promotion.
                    names.add(normalized(block.markdown))
                for name in names:
                    key = block.regions[0].page_index, name
                    matches.setdefault(key, []).append(block)
        taken: set[str] = set()
        for level, text, page, *rest in pdf.get_toc(simple=False):
            candidates = [b for b in matches.get((page - 1, normalized(text)), []) if b.id not in taken]
            dest = rest[0].get("to") if rest else None
            bid = None
            if candidates:
                target = min(candidates, key=lambda b: abs(b.regions[0].bbox.y0 * pdf[page - 1].rect.height
                                                          - (dest.y if dest is not None else 0)))
                bid = target.id
                taken.add(bid)
            outlines.append((level, bid))
    return Evidence(styles, outlines)


def assign_heading_levels(parsed: ParsedDocument, path: Path) -> None:
    if not any(b.type in HEADINGS for b in parsed.blocks):
        return
    if parsed.document.source_format in SHEET_FORMATS | SLIDE_FORMATS | {"csv", "numbers"}:
        apply_heading_evidence(parsed, Evidence({}, []))
        return
    apply_heading_evidence(parsed, collect_evidence(parsed, path))


def apply_heading_evidence(parsed: ParsedDocument, evidence: Evidence) -> None:
    """Keep one structural decision here; exports only read the resulting levels."""
    headings = sorted((b for b in parsed.blocks if b.type in HEADINGS), key=lambda b: b.reading_order)
    by_id = {b.id: b for b in headings}
    # These page-oriented formats have no evaluated cross-page heading tree.
    # Compress existing gaps only; never deepen a heading in a sheet or slide.
    conservative = parsed.document.source_format in SHEET_FORMATS | SLIDE_FORMATS | {"csv", "numbers"}
    sizes: list[float] = []
    styles = evidence.styles
    for size in sorted({styles.get(b.id, Style()).size for b in headings if b.type != "title"} - {0}, reverse=True):
        if not sizes or sizes[-1] - size > max(.4, size * .035):
            sizes.append(size)

    def font(block: DocumentBlock) -> int:
        size = styles.get(block.id, Style()).size
        return min(range(len(sizes)), key=lambda i: abs(sizes[i] - size)) if sizes and size else block.level or 1

    markers = {b.id: printed_marker(b.text) if b.type != "title" else None for b in headings}
    # A bare leading integer is too weak to establish an outline, but it can
    # contradict a later explicit dotted counter. Use it only to withhold an
    # uncertain ancestor, never to promote a block or infer positive depth.
    ancestor_markers = dict(markers)
    for block in headings:
        if block.type != "title" and ancestor_markers[block.id] is None:
            weak = re.match(r"^(\d{1,3})\s+(?=\S)", unicodedata.normalize("NFKC", block.text).strip())
            if weak:
                ancestor_markers[block.id] = Marker("dotted", 1, (int(weak[1]),))
    outline_parents: dict[str, str | None] = {}
    ancestors: list[tuple[int, str | None]] = []
    for level, bid in evidence.outline:
        while ancestors and ancestors[-1][0] >= level:
            ancestors.pop()
        if bid in by_id:
            outline_parents[bid] = next((i for _, i in reversed(ancestors) if i in by_id), None)
        ancestors.append((level, bid))

    first_source_order = min((by_id[bid].reading_order for bid in outline_parents), default=0)

    def unnumbered(block: DocumentBlock) -> bool:
        return block.type != "title" and ancestor_markers[block.id] is None

    # An unnumbered heading may stand for the missing chapter of "N.x"
    # headings in its own style only when all of them share one N and the
    # heading itself prints that N ("Chapter 2" over 2.1 and 2.2). Front
    # matter followed by 1.1 and 1.2 (a chapter line read as a list item)
    # heads none of them.
    one_chapter: dict[str, bool] = {}
    for i, head in enumerate(headings):
        if not unnumbered(head):
            continue
        chapters = set()
        for later in headings[i + 1:]:
            if later.type == "title" or font(later) < font(head):
                break
            if font(later) > font(head):
                continue
            m = markers[later.id]
            if not (m and m.kind == "dotted" and len(m.parts) > 1):
                break
            chapters.add(m.parts[0])
        printed = unicodedata.normalize("NFKC", head.text)
        one_chapter[head.id] = len(chapters) == 1 and bool(re.search(rf"(?<!\d){next(iter(chapters))}(?!\d)", printed))

    def drop_unnumbered_parents(group: int, keep=lambda head: False) -> None:
        # Compare the style group recorded when each heading was placed: without
        # font evidence font() falls back to `level`, which is rewritten below.
        while stack and unnumbered(stack[-1][1]) and stack[-1][0][0] == group and not keep(stack[-1][1]):
            stack.pop()

    # Typography distinguishes reused counters (e.g. large "1." chapters and
    # smaller "1." sections). Numbering refines a shared font style, not the
    # global rank of every string containing a number.
    stack: list[tuple[tuple[int, int], DocumentBlock]] = []
    previous: dict[tuple[int, str, int], tuple[Marker, DocumentBlock, str | None]] = {}
    for block in headings:
        if block.type == "title":
            block.level = 0
            block.markdown = "# " + block.text
            stack = [((-1, -1), block)]
            previous.clear()
            continue
        marker = markers[block.id]
        rank = (block.level or 1, 0) if conservative else (font(block), marker.depth if marker else 0)
        signature = (font(block), marker.kind, marker.depth) if marker else None
        prior = previous.get(signature) if signature else None
        continued = None
        if prior and not conservative:
            old_marker, old, old_parent = prior
            advances = (marker.parts[:-1] == old_marker.parts[:-1]
                           and marker.parts[-1] > old_marker.parts[-1])
            # A sequence may resume after a quoted outline or a sample cover.
            # Its enclosing section must still be active; do not resurrect a
            # counter's parent from a previous chapter.
            if advances and (old_parent is None or any(h.id == old_parent for _, h in stack)):
                continued = old.level

        if not conservative and block.id in outline_parents:
            parent = outline_parents[block.id]
            if parent is not None:
                while stack and stack[-1][1].id != parent:
                    stack.pop()
            else:
                # A source root (or a child of a missing source ancestor)
                # cannot inherit an unrelated, unbookmarked heading inside
                # the preceding chapter. A preceding document cover can stay.
                while (stack and stack[-1][1].type != "title"
                       and stack[-1][1].reading_order >= first_source_order):
                    stack.pop()
                while stack and stack[-1][0] >= rank:
                    stack.pop()
        else:
            while stack and stack[-1][0] >= rank:
                stack.pop()
            if not conservative and marker and len(marker.parts) == 1:
                # An unnumbered heading in the same style ("Summary", a
                # preface, a contents line) gives no evidence that it owns
                # the numbered headings after it; chapters "1.", "2." are
                # its siblings. A larger style or an outline still nests them.
                drop_unnumbered_parents(rank[0])

        if not conservative and marker and marker.kind == "dotted" and len(marker.parts) > 1:
            parent = next((h for _, h in reversed(stack) if (m := markers[h.id])
                           and m.kind == "dotted" and m.parts == marker.parts[:-1]), None)
            if parent:
                while stack and stack[-1][1].id != parent.id:
                    stack.pop()
            else:
                # Missing 3. must not put 3.1 under 2. A shallower path is safer
                # than a fabricated numbered ancestor.
                while True:
                    nearest = next(((i, ancestor_markers[h.id]) for i, (_, h) in reversed(list(enumerate(stack)))
                                    if ancestor_markers[h.id] and ancestor_markers[h.id].kind == "dotted"), None)
                    if nearest is None:
                        break
                    i, ancestor = nearest
                    if len(ancestor.parts) < len(marker.parts) and marker.parts[:len(ancestor.parts)] == ancestor.parts:
                        break
                    del stack[i:]
                if block.id not in outline_parents:
                    drop_unnumbered_parents(rank[0], keep=lambda head: one_chapter[head.id])
        if continued is not None and block.id not in outline_parents:
            while stack and stack[-1][1].level >= continued:
                stack.pop()
        block.level = stack[-1][1].level + 1 if stack else 1
        block.markdown = "#" * min(6, block.level + 1) + " " + block.text
        if signature:
            previous[signature] = marker, block, stack[-1][1].id if stack else None
        stack.append((rank, block))
