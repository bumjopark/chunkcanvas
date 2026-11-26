import json
from pathlib import Path
import subprocess

import pytest

from app.config import API_ROOT, Settings
from app.convert import ConversionError, sanitize_xml_text
from app.sandbox import child_env, python_command


def test_xml_quoting_prefixes_entities_and_split_fields_are_parsed():
    from lxml import etree
    text = """<d xmlns:x='urn:word' xmlns:y='urn:link'>
      <x:fldSimple x:instr='INCLUDETEXT &quot;file:///secret&quot;'/>
      <image y:href='f&#105;le:///secret'/><image y:href='../../secret'/>
      <image y:href='/Pictures/a.png'/><image y:href='Pictures/a.png'/>
      <x:fldChar x:fldCharType='begin'/><x:instrText> INCLU</x:instrText>
      <x:instrText>DETEXT &quot;/secret&quot; </x:instrText><x:fldChar x:fldCharType='end'/>
      <Relationship TargetMode='External' Target='https://example.invalid/secret' Type='x/image'/>
      <Relationship Target='../media/a.png' Type='x/image'/>
    </d>"""
    out = etree.fromstring(sanitize_xml_text(text, {"Pictures/a.png", "media/a.png"}, "word").encode())
    assert out[0].get("{urn:word}instr") == ""
    assert [n.get("{urn:link}href") for n in out.iter("image")] == ["", "", "", "Pictures/a.png"]
    assert all(not n.text for n in out.iter("{urn:word}instrText"))
    rels = list(out.iter("Relationship"))
    assert rels[0].get("Target") == "removed-external-link" and "TargetMode" not in rels[0].attrib
    assert rels[1].get("Target") == "../media/a.png"
    with pytest.raises(ConversionError):
        sanitize_xml_text('<!DOCTYPE x [<!ENTITY y SYSTEM "file:///secret">]><x>&y;</x>')


def test_unused_office_doctype_is_removed_without_loading_it(tmp_path):
    from lxml import etree
    dtd = tmp_path / "office.dtd"
    dtd.write_text('<!ATTLIST document fetched CDATA "secret-from-external-dtd">')
    out = sanitize_xml_text(f'<!DOCTYPE document SYSTEM "{dtd.as_uri()}"><document>visible</document>')
    assert "DOCTYPE" not in out and "secret-from-external-dtd" not in out
    assert etree.fromstring(out.encode()).text == "visible"
    # An unused declaration is also rejected: it must not survive in attributes or text.
    with pytest.raises(ConversionError):
        sanitize_xml_text('<!DOCTYPE x [<!ENTITY secret "hidden">]><x/>')


def test_package_accepts_empty_odf_shortcuts_and_sanitizes_vml_conditionals(tmp_path):
    import zipfile
    from lxml import etree
    from app.convert import sanitize_package
    src, dst = tmp_path / "in.zip", tmp_path / "out.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("Configurations2/accelerator/current.xml", b"")
        z.writestr("content.xml", '<!DOCTYPE x SYSTEM "office.dtd"><x>visible</x>')
        z.writestr("ppt/drawings/image.vml", '<xml xmlns:v="urn:vml" xmlns:x="urn:link">'
                   '<![if gte mso 9]><v:image x:href="file:///secret"/><![endif]></xml>')
    sanitize_package(src, dst)
    with zipfile.ZipFile(dst) as z:
        assert z.read("Configurations2/accelerator/current.xml") == b""
        assert z.read("content.xml") == b"<x>visible</x>"
        root = etree.fromstring(z.read("ppt/drawings/image.vml"))
        assert root[0].get("{urn:link}href") == ""
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("content.xml", b"")
    with pytest.raises(ConversionError):
        sanitize_package(src, dst)


