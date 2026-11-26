"""Recover headings supported by printed lines or source outlines.

Tables, figures, running furniture, code, formulas and notes are not candidates.
No source text is inferred: new heading text is a slice of an existing block or
the existing list item's preserved Markdown marker. Splits retain original IDs
for the first part and deterministic suffixes for additional parts.
"""
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..models import BlockRegion, DocumentBlock, ParsedDocument
from .base import CAPTION, SHEET_FORMATS, SLIDE_FORMATS, blocks_fingerprint, normalized_bbox
from .heading_structure import HEADINGS, Style, collect_evidence, horizontal_direction, normalized, printed_marker

_CANDIDATES = HEADINGS | {"paragraph", "list_item", "caption"}
_BODY_TYPES = frozenset({"paragraph", "list_item", "table", "code", "formula"})
_CONTENTS = {"contents", "tableofcontents", "\ubaa9\ucc28", "\ucc28\ub840", "\uc21c\uc11c"}
_DATE = re.compile(r"\s*(?:19|20)\d{2}[. /-]+\d{1,2}[. /-]+\d{1,2}\.?(?:\s+\d{1,2}:\d{2})?\s*$")
_PAGED = re.compile(r"\s\d{1,4}\s*$")
_LEADING_COUNTER = re.compile(r"^\s*(?:\(\d{1,3}\)|\d{1,3}(?:\.\d{1,3})*[.)]?|[IVXLCDM]+[.)]?|[\uac00-\ud7a3][.)])\s*")
_BARE = re.compile(r"[(\[<]?(?:\d{1,3}|[IVXLCDM]{1,5}|[\u2160-\u216b]|[A-Za-z]|[\u2460-\u2473\u3260-\u327f])[)\]>.]?")


def _counter(text: str):
    """A printed counter that can support recognition. Roman numerals in Latin
    letters need their dot or parenthesis: "I agree", "X axis" and "D grade"
    begin prose, while "IV." and the numeral characters do count."""
    marker = printed_marker(text)
    if (marker and marker.kind == "roman" and not re.match(r"\s*\.?\s*[\u2160-\u217f]", text)
            and not re.match(r"\s*\.?\s*[IVXLCDM]+\s*[.)]", unicodedata.normalize("NFKC", text))):
        return None
    return marker


def _bare(text: str) -> bool:
    """A counter alone ("3", "IV.", "(2)"): its title is somewhere else."""
    return bool(_BARE.fullmatch(unicodedata.normalize("NFKC", text).strip()))


@dataclass(frozen=True)
class PrintedLine:
    text: str
    style: Style
    region: BlockRegion
    horizontal: bool


def _sentence(text: str) -> bool:
    # A misplaced dot after a Roman heading can belong to its printed marker.
    marker = printed_marker(text)
    return bool(re.search(r"[.!?]\s*$", text)) and not (
        marker and marker.kind == "roman" and re.search(r"\s\.\s*$", text))


def _normal_positions(text: str) -> tuple[str, list[int]]:
    chars, positions = [], []
    for i, char in enumerate(text):
        for folded in unicodedata.normalize("NFKC", char).casefold():
            if folded.isalnum() or folded == "_":
                chars.append(folded)
                positions.append(i)
    return "".join(chars), positions


