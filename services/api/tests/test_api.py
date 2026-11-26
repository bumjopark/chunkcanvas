import json
import time

from app import main
from conftest import auth, upload, wait_done
from pdfs import blank_pdf, encrypted_pdf, picture_pdf, text_pdf


def test_health_and_config(client):
    assert client.get("/api/health").json()["ok"] is True
    cfg = client.get("/api/config").json()
    ids = [p["id"] for p in cfg["parsers"]]
    assert {"docling", "pymupdf"} <= set(ids)
    assert cfg["limits"]["maxPages"] == 5


def test_upload_parse_edit_flow(client):
    r = upload(client, text_pdf(pages=2))
    assert r.status_code == 201, r.text
    doc_id, token = r.json()["id"], r.json()["accessToken"]
    st = wait_done(client, doc_id, token)
    assert st["status"] == "completed", st

    doc = client.get(f"/api/documents/{doc_id}", headers=auth(token)).json()
    parsed = doc["parsed"]
    assert parsed["document"]["id"] == doc_id
    assert parsed["document"]["pageCount"] == 2
    assert doc["splitPlan"] is None
    types = [b["type"] for b in parsed["blocks"]]
    assert "section_header" in types or "title" in types
    for b in parsed["blocks"]:
        for region in b["regions"]:
            box = region["bbox"]
            assert 0 <= box["x0"] < box["x1"] <= 1 and 0 <= box["y0"] < box["y1"] <= 1

    pdf = client.get(f"/api/documents/{doc_id}/file", headers=auth(token))
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")

    plan = {
        "documentId": doc_id,
        "blocksFingerprint": parsed["blocksFingerprint"],
        "revision": 0,
        "boundaries": [{"blockId": parsed["blocks"][0]["id"]}],
        "settings": {"targetTokens": 500, "maxTokens": 800, "overlapTokens": 0, "structureAware": True,
                     "keepHeadingWithBody": True, "excludeFurniture": True},
    }
    put = lambda base, p=plan: client.put(  # noqa: E731
        f"/api/documents/{doc_id}/split-plan", headers=auth(token), json={"baseRevision": base, "plan": p})
    r = put(0)
    assert r.status_code == 200 and r.json() == {"revision": 1}
    # A save based on an old revision must not overwrite the newer one.
    r = put(0)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stale_revision"
    assert r.json()["detail"]["revision"] == 1
    assert put(1).json() == {"revision": 2}

    saved = client.get(f"/api/documents/{doc_id}", headers=auth(token)).json()["splitPlan"]
    assert saved["revision"] == 2 and saved["boundaries"] == plan["boundaries"]

    bad = dict(plan, boundaries=[{"blockId": "nope"}])
    assert put(2, bad).json()["detail"]["code"] == "invalid_plan"
    dup = dict(plan, boundaries=plan["boundaries"] * 2)
    assert put(2, dup).json()["detail"]["code"] == "invalid_plan"
    # Offsets are allowed only inside blocks with character positions.
    lined = next(b for b in parsed["blocks"] if b.get("lines") and len(b["text"]) > 10 and b["type"] != "table")
    inner = dict(plan, boundaries=[{"blockId": lined["id"], "offset": 5}])
    assert put(2, inner).json() == {"revision": 3}
    for offset in (0, len(lined["text"]), len(lined["text"]) + 5):
        assert put(3, dict(plan, boundaries=[{"blockId": lined["id"], "offset": offset}])).status_code == 422
    atomic = next(b for b in parsed["blocks"] if not b.get("lines"))
    assert put(3, dict(plan, boundaries=[{"blockId": atomic["id"], "offset": 1}])).json()["detail"]["code"] == "invalid_plan"
    unordered = dict(plan, boundaries=[{"blockId": lined["id"], "offset": 8}, {"blockId": lined["id"], "offset": 5}])
    assert put(3, unordered).json()["detail"]["code"] == "invalid_plan"
    other = dict(plan, blocksFingerprint="x")
    assert put(3, other).json()["detail"]["code"] == "plan_mismatch"
    bad_settings = dict(plan, settings=dict(plan["settings"], maxTokens=10))
    assert put(3, bad_settings).status_code == 422
    # Deleted text is saved with the plan, inside the document's blocks only.
    gone = dict(plan, excluded=[{"blockId": lined["id"], "start": 0, "end": 5}])
    assert put(3, gone).json() == {"revision": 4}
    assert client.get(f"/api/documents/{doc_id}", headers=auth(token)).json()["splitPlan"]["excluded"] == gone["excluded"]
    for r in ({"blockId": "nope", "start": 0, "end": 1}, {"blockId": lined["id"], "start": 3, "end": 3},
              {"blockId": lined["id"], "start": 0, "end": len(lined["text"]) + 1}):
        assert put(4, dict(plan, excluded=[r])).json()["detail"]["code"] == "invalid_plan"

    assert client.delete(f"/api/documents/{doc_id}", headers=auth(token)).status_code == 204
    assert client.get(f"/api/documents/{doc_id}/status", headers=auth(token)).status_code == 404


