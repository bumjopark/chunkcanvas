"""Characters a font has no Unicode for (U+FFFD), read from the embedded font program.

TeX's math extension fonts (cmex, newtx's txex, Latin Modern's) often come without a usable
Unicode map, so an inline sum reads "�𝑘𝑖=1 𝑤𝑖 = 1". Their Type 1 programs still name every glyph
("summationtext", "braceleftbig"), and FreeType numbers a Type 1 font's glyphs in that order,
which gives the glyph id PyMuPDF reports for each character. A TrueType font embedded whole
(Word's Cambria Math) still carries its own cmap, which gives the character of a glyph id.
"""
import binascii
import re
import struct

import pymupdf

# Base glyph names (size and piece suffixes taken off) of math extension fonts.
BASE = {
    "parenleft": "(", "parenright": ")", "bracketleft": "[", "bracketright": "]", "braceleft": "{",
    "braceright": "}", "angbracketleft": "⟨", "angbracketright": "⟩", "angleleft": "⟨", "angleright": "⟩",
    "floorleft": "⌊", "floorright": "⌋", "ceilingleft": "⌈", "ceilingright": "⌉", "bar": "|", "bardbl": "‖",
    "slash": "/", "backslash": "\\", "radical": "√", "summation": "∑", "product": "∏", "coproduct": "∐",
    "integral": "∫", "contintegral": "∮", "contourintegral": "∮", "union": "⋃", "intersection": "⋂",
    "unionmulti": "⨄", "unionsq": "⨆", "circleplus": "⨁", "circlemultiply": "⨂", "circledot": "⨀",
    "logicaland": "⋀", "logicalor": "⋁", "hat": "ˆ", "tilde": "˜", "check": "ˇ",
    # Bars: cmex draws \big| and \big\| (and taller ones, piece by piece) with these.
    "vextendsingle": "|", "vextenddouble": "‖", "barex": "|", "bardblex": "‖",
}
SIZES = ("big", "Big", "bigg", "Bigg", "text", "display", "wide", "dsp")
# Pieces of a delimiter or radical drawn taller than one glyph: only its top piece says which.
PIECES = ("ex", "bt", "mid", "bot", "vertex", "extension")


