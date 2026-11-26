"""Character-level text layer from PyMuPDF.

Turns the characters inside a region into block text plus per-line geometry: for each visual
line, its box and the left edge of every character, so a text offset can be located on the page
exactly. Only horizontal left-to-right lines on unrotated pages are mapped; anything else
(vertical CJK, rotated pages) is left to block-level editing.
"""
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path

import pymupdf

from ..models import BBox
from . import glyph_names, opensymbol
from .base import HANCOM, SUPERSCRIPT
from .spacing import HANGUL, one_word
from .visible_text import read_raw

FLAGS = (pymupdf.TEXTFLAGS_RAWDICT | pymupdf.TEXT_PRESERVE_WHITESPACE) & ~pymupdf.TEXT_PRESERVE_LIGATURES
SPACE = re.compile(r"\s")
# Scripts written without spaces between words: lines join without a space.
# A gap wider than this (in units of the font size) between two characters is a word break.
WORD_GAP_EM = 0.12
# TeX sets an accent as a glyph of its own drawn over the letter ("Fr´echet", "˜ρ"): the spacing
# accents and the combining marks they stand for.
ACCENTS = {"´": "\u0301", "`": "\u0300", "ˆ": "\u0302", "^": "\u0302", "˜": "\u0303", "~": "\u0303",
           "¨": "\u0308", "¯": "\u0304", "˙": "\u0307", "˚": "\u030a", "˝": "\u030b", "ˇ": "\u030c",
           "˘": "\u0306", "¸": "\u0327", "˛": "\u0328"}
# An accent over a dotless i or j is on an i or j ("na¨ıve").
DOTLESS = {"ı": "i", "ȷ": "j"}
# ... and \not as a slash of no width before the relation it negates ("̸ =" is "≠").
NEGATION = "\u0338"
# The same in text read elsewhere (Docling's) from a page whose accents were found drawn that way;
# "`", "^" and "~" are left out there, as quotes and code use them too.
TEXT_ACCENT = re.compile(r"([´ˆ˜¨¯˙˚˝ˇ˘¸˛])([^\W\d_])")
TEXT_CEDILLA = re.compile(r"([cCsStT]¸|[aeiouAEIOU]˛)(?![^\W\d_])")
TEXT_NEGATION = re.compile(r"\u0338(\s?)(\S)")


def _cmex() -> dict[int, str]:
    """TeX's math extension font (cmex10 layout, also Latin Modern's copy) usually has no ToUnicode
    map, so its glyphs arrive as raw codes: "X" for a display sum, "p" for a radical, control
    characters for big parentheses. Pieces of extensible delimiters, braces and arrows mean nothing
    on their own and map to "" (dropped)."""
    table = dict.fromkeys(range(0x80), "")
    rows = {
        0x00: "()[]⌊⌋⌈⌉{}⟨⟩|‖/\\",
        0x10: "()()[]⌊⌋⌈⌉{}⟨⟩/\\",
        0x20: "()[]⌊⌋⌈⌉{}⟨⟩/\\/\\",
        0x44: "⟨⟩⊔⊔∮∮⊙⊙⊕⊕⊗⊗∑∏∫⋃⋂⊎⋀⋁∑∏∫⋃⋂⊎⋀⋁∐∐ˆˆˆ˜˜˜[]⌊⌋⌈⌉{}√√√√√",
    }
    for start, chars in rows.items():
        table.update({start + i: c for i, c in enumerate(chars)})
    return table


CMEX = _cmex()
MATH_EXTENSION_FONT = re.compile(r"^(CMEX|LMMathExtension)\d", re.I)
# AMS blackboard bold (\mathbb): plain capital letters in the font's encoding.
BLACKBOARD_FONT = re.compile(r"^MSBM\d", re.I)
BLACKBOARD = {c: chr(0x1D538 + i) for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")} | {
    "C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ",
}


# mathabx (TeX-matha/mathb/mathx): pdfTeX gives these fonts a ToUnicode map of the T1 text
# encoding, so "d_p(A) := {g ∈ G}" reads as "dppAq :“ tg P G". Built from the package's
# mathabx.dcl (code -> symbol) and that T1 map (code -> character extracted).
MATHABX_FONT = re.compile(r"^TeX-math([abx])\d", re.I)
MATHABX = json.loads((Path(__file__).parent / "mathabx.json").read_text(encoding="utf-8"))


