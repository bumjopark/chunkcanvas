from zipfile import ZipFile

from app import formats
from conftest import upload


def test_directory_limit_before_loading_members(tmp_path, monkeypatch):
    archive = tmp_path / "sheet.xlsx"
    with ZipFile(archive, "w") as out:
        out.writestr("xl/workbook.xml", "")
        out.writestr("xl/styles.xml", "")
    assert formats.detect(archive, archive.name).format == "xlsx"
    monkeypatch.setattr(formats, "MAX_ZIP_ENTRIES", 1)
    assert formats.detect(archive, archive.name).error == formats.DIRECTORY_LIMIT
    monkeypatch.setattr(formats, "MAX_ZIP_ENTRIES", 10)
    monkeypatch.setattr(formats, "MAX_ZIP_DIRECTORY", 10)
    assert formats.detect(archive, archive.name).error == formats.DIRECTORY_LIMIT


def test_incomplete_containers_return_document_error(client):
    for prefix in [formats.ZIP_MAGIC, formats.OLE_MAGIC]:
        result = upload(client, prefix, "partial.bin")
        assert result.status_code == 422
        assert result.json()["detail"]["code"] == "corrupt_file"