def type1_names(font: bytes) -> list[str]:
    """A Type 1 program's glyph names in CharStrings order, .notdef first (FreeType's ids)."""
    if font[:1] == b"\x80":  # PFB segments: ASCII, then the binary encrypted part
        encrypted, i = b"", 0
        while i + 6 <= len(font) and font[i] == 0x80 and font[i + 1] in (1, 2):
            n = int.from_bytes(font[i + 2:i + 6], "little")
            if font[i + 1] == 2:
                encrypted += font[i + 6:i + 6 + n]
            i += 6 + n
    else:
        rest = font[font.index(b"eexec") + 5:]
        rest = rest[2:] if rest[:2] == b"\r\n" else rest[1:]
        if re.fullmatch(rb"[0-9A-Fa-f\s]+", rest[:64]):
            digits = re.sub(rb"[^0-9A-Fa-f]", b"", rest.split(b"cleartomark")[0])
            encrypted = binascii.unhexlify(digits[: len(digits) // 2 * 2])
        else:
            encrypted = rest
    key, plain = 55665, bytearray()
    for c in encrypted:
        plain.append(c ^ (key >> 8))
        key = ((c + key) * 52845 + 22719) & 0xFFFF
    text = bytes(plain[4:])
    pos = text.index(b"begin", text.index(b"/CharStrings")) + 5
    entry, token = re.compile(rb"\s*/([^\s/\[\]{}()<>]+)\s+(\d+)\s+\S+\s"), re.compile(rb"\s*\S+")
    names = []
    while m := entry.match(text, pos):
        names.append(m.group(1).decode("latin1"))
        pos = token.match(text, m.end() + int(m.group(2))).end()  # past the charstring and "ND"
    if ".notdef" in names:
        i = names.index(".notdef")
        names[0], names[i] = names[i], names[0]
    return names


def meaning(name: str) -> str | None:
    """The character a math extension glyph stands for; "" for a piece of a tall delimiter."""
    name = name.split(".")[0]
    if m := re.fullmatch(r"(?:uni|u)([0-9A-F]{4,6})", name):
        return chr(int(m.group(1), 16))
    for _ in range(3):
        if name in BASE:
            return BASE[name]
        if name.endswith(PIECES) and not name.endswith(SIZES):
            return ""
        if name.endswith("tp"):
            name = name[:-2]
            continue
        size = next((s for s in SIZES if name.endswith(s)), None)
        if not size:
            return None
        name = name[: -len(size)]
    return None


def _tables(font: bytes, base: int = 0) -> dict[str, tuple[int, int]]:
    count = struct.unpack(">H", font[base + 4:base + 6])[0]
    out = {}
    for i in range(count):
        tag, _, offset, length = struct.unpack(">4sIII", font[base + 12 + 16 * i:base + 28 + 16 * i])
        out[tag.decode("latin1")] = (offset, length)
    return out


def _postscript_name(font: bytes, base: int) -> str:
    offset, _ = _tables(font, base)["name"]
    count, strings = struct.unpack(">HH", font[offset + 2:offset + 6])
    for i in range(count):
        platform, encoding, _, name_id, length, at = struct.unpack(">6H", font[offset + 6 + 12 * i:offset + 18 + 12 * i])
        if name_id == 6:
            raw = font[offset + strings + at:offset + strings + at + length]
            return raw.decode("utf-16-be" if platform in (0, 3) else "latin1", "replace")
    return ""


def truetype_chars(font: bytes, name: str) -> dict[int, str]:
    """Glyph id -> the character a TrueType font's own Unicode cmap gives it. A whole font
    embedded (Word embeds Cambria Math so, as a collection) still has its cmap; the face of a
    collection is picked by its PostScript name."""
    base = 0
    if font[:4] == b"ttcf":
        count = struct.unpack(">I", font[8:12])[0]
        faces = struct.unpack(f">{count}I", font[12:12 + 4 * count])
        base = next((f for f in faces if _postscript_name(font, f).replace(" ", "") == name), faces[0])
    offset, _ = _tables(font, base)["cmap"]
    subtables = {}
    for i in range(struct.unpack(">H", font[offset + 2:offset + 4])[0]):
        platform, encoding, at = struct.unpack(">HHI", font[offset + 4 + 8 * i:offset + 12 + 8 * i])
        # Unicode tables only (a symbol font's (3, 0) table holds private-use codes).
        if platform == 0 or (platform == 3 and encoding in (1, 10)):
            subtables.setdefault(struct.unpack(">H", font[offset + at:offset + at + 2])[0], offset + at)
    pairs: list[tuple[int, int]] = []
    if 12 in subtables:
        at = subtables[12]
        for g in range(struct.unpack(">I", font[at + 12:at + 16])[0]):
            first, last, glyph = struct.unpack(">III", font[at + 16 + 12 * g:at + 28 + 12 * g])
            pairs += [(code, glyph + code - first) for code in range(first, min(last, first + 0x10000) + 1)]
    elif 4 in subtables:
        at = subtables[4]
        n2 = struct.unpack(">H", font[at + 6:at + 8])[0]
        n = n2 // 2
        ends = struct.unpack(f">{n}H", font[at + 14:at + 14 + n2])
        starts = struct.unpack(f">{n}H", font[at + 16 + n2:at + 16 + 2 * n2])
        deltas = struct.unpack(f">{n}h", font[at + 16 + 2 * n2:at + 16 + 3 * n2])
        ranges_at = at + 16 + 3 * n2
        ranges = struct.unpack(f">{n}H", font[ranges_at:ranges_at + n2])
        for k in range(n):
            for code in range(starts[k], ends[k] + 1):
                if code == 0xFFFF:
                    continue
                if ranges[k]:
                    p = ranges_at + 2 * k + ranges[k] + 2 * (code - starts[k])
                    glyph = struct.unpack(">H", font[p:p + 2])[0]
                    glyph = (glyph + deltas[k]) & 0xFFFF if glyph else 0
                else:
                    glyph = (code + deltas[k]) & 0xFFFF
                pairs.append((code, glyph))
    out: dict[int, str] = {}
    for code, glyph in pairs:
        if glyph and not 0xE000 <= code <= 0xF8FF and glyph not in out:
            out[glyph] = chr(code)
    return out


def _font_program(doc, xref: int) -> bytes | None:
    """The embedded TrueType program of a font (through a Type 0 font's descendant)."""
    obj = doc.xref_object(xref)
    if m := re.search(r"/DescendantFonts\s*\[?\s*(\d+) 0 R", obj):
        obj = doc.xref_object(int(m.group(1)))
    if not (m := re.search(r"/FontDescriptor (\d+) 0 R", obj)):
        return None
    m = re.search(r"/FontFile2 (\d+) 0 R", doc.xref_object(int(m.group(1))))
    return doc.xref_stream(int(m.group(1))) if m else None


def _lookup(doc, xref: int, kind: str, name: str):
    """gid -> character for one font, or None when its program gives no answer."""
    cache = doc.__dict__.setdefault("_glyph_lookups", {})
    if xref not in cache:
        try:
            if kind == "Type1":
                names = type1_names(doc.extract_font(xref)[3])
                cache[xref] = lambda gid: meaning(names[gid]) if 0 <= gid < len(names) else None
            else:
                program = _font_program(doc, xref)
                chars = truetype_chars(program, name) if program else {}
                cache[xref] = chars.get if chars else None
        except (ValueError, IndexError, KeyError, AttributeError, struct.error, UnicodeDecodeError):
            cache[xref] = None
    return cache[xref]


def page_glyphs(page: "pymupdf.Page") -> dict[tuple[float, float], str]:
    """Origin of each U+FFFD character of an embedded Type 1 or TrueType font on the page -> what
    its glyph is."""
    fonts: dict[str, list[tuple[int, str]]] = {}
    for xref, _, kind, name, *_ in page.get_fonts():
        if kind in ("Type1", "TrueType", "Type0"):
            fonts.setdefault(name.split("+")[-1], []).append((xref, kind))
    if not fonts:
        return {}
    out = {}
    for span in page.get_texttrace():
        found = fonts.get(span["font"])
        # Two subsets of one font on a page would share ids: leave those alone.
        if not found or len(found) > 1 or not any(ch[0] == 0xFFFD for ch in span["chars"]):
            continue
        lookup = _lookup(page.parent, found[0][0], found[0][1], span["font"])
        if lookup is None:
            continue
        for code, gid, origin, _ in span["chars"]:
            if code == 0xFFFD and (char := lookup(gid)) is not None:
                out[(round(origin[0], 2), round(origin[1], 2))] = char
    return out
