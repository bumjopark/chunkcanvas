"""Recognize uploaded files by their content (magic bytes and container structure), not by name.

Every non-PDF format is converted to PDF before parsing (see convert.py); this module only
decides what a file is, and rejects archives that would expand to absurd sizes.
"""
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "png",
    b"\xff\xd8\xff": "jpeg",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"II*\x00": "tiff",
    b"MM\x00*": "tiff",
}
# Other signatures LibreOffice reads: Hangul 3.0 (before HWP 5) and WordPerfect.
OTHER_MAGIC = {b"HWP Document File V3": "hwp3", b"\xffWPC": "wpd"}
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK\x03\x04"

ODF = {
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.graphics": "odg",
    # Templates use different package MIME types but the same conversion path.
    "application/vnd.oasis.opendocument.text-template": "odt",
    "application/vnd.oasis.opendocument.presentation-template": "odp",
    "application/vnd.oasis.opendocument.spreadsheet-template": "ods",
    "application/vnd.sun.xml.writer": "sxw",
    "application/vnd.sun.xml.calc": "sxc",
    "application/vnd.sun.xml.impress": "sxi",
    "application/epub+zip": "epub",
}
TEXT_EXTENSIONS = {".md": "md", ".markdown": "md", ".html": "html", ".htm": "html", ".xhtml": "html", ".csv": "csv",
                   ".tsv": "csv", ".txt": "txt", ".text": "txt", ".log": "txt"}
# Single-file XML documents LibreOffice opens: flat ODF, Word 2003 XML, Excel 2003 XML.
FLAT_XML = (
    (b'office:mimetype="application/vnd.oasis.opendocument.text"', "fodt"),
    (b'office:mimetype="application/vnd.oasis.opendocument.spreadsheet"', "fods"),
    (b'office:mimetype="application/vnd.oasis.opendocument.presentation"', "fodp"),
    (b'office:mimetype="application/vnd.oasis.opendocument.graphics"', "fodg"),
    (b"<w:wordDocument", "wordml"),
    (b'<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"', "xmlss"),
)
IWORK = {".pages": "pages", ".numbers": "numbers", ".key": "key"}

# Formats grouped by how they become a PDF.
IMAGES = {"png", "jpeg", "gif", "tiff", "bmp", "webp"}
OFFICE = {"docx", "doc", "pptx", "ppt", "xlsx", "xls", "xlsb", "odt", "odp", "ods", "odg", "rtf", "sxw", "sxc", "sxi",
          "fodt", "fods", "fodp", "fodg", "wordml", "xmlss", "wpd", "pages", "numbers", "key"}
MARKUP = {"html", "md", "txt", "csv", "epub"}
HANGUL = {"hwp", "hwpx", "hwp3"}
# Fixed-layout documents MuPDF turns into PDF pages directly.
FIXED = {"xps"}
# Zip packages whose parts are rewritten without external links and macros before LibreOffice
# opens them; flat XML documents get the same treatment as text.
PACKAGES = {"docx", "pptx", "xlsx", "xlsb", "odt", "odp", "ods", "odg", "sxw", "sxc", "sxi"}
FLAT = {"fodt", "fods", "fodp", "fodg", "wordml", "xmlss"}
# New Office formats require isolation until their active parts are covered.
# XLSB also has binary workbook parts outside the package XML sanitizer.
REQUIRES_ISOLATION = (OFFICE - PACKAGES - FLAT) | {"xlsb", "hwp3"}
ALL = {"pdf"} | IMAGES | OFFICE | MARKUP | HANGUL | FIXED

# What users see in the file picker, by format.
EXTENSIONS = {
    "pdf": [".pdf"], "png": [".png"], "jpeg": [".jpg", ".jpeg"], "gif": [".gif"], "tiff": [".tif", ".tiff"],
    "bmp": [".bmp"], "webp": [".webp"],
    "docx": [".docx", ".docm", ".dotx", ".dotm"], "doc": [".doc", ".dot"],
    "pptx": [".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm"], "ppt": [".ppt", ".pps", ".pot"],
    "xlsx": [".xlsx", ".xlsm", ".xltx", ".xltm"], "xls": [".xls", ".xlt"], "xlsb": [".xlsb"],
    "odt": [".odt", ".ott"], "odp": [".odp", ".otp"], "ods": [".ods", ".ots"], "odg": [".odg"], "rtf": [".rtf"],
    "sxw": [".sxw"], "sxc": [".sxc"], "sxi": [".sxi"], "fodt": [".fodt"], "fods": [".fods"], "fodp": [".fodp"],
    "fodg": [".fodg"], "wordml": [".xml"], "xmlss": [".xml"], "wpd": [".wpd"],
    "pages": [".pages"], "numbers": [".numbers"], "key": [".key"],
    "html": [".html", ".htm", ".xhtml"], "md": [".md", ".markdown"], "txt": [".txt", ".text", ".log"],
    "csv": [".csv", ".tsv"], "epub": [".epub"], "xps": [".xps", ".oxps"],
    "hwp": [".hwp"], "hwpx": [".hwpx"], "hwp3": [".hwp"],
}