def _printed_lines(path: Path) -> tuple[dict[int, list[PrintedLine]], set[tuple[int, str]], float]:
    import pymupdf

    pages, bookmarks, counts = {}, set(), Counter()
    with pymupdf.open(path) as pdf:
        for _, text, page, *_ in pdf.get_toc(simple=False):
            bookmarks.add((page - 1, normalized(text)))
        flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
        for page in pdf:
            lines = []
            for item in page.get_text("dict", flags=flags)["blocks"]:
                for line in item.get("lines", []):
                    spans = line["spans"]
                    text = "".join(s["text"] for s in spans)
                    sizes, bold, total = Counter(), 0, 0
                    for span in spans:
                        n = len(span["text"].strip())
                        sizes[round(span["size"], 1)] += n
                        counts[round(span["size"], 1)] += n
                        total += n
                        if span["flags"] & 16 or "bold" in span["font"].lower():
                            bold += n
                    rect = pymupdf.Rect(line["bbox"]) * page.rotation_matrix
                    box = normalized_bbox(*rect, page.rect.width, page.rect.height)
                    if box is None:
                        continue
                    style = Style(sizes.most_common(1)[0][0] if sizes else 0, bold / max(1, total))
                    lines.append(PrintedLine(text, style, BlockRegion(page_index=page.number, bbox=box),
                                             horizontal_direction(line["dir"], page.rotation_matrix)))
            pages[page.number] = lines
    return pages, bookmarks, counts.most_common(1)[0][0] if counts else 0


def _slice_block(block: DocumentBlock, start: int, end: int, printed: PrintedLine | None,
                 part_id: str, first: bool) -> DocumentBlock:
    raw = block.text[start:end]
    start += len(raw) - len(raw.lstrip())
    text = raw.strip()
    end = start + len(text)
    part = block.model_copy(deep=True)
    part.id, part.text = part_id, text
    part.type = ("title" if first and block.type == "title" else "section_header") if printed else "paragraph"
    part.level = 0 if part.type == "title" else 1 if printed else None
    part.markdown = ("# " if part.type == "title" else "## ") + text if printed else None
    if printed:
        part.regions, part.lines, part.line_geometry = [printed.region], None, None
    else:
        lines = []
        # Estimates from the unsplit block cannot be treated as the character
        # positions of a new fragment. Rebuild them from printed lines below.
        for old in (block.lines or []) if block.line_geometry == "exact" else []:
            if not (0 <= old.start < old.end <= len(block.text) and len(old.xs) == old.end - old.start):
                part.line_geometry = "unavailable"
                continue
            lo, hi = max(start, old.start), min(end, old.end)
            if lo >= hi:
                continue
            line = old.model_copy(deep=True)
            line.start, line.end = lo - start, hi - start
            line.xs = old.xs[lo - old.start:hi - old.start]
            left = old.xs[lo - old.start] if lo > old.start else old.bbox.x0
            right = old.xs[hi - old.start] if hi < old.end else old.bbox.x1
            if old.bbox.x0 <= left < right <= old.bbox.x1:
                line.bbox = old.bbox.model_copy(update={"x0": left, "x1": right})
            else:
                # Non-monotone/vertical character positions cannot define a
                # trustworthy horizontal slice for the editor.
                part.line_geometry = "unavailable"
            lines.append(line)
        part.lines = lines or None
        if lines:
            part.regions = [BlockRegion(page_index=line.page_index, bbox=line.bbox) for line in lines]
        elif part.line_geometry != "unavailable":
            part.line_geometry = None
    return part


