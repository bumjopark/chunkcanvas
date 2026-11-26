"""Turn any supported upload into source.pdf. Runs as a subprocess (inside bubblewrap when
CC_PARSER_SANDBOX=bwrap) so a hostile document cannot reach other files or the network.

    python -m app.convert --doc-dir D --format docx [--soffice PATH] [--max-pages N]

Reads D/original.<ext>; writes D/source.pdf and D/conversion.json ({"notices": [...]}), or
D/error.json with a user-facing message. Office formats, HTML, Markdown, text, CSV and both
Hangul formats go through LibreOffice; images are wrapped with PyMuPDF.
"""
import argparse
import csv
import html
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from .formats import EXTENSIONS, FLAT, IMAGES, ODF, OFFICE, PACKAGES

# Writer is used for everything HTML-like, so generated documents render consistently.
FILTERS = {
    **{f: "writer_pdf_Export" for f in ("docx", "doc", "odt", "rtf", "sxw", "fodt", "wordml", "wpd", "pages", "hwp3")},
    **{f: "impress_pdf_Export" for f in ("pptx", "ppt", "odp", "sxi", "fodp", "key")},
    **{f: "calc_pdf_Export" for f in ("xlsx", "xls", "xlsb", "ods", "sxc", "fods", "xmlss", "numbers")},
    **{f: "draw_pdf_Export" for f in ("odg", "fodg")},
}
BASE_CSS = """
body { font-family: "Noto Sans CJK KR", "NanumGothic", "Noto Sans", "DejaVu Sans", sans-serif; font-size: 10.5pt; line-height: 1.45; }
h1 { font-size: 20pt; } h2 { font-size: 16pt; } h3 { font-size: 13pt; } h4, h5, h6 { font-size: 11pt; }
table { border-collapse: collapse; margin: 4pt 0; }
td, th { border: 1px solid #888; padding: 2pt 5pt; vertical-align: top; }
pre, code { font-family: "NanumGothicCoding", "Noto Sans Mono CJK KR", "DejaVu Sans Mono", monospace; font-size: 9.5pt; }
.box { border: 1px solid #aaa; padding: 4pt; margin: 4pt 0; }
.footnotes { font-size: 9pt; border-top: 1px solid #888; margin-top: 12pt; }
"""
MAX_CSV_ROWS = 3000
CHARS_PER_PAGE = 5000
# Wide tables and prose cells are more readable as labelled records.
MAX_CSV_COLUMNS = 10
MAX_CSV_CELL_CHARS = 512
MAX_IMAGE_PIXELS = 120_000_000


class ConversionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# A run of 12+ characters with no break opportunity (codes, numbers, long Hangul compounds).
LONG_RUN = re.compile(r"[^\s<>&;]{12,}")


def breakable(body: str) -> str:
    """Zero-width spaces every 6 characters inside long unbroken runs of text. LibreOffice widens a
    table column to its longest unbreakable word and cuts off what the table pushes past the right
    margin; the spaces draw nothing and do not appear in the PDF's text."""
    def split(m: re.Match) -> str:
        run, pieces, i = m.group(0), [], 0
        while i < len(run):
            j = min(i + 6, len(run))
            # Not between f and i (fl, ff, ...): the font draws those as one ligature, and a space
            # inside it comes out of the PDF as stray letters ("specifii c").
            while j < len(run) and run[j - 1] in "fF" and run[j] in "fijlt":
                j += 1
            pieces.append(run[i:j])
            i = j
        return "\u200b".join(pieces)

    return re.sub(r"(?<=>)[^<]+", lambda m: LONG_RUN.sub(split, m.group(0)), body)


def page_html(body: str, title: str = "") -> str:
    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
        f"<style>{BASE_CSS}</style></head><body>{breakable(body)}</body></html>"
    )


def decode_text(data: bytes) -> str:
    """UTF-16/UTF-8/CP949 first, then the most likely legacy code page (CP1252, CP866, EBCDIC...)."""
    from .formats import decode_guess

    text = decode_guess(data)
    return text if text is not None else data.decode("latin-1")


# --- HTML sanitizing ------------------------------------------------------------------------

DROP_TAGS = {
    "script", "style", "iframe", "frame", "frameset", "object", "embed", "link", "meta", "base", "form",
    "input", "button", "select", "textarea", "svg", "math", "noscript", "template", "audio", "video",
    "source", "track", "applet", "canvas", "portal",
}
URL_ATTRS = {"src", "href", "srcset", "background", "poster", "data", "action", "formaction", "lowsrc",
             "dynsrc", "longdesc", "cite", "codebase", "usemap", "xlink:href"}


# A saved web page's chrome says nothing of its document: menus and search (landmark roles), the
# site's banner and footer, and MediaWiki's edit links, tables of contents and link boxes.
CHROME_ROLES = {"navigation", "search", "banner", "contentinfo"}
CHROME_CLASSES = {"mw-editsection", "mw-jump-link", "noprint", "navbox", "catlinks", "printfooter", "mw-footer",
                  "vector-header", "vector-sticky-header", "vector-page-toolbar", "vector-menu", "vector-toc",
                  "vector-dropdown", "mw-portlet-lang", "interlanguage-link"}
CHROME_IDS = {"mw-navigation", "mw-head", "mw-panel", "footer", "toc", "catlinks"}


def _chrome(el) -> bool:
    return (el.tag.lower() == "nav" or el.get("role", "").lower() in CHROME_ROLES or el.get("id", "") in CHROME_IDS
            or not CHROME_CLASSES.isdisjoint(el.get("class", "").split()))


