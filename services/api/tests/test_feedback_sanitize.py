import io
import zipfile
import pytest

from lxml import etree

from app.convert import ConversionError, sanitize_package, sanitize_xml_text

REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def package(body, relationships, part="word/document.xml"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   f'<Override PartName="/{part}" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr(part, f'<w:document xmlns:w="{WORD}" xmlns:r="{REL}" xmlns:a="urn:drawing">{body}</w:document>')
        name = part.rsplit("/", 1)[-1]
        z.writestr(f"word/_rels/{name}.rels", f'<Relationships xmlns="{PKG}">{relationships}</Relationships>')
    return out.getvalue()


def sanitize(tmp_path, data):
    src, dst = tmp_path / "in.docx", tmp_path / "out.docx"
    src.write_bytes(data)
    sanitize_package(src, dst)
    return zipfile.ZipFile(dst)


def relationship(rid, kind):
    return f'<Relationship Id="{rid}" Type="{kind}" Target="https://example.invalid/remote" TargetMode="External"/>'


def test_only_passive_hyperlink_consumers_keep_external_relationships(tmp_path):
    rels = relationship("legit", REL + "/hyperlink") + relationship("image", REL + "/hyperlink")
    data = package('<w:hyperlink r:id="legit"><w:r><w:t>Open website</w:t></w:r></w:hyperlink>'
                   '<a:blip r:link="image"/>', rels)
    with sanitize(tmp_path, data) as z:
        nodes = etree.fromstring(z.read("word/_rels/document.xml.rels"))
    by_id = {n.get("Id"): n for n in nodes}
    assert by_id["legit"].get("Target") == "https://example.invalid/remote"
    assert by_id["image"].get("Target") != "https://example.invalid/remote"


def test_fake_hyperlink_type_does_not_exempt_an_image(tmp_path):
    data = package('<a:blip r:link="image"/>', relationship("image", "urn:x/hyperlink"))
    with sanitize(tmp_path, data) as z:
        assert b'https://example.invalid/remote' not in z.read("word/_rels/document.xml.rels")


def test_content_type_identifies_xml_even_with_a_dat_extension(tmp_path):
    data = package('<w:fldSimple w:instr="INCLUDEPICTURE https://example.invalid/image"/>', "",
                   part="word/document.dat")
    with sanitize(tmp_path, data) as z:
        assert b"INCLUDEPICTURE" not in z.read("word/document.dat")


@pytest.mark.parametrize('name,mime', [('word/afchunk.html','text/html'), ('word/import.dat','application/octet-stream'),
                                     ('word/import.xml','application/xml')])
def test_altchunk_import_is_sanitized_even_with_a_false_declared_type(tmp_path,name,mime):
    out = io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('[Content_Types].xml', '<Types><Override PartName="/'+name+'" ContentType="'+mime+'"/></Types>')
        z.writestr('word/document.xml', f'<w:document xmlns:w="{WORD}" xmlns:r="{REL}"><w:altChunk r:id="chunk"/></w:document>')
        z.writestr('word/_rels/document.xml.rels',f'<Relationships xmlns="{PKG}"><Relationship Id="chunk" Type="{REL}/aFChunk" Target="{name[5:]}"/></Relationships>')
        z.writestr(name,'<html><body>Keep this text<img src="http://127.0.0.1:8417/private" alt="Chart"/><p style="background:url(file:///etc/passwd)">Body</p></body></html>')
    with sanitize(tmp_path,out.getvalue()) as z:
        body = z.read(name).decode()
        assert 'Keep this text' in body and '[Chart]' in body and 'Body' in body
        assert '127.0.0.1' not in body and 'file:' not in body and 'url(' not in body


@pytest.mark.parametrize('member',['word/media/imgA%20x.png','word/media/imgA x.png'])
def test_encoded_internal_image_name_is_preserved(tmp_path,member):
    original = package('<a:blip r:embed="img"/>', f'<Relationship Id="img" Type="{REL}/image" Target="media/imgA%20x.png"/>')
    out = io.BytesIO(original)
    with zipfile.ZipFile(out,'a') as z: z.writestr(member,b'embedded-image')
    with sanitize(tmp_path,out.getvalue()) as z:
        assert b'Target="media/imgA%20x.png"' in z.read('word/_rels/document.xml.rels')
        assert z.read(member) == b'embedded-image'


def test_altchunk_wordprocessingml_preserves_paragraphs_and_sanitizes_resources(tmp_path):
    ns = 'http://schemas.microsoft.com/office/word/2003/wordml'
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('word/document.xml', f'<w:document xmlns:w="{WORD}" xmlns:r="{REL}"><w:altChunk r:id="chunk"/></w:document>')
        z.writestr('word/_rels/document.xml.rels', f'<Relationships xmlns="{PKG}"><Relationship Id="chunk" Type="{REL}/aFChunk" Target="import.dat"/></Relationships>')
        z.writestr('word/import.dat', f'<w:wordDocument xmlns:w="{ns}" xmlns:v="urn:schemas-microsoft-com:vml"><w:body><w:p><w:r><w:t>First paragraph</w:t></w:r></w:p><w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p><v:imagedata src="https://example.invalid/image"/><v:imagedata src="media/internal.png"/></w:body></w:wordDocument>')
        z.writestr('word/media/internal.png', b'internal-image')
    with sanitize(tmp_path, out.getvalue()) as z:
        clean = z.read('word/import.dat')
        root = etree.fromstring(clean)
        assert root.tag == '{'+ns+'}wordDocument'
        assert [p.xpath('string(.)') for p in root.findall('.//{'+ns+'}p')] == ['First paragraph', 'Second paragraph']
        assert b'https://example.invalid/image' not in clean
        assert b'src="media/internal.png"' in clean


def test_svg_literal_namespaces_and_inline_raster_survive_without_external_loading():
    text = '''<!DOCTYPE svg [<!ENTITY ns "http://www.w3.org/2000/svg"><!ENTITY label "Chart">]>
    <svg xmlns="&ns;" xmlns:xlink="http://www.w3.org/1999/xlink">
      <text>&label;</text><image xlink:href="data:image/png;base64,YQ=="/>
      <image xlink:href="https://example.invalid/remote.png"/></svg>'''
    clean = sanitize_xml_text(text)
    root = etree.fromstring(clean.encode())
    assert root[0].text == 'Chart' and '<!DOCTYPE' not in clean
    assert root[1].get('{http://www.w3.org/1999/xlink}href') == 'data:image/png;base64,YQ=='
    assert root[2].get('{http://www.w3.org/1999/xlink}href') == ''
    for declaration in ['<!ENTITY label SYSTEM "file:///etc/passwd">', '<!ENTITY a "abc"><!ENTITY label "&a;&a;">']:
        with pytest.raises(ConversionError):
            sanitize_xml_text('<!DOCTYPE svg ['+declaration+']><svg xmlns="http://www.w3.org/2000/svg"><text>&label;</text></svg>')


def test_passive_odf_shape_links_are_preserved_but_image_links_are_removed():
    text = '''<office xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:xlink="http://www.w3.org/1999/xlink">
      <draw:a xlink:href="ftp://example.invalid/document"><draw:image xlink:href="ftp://example.invalid/image"/></draw:a>
      <draw:a xlink:href="../another-document.pdf"/></office>'''
    root = etree.fromstring(sanitize_xml_text(text).encode())
    href = '{http://www.w3.org/1999/xlink}href'
    assert root[0].get(href).startswith('ftp:') and root[0][0].get(href) == ''
    assert root[1].get(href) == '../another-document.pdf'


def test_embedded_zip_packages_are_sanitized_too(tmp_path):
    nested = package('<a:blip r:link="image"/>', relationship("image", "urn:x/hyperlink"))
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("word/embeddings/embedded.docx", nested)
    with sanitize(tmp_path, outer.getvalue()) as z:
        with zipfile.ZipFile(io.BytesIO(z.read("word/embeddings/embedded.docx"))) as inner:
            assert b'https://example.invalid/remote' not in inner.read("word/_rels/document.xml.rels")


def test_odf_preserves_passive_text_links_but_not_linked_images():
    text = ('<office xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
            'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
            'xmlns:xlink="http://www.w3.org/1999/xlink">'
            '<text:a xlink:href="https://example.invalid/page">Read more</text:a>'
            '<draw:image xlink:href="https://example.invalid/picture"/>'
            '<text:a xlink:href="javascript:bad()">Unsafe</text:a></office>')
    root = etree.fromstring(sanitize_xml_text(text).encode())
    href = "{http://www.w3.org/1999/xlink}href"
    assert root[0].get(href) == "https://example.invalid/page"
    assert root[1].get(href) == ""
    assert root[2].get(href) == ""


def test_shared_relationship_with_an_active_consumer_is_not_exempt(tmp_path):
    data = package('<w:hyperlink r:id="both"/><a:blip r:link="both"/>', relationship("both", REL + "/hyperlink"))
    with sanitize(tmp_path, data) as z:
        assert b'https://example.invalid/remote' not in z.read("word/_rels/document.xml.rels")


def test_nested_package_depth_is_bounded(tmp_path):
    data = package("", "")
    for _ in range(6):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("embedded.docx", data)
        data = out.getvalue()
    with pytest.raises(ConversionError, match="processing limits"):
        sanitize(tmp_path, data)


def test_nested_expansion_shares_the_outer_budget(tmp_path, monkeypatch):
    from app import formats
    nested = package("", "")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("embedded.docx", nested)
    monkeypatch.setattr(formats, "MAX_ZIP_TOTAL", len(nested) + 1)
    with pytest.raises(ConversionError, match="processing limits"):
        sanitize(tmp_path, out.getvalue())