def _split_headings(parsed: ParsedDocument, path: Path, body_size: float, remainders: set[str] | None = None) -> int:
    pages, bookmarks, fallback_size = _printed_lines(path)
    body_size = body_size or fallback_size
    output: list[DocumentBlock] = []
    used = {b.id for b in parsed.blocks}
    split = 0
    for block in parsed.blocks:
        # A multi-line list item can carry its first printed marker only in
        # Markdown. Splitting its body would orphan that marker. Whole short
        # list headings are handled below, with their marker preserved.
        if block.type not in _CANDIDATES - {"list_item"} or not block.regions or len(block.text) < 3:
            output.append(block)
            continue
        matches: dict[tuple[int, int], PrintedLine] = {}
        key, positions = _normal_positions(block.text)
        for region in block.regions:
            box = region.bbox
            for line in pages.get(region.page_index, []):
                lb = line.region.bbox
                x, y = (lb.x0 + lb.x1) / 2, (lb.y0 + lb.y1) / 2
                if not (box.x0 - .002 <= x <= box.x1 + .002 and box.y0 - .003 <= y <= box.y1 + .003):
                    continue
                text, wanted = line.text.strip(), normalized(line.text)
                source_heading = (region.page_index, wanted) in bookmarks
                typographic = (line.horizontal and line.style.bold >= .7 and len(text) <= 120
                               and (line.style.size >= body_size * 1.12 or _counter(text)))
                if not wanted or wanted == key or not (source_heading or typographic):
                    continue
                if not source_heading and (_sentence(text) or CAPTION.match(text)):
                    continue
                found = key.find(wanted)
                if found < 0 or key.find(wanted, found + 1) >= 0:
                    continue
                start, end = positions[found], positions[found + len(wanted) - 1] + 1
                if start and not block.text[start - 1].isspace():
                    continue
                if end < len(block.text) and block.text[end].isalnum():
                    continue
                while end < len(block.text) and not block.text[end].isspace() and not block.text[end].isalnum():
                    end += 1
                if start == 0 and end == len(block.text):
                    continue
                matches[start, end] = line
        ordered = sorted(matches.items())
        if not ordered or any(a < z for ((_, z), _), ((a, _), _) in zip(ordered, ordered[1:])):
            output.append(block)
            continue
        pieces, position = [], 0
        for (start, end), line in ordered:
            if block.text[position:start].strip():
                pieces.append((position, start, None))
            pieces.append((start, end, line))
            position = end
        if block.text[position:].strip():
            pieces.append((position, len(block.text), None))
        # A wrapped heading is one heading, not a separate node for every line.
        separate_outlines = sum(line is not None and (line.region.page_index, normalized(line.text)) in bookmarks
                                for _, _, line in pieces)
        same_row_counter = False
        if len(pieces) == 2 and pieces[1][2] is not None and separate_outlines < 2 and len(block.text) < 120:
            start, end, _ = pieces[0]
            prefix = unicodedata.normalize("NFKC", block.text[start:end]).strip()
            if re.fullmatch(r"\.?\s*[IVXLCDM]+\.?|\d+(?:\.\d+)*[.)]?|\(\d+\)", prefix):
                heading_line = pieces[1][2]
                hb = heading_line.region.bbox
                for prefix_line in pages.get(heading_line.region.page_index, []):
                    pb = prefix_line.region.bbox
                    same_row_counter |= (normalized(prefix_line.text) == normalized(prefix)
                        and prefix_line.horizontal and pb.x1 <= hb.x0 + .01
                        and abs((pb.y0 + pb.y1) - (hb.y0 + hb.y1)) <= max(pb.y1 - pb.y0, hb.y1 - hb.y0))
        if same_row_counter:
            # A frame can print its chapter number in a separate left cell.
            # Whole-block recognition keeps that number with the heading,
            # rather than leaving it as the previous chunk's final paragraph.
            output.append(block)
            continue
        heads = [line for _, _, line in pieces if line is not None]

        def wrapped(line: PrintedLine) -> bool:
            """The next printed line of this block continues in the same style
            right below: a wrapped sentence, not a heading over its body."""
            lb, box = line.region.bbox, next(r.bbox for r in block.regions if r.page_index == line.region.page_index)
            below = [other for other in pages.get(line.region.page_index, []) if other is not line
                     and box.x0 - .002 <= (other.region.bbox.x0 + other.region.bbox.x1) / 2 <= box.x1 + .002
                     and lb.y1 - .25 * (lb.y1 - lb.y0) <= other.region.bbox.y0 <= box.y1 + .003]
            if not below:
                return False
            nxt = min(below, key=lambda other: other.region.bbox.y0)
            return (nxt.region.bbox.y0 - lb.y1 < .6 * (lb.y1 - lb.y0) and abs(nxt.style.size - line.style.size) < .4
                    and abs(nxt.style.bold - line.style.bold) < .2 and not any(nxt is other for other in heads))
        # One merged block may give up one printed heading line. Several bold
        # lines of one block are a wrapped emphasis, a figure or a form, not
        # separate sections, unless the source outline names each of them. A
        # counter alone ("3") would leave its title behind, and a remainder
        # without letters or digits ("\u2024 \u2024") is no text of its own.
        def distinct(a: PrintedLine, b: PrintedLine) -> bool:
            """Two heading lines of one block are two headings only in two sizes
            (a title over its first chapter, a chapter over a section), and
            never consecutive counters of one kind (a list of steps)."""
            ma, mb = _counter(a.text), _counter(b.text)
            listed = (ma and mb and ma.kind == mb.kind and ma.depth == mb.depth
                      and ma.parts[:-1] == mb.parts[:-1] and mb.parts[-1] == ma.parts[-1] + 1)
            return abs(a.style.size - b.style.size) >= .4 and not listed

        def marked(line: PrintedLine) -> bool:
            return (line.region.page_index, normalized(line.text)) in bookmarks

        # Several headings from one block need evidence for each: the source
        # outline, a printed counter, or a different size than the heading it
        # touches (a title over its first chapter). Touching lines must differ.
        adjacent = [(a, b) for (_, _, a), (_, _, b) in zip(pieces, pieces[1:]) if a is not None and b is not None]
        stacked = {id(x) for a, b in adjacent if distinct(a, b) for x in (a, b)}
        several = len(heads) >= 2 and not all(marked(h) for h in heads)
        if (several and (any(not (marked(a) and marked(b)) and not distinct(a, b) for a, b in adjacent)
                         or any(not marked(h) and not _counter(h.text) and id(h) not in stacked for h in heads))) or any(
                not marked(line) and wrapped(line) for line in heads) or any(
                (line is not None and _bare(block.text[start:end]))
                or (line is None and not any(c.isalnum() for c in block.text[start:end]))
                for start, end, line in pieces):
            output.append(block)
            continue
        if len(pieces) < 2 or (all(line is not None for _, _, line in pieces)
                              and len({round(line.style.size, 1) for _, _, line in pieces}) == 1
                              and separate_outlines < 2):
            output.append(block)
            continue
        split += 1
        for i, (start, end, line) in enumerate(pieces):
            bid = block.id
            if i:
                bid = f"{block.id}_part{i}"
                while bid in used:
                    bid += "x"
                used.add(bid)
            part = _slice_block(block, start, end, line, bid, not i)
            if line is None and remainders is not None:
                remainders.add(bid)
            if line is None and not part.lines:
                # Do not spread the remaining body over the old merged box,
                # which also contained the heading. Use complete printed lines
                # only when they account for the entire remaining text.
                wanted = normalized(part.text)
                matched = {}
                for region in block.regions:
                    box = region.bbox
                    for body_line in pages.get(region.page_index, []):
                        lb = body_line.region.bbox
                        x, y = (lb.x0 + lb.x1) / 2, (lb.y0 + lb.y1) / 2
                        token = normalized(body_line.text)
                        at = wanted.find(token) if token else -1
                        if (at >= 0 and wanted.find(token, at + 1) < 0
                                and box.x0 - .002 <= x <= box.x1 + .002
                                and box.y0 - .003 <= y <= box.y1 + .003):
                            value = (token, body_line.region)
                            if at not in matched:
                                matched[at] = value
                            elif matched[at] != value:
                                matched[at] = None  # repeated printed line, ambiguous location
                ordered_lines = [matched[k] for k in sorted(matched) if matched[k] is not None]
                if ordered_lines and "".join(t for t, _ in ordered_lines) == wanted:
                    part.regions = [region for _, region in ordered_lines]
                    part.line_geometry = None  # add_text_lines marks estimates
                else:
                    part.line_geometry = "unavailable"
            output.append(part)
    if split:
        # Array order and all original block IDs remain stable. Indexes after
        # inserted parts change, including indexes of otherwise untouched blocks.
        for i, block in enumerate(output):
            block.reading_order = i
        parsed.blocks = output
    return split