# Symbol and Wingdings put their glyphs at U+F020-F0FF; Office documents converted to PDF keep
# those codes, so "α" or a check box arrives as a private-use character.
_SYMBOL = dict(zip(range(0xF020, 0xF100), (
    " !∀#∃%&∍()∗+,−./0123456789:;<=>?"
    "≅ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ[∴]⊥_"
    "‾αβχδεφγηιϕκλμνοπθρστυϖωξψζ{|}∼\x7f"
    + "\x00" * 32 +
    "€ϒ′≤⁄∞ƒ♣♦♥♠↔←↑→↓°±″≥×∝∂•÷≠≡≈…⏐⎯↵"
    "ℵℑℜ℘⊗⊕∅∩∪⊃⊇⊄⊂⊆∈∉∠∇®©™∏√⋅¬∧∨⇔⇐⇑⇒⇓"
    "◊〈®©™∑⎛⎜⎝⎡⎢⎣⎧⎨⎩⎪\x00〉∫⌠⎮⌡⎞⎟⎠⎤⎥⎦⎫⎬⎭\x00")))
_WINGDINGS = {0xF06C: "●", 0xF06E: "■", 0xF06F: "□", 0xF070: "◻", 0xF071: "❑", 0xF075: "◆", 0xF076: "❖", 0xF077: "◆",
              0xF078: "☒", 0xF0A7: "▪", 0xF0A8: "◻", 0xF0B7: "•", 0xF0D8: "➢", 0xF0E0: "→", 0xF0E8: "➔",
              0xF0F0: "⇨", 0xF0FB: "✗", 0xF0FC: "✓", 0xF0FE: "☑", 0xF09F: "•", 0xF09E: "·"}


def private_glyph(font: str, c: str, alone: bool = False) -> str:
    """A private-use character of a symbol font as the Unicode character it shows. LibreOffice
    draws the symbols of Wingdings and Symbol alike with OpenSymbol, keeping their codes; a glyph
    standing alone at the start of a line there is a list bullet, which Office documents take from
    Wingdings."""
    code = ord(c)
    if 0xF020 <= code <= 0xF0FF:
        name = font.lower()
        if "wingding" in name or (alone and "opensymbol" in name and code in _WINGDINGS):
            return _WINGDINGS.get(code, c)
        if "symbol" in name:
            mapped = _SYMBOL.get(code, "\x00")
            return c if mapped in ("\x00", "\x7f") else mapped
    return HANCOM.get(code, c)


def math_glyph(font: str, c: str) -> str:
    """The character a math-font glyph stands for ("" for a meaningless piece); characters of all
    other fonts are returned unchanged. A space is left alone: MuPDF synthesizes those."""
    base = font.split("+")[-1]
    if MATH_EXTENSION_FONT.match(base) and ord(c) < 0x80 and c != " ":
        return CMEX[ord(c)]
    if BLACKBOARD_FONT.match(base):
        return BLACKBOARD.get(c, c)
    if c != " " and (abx := MATHABX_FONT.match(base)):
        table = MATHABX[f"math{abx.group(1).lower()}"]
        # Unlisted glyphs of the extension font are pieces of tall delimiters.
        return table.get(c, "" if abx.group(1).lower() == "x" else c)
    return c
# Kana, Han and halfwidth katakana.
NO_SPACE_SCRIPTS = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]")


@dataclass
class Char:
    c: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    bold: bool
    used: bool = False
    # A glyph of a math extension font: big operators and tall delimiters.
    tall: bool = False
    # A math glyph mapped back from what the PDF's Unicode map gave (see math_glyph).
    remapped: bool = False

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Fragment:
    """One PyMuPDF line (or the part of it inside a region)."""

    chars: list[Char]
    block: int = -1

    @property
    def y0(self) -> float:
        return min(c.y0 for c in self.chars)

    @property
    def y1(self) -> float:
        return max(c.y1 for c in self.chars)

    @property
    def x0(self) -> float:
        return min(c.x0 for c in self.chars)

    @property
    def x1(self) -> float:
        return max(c.x1 for c in self.chars)

    # Where the visible characters are: a line may start with spaces reaching over the word
    # before it (words of a slide title placed one by one).
    @property
    def ink_x0(self) -> float:
        return min((c.x0 for c in self.chars if c.c.strip()), default=self.x0)

    @property
    def ink_x1(self) -> float:
        return max((c.x1 for c in self.chars if c.c.strip()), default=self.x1)


