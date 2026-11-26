import datetime
import http.server
import json
import threading
import httpx
import pytest

from pydantic import SecretStr

from app import vlm
from app.config import Settings
from test_review_regressions import document


def test_administrator_data_directory_symlink_keeps_usage_limits(tmp_path):
    real=tmp_path/'real';real.mkdir()
    link=tmp_path/'configured';link.symlink_to(real,target_is_directory=True)
    settings=Settings(data_dir=link,vlm_daily_images=2)
    assert vlm._reserve(settings,1) == 1
    assert vlm._reserve(settings,2) == 1
    assert vlm._reserve(settings,1) == 0
    assert (real/'vlm-usage.json').is_file()


def figure_job(folder, count=3):
    doc = document("picture", "picture")
    doc.blocks = [doc.blocks[0].model_copy(update={"id": str(i)}) for i in range(count)]
    (folder / "parsed.json").write_text(doc.model_dump_json(by_alias=True))
    (folder / "figures").mkdir()
    for i in range(count):
        (folder / "figures" / f"{i}.png").write_bytes(b"synthetic image")
    (folder / "figures/figures.json").write_text(json.dumps({"blocksFingerprint": "f", "figures": [
        {"blockId": str(i), "file": f"{i}.png"} for i in range(count)]}))


def used(folder):
    path = folder / "vlm-usage.json"
    return json.loads(path.read_text()).get(datetime.date.today().isoformat(), 0) if path.exists() else 0


def test_cancel_interrupts_an_inflight_http_request_and_keeps_unsent_quota(tmp_path):
    figure_job(tmp_path)
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            entered.set()
            release.wait(5)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"figure text"}}]}')
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *_args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    service = threading.Thread(target=server.serve_forever, daemon=True)
    service.start()
    settings = Settings(data_dir=tmp_path, vlm_concurrency=1, vlm_seconds=30, vlm_daily_images=10,
                        openai_base_url=f"http://127.0.0.1:{server.server_port}/v1",
                        openai_api_key=SecretStr("test-key"))
    worker = threading.Thread(target=vlm.describe_figures, args=(tmp_path, settings, cancelled))
    worker.start()
    try:
        assert entered.wait(5)
        cancelled.set()
        worker.join(1)
        assert not worker.is_alive(), "cancelled HTTP request still occupies the parser worker"
        assert used(tmp_path) == 1
        assert all(b["text"] == "picture" for b in json.loads((tmp_path / "parsed.json").read_text())["blocks"])
    finally:
        release.set()
        worker.join(10)
        server.shutdown()
        server.server_close()
        service.join(2)


@pytest.mark.parametrize("kind", ["missing", "link", "traversal"])
def test_unreadable_image_never_consumes_the_daily_allowance(tmp_path, monkeypatch, kind):
    figure_job(tmp_path, 1)
    (tmp_path / "figures/0.png").unlink()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not a permitted crop")
    if kind == "link":
        (tmp_path / "figures/0.png").symlink_to(outside)
    elif kind == "traversal":
        manifest = tmp_path / "figures/figures.json"
        spec = json.loads(manifest.read_text())
        spec["figures"][0]["file"] = "../outside.png"
        manifest.write_text(json.dumps(spec))
    monkeypatch.setattr(vlm, "_ask", lambda *a: (_ for _ in ()).throw(AssertionError("image request must not start")))
    vlm.describe_figures(tmp_path, Settings(data_dir=tmp_path, vlm_daily_images=10))
    assert used(tmp_path) == 0


def test_successful_reading_updates_only_the_allowed_picture(tmp_path, monkeypatch):
    figure_job(tmp_path)
    calls = []
    async def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Observed chart values"}}]})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(vlm.httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw))
    notices = vlm.describe_figures(tmp_path, Settings(data_dir=tmp_path, vlm_daily_images=1,
                                                    openai_api_key=SecretStr("test-key")))
    assert len(calls) == 1 and used(tmp_path) == 1
    assert set(notices) == {"vlm_figures", "vlm_partial"}
    parsed = json.loads((tmp_path / "parsed.json").read_text())
    assert [b["text"] for b in parsed["blocks"]] == ["Observed chart values", "picture", "picture"]
    assert parsed["blocksFingerprint"] != "f"
    assert parsed["blocks"][0]["lineGeometry"] == "unavailable"