def sanitize_html(markup: str) -> str:
    """Keep the document's text and structure, drop anything that loads or runs something.
    Only inline data: images survive; every other URL is removed."""
    from lxml import html as lxml_html

    # XHTML may start with an XML declaration, which lxml refuses in a decoded string.
    markup = re.sub(r"^\s*<\?xml[^>]*\?>", "", markup or "")
    root = lxml_html.document_fromstring(markup if markup.strip() else "<p></p>")
    for el in [e for e in root.iter() if isinstance(e.tag, str) and (e.tag.lower() in DROP_TAGS or _chrome(e))]:
        el.drop_tree()
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for name in list(el.attrib):
            value, lname = el.attrib[name], name.lower()
            inline_image = lname == "src" and el.tag.lower() == "img" and value.strip().lower().startswith("data:image/")
            if lname.startswith("on") or (lname in URL_ATTRS and not inline_image):
                del el.attrib[name]
            elif lname == "style" and re.search(r"url\s*\(|expression|@import|behavior", value, re.I):
                del el.attrib[name]
    # Images that pointed elsewhere become their alt text.
    for img in [e for e in root.iter("img") if "src" not in e.attrib]:
        alt = img.attrib.get("alt", "").strip()
        if alt:
            span = img.makeelement("span", {})
            span.text = f"[{alt}]"
            span.tail = img.tail
            img.getparent().replace(img, span)
        else:
            img.drop_tree()
    body = root.find("body")
    return (html.escape(body.text or "") if body is not None else "") + "".join(
        lxml_html.tostring(child, encoding="unicode") for child in (body if body is not None else [root]))


# --- Markup formats -----------------------------------------------------------------------------


# LibreOffice draws table borders from the border attribute only, not from CSS; without them the
# layout parser reads a data table as lines of text.
DATA_TABLE = '<table border="1" cellspacing="0" cellpadding="4">'


def markdown_body(text: str) -> str:
    from markdown_it import MarkdownIt

    # HTML inside Markdown (a README's centred logo and badges) is markup, not text; it is
    # sanitized like any web page.
    md = MarkdownIt("commonmark", {"html": True}).enable("table").enable("strikethrough")
    return sanitize_html(md.render(text)).replace("<table>", DATA_TABLE)


def tab_separated(text: str) -> bool:
    """Plain text that is a table: lines with the same number of tabs (a .txt export of a sheet)."""
    lines = [ln for ln in text.splitlines()[:500] if ln.strip()]
    counts = [ln.count("\t") for ln in lines]
    common = max(set(counts), key=counts.count) if counts else 0
    return len(lines) >= 5 and common >= 2 and counts.count(common) >= 0.9 * len(lines)


def text_body(text: str) -> str:
    paragraphs = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br/>')}</p>" for p in paragraphs if p.strip())


def csv_header(rows: list[list[str]]) -> tuple[list[str], bool]:
    """Use row 1 as the column headings unless the file itself shows it is a record.

    Production always used row 1 as headings, and most CSV exports have one.
    Row 1 is kept as data only with evidence from the file, never from a list of
    expected field names: a typed value over the same type in its column
    (numbers, dates, e-mail, URLs), a row-1 value that recurs in its own column
    below, a prose cell, too few labelled columns, or no following record.
    A year over measures ("2022" above "10") is a heading, not evidence.
    """
    width = max(map(len, rows))
    first = rows[0] + [""] * (width - len(rows[0]))
    samples = [row + [""] * (width - len(row)) for row in rows[1:201]]
    numbered = [f"Column {i + 1}" for i in range(width)]

    def value_type(value):
        value = value.strip()
        if re.fullmatch(r"(?:[+\-\u2212\u25b3\u25b2$\u20ac\u00a3\u00a5\u20a9]?\d+(?:[.,]\d+)*(?:%|[eE][+-]?\d+)?|\(\d+(?:[.,]\d+)*\))", value):
            return 'number'
        if re.fullmatch(r'\d{2,4}[-/]\d{1,2}[-/]\d{1,4}(?:[ T].*)?', value):
            return 'date'
        if re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            return 'email'
        if re.match(r'https?://\S+$', value):
            return 'url'
        return None

    year = lambda value: bool(re.fullmatch(r'(?:19|20)\d{2}', value.strip()))
    if not samples or any(len(cell.strip()) > 80 for cell in first):
        # A prose first record is data, not a heading repeated on every page or record.
        return numbered, False
    active = [i for i in range(width) if first[i].strip() or any(row[i].strip() for row in samples)]
    labelled = [i for i in active if first[i].strip()]
    if not labelled or len(labelled) < .5 * len(active):
        return numbered, False
    folded = lambda row: tuple(cell.strip().casefold() for cell in row)
    # Some exports repeat the heading row; that is not a recurring data value.
    repeated_heading = any(folded(row) == folded(first) for row in samples)
    heading_votes = record_votes = 0
    for i in labelled:
        label = first[i].strip()
        below = [row[i].strip() for row in samples if row[i].strip()]
        if not below:
            continue
        kinds = [value_type(value) for value in below]
        typed = next((kind for kind in ('number', 'date', 'email', 'url')
                      if kinds.count(kind) >= .8 * len(kinds)), None)
        kind = value_type(label)
        if kind is None:
            if typed:
                heading_votes += 1
            elif not repeated_heading and label.casefold() in {value.casefold() for value in below}:
                record_votes += 1
        elif kind == typed and not (year(label) and not all(year(value) for value in below)):
            record_votes += 1
    if record_votes > heading_votes:
        return numbered, False
    return first, True