@dataclass
class PageText:
    index: int
    width: float
    height: float
    # On rotated pages coordinates are in the unrotated frame: fine for text order, not for display.
    rotated: bool = False
    fragments: list[Fragment] = field(default_factory=list)
    # (type, bbox in PDF points) for every PyMuPDF block, in content-stream order.
    blocks: list[tuple[int, tuple[float, float, float, float]]] = field(default_factory=list)
    # Private-use symbol code -> the character it shows on this page (codes read one way only).
    symbols: dict[str, str] = field(default_factory=dict)
    # Accents drawn as glyphs of their own over letters were found here (see _with_accents).
    accents: bool = False
    # A native table extractor must not reintroduce values hidden by PDF paint/clips.
    visibility_filtered: bool = False

    def chars_in(self, box: BBox, tol: float = 0.003, keep=None) -> list[Fragment]:
        """Unused characters whose centre lies in the normalized box, grouped by source line.
        keep(line) can refuse whole source lines."""
        out = []
        for frag in self.fragments:
            if keep is not None and not keep(frag):
                continue
            inside = [
                ch for ch in frag.chars
                if not ch.used
                and box.x0 - tol <= ch.cx <= box.x1 + tol
                and box.y0 - tol <= ch.cy <= box.y1 + tol
            ]
            if inside:
                out.append(Fragment(inside))
        return out


def horizontal_line(line: dict) -> bool:
    dx, dy = line.get("dir", (1, 0))
    return line.get("wmode", 0) == 0 and dx >= .99 and abs(dy) <= .01


def read_page(page: "pymupdf.Page", index: int) -> PageText:
    rotated = bool(page.rotation)
    frame = page.cropbox if rotated else page.rect
    width, height = frame.width, frame.height
    pt = PageText(index=index, width=width, height=height, rotated=rotated)
    raw, pt.visibility_filtered = read_raw(page, FLAGS, detect_clipping=True)
    # Symbols LibreOffice drew with OpenSymbol, known from their outlines; the rules below are for
    # glyphs this cannot place.
    drawn = opensymbol.page_glyphs(page)
    symbols: dict[str, str] = {}
    # Glyphs without a character (math extension fonts with no Unicode map: their raw codes come
    # through as control characters or stray letters), named by their font.
    named = glyph_names.page_glyphs(page)
    # OpenSymbol drawing + = − ( ) on the page sets an equation in Symbol: its lone glyphs are Greek
    # letters there ("2π"), not Wingdings bullets.
    equation = any(span["font"].lower().endswith("opensymbol") and ch["c"] in "\uf02b\uf03d\uf02d\uf028\uf029"
                   for block in raw["blocks"] for line in block.get("lines", []) for span in line["spans"]
                   for ch in span["chars"])
    for block_no, block in enumerate(raw["blocks"]):
        pt.blocks.append((block.get("type", 0), tuple(block["bbox"])))
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            if not horizontal_line(line):
                continue
            chars = []
            for span in line["spans"]:
                bold = bool(span["flags"] & 16) or "bold" in span["font"].lower()
                tall = bool(MATH_EXTENSION_FONT.match(span["font"].split("+")[-1]))
                # A glyph standing alone at the start of the line is a bullet; inside a line, alone in
                # its span, it is a symbol of an equation ("cos 2π f t").
                alone = len(span["chars"]) == 1 and not any(c.c.strip() for c in chars) and not equation
                for ch in span["chars"]:
                    origin = (round(ch["origin"][0], 2), round(ch["origin"][1], 2))
                    c = named[origin] if origin in named else math_glyph(span["font"], ch["c"])
                    if not c:
                        continue
                    if "\ue000" <= c <= "\uf8ff" or "\U000f0000" <= c <= "\U000ffffd":
                        c = drawn.get(origin) or private_glyph(span["font"], c, alone)
                        if c != ch["c"]:
                            symbols[ch["c"]] = c if symbols.get(ch["c"], c) == c else ""
                    remapped = c != ch["c"]
                    if unicodedata.category(c)[0] == "C" and c not in "\t":
                        continue
                    x0, y0, x1, y1 = ch["bbox"]
                    chars.append(Char(c=" " if SPACE.match(c) else c, x0=x0 / width, y0=y0 / height,
                                      x1=x1 / width, y1=y1 / height, size=span["size"], bold=bold, tall=tall,
                                      remapped=remapped))
            accented = _with_accents(chars, width)
            pt.accents |= accented is not chars
            chars = accented
            if any(ch.c != " " for ch in chars):
                pt.fragments.append(Fragment(chars, block_no))
    pt.fragments = _without_seams(_without_partial_overprints(_without_repeats(pt.fragments, width, height), width, height))
    pt.symbols = {code: c for code, c in symbols.items() if c}
    return pt


