import os
import tempfile
import time
from pathlib import Path

import pytest

DATA = tempfile.mkdtemp(prefix="chunkcanvas-test-")
os.environ.update({
    "CC_DATA_DIR": DATA,
    "CC_DEFAULT_PARSER": "pymupdf",
    "CC_DOCUMENT_TTL_SECONDS": "3600",
    "CC_MAX_PAGES": "5",
    "CC_ALLOWED_HOSTS": "testserver,localhost",
    "CC_MAX_ACTIVE_DOCUMENTS_PER_CLIENT": "1000",
    "CC_MAX_SAMPLE_DOCUMENTS_PER_CLIENT": "1000",
    "CC_UPLOADS_PER_CLIENT_PER_HOUR": "1000",
    # API unit fixtures are trusted synthetic documents. Isolation has its own real subprocess tests.
    "CC_PARSER_SANDBOX": "none",
})

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[3] / "samples"


@pytest.fixture(scope="session")
def client():
    with TestClient(main.app) as c:
        yield c


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def wait_done(client, doc_id: str, token: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"/api/documents/{doc_id}/status", headers=auth(token)).json()
        if st["status"] in ("completed", "failed"):
            return st
        time.sleep(0.3)
    raise AssertionError("parse did not finish")


def upload(client, data: bytes, name: str = "test.pdf", parser: str = "pymupdf"):
    return client.post(f"/api/documents?parser={parser}", content=data,
                       headers={"Content-Type": "application/pdf", "X-Filename": name})