def csv_body(text: str) -> tuple[str, list[str]]:
    try:
        dialect = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    notices = []
    if len(rows) > MAX_CSV_ROWS:
        rows, notices = rows[:MAX_CSV_ROWS], ["csv_rows_truncated"]
    if not rows:
        return "<p></p>", notices
    prefix = ""
    # Spreadsheet exports sometimes put a title in one cell, blank rows, then
    # the actual field names. Preserve the title as prose instead of either
    # repeating it as a column name or hiding the real headings as data.
    if max(map(len, rows)) > 1:
        titles, blank_separator = [], False
        for index, row in enumerate(rows[:8]):
            filled = [cell for cell in row if cell.strip()]
            if not filled:
                blank_separator = bool(titles)
                continue
            if len(filled) == 1 and len(filled[0]) <= 200 and any(ch.isalpha() for ch in filled[0]):
                titles.append(filled[0])
                continue
            if titles and blank_separator and len(filled) >= 2 and csv_header(rows[index:])[1]:
                prefix = ''.join(f'<p>{html.escape(title)}</p>' for title in titles)
                rows = rows[index:]
            break
    labels, inferred = csv_header(rows)
    notices.append("csv_header_inferred" if inferred else "csv_header_unverified")
    data = rows[1:] if inferred else rows
    if max(map(len, rows)) > MAX_CSV_COLUMNS or any(len(cell) > MAX_CSV_CELL_CHARS for row in rows for cell in row):
        notices.append("csv_records")
        records = []
        for row in data:
            pairs = [f"<b>{html.escape(labels[i])}</b>: {html.escape(value)}" for i, value in enumerate(row) if value.strip()]
            if pairs:
                records.append("<p>" + " · ".join(pairs) + "</p>")
        return prefix + ("".join(records) or "<p></p>"), notices
    head = "<thead><tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in labels) + "</tr></thead>"
    trs = "".join("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in row) + "</tr>" for row in data)
    return prefix + f"{DATA_TABLE}{head}<tbody>{trs}</tbody></table>", notices


MAX_EPUB_CHAPTER = 20 * 1024 * 1024


def epub_body(src: Path) -> str:
    """The chapters of an EPUB in reading (spine) order, each sanitized like an HTML upload."""
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    with zipfile.ZipFile(src) as z:
        container = etree.fromstring(z.read("META-INF/container.xml"), parser)
        opf_path = next((el.get("full-path") for el in container.iter("{*}rootfile")), None)
        if not opf_path:
            raise ConversionError("corrupt_file", "The EPUB has no package document.")
        opf = etree.fromstring(z.read(opf_path), parser)
        base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
        items = {el.get("id"): el.get("href") for el in opf.iter("{*}item") if el.get("href")}
        spine = [items[el.get("idref")] for el in opf.iter("{*}itemref") if el.get("idref") in items]
        parts = []
        for href in spine:
            name = base + href.split("#", 1)[0]
            try:
                info = z.getinfo(name)
            except KeyError:
                continue
            if info.file_size > MAX_EPUB_CHAPTER:
                continue
            parts.append(sanitize_html(decode_text(z.read(info))))
    if not parts:
        raise ConversionError("no_text", "No text was found in this document.")
    return "".join(parts)


def iwork_preview(src: Path) -> bytes | None:
    """iWork '09 packages usually carry QuickLook/Preview.pdf, Apple's own rendering of the whole
    document: more faithful than LibreOffice's import, which also fails on some of them."""
    with zipfile.ZipFile(src) as z:
        name = next((n for n in z.namelist() if n.lower() == "quicklook/preview.pdf"), None)
        if name is None:
            return None
        data = z.read(name)
    return data if data.startswith(b"%PDF-") else None


def fixed_to_pdf(src: Path, out: Path, max_pages: int | None) -> None:
    """XPS/OXPS keep their own layout and fonts; MuPDF turns their pages into PDF pages."""
    import pymupdf

    try:
        doc = pymupdf.open(str(src), filetype="xps")
    except Exception as exc:  # noqa: BLE001
        raise ConversionError("corrupt_file", "The document is damaged.") from exc
    last = doc.page_count - 1 if not max_pages else min(doc.page_count, max_pages) - 1
    pdf = pymupdf.open("pdf", doc.convert_to_pdf(from_page=0, to_page=last))
    pdf.save(str(out))


# --- Office packages ----------------------------------------------------------------------------

DANGEROUS_FIELD = re.compile(r"\b(INCLUDETEXT|INCLUDEPICTURE|LINK|DDE|DDEAUTO|IMPORT)\b", re.I)
MACRO_PART = re.compile(r"(^|/)(vbaProject\.bin|vbaData\.xml|vbaProjectSignature\.bin)(\.rels)?$|^(Basic|Scripts)/", re.I)


def _package_reference(value: str, members: set[str], base: str = "", *, package_root: bool = False) -> bool:
    return bool(_package_paths(value, members, base, package_root=package_root)) or value.startswith('#')


def _package_paths(value: str, members: set[str], base: str = "", *, package_root: bool = False) -> list[str]:
    import posixpath
    from urllib.parse import unquote, urlsplit
    if value.startswith("#"):
        return []
    paths = []
    # Producers use both literal URI-escaped member names and decoded ZIP names.
    # Validate the decoded interpretation too before accepting either spelling.
    for candidate in dict.fromkeys((unquote(value), value)):
        try:
            url = urlsplit(candidate)
        except ValueError:
            return []
        if url.scheme or url.netloc or "\\" in candidate or (url.path.startswith("/") and not package_root):
            return []
        path = posixpath.normpath(posixpath.join(base, url.path).lstrip("/"))
        if path == '..' or path.startswith('../'):
            return []
        if path in members or any(name.startswith(path.rstrip('/') + '/') for name in members):
            paths.append(path)
    return paths


REL_NAMESPACES = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
}
PASSIVE_LINK_ELEMENTS = {
    f"{{{ns}}}{tag}" for ns, tag in (
        ("http://schemas.openxmlformats.org/wordprocessingml/2006/main", "hyperlink"),
        ("http://purl.oclc.org/ooxml/wordprocessingml/main", "hyperlink"),
        ("http://schemas.openxmlformats.org/drawingml/2006/main", "hlinkClick"),
        ("http://purl.oclc.org/ooxml/drawingml/main", "hlinkClick"),
        ("http://schemas.openxmlformats.org/spreadsheetml/2006/main", "hyperlink"),
        ("http://purl.oclc.org/ooxml/spreadsheetml/main", "hyperlink"),
    )
}
ODF_TEXT_LINKS = {"{urn:oasis:names:tc:opendocument:xmlns:text:1.0}a", "{http://openoffice.org/2000/text}a",
                  '{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}a'}


