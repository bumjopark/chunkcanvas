"""Background parsing: worker threads claim queued documents and run each parse in its own
subprocess with a timeout, so a slow or crashing parser never blocks or kills the API."""
import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import capabilities, safeio
from .config import Settings
from .formats import REQUIRES_ISOLATION, FIXED, HANGUL, MARKUP, OFFICE
from .db import Database
from .sandbox import child_env, python_command
from .storage import Storage
from .models import ParsedDocument
from .vlm import describe_figures

log = logging.getLogger("chunkcanvas.jobs")
# A direct child reports a negative signal; a sandbox launcher can report 128 + signal.
KILLED_EXITS = {-signal.SIGKILL, 128 + signal.SIGKILL}


class JobRunner:
    def __init__(self, settings: Settings, db: Database, storage: Storage):
        self.settings = settings
        self.db = db
        self.storage = storage
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._procs: dict[str, subprocess.Popen] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._workspaces: dict[str, Path] = {}
        self._procs_lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self.storage.clear_interrupted_workspaces()
        requeued = self.db.requeue_interrupted()
        if requeued:
            log.info("requeued %d interrupted jobs", requeued)
        for i in range(max(1, self.settings.worker_concurrency)):
            t = threading.Thread(target=self._loop, name=f"parse-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._procs_lock:
            for event in self._cancel_events.values():
                event.set()
            for proc in self._procs.values():
                self._kill(proc)

    def wake(self) -> None:
        self._wake.set()

    def progress(self, doc_id: str) -> dict | None:
        with self._procs_lock:
            work = self._workspaces.get(doc_id)
        try:
            value = safeio.read_json(work / "progress.json", 4096) if work else None
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def cancel(self, doc_id: str) -> None:
        with self._procs_lock:
            proc = self._procs.get(doc_id)
            event = self._cancel_events.get(doc_id)
            if event:
                event.set()
        if proc:
            self._kill(proc)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            # The parser runs in its own session; kill the whole group (bwrap and children).
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            row = self.db.claim_next_job()
            if row is None:
                self._wake.wait(timeout=2)
                self._wake.clear()
                continue
            try:
                self._run(row.id, row.parser, row.sample_key, row.file_hash)
            except Exception:  # noqa: BLE001
                log.exception("job runner error")
                self._fail(row.id, "internal_error", "Parsing failed unexpectedly.")
            finally:
                with self._procs_lock:
                    self._cancel_events.pop(row.id, None)

    def _subprocess(self, doc_id: str, module: str, args: list[str], doc_dir: Path, timeout: int) -> tuple[int, bool, str]:
        """Run a sandboxed helper for this document; returns (exit code, timed out, stdout)."""
        cmd = python_command(self.settings, module, args, writable=doc_dir)
        with safeio.open_regular(self.storage.doc_dir(doc_id) / "job-stderr.log", append=True) as stderr:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr,
                env=child_env(self.settings), cwd=str(self.storage.api_root), start_new_session=True,
            )
            with self._procs_lock:
                self._procs[doc_id] = proc
                event = self._cancel_events.get(doc_id)
                if self._stop.is_set() or (event and event.is_set()):
                    self._kill(proc)
            try:
                out, _ = proc.communicate(timeout=timeout)
                return proc.returncode, False, out.decode("utf-8", "replace")
            except subprocess.TimeoutExpired:
                self._kill(proc)
                proc.wait()
                return -1, True, ""
            finally:
                self._kill(proc)  # A helper must not leave descendants modifying its output.
                with self._procs_lock:
                    self._procs.pop(doc_id, None)

    def _fail(self, doc_id: str, code: str, message: str) -> None:
        # A stop initiated by the service leaves the in-flight row for startup recovery.
        if not self._stop.is_set():
            self.db.update(doc_id, status="failed", error_code=code, error_message=message, finished_at=time.time())

    def _prepare(self, doc_id: str, doc_dir: Path, parser: str) -> str | None:
        """Convert a non-PDF upload, then inspect it like an uploaded PDF. Returns the parser to
        use, or None when the document failed (already recorded)."""
        job = safeio.read_json(doc_dir / "job.json")
        fmt = job.get("sourceFormat")
        # job.json is the parsed document's metadata; preparation state lives beside it.
        if not fmt or (doc_dir / "prepared.json").exists():
            return parser
        keep, keep_without_text = capabilities.inspect_limits(self.settings, parser)
        cap = max(keep or 0, keep_without_text or 0)
        if not (doc_dir / "source.pdf").exists():
            # A queued upload may predate a restart with isolation turned off.
            if self.settings.parser_sandbox == "none" and fmt in REQUIRES_ISOLATION:
                self._fail(doc_id, "unsupported_format", "This document requires an isolated converter.")
                return None
            # (After a restart the conversion may already be done; only inspection is repeated.)
            safeio.write_json(doc_dir / "progress.json", {"stage": "converting", "done": 0, "total": 0})
            soffice = self.settings.soffice_path
            args = ["--doc-dir", str(doc_dir), "--format", fmt, "--timeout", str(self.settings.convert_timeout_seconds)]
            if soffice:
                args += ["--soffice", str(soffice)]
            if cap:
                # One page more than kept: tells us whether the document is really longer.
                args += ["--max-pages", str(cap + 1)]
            code, timed_out, _ = self._subprocess(doc_id, "app.convert", args, doc_dir, self.settings.convert_timeout_seconds + 30)
            if timed_out:
                self._fail(doc_id, "timeout", "Converting the document took too long.")
                return None
            if code in KILLED_EXITS:
                self._fail(doc_id, "killed", "The converter was stopped, possibly because it ran out of memory.")
                return None
            if code != 0 or not (doc_dir / "source.pdf").exists():
                err = _read_error(doc_dir / "error.json")
                self._fail(doc_id, err["code"], err["message"])
                return None
        try:
            notices = safeio.read_json(doc_dir / "conversion.json").get("notices", [])
        except (OSError, ValueError):
            notices = []

        source = doc_dir / "source.pdf"
        inspect_args = [str(source), str(keep) if keep else "-"] + ([str(keep_without_text)] if keep_without_text else [])
        code, timed_out, stdout = self._subprocess(doc_id, "app.inspect_pdf", inspect_args, doc_dir, 60)
        try:
            info = json.loads(stdout or "{}") if code == 0 and not timed_out else {}
        except ValueError:
            info = {}
        if not info.get("ok"):
            self._fail(doc_id, info.get("code", "conversion_failed"), info.get("message", "The converted document could not be read."))
            return None
        parser, ocr_notice = capabilities.route_by_text_layer(self.settings, parser, info)
        notices += [ocr_notice] if ocr_notice else []
        pages = int(info["pageCount"])
        if pages > capabilities.page_limit(self.settings, parser):
            self._fail(doc_id, "too_many_pages",
                       f"The document has {pages} pages; this parser accepts up to {capabilities.page_limit(self.settings, parser)}.")
            return None
        source_pages = info.get("sourcePageCount")
        if cap and fmt in OFFICE | HANGUL | MARKUP | FIXED and (source_pages or pages) > cap:
            # LibreOffice stopped after cap+1 pages: longer than shown, real length unknown.
            source_pages = None
            notices.append("page_limit")
        job.update({"pageCount": pages, **({"sourcePageCount": source_pages} if source_pages else {})})
        safeio.write_json(doc_dir / "job.json", job)
        safeio.write_json(doc_dir / "prepared.json", {"parser": parser})
        self.db.update(doc_id, page_count=pages, source_page_count=source_pages, parser=parser,
                       notice=",".join(notices) or None)
        return parser

    def _run(self, doc_id: str, parser: str, sample_key: str | None, file_hash: str) -> None:
        with self._procs_lock:
            self._cancel_events.setdefault(doc_id, threading.Event())
        if not self.storage.doc_dir(doc_id).exists():
            return
        for name in ("parsed.json", "error.json", "progress.json", "parse-notices.json"):
            safeio.remove(self.storage.doc_dir(doc_id) / name)
        safeio.remove(self.storage.doc_dir(doc_id) / "figures")
        try:
            with self.storage.job_workspace(doc_id) as work:
                with self._procs_lock:
                    self._workspaces[doc_id] = work
                self._run_in_workspace(doc_id, parser, sample_key, file_hash, work)
        finally:
            with self._procs_lock:
                self._workspaces.pop(doc_id, None)

    def _run_in_workspace(self, doc_id: str, parser: str, sample_key: str | None,
                          file_hash: str, doc_dir: Path) -> None:
        with self._procs_lock:
            cancelled = self._cancel_events.setdefault(doc_id, threading.Event())
        parser = self._prepare(doc_id, doc_dir, parser)
        if parser is None or cancelled.is_set() or self.db.get(doc_id) is None:
            return
        self.storage.publish_prepared(doc_id, doc_dir)
        args = ["--doc-dir", str(doc_dir), "--parser", parser, "--models", str(self.settings.models_dir.resolve()),
                "--threads", str(self.settings.parser_threads), "--table-mode", self.settings.table_mode,
                "--formula-seconds", str(self.settings.formula_seconds)]
        # Older markers were written only after a valid password. Password-free jobs
        # (including reparses) may use the provider only while open access is enabled.
        consent = self.storage.read_json(doc_id, "vlm.json")
        vlm = (self.settings.vlm_enabled and consent is not None
               and (self.settings.vlm_open or consent.get("access") != "open"))
        if vlm:
            args += ["--figures", str(self.settings.vlm_max_figures)]
        row = self.db.get(doc_id)
        mixed = row is not None and "mixed_ocr" in (row.notice or "").split(",")
        ocr = parser == "hybrid" and capabilities.ocr_available(self.settings) and (self.settings.ocr_pictures or mixed)
        # Picture OCR that is only on by default must not turn a document that parses without it
        # into a failure: it gets part of the time, and the parse runs again without it if needed.
        optional = ocr and not mixed
        budget = self.settings.job_timeout_seconds
        started = time.monotonic()
        code, timed_out = self._parse_once(doc_id, doc_dir, args + (["--ocr"] if ocr else []),
                                           int(budget * 0.6) if optional else budget)
        remaining = int(budget - (time.monotonic() - started))
        if optional and remaining > 0 and not cancelled.is_set() and self.db.get(doc_id) is not None and self._resource_failure(code, timed_out, doc_dir):
            log.info("picture OCR did not finish (%s); parsing again without it", "timeout" if timed_out else "memory")
            (doc_dir / "error.json").unlink(missing_ok=True)
            code, timed_out = self._parse_once(doc_id, doc_dir, args, remaining)
            if code == 0:
                row = self.db.get(doc_id)
                notices = [n for n in ((row.notice if row else "") or "").split(",") if n] + ["ocr_skipped"]
                self.db.update(doc_id, notice=",".join(notices))

        elapsed = time.monotonic() - started
        if self.db.get(doc_id) is None:  # deleted or expired while parsing
            return
        parsed = doc_dir / "parsed.json"
        if code == 0 and parsed.exists():
            log.info("parsed document with %s in %.1fs", parser, elapsed)
            extra = doc_dir / "parse-notices.json"
            added = safeio.read_json(extra) if extra.exists() else []
            if vlm:
                try:
                    added += describe_figures(doc_dir, self.settings, cancelled)
                except Exception as exc:  # noqa: BLE001 - the parse stands without descriptions
                    log.warning("picture reading failed: %s", type(exc).__name__)
                    added.append("vlm_partial")
            if cancelled.is_set() or self.db.get(doc_id) is None:
                return
            result = ParsedDocument.model_validate(safeio.read_json(parsed))
            if result.document.id != doc_id or result.document.file_hash != file_hash:
                raise ValueError("parser returned mismatched document metadata")
            self.storage.publish_parse(doc_id, result.dump())
            if added:
                row = self.db.get(doc_id)
                notices = [n for n in ((row.notice if row else "") or "").split(",") if n]
                notices += [n for n in added if n not in notices]
                self.db.update(doc_id, notice=",".join(notices))
            if sample_key:
                self.storage.store_sample_cache(sample_key, parser, file_hash, self.storage.doc_dir(doc_id) / "parsed.json")
            self.db.update(doc_id, status="completed", finished_at=time.time())
            return

        if timed_out:
            err = {"code": "timeout", "message": f"Parsing took longer than {self.settings.job_timeout_seconds} seconds."}
        elif code in KILLED_EXITS:
            err = {"code": "killed", "message": "The parser was stopped, most likely because it ran out of memory."}
        else:
            err = _read_error(doc_dir / "error.json")
        log.info("parse failed with %s after %.1fs (exit %d): %s", parser, elapsed, code, err["code"])
        self._fail(doc_id, err["code"], err["message"])

    @staticmethod
    def _resource_failure(code: int, timed_out: bool, doc_dir: Path) -> bool:
        """Out of time or memory, as opposed to a document the parser cannot read."""
        if timed_out or code in KILLED_EXITS:
            return True
        return code != 0 and _read_error(doc_dir / "error.json")["code"] == "out_of_memory"

    def _parse_once(self, doc_id: str, doc_dir: Path, args: list[str], timeout: int) -> tuple[int, bool]:
        """Run the parse job; returns (exit code, timed out)."""
        cmd = python_command(self.settings, "app.parse_job", args, writable=doc_dir)
        with safeio.open_regular(self.storage.doc_dir(doc_id) / "job-stderr.log", append=True) as stderr:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
                env=child_env(self.settings), cwd=str(self.storage.api_root),
                start_new_session=True,
            )
            with self._procs_lock:
                self._procs[doc_id] = proc
                event = self._cancel_events.get(doc_id)
                if self._stop.is_set() or (event and event.is_set()):
                    self._kill(proc)
            try:
                return proc.wait(timeout=timeout), False
            except subprocess.TimeoutExpired:
                self._kill(proc)
                proc.wait()
                return -1, True
            finally:
                self._kill(proc)
                with self._procs_lock:
                    self._procs.pop(doc_id, None)


def _read_error(path: Path) -> dict:
    try:
        err = safeio.read_json(path, 4096)
        return {"code": str(err["code"])[:64], "message": str(err["message"])[:300]}
    except (OSError, ValueError, KeyError, TypeError):
        return {"code": "parse_failed", "message": "The parser failed on this PDF."}