def test_access_requires_the_document_token(client):
    r = upload(client, text_pdf())
    doc_id, token = r.json()["id"], r.json()["accessToken"]
    for headers in ({}, auth("wrong"), {"Authorization": token}):
        assert client.get(f"/api/documents/{doc_id}", headers=headers).status_code == 404
        assert client.get(f"/api/documents/{doc_id}/file", headers=headers).status_code == 404
    assert client.get("/api/documents/..%2F..%2Fetc/status", headers=auth(token)).status_code == 404
    wait_done(client, doc_id, token)


def test_rejects_bad_uploads(client):
    assert upload(client, b"").json()["detail"]["code"] == "empty"
    assert upload(client, bytes(range(256)) * 10).json()["detail"]["code"] == "unsupported_format"
    assert upload(client, encrypted_pdf()).json()["detail"]["code"] == "encrypted_pdf"
    assert upload(client, b"%PDF-1.7\n garbage garbage").json()["detail"]["code"] == "corrupt_pdf"
    assert upload(client, text_pdf(pages=6)).json()["detail"]["code"] == "too_many_pages"
    big = b"%PDF-1.7\n" + b"0" * (11 * 1024 * 1024)
    assert upload(client, big).status_code == 413
    assert upload(client, text_pdf(), parser="nope").json()["detail"]["code"] == "unknown_parser"


def test_blank_pdf_is_not_a_scan_and_fails_clearly(client):
    body = upload(client, blank_pdf()).json()
    assert body["parser"] == "pymupdf" and body["notices"] == []
    st = wait_done(client, body["id"], body["accessToken"])
    assert st["status"] == "failed" and st["error"]["code"] == "no_text"


def test_image_only_pdf_goes_to_ocr(client):
    import io

    import pymupdf
    from PIL import Image

    if not main.S().ocr_available():
        return
    buf = io.BytesIO()
    Image.new("RGB", (800, 1100), "white").save(buf, "PNG")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=buf.getvalue())
    body = upload(client, doc.tobytes()).json()
    assert body["parser"] == "docling-ocr" and body["notices"] == ["auto_ocr"]
    wait_done(client, body["id"], body["accessToken"], timeout=300)


def test_inspection_counts_pages_that_are_pictures_of_text(tmp_path):
    import io

    import pymupdf
    from PIL import Image

    from app.inspect_pdf import inspect

    buf = io.BytesIO()
    Image.new("RGB", (800, 1100), "white").save(buf, "PNG")
    doc = pymupdf.open(stream=text_pdf(pages=3))
    page = doc.new_page()
    page.insert_image(page.rect, stream=buf.getvalue())
    doc.new_page()  # blank: not a scan
    path = tmp_path / "m.pdf"
    doc.save(path)
    assert inspect(str(path)) == {"ok": True, "pageCount": 5, "hasText": True, "scanPages": 1}


def test_text_pdfs_keep_the_chosen_parser(client):
    body = upload(client, text_pdf()).json()
    assert body["parser"] == "pymupdf" and body["notices"] == []
    wait_done(client, body["id"], body["accessToken"])