def _passive_url(value: str) -> bool:
    from urllib.parse import urlsplit
    if any(ord(c) < 32 for c in value):
        return False
    try:
        url = urlsplit(value)
        return value.startswith("#") or url.scheme.lower() in {"http", "https", "mailto", 'ftp'} or (
            not url.scheme and not url.netloc and bool(url.path) and '\\' not in value)
    except ValueError:
        return False


def sanitize_xml_text(text: str, members: set[str] | None = None, base: str = "", *,
                      passive_rel_ids: frozenset[str] = frozenset()) -> str:
    """Parse XML once: prefixes, quoting and character references cannot bypass checks."""
    from lxml import etree
    # XML declarations may name the original encoding, but decode_text has already decoded it.
    text = re.sub(r"^\s*<\?xml[^?]*\?>", "", text)
    try:
        root = etree.fromstring(text.encode("utf-8"), etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True))
    except etree.XMLSyntaxError as exc:
        raise ConversionError("corrupt_file", "Invalid document XML.") from exc
    # StarOffice documents declare an external office.dtd. It is never loaded
    # and serializing the root drops the declaration. Reject entity declarations
    # (including unused ones), whose expansion could otherwise change content.
    dtd = root.getroottree().docinfo.internalDTD
    if dtd is not None and (entities := list(dtd.iterentities())):
        # Illustrator SVGs use literal namespace entities. Expand only bounded
        # internal literals in SVG, without nested entities, markup or external IDs.
        literals = {e.name: e.content for e in entities}
        if (root.tag != '{http://www.w3.org/2000/svg}svg' or len(entities) > 64
                or any(e.system_url or e.content is None or any(c in e.content for c in '<>&') for e in entities)
                or sum(len(v) for v in literals.values()) > 16384):
            raise ConversionError("unsupported_format", "Document XML with entity declarations is not supported.")
        expanded = 0
        for entity in [n for n in root.iter() if isinstance(n, etree._Entity)]:
            value = literals.get(entity.name)
            expanded += len(value or '')
            if value is None or expanded > 65536:
                raise ConversionError('too_large', 'SVG entity expansion exceeds the document limit.')
            parent = entity.getparent(); previous = entity.getprevious()
            if previous is None:
                parent.text = (parent.text or '') + value + (entity.tail or '')
            else:
                previous.tail = (previous.tail or '') + value + (entity.tail or '')
            parent.remove(entity)
    members = members or set()
    fields: list[list] = []
    for node in list(root.iter()):
        if not isinstance(node.tag, str):
            continue
        local = etree.QName(node).localname
        attrs = {etree.QName(key).localname: (key, value) for key, value in node.attrib.items()}
        def value(name: str) -> str:
            return attrs.get(name, ("", ""))[1]
        remove = local in ("scripts", "event-listeners", "section-source")
        if local in ("Relationship", "Override", "file-entry"):
            remove |= any(MACRO_PART.search(v.lstrip("/")) or "vbaproject" in v.lower() or "vbadata" in v.lower()
                          for _, v in attrs.values())
        if remove and node.getparent() is not None:
            node.getparent().remove(node)
            continue
        passive = (value("Type") in {ns + "/hyperlink" for ns in REL_NAMESPACES}
                   and value("Id") in passive_rel_ids and _passive_url(value("Target")))
        if local == "Relationship" and not passive:
            if value("TargetMode").lower() == "external" or not _package_reference(value("Target"), members, base, package_root=True):
                if "TargetMode" in attrs:
                    node.attrib.pop(attrs["TargetMode"][0])
                node.set(attrs.get("Target", ("Target", ""))[0], "removed-external-link")
        for key, val in list(node.attrib.items()):
            name = etree.QName(key).localname
            passive_href = (node.tag in ODF_TEXT_LINKS and key == "{http://www.w3.org/1999/xlink}href"
                            and _passive_url(val))
            inline_raster = (node.tag == '{http://www.w3.org/2000/svg}image' and name == 'href'
                             and re.fullmatch(r'data:image/(?:png|jpe?g|gif|webp);base64,[A-Za-z0-9+/\r\n]*={0,2}', val, re.I))
            if name == "href" and not passive_href and not inline_raster and not _package_reference(val, members):
                node.set(key, "")
            if name == "src" and not _package_reference(val, members, base):
                node.set(key, "")
            if name == "instr" and DANGEROUS_FIELD.search(val):
                node.set(key, "")
        if local == "fldChar":
            if value("fldCharType") == "begin":
                fields.append([])
            elif value("fldCharType") in ("separate", "end") and fields:
                group = fields[-1]
                if DANGEROUS_FIELD.search("".join(n.text or "" for n in group)):
                    for n in group:
                        n.text = ""
                if value("fldCharType") == "end":
                    fields.pop()
        elif local == "instrText":
            if fields:
                fields[-1].append(node)
            elif DANGEROUS_FIELD.search(node.text or ""):
                node.text = ""
    # Malformed/unclosed fields still cannot fetch an external source.
    for group in fields:
        if DANGEROUS_FIELD.search("".join(n.text or "" for n in group)):
            for node in group:
                node.text = ""
    return etree.tostring(root, encoding="unicode")