def _change(block: DocumentBlock, kind: str) -> None:
    block.type, block.level = kind, None
    if kind == "paragraph":
        block.markdown = None
    elif kind == "title":
        block.level, block.markdown = 0, "# " + block.text
    elif kind == "section_header":
        block.markdown = "## " + block.text


def _body_size(parsed: ParsedDocument, styles: dict[str, Style]) -> float:
    all_text, prose = Counter(), Counter()
    for block in parsed.blocks:
        size = styles.get(block.id, Style()).size
        if block.type != "paragraph" or not size or styles[block.id].horizontal < .8:
            continue
        all_text[size] += min(400, len(block.text))
        letters = sum(c.isalpha() for c in block.text)
        digits = sum(c.isdigit() for c in block.text)
        # Numerous small chart values must not make ordinary prose appear to be
        # large type. Prefer paragraph-sized stretches of letters; cap each
        # block's vote so one merged chart or paragraph cannot dominate a file.
        if letters >= 20 and letters >= digits * 2:
            prose[size] += min(400, letters)
    counts = prose or all_text
    return counts.most_common(1)[0][0] if counts else 0


def _merge_wrapped_titles(parsed: ParsedDocument, styles: dict[str, Style], outlines: set[str]) -> int:
    output: list[DocumentBlock] = []
    merged = 0
    for block in parsed.blocks:
        previous = output[-1] if output else None
        join = False
        if (previous and previous.type == block.type == "title" and previous.regions and block.regions
                and not (previous.id in outlines and block.id in outlines)):
            left, right = previous.regions[-1], block.regions[0]
            a, b = left.bbox, right.bbox
            gap = b.y0 - a.y1
            height = min(a.y1 - a.y0, b.y1 - b.y0)
            aligned = abs((a.x0 + a.x1) - (b.x0 + b.x1)) < .06 or abs(a.x0 - b.x0) < .02
            first_style, next_style = styles.get(previous.id, Style()), styles.get(block.id, Style())
            join = (left.page_index == right.page_index and aligned and -.25 * height <= gap <= .75 * height
                    and first_style.size > 0 and abs(first_style.size - next_style.size) < .4)
        if join:
            previous.text += " " + block.text
            previous.markdown = "# " + previous.text
            previous.regions.extend(block.regions)
            previous.lines, previous.line_geometry = None, None
            merged += 1
        else:
            output.append(block)
    if merged:
        for i, block in enumerate(output):
            block.reading_order = i
        parsed.blocks = output
    return merged




