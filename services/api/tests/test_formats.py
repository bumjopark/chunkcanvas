"""Every supported upload format becomes a parsed document, and converting a hostile document
never reads another document's files or makes network requests."""
import http.server
import io
import json
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest

from app import main
from app.config import get_settings
from app.formats import detect
from app.sandbox import child_env, python_command
from conftest import auth, wait_done

SOFFICE = get_settings().soffice_path
needs_soffice = pytest.mark.skipif(SOFFICE is None, reason="LibreOffice not installed")
CANARY = "CANARY-7f3a9-SECRET"


# --- Fixture builders --------------------------------------------------------------------------

def make_docx() -> bytes:
    import docx

    d = docx.Document()
    d.add_heading("문서 청킹 운영 가이드", 0)
    d.add_heading("제1장 목적", 1)
    d.add_paragraph("이 가이드는 모든 내부 문서에 적용됩니다. 문서는 자동으로 분할됩니다. 담당자가 검토합니다.")
    t = d.add_table(rows=2, cols=2)
    t.style = "Table Grid"
    for r, row in enumerate([["유형", "토큰"], ["규정", "400"]]):
        for c, v in enumerate(row):
            t.cell(r, c).text = v
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def make_pptx() -> bytes:
    import pptx

    p = pptx.Presentation()
    s = p.slides.add_slide(p.slide_layouts[1])
    s.shapes.title.text = "청킹 전략"
    s.placeholders[1].text = "구조 기반 분할\n문장 단위 분할"
    buf = io.BytesIO()
    p.save(buf)
    return buf.getvalue()


def make_pptx_table() -> bytes:
    import pptx
    from pptx.util import Inches

    p = pptx.Presentation()
    s = p.slides.add_slide(p.slide_layouts[5])
    s.shapes.title.text = "분할 전략 비교"
    t = s.shapes.add_table(3, 3, Inches(1), Inches(2), Inches(8), Inches(2)).table
    t.cell(0, 0).merge(t.cell(0, 2))
    t.cell(0, 0).text = "전략별 결과"
    for r, row in enumerate([["전략", "방법", "결과"], ["구조", "제목 기준", "좋음"]], start=1):
        for c, v in enumerate(row):
            t.cell(r, c).text = v
    buf = io.BytesIO()
    p.save(buf)
    return buf.getvalue()


def make_xlsx(rows: int = 3) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["문서 유형", "목표 토큰"])
    for i in range(rows):
        ws.append([f"규정 {i}", 400 + i])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


HWPX_NS = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head"'