def test_legacy_manifest_namespace_does_not_require_loading_its_dtd(tmp_path):
    import zipfile
    from lxml import etree
    from app.convert import sanitize_package
    declaration = ('<!DOCTYPE manifest:manifest PUBLIC '
                   '"-//OpenOffice.org//DTD Manifest 1.0//EN" "Manifest.dtd">')
    body = ('<manifest:manifest><manifest:file-entry manifest:full-path="/" '
            'manifest:media-type="application/vnd.oasis.opendocument.spreadsheet-template"/>'
            '</manifest:manifest>')
    src, dst = tmp_path / "in.zip", tmp_path / "out.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("META-INF/manifest.xml", declaration + body)
    sanitize_package(src, dst)
    with zipfile.ZipFile(dst) as z:
        data = z.read("META-INF/manifest.xml")
    assert b"DOCTYPE" not in data
    root = etree.fromstring(data)
    assert root.tag == "{http://openoffice.org/2001/manifest}manifest"
    assert root[0].get("{http://openoffice.org/2001/manifest}full-path") == "/"
    # No generic recovery for unrelated malformed XML or an attacker-supplied DTD.
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("META-INF/manifest.xml", declaration.replace("Manifest.dtd", "file:///secret") + body)
    with pytest.raises(ConversionError):
        sanitize_package(src, dst)


