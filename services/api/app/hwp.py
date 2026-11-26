"""HWP 5.x (the binary, OLE-based format) to HTML, from pyhwp's record models.

Produces the same HTML as hwpx.py: paragraphs, outline headings, font sizes, tables with merged
cells (nested too), pictures, text boxes, captions and footnotes. pyhwp's own XHTML export runs an
XSLT pass that took minutes on large documents; reading the records takes seconds.

Records come as a flat list with nesting levels. A list header (table cell, text box, caption,
footnote) is followed by its paragraphs at the same level, so cells are rebuilt from that order.
"""
import html
import re
from collections import Counter
from pathlib import Path

from .hangul_pictures import EmbeddedPictures
from .hwpx import SOURCE_TABLES, cell_attrs, image_html, styled, table_html
from .parsers.base import HANCOM

OUTLINE_STYLE = re.compile(r"^(?:개요|outline|heading|제목)\s*(\d)$", re.I)
# Headers whose paragraphs follow them at the same level.
LIST_HEADERS = {"TableCell", "TextboxParagraphList", "GShapeObjectCaption", "TableCaption", "ListHeader"}
# Inline character controls (HWP 5 spec, "Characters"): what they stand for in text.
CHAR_CONTROLS = {9: " ", 10: "<br/>", 24: "-", 30: " ", 31: " "}
IMAGE_TYPES = {"png", "jpg", "jpeg", "gif", "bmp"}


class Node:
    __slots__ = ("name", "content", "children", "payload")

    def __init__(self, name: str, content: dict, payload: bytes = b""):
        self.name, self.content, self.children, self.payload = name, content, [], payload


def build_tree(models) -> list[Node]:
    roots: list[Node] = []
    stack: list[tuple[int, Node]] = []
    for m in models:
        node = Node(m["type"].__name__, m["content"], m.get("payload") or b"")
        level = m["level"]
        while stack and stack[-1][0] >= level:
            stack.pop()
        (stack[-1][1].children if stack else roots).append(node)
        stack.append((level, node))
    return roots


def equation_script(node: Node) -> str:
    """The equation's script (Hangul's equation language, e.g. "a over b"). pyhwp leaves the
    record unparsed: UINT32 attributes, WORD length, then the UTF-16 script."""
    script = node.content.get("script")
    if not script and len(node.payload) >= 6:
        size = int.from_bytes(node.payload[4:6], "little")
        script = node.payload[6:6 + 2 * size].decode("utf-16-le", "ignore")
    return (script or "").strip()


def lists(node: Node) -> list[tuple[Node, list[Node]]]:
    """(list header, its paragraphs) among a control's children."""
    out: list[tuple[Node, list[Node]]] = []
    for child in node.children:
        if child.name in LIST_HEADERS:
            out.append((child, []))
        elif child.name == "Paragraph" and out:
            out[-1][1].append(child)
    return out