def _with_accents(chars: list[Char], page_width: float) -> list[Char]:
    """An accent drawn over a letter goes onto it: "´" + "e" is "é", "˜" + "ρ" is "ρ" and a combining
    tilde (a character each, so offsets stay one per character). The letter is the neighbour the
    accent sits on (after it, or before it for a cedilla); a space in between is no word break.
    A typed "´" beside a letter ("don´t") is left alone. A negation slash makes "=" "≠"."""
    if not any(ch.c in ACCENTS or ch.c == NEGATION for ch in chars):
        return chars
    drop: set[int] = set()
    marks: dict[int, Char] = {}
    out = list(chars)
    for i, a in enumerate(chars):
        if a.c == NEGATION:
            j = next((k for k in range(i + 1, len(chars)) if chars[k].c != " "), None)
            if j is None or (chars[j].x0 - a.x1) * page_width > 0.6 * a.size:
                continue
            negated = unicodedata.normalize("NFC", out[j].c + NEGATION)
            if len(negated) == 1:
                out[j] = replace(out[j], c=negated)
                drop.add(i)
            continue
        if a.c not in ACCENTS:
            continue
        after = next((k for k in range(i + 1, len(chars)) if chars[k].c != " "), None)
        before = next((k for k in range(i - 1, -1, -1) if chars[k].c != " "), None)
        j = next((k for k in (after, before) if k is not None and k not in drop and k not in marks
                  and unicodedata.category(chars[k].c).startswith("L")
                  and chars[k].x0 <= a.cx <= chars[k].x1), None)
        if j is None:
            continue
        letter = out[j]
        drop.add(i)
        drop.update(k for k in range(min(i, j) + 1, max(i, j)))
        # A space before the accent came from the accent's width when the letter touches the
        # character before it ("D ˛EBIEC"); in math a small gap stays a space ("𝔽p ˆG").
        k = i - 1 if j > i else i + 1
        if 0 < k < len(chars) and j > i and chars[k].c == " " and chars[k - 1].c != " " \
                and (letter.x0 - chars[k - 1].x1) * page_width <= 0.02 * letter.size:
            drop.add(k)
        accented = unicodedata.normalize("NFC", DOTLESS.get(letter.c, letter.c) + ACCENTS[a.c])
        if len(accented) == 1:
            out[j] = replace(letter, c=accented)
        else:
            marks[j] = replace(letter, c=ACCENTS[a.c])
    if not drop:
        return chars
    result = []
    for i, ch in enumerate(out):
        if i not in drop:
            result.append(ch)
            if i in marks:
                result.append(marks[i])
    return result


def tex_accents(text: str) -> str:
    """Accents of a page that draws them as glyphs of their own, put onto their letters in text
    without positions: "Fr´echet" is "Fréchet", "Gonc¸" is "Gonç", "b̸ = p" is "b ≠ p"."""
    def accent(m: re.Match) -> str:
        return unicodedata.normalize("NFC", DOTLESS.get(m.group(2), m.group(2)) + ACCENTS[m.group(1)])

    def negation(m: re.Match) -> str:
        negated = unicodedata.normalize("NFC", m.group(2) + NEGATION)
        return m.group(1) + negated if len(negated) == 1 else m.group(0)

    text = TEXT_ACCENT.sub(accent, text)
    text = TEXT_CEDILLA.sub(lambda m: unicodedata.normalize("NFC", m.group(0)[0] + ACCENTS[m.group(0)[1]]), text)
    return TEXT_NEGATION.sub(negation, text)


