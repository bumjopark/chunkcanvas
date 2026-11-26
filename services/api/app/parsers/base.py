import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Callable

from ..models import BBox, DocumentBlock, ParsedDocument, TextLine
from .spacing import HANGUL, one_word

ProgressFn = Callable[[int, int], None]

_SPACES = re.compile(r"[ \t  -​ 　]+")
# "Table 1", "<표 2-1>", "[그림 3]", the Japanese chart label with "Ⅱ-1-1", "Figure IV." ...
CAPTION = re.compile(
    r"^\s*[<\[(〈【]?\s*(?:table|figure|fig\.|chart|exhibit|표|그림|도표|도면|사진|\u56f3\u8868|\u56f3|\u8868|\u56fe|\u5716)\s*"
    r"(?:\d+|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+|[IVX]+(?=[\s.\-:–]))",
    re.IGNORECASE,
)
_TERMINAL = re.compile(r"[.!?。！？:;…\"'”’)\]]\s*$")
FURNITURE = {"page_header", "page_footer"}
# Hancom's fonts draw the "한" of "한글" and circled item numbers (U+F02B1 = ①) from private-use codes.
HANCOM = {0xF53A: "한", **{0xF02B1 + i: chr(0x2460 + i) for i in range(20)}}
# Digits and signs written as superscripts (exponents, units, citation marks).
SUPERSCRIPT = str.maketrans("0123456789+-−=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾")
# Block types that may be split inside (they get per-character line geometry); others are atomic.
SPLITTABLE = {"paragraph", "list_item", "caption", "footnote"}
# Presentation formats: every page is a slide, and nothing runs on to the next one.
SLIDE_FORMATS = {"pptx", "ppt", "odp", "sxi", "fodp", "key"}
SHEET_FORMATS = {"xlsx", "xls", "xlsb", "ods", "sxc", "fods", "xmlss"}
# Pictures of a page (one page each, never with running heads).
IMAGE_FORMATS = {"png", "jpeg", "gif", "tiff", "bmp", "webp"}
# Documents written as a flow of paragraphs and tables (a paragraph never holds a table).
FLOW_FORMATS = {"docx", "doc", "odt", "rtf", "sxw", "fodt", "wordml", "wpd", "pages", "hwp", "hwpx", "hwp3", "html", "md", "txt", "epub"}


def clean_text(text: str) -> str:
    """Collapse runs of spaces (including no-break and ideographic spaces) and trim."""
    return "\n".join(_SPACES.sub(" ", line).strip() for line in text.splitlines()).strip()


class ParseError(Exception):
    """A failure whose message is safe to show to the user (never contains document text)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def normalized_bbox(x0: float, y0: float, x1: float, y1: float, width: float, height: float) -> BBox | None:
    """Top-left-origin page points to 0..1 page coordinates. None for degenerate boxes."""
    if width <= 0 or height <= 0:
        return None
    nx0, nx1 = sorted((x0 / width, x1 / width))
    ny0, ny1 = sorted((y0 / height, y1 / height))
    clamp = lambda v: min(1.0, max(0.0, v))  # noqa: E731
    box = BBox(x0=clamp(nx0), y0=clamp(ny0), x1=clamp(nx1), y1=clamp(ny1))
    if box.x1 - box.x0 <= 0 or box.y1 - box.y0 <= 0:
        return None
    return box


def table_markdown(rows: list[list[str]]) -> str:
    rows = [[clean_text(c).replace("\n", " ").replace("|", "\\|") for c in row] for row in rows if row]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = [rows[0], ["---"] * width, *rows[1:]]
    return "\n".join("| " + " | ".join(r) + " |" for r in lines)


def add_table_lines(parsed: ParsedDocument) -> None:
    """Give each table a line per row, so a table too long for one chunk can be cut between rows.
    Where each row is drawn is not known: the rows are spread by height over the area the table
    covers on each of its pages."""
    for b in parsed.blocks:
        if b.type != "table" or b.lines or not b.regions or "\n" not in b.text:
            continue
        rows, start = [], 0
        for i, row in enumerate(b.text.split("\n")):
            if i != 1 or not re.match(r"\|\s*:?-{3}", row):
                rows.append((start, start + len(row)))
            start += len(row) + 1
        areas: dict[int, BBox] = {}
        for r in b.regions:
            a = areas.get(r.page_index)
            areas[r.page_index] = r.bbox if a is None else BBox(x0=min(a.x0, r.bbox.x0), y0=min(a.y0, r.bbox.y0),
                                                                  x1=max(a.x1, r.bbox.x1), y1=max(a.y1, r.bbox.y1))
        heights = [max(a.y1 - a.y0, 1e-6) for a in areas.values()]
        lines, done = [], 0
        for k, ((page, box), height) in enumerate(zip(areas.items(), heights)):
            share = len(rows) - done if k == len(areas) - 1 else min(len(rows) - done, round(len(rows) * height / sum(heights)))
            for j in range(share):
                s, e = rows[done + j]
                y0 = box.y0 + (box.y1 - box.y0) * j / share
                y1 = box.y0 + (box.y1 - box.y0) * (j + 1) / share
                xs = [round(box.x0 + (box.x1 - box.x0) * i / max(1, e - s), 4) for i in range(e - s)]
                lines.append(TextLine(page_index=page, bbox=BBox(x0=box.x0, y0=y0, x1=box.x1, y1=y1), start=s, end=e, xs=xs))
            done += share
        b.lines = lines
        b.line_geometry = "estimated"


# A line of body text, as a share of the page height (about 13 pt on A4).
LINE_HEIGHT = 0.016


def add_text_lines(parsed: ParsedDocument) -> None:
    """Text without character positions (read by OCR, or kept as the layout model read it) gets
    estimated lines filling its regions, so a paragraph too long for a chunk can still be cut
    at a sentence."""
    for b in parsed.blocks:
        if b.type not in SPLITTABLE or b.lines or not b.regions or len(b.text) < 2 or b.line_geometry == "unavailable":
            continue
        counts = [max(1, round((r.bbox.y1 - r.bbox.y0) / LINE_HEIGHT)) for r in b.regions]
        per = len(b.text) / sum(counts)
        lines, k = [], 0
        for region, n in zip(b.regions, counts):
            box = region.bbox
            for j in range(n):
                s, e = round(k * per), round((k + 1) * per)
                k += 1
                if e <= s:
                    continue
                y0 = box.y0 + (box.y1 - box.y0) * j / n
                y1 = box.y0 + (box.y1 - box.y0) * (j + 1) / n
                xs = [round(box.x0 + (box.x1 - box.x0) * i / (e - s), 4) for i in range(e - s)]
                lines.append(TextLine(page_index=region.page_index, bbox=BBox(x0=box.x0, y0=y0, x1=box.x1, y1=y1), start=s, end=e, xs=xs))
        b.lines = lines
        b.line_geometry = "estimated"


# "(Continued)" at the foot of a patent column: a running mark, not text.
# A patent's "-continued" over the rest of a table or formula list (twice when two columns' are read together).
_DASH_CONTINUED = re.compile(r"(?:[-\u2013\u2014]\s*continued\s*[-\u2013\u2014]?\s*)+", re.IGNORECASE)
_CONTINUED = re.compile(r"\(\s*(?:(?:뒷\s*면|다음\s*[면쪽장])\s*에\s*)?(?:continued|계속|\u7d9aく|\u7eed)(?:\s+(?:on|from)\s[^()]{0,20})?\s*\)",
                        re.IGNORECASE)


def _first_page(block: dict) -> int | None:
    return block["regions"][0]["pageIndex"] if block["regions"] else None


def _last_page(block: dict) -> int | None:
    return block["regions"][-1]["pageIndex"] if block["regions"] else None


# Front page of a Korean patent: INID-coded fields ("(72) 발명자") and their labels.
_INID = re.compile(r"^\(\d{2}\)|^(?:발명자|특허권자|출원인|대리인|권리자)$")
_PARTY = re.compile(r"^[가-힣]{2,4}$|주식회사|㈜|\(주\)|연구원$|산학협력단|재단$|공사$|\b(?:Inc|Ltd|Co|GmbH|Corp|LLC)\b")


def _captions_before_figures(blocks: list[dict]) -> list[dict]:
    """A caption set above its table or picture ("Table 1: …" over the table) but read after it
    goes in front of it, where it introduces it (and stays with its first piece when it is cut)."""
    out: list[dict] = []
    for b in blocks:
        figure = next((k for k in range(len(out) - 1, -1, -1) if out[k]["type"] not in FURNITURE), None)
        if b["type"] == "caption" and b["regions"] and figure is not None and out[figure]["type"] in ("table", "picture") \
                and out[figure]["regions"]:
            above, box = b["regions"][-1], out[figure]["regions"][0]
            if above["pageIndex"] == box["pageIndex"] and above["bbox"]["y1"] <= box["bbox"]["y0"] + 0.01 \
                    and min(above["bbox"]["x1"], box["bbox"]["x1"]) - max(above["bbox"]["x0"], box["bbox"]["x0"]) > 0:
                out.insert(figure, b)
                continue
        out.append(b)
    return out


def _mark_patent_parties(blocks: list[dict]) -> None:
    """On a patent's front pages, inventors and applicants ("정미혜", "삼성디스플레이 주식회사")
    are names in the bibliographic fields, not headings: as headings each would start a chunk."""
    fields: dict[int, int] = {}
    for b in blocks:
        if b["regions"] and _INID.match(b["text"].strip()):
            fields[_first_page(b)] = fields.get(_first_page(b), 0) + 1
    for b in blocks:
        text = b["text"].strip()
        if (b["type"] == "section_header" and b["regions"] and fields.get(_first_page(b), 0) >= 2
                and len(text) <= 30 and _PARTY.search(text)):
            b["type"], b["level"] = "paragraph", None


def _mark_slide_labels(blocks: list[dict]) -> None:
    """On a slide, a heading after the first with nothing but pictures and other headings below it
    is a label of a picture or diagram ("polarized Th2 response"), not the start of a section:
    as one it would open the next slide's chunk."""
    slides: dict[int, list[dict]] = {}
    for b in blocks:
        if b["type"] not in FURNITURE and b["regions"]:
            slides.setdefault(b["regions"][0]["pageIndex"], []).append(b)
    for slide in slides.values():
        heads = [i for i, b in enumerate(slide) if b["type"] in ("section_header", "title")]
        for i in heads[1:]:
            if all(b["type"] in ("picture", "caption", "section_header", "title") for b in slide[i + 1:]):
                slide[i]["type"], slide[i]["level"] = "paragraph", None