def test_filename_is_sanitized(client):
    r = upload(client, text_pdf(), name="..%2F..%2Fevil%0Aname.pdf")
    assert r.json()["filename"] == "evilname.pdf"
    wait_done(client, r.json()["id"], r.json()["accessToken"])


def test_samples_are_cached_after_first_parse(client):
    samples = client.get("/api/samples").json()
    assert {"chunking-field-guide", "chunking-guide-ko"} <= {s["key"] for s in samples}
    r = client.post("/api/samples/chunking-guide-ko/documents?parser=pymupdf")
    assert r.status_code == 201
    first = wait_done(client, r.json()["id"], r.json()["accessToken"])
    assert first["status"] == "completed"
    r2 = client.post("/api/samples/chunking-guide-ko/documents?parser=pymupdf").json()
    assert r2["status"] == "completed" and r2["id"] != r.json()["id"]
    doc = client.get(f"/api/documents/{r2['id']}", headers=auth(r2["accessToken"])).json()
    assert doc["parsed"]["document"]["id"] == r2["id"]
    assert any("청킹" in b["text"] for b in doc["parsed"]["blocks"])
    assert client.post("/api/samples/nope/documents").status_code == 404


def test_expired_documents_are_deleted(client):
    r = upload(client, text_pdf()).json()
    wait_done(client, r["id"], r["accessToken"])
    doc_dir = main.S().storage.doc_dir(r["id"])
    assert doc_dir.exists()
    main.S().db.update(r["id"], expires_at=time.time() - 1)
    assert client.get(f"/api/documents/{r['id']}", headers=auth(r["accessToken"])).status_code == 404
    main.cleanup_once()
    assert not doc_dir.exists() and main.S().db.get(r["id"]) is None


def test_reparse_with_another_parser(client):
    r = upload(client, text_pdf()).json()
    wait_done(client, r["id"], r["accessToken"])
    st = client.post(f"/api/documents/{r['id']}/parse?parser=pymupdf", headers=auth(r["accessToken"])).json()
    assert st["status"] in ("queued", "processing", "completed")
    assert wait_done(client, r["id"], r["accessToken"])["status"] == "completed"


def test_rotated_and_cropped_pages_stay_in_bounds(client):
    for kwargs in ({"rotate": 90}, {"crop": (30, 40, 500, 700)}, {"rotate": 270, "crop": (30, 40, 500, 700)}):
        r = upload(client, text_pdf(**kwargs)).json()
        assert wait_done(client, r["id"], r["accessToken"])["status"] == "completed"
        parsed = client.get(f"/api/documents/{r['id']}", headers=auth(r["accessToken"])).json()["parsed"]
        page = parsed["pages"][0]
        if kwargs.get("rotate") in (90, 270):
            assert page["width"] > page["height"]
        assert all(reg["bbox"]["x1"] <= 1 for b in parsed["blocks"] for reg in b["regions"])


def test_unknown_api_route_and_security_headers(client):
    r = client.get("/api/nope")
    assert r.status_code == 404
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400


def test_rate_limit_returns_429():
    from app.ratelimit import SlidingWindow

    w = SlidingWindow(2, 60)
    assert w.allow("a") and w.allow("a") and not w.allow("a")
    assert w.allow("b")
    assert w.retry_after("a") >= 1


def test_parser_output_matches_schema(client):
    from app.models import ParsedDocument

    r = client.post("/api/samples/chunking-field-guide/documents?parser=pymupdf").json()
    wait_done(client, r["id"], r["accessToken"])
    parsed = client.get(f"/api/documents/{r['id']}", headers=auth(r["accessToken"])).json()["parsed"]
    ParsedDocument.model_validate(parsed)
    ids = [b["id"] for b in parsed["blocks"]]
    assert len(ids) == len(set(ids))
    assert [b["readingOrder"] for b in parsed["blocks"]] == list(range(len(ids)))
    assert json.dumps(parsed, ensure_ascii=False).count(" ") == 0


def test_per_client_document_limit(client):
    settings = main.S().settings
    old = settings.max_active_documents_per_client
    settings.max_active_documents_per_client = 1
    try:
        r = upload(client, text_pdf())
        assert r.status_code == 429 and r.json()["detail"]["code"] == "too_many_documents"
    finally:
        settings.max_active_documents_per_client = old


