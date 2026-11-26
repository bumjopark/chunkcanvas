"""SQLite metadata store. One process owns the database; a lock serializes access."""
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

MAX_INTERRUPTED_RETRIES = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    parser TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    client_key TEXT NOT NULL,
    sample_key TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    revision INTEGER NOT NULL DEFAULT 0,
    source_page_count INTEGER,
    notice TEXT,
    source_format TEXT,
    interrupted_retries INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS documents_status ON documents (status, created_at);
CREATE INDEX IF NOT EXISTS documents_expiry ON documents (expires_at);
CREATE TABLE IF NOT EXISTS admissions (
    id TEXT PRIMARY KEY, client_key TEXT NOT NULL, sample INTEGER NOT NULL, expires_at REAL NOT NULL,
    network_key TEXT
);
"""


@dataclass
class DocRow:
    id: str
    token_hash: str
    filename: str
    file_hash: str
    page_count: int
    parser: str
    status: str
    error_code: str | None
    error_message: str | None
    client_key: str
    sample_key: str | None
    created_at: float
    expires_at: float
    started_at: float | None
    finished_at: float | None
    revision: int
    # Pages in the uploaded file when only the first page_count pages were kept.
    source_page_count: int | None = None
    # Comma-separated notes on how the document was processed (e.g. "auto_ocr,text_fallback").
    notice: str | None = None
    # Format of the uploaded file when it was converted to PDF (e.g. "docx"); None for PDFs.
    source_format: str | None = None
    interrupted_retries: int = 0


class QueueFull(Exception):
    pass


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(documents)")}
            for column in ("source_page_count INTEGER", "notice TEXT", "source_format TEXT",
                           "interrupted_retries INTEGER NOT NULL DEFAULT 0"):
                if column.split()[0] not in columns:
                    self._conn.execute(f"ALTER TABLE documents ADD COLUMN {column}")
            if "network_key" not in {r[1] for r in self._conn.execute("PRAGMA table_info(admissions)")}:
                self._conn.execute("ALTER TABLE admissions ADD COLUMN network_key TEXT")

    def _row(self, row: sqlite3.Row | None) -> DocRow | None:
        return DocRow(**dict(row)) if row else None

    def insert(self, row: DocRow, *, pending_limit: int | None = None) -> None:
        values = row.__dict__
        cols = ", ".join(values)
        marks = ", ".join(f":{k}" for k in values)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if pending_limit is not None and row.status in ("queued", "processing"):
                    pending = self._conn.execute(
                        "SELECT COUNT(*) FROM documents WHERE status IN ('queued','processing') AND expires_at > ?",
                        (time.time(),)).fetchone()[0]
                    if pending >= pending_limit:
                        raise QueueFull()
                self._conn.execute(f"INSERT INTO documents ({cols}) VALUES ({marks})", values)
                self._conn.execute("DELETE FROM admissions WHERE id = ?", (row.id,))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def reserve(self, doc_id: str, client_key: str, *, sample: bool, pending_limit: int,
                client_limit: int, stored_limit: int, upload_limit: int = 32,
                client_upload_limit: int = 2, sample_stored_limit: int | None = None,
                network_key: str | None = None, network_upload_limit: int | None = None) -> str | None:
        """Reserve capacity before request I/O. No transaction spans an upload or parse."""
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM admissions WHERE expires_at <= ?", (now,))
                count = lambda sql, args=(): self._conn.execute(sql, args).fetchone()[0]
                reserved = count("SELECT COUNT(*) FROM admissions")
                pending = count("SELECT COUNT(*) FROM documents WHERE status IN ('queued','processing') AND expires_at > ?", (now,))
                stored = count("SELECT COUNT(*) FROM documents WHERE expires_at > ?", (now,))
                active = count("SELECT COUNT(*) FROM documents WHERE client_key = ? AND (sample_key IS NOT NULL) = ? AND expires_at > ?",
                               (client_key, int(sample), now))
                active += count("SELECT COUNT(*) FROM admissions WHERE client_key = ? AND sample = ?", (client_key, int(sample)))
                uploads = count("SELECT COUNT(*) FROM admissions WHERE sample = 0")
                own_uploads = count("SELECT COUNT(*) FROM admissions WHERE sample = 0 AND client_key = ?", (client_key,))
                network_uploads = (count("SELECT COUNT(*) FROM admissions WHERE sample = 0 AND network_key = ?", (network_key,))
                                   if network_key and network_upload_limit else 0)
                samples = count('SELECT COUNT(*) FROM documents WHERE sample_key IS NOT NULL AND expires_at > ?', (now,))
                samples += count('SELECT COUNT(*) FROM admissions WHERE sample = 1')
                reason = ("storage_full" if stored + reserved >= stored_limit
                          else 'sample_storage_full' if sample and sample_stored_limit is not None and samples >= sample_stored_limit
                          else "too_many_documents" if active >= client_limit
                          else "upload_busy" if not sample and (uploads >= upload_limit or own_uploads >= client_upload_limit
                                                                or (network_upload_limit is not None and network_key is not None
                                                                    and network_uploads >= network_upload_limit))
                          else "busy" if not sample and pending >= pending_limit else None)
                if reason is None:
                    self._conn.execute("INSERT INTO admissions (id, client_key, sample, expires_at, network_key) VALUES (?, ?, ?, ?, ?)",
                                       (doc_id, client_key, int(sample), now + 600, network_key))
                self._conn.execute("COMMIT")
                return reason
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def release(self, doc_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM admissions WHERE id = ?", (doc_id,))

    def get(self, doc_id: str) -> DocRow | None:
        with self._lock:
            return self._row(self._conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone())

    def update(self, doc_id: str, **fields) -> None:
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE documents SET {sets} WHERE id = :id", {**fields, "id": doc_id})

    def bump_revision(self, doc_id: str, base_revision: int) -> int | None:
        """Compare-and-set: returns the new revision, or None if `base_revision` is stale."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE documents SET revision = revision + 1 WHERE id = ? AND revision = ?",
                (doc_id, base_revision),
            )
            if cur.rowcount != 1:
                return None
            return self._conn.execute("SELECT revision FROM documents WHERE id = ?", (doc_id,)).fetchone()[0]

    def delete(self, doc_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))

    def claim_next_job(self) -> DocRow | None:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM documents WHERE status = 'queued' AND expires_at > ? ORDER BY created_at LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE documents SET status = 'processing', started_at = ? WHERE id = ?", (now, row["id"])
            )
            return self._row(row)

    def queue_position(self, doc_id: str, created_at: float) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM documents WHERE status = 'queued' AND created_at < ? AND id != ?",
                (created_at, doc_id),
            ).fetchone()[0]

    def count_pending(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM documents WHERE status IN ('queued', 'processing') AND expires_at > ?",
                (time.time(),)
            ).fetchone()[0]

    def count_active_for_client(self, client_key: str) -> int:
        """Uploaded documents kept for this client. Sample copies are cheap and don't count."""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM documents WHERE client_key = ? AND sample_key IS NULL AND expires_at > ?",
                (client_key, time.time()),
            ).fetchone()[0]

    def expired(self, now: float) -> list[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT id FROM documents WHERE expires_at <= ?", (now,))]

    def all_ids(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._conn.execute("SELECT id FROM documents UNION SELECT id FROM admissions WHERE expires_at > ?", (time.time(),))}

    def requeue_interrupted(self) -> int:
        """Recover an in-flight job twice; a third interruption ends its automatic attempts."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM admissions")
                self._conn.execute(
                    "UPDATE documents SET status = 'failed', finished_at = ?, error_code = 'processing_interrupted', "
                    "error_message = 'The server stopped repeatedly while processing this document. Automatic retries have ended.' "
                    "WHERE status = 'processing' AND interrupted_retries >= ?",
                    (time.time(), MAX_INTERRUPTED_RETRIES),
                )
                requeued = self._conn.execute(
                    "UPDATE documents SET status = 'queued', started_at = NULL, interrupted_retries = interrupted_retries + 1 "
                    "WHERE status = 'processing'"
                ).rowcount
                self._conn.execute("COMMIT")
                return requeued
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