class Renderer:
    def __init__(self, docinfo: list, bindata, pictures: EmbeddedPictures, page_width: int):
        self.bindata, self.page_width = bindata, page_width
        self.pictures = pictures
        self.container = 0
        self.footnotes: list[str] = []
        names = {"CharShape": [], "ParaShape": [], "Style": [], "BinData": []}
        for m in docinfo:
            if m["type"].__name__ in names:
                names[m["type"].__name__].append(m["content"])
        self.chars = [(c["basesize"] / 100, bool(c["charshapeflags"].bold), bool(c["charshapeflags"].italic))
                      for c in names["CharShape"]]
        self.paras = [int(p["parashapeflags"].level) + 1 if int(p["parashapeflags"].head_shape) == 1 else None
                      for p in names["ParaShape"]]
        self.styles = []
        for st in names["Style"]:
            m = OUTLINE_STYLE.match((st.get("name") or "").strip()) or OUTLINE_STYLE.match((st.get("local_name") or "").strip())
            self.styles.append(int(m.group(1)) if m else None)
        self.bins = [b.get("bindata", {}) for b in names["BinData"]]
        self.body_size = 10.0

    def char(self, char_id: int) -> tuple[float, bool, bool]:
        return self.chars[char_id] if 0 <= char_id < len(self.chars) else (self.body_size, False, False)

    def runs(self, para: Node):
        """(text, char shape id) pieces of a paragraph, controls resolved."""
        text = next((c for c in para.children if c.name == "ParaText"), None)
        shapes = next((c.content.get("charshapes", []) for c in para.children if c.name == "ParaCharShape"), [])
        if text is None:
            return
        for (start, end), value in text.content.get("chunks", []):
            if isinstance(value, str):
                # Split where the character shape changes (positions count UTF-16 units, so a text
                # with characters outside the BMP is not split).
                inside = (pos for pos, _ in shapes if start < pos < end) if len(value) == end - start else ()
                cuts = sorted({start, end, *inside})
                for a, b in zip(cuts, cuts[1:]):
                    shape = max(((pos, sid) for pos, sid in shapes if pos <= a), default=(0, -1))[1]
                    yield html.escape(value[a - start:b - start] if b - a < len(value) else value), shape
            elif isinstance(value, dict) and value.get("code") in CHAR_CONTROLS:
                yield CHAR_CONTROLS[value["code"]], -1

    def paragraph(self, para: Node) -> str:
        inline = "".join(styled(t, *self.char(sid), self.body_size) if sid >= 0 else t for t, sid in self.runs(para)).strip()
        blocks = []
        for child in para.children:
            if child.name == "TableControl":
                blocks.append(self.table(child))
            elif child.name == "GShapeObjectControl":
                blocks.append(self.shape(child, child))
            elif child.name in ("FootNote", "EndNote"):
                self.footnotes.append("".join(self.paragraphs(ps) for _, ps in lists(child)))
            elif child.name == "Dutmal" and child.content.get("maintext"):
                # Ruby text: the main text with its gloss in parentheses.
                main, sub = html.escape(child.content["maintext"]), html.escape(child.content.get("subtext") or "")
                inline += f" {main}({sub})" if sub else f" {main}"
            elif child.name in ("Control", "EqEdit"):
                # Equations (and other controls pyhwp does not model): script and caption.
                for eq in (n for n in _walk([child]) if n.name == "EqEdit"):
                    if script := equation_script(eq):
                        inline += f" <code>{html.escape(script)}</code>"
                blocks += [self.paragraphs(ps) for header, ps in lists(child) if header.name == "ListHeader"]
        pshape, style = para.content.get("parashape_id", -1), para.content.get("style_id", -1)
        level = (self.paras[pshape] if 0 <= pshape < len(self.paras) else None) or (
            self.styles[style] if 0 <= style < len(self.styles) else None)
        out = ""
        if inline and inline != "<br/>":
            tag = f"h{min(6, level)}" if level else "p"
            out = f"<{tag}>{inline}</{tag}>"
        return out + "".join(blocks)

    def paragraphs(self, paras: list[Node]) -> str:
        return "".join(self.paragraph(p) for p in paras)

    def table(self, ctrl: Node) -> str:
        width = ctrl.content.get("width", 0)
        rows: dict[int, list[tuple[str, str]]] = {}
        for cell, paras in lists(ctrl):
            if cell.name != "TableCell":
                continue
            c = cell.content
            # A nested table is sized against the cell that holds it.
            saved, self.container = self.container, c.get("width", 0)
            body = self.paragraphs(paras)
            self.container = saved
            attrs = cell_attrs(c.get("colspan", 1), c.get("rowspan", 1), c.get("width", 0), width)
            rows.setdefault(c.get("row", 0), []).append((attrs, body))
        caption = "".join(self.paragraphs(ps) for h, ps in lists(ctrl) if h.name == "TableCaption")
        return table_html([cells for _, cells in sorted(rows.items())], width, self.container or self.page_width) + caption

    def shape(self, node: Node, ctrl: Node) -> str:
        """Pictures, text boxes and captions anywhere inside a drawing object (groups nest)."""
        out = []
        for header, paras in lists(node):
            if header.name in ("TextboxParagraphList", "GShapeObjectCaption"):
                inner = self.paragraphs(paras)
                out.append(f'<div class="box">{inner}</div>' if header.name == "TextboxParagraphList" and inner else inner)
        for child in node.children:
            if child.name == "ShapePicture":
                out.append(self.picture(child, ctrl))
            elif child.name == "ShapeComponent":
                out.append(self.shape(child, ctrl))
        return "".join(out)

    def picture(self, pic: Node, ctrl: Node) -> str:
        index = pic.content.get("picture", {}).get("bindata_id", 0) - 1
        if not 0 <= index < len(self.bins):
            return ""
        info = self.bins[index]
        ext = str(info.get("ext", "")).lower()
        if ext not in IMAGE_TYPES or not info.get("storage_id"):
            return ""
        name = f"BIN{info['storage_id']:04X}.{info['ext']}"
        picture = self.pictures.get(name, lambda: self.bindata[name].open())
        if picture is None:
            return ""
        return image_html(picture.slot, ctrl.content.get("width", 0), ctrl.content.get("height", 0), self.container or self.page_width)


