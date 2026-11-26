"""Symbols LibreOffice drew with OpenSymbol, read from the glyphs drawn.

Office text set in Symbol or Wingdings keeps the font's own codes (U+F020-F0FF) in the PDF that
LibreOffice writes, but the glyph drawn comes from OpenSymbol. U+F070 is "π" in Symbol and a box
in Wingdings, and only the drawing tells which. So each OpenSymbol glyph embedded in the PDF is
matched by its outline to the installed OpenSymbol. opensymbol.json, which
scripts/opensymbol_table.py makes from LibreOffice's conversion of every Symbol and Wingdings code,
lists the glyph drawn for each code. A glyph drawn for a listed code means that font's character
("π", "☒"); any other glyph means the character OpenSymbol has it for.
"""
import functools
import hashlib
import json
import struct
import unicodedata
from pathlib import Path

import pymupdf

FONT = "share/fonts/truetype/opens___.ttf"
# OpenSymbol code drawn -> {original code: character} (hex keys).
DRAWN = json.loads((Path(__file__).parent / "opensymbol.json").read_text(encoding="utf-8"))


def _tables(font: bytes) -> dict[str, bytes]:
    count = struct.unpack(">H", font[4:6])[0]
    out = {}
    for i in range(count):
        tag, _, offset, length = struct.unpack(">4sIII", font[12 + 16 * i: 28 + 16 * i])
        out[tag.decode("latin1")] = font[offset: offset + length]
    return out


def outlines(font: bytes) -> tuple[dict[int, str], dict[int, int], int]:
    """A TrueType font's glyphs as outline fingerprints (glyph id -> hash), with their advance
    widths and the font's units per em. Hints are left out (subsetting may drop them), and a
    composite glyph is hashed through its parts, whose ids a subset renumbers."""
    t = _tables(font)
    long_offsets = struct.unpack(">h", t["head"][50:52])[0]
    units = struct.unpack(">H", t["head"][18:20])[0]
    count = struct.unpack(">H", t["maxp"][4:6])[0]
    if long_offsets:
        offsets = struct.unpack(f">{count + 1}I", t["loca"][: 4 * (count + 1)])
    else:
        offsets = [2 * x for x in struct.unpack(f">{count + 1}H", t["loca"][: 2 * (count + 1)])]
    glyf = t["glyf"]
    metrics = struct.unpack(">H", t["hhea"][34:36])[0]
    advances = {g: struct.unpack(">H", t["hmtx"][4 * min(g, metrics - 1): 4 * min(g, metrics - 1) + 2])[0]
                for g in range(count)}

    @functools.cache
    def fingerprint(g: int) -> str:
        data = glyf[offsets[g]: offsets[g + 1]] if g < count else b""
        if len(data) < 10:
            return ""
        contours = struct.unpack(">h", data[:2])[0]
        if contours >= 0:
            ends = data[10: 10 + 2 * contours]
            hints = struct.unpack(">H", data[10 + 2 * contours: 12 + 2 * contours])[0]
            body = data[:10] + ends + data[12 + 2 * contours + hints:].rstrip(b"\0")
        else:
            body, pos, more = data[:10], 10, True
            while more and pos + 4 <= len(data):
                flags, part = struct.unpack(">HH", data[pos: pos + 4])
                size = (4 if flags & 1 else 2) + (2 if flags & 8 else 4 if flags & 0x40 else 8 if flags & 0x80 else 0)
                body += struct.pack(">H", flags & 0xFEFF) + fingerprint(part).encode() + data[pos + 4: pos + 4 + size]
                pos, more = pos + 4 + size, bool(flags & 0x20)
        return hashlib.sha1(body).hexdigest()[:20]

    return {g: fingerprint(g) for g in range(count)}, advances, units