def sanitize_flat_xml(src: Path, dst: Path) -> None:
    # LibreOffice's flat-XML detector requires a declaration. Without it Writer
    # renders the XML source, or Calc imports XML nodes as an unrelated table.
    # The input was decoded above; always declare the encoding we actually write.
    dst.write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                   + sanitize_xml_text(decode_text(src.read_bytes())), encoding="utf-8")


def sanitize_package(src: Path, dst: Path) -> None:
    """Sanitize package content and its relationship consumers, including embedded packages."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        _sanitize_archive(zin, zout, {"bytes": 0, "entries": 0}, 0)


def _passive_relationships(data: bytes) -> frozenset[str]:
    """A relationship is passive only when every consumer is a known text/click hyperlink."""
    from lxml import etree
    try:
        root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True))
    except etree.XMLSyntaxError:
        return frozenset()
    uses: dict[str, list[bool]] = {}
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        for key, rid in node.attrib.items():
            name = etree.QName(key)
            if name.namespace in REL_NAMESPACES:
                uses.setdefault(rid, []).append(node.tag in PASSIVE_LINK_ELEMENTS and name.localname == "id")
    return frozenset(rid for rid, passive in uses.items() if all(passive))


def _empty_shortcut_part(name: str, data: bytes, archive: zipfile.ZipFile) -> bool:
    shortcuts = "Configurations2/accelerator/current.xml"
    if name == shortcuts:
        return not data.strip()
    # LibreOffice embeds ODF objects in directories of its choosing. Only an
    # actually empty shortcut file can omit XML in one of these directories.
    if data or not name.endswith("/" + shortcuts):
        return False
    try:
        mime = archive.getinfo("mimetype")
    except KeyError:
        return False
    if mime.file_size > 200:
        return False
    return ODF.get(archive.read(mime).decode("ascii", "replace").strip()) in {"odt", "ods", "odp", "odg"}


def _sanitize_archive(zin: zipfile.ZipFile, zout: zipfile.ZipFile, budget: dict, depth: int) -> None:
    from copy import copy
    import posixpath
    from lxml import etree
    from .formats import MAX_ZIP_MEMBER, MAX_ZIP_TOTAL
    infos = zin.infolist()
    names = [i.filename for i in infos]
    if len(set(names)) != len(names):
        raise ConversionError("corrupt_file", "Duplicate document package entries.")
    budget["bytes"] += sum(i.file_size for i in infos)
    budget["entries"] += len(infos)
    if (depth > 4 or budget["entries"] > 10000 or budget["bytes"] > MAX_ZIP_TOTAL
            or any(i.file_size > MAX_ZIP_MEMBER for i in infos)):
        raise ConversionError("too_large", "The document's embedded packages exceed the processing limits.")
    members = {name for name in names if not MACRO_PART.search(name) and not name.startswith("/")
               and ".." not in name.split("/") and "\\" not in name}
    xml_parts = {n for n in members if n.lower().endswith((".xml", ".rels", ".vml"))}
    html_parts = {n for n in members if n.lower().endswith(('.html', '.htm', '.xhtml'))}
    if "[Content_Types].xml" in members:
        root = etree.fromstring(sanitize_xml_text(decode_text(zin.read("[Content_Types].xml")), members).encode())
        for node in root:
            if not isinstance(node.tag, str):
                continue
            mime = node.get("ContentType", "").split(";")[0].lower()
            is_html = mime in ('text/html', 'application/xhtml+xml')
            if not is_html and not mime.endswith(("/xml", "+xml")):
                continue
            parts = html_parts if is_html else xml_parts
            if etree.QName(node).localname == "Override":
                parts.update(_package_paths(node.get('PartName', ''), members, package_root=True))
            elif etree.QName(node).localname == "Default":
                extension = "." + node.get("Extension", "").lower()
                parts.update(n for n in members if n.lower().endswith(extension))
    # Import relationships determine what LibreOffice reads; a false content type
    # or filename must not exempt an HTML altChunk from resource sanitization.
    imported_parts = set()
    for name in members:
        if not name.lower().endswith('.rels'):
            continue
        base = posixpath.dirname(posixpath.dirname(name))
        root = etree.fromstring(sanitize_xml_text(decode_text(zin.read(name)), members, base).encode())
        for rel in root:
            if rel.get('Type') in {ns + '/aFChunk' for ns in REL_NAMESPACES}:
                imported_parts.update(_package_paths(rel.get('Target', ''), members, base, package_root=True))
    for info in infos:
        name = info.filename
        if name not in members:
            continue
        data = zin.read(info)
        imported_html = (name in imported_parts and not data.lstrip().startswith((b'{\\rtf', b'PK')))
        if imported_html:
            # altChunk also imports WordprocessingML. Preserve its structure and
            # apply XML resource rules; an HTML payload with a false XML MIME
            # declaration must still take the HTML sanitizer below.
            try:
                imported_root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True))
            except etree.XMLSyntaxError:
                pass
            else:
                if imported_root.tag in {
                    '{http://schemas.microsoft.com/office/word/2003/wordml}wordDocument',
                    '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document',
                }:
                    imported_html = False
                    xml_parts.add(name)
        if name in html_parts or imported_html:
            data = ('<html><body>' + sanitize_html(decode_text(data)) + '</body></html>').encode('utf-8')
        elif name in xml_parts:
            if not _empty_shortcut_part(name, data, zin):
                text = decode_text(data)
                if name == "META-INF/manifest.xml" and re.search(
                    r"""<!DOCTYPE\s+manifest:manifest\s+PUBLIC\s+(["'])-//OpenOffice\.org//DTD Manifest 1\.0//EN\1\s+(["'])Manifest\.dtd\2\s*>""", text,
                ):
                    text = re.sub(r"<manifest:manifest\s*>",
                                  '<manifest:manifest xmlns:manifest="http://openoffice.org/2001/manifest">', text, count=1)
                if name.lower().endswith(".vml"):
                    text = re.sub(r"<!\[(?:if\s+[^\]<>]*|endif)\]>", "", text, flags=re.I)
                passive = frozenset()
                base = posixpath.dirname(name)
                if name.lower().endswith(".rels"):
                    base = posixpath.dirname(posixpath.dirname(name))
                    owner = posixpath.join(base, posixpath.basename(name)[:-5])
                    if owner in xml_parts and owner in members:
                        passive = _passive_relationships(zin.read(owner))
                data = sanitize_xml_text(text, members, base, passive_rel_ids=passive).encode("utf-8")
        elif data[:2] == b"PK" and zipfile.is_zipfile(io.BytesIO(data)):
            nested = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(data)) as inner, zipfile.ZipFile(nested, "w", zipfile.ZIP_DEFLATED) as cleaned:
                _sanitize_archive(inner, cleaned, budget, depth + 1)
            data = nested.getvalue()
        # writestr updates CRC/size on its ZipInfo. Keep the input metadata intact: later
        # relationship parts may still need to read their already-written owner part.
        zout.writestr(copy(info), data)


