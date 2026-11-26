import os

import pytest

from app.config import Settings
from app.storage import Storage


@pytest.fixture
def stored(tmp_path):
    storage = Storage(Settings(data_dir=tmp_path / "data"))
    doc_id = "a" * 24
    folder = storage.doc_dir(doc_id)
    folder.mkdir()
    return storage, doc_id, folder


def test_saving_does_not_follow_predictable_temporary_link(stored, tmp_path):
    storage, doc_id, folder = stored
    victim = tmp_path / "outside.txt"
    victim.write_text("unchanged")
    (folder / "split-plan.tmp").symlink_to(victim)
    storage.write_json(doc_id, "split-plan.json", {"revision": 1})
    assert victim.read_text() == "unchanged"
    assert storage.read_json(doc_id, "split-plan.json") == {"revision": 1}


@pytest.mark.parametrize("link", ["symbolic", "hard"])
def test_document_json_never_reads_a_link(stored, tmp_path, link):
    storage, doc_id, folder = stored
    victim = tmp_path / "outside.json"
    victim.write_text('{"outside":true}')
    target = folder / "parsed.json"
    if link == "symbolic":
        target.symlink_to(victim)
    else:
        os.link(victim, target)
    assert storage.read_json(doc_id, "parsed.json") is None


def test_document_directory_link_cannot_redirect_save(stored, tmp_path):
    storage, doc_id, folder = stored
    folder.rmdir()
    victim = tmp_path / "outside"
    victim.mkdir()
    folder.symlink_to(victim, target_is_directory=True)
    with pytest.raises(OSError):
        storage.write_json(doc_id, "split-plan.json", {"revision": 1})
    assert list(victim.iterdir()) == []


def test_parser_workspace_cannot_change_parent_control_files(stored):
    storage, doc_id, folder = stored
    storage.write_json(doc_id, "job.json", {"id": doc_id})
    storage.write_json(doc_id, "split-plan.json", {"revision": 3})
    (folder / "source.pdf").write_bytes(b"%PDF-input")
    with storage.job_workspace(doc_id) as work:
        assert work.parent != folder
        assert not (work / "split-plan.json").exists()
        (work / "split-plan.tmp").symlink_to(folder / "split-plan.json")
        (work / "source.pdf").unlink()
        (work / "source.pdf").symlink_to(folder / "split-plan.json")
        with pytest.raises(OSError):
            storage.publish_prepared(doc_id, work)
    assert storage.read_json(doc_id, "split-plan.json") == {"revision": 3}
    assert (folder / "source.pdf").read_bytes() == b"%PDF-input"


def test_nonregular_output_is_rejected_without_blocking(stored):
    from app import safeio
    _, _, folder = stored
    os.mkfifo(folder / "parsed.json")
    with pytest.raises(OSError):
        safeio.read_json(folder / "parsed.json")


def test_copy_cannot_follow_a_parent_directory_link(stored, tmp_path):
    from app import safeio
    _, _, folder = stored
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "image.png").write_bytes(b"outside")
    (folder / "figures").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        safeio.copy_file(folder / "figures/image.png", folder / "copy.png")
    assert not (folder / "copy.png").exists()


def test_response_rejects_links_and_supports_pdf_ranges(stored, tmp_path):
    from app.safeio import ArtifactResponse
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    _, _, folder = stored
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-private")
    source = folder / "source.pdf"
    source.symlink_to(outside)
    app = Starlette(routes=[Route("/f", lambda _: ArtifactResponse(source, "application/pdf"))])
    with TestClient(app) as client:
        assert client.get("/f").status_code == 404
        source.unlink()
        source.write_bytes(b"%PDF-public")
        response = client.get("/f", headers={"Range": "bytes=0-4"})
        assert response.status_code == 206
        assert response.content == b"%PDF-"


def test_atomic_replacement_does_not_modify_link_target(stored, tmp_path):
    storage, doc_id, folder = stored
    outside = tmp_path / "outside.json"
    outside.write_text("unchanged")
    (folder / "split-plan.json").symlink_to(outside)
    storage.write_json(doc_id, "split-plan.json", {"revision": 4})
    assert outside.read_text() == "unchanged"
    assert not (folder / "split-plan.json").is_symlink()


def test_resume_keeps_conversion_word_evidence(stored):
    storage, doc_id, folder = stored
    storage.write_json(doc_id, "job.json", {"id": doc_id})
    with storage.job_workspace(doc_id) as work:
        (work / "source.pdf").write_bytes(b"%PDF-input")
        (work / "source-words.json").write_text('["wrappedword"]')
        (work / "source-furniture.json").write_text('[{"kind":"footer","text":"Even footer"}]')
        storage.publish_prepared(doc_id, work)
    with storage.job_workspace(doc_id) as resumed:
        assert (resumed / "source-words.json").read_text() == '["wrappedword"]'
        assert "Even footer" in (resumed / "source-furniture.json").read_text()