def test_landlock_denies_other_documents_network_and_path_metadata_changes(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    secret = other / "secret"
    secret.write_text("outside-document")
    job = tmp_path / "job"
    job.mkdir()
    (job / "escape").symlink_to(secret)
    probe = job / "probe.py"
    probe.write_text('''import os, socket, sys, threading, ctypes, errno
from pathlib import Path
root, secret = map(Path, sys.argv[1:])
def blocked(call):
    try:
        call()
    except PermissionError:
        return
    raise AssertionError("operation escaped the sandbox")
blocked(lambda: secret.read_text())
blocked(lambda: (root / "escape").read_text())
blocked(lambda: secret.write_text("changed"))
blocked(lambda: secret.chmod(0o777))
with (root / "result").open("w") as own:
    blocked(lambda: os.fchmod(own.fileno(), 0o777))
blocked(lambda: socket.socket(socket.AF_INET))
blocked(lambda: socket.socket(socket.AF_INET6))
blocked(lambda: socket.socket(socket.AF_UNIX).connect(str(secret.parent / "socket")))
blocked(lambda: os.setpriority(os.PRIO_PROCESS, os.getpid(), 19))
blocked(lambda: os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0)))
for detach in (os.setsid, lambda: os.setpgid(0, 0)):
    pid = os.fork()
    if pid == 0:
        try:
            detach()
        except PermissionError:
            os._exit(0)
        os._exit(4)
    assert os.waitpid(pid, 0)[1] == 0, 'child escaped the job process group'
(root / "result").write_text("inside-document")
assert (root / "result").read_text() == "inside-document"
assert Path('/proc/self/oom_score_adj').read_text().strip() == '1000'
ran = []
t = threading.Thread(target=lambda: ran.append(True)); t.start(); t.join()
assert ran == [True]
libc = ctypes.CDLL(None, use_errno=True)
sec = ctypes.CDLL('libseccomp.so.2')
nr = sec.seccomp_syscall_resolve_name(b'clone')
ret = libc.syscall(ctypes.c_long(nr), ctypes.c_ulong(0x10000000 | 17), 0, 0, 0, 0)
if ret == 0: os._exit(0)
if ret > 0:
    os.waitpid(ret, 0)
    raise AssertionError('new user namespace was allowed')
assert ctypes.get_errno() == errno.EPERM
''')
    settings = Settings(parser_sandbox="landlock")
    code = "import runpy, sys; sys.argv = " + repr([str(probe), str(job), str(secret)]) + "; runpy.run_path(sys.argv[0], run_name='__main__')"
    cmd = python_command(settings, "timeit", ["-n", "1", "-r", "1", code], job)
    result = subprocess.run(cmd, cwd=API_ROOT, env=child_env(settings), capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode()
    assert secret.read_text() == "outside-document"
    assert secret.stat().st_mode & 0o777 != 0o777
    assert (job / "result").read_text() == "inside-document"


@pytest.mark.parametrize("fmt,maker,expected", [
    ("pptx", "make_pptx", "청킹 전략"), ("xlsx", "make_xlsx", "목표 토큰"),
    ("hwpx", "make_hwpx", "재난 상황 분석"),
])
def test_office_and_hangul_conversion_in_readonly_container(tmp_path, fmt, maker, expected):
    import test_formats
    settings = Settings(parser_sandbox="landlock")
    if not settings.soffice_path:
        pytest.skip("LibreOffice not installed")
    (tmp_path / f"original.{fmt}").write_bytes(getattr(test_formats, maker)())
    cmd = python_command(settings, "app.convert", ["--doc-dir", str(tmp_path), "--format", fmt,
                         "--soffice", str(settings.soffice_path)], tmp_path)
    cmd = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--bind", str(tmp_path), str(tmp_path), "--", *cmd]
    result = subprocess.run(cmd, cwd=API_ROOT, env=child_env(settings), capture_output=True, timeout=90)
    assert result.returncode == 0, result.stderr.decode()
    assert expected in test_formats.pdf_text(tmp_path / "source.pdf")


@pytest.mark.parametrize("parser", ["pymupdf", "hybrid", "docling-ocr"])
def test_parsing_models_in_landlock(tmp_path, parser):
    import shutil
    settings = Settings(parser_sandbox="landlock")
    if parser != "pymupdf" and not (settings.models_dir / "docling-project--docling-layout-heron").exists():
        pytest.skip("Docling models not installed")
    shutil.copyfile(API_ROOT.parents[1] / "samples/chunking-field-guide.pdf", tmp_path / "source.pdf")
    (tmp_path / "job.json").write_text(json.dumps({"id": "isolation", "filename": "sample.pdf", "fileHash": "h"}))
    cmd = python_command(settings, "app.parse_job", ["--doc-dir", str(tmp_path), "--parser", parser,
                         "--models", str(settings.models_dir), "--threads", "2"], tmp_path)
    result = subprocess.run(cmd, cwd=API_ROOT, env=child_env(settings), capture_output=True, timeout=180)
    assert result.returncode == 0, result.stderr.decode()[-2000:]
    parsed = json.loads((tmp_path / "parsed.json").read_text())
    assert len(parsed["blocks"]) > 5


@pytest.mark.parametrize(('mime', 'name', 'payload', 'accept'), [
    ('text', 'Object 1/Configurations2/accelerator/current.xml', b'', True),
    ('spreadsheet', 'Charts/Custom object/Configurations2/accelerator/current.xml', b'', True),
    ('text', 'Object 1/Configurations2/accelerator/current.xml', b' ', False),
    ('text', 'Object 1/Configurations2/accelerator/current.xml', b'<broken', False),
    ('text', 'Object 1/Configurations2/accelerator/current.xml', b'<!DOCTYPE x [<!ENTITY y "local">]><x/>', False),
    ('text', 'Object 1/NotConfigurations2/accelerator/current.xml', b'', False),
    ('text', 'Object 1/content.xml', b'', False),
    ('text', 'content.xml', b'<broken', False),
    ('text', 'content.xml', b'', False),
    ('text', 'content.xml', b'<!DOCTYPE x [<!ENTITY y "local">]><x/>', False),
    (None, 'Object 1/Configurations2/accelerator/current.xml', b'', False),
])
def test_embedded_odf_shortcut_exception(tmp_path, mime, name, payload, accept):
    import zipfile
    from app.convert import sanitize_package

    src, dst = tmp_path / 'in.zip', tmp_path / 'out.zip'
    with zipfile.ZipFile(src, 'w') as archive:
        if mime:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.' + mime)
        archive.writestr(name, payload)
    if accept:
        sanitize_package(src, dst)
        with zipfile.ZipFile(dst) as archive:
            assert archive.read(name) == b''
    else:
        with pytest.raises(ConversionError) as caught:
            sanitize_package(src, dst)
        assert caught.value.code == ('unsupported_format' if b'<!ENTITY' in payload else 'corrupt_file')