def test_sample_copies_do_not_count_toward_the_document_limit(client):
    settings = main.S().settings
    old = settings.max_active_documents_per_client
    settings.max_active_documents_per_client = 1
    try:
        for _ in range(3):
            assert client.post("/api/samples/chunking-guide-ko/documents?parser=pymupdf").status_code == 201
    finally:
        settings.max_active_documents_per_client = old


def test_pymupdf_blocks_carry_character_lines(client):
    r = client.post("/api/samples/chunking-field-guide/documents?parser=pymupdf").json()
    wait_done(client, r["id"], r["accessToken"])
    parsed = client.get(f"/api/documents/{r['id']}", headers=auth(r["accessToken"])).json()["parsed"]
    lined = [b for b in parsed["blocks"] if b.get("lines")]
    # Tables have a line per row, so they can be cut between rows.
    assert lined and all(b["type"] in ("paragraph", "list_item", "caption", "footnote", "table") for b in lined)
    for b in lined:
        for ln in b["lines"]:
            assert len(ln["xs"]) == ln["end"] - ln["start"]
    items = [b for b in parsed["blocks"] if b["type"] == "list_item"]
    assert items and not any(b["text"].startswith("•") for b in items)


def test_long_pdfs_can_be_cut_to_the_page_limit(client):
    import hashlib

    import pypdfium2 as pdfium

    settings = main.S().settings
    settings.truncate_long_pdfs = True
    try:
        data = text_pdf(pages=7)
        r = upload(client, data)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["pageCount"] == 5 and body["sourcePageCount"] == 7
        assert wait_done(client, body["id"], body["accessToken"])["status"] == "completed"
        doc = client.get(f"/api/documents/{body['id']}", headers=auth(body["accessToken"])).json()
        meta = doc["parsed"]["document"]
        assert meta["pageCount"] == 5 and meta["sourcePageCount"] == 7
        # The hash identifies the file the user uploaded, not the trimmed copy.
        assert meta["fileHash"] == hashlib.sha256(data).hexdigest()
        assert len(doc["parsed"]["pages"]) == 5
        kept = client.get(f"/api/documents/{body['id']}/file", headers=auth(body["accessToken"])).content
        assert len(pdfium.PdfDocument(kept)) == 5
        assert client.get("/api/config").json()["limits"]["truncateLongPdfs"] is True
    finally:
        settings.truncate_long_pdfs = False
    assert upload(client, text_pdf(pages=7)).json()["detail"]["code"] == "too_many_pages"


def test_failed_uploads_leave_no_files(client):
    before = set(main.S().storage.root.iterdir())
    assert upload(client, b"%PDF-1.7 broken").status_code == 422
    assert upload(client, bytes(range(256)) * 10).status_code == 415
    assert set(main.S().storage.root.iterdir()) == before


def test_picture_ocr_that_runs_out_of_time_falls_back_to_the_text_layer(client, monkeypatch):
    s = main.S()
    if not s.ocr_available():
        return
    runner = s.runner
    real, calls = runner._parse_once, []

    def flaky(doc_id, doc_dir, args, timeout):
        calls.append((doc_id, "--ocr" in args, timeout))
        if "--ocr" in args:
            return -1, True  # as if picture OCR had run out of time
        return real(doc_id, doc_dir, args, timeout)

    monkeypatch.setattr(runner, "_parse_once", flaky)
    body = upload(client, text_pdf(), parser="hybrid").json()
    st = wait_done(client, body["id"], body["accessToken"], timeout=300)
    assert st["status"] == "completed" and "ocr_skipped" in st["notices"]
    mine = [c[1:] for c in calls if c[0] == body["id"]]
    assert mine[0] == (True, int(s.settings.job_timeout_seconds * 0.6)) and mine[1][0] is False and len(mine) == 2