def first_records(sections, max_chars: int | None):
    """Records of each section, stopping at the first top-level paragraph after max_chars of text:
    the page limit shows only the beginning, and a 150,000-paragraph file took minutes to read."""
    seen = 0
    for section in sections:
        kept = []
        for m in section:
            if max_chars and seen > max_chars and m["level"] == 0 and m["type"].__name__ == "Paragraph":
                yield kept
                return
            if m["type"].__name__ == "ParaText":
                seen += sum(len(v) for _, v in m["content"].get("chunks", []) if isinstance(v, str))
            kept.append(m)
        yield kept


def hwp_to_html(src: Path, workdir: Path, page_html, max_chars: int | None = None,
                pictures: EmbeddedPictures | None = None) -> Path:
    import logging

    from hwp5.binmodel import Hwp5File

    # pyhwp logs every unknown enum value it meets; nothing in them is useful here.
    logging.getLogger("hwp5").setLevel(logging.CRITICAL)
    SOURCE_TABLES.clear()
    hwp = Hwp5File(str(src))
    try:
        # A distribution document keeps its text in ViewText; BodyText only says to upgrade Hangul.
        storage = hwp.viewtext if hwp.header.flags.distributable else hwp.bodytext
        sections = list(first_records((storage.section(i).models() for i in storage.section_indexes()), max_chars))
        try:
            bindata = hwp["BinData"]
        except KeyError:
            bindata = {}
        return render(sections, list(hwp.docinfo.models()), bindata, workdir, page_html, pictures)
    finally:
        hwp.close()


def render(sections_models: list[list[dict]], docinfo: list[dict], bindata, workdir: Path, page_html,
           pictures: EmbeddedPictures | None = None) -> Path:
    """HTML for the record models of each section; bindata maps "BIN0001.png" to a stream."""
    sections = [build_tree(models) for models in sections_models]
    page_width = 0
    for roots in sections:
        page = _find(roots, "PageDef")
        if page is not None:
            c = page.content
            # Attribute bit 0: landscape, the paper's width and height swap.
            paper = c.get("height", 0) if c.get("attr", 0) & 1 else c.get("width", 0)
            page_width = max(0, paper - c.get("left_offset", 0) - c.get("right_offset", 0) - c.get("bookbinding_offset", 0))
            break
    renderer = Renderer(docinfo, bindata, pictures or EmbeddedPictures(workdir), page_width)
    # Body size: the most used character size in the text.
    usage: Counter = Counter()
    for roots in sections:
        for node in _walk(roots):
            if node.name == "Paragraph":
                for text, sid in renderer.runs(node):
                    usage[renderer.char(sid)[0]] += len(text)
    renderer.body_size = usage.most_common(1)[0][0] if usage else 10.0
    body = "".join(renderer.paragraph(p) for roots in sections for p in roots if p.name == "Paragraph")
    if renderer.footnotes:
        body += '<div class="footnotes">' + "".join(renderer.footnotes) + "</div>"
    if not re.sub(r"<[^>]+>", "", body).strip() and "<img" not in body:
        if not renderer.pictures.omitted:
            raise ValueError("no text found")
        body += "<p>[Pictures omitted]</p>"
    return renderer.pictures.write_page(page_html(body).translate(HANCOM))


def _walk(nodes: list[Node]):
    for node in nodes:
        yield node
        yield from _walk(node.children)


def _find(nodes: list[Node], name: str) -> Node | None:
    return next((n for n in _walk(nodes) if n.name == name), None)
