"""Make services/api/app/parsers/opensymbol.json: the OpenSymbol glyph drawn for each symbol code.

    services/api/.venv/bin/python scripts/opensymbol_table.py

LibreOffice draws text set in Symbol or Wingdings with OpenSymbol, often with a glyph whose
OpenSymbol code is private ("☒") or a look-alike ("˚" for Symbol's "°"). This converts every
Symbol and Wingdings code with the configured LibreOffice, finds the OpenSymbol glyph drawn for
each, and records it with the character the code means in that font.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api"))

import pymupdf  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.parsers import opensymbol  # noqa: E402
from app.parsers.textlayer import _SYMBOL, _WINGDINGS  # noqa: E402

FONTS = {"Symbol": _SYMBOL, "Wingdings": _WINGDINGS}
DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
 office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.text">
<office:font-face-decls><style:font-face style:name="F" svg:font-family="'{font}'" style:font-charset="x-symbol"/>
</office:font-face-decls>
<office:automatic-styles><style:style style:name="T" style:family="text">
<style:text-properties style:font-name="F"/></style:style></office:automatic-styles>
<office:body><office:text>{body}</office:text></office:body></office:document>"""


def drawn(pdf: Path) -> dict[int, int]:
    """Original code -> OpenSymbol code of the glyph drawn for it (one symbol font per file)."""
    doc = pymupdf.open(pdf)
    known = opensymbol.reference()
    out = {}
    for page in doc:
        subsets = [opensymbol.outlines(doc.extract_font(x[0])[3]) for x in page.get_fonts()
                   if x[3].split("+")[-1] == "OpenSymbol"]
        for span in page.get_texttrace():
            if span["font"] != "OpenSymbol":
                continue
            for code, gid, _, _ in span["chars"]:
                targets = {known.get(prints.get(gid, "")) for prints, _, _ in subsets} - {None}
                if len(targets) == 1:
                    out[code] = targets.pop()
    return out


def main() -> None:
    soffice = get_settings().soffice_path
    if soffice is None or opensymbol.installed_font() is None:
        sys.exit("LibreOffice not found (set CC_SOFFICE)")
    table: dict[str, dict[str, str]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for i, (font, chars) in enumerate(FONTS.items()):
            body = "".join(f'<text:p><text:span text:style-name="T">{chr(0xF000 + c)}</text:span></text:p>'
                           for c in range(0x21, 0x100) if c != 0x7F)
            source = work / f"font{i}.fodt"
            source.write_text(DOCUMENT.format(font=font, body=body), encoding="utf-8")
            subprocess.run([soffice, f"-env:UserInstallation=file://{work}/profile", "--headless",
                            "--convert-to", "pdf", "--outdir", str(work), str(source)],
                           check=True, capture_output=True, timeout=300)
            for code, target in drawn(work / f"font{i}.pdf").items():
                char = chars.get(code)
                if char and char not in "\x00\x7f":
                    table.setdefault(f"{target:04X}", {})[f"{code:04X}"] = char
    out = ROOT / "services/api/app/parsers/opensymbol.json"
    out.write_text(json.dumps(dict(sorted(table.items())), ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    print(f"{sum(map(len, table.values()))} codes on {len(table)} glyphs -> {out}")


if __name__ == "__main__":
    main()