# A title set in angle brackets, as Korean documents title a figure or table ("<기술개발 개념도>",
# "< 다부처 병렬형 R&D 추진체계(안) >").
BRACKETED_TITLE = re.compile(r"^\s*[<〈《＜]\s*[^<>〈〉《》＜＞]{2,60}?\s*[>〉》＞]\s*$")


def _mark_captions(blocks: list[dict]) -> None:
    """A short 'Table 1. ...' paragraph next to a table or picture is its caption, and so is a
    title in angle brackets right above or below one on its page."""
    for i, b in enumerate(blocks):
        # Layout parsers often call a caption above a figure a heading.
        if b["type"] not in ("paragraph", "section_header") or len(b["text"]) > 300:
            continue
        if BRACKETED_TITLE.match(b["text"]) and any(
                blocks[j]["type"] in ("table", "picture") and _first_page(blocks[j]) == _first_page(b)
                for j in (i - 1, i + 1) if 0 <= j < len(blocks)):
            b["type"], b["level"] = "caption", None
            continue
        if not CAPTION.match(b["text"]):
            continue
        neighbours = [blocks[j]["type"] for j in (i - 1, i + 1) if 0 <= j < len(blocks)]
        # A heading numbered as a figure or table is its title wherever the reading order put it;
        # a paragraph has to sit next to one ("Figure 3 shows..." is body text).
        if any(t in ("table", "picture") for t in neighbours) or (b["type"] == "section_header" and len(b["text"]) < 150):
            b["type"], b["level"] = "caption", None
    titles = _figure_titles(blocks)
    for b in blocks:
        if id(b) in titles:
            b["type"], b["level"] = "caption", None


def _figure_titles(blocks: list[dict]) -> set[int]:
    """Short lines set right above the table or picture read next ("\uadf8\ub9ac\uc2a4 \uc18c\ubb38\uc790()", "Basic
    commands"): titles of the figure, past labels drawn inside it. Returned by id()."""
    titles = set()
    for i, b in enumerate(blocks):
        text = b["text"].strip()
        if b["type"] != "paragraph" or not b.get("regions") or len(text) > 40 or _SENTENCE_END.search(text):
            continue
        tb = b["regions"][-1]
        for f in blocks[i + 1:i + 5]:
            if f["type"] in FURNITURE or not f["regions"]:
                continue
            if f["type"] in ("table", "picture"):
                # A table's part on the page may be several cell regions: their span.
                on_page = [r["bbox"] for r in f["regions"] if r["pageIndex"] == tb["pageIndex"]]
                if on_page:
                    fb = {"x0": min(r["x0"] for r in on_page), "x1": max(r["x1"] for r in on_page), "y0": min(r["y0"] for r in on_page)}
                    if -0.005 <= fb["y0"] - tb["bbox"]["y1"] <= 0.04 \
                            and min(fb["x1"], tb["bbox"]["x1"]) - max(fb["x0"], tb["bbox"]["x0"]) >= 0.5 * (tb["bbox"]["x1"] - tb["bbox"]["x0"]):
                        titles.add(id(b))
                break
            # Past a label drawn inside the next figure (a stray "1" over a symbol table).
            nxt = next((n for n in blocks[blocks.index(f) + 1:i + 5] if n["type"] in ("table", "picture") and n["regions"]), None)
            if nxt is None or len(f["text"]) > 20 or not _inside(f["regions"][0]["bbox"], nxt["regions"][0]["bbox"]):
                break
    return titles


def _inside(inner: dict, outer: dict) -> bool:
    cx, cy = (inner["x0"] + inner["x1"]) / 2, (inner["y0"] + inner["y1"]) / 2
    return outer["x0"] <= cx <= outer["x1"] and outer["y0"] <= cy <= outer["y1"]


# The end of a sentence (a closing bracket is not one: "\ud654\uc0b4\ud45c()" is a title).
_SENTENCE_END = re.compile(r"[.!?\u3002\uff01\uff1f:;\u2026]\s*$")


# A word for picture text: two or more letters in any script (not "68", "-2" or "∞8").
PICTURE_WORD = re.compile(r"[^\W\d_]{2,}")
# Picture text of fewer words is a stray mark or label ("FIG. 3", "R"); two words already name
# something ("The end!" in a drawing, "PPT.ppt" under an embedded file's icon).
PICTURE_MIN_WORDS = 2