MAX_ZIP_MEMBER = 200 * 1024 * 1024
MAX_ZIP_ENTRIES = 16_384
MAX_ZIP_DIRECTORY = 4 * 1024 * 1024
CORRUPT = ("corrupt_file", "The file is damaged.")
DIRECTORY_LIMIT = ("too_large", "The archive directory exceeds the server limit.")
MAX_ZIP_TOTAL = 500 * 1024 * 1024


@dataclass
class Detected:
    format: str | None
    # A user-facing reason when the file is recognized but cannot be processed.
    error: tuple[str, str] | None = None


def zip_prefix(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    with archive.open(name) as stream:
        return stream.read(limit)


def _zip_format(path: Path, name: str = "") -> Detected:
    try:
        # ZipFile constructs all ZipInfo objects in its constructor. Inspect the
        # bounded end record (including ZIP64) before allowing that allocation.
        with path.open("rb") as stream:
            end = zipfile._EndRecData(stream)
            if end is None:
                return Detected(None, CORRUPT)
            if end[zipfile._ECD_ENTRIES_TOTAL] > MAX_ZIP_ENTRIES or end[zipfile._ECD_SIZE] > MAX_ZIP_DIRECTORY:
                return Detected(None, DIRECTORY_LIMIT)
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                return Detected(None, DIRECTORY_LIMIT)
            if any(i.file_size > MAX_ZIP_MEMBER for i in infos) or sum(i.file_size for i in infos) > MAX_ZIP_TOTAL:
                return Detected(None, ("too_large", "The file expands to more data than this server accepts."))
            names = {i.filename for i in infos}
            # Encrypted entries (a password-protected Pages file, an encrypted zip).
            # (Password-protected iWork '09 files scramble the compression method instead.)
            if any(i.flag_bits & 0x1 or i.compress_type not in (0, 8, 9, 12, 14) for i in infos):
                return Detected(None, ENCRYPTED)
            if "META-INF/manifest.xml" in names and b"encryption-data" in zip_prefix(z, "META-INF/manifest.xml", 200000):
                return Detected(None, ENCRYPTED)
            if "mimetype" in names:
                mime = zip_prefix(z, "mimetype", 200).decode("ascii", "ignore").strip()
                if mime == "application/hwp+zip":
                    return Detected("hwpx")
                if mime in ODF:
                    return Detected(ODF[mime])
            elif "META-INF/manifest.xml" in names:
                # Packages without a mimetype entry (StarOffice 6) name it in the manifest.
                manifest = zip_prefix(z, "META-INF/manifest.xml", 20000).decode("utf-8", "ignore")
                found = re.search(r'manifest:media-type="([^"]+)"\s+manifest:full-path="/"', manifest) or re.search(
                    r'manifest:full-path="/"\s+manifest:media-type="([^"]+)"', manifest)
                if found and found.group(1) in ODF:
                    return Detected(ODF[found.group(1)])
            # The main part decides: a deck or workbook with an embedded Word object also
            # declares wordprocessingml content.
            for part, fmt in (("ppt/presentation.xml", "pptx"), ("xl/workbook.xml", "xlsx"), ("xl/workbook.bin", "xlsb"),
                              ("word/document.xml", "docx")):
                if part in names:
                    return Detected(fmt)
            if any(n.startswith("Contents/section") for n in names):
                return Detected("hwpx")
            if "FixedDocumentSequence.fdseq" in names or any(n.endswith(".fdseq") for n in names):
                return Detected("xps")
            # Apple iWork: '09 XML (index.xml, index.apxl), 2013+ (Index/*.iwa), or a zipped bundle.
            if names & {"index.xml", "index.apxl", "Index/Document.iwa"} or any(n.endswith("/Index.zip") for n in names):
                kind = IWORK.get(Path(name).suffix.lower())
                if kind is None:
                    kind = "key" if "index.apxl" in names else "numbers" if "Index/CalculationEngine.iwa" in names else "pages"
                return Detected(kind)
    except Exception:  # Container and compression backends have no shared data-error base class.
        return Detected(None, CORRUPT)
    return Detected(None)


ENCRYPTED = ("encrypted_file", "The document is password-protected.")


def _biff_encrypted(data: bytes) -> bool:
    """A FILEPASS record (0x002F) among the workbook's first records."""
    i = 0
    for _ in range(20):
        if i + 4 > len(data):
            break
        record, size = int.from_bytes(data[i:i + 2], "little"), int.from_bytes(data[i + 2:i + 4], "little")
        if record == 0x002F:
            return True
        i += 4 + size
    return False


def _ole_format(path: Path) -> Detected:
    import olefile

    try:
        with olefile.OleFileIO(str(path)) as ole:
            # Some writers store stream names in other letter cases ("WORDDOCUMENT").
            streams = {"/".join(s) for s in ole.listdir()}
            folded = {s.lower() for s in streams}
            if "FileHeader" in streams and any(s.startswith("BodyText") for s in streams):
                header = ole.openstream("FileHeader").read(40)
                # FileHeader property bit 1: the document is password-protected.
                if len(header) >= 40 and int.from_bytes(header[36:40], "little") & 0x2:
                    return Detected("hwp", ("encrypted_file", "The HWP file is password-protected."))
                return Detected("hwp")
            actual = {s.lower(): s for s in streams}
            if "encryptedpackage" in folded or "encryptedsummary" in folded:
                return Detected(None, ENCRYPTED)
            if "worddocument" in folded:
                # FIB flags, bit fEncrypted.
                head = ole.openstream(actual["worddocument"]).read(12)
                return Detected(None, ENCRYPTED) if len(head) >= 12 and head[11] & 0x01 else Detected("doc")
            if "workbook" in folded or "book" in folded:
                return Detected(None, ENCRYPTED) if _biff_encrypted(ole.openstream(actual.get("workbook") or actual["book"]).read(4096)) \
                    else Detected("xls")
            if "powerpoint document" in folded:
                return Detected("ppt")
    except Exception:  # olefile can also raise indexing, decoding and record-unpacking errors.
        return Detected(None, CORRUPT)
    return Detected(None)


def _is_bmp(head: bytes) -> bool:
    """"BM" alone is too weak (a text file can start with it): check the reserved bytes and the
    size of the info header that follows."""
    return (head[:2] == b"BM" and len(head) >= 18 and head[6:10] == b"\0\0\0\0"
            and int.from_bytes(head[14:18], "little") in (12, 40, 52, 56, 64, 108, 124))


def decode_guess(data: bytes) -> str | None:
    """Text in whatever encoding it most likely uses (BOMs, UTF-8, CP949, then a statistical
    guess for legacy code pages such as CP1252, CP866 or EBCDIC); None when it is not text."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data[: len(data) // 2 * 2].decode("utf-16")
        except UnicodeDecodeError:
            return None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as exc:
            # A multi-byte character may be cut at the end of a sample.
            if exc.start >= len(data) - 3:
                return data[: exc.start].decode(encoding)
    from charset_normalizer import from_bytes

    best = from_bytes(data).best()
    if best is None or best.percent_chaos > 20:  # a percentage
        return None
    return str(best)


def _looks_like_text(head: bytes) -> bool:
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        # UTF-16 with a byte-order mark: Excel's "Unicode Text" and Notepad's "Unicode".
        try:
            head[: len(head) // 2 * 2].decode("utf-16")
            return True
        except UnicodeDecodeError:
            return False
    if b"\x00" in head:
        return False
    for encoding in ("utf-8", "cp949"):
        try:
            head.decode(encoding)
            return True
        except UnicodeDecodeError:
            # A multi-byte character may be cut at the end of the sample.
            try:
                head[:-3].decode(encoding)
                return True
            except UnicodeDecodeError:
                continue
    return False


def detect(path: Path, filename: str) -> Detected:
    with open(path, "rb") as f:
        head = f.read(4096)
    if b"%PDF-" in head[:1024]:
        return Detected("pdf")
    for magic, fmt in IMAGE_MAGIC.items():
        if head.startswith(magic):
            return Detected(fmt)
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return Detected("webp")
    if _is_bmp(head):
        return Detected("bmp")
    for magic, fmt in OTHER_MAGIC.items():
        if head.startswith(magic):
            # Hangul 3.0: the document info after the 30-byte signature has a password word at 96.
            if fmt == "hwp3" and len(head) >= 128 and int.from_bytes(head[126:128], "little"):
                return Detected("hwp3", ENCRYPTED)
            return Detected(fmt)
    # Excel 2.x-4.0 worksheets are bare BIFF records, not OLE files.
    if head[:2] in (b"\x09\x00", b"\x09\x02", b"\x09\x04") and Path(filename).suffix.lower() == ".xls":
        return Detected("xls")
    if head.startswith(ZIP_MAGIC):
        return _zip_format(path, filename)
    if head.startswith(OLE_MAGIC):
        return _ole_format(path)
    if head.startswith(b"{\\rtf"):
        return Detected("rtf")
    if _looks_like_text(head) or (b"\x00" not in head and decode_guess(head) is not None):
        ext = Path(filename).suffix.lower()
        lowered = head.lstrip()[:400].lower()
        if lowered.startswith(b"<?xml") or lowered.startswith(b"<"):
            for marker, fmt in FLAT_XML:
                if marker in head:
                    return Detected(fmt)
        if ext in TEXT_EXTENSIONS:
            return Detected(TEXT_EXTENSIONS[ext])
        if lowered.startswith((b"<!doctype html", b"<html")):
            return Detected("html")
        return Detected("txt")
    return Detected(None)
