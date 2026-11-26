"""ChunkCanvas HTTP API. Run with:  uvicorn app.main:app --host 127.0.0.1 --port 8300"""
import asyncio
import hashlib
import hmac
import json
import ipaddress
import logging
import os
import re
import secrets
import shutil
import time
import tempfile
import unicodedata
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, capabilities, safeio
from .formats import ALL, CORRUPT, DIRECTORY_LIMIT, ENCRYPTED, EXTENSIONS, Detected
from .config import Settings, get_settings
from .db import Database, DocRow, QueueFull
from .jobs import JobRunner
from .models import ParserId, SaveSplitPlan
from .offsets import length as text_length, positions as text_positions, wire_document
from .ratelimit import SlidingWindow
from .sandbox import child_env, python_command, verify_sandbox
from .storage import DOC_ID, Storage

log = logging.getLogger("chunkcanvas")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
SYSTEM_CLIENT = "system"



def content_security_policy(settings: Settings) -> str:
    script, connect = "'self' 'wasm-unsafe-eval'", "'self'"
    if settings.csp_allow_cloudflare_insights:
        script += " https://static.cloudflareinsights.com"
        connect += " https://cloudflareinsights.com"
    return (
        f"default-src 'self'; script-src {script}; worker-src 'self' blob:; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
        f"connect-src {connect}; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )


def error(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


class App:
    """Process-wide state, created in the lifespan handler."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.storage = Storage(settings)
        self.runner = JobRunner(settings, self.db, self.storage)
        self.requests = SlidingWindow(settings.requests_per_client_per_minute, 60)
        self.uploads = SlidingWindow(settings.uploads_per_client_per_hour, 3600)
        self.sample_creations = SlidingWindow(settings.samples_per_client_per_hour, 3600)
        self.inspections = asyncio.Semaphore(settings.inspection_concurrency)
        # Wrong picture-reading passwords per client: a few typos, not a guessing attack.
        self.vlm_failures = SlidingWindow(5, 3600)
        self.salt = self._salt()
        self.samples = self._load_samples()
        self.parser_versions = {"docling": version("docling"), "pymupdf": version("pymupdf")}

    def _salt(self) -> bytes:
        path = self.settings.data_dir / ".client-salt"
        if not path.exists():
            path.write_bytes(secrets.token_bytes(32))
            path.chmod(0o600)
        return path.read_bytes()

    def _load_samples(self) -> dict[str, dict]:
        # samples/manifest.json lists the basic samples; gallery/manifest.json, when present, a site's
        # own gallery built with scripts/build_samples.py. Paths are relative to each list.
        out = {}
        manifests = [self.settings.samples_dir / "manifest.json"] if self.settings.basic_samples else []
        for manifest in manifests + [self.settings.gallery_dir / "manifest.json"]:
            if not manifest.exists():
                continue
            for s in json.loads(manifest.read_text(encoding="utf-8")):
                original = manifest.parent / s["file"]
                if not original.exists():
                    continue
                sample = {**s, "path": original}
                # Ready-made samples carry their parse next to the display PDF, so any format opens
                # at once, with the notices of the run that built it.
                built = original.parent / "meta.json"
                if built.exists() and (original.parent / "parsed.json").exists():
                    sample["built"] = json.loads(built.read_text(encoding="utf-8"))
                sample["fileHash"] = (sample.get("built") or {}).get("fileHash") or hashlib.sha256(original.read_bytes()).hexdigest()
                out[s["key"]] = sample
        return out

    def ocr_available(self) -> bool:
        return capabilities.ocr_available(self.settings)

    def parser_allowed(self, parser: str) -> bool:
        return parser in ("hybrid", "docling", "pymupdf") or (parser == "docling-ocr" and self.ocr_available())

    def page_limit(self, parser: str) -> int:
        return capabilities.page_limit(self.settings, parser)

    def _client_address(self, request: Request, ipv6_prefix: int) -> str:
        ip = request.client.host if request.client else "unknown"
        if self.settings.trust_proxy_headers:
            # Only safe when every request comes through the proxy (see CC_TRUST_PROXY_HEADERS).
            forwarded = request.headers.get(self.settings.proxy_ip_header, "")
            try:
                ip = str(ipaddress.ip_address(forwarded.split(",")[0].strip()))
            except ValueError:
                pass
        try:
            address = ipaddress.ip_address(ip)
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            ip = str(ipaddress.ip_network(f"{address}/{ipv6_prefix}", strict=False)
                     if isinstance(address, ipaddress.IPv6Address) else address)
        except ValueError:
            pass
        return ip

    def client_key(self, request: Request) -> str:
        ip = self._client_address(request, self.settings.ipv6_rate_prefix)
        return hmac.new(self.salt, ip.encode(), hashlib.sha256).hexdigest()[:24]

    def network_key(self, request: Request) -> str:
        """The client's wider network: an IPv6 /48 (one site's allocation) or the IPv4 address."""
        prefix = min(self.settings.ipv6_network_prefix, self.settings.ipv6_rate_prefix)
        ip = self._client_address(request, prefix)
        return hmac.new(self.salt, b"network:" + ip.encode(), hashlib.sha256).hexdigest()[:24]


state: App | None = None


def S() -> App:
    assert state is not None
    return state


async def cleanup_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(cleanup_once)
        except Exception:  # noqa: BLE001
            log.exception("cleanup failed")
        await asyncio.sleep(30)


def cleanup_once() -> None:
    """Delete expired documents and stray directories (e.g. abandoned uploads)."""
    s = S()
    now = time.time()
    for doc_id in s.db.expired(now):
        s.runner.cancel(doc_id)
        s.storage.remove(doc_id)
        s.db.delete(doc_id)
    known = s.db.all_ids()
    for name in s.storage.orphan_dirs(known):
        path = s.storage.root / name
        if now - path.lstat().st_mtime > 300:
            s.storage.remove(name)


def warm_samples() -> None:
    """Parse bundled samples once with the default parser so 'Try a sample' is instant."""
    s = S()
    parser = s.settings.default_parser
    for key, sample in s.samples.items():
        if s.storage.load_sample_cache(key, parser, sample["fileHash"]) is None:
            try:
                create_document_from_sample(key, parser, SYSTEM_CLIENT)
            except HTTPException as exc:
                if exc.detail.get("code") == "busy":
                    log.info("sample warmup deferred until parser capacity is available")
                    break
                raise


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global state
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    verify_sandbox(settings)
    state = App(settings)
    state.runner.start()
    cleanup_once()
    warm_samples()
    task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        state.runner.stop()


app = FastAPI(title="ChunkCanvas API", version=__version__, lifespan=lifespan, docs_url="/api/docs",
              openapi_url="/api/openapi.json", redoc_url=None)
_settings = get_settings()
CSP = content_security_policy(_settings)
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_settings.allowed_hosts or ["*"])


@app.middleware("http")
async def edge(request: Request, call_next):
    s = S()
    if (
        s.settings.trust_proxy_headers
        and s.settings.public_origin
        and request.headers.get("x-forwarded-proto") == "http"
    ):
        target = s.settings.public_origin.rstrip("/") + request.url.path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=308)
    if request.url.path.startswith("/api/"):
        key = s.client_key(request)
        if not s.requests.allow(key):
            return JSONResponse(
                {"detail": {"code": "rate_limited", "message": "Too many requests. Please slow down."}},
                status_code=429,
                headers={"Retry-After": str(s.requests.retry_after(key))},
            )
    response: Response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if s.settings.public_origin and s.settings.public_origin.startswith("https://"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# --- helpers -------------------------------------------------------------------------


def iso(ts: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


def authorize(doc_id: str, request: Request) -> DocRow:
    """404 for unknown, expired or wrongly-authorized documents alike."""
    not_found = error(404, "not_found", "This document does not exist or has expired.")
    if not DOC_ID.match(doc_id):
        raise not_found
    row = S().db.get(doc_id)
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if (
        row is None
        or row.expires_at <= time.time()
        or not token
        or not hmac.compare_digest(row.token_hash, hashlib.sha256(token.encode()).hexdigest())
    ):
        raise not_found
    return row


def status_payload(row: DocRow) -> dict:
    s = S()
    progress = None
    if row.status == "processing":
        p = s.runner.progress(row.id) or s.storage.read_json(row.id, "progress.json")
        if p:
            progress = {"done": int(p.get("done", 0)), "total": int(p.get("total", row.page_count)),
                        "stage": p.get("stage", "parsing")}
    return {
        "id": row.id,
        "status": row.status,
        "parser": row.parser,
        "filename": row.filename,
        "pageCount": row.page_count,
        "progress": progress,
        "queuePosition": s.db.queue_position(row.id, row.created_at) if row.status == "queued" else None,
        "error": {"code": row.error_code, "message": row.error_message} if row.status == "failed" else None,
        "createdAt": iso(row.created_at),
        "expiresAt": iso(row.expires_at),
        "revision": row.revision,
        "sample": row.sample_key is not None,
        "sourcePageCount": row.source_page_count,
        "notices": [n for n in (row.notice or "").split(",") if n],
        "sourceFormat": row.source_format,
    }


def safe_filename(raw: str | None) -> str:
    name = unicodedata.normalize("NFC", unquote(raw or ""))
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip(" .")
    return name[:180] or "document"


def reserve_capacity(key: str, doc_id: str, *, sample: bool = False, network: str | None = None) -> None:
    s = S()
    reason = s.db.reserve(doc_id, key, sample=sample, pending_limit=s.settings.max_queued_jobs,
                          client_limit=s.settings.max_sample_documents_per_client if sample else s.settings.max_active_documents_per_client,
                          stored_limit=s.settings.max_stored_documents,
                          sample_stored_limit=min(s.settings.max_stored_sample_documents, s.settings.max_stored_documents // 2),
                          upload_limit=s.settings.max_uploads_in_progress,
                          client_upload_limit=s.settings.max_uploads_in_progress_per_client,
                          network_key=network,
                          network_upload_limit=s.settings.max_uploads_in_progress_per_network)
    if reason:
        raise error(429 if reason == "too_many_documents" else 503, reason,
                    "Document capacity reached. Delete a document or try again later.")


def insert_document(row: DocRow) -> None:
    try:
        S().db.insert(row, pending_limit=S().settings.max_queued_jobs)
    except QueueFull:
        S().storage.remove(row.id)
        S().db.release(row.id)
        raise error(503, "busy", "The parser queue is full. Please try again later.") from None


def new_document(*, key: str, filename: str, file_hash: str, page_count: int, parser: str,
                 sample_key: str | None, status: str, doc_id: str | None = None,
                 source_page_count: int | None = None) -> tuple[DocRow, str]:
    s = S()
    doc_id = doc_id or secrets.token_urlsafe(18)
    token = secrets.token_urlsafe(32)
    now = time.time()
    row = DocRow(
        id=doc_id, token_hash=hashlib.sha256(token.encode()).hexdigest(), filename=filename, file_hash=file_hash,
        page_count=page_count, parser=parser, status=status, error_code=None, error_message=None, client_key=key,
        sample_key=sample_key, created_at=now, expires_at=now + s.settings.document_ttl_seconds,
        started_at=None, finished_at=now if status == "completed" else None, revision=0,
        source_page_count=source_page_count,
    )
    return row, token


def write_job(row: DocRow) -> None:
    job = {"id": row.id, "filename": row.filename, "fileHash": row.file_hash, "pageCount": row.page_count}
    if row.source_page_count:
        job["sourcePageCount"] = row.source_page_count
    if row.source_format:
        job["sourceFormat"] = row.source_format
    S().storage.write_json(row.id, "job.json", job)


def create_document_from_sample(sample_key: str, parser: str, key: str, doc_id: str | None = None) -> tuple[DocRow, str]:
    s = S()
    sample = s.samples[sample_key]
    if "built" in sample:
        return _open_built_sample(sample, key, doc_id)
    filename = sample["file"]
    cached = s.storage.load_sample_cache(sample_key, parser, sample["fileHash"])
    page_count = cached["document"]["pageCount"] if cached else _inspect_sync(sample["path"])
    row, token = new_document(key=key, filename=filename, file_hash=sample["fileHash"], page_count=page_count,
                              parser=parser, sample_key=sample_key, status="completed" if cached else "queued", doc_id=doc_id)
    doc_dir = s.storage.doc_dir(row.id)
    doc_dir.mkdir()
    shutil.copyfile(sample["path"], doc_dir / "source.pdf")
    write_job(row)
    if cached:
        cached["document"]["id"] = row.id
        cached["document"]["filename"] = filename
        s.storage.write_json(row.id, "parsed.json", cached)
    insert_document(row)
    if not cached:
        s.runner.wake()
    return row, token


def _open_built_sample(sample: dict, key: str, doc_id: str | None = None) -> tuple[DocRow, str]:
    s = S()
    meta = sample["built"]
    parsed = json.loads((sample["path"].parent / "parsed.json").read_text(encoding="utf-8"))
    filename = parsed["document"]["filename"]
    row, token = new_document(key=key, filename=filename, file_hash=sample["fileHash"],
                              page_count=parsed["document"]["pageCount"], parser=meta["parser"],
                              sample_key=sample["key"], status="completed", source_page_count=meta.get("sourcePageCount"), doc_id=doc_id)
    row.source_format = meta.get("sourceFormat")
    row.notice = ",".join(meta.get("notices") or []) or None
    doc_dir = s.storage.doc_dir(row.id)
    doc_dir.mkdir()
    shutil.copyfile(sample["path"], doc_dir / "source.pdf")
    write_job(row)
    parsed["document"]["id"] = row.id
    s.storage.write_json(row.id, "parsed.json", parsed)
    insert_document(row)
    return row, token


def _inspect_sync(path: Path) -> int:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        return len(pdf)
    finally:
        pdf.close()


@asynccontextmanager
async def inspection_slot():
    slots = S().inspections
    try:
        await asyncio.wait_for(slots.acquire(), timeout=30)
    except TimeoutError:
        raise error(503, "busy", "Document inspection is busy. Try again later.")
    try:
        yield
    finally:
        slots.release()


async def detect_upload(path: Path, filename: str) -> Detected:
    async with inspection_slot():
        with tempfile.TemporaryDirectory(prefix="detect-", dir=S().storage.jobs) as folder:
            source = Path(folder) / "input"
            await asyncio.to_thread(safeio.copy_file, path, source, S().settings.max_upload_mb * 1024**2)
            result = await run_inspection("app.inspect_format", [str(source), Path(filename).suffix], source.parent)
    if "format" not in result:
        return Detected(None, CORRUPT)
    fmt = result["format"]
    if fmt is not None and (not isinstance(fmt, str) or fmt not in ALL):
        return Detected(None, CORRUPT)
    reason = result.get("error")
    if reason is None:
        return Detected(fmt)
    # The helper handles untrusted bytes. Only publish API-owned error text.
    if isinstance(reason, list) and len(reason) == 2:
        if reason[0] == "encrypted_file":
            return Detected(fmt, ENCRYPTED)
        if reason[0] == "too_large":
            return Detected(None, DIRECTORY_LIMIT if reason == list(DIRECTORY_LIMIT) else
                            ("too_large", "The file expands to more data than this server accepts."))
    return Detected(None, CORRUPT)


async def inspect_upload(path: Path, keep_pages: int | None = None, keep_pages_without_text: int | None = None) -> dict:
    async with inspection_slot():
        return await _inspect_upload(path, keep_pages, keep_pages_without_text)


async def _inspect_upload(path: Path, keep_pages: int | None = None, keep_pages_without_text: int | None = None) -> dict:
    """Inspection, like parsing, writes to a private workspace rather than the API directory."""
    with tempfile.TemporaryDirectory(prefix="inspect-", dir=S().storage.jobs) as folder:
        source = Path(folder) / "source.pdf"
        try:
            await asyncio.to_thread(safeio.copy_file, path, source)
            info = await _inspect_pdf_workspace(source, keep_pages, keep_pages_without_text)
            if info.get("ok"):
                await asyncio.to_thread(safeio.copy_file, source, path)
            return info
        except OSError:
            return {"ok": False, "code": "corrupt_pdf", "message": "The PDF could not be read safely."}


async def _inspect_pdf_workspace(path: Path, keep_pages: int | None, keep_pages_without_text: int | None) -> dict:
    args = [str(path), str(keep_pages) if keep_pages else "-"]
    if keep_pages_without_text:
        args.append(str(keep_pages_without_text))
    result = await run_inspection("app.inspect_pdf", args, path.parent)
    return result or {"ok": False, "code": "corrupt_pdf", "message": "The PDF could not be read safely."}


async def run_inspection(module: str, args: list[str], directory: Path) -> dict:
    s = S()
    cmd = python_command(s.settings, module, args, writable=directory)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=8192,
        env=child_env(s.settings), cwd=str(s.storage.api_root), start_new_session=True,
    )
    try:
        # Both inspectors emit one short JSON record. Bound the pipe even if a
        # helper misbehaves, and reap it on disconnect, timeout or excess output.
        async with asyncio.timeout(30):
            line = await proc.stdout.readline()
            if len(line) > 8192 or await proc.stdout.read(1):
                return {}
            if await proc.wait() != 0:
                return {}
            result = json.loads(line)
            return result if isinstance(result, dict) else {}
    except (TimeoutError, ValueError):
        return {}
    finally:
        s.runner._kill(proc)
        # Drain buffered bytes after termination so a full pipe cannot hold wait().
        while await proc.stdout.read(8192):
            pass
        await proc.wait()


# --- routes ----------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


def vlm_unlocked(request: Request, key: str, password: str | None = None) -> bool:
    """True when the request carries the picture-reading password (header X-VLM-Password)."""
    s = S()
    given = password if password is not None else request.headers.get("x-vlm-password")
    if not given:
        return False
    if not s.settings.vlm_enabled:
        raise error(422, "vlm_unavailable", "Picture reading is not available on this server.")
    if s.vlm_failures.full(key):
        raise error(429, "vlm_locked", "Too many wrong passwords. Please try again later.",
                    retryAfter=s.vlm_failures.retry_after(key))
    if hmac.compare_digest(given.encode(), s.settings.vlm_password.get_secret_value().encode()):
        return True
    s.vlm_failures.allow(key)
    raise error(403, "vlm_password", "The password for picture reading is wrong.")


class VlmUnlock(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


@app.post("/api/vlm/unlock")
async def vlm_unlock(request: Request) -> dict:
    """Lets the web app check the password before an upload."""
    try:
        body = VlmUnlock.model_validate_json(await limited_body(request, 8192))
    except ValidationError:
        raise error(422, "invalid_request", "A password is required.") from None
    vlm_unlocked(request, S().client_key(request), body.password or "")
    return {"ok": True, "model": S().settings.vlm_model}


@app.get("/api/config")
def config() -> dict:
    s = S()
    parsers = [
        {"id": "hybrid", "label": "Docling + PyMuPDF", "maxPages": s.settings.max_pages,
         "version": f"{s.parser_versions['docling']} / {s.parser_versions['pymupdf']}",
         "description": "Docling layout with the exact PDF text layer: heading levels, character positions "
                        "for splitting inside paragraphs, and recovery of text Docling missed. About 5 s per page."},
        {"id": "docling", "label": "Docling", "maxPages": s.settings.max_pages, "version": s.parser_versions["docling"],
         "description": "Layout model only: headings, lists, tables and reading order. About 5 s per page on CPU."},
        {"id": "pymupdf", "label": "PyMuPDF", "maxPages": s.settings.max_pages, "version": s.parser_versions["pymupdf"],
         "description": "Fast text-layer extraction with heuristic structure. Under a second for most files."},
    ]
    if s.ocr_available():
        parsers.append({"id": "docling-ocr", "label": "Docling + OCR (CPU)", "maxPages": s.settings.max_ocr_pages,
                        "version": s.parser_versions["docling"],
                        "description": "For scanned PDFs without a text layer. RapidOCR on CPU; slow."})
    return {
        "version": __version__,
        "defaultParser": s.settings.default_parser,
        "parsers": parsers,
        "limits": {
            "maxUploadMb": s.settings.max_upload_mb,
            "maxPages": s.settings.max_pages,
            "truncateLongPdfs": s.settings.truncate_long_pdfs,
            "documentTtlSeconds": s.settings.document_ttl_seconds,
        },
        "repoUrl": s.settings.repo_url,
        "formats": sorted(capabilities.accepted_formats(s.settings)),
        "extensions": sorted({e for f in capabilities.accepted_formats(s.settings) for e in EXTENSIONS[f]}),
        "vlm": {"available": s.settings.vlm_enabled, "model": s.settings.vlm_model},
    }


@app.get("/api/samples")
def samples() -> list[dict]:
    return [{**{k: v for k, v in sample.items() if k in ("key", "title", "language", "description", "group")},
             "format": (sample.get("built") or {}).get("sourceFormat") or "pdf"}
            for sample in S().samples.values()]


@app.post("/api/samples/{sample_key}/documents", status_code=201)
async def create_from_sample(sample_key: str, request: Request, parser: str | None = None) -> dict:
    # Keep file copies, JSON work and fsync outside the ASGI event loop. The
    # worker owns reservation cleanup even if the waiting request is cancelled.
    return await asyncio.to_thread(_create_from_sample, sample_key, request, parser)


def _create_from_sample(sample_key: str, request: Request, parser: str | None) -> dict:
    s = S()
    parser = parser or s.settings.default_parser
    if sample_key not in s.samples:
        raise error(404, "not_found", "Unknown sample.")
    if not s.parser_allowed(parser):
        raise error(422, "unknown_parser", "This parser is not available.")
    key = s.client_key(request)
    if not s.sample_creations.allow(key):
        raise error(429, "sample_rate_limited", "Sample limit reached. Please try again later.",
                    retryAfter=s.sample_creations.retry_after(key))
    # Capacity reservation and final queue admission are transactional in SQLite.
    doc_id = secrets.token_urlsafe(18)
    reserve_capacity(key, doc_id, sample=True)
    try:
        row, token = create_document_from_sample(sample_key, parser, key, doc_id)
    except BaseException:
        s.storage.remove(doc_id)
        raise
    finally:
        s.db.release(doc_id)
    return {**status_payload(row), "accessToken": token}


@app.post("/api/documents", status_code=201)
async def upload(request: Request, parser: str | None = None) -> dict:
    s = S()
    parser = parser or s.settings.default_parser
    if not s.parser_allowed(parser):
        raise error(422, "unknown_parser", "This parser is not available.")
    key = s.client_key(request)
    limit = s.settings.max_upload_mb * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise error(413, "too_large", f"The file is larger than {s.settings.max_upload_mb} MB.")
    minimum_rate = s.settings.upload_min_bytes_per_second
    if declared and declared.isdigit():
        # A large declared body must make enough progress to finish within the
        # same total deadline. A tiny trickle cannot retain its slot for 120s.
        transfer_seconds = max(1., s.settings.upload_timeout_seconds - s.settings.upload_grace_seconds)
        minimum_rate = max(minimum_rate, int(int(declared) / transfer_seconds) + 1)
    vlm = vlm_unlocked(request, key)
    if s.uploads.full(key):
        raise error(429, "upload_rate_limited", "Upload limit reached. Please try again later.",
                    retryAfter=s.uploads.retry_after(key))

    # Each upload gets its own directory from the start, so sandboxed tools can write only there.
    doc_id = secrets.token_urlsafe(18)
    reserve_capacity(key, doc_id, network=s.network_key(request))
    # A capacity refusal must not consume the user's hourly allowance.
    if not s.uploads.allow(key):
        s.db.release(doc_id)
        raise error(429, 'upload_rate_limited', 'Upload limit reached. Please try again later.',
                    retryAfter=s.uploads.retry_after(key))
    doc_dir = s.storage.doc_dir(doc_id)
    received = doc_dir / "upload.bin"
    filename = safe_filename(request.headers.get("x-filename"))
    digest = hashlib.sha256()
    size = 0
    try:
        doc_dir.mkdir()
        if vlm:
            (doc_dir / "vlm.json").write_text(json.dumps({"model": s.settings.vlm_model}))
        with open(received, "wb") as f:
            async with asyncio.timeout(s.settings.upload_timeout_seconds):
                async for chunk in idle_limited_stream(request, s.settings.upload_idle_seconds,
                        minimum_rate=minimum_rate, grace_seconds=s.settings.upload_grace_seconds):
                    size += len(chunk)
                    if size > limit:
                        raise error(413, "too_large", f"The file is larger than {s.settings.max_upload_mb} MB.")
                    digest.update(chunk)
                    f.write(chunk)
        if size == 0:
            raise error(400, "empty", "The file is empty.")
        found = await detect_upload(received, filename)
        if found.error:
            raise error(422, *found.error)
        accepted = capabilities.accepted_formats(s.settings)
        if found.format not in accepted:
            names = ", ".join(sorted({e for f in accepted for e in EXTENSIONS[f]}))
            raise error(415, "unsupported_format", f"This file type is not supported. Supported: {names}.")
        if found.format != "pdf":
            # Converted to PDF in the background job, then inspected like any upload.
            received.rename(doc_dir / f"original{EXTENSIONS[found.format][0]}")
            row, token = new_document(key=key, filename=filename, file_hash=digest.hexdigest(), page_count=0,
                                      parser=parser, sample_key=None, status="queued", doc_id=doc_id)
            row.source_format = found.format
            write_job(row)
            insert_document(row)
            s.runner.wake()
            return {**status_payload(row), "accessToken": token}

        source = doc_dir / "source.pdf"
        received.rename(source)
        info = await inspect_upload(source, *capabilities.inspect_limits(s.settings, parser))
        if not info.get("ok"):
            raise error(422, info.get("code", "corrupt_pdf"), info.get("message", "The PDF is damaged or unreadable."))
        parser, notice = capabilities.route_by_text_layer(s.settings, parser, info)
        pages = int(info["pageCount"])
        if pages > s.page_limit(parser):
            what = "scanned PDFs are read with OCR, which" if notice == "auto_ocr" else "this parser"
            raise error(422, "too_many_pages", f"The PDF has {pages} pages; {what} accepts up to {s.page_limit(parser)}.")

        row, token = new_document(key=key, filename=filename, file_hash=digest.hexdigest(), page_count=pages,
                                  parser=parser, sample_key=None, status="queued", doc_id=doc_id,
                                  source_page_count=info.get("sourcePageCount"))
        row.notice = notice
        write_job(row)
        insert_document(row)
        s.runner.wake()
        return {**status_payload(row), "accessToken": token}
    except TimeoutError:
        shutil.rmtree(doc_dir, ignore_errors=True)
        raise error(408, "upload_timeout", "The upload took too long.")
    except BaseException:
        shutil.rmtree(doc_dir, ignore_errors=True)
        raise
    finally:
        s.db.release(doc_id)


@app.get("/api/documents/{doc_id}/status")
def document_status(doc_id: str, request: Request) -> dict:
    return status_payload(authorize(doc_id, request))


@app.get("/api/documents/{doc_id}")
def document(doc_id: str, request: Request) -> dict:
    row = authorize(doc_id, request)
    s = S()
    parsed = s.storage.read_json(doc_id, "parsed.json") if row.status == "completed" else None
    plan = s.storage.read_json(doc_id, "split-plan.json") if parsed else None
    return {**status_payload(row), "parsed": wire_document(parsed), "splitPlan": plan}


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: str, request: Request) -> Response:
    authorize(doc_id, request)
    return safeio.ArtifactResponse(S().storage.doc_dir(doc_id) / "source.pdf", media_type="application/pdf")


SPLITTABLE = {"paragraph", "list_item", "caption", "footnote", "table"}


def table_row_starts(text: str) -> set[int]:
    """Where a Markdown table may be cut: the starts of its rows from the second data row on."""
    lines = text.split("\n")
    if len(lines) < 4 or not re.match(r"\|\s*:?-{3}", lines[1]):
        return set()
    starts, at = set(), 0
    for k, line in enumerate(lines[:-1]):
        at += text_length(line) + 1
        if k + 1 >= 3:
            starts.add(at)
    return starts


def boundaries_valid(blocks: list[dict], boundaries: list) -> bool:
    """Validate positions, not the presentation order of blocks.

    Core owns chunk ordering (including moving page notes after the body). Within
    each block, cuts must still be unique and increasing, regardless of that order.
    """
    by_id = {b["id"]: b for b in blocks}
    last: dict[str, float] = {}
    offsets: dict[str, tuple[set[int], int]] = {}
    table_cuts: dict[str, set[int]] = {}
    for boundary in boundaries:
        block = by_id.get(boundary.block_id)
        if block is None:
            return False
        if boundary.offset is not None:
            # Long blocks can have many cuts; build their UTF-16 index only once.
            if boundary.block_id not in offsets:
                positions = text_positions(block["text"])
                offsets[boundary.block_id] = (set(positions), positions[-1])
            valid, end = offsets[boundary.block_id]
            if not (0 < boundary.offset < end) or boundary.offset not in valid or not block.get("lines") or block["type"] not in SPLITTABLE:
                return False
            if block["type"] == "table":
                if boundary.block_id not in table_cuts:
                    table_cuts[boundary.block_id] = table_row_starts(block["text"])
                if boundary.offset not in table_cuts[boundary.block_id]:
                    return False
        pos = boundary.offset if boundary.offset is not None else float("inf")
        if pos <= last.get(boundary.block_id, -1):
            return False
        last[boundary.block_id] = pos
    return True


def excluded_valid(blocks: list[dict], excluded: list) -> bool:
    """Deleted text lies inside known blocks: an empty range only for a block without text."""
    positions = {b["id"]: set(text_positions(b["text"])) for b in blocks}
    for r in excluded:
        valid = positions.get(r.block_id)
        if valid is None or r.start not in valid or r.end not in valid or r.start > r.end or (r.start == r.end and len(valid) > 1):
            return False
    return True


async def idle_limited_stream(request: Request, idle_seconds: float, *, minimum_rate: int = 0, grace_seconds: float = 0):
    stream = request.stream().__aiter__()
    started, received = time.monotonic(), 0
    while True:
        try:
            timeout = idle_seconds
            if minimum_rate:
                remaining = started + grace_seconds + received / minimum_rate - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                timeout = min(timeout, remaining)
            chunk = await asyncio.wait_for(anext(stream), timeout=timeout)
            received += len(chunk)
            yield chunk
        except StopAsyncIteration:
            return


async def limited_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise error(413, "too_large", "Request body is too large.")
    chunks, size = [], 0
    try:
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise error(413, "too_large", "Request body is too large.")
                chunks.append(chunk)
    except TimeoutError:
        raise error(408, "request_timeout", "The request body took too long to arrive.") from None
    return b"".join(chunks)


@app.put("/api/documents/{doc_id}/split-plan")
async def save_split_plan(doc_id: str, request: Request) -> dict:
    row = authorize(doc_id, request)
    s = S()
    body = await limited_body(request, 4 * 1024 * 1024)
    try:
        payload = SaveSplitPlan.model_validate_json(body)
    except ValidationError as exc:
        raise error(422, "invalid_plan", "The split plan is malformed.",
                    errors=json.loads(exc.json(include_url=False, include_input=False))) from exc
    parsed = s.storage.read_json(doc_id, "parsed.json") if row.status == "completed" else None
    if parsed is None:
        raise error(409, "not_ready", "The document has not been parsed yet.")
    plan = payload.plan
    if plan.document_id != doc_id or plan.blocks_fingerprint != parsed["blocksFingerprint"]:
        raise error(422, "plan_mismatch", "This split plan belongs to a different parse of the document.")
    if not boundaries_valid(parsed["blocks"], plan.boundaries):
        raise error(422, "invalid_plan", "Boundaries must be valid, unique positions, increasing within each block.")
    if not excluded_valid(parsed["blocks"], plan.excluded):
        raise error(422, "invalid_plan", "Deleted text must lie inside the document's blocks.")

    new_revision = s.db.bump_revision(doc_id, payload.base_revision)
    if new_revision is None:
        current = s.db.get(doc_id)
        raise error(409, "stale_revision", "The document was changed elsewhere.",
                    revision=current.revision if current else 0,
                    splitPlan=s.storage.read_json(doc_id, "split-plan.json"))
    data = {**plan.dump(), "revision": new_revision}
    s.storage.write_json(doc_id, "split-plan.json", data)
    return {"revision": new_revision}


@app.post("/api/documents/{doc_id}/parse")
async def reparse(doc_id: str, request: Request, parser: str) -> dict:
    """Parse the same PDF again with another parser. The saved split plan is discarded."""
    row = authorize(doc_id, request)
    s = S()
    if not s.parser_allowed(parser):
        raise error(422, "unknown_parser", "This parser is not available.")
    if row.status in ("queued", "processing"):
        raise error(409, "busy", "The document is still being parsed.")
    if row.page_count > s.page_limit(parser):
        raise error(422, "too_many_pages", f"This parser accepts up to {s.page_limit(parser)} pages.")
    key = s.client_key(request)
    if s.db.count_pending() >= s.settings.max_queued_jobs:
        raise error(503, "busy", "The parser queue is full. Please try again in a few minutes.")
    if not s.uploads.allow(key):
        raise error(429, "upload_rate_limited", "Parse limit reached. Please try again later.")
    for name in ("parsed.json", "split-plan.json", "error.json", "progress.json"):
        safeio.remove(s.storage.doc_dir(doc_id) / name)
    safeio.remove(s.storage.doc_dir(doc_id) / "figures")
    cached = s.storage.load_sample_cache(row.sample_key, parser, row.file_hash) if row.sample_key else None
    if cached:
        cached["document"]["id"] = doc_id
        s.storage.write_json(doc_id, "parsed.json", cached)
    s.db.update(doc_id, parser=parser, status="completed" if cached else "queued", error_code=None,
                error_message=None, created_at=time.time(), started_at=None, finished_at=None, interrupted_retries=0)
    if not cached:
        s.runner.wake()
    return status_payload(s.db.get(doc_id))


@app.delete("/api/documents/{doc_id}", status_code=204)
async def delete_document(doc_id: str, request: Request) -> Response:
    authorize(doc_id, request)
    s = S()
    s.db.delete(doc_id)
    s.runner.cancel(doc_id)
    s.storage.remove(doc_id)
    return Response(status_code=204)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def api_not_found(path: str) -> None:
    raise error(404, "not_found", "Unknown API route.")


# --- web app -------------------------------------------------------------------------------

if _settings.web_dist:
    dist = _settings.web_dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def web(path: str) -> Response:
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist):
            immutable = path.startswith("assets/")
            return FileResponse(candidate, headers={
                "Cache-Control": "public, max-age=31536000, immutable" if immutable else "public, max-age=3600"})
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})