def _without_seams(fragments: list[Fragment]) -> list[Fragment]:
    """Some PDFs set a line as pieces overlapping by a character drawn twice ("셋째" + "째는 실리콘
    칩", "Netwo" + "orks on chip)"). As overlapping boxes they would become lines of their own and
    a column would read strip by strip; the later piece loses the repeated characters instead."""
    def mid(f: Fragment) -> float:
        return (f.y0 + f.y1) / 2

    ordered = sorted(fragments, key=mid)
    rows: list[list[Fragment]] = []
    for f in ordered:
        if rows and abs(mid(rows[-1][0]) - mid(f)) <= 0.3 * (f.y1 - f.y0):
            rows[-1].append(f)
        else:
            rows.append([f])
    for row in rows:
        row.sort(key=lambda f: f.ink_x0)
        for a, b in zip(row, row[1:]):
            # Same line: top and bottom agree (an index under an exponent shares only the middle).
            height = max(a.y1 - a.y0, b.y1 - b.y0)
            if not a.ink_x0 < b.ink_x0 < a.ink_x1 or abs(a.y0 - b.y0) > 0.15 * height or abs(a.y1 - b.y1) > 0.15 * height:
                continue
            repeated = [c for c in b.chars if c.c.strip() and (c.x0 + c.x1) / 2 < a.ink_x1]
            tail = [c for c in a.chars if c.c.strip()][-len(repeated):] if repeated else []
            if 0 < len(repeated) <= 2 and [c.c for c in tail] == [c.c for c in repeated] \
                    and all(abs(t.x0 - r.x0) < 0.5 * (r.x1 - r.x0) and abs(t.y0 - r.y0) < 0.2 * (r.y1 - r.y0)
                            and t.size == r.size for t, r in zip(tail, repeated)):
                gone = {id(c) for c in repeated}
                b.chars = [c for c in b.chars if id(c) not in gone]
    return [f for f in fragments if any(c.c.strip() for c in f.chars)]


def _without_repeats(fragments: list[Fragment], width: float, height: float) -> list[Fragment]:
    """A line drawn twice at nearly the same place (a text shadow, a slide's copy highlighted for
    an animation step, bold made by printing twice) is read once."""
    seen: dict[str, list[list[Char]]] = {}
    out = []
    for frag in fragments:
        text = "".join(c.c for c in frag.chars).strip()
        # Placed by the first visible character: one copy may start with spaces.
        ink = [c for c in frag.chars if c.c.strip()] or frag.chars
        x0, y0 = min(c.x0 for c in ink), min(c.y0 for c in ink)
        size = max(c.size for c in frag.chars)
        def same_print(previous: list[Char]) -> bool:
            dx = (min(c.x0 for c in previous) - x0) * width
            dy = (min(c.y0 for c in previous) - y0) * height
            # Preserve the established close-overprint rule: embedded artwork can
            # carry two almost coincident copies using different font metrics.
            if abs(dx) <= 0.25 * size and abs(dy) <= 0.25 * size:
                return True
            # Larger slide shadows can be 0.3 em away. Accept that displacement only
            # when every visible glyph has the same size and the same translation;
            # a neighbouring label or differently scaled copy is separate content.
            near = (0.4 if len(ink) >= 3 else 0.25) * size
            return (len(previous) == len(ink) and abs(dx) <= near and abs(dy) <= near
                    and all(abs(a.size - b.size) <= 0.02 * size
                        and abs((a.x0 - b.x0) * width - dx) <= 0.03 * size
                        and abs((a.y0 - b.y0) * height - dy) <= 0.03 * size
                        and abs((a.x1 - b.x1) * width - dx) <= 0.03 * size
                        for a, b in zip(previous, ink)))
        if any(same_print(previous) for previous in seen.get(text, [])):
            continue
        seen.setdefault(text, []).append(ink)
        out.append(frag)
    return out


