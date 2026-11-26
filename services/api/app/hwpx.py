"""HWPX (OWPML, a zip of XML) to HTML: paragraphs, outline headings, font sizes, tables with
merged cells (nested too), images, text boxes and footnotes. The HTML is then rendered to PDF by
LibreOffice; font sizes are kept so the PDF parser can still tell headings from body text.
"""
import html
import re
import zipfile
from collections import Counter
from pathlib import Path

from lxml import etree

from .hangul_pictures import EmbeddedPictures, MAX_IMAGE
from .parsers.base import HANCOM

PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, remove_comments=True)
HWPUNIT_PER_PX = 75  # 7200 units per inch, 96 px per inch
A4_TEXT_WIDTH = 42520  # 150 mm
OUTLINE_STYLE = re.compile(r"^(?:개요|outline|heading|제목)\s*(\d)$", re.I)
SHAPES = {"rect", "ellipse", "polygon", "curve", "arc", "container", "connectLine", "textart"}


def local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def children(el, name: str):
    return [c for c in el if local(c.tag) == name]


def first(el, name: str):
    return next((c for c in el.iter() if local(c.tag) == name), None)


class Header:
    """Character sizes/weights, paragraph outline levels and style names from header.xml."""

    def __init__(self, root):
        self.chars: dict[str, tuple[float, bool, bool]] = {}
        self.paras: dict[str, int | None] = {}
        self.styles: dict[str, int | None] = {}
        for el in root.iter():
            name = local(el.tag)
            if name == "charPr":
                height = int(el.get("height", "1000")) / 100
                self.chars[el.get("id")] = (height, first(el, "bold") is not None, first(el, "italic") is not None)
            elif name == "paraPr":
                heading = first(el, "heading")
                level = None
                if heading is not None and heading.get("type") == "OUTLINE":
                    level = int(heading.get("level", "0")) + 1
                self.paras[el.get("id")] = level
            elif name == "style":
                m = OUTLINE_STYLE.match((el.get("engName") or "").strip()) or OUTLINE_STYLE.match((el.get("name") or "").strip())
                self.styles[el.get("id")] = int(m.group(1)) if m else None


def _units(el, attr: str) -> int:
    value = el.get(attr, "") if el is not None else ""
    return int(value) if value.isdigit() else 0


def text_width(roots) -> int:
    """Width of the text area of the first section's page, in HWPUNIT (0 when unknown)."""
    for root in roots:
        page = first(root, "pagePr")
        if page is not None:
            margin = next((c for c in page if local(c.tag) == "margin"), None)
            return max(0, _units(page, "width") - _units(margin, "left") - _units(margin, "right") - _units(margin, "gutter"))
    return 0


def styled(text: str, size: float, bold: bool, italic: bool, body_size: float) -> str:
    if not text:
        return text
    if bold:
        text = f"<b>{text}</b>"
    if italic:
        text = f"<i>{text}</i>"
    if abs(size - body_size) >= 0.5:
        text = f'<span style="font-size:{size:.1f}pt">{text}</span>'
    return text


# A cell longer than this makes a table a layout (form) table rather than a data table, unless
# each of its rows fits in a chunk: a table is cut between rows, so only a row too long for one
# (a form's paragraphs in a cell) keeps it from being a table. About a token a character in Hangul.
DATA_CELL_CHARS = 100
DATA_ROW_CHARS = 400


# Data tables of the document being rendered, as grids of cell text, for
# the parser: a table read back from the PDF loses merged and multi-line cells; these are exact.
SOURCE_TABLES: list[list[list[str]]] = []