def _demote_inline_displays(parsed: ParsedDocument, styles: dict[str, Style], outlines: set[str]) -> None:
    """A bounded centered form between consecutive peers is not a new chapter.

    Require two display lines on the same page and no source outline. Page
    breaks, a new numbering sequence, a changed peer style, and ordinary left
    aligned chapter headings deliberately fall outside this evidence.
    """
    headings = [b for b in parsed.blocks if b.type in HEADINGS]
    numbered = [(i, b, marker) for i, b in enumerate(headings)
                if b.type == "section_header" and (marker := printed_marker(b.text))]
    for (a, left, lm), (z, right, rm) in zip(numbered, numbered[1:]):
        display = headings[a + 1:z]
        if (len(display) < 2 or lm.kind != rm.kind or lm.depth != rm.depth
                or lm.parts[:-1] != rm.parts[:-1] or rm.parts[-1] != lm.parts[-1] + 1
                or len(left.regions) != 1 or len(right.regions) != 1):
            continue
        ls, rs = styles.get(left.id, Style()), styles.get(right.id, Style())
        first, last = left.regions[0], right.regions[0]
        if (not ls.size or abs(ls.size - rs.size) >= .4 or abs(ls.bold - rs.bold) >= .2
                or first.page_index != last.page_index
                or abs(first.bbox.x0 - last.bbox.x0) >= .02):
            continue
        if max((styles.get(b.id, Style()).size for b in display), default=0) < ls.size * 1.2:
            continue
        matched = True
        for block in display:
            if block.type != "section_header" or block.id in outlines or len(block.regions) != 1:
                matched = False
                break
            region = block.regions[0]
            box = region.bbox
            if (region.page_index != first.page_index
                    or abs((box.x0 + box.x1) / 2 - .5) > .05
                    or box.x0 < first.bbox.x0 + .08
                    or not first.bbox.y1 <= box.y0 < box.y1 <= last.bbox.y0):
                matched = False
                break
        if matched:
            for block in display:
                _change(block, "paragraph")