def _cmap(font: bytes) -> dict[int, list[int]]:
    """Glyph id -> the codes a format-4 Unicode cmap gives it."""
    c = _tables(font)["cmap"]
    offset = None
    for i in range(struct.unpack(">H", c[2:4])[0]):
        platform, _, at = struct.unpack(">HHI", c[4 + 8 * i: 12 + 8 * i])
        if platform in (0, 3) and struct.unpack(">H", c[at: at + 2])[0] == 4:
            offset = at
    out: dict[int, list[int]] = {}
    if offset is None:
        return out
    n2 = struct.unpack(">H", c[offset + 6: offset + 8])[0]
    n = n2 // 2
    ends = struct.unpack(f">{n}H", c[offset + 14: offset + 14 + n2])
    starts = struct.unpack(f">{n}H", c[offset + 16 + n2: offset + 16 + 2 * n2])
    deltas = struct.unpack(f">{n}h", c[offset + 16 + 2 * n2: offset + 16 + 3 * n2])
    at_ranges = offset + 16 + 3 * n2
    ranges = struct.unpack(f">{n}H", c[at_ranges: at_ranges + n2])
    for s in range(n):
        for code in range(starts[s], ends[s] + 1):
            if code == 0xFFFF:
                continue
            if ranges[s]:
                p = at_ranges + 2 * s + ranges[s] + 2 * (code - starts[s])
                g = struct.unpack(">H", c[p: p + 2])[0]
                g = (g + deltas[s]) & 0xFFFF if g else 0
            else:
                g = (code + deltas[s]) & 0xFFFF
            if g:
                out.setdefault(g, []).append(code)
    return out


def _private(code: int) -> bool:
    return 0xE000 <= code <= 0xF8FF


def _rank(code: int) -> tuple:
    # A visible standard character first: OpenSymbol shares glyphs with private and format codes.
    return _private(code), unicodedata.category(chr(code))[0] == "C", code


def installed_font() -> Path | None:
    """OpenSymbol of the LibreOffice that converts uploads, or of a system install."""
    from ..config import get_settings

    soffice = get_settings().soffice_path
    places = [soffice.resolve().parents[1] / FONT] if soffice else []
    places.append(Path("/usr/share/fonts/truetype/libreoffice/opens___.ttf"))
    return next((p for p in places if p.is_file()), None)


@functools.cache
def reference() -> dict[str, int]:
    """Outline fingerprint -> OpenSymbol code, preferring a standard character where OpenSymbol
    gives one glyph several codes."""
    path = installed_font()
    if path is None:
        return {}
    font = path.read_bytes()
    prints, _, _ = outlines(font)
    out: dict[str, int] = {}
    for g, codes in _cmap(font).items():
        if prints.get(g):
            best = min(codes, key=_rank)
            old = out.get(prints[g])
            if old is None or _rank(old) > _rank(best):
                out[prints[g]] = best
    return out


def meaning(target: int, code: int) -> str | None:
    """What OpenSymbol's glyph `target`, drawn for the original code `code`, stands for."""
    listed = DRAWN.get(f"{target:04X}", {}).get(f"{code:04X}")
    return listed or (None if _private(target) else chr(target))


def page_glyphs(page: "pymupdf.Page") -> dict[tuple[float, float], str]:
    """Origin of each OpenSymbol private-use character on the page -> the character drawn, where
    the drawing is known."""
    subsets = []
    for xref, _, kind, name, *_ in page.get_fonts():
        if name.split("+")[-1] == "OpenSymbol" and kind == "TrueType":
            try:
                subsets.append(outlines(page.parent.extract_font(xref)[3]))
            except (KeyError, IndexError, ValueError, RuntimeError, struct.error):
                continue  # not a font this can read: the rules in textlayer decide
    known = reference() if subsets else {}
    if not known:
        return {}
    out = {}
    for span in page.get_texttrace():
        if span["font"] != "OpenSymbol":
            continue
        for code, gid, origin, bbox in span["chars"]:
            if not 0xF020 <= code <= 0xF0FF:
                continue
            width = (bbox[2] - bbox[0]) / span["size"] if span["size"] else 0
            found = set()
            for prints, advances, units in subsets:
                target = known.get(prints.get(gid, ""))
                # Several subsets on one page share glyph ids: the drawn advance tells them apart.
                if target is not None and (len(subsets) == 1 or abs(advances.get(gid, 0) / units - width) < 0.01):
                    found.add(meaning(target, code))
            if len(found) == 1 and None not in found:
                out[(round(origin[0], 2), round(origin[1], 2))] = found.pop()
    return out