def _picture_reading_on(monkeypatch):
    from pydantic import SecretStr

    from app.ratelimit import SlidingWindow

    s = main.S()
    monkeypatch.setattr(s.settings, "openai_api_key", SecretStr("test-key"))
    monkeypatch.setattr(s.settings, "vlm_password", SecretStr("open sesame"))
    monkeypatch.setattr(s, "vlm_failures", SlidingWindow(5, 3600))
    return s


def test_picture_reading_needs_the_password(client, monkeypatch):
    assert client.get("/api/config").json()["vlm"]["available"] is False
    _picture_reading_on(monkeypatch)
    assert client.get("/api/config").json()["vlm"] == {"available": True, "model": "gpt-6-luna"}
    assert client.post("/api/vlm/unlock", json={"password": "nope"}).json()["detail"]["code"] == "vlm_password"
    assert client.post("/api/vlm/unlock", json={"password": "open sesame"}).json()["ok"] is True
    res = client.post("/api/documents", content=text_pdf(),
                      headers={"Content-Type": "application/pdf", "X-Filename": "a.pdf", "X-VLM-Password": "nope"})
    assert res.status_code == 403
    # Five wrong passwords lock the client out for a while, even with the right one.
    for _ in range(3):
        client.post("/api/vlm/unlock", json={"password": "nope"})
    assert client.post("/api/vlm/unlock", json={"password": "open sesame"}).status_code == 429


def test_unlocked_uploads_have_their_pictures_read(client, monkeypatch):
    import httpx
    from app import vlm

    s = _picture_reading_on(monkeypatch)
    sent = []

    async def handle(req):
        sent.append((str(req.url), req.headers.get("Authorization"), json.loads(req.content)))
        answer = {"choices": [{"message": {"content": "Text in the figure: Q3\n\nDescription: A red square."}}]}
        return httpx.Response(200, json=answer)

    client_type = httpx.AsyncClient
    monkeypatch.setattr(vlm.httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw))
    body = client.post("/api/documents?parser=pymupdf", content=picture_pdf(),
                       headers={"Content-Type": "application/pdf", "X-Filename": "p.pdf", "X-VLM-Password": "open sesame"}).json()
    st = wait_done(client, body["id"], body["accessToken"])
    assert st["status"] == "completed" and "vlm_figures" in st["notices"]
    doc = client.get(f"/api/documents/{body['id']}", headers=auth(body["accessToken"])).json()["parsed"]
    picture = next(b for b in doc["blocks"] if b["type"] == "picture")
    assert picture["text"] == "Text in the figure: Q3\n\nDescription: A red square."
    assert picture["markdown"].startswith("[Figure description]\n") and picture["source"]["parser"] == "gpt-6-luna"
    url, authorization, payload = sent[0]
    assert url.endswith("/chat/completions") and authorization == "Bearer test-key"
    image = payload["messages"][1]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/png;base64,") and payload["model"] == "gpt-6-luna"
    # Without the password nothing is sent.
    plain = upload(client, picture_pdf()).json()
    wait_done(client, plain["id"], plain["accessToken"])
    assert len(sent) == 1


def test_picture_reading_stops_at_an_optional_daily_allowance(client, monkeypatch):
    import httpx
    from app import vlm
    import datetime

    s = _picture_reading_on(monkeypatch)
    monkeypatch.setattr(s.settings, "vlm_daily_images", 1)
    (s.settings.data_dir / "vlm-usage.json").write_text(json.dumps({datetime.date.today().isoformat(): 1}))
    sent = []
    async def handle(req):
        sent.append(req)
        return httpx.Response(500)
    client_type = httpx.AsyncClient
    monkeypatch.setattr(vlm.httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw))
    body = client.post("/api/documents?parser=pymupdf", content=picture_pdf(),
                       headers={"Content-Type": "application/pdf", "X-Filename": "p.pdf", "X-VLM-Password": "open sesame"}).json()
    st = wait_done(client, body["id"], body["accessToken"])
    assert st["status"] == "completed" and "vlm_partial" in st["notices"] and "vlm_figures" not in st["notices"]
    assert sent == []