def _without_partial_overprints(fragments: list[Fragment], width: float, height: float) -> list[Fragment]:
    """A fake-bold copy can split one line into several shorter PDF text runs.

    Remove a shorter run only when all its visible glyphs have a matching run
    inside a longer fragment at the same position and size. Separate occurrences
    and neighbouring labels remain distinct. Index glyph positions to avoid
    comparing every pair of lines on large pages.
    """
    import math

    indexed: dict[tuple[str, int, int], list[tuple[list[Char], int]]] = {}
    gone = set()
    runs = [(frag, [c for c in frag.chars if c.c.strip()]) for frag in fragments]
    for frag, ink in sorted(runs, key=lambda item: len(item[1]), reverse=True):
        if not ink:
            continue
        first = ink[0]
        size = max(c.size for c in ink)
        bx, by = math.floor(first.x0 * width / 4), math.floor(first.y0 * height / 4)
        # A one- or two-glyph remnant needs nearly coincident glyph boxes;
        # the wider shadow tolerance needs a longer matching run.
        tolerance = (.05 if len(ink) < 3 else .15) * size
        radius = math.ceil(tolerance / 4)
        duplicate = False
        for x in range(bx - radius, bx + radius + 1):
            for y in range(by - radius, by + radius + 1):
                for previous, start in indexed.get((first.c, x, y), []):
                    if len(previous) <= len(ink) or start + len(ink) > len(previous):
                        continue
                    part = previous[start:start + len(ink)]
                    dx, dy = (part[0].x0 - first.x0) * width, (part[0].y0 - first.y0) * height
                    if abs(dx) <= tolerance and abs(dy) <= tolerance and all(
                            a.c == b.c and abs(a.size - b.size) <= .02 * size
                            and abs((a.x0 - b.x0) * width - dx) <= .03 * size
                            and abs((a.y0 - b.y0) * height - dy) <= .03 * size
                            and abs((a.x1 - b.x1) * width - dx) <= .03 * size
                            for a, b in zip(part, ink)):
                        duplicate = True
                        break
                if duplicate:
                    break
            if duplicate:
                break
        if duplicate:
            gone.add(id(frag))
        else:
            for i, ch in enumerate(ink):
                key = (ch.c, math.floor(ch.x0 * width / 4), math.floor(ch.y0 * height / 4))
                indexed.setdefault(key, []).append((ink, i))
    return [frag for frag in fragments if id(frag) not in gone]

def _visual_lines(fragments: list[Fragment]) -> list[list[Char]]:
    """Merge fragments sharing a baseline into visual lines, top to bottom, left to right.

    A fragment that overlaps another horizontally (e.g. two text boxes stacked at the same
    height) starts its own line instead, so their characters are never interleaved.
    """
    fragments = _without_stacked_pieces(fragments)
    lines: list[tuple[float, float, list[Fragment]]] = []
    for frag in sorted(fragments, key=lambda f: ((f.y0 + f.y1) / 2, f.chars[0].x0)):
        mid = (frag.y0 + frag.y1) / 2
        fx0, fx1 = frag.ink_x0, frag.ink_x1
        target = None
        for i in range(len(lines) - 1, -1, -1):
            y0, y1, frags = lines[i]
            if not (y0 <= mid <= y1):
                break
            if all(fx1 <= g.ink_x0 + 0.002 or fx0 >= g.ink_x1 - 0.002 for g in frags):
                target = i
                break
        if target is None:
            lines.append((frag.y0, frag.y1, [frag]))
        else:
            y0, y1, frags = lines[target]
            lines[target] = (min(y0, frag.y0), max(y1, frag.y1), frags + [frag])
    lines = _with_raised_pieces(lines)
    return [
        [ch for f in sorted(frags, key=lambda g: g.ink_x0) for ch in f.chars]
        for _, _, frags in lines
    ]


def _with_raised_pieces(lines: list[tuple[float, float, list[Fragment]]]) -> list[tuple[float, float, list[Fragment]]]:
    """A piece in a smaller size set above or below a line (an exponent, an index) has its middle
    outside the line, so it came out as a line of its own: it goes back into the line it touches."""
    out: list[tuple[float, float, list[Fragment]]] = []
    for y0, y1, frags in lines:
        if out:
            p0, p1, prev = out[-1]
            size, prev_size = (max(c.size for f in fs for c in f.chars) for fs in (frags, prev))
            smaller = size <= 0.85 * prev_size or prev_size <= 0.85 * size
            apart = all(f.x1 <= g.x0 + 0.002 or f.x0 >= g.x1 - 0.002 for f in frags for g in prev)
            if smaller and min(y1, p1) - max(y0, p0) > 0 and apart:
                out[-1] = (min(y0, p0), max(y1, p1), prev + frags)
                continue
        out.append((y0, y1, frags))
    return out


