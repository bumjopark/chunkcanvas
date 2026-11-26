"""Keep Hangul picture data inside the document passed to LibreOffice."""
import base64
import io
import re
import resource
import zipfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from lxml import etree
from PIL import Image

MAX_IMAGE = 30 * 1024 * 1024
# Switch import paths when duplicate HTML attributes alone would cost tens of MiB.
INLINE_REPEAT_LIMIT = 24 * 1024 * 1024
RASTER_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "BMP": "image/bmp", "GIF": "image/gif"}
TEXT_PAGE = "hangul-text.html"
_TAG = re.compile(rb'<img src="cc-picture-(\d+)"([^>]*)>')
_DRAW = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"
_SVG = "{urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0}"
_MAN = "{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}"
_HREF = "{http://www.w3.org/1999/xlink}href"


def file_limit() -> int:
    soft, _ = resource.getrlimit(resource.RLIMIT_FSIZE)
    return 512 * 1024 * 1024 if soft == resource.RLIM_INFINITY else soft


@dataclass(frozen=True)
class Picture:
    slot: int
    width: int
    height: int
    mime: str
    raw: Path
    size: int

    @property
    def member(self) -> str:
        return f"Pictures/cc-{self.slot}.{self.mime.split('/')[-1]}"


class EmbeddedPictures:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.references: dict[str, Picture | None] = {}
        self.sources: list[Picture] = []
        self.pack = False
        self.omitted = False

    def get(self, key: str, open_member: Callable[[], BinaryIO]) -> Picture | None:
        """The key selects a stream inside the document, never a filesystem path."""
        if key not in self.references:
            try:
                with closing(open_member()) as member:
                    data = member.read(MAX_IMAGE + 1)
            except (KeyError, OSError):
                return None
            self.references[key] = self._store(data)
        return self.references[key]

    def _store(self, data: bytes) -> Picture | None:
        if len(data) > MAX_IMAGE:
            self.omitted = True
            return None
        if not data:
            return None
        try:
            with Image.open(io.BytesIO(data), formats=list(RASTER_TYPES)) as image:
                width, height = image.size
                mime = RASTER_TYPES[image.format]
        except (OSError, ValueError, Image.DecompressionBombError):
            return None
        slot = len(self.sources)
        raw = self.workdir / f"raster-{slot}"
        raw.write_bytes(data)
        picture = Picture(slot, width, height, mime, raw, len(data))
        self.sources.append(picture)
        return picture

    def write_page(self, markup: str) -> Path:
        page = self.workdir / "document.html"
        template = markup.encode("utf-8")
        if not self.sources:
            page.write_bytes(template)
            return page
        (self.workdir / TEXT_PAGE).write_bytes(template)
        total, duplicate = len(template), 0
        used = set()
        for match in _TAG.finditer(template):
            slot = int(match[1])
            pic = self.sources[slot]
            uri_len = len(pic.mime) + 13 + 4 * ((pic.size + 2) // 3)
            total += uri_len - len(f"cc-picture-{slot}")
            if slot in used:
                duplicate += uri_len
            used.add(slot)
        self.pack = total > file_limit() or duplicate > INLINE_REPEAT_LIMIT
        # An internal 1px image reserves the frame; its title identifies the original.
        if self.pack:
            pixel = io.BytesIO()
            Image.new("RGB", (1, 1), "white").save(pixel, "PNG")
            marker = base64.b64encode(pixel.getvalue())
            # Even tiny markers can exceed the file limit in a very long document.
            layout_size = len(template)
            for match in _TAG.finditer(template):
                layout_size += len(self._layout_tag(match, marker)) - len(match[0])
                if layout_size > file_limit():
                    self.pack = False
                    return self.text_only()
        with page.open("wb") as output:
            start = 0
            for match in _TAG.finditer(template):
                output.write(template[start:match.start()])
                pic = self.sources[int(match[1])]
                if self.pack:
                    output.write(self._layout_tag(match, marker))
                else:
                    output.write(f'<img src="data:{pic.mime};base64,'.encode())
                    output.write(base64.b64encode(pic.raw.read_bytes()))
                    output.write(b'"' + match[2] + b'>')
                start = match.end()
            output.write(template[start:])
        return page

    def _layout_tag(self, match: re.Match, marker: bytes) -> bytes:
        pic = self.sources[int(match[1])]
        attrs = match[2].rstrip(b"/")
        if b' width="' not in attrs:
            attrs += f' width="{pic.width}" height="{pic.height}"'.encode()
        return (b'<img src="data:image/png;base64,' + marker
                + f'" alt="cc-picture-{pic.slot}"'.encode() + attrs + b'/>')

    def fill_package(self, layout: Path) -> Path | None:
        """The Writer-generated layout supplies the frames. Only our numbered assets fill them."""
        output = self.workdir / "pictures.odt"
        with zipfile.ZipFile(layout) as source:
            content = etree.fromstring(source.read("content.xml"))
            manifest = etree.fromstring(source.read("META-INF/manifest.xml"))
            originals: dict[int, Picture] = {}
            skipped: set[int] = set()
            # Store uncompressed members. Reserve room for the layout, ZIP headers and
            # manifest additions before choosing each original in reading order.
            remaining = file_limit() - sum(i.file_size + 256 + 2 * len(i.filename.encode())
                                           for i in source.infolist()) - 4096
            for frame in list(content.iter(_DRAW + "frame")):
                image = frame.find(_DRAW + "image")
                if image is None:
                    continue
                title = frame.find(_SVG + "title")
                if title is None or not (slot := re.fullmatch(r"cc-picture-(\d+)", title.text or "")):
                    raise ValueError("Missing Hangul picture slot in the generated layout")
                pic = self.sources[int(slot[1])]
                if pic.slot not in originals and pic.slot not in skipped:
                    if pic.size + 512 <= remaining:
                        originals[pic.slot] = pic
                        remaining -= pic.size + 512
                    else:
                        skipped.add(pic.slot)
                if pic.slot in skipped:
                    parent, previous = frame.getparent(), frame.getprevious()
                    if frame.tail:
                        if previous is None:
                            parent.text = (parent.text or "") + frame.tail
                        else:
                            previous.tail = (previous.tail or "") + frame.tail
                    parent.remove(frame)
                    continue
                image.set(_HREF, pic.member)
                image.set(_DRAW + "mime-type", pic.mime)
                frame.remove(title)
            self.omitted = self.omitted or bool(skipped)
            if not originals:
                return None
            for entry in list(manifest):
                if entry.get(_MAN + "full-path", "").startswith("Pictures/"):
                    manifest.remove(entry)
            for pic in originals.values():
                entry = etree.SubElement(manifest, _MAN + "file-entry")
                entry.set(_MAN + "full-path", pic.member)
                entry.set(_MAN + "media-type", pic.mime)
            changed = {"content.xml": etree.tostring(content), "META-INF/manifest.xml": etree.tostring(manifest)}
            with zipfile.ZipFile(output, "w") as target:
                for info in source.infolist():
                    if not info.filename.startswith("Pictures/"):
                        data = changed.get(info.filename)
                        target.writestr(info.filename, data if data is not None else source.read(info))
                for pic in originals.values():
                    target.write(pic.raw, pic.member)
        return output

    def text_only(self) -> Path:
        self.omitted = True
        path = self.workdir / "text-only.html"
        markup = _TAG.sub(b"", (self.workdir / TEXT_PAGE).read_bytes())
        # Image-only documents still need a visible explanation on the resulting page.
        from lxml import html
        body = html.document_fromstring(markup).find("body")
        if body is not None and not body.text_content().strip():
            markup = markup.replace(b"</body>", b"<p>[Pictures omitted]</p></body>")
        path.write_bytes(markup)
        return path
