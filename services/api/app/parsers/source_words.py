"""Words a PDF line wrap cut in two, joined again from the text the document was made from.

LibreOffice breaks a token too long for its line anywhere ("ISO3166-1-nu" | "meric:"), without a
hyphen, and read back the halves come apart with a space. For the formats ChunkCanvas renders
itself (text, Markdown, CSV, HTML, HWP) the words of the source are known: two neighbours that
are no source words but make one together are that word.
"""
import re
from pathlib import Path

from ..models import ParsedDocument
from .base import blocks_fingerprint

TYPES = {"paragraph", "list_item", "section_header", "title", "caption", "footnote"}
MAX_WORDS = 300_000
# Elements that end a word: the text of two cells or paragraphs side by side is no one word
# ("tan" | "cot", "생략합니다." | "x atop y"), though the page source runs it together.
BREAKS = {"address", "article", "aside", "blockquote", "body", "br", "caption", "dd", "div", "dl", "dt",
          "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "img", "li",
          "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "title",
          "tr", "ul"}


def page_words(page: Path) -> list[str]:
    """The words (whitespace-separated, punctuation kept) of an HTML page's visible text."""
    import lxml.html

    root = lxml.html.parse(str(page)).getroot()
    if root is None:
        return []
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.lower() in BREAKS:
            el.tail = " " + (el.tail or "")
            if len(el) or el.text:
                el.text = " " + (el.text or "")
    # Long tokens carry zero-width spaces (and soft hyphens) where they may wrap: not in the word.
    text = root.text_content().replace("\u200b", "").replace("\u00ad", "")
    return sorted(set(text.split()))[:MAX_WORDS]


def rejoin(parsed: ParsedDocument, words: set[str]) -> int:
    """Join the broken words of every text block; returns how many were joined."""
    joined = 0
    for b in parsed.blocks:
        if b.type not in TYPES or " " not in b.text:
            continue
        spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", b.text)]
        gaps = [left_end for (left_start, left_end), (right_start, right_end) in zip(spans, spans[1:])
                if right_start == left_end + 1
                and _broken(b.text[left_start:left_end], b.text[right_start:right_end], words)]
        if not gaps:
            continue
        old = b.text
        for p in reversed(gaps):
            b.text = b.text[:p] + b.text[p + 1:]
            for line in b.lines or []:
                if line.start > p:
                    line.start, line.end = line.start - 1, line.end - 1
                elif line.start <= p < line.end:
                    del line.xs[p - line.start]
                    line.end -= 1
        if b.markdown and b.markdown.endswith(old):
            b.markdown = b.markdown[: len(b.markdown) - len(old)] + b.text
        joined += len(gaps)
    if joined:
        parsed.blocks_fingerprint = blocks_fingerprint(parsed.document.parser, parsed.document.parser_version, parsed.blocks)
    return joined


def _broken(left: str, right: str, words: set[str]) -> bool:
    return len(left) + len(right) >= 6 and left + right in words and left not in words and right not in words