def test_ready_made_samples_open_at_once_with_their_format_and_notices(client, monkeypatch, tmp_path):
    s = main.S()
    built = tmp_path / "gallery" / "hwpx-1"
    built.mkdir(parents=True)
    (built / "source.pdf").write_bytes(text_pdf())
    parsed = {
        "schemaVersion": 1,
        "document": {"id": "x", "filename": "보고서.hwpx", "fileHash": "h", "pageCount": 1, "sourceFormat": "hwpx",
                     "parser": "hybrid", "parserVersion": "test"},
        "pages": [{"index": 0, "width": 595, "height": 842}],
        "blocks": [{"id": "b0000", "readingOrder": 0, "type": "paragraph", "text": "본문", "regions": [],
                    "source": {"parser": "docling"}}],
        "blocksFingerprint": "f",
    }
    (built / "parsed.json").write_text(json.dumps(parsed, ensure_ascii=False))
    (built / "meta.json").write_text(json.dumps({"parser": "hybrid", "notices": ["vlm_figures"], "sourceFormat": "hwpx",
                                                 "pageCount": 1, "fileHash": "abc", "originalName": "보고서.hwpx"}))
    (tmp_path / "gallery" / "manifest.json").write_text(json.dumps([{"key": "hwpx-1", "file": "hwpx-1/source.pdf", "title": "t",
                                                         "language": "ko", "description": "d", "group": "hwpx"}]))
    monkeypatch.setattr(s.settings, "samples_dir", tmp_path)
    monkeypatch.setattr(s.settings, "gallery_dir", tmp_path / "gallery")
    monkeypatch.setattr(s, "samples", s._load_samples())
    listed = client.get("/api/samples").json()
    assert {"key": "hwpx-1", "title": "t", "language": "ko", "description": "d", "group": "hwpx", "format": "hwpx"} in listed
    monkeypatch.setattr(s.settings, "basic_samples", False)
    assert [x["key"] for x in s._load_samples().values()] == ["hwpx-1"]
    body = client.post("/api/samples/hwpx-1/documents").json()
    assert body["status"] == "completed" and body["sourceFormat"] == "hwpx" and body["notices"] == ["vlm_figures"]
    doc = client.get(f"/api/documents/{body['id']}", headers=auth(body["accessToken"])).json()
    assert doc["parsed"]["document"]["id"] == body["id"] and doc["parsed"]["blocks"][0]["text"] == "본문"
    assert client.get(f"/api/documents/{body['id']}/file", headers=auth(body["accessToken"])).status_code == 200


def test_a_table_may_be_cut_only_where_a_row_starts():
    from types import SimpleNamespace

    text = "| k | v |\n| --- | --- |\n| a | 1 |\n| b | 2 |\n| c | 3 |"
    table = {"id": "t", "type": "table", "text": text, "lines": [{"start": 0, "end": len(text)}]}

    def cut(offset):
        return main.boundaries_valid([table, {"id": "p", "type": "paragraph", "text": "end"}],
                                     [SimpleNamespace(block_id="t", offset=offset)])

    assert cut(text.index("| b")) and cut(text.index("| c"))
    # Not inside a row, and not before the first data row (the header would stand alone).
    assert not cut(text.index("| b") + 2) and not cut(text.index("| a"))


def test_an_empty_section_of_a_picture_description_is_dropped():
    from app.vlm import _without_empty_sections

    names = ("그림 속 글자", "설명")
    assert _without_empty_sections("그림 속 글자:\n설명: 고양이가 정면을 본다.", names) == "설명: 고양이가 정면을 본다."
    assert _without_empty_sections("그림 속 글자:\nA, B\n\n설명:\n표입니다.", names) == "그림 속 글자:\nA, B\n\n설명:\n표입니다."
    assert _without_empty_sections("설명: 사진.\n그림 속 글자:", names) == "설명: 사진."


def test_a_label_listed_many_times_in_a_row_is_written_once_with_its_count():
    from app.vlm import _without_runs

    answer = "Text in the figure:\nTotal, Expense Row, Expense Row, Expense Row, Expense Row, Taxi, Taxi\n\nDescription:\nA form."
    assert _without_runs(answer) == "Text in the figure:\nTotal, Expense Row (×4), Taxi, Taxi\n\nDescription:\nA form."