def _without_stacked_pieces(fragments: list[Fragment]) -> list[Fragment]:
    """A tall delimiter is drawn as a stack of identical glyphs (‖ ‖ ‖): keep the first of each stack."""
    if not any(c.tall for f in fragments for c in f.chars):
        return fragments
    stacks: list[list] = []  # [first piece, top, bottom]

    def stacked(ch: Char) -> bool:
        for stack in stacks:
            first, top, bottom = stack
            touching = max(top, ch.y0) - min(bottom, ch.y1) <= 0.5 * (ch.y1 - ch.y0)
            if first.c == ch.c and first.x0 < ch.x1 and ch.x0 < first.x1 and touching:
                stack[1], stack[2] = min(top, ch.y0), max(bottom, ch.y1)
                return True
        stacks.append([ch, ch.y0, ch.y1])
        return False

    out = []
    for frag in sorted(fragments, key=lambda f: f.y0):
        chars = [ch for ch in frag.chars if not (ch.tall and stacked(ch))]
        if any(ch.c != " " for ch in chars):
            out.append(Fragment(chars, frag.block))
    return out


def _with_gap_spaces(chars: list[Char], page_width: float) -> list[Char]:
    """Insert a space where the gap between two characters is wide enough to be a word break.

    Some producers (notably PowerPoint) position words without writing space characters.
    """
    out: list[Char] = []
    for ch in chars:
        if out and ch.c != " " and out[-1].c != " ":
            prev = out[-1]
            gap_em = (ch.x0 - prev.x1) * page_width / max(prev.size, 1.0)
            if gap_em > WORD_GAP_EM:
                out.append(Char(c=" ", x0=prev.x1, y0=prev.y0, x1=ch.x0, y1=prev.y1, size=prev.size, bold=prev.bold, used=prev.used))
        out.append(ch)
    return out


def _last_word(text: list[str]) -> str:
    word = []
    for c in reversed(text):
        if c == " ":
            break
        word.append(c)
    return "".join(reversed(word))


def _first_word(chars: list[Char]) -> str:
    word = []
    for ch in chars:
        if ch.c == " ":
            break
        word.append(ch.c)
    return "".join(word)