def run_soffice(soffice: str, src: Path, outdir: Path, export_filter: str, max_pages: int | None, timeout: float,
                target: str = "pdf") -> Path:
    options = ""
    if max_pages and target == "pdf":
        options = ":" + json.dumps({"PageRange": {"type": "string", "value": f"1-{max_pages}"}})
    profile = Path(tempfile.mkdtemp(prefix="lo-profile-"))
    if os.environ.get("CC_JOB_ISOLATED") == "landlock":
        # The oosplash launcher requires a globally writable /tmp. The actual engine
        # supports OSL_SOCKET_PATH and can keep IPC in this one job's private profile.
        engine = Path(soffice).resolve().with_name("soffice.bin")
        if not engine.is_file():
            raise ConversionError("unsupported_format", "This LibreOffice install has no isolated conversion engine.")
        soffice = str(engine)
    try:
        deadline = time.monotonic() + timeout
        for attempt in range(2):
            proc = subprocess.run(
                [soffice, "--headless", "--norestore", "--nologo", "--nolockcheck", "--nodefault", "--nofirststartwizard",
                 f"-env:UserInstallation={profile.as_uri()}", "--convert-to", f"{target}:{export_filter}{options}",
                 "--outdir", str(outdir), str(src)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=max(.01, deadline - time.monotonic()), cwd=profile,
            )
            # soffice.bin requests one restart after initializing a new user profile.
            if proc.returncode != 81 or os.environ.get("CC_JOB_ISOLATED") != "landlock":
                break
    except subprocess.TimeoutExpired as exc:
        raise ConversionError("timeout", "Converting the document took too long.") from exc
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    out = outdir / f"{src.stem}.{target}"
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise ConversionError("conversion_failed", "The document could not be converted. It may be damaged or password-protected.")
    return out


# Tables are also read from the original (source-tables.json, put back by the parse job): from
# LibreOffice's HTML for text documents and workbooks, from the PowerPoint file for presentations.
TABLE_EXPORTS = {
    **{f: ("html", "HTML (StarWriter)") for f in ("docx", "doc", "odt", "rtf", "sxw", "fodt", "wordml", "wpd")},
    **{f: ("html", "HTML (StarCalc)") for f in ("xlsx", "xls", "xlsb", "ods", "sxc", "fods", "xmlss")},
    **{f: ("pptx", "Impress MS PowerPoint 2007 XML") for f in ("ppt", "odp", "sxi", "fodp")},
}


def office_tables(soffice: str, fmt: str, target: Path, work: Path, timeout: int) -> list[list[list[str]]]:
    from .office_tables import html_tables, pptx_tables

    if fmt == "pptx":
        return pptx_tables(target)
    kind, export_filter = TABLE_EXPORTS[fmt]
    outdir = work / "tables"
    outdir.mkdir()
    exported = run_soffice(soffice, target, outdir, export_filter, None, timeout, target=kind)
    return html_tables(exported) if kind == "html" else pptx_tables(exported)


# --- Hangul -------------------------------------------------------------------------------------

def hwp_text(src: Path) -> str:
    from contextlib import closing

    from hwp5.hwp5txt import TextTransform
    from hwp5.xmlmodel import Hwp5File

    with closing(Hwp5File(str(src))) as hwp:
        buf = io.BytesIO()
        TextTransform().transform_hwp5_to_text(hwp, buf)
        return buf.getvalue().decode("utf-8", "replace")


def convert(doc_dir: Path, fmt: str, soffice: str | None, max_pages: int | None, timeout: int) -> list[str]:
    from .hangul_pictures import EmbeddedPictures, TEXT_PAGE
    from .hwp import hwp_to_html as hwp_records_to_html
    from .hwpx import hwpx_preview_text, hwpx_to_html

    src = next(doc_dir.glob("original.*"))
    # Written under a temporary name and renamed at the end, so a half-written file never
    # looks like a finished conversion.
    final = doc_dir / "source.pdf"
    out = doc_dir / "source.pdf.part"
    work = Path(tempfile.mkdtemp(prefix="convert-"))
    notices: list[str] = []
    tables: list[list[list[str]]] = []
    # The HTML page written for formats rendered here (text, Markdown, CSV, HTML, HWP, HWPX).
    page: Path | None = None
    started = time.monotonic()
    # Text worth converting under a page limit: far more than the kept pages hold.
    budget = max_pages * CHARS_PER_PAGE if max_pages else None

    def need_soffice() -> str:
        if not soffice:
            raise ConversionError("unsupported_format", "This server cannot convert this file type.")
        return soffice

    def html_to_pdf(page: Path, pictures: EmbeddedPictures | None = None) -> None:
        source = page
        export = "writer_web_pdf_Export"
        if pictures and pictures.pack and "text_fallback" not in notices:
            remaining = max(0.01, timeout - (time.monotonic() - started))
            layout = run_soffice(need_soffice(), page, work, "writerweb8_writer", None, remaining, target="odt")
            package = pictures.fill_package(layout)
            if package is None:
                source = pictures.text_only()
            else:
                source, export = package, "writer_pdf_Export"
        if pictures and pictures.omitted:
            notices.append("pictures_omitted")
        remaining = max(0.01, timeout - (time.monotonic() - started))
        pdf = run_soffice(need_soffice(), source, work, export, max_pages, remaining)
        shutil.move(pdf, out)

    try:
        if fmt in IMAGES:
            image_to_pdf(src, out)
        elif fmt in ("pages", "numbers", "key") and (preview := iwork_preview(src)):
            out.write_bytes(preview)
        elif fmt in OFFICE or fmt == "hwp3":
            target = src
            if fmt in PACKAGES:
                target = work / f"document{src.suffix}"
                sanitize_package(src, target)
            elif fmt in FLAT:
                target = work / f"document{src.suffix}"
                sanitize_flat_xml(src, target)
            pdf = run_soffice(need_soffice(), target, work, FILTERS[fmt], max_pages, timeout)
            shutil.move(pdf, out)
            # Only in the time the conversion has left: the tables are a refinement.
            left = timeout - (time.monotonic() - started)
            if (fmt in TABLE_EXPORTS or fmt == "pptx") and left > 10:
                try:
                    tables = office_tables(need_soffice(), fmt, target, work, int(left))
                except Exception as exc:  # noqa: BLE001 - the tables read off the PDF stay
                    print(f"source tables failed: {type(exc).__name__}", file=sys.stderr)
            # Word stores alternating and first-page running text explicitly. Keep that
            # structural evidence instead of requiring the same text on three PDF pages.
            if fmt in ("docx", "doc", "pptx", "wordml"):
                try:
                    from .parsers.source_furniture import docx_templates, fodt_templates, pptx_templates, wordml_templates
                    if fmt == "docx":
                        furniture = docx_templates(target)
                    elif fmt == "pptx":
                        furniture = pptx_templates(target)
                    elif fmt == "wordml":
                        furniture = wordml_templates(target)
                    else:
                        left = timeout - (time.monotonic() - started)
                        furniture = []
                        if left > 5:
                            folder = work / "furniture"
                            folder.mkdir()
                            exported = run_soffice(need_soffice(), target, folder,
                                "OpenDocument Text Flat XML", None, min(30, int(left)), target="fodt")
                            furniture = fodt_templates(exported)
                    if furniture:
                        (doc_dir / "source-furniture.json").write_text(json.dumps(furniture, ensure_ascii=False), encoding="utf-8")
                except Exception as exc:  # PDF evidence remains when this optional refinement fails.
                    print(f"source furniture failed: {type(exc).__name__}", file=sys.stderr)
        elif fmt == "xps":
            fixed_to_pdf(src, out, max_pages)
        elif fmt in ("html", "md", "txt", "csv", "epub"):
            text = "" if fmt == "epub" else decode_text(src.read_bytes())
            if budget:
                # LibreOffice lays out the whole input before exporting the kept pages.
                text = text[: budget * 4]
            if fmt == "epub":
                body = epub_body(src)
            elif fmt == "html":
                body = sanitize_html(text)
            elif fmt == "md":
                body = markdown_body(text)
            elif fmt == "csv" or tab_separated(text):
                body, notices = csv_body(text)
            else:
                body = text_body(text)
            page = work / "document.html"
            page.write_text(page_html(body), encoding="utf-8")
            html_to_pdf(page)
            from .office_tables import html_tables

            # The tables of the page written here, cell by cell.
            tables = html_tables(page)
        elif fmt == "hwpx":
            pictures = EmbeddedPictures(work)
            try:
                page = hwpx_to_html(src, work, page_html, budget, pictures)
            except Exception as exc:  # noqa: BLE001 - fall back to the preview text every HWPX carries
                print(f"hwpx structure failed: {type(exc).__name__}", file=sys.stderr)
                try:
                    preview = hwpx_preview_text(src)
                except (KeyError, zipfile.BadZipFile) as exc:
                    raise ConversionError("no_text", "No text was found in this document.") from exc
                page = work / "document.html"
                page.write_text(page_html(text_body(preview)), encoding="utf-8")
                notices.append("text_fallback")
            html_to_pdf(page, pictures)
        elif fmt == "hwp":
            pictures = EmbeddedPictures(work)
            try:
                page = hwp_records_to_html(src, work, page_html, budget, pictures)
            except Exception as exc:  # noqa: BLE001 - pyhwp is beta; plain text is better than nothing
                print(f"hwp structure failed: {type(exc).__name__}", file=sys.stderr)
                empty = isinstance(exc, ValueError) and str(exc) == "no text found"
                try:
                    text = hwp_text(src)
                except Exception as exc2:  # noqa: BLE001
                    print(f"hwp text failed: {type(exc2).__name__}", file=sys.stderr)
                    if empty:
                        raise ConversionError("no_text", "No text was found in this document.") from exc2
                    raise ConversionError("corrupt_file", "The HWP file is damaged or incomplete.") from exc2
                page = work / "document.html"
                page.write_text(page_html(text_body(text)), encoding="utf-8")
                notices.append("text_fallback")
            html_to_pdf(page, pictures)
        else:
            raise ConversionError("unsupported_format", "This file type is not supported.")
        if fmt in ("hwp", "hwpx") and "text_fallback" not in notices:
            from .hwpx import SOURCE_TABLES

            tables = SOURCE_TABLES
        if tables:
            # The exact tables, for the parser to put back over what it reads from the PDF.
            (doc_dir / "source-tables.json").write_text(json.dumps(tables, ensure_ascii=False), encoding="utf-8")
        if page is not None and page.exists():
            from .parsers.source_words import page_words

            # Its words, so the parser can join a word the PDF line wrap cut in two.
            word_source = page
            if fmt in ("hwp", "hwpx") and "text_fallback" not in notices and (work / TEXT_PAGE).is_file():
                word_source = work / TEXT_PAGE
            (doc_dir / "source-words.json").write_text(json.dumps(page_words(word_source), ensure_ascii=False), encoding="utf-8")
        os.replace(out, final)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        out.unlink(missing_ok=True)
    return notices


def points_per_pixel(im) -> float:
    """Page size for an image: its own resolution when that gives a plausible paper width,
    otherwise its width read as an A4 page. OCR renders pages at a fixed scale, so a 200 dpi scan
    laid out at 96 dpi was rendered at over four times the pixels it has."""
    dpi = im.info.get("dpi")
    try:
        width_in = im.width / float(dpi[0]) if dpi and float(dpi[0]) > 0 else 0.0
    except (TypeError, ValueError, IndexError):
        width_in = 0.0
    if not 3 <= width_in <= 17:
        width_in = 8.27
    return width_in * 72 / im.width


def image_to_pdf(src: Path, out: Path) -> None:
    """Each page of a multi-page TIFF becomes an RGB page (an animated GIF or WebP gives its first
    frame only: its frames are an animation, not pages); CMYK, alpha and WebP are normalized."""
    import pymupdf
    from PIL import Image, ImageSequence

    pdf = pymupdf.open()
    with Image.open(src) as im:
        if im.size[0] * im.size[1] > MAX_IMAGE_PIXELS:
            raise ConversionError("too_large", "The image is too large.")
        # The iterator seeks one image object: each frame must be used before the next seek.
        for frame in itertools.islice(ImageSequence.Iterator(im), 200 if im.format == "TIFF" else 1):
            rgb = frame.convert("RGBA")
            flat = Image.new("RGB", rgb.size, "white")
            flat.paste(rgb, mask=rgb.getchannel("A"))
            buf = io.BytesIO()
            flat.save(buf, "PNG")
            scale = points_per_pixel(frame)
            page = pdf.new_page(width=flat.width * scale, height=flat.height * scale)
            page.insert_image(page.rect, stream=buf.getvalue())
    pdf.save(out)


def main() -> int:
    from .sandbox import limit_self

    limit_self()
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc-dir", required=True)
    ap.add_argument("--format", required=True, choices=sorted(EXTENSIONS))
    ap.add_argument("--soffice")
    ap.add_argument("--max-pages", type=int)
    ap.add_argument("--timeout", type=int, default=150)
    args = ap.parse_args()
    doc_dir = Path(args.doc_dir)

    def write(name: str, data: dict) -> None:
        tmp = doc_dir / f"{name}.tmp"
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, doc_dir / name)

    try:
        notices = convert(doc_dir, args.format, args.soffice, args.max_pages, args.timeout)
        write("conversion.json", {"notices": notices})
        return 0
    except ConversionError as exc:
        write("error.json", {"code": exc.code, "message": exc.message})
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"conversion failed: {type(exc).__name__}", file=sys.stderr)
        write("error.json", {"code": "conversion_failed", "message": "The document could not be converted."})
        return 1


if __name__ == "__main__":
    sys.exit(main())