def cell_text(body: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>|</div>|</td>", " ", body)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


def table_grid(rows: list[list[tuple[str, str]]]) -> list[list[str]]:
    from .office_tables import fill_grid

    cells = [[(*[int(m.group(1)) if (m := re.search(rf'{k}="(\d+)"', attrs)) else 1 for k in ("colspan", "rowspan")],
               cell_text(body)) for attrs, body in row] for row in rows]
    # Like the page HTML, cells say "한" where Hancom's fonts draw it from a private-use code.
    return [[c.translate(HANCOM) for c in row] for row in fill_grid(cells)]


def table_html(rows: list[list[tuple[str, str]]], width: int, outer: int) -> str:
    """rows of (td attributes, cell HTML). Widths are kept as percentages: without them
    LibreOffice sizes tables to their longest line and cuts off what passes the right margin.

    Hangul documents use tables for layout: a 1x1 table frames a section, and forms put whole
    paragraphs in cells. Only tables of short cells or short rows get borders, so the layout parser
    reads them as tables; a frame becomes a box and a form is drawn without lines, so its text stays
    paragraphs that can be chunked instead of rows of thousands of characters."""
    cells = [body for row in rows for _, body in row]
    # A frame has no two filled cells side by side: one cell, or a title tab over a box with empty
    # spacer cells around them. It is drawn as one box holding what its cells hold.
    filled = [[body for _, body in row if "<img" in body or re.sub(r"<[^>]+>|&nbsp;|\s", "", body)] for row in rows]
    if all(len(row) <= 1 for row in filled):
        return '<div class="box">' + "".join(f"<div>{body}</div>" for row in filled for body in row) + "</div>"
    lengths = [[len(re.sub(r"<[^>]+>", "", body)) for _, body in row] for row in rows]
    longest = max((n for row in lengths for n in row), default=0)
    widest = max((sum(row) for row in lengths), default=0)
    border = '1' if longest <= DATA_CELL_CHARS or widest <= DATA_ROW_CHARS else '0'
    if border == '1':
        SOURCE_TABLES.append(table_grid(rows))
    share = min(100, round(100 * width / outer)) if width and outer else 100
    body = "".join("<tr>" + "".join(f"<td{attrs}>{cell}</td>" for attrs, cell in row) + "</tr>" for row in rows)
    return f'<table width="{share}%" border="{border}" cellspacing="0" cellpadding="4">{body}</table>'


def cell_attrs(colspan: int, rowspan: int, cell_width: int, table_width: int) -> str:
    attrs = f' colspan="{colspan}"' if colspan > 1 else ""
    attrs += f' rowspan="{rowspan}"' if rowspan > 1 else ""
    if cell_width and table_width:
        attrs += f' width="{max(1, round(100 * cell_width / table_width))}%"'
    return attrs


def image_html(slot: int, width: int, height: int, room: int) -> str:
    """An image sized from HWPUNIT, shrunk to fit the room it is in. LibreOffice ignores a width
    without a height and lays the image out at its pixel size, which can push a table past the
    right margin."""
    dims = ""
    width_px, height_px = width / HWPUNIT_PER_PX, height / HWPUNIT_PER_PX
    if width_px and height_px:
        # Without a page size in the document, the text width of an A4 page with Hangul's margins.
        room_px = (room or A4_TEXT_WIDTH) / HWPUNIT_PER_PX - 12
        scale = min(1.0, room_px / width_px) if room_px > 0 else 1.0
        dims = f' width="{max(8, round(width_px * scale))}" height="{max(8, round(height_px * scale))}"'
    return f'<p><img src="cc-picture-{slot}"{dims}/></p>'


class Renderer:
    def __init__(self, z: zipfile.ZipFile, header: Header, manifest: dict[str, str], pictures: EmbeddedPictures, body_size: float,
                 page_width: int = 0):
        self.z, self.header, self.manifest = z, header, manifest
        self.pictures = pictures
        self.body_size = body_size
        self.page_width = page_width
        self.container = 0
        self.footnotes: list[str] = []

    def text_of(self, t) -> str:
        parts = [html.escape(t.text or "")]
        for c in t:
            name = local(c.tag)
            parts.append({"lineBreak": "<br/>", "tab": " ", "nbSpace": " ", "fwSpace": " ", "hyphen": "-"}.get(name, ""))
            parts.append(html.escape(c.tail or ""))
        return "".join(parts)

    def styled(self, text: str, char_id: str | None) -> str:
        size, bold, italic = self.header.chars.get(char_id or "", (self.body_size, False, False))
        return styled(text, size, bold, italic, self.body_size)

    def paragraph(self, p) -> str:
        inline: list[str] = []
        blocks: list[str] = []
        for run in children(p, "run"):
            char_id = run.get("charPrIDRef")
            for c in run:
                name = local(c.tag)
                if name == "t":
                    inline.append(self.styled(self.text_of(c), char_id))
                elif name == "tbl":
                    blocks.append(self.table(c))
                elif name == "pic":
                    blocks.append(self.picture(c))
                elif name in SHAPES:
                    inner = [self.sublist(s) for s in c.iter() if local(s.tag) == "subList"]
                    if any(inner):
                        blocks.append(f'<div class="box">{"".join(inner)}</div>')
                elif name == "dutmal":
                    # Ruby text: the main text with its gloss in parentheses.
                    main, sub = first(c, "mainText"), first(c, "subText")
                    text = html.escape((main.text or "") if main is not None else "")
                    gloss = html.escape((sub.text or "") if sub is not None else "")
                    inline.append(self.styled(f"{text}({gloss})" if gloss else text, char_id))
                elif name == "equation":
                    script = first(c, "script")
                    if script is not None and (script.text or "").strip():
                        inline.append(f"<code>{html.escape(script.text.strip())}</code>")
                elif name == "ctrl":
                    for note in c:
                        if local(note.tag) in ("footNote", "endNote"):
                            self.footnotes.append(self.sublist(note))
        text = "".join(inline).strip()
        level = self.header.paras.get(p.get("paraPrIDRef")) or self.header.styles.get(p.get("styleIDRef"))
        out = ""
        if text:
            tag = f"h{min(6, level)}" if level else "p"
            out = f"<{tag}>{text}</{tag}>"
        return out + "".join(blocks)

    def sublist(self, el) -> str:
        sub = el if local(el.tag) == "subList" else next((c for c in el if local(c.tag) == "subList"), None)
        if sub is None:
            return ""
        return "".join(self.paragraph(p) for p in children(sub, "p"))

    def table(self, tbl) -> str:
        width = _units(next((c for c in tbl if local(c.tag) == "sz"), None), "width")
        rows = []
        for tr in children(tbl, "tr"):
            cells = []
            for tc in children(tr, "tc"):
                span = next((c for c in tc if local(c.tag) == "cellSpan"), None)
                cell_width = _units(next((c for c in tc if local(c.tag) == "cellSz"), None), "width")
                attrs = cell_attrs(_units(span, "colSpan") or 1, _units(span, "rowSpan") or 1, cell_width, width)
                # A nested table is sized against the cell that holds it.
                saved, self.container = self.container, cell_width
                cells.append((attrs, self.sublist(tc)))
                self.container = saved
            rows.append(cells)
        return table_html(rows, width, self.container or self.page_width)

    def picture(self, pic) -> str:
        img = first(pic, "img")
        ref = img.get("binaryItemIDRef") if img is not None else None
        href = self.manifest.get(ref or "")
        if not href:
            return ""
        try:
            info = self.z.getinfo(href)
        except KeyError:
            return ""
        if info.file_size > MAX_IMAGE:
            self.pictures.omitted = True
            return ""
        suffix = Path(href).suffix.lower()
        if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".bmp"):
            return ""
        picture = self.pictures.get(href, lambda: self.z.open(info))
        if picture is None:
            return ""
        # The first size that is set: curSz is often 0 x 0 next to the real sz.
        sizes = {local(c.tag): c for c in pic if local(c.tag) in ("curSz", "sz", "orgSz")}
        size = next((sizes[k] for k in ("curSz", "sz", "orgSz") if k in sizes and _units(sizes[k], "width") and _units(sizes[k], "height")), None)
        width, height = _units(size, "width"), _units(size, "height")
        if not (width and height):
            width, height = picture.width * HWPUNIT_PER_PX, picture.height * HWPUNIT_PER_PX
        return image_html(picture.slot, width, height, self.container or self.page_width)