def make_hwpx() -> bytes:
    header = f"""<?xml version="1.0" encoding="UTF-8"?><hh:head {HWPX_NS}><hh:refList>
      <hh:charProperties><hh:charPr id="0" height="1000"/><hh:charPr id="1" height="1800"><hh:bold/></hh:charPr></hh:charProperties>
      <hh:paraProperties><hh:paraPr id="0"><hh:heading type="NONE" idRef="0" level="0"/></hh:paraPr>
        <hh:paraPr id="1"><hh:heading type="OUTLINE" idRef="0" level="0"/></hh:paraPr></hh:paraProperties>
      <hh:styles><hh:style id="0" type="PARA" name="바탕글" engName="Normal" paraPrIDRef="0" charPrIDRef="0"/></hh:styles>
      </hh:refList></hh:head>"""
    section = f"""<?xml version="1.0" encoding="UTF-8"?><hs:sec {HWPX_NS}>
      <hp:p paraPrIDRef="1" styleIDRef="0"><hp:run charPrIDRef="1"><hp:t>재난 상황 분석</hp:t></hp:run></hp:p>
      <hp:p paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="0"><hp:t>이번 달 재난 발생은 감소했습니다.<hp:lineBreak/>다음 달에도 주의가 필요합니다.</hp:t></hp:run></hp:p>
      <hp:p paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="0"><hp:tbl><hp:tr>
        <hp:tc><hp:cellSpan colSpan="2" rowSpan="1"/><hp:subList><hp:p><hp:run charPrIDRef="0"><hp:t>구분 합계</hp:t></hp:run></hp:p></hp:subList></hp:tc>
      </hp:tr><hp:tr>
        <hp:tc><hp:subList><hp:p><hp:run charPrIDRef="0"><hp:t>화재</hp:t></hp:run></hp:p></hp:subList></hp:tc>
        <hp:tc><hp:subList><hp:p><hp:run charPrIDRef="0"><hp:t>12건</hp:t></hp:run></hp:p></hp:subList></hp:tc>
      </hp:tr></hp:tbl></hp:run></hp:p></hs:sec>"""
    hpf = """<?xml version="1.0" encoding="UTF-8"?><opf:package xmlns:opf="http://www.idpf.org/2007/opf/"><opf:manifest>
      <opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>
      <opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/></opf:manifest>
      <opf:spine><opf:itemref idref="header"/><opf:itemref idref="section0"/></opf:spine></opf:package>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/hwp+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("Contents/header.xml", header)
        z.writestr("Contents/section0.xml", section)
        z.writestr("Contents/content.hpf", hpf)
        z.writestr("Preview/PrvText.txt", "재난 상황 분석".encode("utf-16"))
    return buf.getvalue()


def pptx_with_embedded_docx() -> bytes:
    src = zipfile.ZipFile(io.BytesIO(make_pptx()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "[Content_Types].xml":
                extra = '<Default Extension="docx" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document"/>'
                data = data.replace(b"<Default ", extra.encode() + b"<Default ", 1)
            z.writestr(info, data)
        z.writestr("ppt/embeddings/Microsoft_Word_Document.docx", make_docx())
    return out.getvalue()


def make_png() -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    im = Image.new("RGB", (1400, 500), "white")
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 56)
    except OSError:
        font = ImageFont.load_default()
    ImageDraw.Draw(im).text((60, 180), "Scanned invoice total 1234", fill="black", font=font)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def upload_file(client, data: bytes, name: str, parser: str = "pymupdf"):
    return client.post(f"/api/documents?parser={parser}", content=data,
                       headers={"Content-Type": "application/octet-stream", "X-Filename": quote(name)})


def parsed(client, body):
    return client.get(f"/api/documents/{body['id']}", headers=auth(body["accessToken"])).json()


# --- Detection -----------------------------------------------------------------------------------

@pytest.mark.parametrize("name,data,expected", [
    ("a.docx", make_docx(), "docx"), ("a.pptx", make_pptx(), "pptx"), ("a.xlsx", make_xlsx(), "xlsx"),
    ("a.hwpx", make_hwpx(), "hwpx"), ("a.png", make_png(), "png"), ("notes.md", "# 제목\n본문".encode(), "md"),
    ("page.html", b"<!doctype html><p>x</p>", "html"), ("data.csv", b"a,b\n1,2\n", "csv"),
    ("legacy.txt", "한국어 텍스트".encode("cp949"), "txt"), ("x.rtf", b"{\\rtf1 hi}", "rtf"),
    # The name does not decide the format: a DOCX called .pdf is still a DOCX.
    ("misnamed.pdf", make_docx(), "docx"),
    # A deck with an embedded Word object is still a deck.
    ("deck.pptx", None, "pptx"),
    ("unicode.txt", "유니코드 텍스트".encode("utf-16"), "txt"),
    ("unicode.csv", "이름\t값\n가\t1\n".encode("utf-16"), "csv"),
    # Hangul 3.0, WordPerfect and single-file XML documents LibreOffice opens.
    ("old.hwp", b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05" + bytes(128), "hwp3"),
    ("letter.wpd", b"\xffWPC\x10\x00\x00\x00\x01\x0a" + bytes(64), "wpd"),
    ("flat.fodt", b'<?xml version="1.0"?><office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                  b'office:mimetype="application/vnd.oasis.opendocument.text"><office:body/></office:document>', "fodt"),
    ("word2003.xml", b'<?xml version="1.0"?><?mso-application progid="Word.Document"?><w:wordDocument xmlns:w="x"/>', "wordml"),
    ("sheet2003.xml", b'<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"/>', "xmlss"),
    # Legacy code pages are still text; "BM" at the start of a text file does not make it a bitmap.
    ("cafe.txt", "Café crème brûlée, déjà vu. Naïve façade, señor.\n".encode("cp1252") * 20, "txt"),
    ("note.txt", b"BMW and Mercedes are car makers.\n", "txt"),
    ("page.xhtml", b'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><body><p>x</p></body></html>', "html"),
])
def test_formats_are_detected_by_content(tmp_path, name, data, expected):
    path = tmp_path / "f"
    path.write_bytes(data if data is not None else pptx_with_embedded_docx())
    assert detect(path, name).format == expected


def test_zip_bombs_and_unknown_binaries_are_rejected(tmp_path):
    bomb = tmp_path / "bomb.docx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        with z.open("word/document.xml", "w", force_zip64=True) as f:
            chunk = b"\0" * (1 << 20)
            for _ in range(210):
                f.write(chunk)
    assert detect(bomb, "bomb.docx").error[0] == "too_large"
    junk = tmp_path / "junk"
    junk.write_bytes(bytes(range(256)) * 20)
    assert detect(junk, "junk.bin").format is None


@pytest.mark.parametrize("kind,extension,expected", [
    ("text-template", ".ott", "odt"),
    ("presentation-template", ".otp", "odp"),
    ("spreadsheet-template", ".ots", "ods"),
])
def test_advertised_opendocument_templates_are_recognized(tmp_path, kind, extension, expected):
    path = tmp_path / ("template" + extension)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument." + kind)
        z.writestr("content.xml", "<document/>")
    assert detect(path, path.name).format == expected


# --- End to end through the API -------------------------------------------------------------------

@needs_soffice
@pytest.mark.parametrize("name,maker,expect_text", [
    ("가이드.docx", make_docx, "청킹 운영 가이드"),
    ("slides.pptx", make_pptx, "청킹 전략"),
    ("table.xlsx", make_xlsx, "목표 토큰"),
    ("재난.hwpx", make_hwpx, "재난 상황 분석"),
    ("notes.md", lambda: "# 마크다운 제목\n\n첫 문단입니다. 두 번째 문장.\n\n- 항목\n".encode(), "마크다운 제목"),
    ("page.html", lambda: "<!doctype html><h1>HTML 제목</h1><p>본문입니다.</p>".encode(), "HTML 제목"),
    ("legacy.txt", lambda: "옛날 텍스트 파일입니다.\n\n둘째 문단.".encode("cp949"), "옛날 텍스트"),
    ("data.csv", lambda: "이름,값\n가,1\n나,2\n".encode(), "이름"),
    # Very short text must not be mistaken for a scan and sent to OCR.
    ("tiny.txt", lambda: "짧은 메모".encode(), "짧은 메모"),
])
def test_office_hangul_and_markup_uploads_are_converted_and_parsed(client, name, maker, expect_text):
    r = upload_file(client, maker(), name)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["sourceFormat"] == name.rsplit(".", 1)[1] and body["filename"] == name
    st = wait_done(client, body["id"], body["accessToken"], timeout=240)
    assert st["status"] == "completed", st
    doc = parsed(client, body)
    text = " ".join(b["text"] for b in doc["parsed"]["blocks"])
    assert expect_text in text
    assert doc["parsed"]["document"]["sourceFormat"] == body["sourceFormat"]
    assert doc["parser"] == "pymupdf" and "auto_ocr" not in doc["notices"]
    assert doc["pageCount"] >= 1
    pdf = client.get(f"/api/documents/{body['id']}/file", headers=auth(body["accessToken"])).content
    assert pdf.startswith(b"%PDF-")


def test_images_are_read_with_ocr(client):
    if not main.S().ocr_available():
        pytest.skip("OCR models not installed")
    body = upload_file(client, make_png(), "scan.png").json()
    st = wait_done(client, body["id"], body["accessToken"], timeout=300)
    assert st["status"] == "completed", st
    assert st["parser"] == "docling-ocr" and "auto_ocr" in st["notices"]
    assert "invoice" in " ".join(b["text"] for b in parsed(client, body)["parsed"]["blocks"]).lower()


def test_unsupported_files_get_a_clear_error(client):
    r = upload_file(client, bytes(range(256)) * 10, "thing.bin")
    assert r.status_code == 415 and "Supported:" in r.json()["detail"]["message"]


@needs_soffice
def test_long_spreadsheets_stop_at_the_page_limit(client):
    settings = main.S().settings
    settings.truncate_long_pdfs = True
    try:
        body = upload_file(client, make_xlsx(rows=5000), "big.xlsx").json()
        st = wait_done(client, body["id"], body["accessToken"], timeout=300)
        assert st["status"] == "completed", st
        assert st["pageCount"] == settings.max_pages and "page_limit" in st["notices"]
    finally:
        settings.truncate_long_pdfs = False


# --- Hostile documents ----------------------------------------------------------------------------

@pytest.fixture()
def listener():
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], hits
    server.shutdown()


def hostile_documents(canary: Path, port: int) -> dict[str, tuple[str, bytes]]:
    url = f"http://127.0.0.1:{port}"
    html_doc = f"""<!doctype html><html><head><link rel="stylesheet" href="file://{canary}">
      <style>body {{ background: url({url}/css.png) }}</style></head><body>
      <h1>Visible title</h1><img src="file://{canary}" alt="local"><img src="{url}/img.png">
      <iframe src="file://{canary}"></iframe><object data="file://{canary}"></object>
      <p style="background:url({url}/inline.png)">Body text.</p></body></html>"""
    md_doc = f"# Title\n\n![x]({url}/md.png)\n\n<img src=\"file://{canary}\">\n\n<iframe src=\"{url}/frame\"></iframe>\n"
    import docx

    d = docx.Document()
    d.add_paragraph("Visible paragraph.")
    buf = io.BytesIO()
    d.save(buf)
    # Add an INCLUDETEXT field pointing at the canary and an external relationship to the listener.
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "word/document.xml":
                field = (f'<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> INCLUDETEXT "{canary}" </w:instrText></w:r>'
                         '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>placeholder</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
                data = data.replace(b"<w:sectPr", field.encode() + b"<w:sectPr", 1)
            if info.filename == "word/_rels/document.xml.rels":
                rel = f'<Relationship Id="rIdX1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="{url}/linked.png" TargetMode="External"/>'
                data = data.replace(b"</Relationships>", rel.encode() + b"</Relationships>")
            z.writestr(info, data)
    return {"html": ("page.html", html_doc.encode()), "md": ("notes.md", md_doc.encode()), "docx": ("x.docx", out.getvalue())}


@needs_soffice
@pytest.mark.parametrize("sandbox", ["none", "bwrap", "landlock"])
@pytest.mark.parametrize("fmt", ["html", "md", "docx"])
def test_conversion_cannot_read_other_files_or_reach_the_network(tmp_path, listener, sandbox, fmt):
    if sandbox == "bwrap" and not Path("/usr/bin/bwrap").exists():
        pytest.skip("bubblewrap not available")
    port, hits = listener
    # Another visitor's document, outside the converting document's directory.
    other = tmp_path / "other-document"
    other.mkdir()
    canary = other / "secret.txt"
    canary.write_text(CANARY)
    name, data = hostile_documents(canary, port)[fmt]
    doc_dir = tmp_path / "this-document"
    doc_dir.mkdir()
    (doc_dir / f"original{Path(name).suffix}").write_bytes(data)

    settings = get_settings().model_copy(update={"parser_sandbox": sandbox})
    cmd = python_command(settings, "app.convert", ["--doc-dir", str(doc_dir), "--format", fmt, "--soffice", str(SOFFICE)], writable=doc_dir)
    if sandbox == "landlock":
        # Run with a read-only root and non-writable /tmp for Landlock. The separate
        # isolation test exercises Landlock/seccomp alone without this mount wrapper.
        cmd = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--bind", str(doc_dir), str(doc_dir), "--", *cmd]
    proc = subprocess.run(cmd, env=child_env(settings), cwd=str(Path(main.__file__).parents[1]), capture_output=True, timeout=240)
    assert proc.returncode == 0, proc.stderr.decode()[-500:]
    import pymupdf

    text = "".join(page.get_text() for page in pymupdf.open(doc_dir / "source.pdf"))
    assert CANARY not in text
    assert hits == []
    shutil.rmtree(doc_dir)


# --- Every advertised format actually converts ----------------------------------------------------

def run_convert(tmp_path: Path, data: bytes, suffix: str, fmt: str) -> Path:
    doc_dir = tmp_path / fmt
    doc_dir.mkdir()
    (doc_dir / f"original{suffix}").write_bytes(data)
    settings = get_settings()
    args = ["--doc-dir", str(doc_dir), "--format", fmt] + (["--soffice", str(SOFFICE)] if SOFFICE else [])
    proc = subprocess.run(python_command(settings, "app.convert", args, writable=doc_dir), env=child_env(settings),
                          cwd=str(Path(main.__file__).parents[1]), capture_output=True, timeout=240)
    assert proc.returncode == 0, (proc.stderr.decode()[-400:], (doc_dir / "error.json").read_text() if (doc_dir / "error.json").exists() else "")
    return doc_dir / "source.pdf"


def pdf_text(path: Path) -> str:
    import pymupdf

    return " ".join(" ".join(page.get_text() for page in pymupdf.open(path)).split())


@needs_soffice
@pytest.mark.parametrize("fmt,body", [
    ("fodt", '<office:text><text:p>FLATXML CONTENT</text:p></office:text>'),
    ("fods", '<office:spreadsheet><table:table table:name="Sheet1"><table:table-row>'
             '<table:table-cell office:value-type="string"><text:p>FLATXML CONTENT</text:p>'
             '</table:table-cell></table:table-row></table:table></office:spreadsheet>'),
    ("fodp", '<office:presentation><draw:page draw:name="Slide1"><draw:frame svg:x="1cm" '
             'svg:y="1cm" svg:width="10cm" svg:height="3cm"><draw:text-box><text:p>FLATXML CONTENT'
             '</text:p></draw:text-box></draw:frame></draw:page></office:presentation>'),
    ("fodg", '<office:drawing><draw:page draw:name="Page1"><draw:frame svg:x="1cm" '
             'svg:y="1cm" svg:width="10cm" svg:height="3cm"><draw:text-box><text:p>FLATXML CONTENT'
             '</text:p></draw:text-box></draw:frame></draw:page></office:drawing>'),
    ("wordml", '<w:wordDocument xmlns:w="http://schemas.microsoft.com/office/word/2003/wordml">'
               '<w:body><w:p><w:r><w:t>FLATXML CONTENT</w:t></w:r></w:p></w:body></w:wordDocument>'),
    ("xmlss", '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
              'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="Sheet1">'
              '<Table><Row><Cell><Data ss:Type="String">FLATXML CONTENT</Data></Cell></Row></Table>'
              '</Worksheet></Workbook>'),
])
def test_flat_xml_renders_document_content_instead_of_xml_source(tmp_path, fmt, body):
    if fmt.startswith("fod"):
        mime = {"fodt": "text", "fods": "spreadsheet", "fodp": "presentation", "fodg": "graphics"}[fmt]
        namespaces = {"office": "office", "text": "text", "table": "table", "draw": "drawing",
                      "svg": "svg-compatible"}
        attrs = " ".join(f'xmlns:{key}="urn:oasis:names:tc:opendocument:xmlns:{value}:1.0"'
                         for key, value in namespaces.items())
        body = (f'<office:document {attrs} office:version="1.2" '
                f'office:mimetype="application/vnd.oasis.opendocument.{mime}">'
                f'<office:body>{body}</office:body></office:document>')
    data = ('<?xml version="1.0" encoding="UTF-8"?>\n' + body).encode()
    suffix = f".{fmt}" if fmt.startswith("fod") else ".xml"
    text = pdf_text(run_convert(tmp_path, data, suffix, fmt))
    assert "FLATXML CONTENT" in text
    assert "xmlns" not in text and "<office:" not in text and "<w:" not in text
    assert "range-" not in text  # Calc must not interpret XML as generic tabular data.


@needs_soffice
@pytest.mark.parametrize("target,source,maker,expect", [
    ("doc", "docx", make_docx, "청킹 운영 가이드"), ("rtf", "docx", make_docx, "청킹 운영 가이드"),
    ("odt", "docx", make_docx, "청킹 운영 가이드"), ("ppt", "pptx", make_pptx, "청킹 전략"),
    ("odp", "pptx", make_pptx, "청킹 전략"), ("xls", "xlsx", make_xlsx, "목표 토큰"), ("ods", "xlsx", make_xlsx, "목표 토큰"),
])
def test_legacy_and_open_document_formats_convert(tmp_path, target, source, maker, expect):
    made = tmp_path / "made"
    made.mkdir()
    (made / f"in.{source}").write_bytes(maker())
    subprocess.run([str(SOFFICE), "--headless", "--norestore", f"-env:UserInstallation={(tmp_path / 'p').as_uri()}",
                    "--convert-to", target, "--outdir", str(made), str(made / f"in.{source}")], capture_output=True, timeout=120)
    data = (made / f"in.{target}").read_bytes()
    found = tmp_path / "found"
    found.write_bytes(data)
    assert detect(found, f"x.{target}").format == target
    pdf = run_convert(tmp_path, data, f".{target}", target)
    assert expect in pdf_text(pdf)
    # Their tables are read from the original too (the slides made here have none, and the .doc
    # LibreOffice writes from this .docx keeps its table as a plain line).
    assert (pdf.parent / "source-tables.json").exists() == (target not in ("ppt", "odp", "doc"))


@needs_soffice
@pytest.mark.parametrize("fmt,maker,expect", [
    ("docx", make_docx, [["유형", "토큰"], ["규정", "400"]]),
    ("xlsx", make_xlsx, [["문서 유형", "목표 토큰"], ["규정 0", "400"], ["규정 1", "401"], ["규정 2", "402"]]),
    ("pptx", make_pptx_table, [["전략별 결과", "", ""], ["전략", "방법", "결과"], ["구조", "제목 기준", "좋음"]]),
])
def test_tables_of_office_documents_are_kept_from_the_original(tmp_path, fmt, maker, expect):
    import json

    pdf = run_convert(tmp_path, maker(), f".{fmt}", fmt)
    assert expect in json.loads((pdf.parent / "source-tables.json").read_text(encoding="utf-8"))


def image_bytes(kind: str) -> bytes:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (600, 300), "white")
    ImageDraw.Draw(im).rectangle((50, 50, 550, 250), outline="black", width=5)
    buf = io.BytesIO()
    if kind == "jpeg":
        im.convert("CMYK").save(buf, "JPEG")
    elif kind == "tiff":
        im.save(buf, "TIFF", save_all=True, append_images=[im.rotate(90, expand=True)])
    elif kind == "png":
        im.convert("RGBA").save(buf, "PNG")
    else:
        im.save(buf, kind.upper())
    return buf.getvalue()


@pytest.mark.parametrize("kind,pages", [("jpeg", 1), ("tiff", 2), ("bmp", 1), ("webp", 1), ("png", 1), ("gif", 1)])
def test_every_image_kind_becomes_pages(tmp_path, kind, pages):
    import pymupdf

    data = image_bytes(kind)
    found = tmp_path / "found"
    found.write_bytes(data)
    assert detect(found, f"x.{kind}").format == kind
    pdf = pymupdf.open(run_convert(tmp_path, data, f".{kind}", kind))
    assert pdf.page_count == pages
    if pages > 1:  # each frame, not the last one repeated (the second TIFF frame is rotated)
        assert pdf[0].rect != pdf[1].rect


@needs_soffice
def test_utf16_text_converts(tmp_path):
    assert "유니코드 문서입니다" in pdf_text(run_convert(tmp_path, "유니코드 문서입니다.".encode("utf-16"), ".txt", "txt"))


@needs_soffice
def test_docx_with_a_linked_image_still_converts(tmp_path, listener):
    import docx

    port, hits = listener
    png = io.BytesIO()
    from PIL import Image

    Image.new("RGB", (40, 40), "red").save(png, "PNG")
    d = docx.Document()
    d.add_paragraph("Before the picture.")
    d.add_picture(io.BytesIO(png.getvalue()))
    d.add_paragraph("After the picture.")
    buf = io.BytesIO()
    d.save(buf)
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "word/document.xml":
                data = data.replace(b'r:embed="', b'r:link="rIdLinked" r:embed="', 1)
            if info.filename == "word/_rels/document.xml.rels":
                rel = f'<Relationship Id="rIdLinked" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="http://127.0.0.1:{port}/linked.png" TargetMode="External"/>'
                data = data.replace(b"</Relationships>", rel.encode() + b"</Relationships>")
            z.writestr(info, data)
    text = pdf_text(run_convert(tmp_path, out.getvalue(), ".docx", "docx"))
    assert "Before the picture." in text and "After the picture." in text
    assert hits == []


@needs_soffice
def test_preparation_resumes_after_a_restart(client):
    body = upload_file(client, "# 재시작\n\n본문입니다.".encode(), "resume.md").json()
    assert wait_done(client, body["id"], body["accessToken"], timeout=240)["status"] == "completed"
    s = main.S()
    doc_dir = s.storage.doc_dir(body["id"])
    # As if the process stopped after conversion but before inspection was recorded.
    (doc_dir / "prepared.json").unlink()
    (doc_dir / "parsed.json").unlink()
    s.db.update(body["id"], status="queued", page_count=0)
    s.runner.wake()
    st = wait_done(client, body["id"], body["accessToken"], timeout=240)
    assert st["status"] == "completed" and st["pageCount"] == 1
    assert (doc_dir / "prepared.json").exists()


def hwpx_with_wide_picture_in_a_cell() -> bytes:
    """A table whose cell holds a picture far wider in pixels than the page, then more text, then
    two pictures on their own: one whose curSz is 0 x 0 before its real sz, one with no size."""
    from PIL import Image

    png = io.BytesIO()
    Image.new("RGB", (2400, 600), "lightgray").save(png, "PNG")
    src = zipfile.ZipFile(io.BytesIO(make_hwpx()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "Contents/section0.xml":
                cell = ('<hp:tc><hp:cellSz width="48000" height="1000"/><hp:subList><hp:p><hp:run charPrIDRef="0">'
                        '<hp:t>그림 앞 문장입니다.</hp:t><hp:pic><hp:sz width="36000" height="9000"/>'
                        '<hp:img binaryItemIDRef="image1"/></hp:pic><hp:t>' + '그림 뒤 오른쪽 끝까지 이어지는 아주 긴 설명 문장입니다. ' * 3 +
                        '</hp:t></hp:run></hp:p></hp:subList></hp:tc>')
                alone = ('<hp:p><hp:run><hp:pic><hp:orgSz width="47464" height="23850"/><hp:curSz width="0" height="0"/>'
                         '<hp:sz width="47464" height="23850"/><hp:img binaryItemIDRef="image1"/></hp:pic></hp:run></hp:p>'
                         '<hp:p><hp:run><hp:pic><hp:img binaryItemIDRef="image1"/></hp:pic></hp:run></hp:p>')
                data = data.replace(b"</hs:sec>", f'<hp:p><hp:run><hp:tbl><hp:sz width="48000" height="9000"/><hp:tr>{cell}</hp:tr></hp:tbl></hp:run></hp:p>{alone}</hs:sec>'.encode())
            if info.filename == "Contents/content.hpf":
                data = data.replace(b"</opf:manifest>", b'<opf:item id="image1" href="BinData/image1.png" media-type="image/png"/></opf:manifest>')
            z.writestr(info, data)
        z.writestr("BinData/image1.png", png.getvalue())
    return out.getvalue()


def test_hwpx_repeated_pictures_embed_the_member_bytes_without_libreoffice(tmp_path):
    import base64
    from lxml import html
    from app.convert import page_html
    from app.hwpx import hwpx_to_html

    src = tmp_path / "pictures.hwpx"
    src.write_bytes(hwpx_with_wide_picture_in_a_cell())
    with zipfile.ZipFile(src) as archive:
        expected = archive.read("BinData/image1.png")
    page = hwpx_to_html(src, tmp_path, page_html)
    pictures = html.parse(str(page)).xpath("//img")
    assert len(pictures) == 3
    for picture in pictures:
        media, encoded = picture.get("src").split(",", 1)
        assert media == "data:image/png;base64"
        assert base64.b64decode(encoded, validate=True) == expected
        assert int(picture.get("width")) > 0 and int(picture.get("height")) > 0


@needs_soffice
def test_hwpx_pictures_in_table_cells_stay_inside_the_page(tmp_path):
    import pymupdf

    pdf = pymupdf.open(run_convert(tmp_path, hwpx_with_wide_picture_in_a_cell(), ".hwpx", "hwpx"))
    text = " ".join(" ".join(page.get_text().split()) for page in pdf)
    assert "그림 앞 문장입니다." in text and "아주 긴 설명 문장입니다." in text
    assert sum(len(page.get_image_info()) for page in pdf) == 3
    for page in pdf:
        assert all(block[2] <= page.rect.width for block in page.get_text("blocks"))
        assert all(info["bbox"][2] <= page.rect.width for info in page.get_image_info())


@needs_soffice
def test_hangul_picture_budget_preserves_text_and_reports_omissions(tmp_path, monkeypatch):
    from app import hangul_pictures
    from app.convert import convert
    import pymupdf

    (tmp_path / "original.hwpx").write_bytes(hwpx_with_wide_picture_in_a_cell())
    monkeypatch.setattr(hangul_pictures, "file_limit", lambda: 400)
    notices = convert(tmp_path, "hwpx", str(SOFFICE), 20, 40)
    assert notices == ["pictures_omitted"]
    with pymupdf.open(tmp_path / "source.pdf") as pdf:
        assert not any(page.get_image_info() for page in pdf)
        text = "".join(page.get_text() for page in pdf)
        assert "그림 앞 문장입니다." in text and "아주 긴 설명 문장입니다." in text


@needs_soffice
def test_shared_package_skips_only_originals_that_do_not_fit(tmp_path, monkeypatch):
    import random
    from lxml import etree
    from PIL import Image
    from app import hangul_pictures
    from app.convert import page_html, run_soffice

    large = Image.frombytes("RGB", (300, 300), random.Random(7).randbytes(300 * 300 * 3))
    pictures = hangul_pictures.EmbeddedPictures(tmp_path)
    originals = []
    for key, image in enumerate([large, Image.new("RGB", (8, 8), "red")]):
        stream = io.BytesIO()
        image.save(stream, "PNG")
        originals.append(stream.getvalue())
        pictures.get(str(key), lambda: io.BytesIO(originals[-1]))
    monkeypatch.setattr(hangul_pictures, "file_limit", lambda: 128 * 1024)
    body = '<p>Before</p><p><img src="cc-picture-0"/></p><p>Between</p><p><img src="cc-picture-1"/></p><p>After</p>'
    page = pictures.write_page(page_html(body))
    layout = run_soffice(str(SOFFICE), page, tmp_path, "writerweb8_writer", None, 30, target="odt")
    package = pictures.fill_package(layout)
    assert pictures.omitted and package.stat().st_size <= hangul_pictures.file_limit()
    with zipfile.ZipFile(package) as archive:
        member, = [key for key in archive.namelist() if key.startswith("Pictures/")]
        assert archive.read(member) == originals[1]
        content = etree.fromstring(archive.read("content.xml"))
        assert content.xpath('//*[local-name()="image"]/@*[local-name()="href"]') == [member]
        assert all(text in "".join(content.itertext()) for text in ("Before", "Between", "After"))


def test_wide_csv_becomes_one_record_per_row():
    from app.convert import breakable, csv_body

    head = ",".join(f"col{i}" for i in range(14))
    body, notices = csv_body(head + "\n" + ",".join(str(i) for i in range(14)) + "\n")
    assert notices == ["csv_header_inferred", "csv_records"]
    assert body.startswith("<p><b>col0</b>: 0 · <b>col1</b>: 1")
    assert body.count("<p>") == 1 and "<b>col13</b>: 13" in body
    narrow, notices = csv_body("a,b\n1,2\n")
    # A data table is drawn with borders, so the layout parser sees a table.
    assert notices == ["csv_header_inferred"] and narrow.startswith('<table border="1"')
    assert "<thead>" in narrow and "<th>a</th><th>b</th>" in narrow
    # Long unbroken runs get invisible break opportunities so table columns cannot push past the margin.
    assert breakable("<td>서울특별시강남구테헤란로123번길</td>") == "<td>서울특별시강​남구테헤란로​123번길</td>"
    assert breakable('<p class="x">a &amp; b &#x1F600;</p>') == '<p class="x">a &amp; b &#x1F600;</p>'


def test_csv_preserves_headerless_first_record_and_ragged_wide_tail():
    from app.convert import csv_body
    from lxml import html

    data = "first,record\n" + ",".join(f"value{i}" for i in range(15)) + "\n"
    body, notices = csv_body(data)
    assert notices == ["csv_header_unverified", "csv_records"]
    rows = html.fromstring("<div>" + body + "</div>").findall("p")
    assert len(rows) == 2
    assert rows[0].text_content() == "Column 1: first · Column 2: record"
    assert "Column 15: value14" in rows[1].text_content()


def test_scanned_pages_inside_a_digital_pdf_are_read_with_ocr(client):
    import pymupdf

    if not main.S().ocr_available():
        pytest.skip("OCR models not installed")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Digital page with a real text layer about chunking strategies.", fontsize=12)
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(0, 0, page.rect.width, page.rect.width * 500 / 1400), stream=make_png())
    page.insert_image(pymupdf.Rect(0, 300, page.rect.width, 300 + page.rect.width * 500 / 1400), stream=make_png())
    body = upload_file(client, doc.tobytes(), "mixed.pdf", parser="hybrid").json()
    assert body["parser"] == "hybrid" and body["notices"] == ["mixed_ocr"]
    st = wait_done(client, body["id"], body["accessToken"], timeout=600)
    assert st["status"] == "completed", st
    blocks = parsed(client, body)["parsed"]["blocks"]
    digital = [b for b in blocks if "chunking strategies" in b["text"]]
    assert digital and digital[0]["lines"]  # the text layer keeps character positions
    assert "invoice" in " ".join(b["text"] for b in blocks if b["regions"] and b["regions"][0]["pageIndex"] == 1).lower()



def zip_bytes(entries: dict[str, bytes], **kw) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data, **kw)
    return buf.getvalue()


@pytest.mark.parametrize("name,entries,expected", [
    ("book.epub", {"mimetype": b"application/epub+zip", "META-INF/container.xml": b"<container/>"}, "epub"),
    ("old.sxw", {"content.xml": b"<x/>", "META-INF/manifest.xml":
                 b'<manifest:manifest><manifest:file-entry manifest:media-type="application/vnd.sun.xml.writer" manifest:full-path="/"/></manifest:manifest>'}, "sxw"),
    ("drawing.odg", {"mimetype": b"application/vnd.oasis.opendocument.graphics", "content.xml": b"<x/>"}, "odg"),
    ("binary.xlsb", {"[Content_Types].xml": b"<Types/>", "xl/workbook.bin": b"\x00"}, "xlsb"),
    ("doc.xps", {"FixedDocumentSequence.fdseq": b"<x/>", "Documents/1/FixedDocument.fdoc": b"<x/>"}, "xps"),
    ("report.pages", {"index.xml": b"<sl:document/>", "buildVersionHistory.plist": b""}, "pages"),
    ("deck.key", {"index.apxl": b"<key:presentation/>"}, "key"),
    ("table.numbers", {"Index/Document.iwa": b"", "Index/CalculationEngine.iwa": b""}, "numbers"),
])
def test_more_packages_are_detected(tmp_path, name, entries, expected):
    path = tmp_path / name
    path.write_bytes(zip_bytes(entries))
    assert detect(path, name).format == expected


def test_password_protected_packages_and_workbooks_are_reported(tmp_path):
    from app.formats import _biff_encrypted

    odt = tmp_path / "secret.odt"
    odt.write_bytes(zip_bytes({"mimetype": b"application/vnd.oasis.opendocument.text",
                               "META-INF/manifest.xml": b"<manifest:file-entry><manifest:encryption-data/></manifest:file-entry>"}))
    assert detect(odt, "secret.odt").error[0] == "encrypted_file"
    # BOF, then FILEPASS: an encrypted workbook stream; BOF then WINDOW1: a plain one.
    assert _biff_encrypted(b"\x09\x08\x02\x00\x00\x06" + b"\x2f\x00\x02\x00\x00\x00")
    assert not _biff_encrypted(b"\x09\x08\x02\x00\x00\x06" + b"\x3d\x00\x02\x00\x00\x00")
    hwp3 = tmp_path / "old.hwp"
    hwp3.write_bytes(b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05" + bytes(96) + b"\x02\x00" + bytes(30))
    assert detect(hwp3, "old.hwp").error[0] == "encrypted_file"


def test_macros_are_removed_and_the_package_still_opens(tmp_path):
    from app.convert import sanitize_package

    src = zipfile.ZipFile(io.BytesIO(make_docx()))
    entries = {i.filename: src.read(i) for i in src.infolist()}
    entries["word/vbaProject.bin"] = b"\xd0\xcf\x11\xe0 macro code"
    entries["word/_rels/vbaProject.bin.rels"] = b"<Relationships/>"
    entries["[Content_Types].xml"] = entries["[Content_Types].xml"].replace(
        b"</Types>", b'<Override PartName="/word/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/></Types>')
    entries["word/_rels/document.xml.rels"] = entries["word/_rels/document.xml.rels"].replace(
        b"</Relationships>", b'<Relationship Id="rIdVba" Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" Target="vbaProject.bin"/></Relationships>')
    (tmp_path / "in.docm").write_bytes(zip_bytes(entries))
    sanitize_package(tmp_path / "in.docm", tmp_path / "out.docm")
    out = zipfile.ZipFile(tmp_path / "out.docm")
    assert not [n for n in out.namelist() if "vbaProject" in n]
    assert b"vbaProject" not in out.read("[Content_Types].xml") and b"vbaProject" not in out.read("word/_rels/document.xml.rels")
    if SOFFICE:
        assert "청킹 운영 가이드" in pdf_text(run_convert(tmp_path, (tmp_path / "out.docm").read_bytes(), ".docx", "docx"))


@needs_soffice
def test_epub_chapters_are_read_in_spine_order(tmp_path):
    opf = (b'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf"><manifest>'
           b'<item id="c2" href="text/two.xhtml" media-type="application/xhtml+xml"/>'
           b'<item id="c1" href="text/one.xhtml" media-type="application/xhtml+xml"/></manifest>'
           b'<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>')
    chapter = lambda title, text: (f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'  # noqa: E731
                                  f'<h1>{title}</h1><p>{text}</p><script>alert(1)</script></body></html>').encode()
    data = zip_bytes({"mimetype": b"application/epub+zip",
                      "META-INF/container.xml": b'<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
                      "OEBPS/content.opf": opf, "OEBPS/text/one.xhtml": chapter("첫째 장", "처음 이야기입니다."),
                      "OEBPS/text/two.xhtml": chapter("둘째 장", "다음 이야기입니다.")})
    text = pdf_text(run_convert(tmp_path, data, ".epub", "epub"))
    assert text.index("첫째 장") < text.index("둘째 장") and "alert" not in text


def test_iwork_packages_use_their_own_preview(tmp_path):
    import pymupdf

    preview = pymupdf.open()
    preview.new_page().insert_text((72, 72), "Rendered by Pages itself")
    data = zip_bytes({"index.xml": b"<sl:document/>", "QuickLook/Preview.pdf": preview.tobytes()})
    assert "Rendered by Pages itself" in pdf_text(run_convert(tmp_path, data, ".pages", "pages"))


def test_a_word_the_line_wrap_cut_is_joined_from_the_source_words():
    from app.models import ParsedDocument
    from app.parsers.source_words import rejoin

    text = "ISO3166-1-nu meric: 4 · Dial: 93"
    line = {"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.12}, "start": 13, "end": len(text),
            "xs": [0.1] * (len(text) - 13)}
    first = {"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.08, "x1": 0.9, "y1": 0.1}, "start": 0, "end": 12, "xs": [0.1] * 12}
    parsed = ParsedDocument.model_validate({
        "document": {"id": "d", "filename": "a.csv", "fileHash": "0" * 64, "pageCount": 1, "parser": "t", "parserVersion": "1"},
        "pages": [{"index": 0, "width": 595, "height": 842}],
        "blocks": [{"id": "b0000", "readingOrder": 0, "type": "paragraph", "text": text, "regions": [
            {"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.08, "x1": 0.9, "y1": 0.12}}], "source": {"parser": "t"},
            "lines": [first, line]}],
        "blocksFingerprint": "x"})
    words = {"ISO3166-1-numeric:", "4", "·", "Dial:", "93"}
    assert rejoin(parsed, words) == 1
    block = parsed.blocks[0]
    assert block.text == "ISO3166-1-numeric: 4 · Dial: 93"
    assert block.lines[1].start == 12 and block.lines[1].end == len(block.text)


def test_source_words_end_at_cells_and_paragraphs(tmp_path):
    from app.parsers.source_words import page_words

    page = tmp_path / "document.html"
    page.write_text("<html><head><meta charset=\"utf-8\"></head><body><table><tr><td>tan</td><td>cot</td></tr></table><p>생략합니다.</p><p>x atop y</p>"
                    "<p><span>ISO3166-1-</span><b>numeric:</b> 4<br>Dial</p></body></html>", encoding="utf-8")
    words = set(page_words(page))
    assert {"tan", "cot", "생략합니다.", "x", "ISO3166-1-numeric:", "4", "Dial"} <= words
    assert not words & {"tancot", "생략합니다.x", "4Dial"}


def test_a_photo_without_text_is_one_picture_for_the_vision_model(tmp_path):
    import sys

    from PIL import Image

    settings = get_settings()
    if not (settings.models_dir / "RapidOcr").exists():
        pytest.skip("OCR models not installed")
    Image.new("RGB", (120, 90), (40, 120, 60)).save(tmp_path / "original.png")
    (tmp_path / "job.json").write_text(json.dumps({"id": "t", "filename": "cat.png", "fileHash": "0" * 64, "sourceFormat": "png"}))
    root = Path(main.__file__).parents[1]
    subprocess.run([sys.executable, "-m", "app.convert", "--doc-dir", str(tmp_path), "--format", "png"], cwd=root, check=True,
                   capture_output=True, timeout=120)
    subprocess.run([sys.executable, "-m", "app.parse_job", "--doc-dir", str(tmp_path), "--parser", "docling-ocr",
                    "--models", str(settings.models_dir.resolve()), "--threads", "2", "--figures", "4"],
                   cwd=root, check=True, capture_output=True, timeout=600)
    parsed = json.loads((tmp_path / "parsed.json").read_text())
    assert [b["type"] for b in parsed["blocks"]] == ["picture"]
    assert (tmp_path / "figures" / "b0000.png").exists()


def test_tab_separated_plain_text_is_read_as_a_table():
    from app.convert import tab_separated

    rows = "\n".join("\t".join(f"{c}{r}" for c in "abcd") for r in range(8))
    assert tab_separated(rows)
    assert not tab_separated("A paragraph of prose.\n\nAnother one\twith a tab.\n")


def test_a_saved_web_page_loses_its_menus_and_edit_links():
    from app.convert import sanitize_html

    page = """<div class="vector-header"><a>Main menu</a></div><nav><a>Contents</a></nav>
    <div role="search"><input></div><h1>Hangul</h1>
    <h2>History<span class="mw-editsection">[edit]</span></h2><p>Hangul was created in 1443.</p>
    <div class="vector-dropdown mw-portlet-lang"><li class="interlanguage-link">Deutsch</li></div>
    <header><p>Article header kept</p></header><div id="footer">Privacy policy</div>"""
    out = sanitize_html(page)
    for gone in ("Main menu", "Contents", "[edit]", "Deutsch", "Privacy policy", "<input"):
        assert gone not in out
    for kept in ("Hangul", "History", "Hangul was created in 1443.", "Article header kept"):
        assert kept in out


def test_long_runs_break_outside_ligatures_and_markdown_html_is_markup():
    from app.convert import breakable, markdown_body

    out = breakable("<p>application-specific-official</p>")
    assert "\u200b" in out and "f\u200bi" not in out and "f\u200bf" not in out
    assert out.replace("\u200b", "") == "<p>application-specific-official</p>"
    body = markdown_body('<p align="center"><img alt="Docling" src="https://example.com/logo.png"></p>\n\n# Title\n\n`<b>code</b>`')
    assert "[Docling]" in body and "&lt;p" not in body and "&lt;b&gt;code" in body