def _has_later_title_peer(block: DocumentBlock, blocks: list[DocumentBlock], styles: dict[str, Style]) -> bool:
    style = styles.get(block.id, Style())
    # Before classification, a peer chapter can still be a paragraph/list.
    # Its matching style is enough to withhold the table-cover exception.
    # After classification the caller supplies only recognized headings.
    peer_types = _CANDIDATES - {"title"}
    return any(h.reading_order > block.reading_order and h.type in peer_types
               and style.size and abs(styles.get(h.id, Style()).size - style.size) < .4
               and abs(styles.get(h.id, Style()).bold - style.bold) < .2 for h in blocks)


def recognize_headings(parsed: ParsedDocument, path: Path) -> dict[str, int]:
    """Runs on code-point parser output, before the public UTF-16 conversion."""
    if parsed.document.source_format in SHEET_FORMATS | SLIDE_FORMATS | {"csv", "numbers"}:
        return {"split": 0, "merged": 0, "changed": 0}
    before = [(b.id, b.type, b.text) for b in parsed.blocks]
    evidence = collect_evidence(parsed, path, block_types=_CANDIDATES)
    body_size = _body_size(parsed, evidence.styles)
    remainders: set[str] = set()
    split = _split_headings(parsed, path, body_size, remainders)
    if split:
        evidence = collect_evidence(parsed, path, block_types=_CANDIDATES)
    styles = evidence.styles
    outlines = {bid for _, bid in evidence.outline if bid}
    source_parents, source_stack = set(), []
    for level, bid in evidence.outline:
        while source_stack and source_stack[-1][0] >= level:
            source_stack.pop()
        if bid:
            source_parents.update(parent for _, parent in source_stack if parent)
        source_stack.append((level, bid))
    outline_levels = {bid: level for level, bid in evidence.outline if bid}
    outline_counts = Counter(level for level, _ in evidence.outline)
    source_titles = {bid for bid in source_parents if outline_counts[outline_levels[bid]] == 1}

    toc_ids, prelude, cover_title = set(), set(), None
    for i, block in enumerate(parsed.blocks):
        if normalized(block.text) not in _CONTENTS:
            continue
        entries, pending = [], []
        paged_entries = 0
        for item in parsed.blocks[i + 1:]:
            if item.type in {"page_header", "page_footer"}:
                continue
            if item.type not in _CANDIDATES or len(item.text) >= 180:
                break
            if re.search(r"\s\d{1,4}\s*$", item.text):
                entries.extend([*pending, item.id])
                pending = []
                paged_entries += 1
            elif printed_marker(item.text):
                # TOCs can group paginated entries under a chapter label with
                # no page number. Require a following page entry before taking
                # such a label, so the first body heading is not swallowed.
                pending.append(item.id)
                if len(pending) > 2:
                    break
            else:
                break
        if paged_entries < 2:
            continue
        toc_ids.update([block.id, *entries])
        earlier = parsed.blocks[:i]
        heads = [x for x in earlier if x.type in HEADINGS]
        biggest = max((styles.get(x.id, Style()).size for x in heads), default=0)
        candidate = next((x for x in heads if styles.get(x.id, Style()).size == biggest), None)
        # Covers can hold contact/project metadata in a table. Keep that
        # exception only for an existing title with a distinct style; a chapter
        # with peers or a section header cannot gain cover status from it.
        cover_tables = (candidate is not None and candidate.type == "title"
                        and not _has_later_title_peer(candidate, parsed.blocks, styles))
        body = [x for x in earlier if x.type in _BODY_TYPES
                and not (cover_tables and x.type == "table")]
        if (not block.regions or block.regions[0].page_index > 2
                or any(len(x.text) > 80 for x in body)
                or sum(len(x.text) for x in body) > 160):
            continue
        if biggest and biggest >= body_size * 1.5:
            cover_title = candidate.id
            prelude.update(x.id for x in earlier if x.type in _CANDIDATES and x.id != cover_title)

    def printed(block: DocumentBlock) -> str:
        if block.type == "list_item" and block.text and block.markdown and block.markdown.endswith(block.text):
            return block.markdown
        return block.text

    flow = [b for b in parsed.blocks if b.type not in {"page_header", "page_footer"}]
    at = {b.id: i for i, b in enumerate(flow)}
    paged = [bool(_PAGED.search(b.text)) for b in flow]
    names = [normalized(_PAGED.sub("", b.text)) for b in flow]
    # The same label without its counter: a contents line "I. Outlook" may
    # announce a chapter printed as just "Outlook".
    labels = [normalized(_LEADING_COUNTER.sub("", unicodedata.normalize("NFKC", _PAGED.sub("", b.text)))) for b in flow]

    def contents_entry(block: DocumentBlock) -> bool:
        """A line of a contents list: paged among paged neighbours, an unpaged
        chapter label between paged entries, or one whose text (with or without
        its counter) opens a later block, the chapter itself."""
        i = at.get(block.id)
        if i is None or sum(paged[j] for j in range(max(0, i - 3), min(len(flow), i + 4)) if j != i) < 2:
            return False
        if paged[i] or (0 < i < len(flow) - 1 and paged[i - 1] and paged[i + 1]):
            return True  # paged itself, or a chapter label between paged entries
        later = names[i + 1:] + labels[i + 1:]
        return any(len(key) >= 4 and any(n.startswith(key) for n in later) for key in {names[i], labels[i]})

    def wrapped_sentence(block: DocumentBlock) -> bool:
        """The first line of a sentence that the parser cut into blocks: the next
        one or two lines in the same style follow without a gap and end it."""
        i = at.get(block.id)
        if i is None or not block.regions:
            return False
        text, last, style = printed(block), block.regions[-1], styles.get(block.id, Style())
        for following in flow[i + 1:i + 3]:
            if not following.regions or following.type not in _CANDIDATES or printed_marker(printed(following)):
                return False
            first, other = following.regions[0], styles.get(following.id, Style())
            height = last.bbox.y1 - last.bbox.y0
            if (first.page_index != last.page_index or not -.25 * height <= first.bbox.y0 - last.bbox.y1 < .6 * height
                    or not other.size or abs(other.size - style.size) >= .4 or abs(other.bold - style.bold) >= .2):
                return False
            text, last = text + " " + following.text, following.regions[-1]
            if _sentence(text):
                return True
        return False

    # Consecutive numbered lines of one kind with nothing between them are a
    # list (steps, options, fields), not sections without bodies.
    listed: set[str] = set()
    for a, b in zip(flow, flow[1:]):
        ma, mb = printed_marker(printed(a)), printed_marker(printed(b))
        if (ma and mb and a.type in _CANDIDATES and b.type in _CANDIDATES and ma.kind == mb.kind
                and ma.depth == mb.depth and ma.parts[:-1] == mb.parts[:-1] and mb.parts[-1] == ma.parts[-1] + 1):
            listed.update((a.id, b.id))

    for block in parsed.blocks:
        if block.type not in _CANDIDATES:
            continue
        style, text = styles.get(block.id, Style()), block.text
        if block.type == "list_item" and text and block.markdown and block.markdown.endswith(text):
            prefix = block.markdown[:-len(text)]
            if printed_marker(block.markdown) or re.fullmatch(r"(?:[A-Za-z]{1,8}[.)]|\([A-Za-z]{1,8}\))\s*", prefix):
                # Preserve alphabetic/Roman list markers too. Their presence
                # alone does not prove heading depth or authorize promotion.
                text = block.markdown
        marker = _counter(text)
        if block.id in toc_ids or block.id in prelude:
            if block.type in HEADINGS:
                _change(block, "paragraph")
            continue
        if block.id == cover_title:
            _change(block, "title")
            continue
        # Evidence of a numbered chapter can be its size even when a converted
        # font lost its bold flag. Numbered prose alone is insufficient.
        numbered = (marker and body_size and (style.bold >= .6 or style.size >= body_size * 1.2)
                    and style.size >= body_size * .95 and len(text) < 120)
        large = (body_size and len(normalized(text)) >= 3 and style.bold >= .7
                 and style.size >= body_size * 1.12 and len(text) < 100)
        ordinary = (numbered or large) and style.horizontal >= .8 and not (
            _DATE.fullmatch(text) or _sentence(text) or CAPTION.match(text))
        ordinary = (ordinary and block.id not in listed and block.id not in remainders
                    and not contents_entry(block) and not wrapped_sentence(block))
        if block.type not in HEADINGS and (block.id in outlines or ordinary):
            if text != block.text:
                block.text, block.lines, block.line_geometry = text, None, None
            _change(block, "section_header")
        elif (block.type in HEADINGS and _DATE.fullmatch(text) and style.bold < .5
              and body_size and style.size <= body_size * 1.15):
            _change(block, "paragraph")

    # A separate opening cover has a title and almost no body. In particular,
    # short paragraphs or numbered sections on page one do not make it a cover.
    opening_titles = set()
    last_page = max((b.regions[0].page_index for b in parsed.blocks
                     if b.regions and b.type not in {"page_header", "page_footer", "picture"}), default=0)
    for page in range(3):
        if page >= last_page:
            break  # a one-page notice has no separate cover
        blocks = [b for b in parsed.blocks if b.regions and b.regions[0].page_index == page
                  and b.type not in {"page_header", "page_footer", "picture"}]
        if not blocks:
            continue
        has_body = (sum(len(b.text) for b in blocks
                        if b.type in _BODY_TYPES) > 80
                 or any(printed_marker(b.text) for b in blocks if b.type in HEADINGS))
        if has_body or not any(b.type == "title" for b in blocks):
            break
        opening_titles.update(b.id for b in blocks if b.type == "title")
        for block in blocks:
            if block.type == "section_header" and block.id not in outlines:
                _change(block, "paragraph")

    # A top-sized first chapter is not a document title if later chapters repeat
    # that style. Covers proved by a contents prelude remain document titles.
    headings = [b for b in parsed.blocks if b.type in HEADINGS]
    for block in headings:
        if (block.type != "title" or block.id == cover_title
                or block.id in source_titles or block.id in opening_titles):
            continue
        if _has_later_title_peer(block, headings, styles):
            _change(block, "section_header")

    _demote_inline_displays(parsed, styles, outlines)
    merged = _merge_wrapped_titles(parsed, styles, outlines)
    after = [(b.id, b.type, b.text) for b in parsed.blocks]
    before_by_id = {row[0]: row for row in before}
    changed = sum(before_by_id.get(row[0]) != row for row in after) + len(set(before_by_id) - {b.id for b in parsed.blocks})
    if before != after:
        parsed.blocks_fingerprint = blocks_fingerprint(parsed.document.parser, parsed.document.parser_version, parsed.blocks)
    return {"split": split, "merged": merged, "changed": changed}