def _manifest(z: zipfile.ZipFile) -> tuple[dict[str, str], list[str]]:
    """Item id -> path, and section paths in reading (spine) order."""
    items, spine = {}, []
    try:
        root = etree.fromstring(z.read("Contents/content.hpf"), PARSER)
        for el in root.iter():
            name = local(el.tag)
            if name == "item" and el.get("id") and el.get("href"):
                items[el.get("id")] = el.get("href")
            elif name == "itemref" and el.get("idref"):
                spine.append(el.get("idref"))
    except (KeyError, etree.XMLSyntaxError):
        pass
    sections = [items[i] for i in spine if re.match(r"Contents/section\d+\.xml$", items.get(i, ""))]
    if not sections:
        sections = sorted((n for n in z.namelist() if re.match(r"Contents/section\d+\.xml$", n)),
                          key=lambda n: int(re.findall(r"\d+", n)[-1]))
    return items, sections


def hwpx_to_html(src: Path, workdir: Path, page_html, max_chars: int | None = None,
                 pictures: EmbeddedPictures | None = None) -> Path:
    SOURCE_TABLES.clear()
    with zipfile.ZipFile(src) as z:
        header = Header(etree.fromstring(z.read("Contents/header.xml"), PARSER))
        manifest, sections = _manifest(z)
        roots = [etree.fromstring(z.read(s), PARSER) for s in sections]
        # Body size: the most used character size in the text.
        usage: Counter = Counter()
        for root in roots:
            for run in root.iter():
                if local(run.tag) == "run":
                    chars = sum(len(t.text or "") for t in run if local(t.tag) == "t")
                    usage[header.chars.get(run.get("charPrIDRef"), (10.0, False, False))[0]] += chars
        body_size = usage.most_common(1)[0][0] if usage else 10.0
        renderer = Renderer(z, header, manifest, pictures or EmbeddedPictures(workdir), body_size, text_width(roots))
        parts, seen = [], 0
        for p in (p for root in roots for p in children(root, "p")):
            # Only the beginning is shown under a page limit; stop well past it.
            if max_chars and seen > max_chars:
                break
            parts.append(renderer.paragraph(p))
            seen += sum(len(t.text or "") for t in p.iter() if local(t.tag) == "t")
        body = "".join(parts)
        if renderer.footnotes:
            body += '<div class="footnotes">' + "".join(renderer.footnotes) + "</div>"
    if not re.sub(r"<[^>]+>", "", body).strip() and "<img" not in body:
        if not renderer.pictures.omitted:
            raise ValueError("no text found")
        body += "<p>[Pictures omitted]</p>"
    return renderer.pictures.write_page(page_html(body).translate(HANCOM))


def hwpx_preview_text(src: Path) -> str:
    """The plain-text preview Hancom stores in every HWPX."""
    with zipfile.ZipFile(src) as z:
        data = z.read("Preview/PrvText.txt")
    for encoding in ("utf-16", "utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")