def _compact(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def _chart_labels(text: str) -> str:
    """Keep chart values and repeated labels: text alone cannot establish duplication."""
    return text


_MARKERS = set("◆◇■□▪▫●○▲△▼▽◀◄★☆♦◊")


def _clean_picture_text(blocks: list[dict]) -> None:
    """Preserve short labels, tick values, and repeated words at distinct positions.

    Only graphic marker glyphs and a whole caption repeated verbatim are suppressed here.
    Geometry-based duplicate ownership is settled before this presentation cleanup.
    """
    for b in blocks:
        if b["type"] != "picture" or not b["text"]:
            continue
        lines = [" ".join(t for t in ln.split() if not set(t) <= _MARKERS)
                 for ln in b["text"].split("\n")]
        def adjacent(o):
            return any(r["pageIndex"] == c["pageIndex"]
                       and min(r["bbox"]["x1"],c["bbox"]["x1"]) > max(r["bbox"]["x0"],c["bbox"]["x0"])
                       and max(r["bbox"]["y0"],c["bbox"]["y0"]) - min(r["bbox"]["y1"],c["bbox"]["y1"]) <= .04
                       for r in b["regions"] for c in o["regions"])
        captions = {" ".join(o["text"].split()) for o in blocks
                    if o["type"] == "caption" and adjacent(o)}
        b["text"] = "\n".join(ln for ln in lines if ln.strip() and ln not in captions)


# A table of contents line: a short title ending in its page number ("3. 변경 사항 이력 15",
# "Introduction ....... 4").
_TOC_LINE = re.compile(r"(?:\s|\.{2,}|·{2,}|…+)\(?(\d{1,4})\)?$")


def _mark_toc(blocks: list[dict]) -> None:
    """Lines of a table of contents are a list, not headings: read as headings, every one of them
    would start a chunk of its own."""
    def entry(b: dict) -> re.Match | None:
        # An entry at the very top or bottom of a page may have been taken for a running head
        # ("1.1. 수식 편집기의 기본 개념 5"); a bare page number never is one.
        kinds = ("section_header", "title", "paragraph", "list_item", *FURNITURE)
        return (b["type"] in kinds and len(b["text"]) < 120 and not _PAGE_NUMBER.fullmatch(b["text"].strip())
                and _TOC_LINE.search(b["text"].strip())) or None

    run: list[dict] = []

    def close() -> None:
        pages = [int(entry(b).group(1)) for b in run]
        rising = sum(a <= b for a, b in zip(pages, pages[1:]))
        if len(run) - sum(b["type"] in FURNITURE for b in run) >= 3 and len(run) >= 4 and rising >= 0.8 * (len(run) - 1):
            for b in run:
                b["type"], b["level"] = "list_item", None
        run.clear()

    for b in blocks:
        if b["type"] in FURNITURE and not entry(b):
            continue
        if entry(b) and (not run or (_first_page(b) == _first_page(run[-1]) and _first_page(b) is not None)):
            run.append(b)
            continue
        close()
        if entry(b):
            run.append(b)
    close()


def _split_toc_paragraphs(blocks: list[dict]) -> list[dict]:
    """A whole page of a table of contents read as one paragraph ("1.1.1. 수식에 쓰이는 글씨체 5
    1.1.1.1. 영문 글꼴 명령 5 …") becomes one item per line, as the entries are elsewhere."""
    out = []
    for b in blocks:
        lines = b.get("lines") or []
        parts = [b["text"][ln["start"]:ln["end"]] for ln in lines]
        found = [_TOC_LINE.search(p.strip()) for p in parts]
        numbers = [int(m.group(1)) for m in found if m]
        if not (b["type"] == "paragraph" and len(lines) >= 4
                and sum(bool(m) and len(re.findall(r"[^\W\d_]", p)) >= 2 for m, p in zip(found, parts)) >= 0.8 * len(parts)
                and sum(x <= y for x, y in zip(numbers, numbers[1:])) >= 0.8 * (len(numbers) - 1)):
            out.append(b)
            continue
        for ln, part in zip(lines, parts):
            line = {**ln, "start": 0, "end": len(part)}
            out.append({**b, "type": "list_item", "level": None, "text": part, "lines": [line],
                        "regions": [{"pageIndex": ln["pageIndex"], "bbox": ln["bbox"]}]})
    return out


def _join_line_pieces(blocks: list[dict]) -> list[dict]:
    """A line the layout model cut in two ("하한: T1a" | "= 86.8036×P⁰·³⁵⁷⁰") is one line: the right
    piece joins the left one. Read in column order, every left piece would come first and the
    pieces would be paired up wrongly."""
    def box(b: dict) -> dict | None:
        return b["regions"][0]["bbox"] if len(b["regions"]) == 1 and b["type"] == "paragraph" and not (b.get("_rotated_text") or b.get("_diagram_label")) else None

    gone: set[int] = set()
    for i, a in enumerate(blocks):
        ba = box(a)
        if i in gone or not ba or ba["y1"] - ba["y0"] > 0.025:
            continue
        for j, b in enumerate(blocks):
            bb = box(b)
            if j == i or j in gone or not bb or bb["y1"] - bb["y0"] > 0.025 or _first_page(a) != _first_page(b):
                continue
            overlap = min(ba["y1"], bb["y1"]) - max(ba["y0"], bb["y0"])
            ha, hb = ba["y1"] - ba["y0"], bb["y1"] - bb["y0"]
            # An exponent the layout model boxed on its own: smaller, raised, touching the line.
            raised = (hb <= 0.7 * ha and overlap > 0 and bb["x0"] - ba["x1"] <= 0.004 and bb["y1"] < ba["y1"] - 0.2 * ha
                      and re.fullmatch(r"[\d.,+\-−=()]+", b["text"].strip()))
            if 0 <= bb["x0"] - ba["x1"] <= 0.015 and (overlap >= 0.6 * min(ha, hb) or raised):
                joiner, piece = ("", b["text"].strip().translate(SUPERSCRIPT)) if raised else (" ", b["text"])
                shift = len(a["text"]) + len(joiner)
                a["text"] = f"{a['text']}{joiner}{piece}"
                a["regions"] = [{**a["regions"][0], "bbox": {"x0": ba["x0"], "y0": min(ba["y0"], bb["y0"]),
                                                             "x1": bb["x1"], "y1": max(ba["y1"], bb["y1"])}}]
                if a.get("lines") is not None and b.get("lines") is not None:
                    a["lines"] = a["lines"] + [{**ln, "start": ln["start"] + shift, "end": ln["end"] + shift} for ln in b["lines"]]
                else:
                    a["lines"] = None
                gone.add(j)
                ba = a["regions"][0]["bbox"]
    return [b for k, b in enumerate(blocks) if k not in gone]


_PAGE_NUMBER = re.compile(r"[-–—(\[]?\s*(?:page\s*)?\d{1,4}\s*(?:(?:/|of)\s*\d{1,4})?\s*[-–—)\]]?", re.IGNORECASE)


def _furniture_that_repeats(blocks: list[dict]) -> None:
    """In a document converted here, the layout model takes text at the top or bottom of a page for
    a running head or foot by its place alone ("데이터 구축으로 …" going on from the page before, a
    table's last row). A running head repeats on other pages; one that does not, and is no page
    number, is text."""
    def key(b: dict) -> str:
        return re.sub(r"\d+", "#", " ".join(b["text"].lower().split()))

    furniture = [b for b in blocks if b["type"] in ("page_header", "page_footer") and b["regions"]]
    pages: dict[str, set] = {}
    for b in furniture:
        pages.setdefault(key(b), set()).add(_first_page(b))
    if len({_first_page(b) for b in blocks if b["regions"]}) < 2:
        return
    for b in furniture:
        if len(pages[key(b)]) == 1 and not b.get("_source_furniture") and not _PAGE_NUMBER.fullmatch(b["text"].strip()):
            # A foot of its own page ("Odd page footer") does not run on into the next page.
            b["_foot"] = b["type"] == "page_footer"
            b["type"], b["level"] = "paragraph", None


# A note's mark at the start of a line: "＊16", "*", "※", "†", "1)", "¹".
_NOTE_MARK = re.compile(r"^\s*(?:[*\uff0a\u203b\u2020\u2021]+\s*\d{0,3}|\d{1,3}\)|[\u00b9\u00b2\u00b3\u2070-\u2079]+)\s*\S")


def _notes_taken_for_footers(blocks: list[dict]) -> None:
    """A note at the foot of a page ("＊16 Alphabet (Google), Amazon, ...") is sometimes taken for
    the running foot by its place, and left out of the chunks with it. A foot starting with a
    note's mark that no other page repeats is a footnote."""
    def key(b: dict) -> str:
        return re.sub(r"\d+", "#", " ".join(b["text"].lower().split()))

    feet = [b for b in blocks if b["type"] == "page_footer" and b["regions"]]
    pages: dict[str, set] = {}
    for b in feet:
        pages.setdefault(key(b), set()).add(_first_page(b))
    for b in feet:
        if len(b["text"].strip()) >= 8 and _NOTE_MARK.match(b["text"]) and len(pages[key(b)]) == 1:
            b["type"], b["level"] = "footnote", None


def _decorations_that_repeat(blocks: list[dict], slides: bool = False) -> None:
    """An empty picture at the top or bottom of a page, drawn in the same place on another page (a
    corner ornament, a logo on every page), is furniture: in a chunk it is a box with nothing in
    it, and it takes a caption under it from the figure the caption names. On slides the template's
    pictures (a logo in a corner, a band down the edge) and short text boxes repeated in the same
    place on three slides or more can be furniture too. Repeated body illustrations
    remain pictures; size alone does not establish a template decoration."""
    band = 0.08

    def box(b: dict) -> dict:
        return b["regions"][0]["bbox"]

    def edge(b: dict) -> bool:
        return box(b)["y1"] <= band or box(b)["y0"] >= 1 - band

    def same_place(a: dict, b: dict) -> bool:
        return all(abs(box(a)[k] - box(b)[k]) <= 0.015 for k in ("x0", "y0", "x1", "y1"))

    def mark(b: dict) -> None:
        b["type"] = "page_header" if (box(b)["y0"] + box(b)["y1"]) / 2 <= 0.5 else "page_footer"
        b["level"] = None

    found = [b for b in blocks if b["type"] == "picture" and not b["text"].strip() and len(b["regions"]) == 1 and edge(b)]
    for b in found:
        if any(o is not b and _first_page(o) != _first_page(b) and same_place(o, b) for o in found):
            mark(b)
    if not slides:
        return

    def template_picture(b: dict) -> bool:
        bb = box(b)
        area = (bb["x1"] - bb["x0"]) * (bb["y1"] - bb["y0"])
        touches = min(bb["x0"], bb["y0"], 1 - bb["x1"], 1 - bb["y1"]) <= 0.03
        outer = bb['y1'] <= .15 or bb['y0'] >= .85 or bb['x1'] <= .1 or bb['x0'] >= .9
        return (outer and area <= 0.04) or (touches and area <= 0.15)

    def short_text(b: dict) -> bool:
        return b["type"] in ("paragraph", "caption", "list_item") and 0 < len(b["text"].split()) <= 2 and len(b["text"]) <= 24

    candidates = [b for b in blocks if len(b["regions"]) == 1 and b["type"] not in FURNITURE
                  and ((b["type"] == "picture" and template_picture(b)) or short_text(b))]
    repeated = [b for b in candidates
                if len({_first_page(o) for o in candidates if same_place(o, b) and o["type"] == b["type"]
                        and (b["type"] == "picture" or " ".join(o["text"].split()) == " ".join(b["text"].split()))}) >= 3]
    for b in repeated:
        mark(b)


def _slide_titles_that_repeat(blocks: list[dict]) -> None:
    """A slide title shown again on a later slide ("3 Research Questions" opening each part) is
    taken for a running head because it repeats, and left out with the slide's heading. Large type
    at the top of a few slides is a title; a deck title on most slides stays a running head."""
    slides = {_first_page(b) for b in blocks if b["regions"]}
    heads = [b for b in blocks if b["type"] == "page_header" and len(b["regions"]) == 1 and len(b["text"].split()) >= 2]
    seen: dict[str, set] = {}
    for b in heads:
        seen.setdefault(" ".join(b["text"].split()).lower(), set()).add(_first_page(b))
    for b in heads:
        bb = b["regions"][0]["bbox"]
        if bb["y1"] - bb["y0"] >= 0.045 and bb["y0"] <= 0.2 and len(seen[" ".join(b["text"].split()).lower()]) * 2 < len(slides):
            b["type"], b["level"] = "section_header", 1


def _notes_at_the_foot(blocks: list[dict]) -> None:
    """A footnote of a Word or Hangul file is often read as a paragraph ("\u00b9 In accordance with
    article 34 ...", its lines as paragraphs of their own), and then read between the text of the
    page and the text going on overleaf. A paragraph in the lower half of its page that starts with
    a note's mark, with only text below it on the page, is a footnote, and so is that text."""
    pages: dict[int, list[dict]] = {}
    for b in blocks:
        if b["regions"] and b["type"] not in FURNITURE:
            pages.setdefault(_first_page(b), []).append(b)
    for page in pages.values():
        for i, b in enumerate(page):
            bb = b["regions"][0]["bbox"]
            if b["type"] != "paragraph" or bb["y0"] < 0.5 or not _FOOT_MARK.match(b["text"]):
                continue
            rest = page[i + 1:]
            if all(o["type"] in ("paragraph", "footnote") and o["regions"][0]["bbox"]["y0"] >= bb["y0"] - 0.005 for o in rest):
                for o in [b, *rest]:
                    o["type"], o["level"] = "footnote", None
                break


# A footnote's own mark: a raised digit, or an asterisk or dagger with or without a number ("\uff0a7").
_FOOT_MARK = re.compile(r"^\s*(?:[\u00b9\u00b2\u00b3\u2070-\u2079]+|[*\uff0a\u2020\u2021]+\s*\d{0,3})\s*\S")


def _repeated_running_text(blocks: list[dict]) -> None:
    """A running head or foot read as text ("Even page footer", a chapter title over every page)
    goes into the chunks, and runs on into the text next to it. Short text at the very top or
    bottom of pages, the same (but for its numbers) in the same band on three pages or more, is
    furniture."""
    def key(b: dict) -> str:
        return re.sub(r"\d+", "#", " ".join(b["text"].lower().split()))

    def band(b: dict) -> str | None:
        bb = b["regions"][0]["bbox"]
        return "top" if bb["y1"] <= 0.09 else "bottom" if bb["y0"] >= 0.91 else None

    # Copies already taken for running heads count too ("Even page footer" found on pages 2 and 4).
    found = [b for b in blocks if b["type"] in ("paragraph", "section_header", "caption", *FURNITURE) and len(b["regions"]) == 1
             and len(b["text"]) <= 100 and band(b)]
    seen: dict[tuple, set] = {}
    for b in found:
        seen.setdefault((band(b), key(b)), set()).add(_first_page(b))
    def alternating(b):
        own = seen[(band(b), key(b))]
        if len(own) < 2 or len({p % 2 for p in own}) != 1:
            return False
        bb = b["regions"][0]["bbox"]
        aligned = [o for o in found if band(o) == band(b)
                   and abs(o["regions"][0]["bbox"]["y0"]-bb["y0"]) <= .01
                   and abs(o["regions"][0]["bbox"]["y1"]-bb["y1"]) <= .01]
        aligned_pages = {}
        for other in aligned:
            aligned_pages.setdefault(key(other), set()).add(_first_page(other))
        if not own <= aligned_pages.get(key(b), set()):
            return False
        parity = next(iter(own)) % 2
        for other_key, pages_at_position in aligned_pages.items():
            peers = seen[(band(b), other_key)]
            if other_key != key(b) and len(peers) >= 2 and all(p % 2 != parity for p in peers) \
                    and peers <= pages_at_position:
                # Require interleaving pages, not two isolated footer-like body snippets.
                pages = sorted(own | peers)
                if len(pages) >= 4 and all(q-p == 1 for p,q in zip(pages,pages[1:])):
                    return True
        return False

    for b in found:
        # Three repeated pages or two interleaved parity groups at the same edge height.
        if b["type"] not in FURNITURE and (len(seen[(band(b), key(b))]) >= 3 or alternating(b)):
            b["type"] = "page_header" if band(b) == "top" else "page_footer"
            b["level"] = None


def _one_page_scan_heads(blocks: list[dict]) -> None:
    """A picture of one page has no running heads: a line at its top or bottom taken for one (the
    first line of a cut-out page, a bullet item) is text, unless it is a page number."""
    for b in blocks:
        if b["type"] in FURNITURE and not _PAGE_NUMBER.fullmatch(b["text"].strip()) and len(b["text"].split()) >= 2:
            b["type"], b["level"] = "paragraph", None


def _late_blocks_in_place(blocks: list[dict], columns: set[int]) -> list[dict]:
    """A block read after the text of a later page goes back to its place on its own page, between
    the blocks above and below it, where that page is one column. Layout models read a list running
    over pages as one group, and the tables set between its items after all of it. So does a block
    read before the text of an earlier page (a word OCR found under a drawing three pages on)."""
    def page(b: dict) -> int:
        return b["regions"][0]["pageIndex"]

    def top(b: dict) -> float:
        return b["regions"][0]["bbox"]["y0"]

    def placed(b: dict) -> bool:
        return bool(b["regions"]) and b["type"] not in FURNITURE

    # Read too early: a few blocks of a later page between the text of earlier ones.
    blocks = list(blocks)
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if not placed(b) or page(b) in columns:
            i += 1
            continue
        run = [k for k in range(i, min(i + 4, len(blocks))) if placed(blocks[k])]
        run = run[:next((n for n, k in enumerate(run) if page(blocks[k]) != page(b)), len(run))]
        before = next((page(x) for x in reversed(blocks[:i]) if placed(x)), None)
        after = next((x for x in blocks[run[-1] + 1:] if placed(x)), None)
        # Early, not the blocks after it late: more of the earlier pages follow it than it holds.
        earlier = 0
        for x in blocks[run[-1] + 1:]:
            if placed(x):
                if page(x) >= page(b):
                    break
                earlier += 1
        if len(run) <= 3 and earlier > len(run) and before is not None and after is not None \
                and before < page(b) and before <= page(after) < page(b):
            moved = [blocks[k] for k in run]
            rest = [x for n, x in enumerate(blocks) if n not in run]
            at = next((n for n, x in enumerate(rest) if n >= i and placed(x) and (
                page(x) > page(b) or (page(x) == page(b) and top(x) > top(b)))), len(rest))
            blocks = rest[:at] + moved + rest[at:]
            continue
        i += 1
    out: list[dict] = []
    reached = -1
    for b in blocks:
        if not placed(b):
            out.append(b)
            continue
        if page(b) < reached:
            if page(b) in columns:
                # In its column: before the first block of its page below it in that column, else
                # after the last block of its page.
                bb = b["regions"][0]["bbox"]

                def below(x: dict) -> bool:
                    xb = x["regions"][0]["bbox"]
                    width = min(xb["x1"], bb["x1"]) - max(xb["x0"], bb["x0"])
                    return xb["y0"] > bb["y0"] and width > 0.3 * min(xb["x1"] - xb["x0"], bb["x1"] - bb["x0"])

                mine = [i for i, x in enumerate(out) if placed(x) and page(x) == page(b)]
                at = next((i for i in mine if below(out[i])), mine[-1] + 1 if mine else None)
                if at is None:
                    at = next((i for i, x in enumerate(out) if placed(x) and page(x) > page(b)), len(out))
            else:
                at = next((i for i, x in enumerate(out) if x["regions"] and (
                    page(x) > page(b) or (page(x) == page(b) and x["type"] not in FURNITURE and top(x) > top(b)))), len(out))
            out.insert(at, b)
        else:
            out.append(b)
        reached = max(reached, b["regions"][-1]["pageIndex"])
    return out


def _one_region_each(blocks: list[dict]) -> list[dict]:
    """On a workbook's pages a text block over several regions is cells the layout model joined
    ("Comment" and the footer "Page 4") or a cell the page edge cut: one block per region, each
    with its own lines, when the lines say which text is where."""
    out = []
    for b in blocks:
        regions, lines = b["regions"], b.get("lines")
        if len(regions) < 2 or not lines or b["type"] not in ("paragraph", "caption", "section_header", "list_item"):
            out.append(b)
            continue

        def inside(ln: dict, region: dict) -> bool:
            box, x, y = region["bbox"], (ln["bbox"]["x0"] + ln["bbox"]["x1"]) / 2, (ln["bbox"]["y0"] + ln["bbox"]["y1"]) / 2
            return ln["pageIndex"] == region["pageIndex"] and box["x0"] - 0.005 <= x <= box["x1"] + 0.005 \
                and box["y0"] - 0.005 <= y <= box["y1"] + 0.005

        parts = [[ln for ln in lines if inside(ln, r)] for r in regions]
        spans = [(min(ln["start"] for ln in p), max(ln["end"] for ln in p)) if p else None for p in parts]
        ordered = sorted(sp for sp in spans if sp)
        if None in spans or sum(map(len, parts)) != len(lines) or any(a[1] > c[0] for a, c in zip(ordered, ordered[1:])):
            out.append(b)
            continue
        for region, part, (start, end) in zip(regions, parts, spans):
            out.append({**b, "text": b["text"][start:end], "regions": [region],
                        "lines": [{**ln, "start": ln["start"] - start, "end": ln["end"] - start} for ln in part]})
    return out


def _mark_page_numbers(blocks: list[dict]) -> None:
    """A page number ("3", "- 3 -", "Page 3 of 20") at the top or bottom of the page is a running
    mark, also when the layout model took it for a paragraph."""
    for b in blocks:
        if b["type"] in ("paragraph", "list_item", "caption", "section_header") and len(b["regions"]) == 1 \
                and _PAGE_NUMBER.fullmatch(b["text"].strip()):
            box = b["regions"][0]["bbox"]
            # A number that says it is one ("Page 3") may sit further in (a workbook's footer).
            margin = 0.15 if re.search(r"page", b["text"], re.IGNORECASE) else 0.08
            if box["y0"] > 1 - margin or box["y1"] < margin:
                b["type"], b["level"] = ("page_footer" if box["y0"] > 0.5 else "page_header"), None


def _prose(text: str) -> bool:
    """Sentences, not code: several sentence ends and hardly any of code's symbols."""
    symbols = len(re.findall(r"[{}();=<>\[\]_#$\\/*+|&]", text))
    return len(re.findall(r"[a-z]\.\s+[A-Z]", text)) >= 5 and symbols <= 0.01 * len(text)


def _without_proof_marks(blocks: list[dict]) -> list[dict]:
    """An end-of-proof box set at the right end of the proof's last line says nothing to search
    for; as a block of its own it made the page look like two columns (so its order was not
    fixed) and opened the next paragraph ("□ Proof. The second statement...")."""
    lines = [(r["pageIndex"], r["bbox"]) for b in blocks if b["type"] not in FURNITURE for r in b["regions"]]

    def at_line_end(b: dict) -> bool:
        if len(b["regions"]) != 1 or b["text"].strip() not in ("□", "■", "∎"):
            return False
        page, box = b["regions"][0]["pageIndex"], b["regions"][0]["bbox"]
        return any(p == page and o["x1"] <= box["x0"] + 0.003 and min(o["y1"], box["y1"]) - max(o["y0"], box["y0"]) > 0
                   for p, o in lines)

    return [b for b in blocks if not at_line_end(b)]


def _without_letter_labels(blocks: list[dict]) -> list[dict]:
    """One physical label belongs to its picture; equal text elsewhere is not a duplicate."""
    pictures = [b for b in blocks if b["type"] == "picture"]
    def duplicate(b: dict) -> bool:
        text = b["text"].strip()
        if b["type"] != "paragraph" or not re.fullmatch(r"(?:[^\W_]|[+−-]?\d{1,8}(?:[.,]\d{1,4})?)", text) or len(b["regions"]) != 1:
            return False
        for picture in pictures:
            if not all(_on_picture(r, [(pr["pageIndex"], pr["bbox"]) for pr in picture["regions"]]) for r in b["regions"]):
                continue
            # A full-page background is not evidence that a raised exponent is
            # a separate diagram label. Native ownership checks below still apply.
            if all((pr['bbox']['x1']-pr['bbox']['x0'])*(pr['bbox']['y1']-pr['bbox']['y0']) < .85
                   for pr in picture['regions']):
                b["_diagram_label"] = True
            box = b["regions"][0]["bbox"]
            area = (box["x1"]-box["x0"])*(box["y1"]-box["y0"])
            if picture["text"].strip() == text and len(picture["regions"]) == 1:
                pb = picture["regions"][0]["bbox"]
                if (pb["x1"]-pb["x0"])*(pb["y1"]-pb["y0"]) <= 3 * area:
                    return True
            for part in picture.get("_picture_parts", []):
                if part["text"].strip() == text and text in picture["text"].split() and all(
                    _on_picture(r, [(pr["pageIndex"], pr["bbox"]) for pr in part["regions"]]) for r in b["regions"]):
                    return True
            box = b["regions"][0]["bbox"]
            chars = [c.c for c in picture.get("_chars", [])
                     if box["x0"] <= c.cx <= box["x1"] and box["y0"] <= c.cy <= box["y1"]]
            if "".join(chars).strip() == text and text in picture["text"].split():
                return True
        return False
    return [b for b in blocks if not duplicate(b)]


def _on_picture(region: dict, pictures: list[tuple[int, dict]]) -> bool:
    """Most of the region lies inside a picture on its page."""
    box = region["bbox"]
    area = max(1e-9, (box["x1"] - box["x0"]) * (box["y1"] - box["y0"]))
    return any(page == region["pageIndex"]
               and max(0.0, min(box["x1"], p["x1"]) - max(box["x0"], p["x0"])) * max(0.0, min(box["y1"], p["y1"]) - max(box["y0"], p["y0"]))
               >= 0.5 * area for page, p in pictures)


def _side_by_side_pages(blocks: list[dict], pictures: bool = True) -> set[int]:
    """Pages where two blocks sit side by side (columns, a sidebar, a picture beside text): on
    those the layout model's order is kept. pictures=False leaves pictures out."""
    by_page: dict[int, list[dict]] = {}
    skip = {"formula"} if pictures else {"formula", "picture"}
    drawn = [(b["regions"][0]["pageIndex"], b["regions"][0]["bbox"]) for b in blocks if b["type"] == "picture" and b["regions"]]
    for b in blocks:
        # Formulas are often cut into pieces side by side ("E(e) = {" beside its cases); text set on
        # a picture (a logo's name, a label) is part of it, not a column.
        if b["regions"] and b["type"] not in FURNITURE and b["type"] not in skip \
                and not (b["type"] != "picture" and _on_picture(b["regions"][0], drawn)):
            by_page.setdefault(b["regions"][0]["pageIndex"], []).append(b["regions"][0]["bbox"])
    out = set()
    for page, boxes in by_page.items():
        for i, a in enumerate(boxes):
            if any(min(a["y1"], b["y1"]) - max(a["y0"], b["y0"]) > 0.003
                   and (b["x0"] >= a["x1"] - 0.003 or a["x0"] >= b["x1"] - 0.003) for b in boxes[i + 1:]):
                out.add(page)
                break
    return out


def _order_simple_pages(blocks: list[dict], complex_pages: set[int], picture_pages: frozenset[int] = frozenset()) -> list[dict]:
    """On a page of blocks stacked one above another, reading order is top to bottom. Layout
    models sometimes leave a few pieces (a formula part, a short line) at the end of such a page
    or read two formulas in a row over the sentence between them; sorting by position fixes both.
    On `picture_pages` the text is stacked but a picture sits beside it (a slide's decoration down
    the left edge, a photo filling the right half): the text is put in order around the pictures,
    which keep their places with their captions (figures side by side each keep their own)."""
    out, run = [], []

    def flush():
        page = run[0]["regions"][0]["pageIndex"] if run and run[0]["regions"] else None
        if page is not None and page not in complex_pages and all(b["regions"] for b in run):
            top = lambda b: next(r["bbox"]["y0"] for r in b["regions"] if r["pageIndex"] == page)  # noqa: E731
            if page in picture_pages:
                slots = [i for i, b in enumerate(run) if b["type"] not in FURNITURE | {"picture", "caption", "formula"}]
                for i, b in zip(slots, sorted((run[i] for i in slots), key=top)):
                    run[i] = b
            else:
                run.sort(key=top)
        out.extend(run)
        run.clear()

    for b in blocks:
        page = b["regions"][0]["pageIndex"] if b["regions"] else None
        if run and (page is None or page != (run[0]["regions"][0]["pageIndex"] if run[0]["regions"] else None)):
            flush()
        run.append(b)
    flush()
    return out


def _figures_in_place(blocks: list[dict]) -> list[dict]:
    """Layout models read a page column by column: a figure beside another comes after the text
    under both, and the source line under a figure after the rest of the page. A figure is read with
    its pieces (a caption right above or under it, a short line under it, labels inside it), and a
    figure beside an earlier one, with text below both read between them, right after that one.
    Figures themselves never move before text."""
    def box(b: dict) -> dict:
        return b["regions"][0]["bbox"]

    def page(b: dict) -> int | None:
        return b["regions"][0]["pageIndex"] if b["regions"] else None

    def titled(m: dict) -> bool:
        # Captions are only marked later: a figure's title may still be read as a paragraph or heading.
        return m["type"] == "caption" or (m["type"] in ("paragraph", "section_header") and len(m["text"]) <= 300
                                          and bool(CAPTION.match(m["text"]) or BRACKETED_TITLE.match(m["text"])))

    def piece(m: dict, fig: dict, above: bool) -> bool:
        if len(m["regions"]) != 1 or not (titled(m) or m["type"] == "paragraph") or page(m) != page(fig):
            return False
        if m["type"] == "paragraph" and not titled(m) and len(m["text"]) > 150:
            return False
        mb, fb = box(m), box(fig)
        if fb["x0"] <= (mb["x0"] + mb["x1"]) / 2 <= fb["x1"] and fb["y0"] <= (mb["y0"] + mb["y1"]) / 2 <= fb["y1"]:
            return True
        if min(mb["x1"], fb["x1"]) - max(mb["x0"], fb["x0"]) < 0.5 * (mb["x1"] - mb["x0"]):
            return False
        gap = fb["y0"] - mb["y1"] if above else mb["y0"] - fb["y1"]
        return -0.005 <= gap <= 0.03

    def at(b: dict) -> int:
        return next(n for n, x in enumerate(order) if x is b)

    def beside(b1: dict, b2: dict) -> bool:
        overlap = min(b1["y1"], b2["y1"]) - max(b1["y0"], b2["y0"])
        apart = b2["x0"] >= b1["x1"] - 0.01 or b1["x0"] >= b2["x1"] - 0.01
        return apart and overlap >= 0.5 * min(b1["y1"] - b1["y0"], b2["y1"] - b2["y0"])

    order = list(blocks)
    for pg in sorted({page(b) for b in order if page(b) is not None}):
        groups: list[tuple[list[dict], dict]] = []
        claimed: set[int] = set()
        for f in [b for b in order if page(b) == pg and b["type"] in ("picture", "table") and len(b["regions"]) == 1]:
            # Its caption may come before another figure read in between (a QR code beside it).
            early: list[dict] = []
            for m in reversed(order[:at(f)]):
                if id(m) not in claimed and piece(m, f, above=True):
                    early.insert(0, m)
                elif not (m["type"] in FURNITURE or (m["type"] in ("picture", "table") and page(m) == pg)):
                    break
            late = [m for m in order[at(f) + 1:] if id(m) not in claimed and piece(m, f, above=False)]
            # Its title set above it but read after it goes in front of it.
            title = [m for m in order[at(f) + 1:] if id(m) not in claimed and titled(m)
                     and all(m is not x for x in late) and piece(m, f, above=True)]
            members = [*title, *early, f, *late]
            claimed.update(id(m) for m in members)
            band = {k: (min if k in ("x0", "y0") else max)(box(m)[k] for m in members) for k in ("x0", "y0", "x1", "y1")}
            if late or title:
                order = [b for b in order if all(b is not m for m in late + title)]
                order[at(f) + 1:at(f) + 1] = late
                first = at(early[0] if early else f)
                order[first:first] = title
            groups.append((members, band))

        for m1, b1 in groups:
            # The partner is the first figure read later that stands beside this one, past any
            # figure between them (an icon by a heading, a QR code).
            end1 = at(m1[-1]) + 1
            partner = next((g for g in sorted(groups, key=lambda g: at(g[0][0])) if at(g[0][0]) >= end1 and beside(b1, g[1])), None)
            if partner is None:
                continue
            m2, b2 = partner
            # Figures of one row (their tops level), with text read in between that runs under both
            # (in two columns of text, each figure's column goes on under it alone).
            if abs(b1["y0"] - b2["y0"]) > 0.05:
                continue
            bottom = max(b1["y1"], b2["y1"])
            between = order[end1:at(m2[0])]

            def under_both(b: dict) -> bool:
                bb = box(b)
                return bb["y0"] >= bottom - 0.005 and all(min(bb["x1"], f["x1"]) - max(bb["x0"], f["x0"]) > 0 for f in (b1, b2))

            if any(page(b) == pg and b["type"] not in FURNITURE and under_both(b) for b in between):
                order = order[:end1] + m2 + [b for b in order[end1:] if all(b is not m for m in m2)]
    return order


def _place_formulas_in_flow(blocks: list[dict]) -> None:
    """Put each display formula after the text above it in its column. Layout models sometimes
    read two display formulas one after the other although a sentence sits between them on the
    page ("... can be written as (2.2) ... The predictor Z_t is (2.3)"), which puts the second
    formula before the sentence that introduces it, and into the previous chunk; or read a formula
    after the text below it, which puts it after the sentence that follows it."""
    columns = _side_by_side_pages(blocks)
    i = 0
    while i < len(blocks):
        f = blocks[i]
        if f["type"] != "formula" or not f["regions"]:
            i += 1
            continue
        page, box = f["regions"][0]["pageIndex"], f["regions"][0]["bbox"]
        last = None
        for j in range(i + 1, len(blocks)):
            b = blocks[j]
            if b["type"] in FURNITURE:
                continue
            if not b["regions"] or b["regions"][0]["pageIndex"] != page:
                break
            other = b["regions"][0]["bbox"]
            # On a page with columns only a block sharing the formula's x-range is in its column.
            same_column = page not in columns or min(box["x1"], other["x1"]) - max(box["x0"], other["x0"]) > 0
            if not (same_column and other["y1"] <= box["y0"] + 0.005):
                break
            last = j
        if last is None:
            # Read after text drawn below it in its column: it goes back above that text, past
            # the other column's blocks, to just after the text above it.
            first = None
            for j in range(i - 1, -1, -1):
                b = blocks[j]
                if b["type"] in FURNITURE:
                    continue
                if not b["regions"] or any(r["pageIndex"] != page for r in b["regions"]):
                    break
                other = b["regions"][-1]["bbox"]
                if page in columns and min(box["x1"], other["x1"]) - max(box["x0"], other["x0"]) <= 0:
                    continue
                if other["y0"] < box["y1"] - 0.005:
                    break
                first = j
            if first is not None:
                blocks.insert(first, blocks.pop(i))
            i += 1
            continue
        blocks.insert(last, blocks.pop(i))


def _math_piece(text: str) -> bool:
    """Display math broken into pieces: a lone radical, big operator or delimiter, or a piece that
    starts with an operator (a paragraph never does; one may start with a parenthesis)."""
    stripped = text.strip()
    if 0 < len(stripped) <= 3 and all(c in MATH_DELIMITERS or c in MATH_OPERATORS or c == " " for c in stripped):
        return True
    return stripped[:1] in MATH_OPERATORS


MATH_OPERATORS = set("∑∏∫√ˆ˜⋃⋂⊎⋀⋁∐∮⊙⊕⊗⊔")
MATH_DELIMITERS = set("()[]{}⌊⌋⌈⌉⟨⟩|‖")


# Blocks a sentence can run past: footnotes and figures set at the bottom or top of a column.
_INTERRUPTIONS = FURNITURE | {"footnote", "picture", "caption", "table"}


def _next_column(prev: dict, b: dict) -> bool:
    """b starts higher on the same page and to the right of where prev ends: the next column."""
    if _first_page(b) != _last_page(prev):
        return False
    end, start = prev["regions"][-1]["bbox"], b["regions"][0]["bbox"]
    return start["x0"] >= end["x1"] - 0.02 and start["y0"] < end["y0"]


# A list item or a new point: never the rest of the line above.
_ITEM_START = re.compile(r"^\s*(?:[•●◦○◎◈◆◇♦■□▪▶►▷☞※✓√\-–—·*]|[ㅇo]\s|\(?\d{1,2}[.)]|[가-하][.)]|\([가-하]\)|[①-⑳])")


def _next_line(prev: dict, b: dict) -> bool:
    """b is the next line of prev's paragraph, split off into a block of its own (widely spaced
    lines of converted Hangul and Office documents): prev ends in a full line, and b starts
    right below it at the same left edge (or a hanging indent)."""
    # Latin text continues in lowercase, which the caller checks; this is for Korean.
    if not (HANGUL.match(prev["text"][-1:]) or HANGUL.match(b["text"][:1])):
        return False
    if _first_page(b) != _last_page(prev) or _ITEM_START.match(b["text"]):
        return False
    end, start = prev["regions"][-1]["bbox"], b["regions"][0]["bbox"]
    line = min(end["y1"] - end["y0"], start["y1"] - start["y0"])
    return (end["x1"] - end["x0"] > 0.4 and end["x1"] >= start["x1"] - 0.03
            and end["x0"] - 0.03 <= start["x0"] <= end["x0"] + 0.06
            and 0 <= start["y0"] - end["y1"] < 1.5 * line)


_LETTER = re.compile(r"[^\W\d_]")


def _merge_continuations(blocks: list[dict], slides: bool = False, flow: bool = False) -> list[dict]:
    """Join a paragraph that runs onto the next page or column into one block with several regions,
    also past footnotes, figures and running heads set in between.

    Only merges when the first part does not end a sentence and the second part continues it:
    it starts on a later page or at the top of the next column, or with a lowercase letter. A list
    item goes on the same way into a lowercase paragraph, or an item with no bullet on the page,
    after a page or column break. Nothing goes on past a table of a `flow` document (a paragraph
    of a Word or Hangul file never holds one), nor past a table or figure that runs over a page
    itself (a float never does).
    """
    out: list[dict] = []
    last_para: int | None = None
    titles = _figure_titles(blocks)
    for b in blocks:
        prev = out[last_para] if last_para is not None else None
        if id(b) in titles:
            prev = None
        broken = prev is not None and prev["regions"] and b["regions"] and (_first_page(b) > _last_page(prev) or _next_column(prev, b))
        if (
            prev is not None
            and not (prev.get("_rotated_text") or b.get("_rotated_text") or prev.get("_diagram_label") or b.get("_diagram_label"))
            # Nothing runs on from one slide to the next, nor across a page from or into a piece
            # without a single letter (a drawing's reference numerals "205 300"), nor from a foot.
            and not (b["regions"] and prev["regions"] and _first_page(b) != _last_page(prev)
                     and (slides or not _LETTER.search(prev["text"]) or not _LETTER.search(b["text"]) or prev.get("_foot")))
            and prev["regions"]
            and b["regions"]
            and not _TERMINAL.search(prev["text"])
            and ((prev["type"] == b["type"] == "paragraph"
                  and (broken or _next_line(prev, b) or b["text"][:1].islower() or _math_piece(b["text"])))
                 or ("list_item" in (prev["type"], b["type"]) and broken
                     # Hangul has no lowercase: a paragraph in Hangul goes on with an open item cut off
                     # mid-sentence (not a short title such as "o 추진 배경").
                     and ((b["type"] == "paragraph" and (b["text"][:1].islower()
                                                         or (HANGUL.match(b["text"][:1]) and len(prev["text"]) >= 40)))
                          # No marker on the page at all: the item goes on, capital or not ("…информировать" /
                          # "Государственного заказчика…").
                          or b.get("_bullet") is False)))
        ):
            # Korean justified text breaks lines inside words, also at a column or page break.
            head, tail = prev["text"].rsplit(" ", 1)[-1], b["text"].split(" ", 1)[0]
            joiner = "" if (HANGUL.match(head[-1:]) or head[-1:].isdigit()) and HANGUL.match(tail[:1]) and one_word(head, tail) else " "
            shift = len(prev["text"]) + len(joiner)
            prev["text"] = f"{prev['text']}{joiner}{b['text']}"
            prev["regions"] = prev["regions"] + b["regions"]
            if prev.get("lines") is not None and b.get("lines") is not None:
                prev["lines"] = prev["lines"] + [
                    {**ln, "start": ln["start"] + shift, "end": ln["end"] + shift} for ln in b["lines"]
                ]
            else:
                prev["lines"] = None
            continue
        out.append(b)
        if b["type"] in ("paragraph", "list_item"):
            last_para = len(out) - 1
        elif b["type"] not in _INTERRUPTIONS or (b["type"] in ("table", "picture") and (
                (flow and b["type"] == "table") or len({r["pageIndex"] for r in b["regions"]}) > 1)):
            last_para = None
    return out


def blocks_fingerprint(parser: str, parser_version: str, blocks: list[DocumentBlock]) -> str:
    payload = json.dumps(
        [parser, parser_version, [(b.id, b.type, b.text) for b in blocks]],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


_BULLET_START = re.compile(r"[•●◦○◎◈◆◇♦■□▪▫▶►▷☞※✓√·*➢➤▸❑◻◼→⇨➔‣⁃∙❖–—-]\s")


def block_markdown(block: dict) -> str | None:
    """Markdown for a raw block; None when it equals the plain text."""
    kind, text, level = block["type"], block["text"], block.get("level")
    if kind == "title":
        return f"# {text}"
    if kind == "section_header":
        return f"{'#' * min(6, (level or 1) + 1)} {text}"
    if kind == "list_item":
        marker = block.get("_marker")
        # A number the text still starts with ("[12] Pick-it") is not written twice, nor is a
        # bullet ("□ 지원 배경").
        if marker and text.startswith(marker):
            marker = None
        elif not marker and _BULLET_START.match(text):
            return text
        return f"{marker or '-'} {text}"
    if kind == "table":
        return text
    if kind == "code":
        return f"```\n{text}\n```"
    if kind == "formula" and text and block.get("_latex", True):
        return f"$${text}$$"
    # Text Markdown would take for a heading ("### " a sheet prints for a number its column is too
    # narrow for) stays text.
    if re.match(r"#{1,6}(?:\s|$)", text):
        return "\\" + text
    return None


# Hangul syllables from U+D400 on, where a mathematical letter (U+1D400-U+1D7FF) lands without
# its top bit, and the syllables before them, which any Korean text has.
_HIGH_SYLLABLES = re.compile("[\ud400-\ud7a3]")
_LOW_SYLLABLES = re.compile("[\uac00-\ud3ff]")


def _plain_math_letter(syllable: str) -> str:
    letter = chr(ord(syllable) + 0x10000)
    plain = unicodedata.normalize("NFKC", letter)
    return plain if unicodedata.name(letter, "").startswith("MATHEMATICAL") and len(plain) == 1 else syllable


def _truncated_math_letters(blocks: list[dict]) -> None:
    """Some PDFs map mathematical letters (U+1D446 for an italic S) to their code less the top
    bit, which is a Hangul syllable (U+D446). In a document without the Hangul syllables before
    U+D400, which Korean text always has, those syllables are the letters, written plain."""
    if not any(_HIGH_SYLLABLES.search(b["text"]) for b in blocks) or any(_LOW_SYLLABLES.search(b["text"]) for b in blocks):
        return
    for b in blocks:
        b["text"] = _HIGH_SYLLABLES.sub(lambda m: _plain_math_letter(m.group()), b["text"])


def finish(meta: dict, pages: list[dict], raw_blocks: list[dict]) -> ParsedDocument:
    """Shared post-processing, then stable ids in reading order.

    `raw_blocks` are dicts in reading order with type, text, level, regions, source and optional
    lines; keys starting with "_" are parser-internal hints (e.g. `_marker` for list numbers).
    """
    meta = dict(meta)
    source_furniture = meta.pop("_sourceFurniture", [])
    sheet = meta.get("sourceFormat") in SHEET_FORMATS
    if meta.get("sourceFormat") in (None, "pdf"):
        # (Converted documents are printed by LibreOffice, whose fonts map their letters right.)
        _truncated_math_letters(raw_blocks)
    if sheet:
        raw_blocks = _one_region_each(raw_blocks)
    _notes_taken_for_footers(raw_blocks)
    if source_furniture:
        from .source_furniture import mark_source_furniture
        mark_source_furniture(raw_blocks, source_furniture)
    if meta.get("sourceFormat") not in (None, "pdf"):
        _furniture_that_repeats(raw_blocks)
    if meta.get("sourceFormat") in IMAGE_FORMATS:
        _one_page_scan_heads(raw_blocks)
    elif not sheet and meta.get("sourceFormat") not in SLIDE_FORMATS:
        _repeated_running_text(raw_blocks)
        from .margin_furniture import mark_margin_furniture
        mark_margin_furniture(raw_blocks)
    if meta.get("sourceFormat") in FLOW_FORMATS:
        _notes_at_the_foot(raw_blocks)
    for b in raw_blocks:
        if b["type"] in ("paragraph", "caption", "section_header") and (_CONTINUED.fullmatch(b["text"].strip())
                                                                       or _DASH_CONTINUED.fullmatch(b["text"].strip())):
            b["type"] = "page_footer"
    _mark_page_numbers(raw_blocks)
    _decorations_that_repeat(raw_blocks, slides=meta.get("sourceFormat") in SLIDE_FORMATS)
    if meta.get("sourceFormat") in SLIDE_FORMATS:
        _slide_titles_that_repeat(raw_blocks)
    for b in raw_blocks:
        # Prose set apart (a scanned page turned a little) is sometimes taken for code.
        if b["type"] == "code" and _prose(b["text"]):
            b["type"] = "paragraph"
    if not sheet:
        raw_blocks = _without_letter_labels(raw_blocks)
    raw_blocks = _without_proof_marks(_join_line_pieces(raw_blocks))
    text_columns = _side_by_side_pages(raw_blocks, pictures=False)
    columns = _side_by_side_pages(raw_blocks)
    raw_blocks = _order_simple_pages(raw_blocks, text_columns, frozenset(columns - text_columns))
    if not sheet:
        raw_blocks = _late_blocks_in_place(raw_blocks, columns)
    raw_blocks = _figures_in_place(raw_blocks)
    _place_formulas_in_flow(raw_blocks)
    _mark_toc(raw_blocks)
    raw_blocks = _split_toc_paragraphs(raw_blocks)
    _mark_captions(raw_blocks)
    raw_blocks = _captions_before_figures(raw_blocks)
    _mark_patent_parties(raw_blocks)
    if meta.get("sourceFormat") in SLIDE_FORMATS:
        _mark_slide_labels(raw_blocks)
    _clean_picture_text(raw_blocks)
    if meta.get("sourceFormat") == "csv":
        # csv_body prints only source cells/titles, with no running furniture.
        # Layout guesses at a page edge must not hide those values from chunks.
        # Apply after furniture/page-number detection so numeric edge records
        # also survive; keep the usual classification for actual PDF documents.
        for b in raw_blocks:
            if b["type"] in ("page_header", "page_footer"):
                b["type"], b["level"] = "paragraph", None
    # The cells of a workbook do not run on into one another, on a page or over pages.
    merged = raw_blocks if sheet else _merge_continuations(raw_blocks, slides=meta.get("sourceFormat") in SLIDE_FORMATS,
                                                           flow=meta.get("sourceFormat") in FLOW_FORMATS)
    blocks = []
    for i, b in enumerate(merged):
        public = {k: v for k, v in b.items() if not k.startswith("_")}
        public["markdown"] = block_markdown(b)
        if public.get("lines"):
            public.setdefault("lineGeometry", "exact")
        else:
            public.pop("lines", None)
        block = DocumentBlock.model_validate({**public, "id": f"b{i:04d}", "readingOrder": i})
        block._table_ocr_cells = b.get("_table_ocr_cells", [])
        block._table_layout_text = b.get("_table_layout_text")
        blocks.append(block)
    parsed = ParsedDocument.model_validate(
        {
            "document": meta,
            "pages": pages,
            "blocks": blocks,
            "blocksFingerprint": blocks_fingerprint(meta["parser"], meta["parserVersion"], blocks),
        }
    )

    normalize_line_geometry(parsed)
    return parsed


def normalize_line_geometry(parsed: ParsedDocument) -> None:
    """Finalize geometry after all text merges/restoration, without inventing exact positions."""
    for block in parsed.blocks:
        if not block.lines:
            if block.line_geometry in ("exact", "estimated"):
                block.line_geometry = "unavailable"
            continue
        size = len(block.text.encode("utf-16-le")) // 2 if parsed.offset_encoding == "utf16" else len(block.text)
        valid = all(0 <= line.start < line.end <= size and line.page_index < len(parsed.pages)
                    and len(line.xs) == line.end - line.start
                    and all(math.isfinite(x) and 0 <= x <= 1 for x in line.xs) for line in block.lines)
        if not valid or block.line_geometry == "unavailable":
            block.lines = None
            block.line_geometry = "unavailable"
        elif block.line_geometry is None:
            block.line_geometry = "unverified"