def _with_superscripts(chars: list[Char]) -> list[Char]:
    """Digits set smaller and raised above the line (an exponent, cm², a footnote mark) become
    superscript characters, so "x2" in the text still means x². One character for one, so offsets
    and positions stay as they are."""
    sized = [c for c in chars if c.c.strip()]
    if len(sized) < 3:
        return chars
    sizes = sorted(c.size for c in sized)
    body = sizes[len(sizes) // 2]
    normal = [c for c in sized if abs(c.size - body) <= 0.05 * body]
    if len(normal) < 2:
        return chars
    bottoms = sorted(c.y1 for c in normal)
    heights = sorted(c.y1 - c.y0 for c in normal)
    base, height = bottoms[len(bottoms) // 2], heights[len(heights) // 2]
    raised = [c.size <= 0.85 * body and c.y1 < base - 0.2 * height and c.c.strip() != "" for c in chars]
    out, i = list(chars), 0
    while i < len(chars):
        if not raised[i]:
            i += 1
            continue
        j = i
        while j < len(chars) and raised[j]:
            j += 1
        # Only a run that is all digits and signs ("2", "−1", "0.3570"); "(kj)" stays as it is.
        run = chars[i:j]
        if (all(c.c in ".," or c.c.translate(SUPERSCRIPT) != c.c for c in run) and run[0].c not in ".,"
                and any(c.c.isdigit() or c.c in "+-−" for c in run)):
            out[i:j] = [replace(c, c=c.c.translate(SUPERSCRIPT)) for c in run]
        i = j
    return out


def build_text(fragments: list[Fragment], page_index: int, page_width: float = 595.0) -> tuple[str, list[dict], list[Char]]:
    """Text and line geometry for a set of fragments.

    Returns (text, lines, chars) where each line is {pageIndex, bbox, start, end, xs} with
    offsets into `text` and xs the normalized left edge of each character on the line.
    """
    text: list[str] = []
    lines: list[dict] = []
    used: list[Char] = []
    for chars in _visual_lines(fragments):
        chars = _with_gap_spaces(chars, page_width)
        # Collapse runs of spaces and trim the line.
        kept: list[Char] = []
        for ch in chars:
            if ch.c == " " and (not kept or kept[-1].c == " "):
                continue
            kept.append(ch)
        while kept and kept[-1].c == " ":
            kept.pop()
        if not kept:
            continue
        kept = _with_superscripts(kept)
        if text:
            prev = text[-1]
            first = kept[0].c
            if prev == "-" and len(text) > 1 and text[-2].isalpha() and first.islower():
                # Hyphenated word across lines: drop the hyphen and join directly.
                text.pop()
                last = lines[-1]
                last["end"] -= 1
                last["xs"].pop()
            elif NO_SPACE_SCRIPTS.match(prev) and NO_SPACE_SCRIPTS.match(first):
                pass
            elif HANGUL.match(prev) and HANGUL.match(first) and one_word(_last_word(text), _first_word(kept)):
                pass
            else:
                text.append(" ")
        start = len(text)
        text.extend(ch.c for ch in kept)
        used.extend(kept)
        lines.append({
            "pageIndex": page_index,
            "bbox": {"x0": min(c.x0 for c in kept), "y0": min(c.y0 for c in kept),
                     "x1": max(c.x1 for c in kept), "y1": max(c.y1 for c in kept)},
            "start": start,
            "end": len(text),
            "xs": [round(c.x0, 5) for c in kept],
        })
    for line in lines:
        b = line["bbox"]
        line["bbox"] = {k: round(min(1.0, max(0.0, v)), 5) for k, v in b.items()}
    return "".join(text), lines, used


def code_text(fragments: list[Fragment], page_width: float) -> str:
    """Text of a code block with its line breaks and indentation, which prose joining would lose.

    Indentation and runs of spaces are measured in units of the median character width, which
    is exact for the monospaced fonts code is set in.
    """
    lines = [_with_gap_spaces(chars, page_width) for chars in _visual_lines(fragments)]
    widths = sorted(ch.x1 - ch.x0 for chars in lines for ch in chars if ch.c != " " and ch.x1 > ch.x0)
    if not widths:
        return ""
    cell = widths[len(widths) // 2]
    left = min(ch.x0 for chars in lines for ch in chars)
    out = []
    for chars in lines:
        text, last_x1 = [], None
        for ch in chars:
            if ch.c == " ":
                continue
            if last_x1 is None:
                text.append(" " * max(0, round((ch.x0 - left) / cell)))
            elif ch.x0 - last_x1 > cell * 0.5:
                text.append(" " * max(1, round((ch.x0 - last_x1) / cell)))
            text.append(ch.c)
            last_x1 = ch.x1
        out.append("".join(text).rstrip())
    return "\n".join(out).strip("\n")


def strip_prefix(text: str, lines: list[dict], n: int) -> tuple[str, list[dict]]:
    """Remove the first n characters (e.g. a list bullet) and following spaces, shifting offsets."""
    while n < len(text) and text[n] == " ":
        n += 1
    out = []
    for line in lines:
        start, end = line["start"], line["end"]
        if end <= n:
            continue
        cut = max(0, n - start)
        out.append({**line, "start": max(0, start - n), "end": end - n, "xs": line["xs"][cut:]})
    return text[n:], out


def dominant_size(chars: list[Char]) -> tuple[float, bool]:
    """Most common font size (by character count) and whether most characters are bold."""
    sizes: dict[float, int] = {}
    bold = 0
    for ch in chars:
        if ch.c == " ":
            continue
        key = round(ch.size, 1)
        sizes[key] = sizes.get(key, 0) + 1
        bold += ch.bold
    if not sizes:
        return 0.0, False
    total = sum(sizes.values())
    return max(sizes, key=sizes.get), bold * 2 > total


_DASHES = str.maketrans("‐‑‒–—―−", "-------")


def similarity(a: str, b: str) -> float:
    """How alike two extractions of the same text are, ignoring whitespace, case, the kind of dash
    and private-use symbol codes (one side may have them mapped: "\uf061" and "α")."""
    import difflib

    na = re.sub(r"[\s\ue000-\uf8ff]+", "", unicodedata.normalize("NFKC", a)).lower().translate(_DASHES)
    nb = re.sub(r"[\s\ue000-\uf8ff]+", "", unicodedata.normalize("NFKC", b)).lower().translate(_DASHES)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()
