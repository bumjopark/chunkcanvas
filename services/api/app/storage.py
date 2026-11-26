"""Files on disk: one directory per uploaded document plus a cache of parsed samples."""
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .config import API_ROOT, Settings
from . import safeio

DOC_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SAMPLE_KEY = re.compile(r"^[a-z0-9-]{1,64}$")
# Bump whenever parser adapters change their output, so cached sample parses are rebuilt.
PARSE_OUTPUT_VERSION = 27


class Storage:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.api_root = API_ROOT
        self.root = settings.documents_dir.resolve()
        self.sample_cache = settings.sample_cache_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.sample_cache.mkdir(parents=True, exist_ok=True)
        self.jobs = settings.data_dir.resolve() / "jobs"
        self.jobs.mkdir(parents=True, exist_ok=True, mode=0o700)

    def doc_dir(self, doc_id: str) -> Path:
        # Ids are generated server-side, but the pattern check also blocks path tricks in URLs.
        if not DOC_ID.match(doc_id):
            raise ValueError("invalid document id")
        return self.root / doc_id

    def remove(self, doc_id: str) -> None:
        safeio.remove(self.doc_dir(doc_id))

    def orphan_dirs(self, known: set[str]) -> list[str]:
        return [p.name for p in self.root.iterdir() if DOC_ID.fullmatch(p.name) and p.name not in known
                and (p.is_dir() or p.is_symlink())]

    def clear_interrupted_workspaces(self) -> None:
        # Called once at startup, before this single-process API starts any workers/inspections.
        for path in self.jobs.iterdir():
            safeio.remove(path)

    def read_json(self, doc_id: str, name: str) -> dict | None:
        path = self.doc_dir(doc_id) / name
        try:
            return safeio.read_json(path)
        except (OSError, ValueError):
            return None

    def write_json(self, doc_id: str, name: str, data: dict) -> None:
        path = self.doc_dir(doc_id) / name
        safeio.write_json(path, data)

    @contextmanager
    def job_workspace(self, doc_id: str):
        """The child can write only here, never to API control files or published results."""
        source = self.doc_dir(doc_id)
        with tempfile.TemporaryDirectory(prefix=f"{doc_id}-", dir=self.jobs) as name:
            work = Path(name)
            inputs = {"job.json", "source.pdf", "prepared.json", "conversion.json", "source-tables.json", "source-words.json", "source-furniture.json"}
            inputs.update(p.name for p in source.iterdir() if re.fullmatch(r"original\.[a-zA-Z0-9]+", p.name))
            for filename in inputs:
                try:
                    safeio.copy_file(source / filename, work / filename)
                except FileNotFoundError:
                    if filename == "job.json":
                        raise
            yield work

    def publish_prepared(self, doc_id: str, work: Path) -> None:
        for name in ("source.pdf", "prepared.json", "conversion.json", "source-tables.json", "source-words.json", "source-furniture.json"):
            try:
                safeio.copy_file(work / name, self.doc_dir(doc_id) / name)
            except FileNotFoundError:
                if name == "source.pdf":
                    raise

    def publish_parse(self, doc_id: str, parsed: dict) -> None:
        # The JSON has already passed the model validation. Private OCR crops stay in the job
        # workspace; the API never needs to expose or retain child-controlled figure paths.
        self.write_json(doc_id, "parsed.json", parsed)

    # Samples -------------------------------------------------------------------------

    def _cache_path(self, sample_key: str, parser: str, file_hash: str) -> Path:
        if not SAMPLE_KEY.match(sample_key):
            raise ValueError("invalid sample key")
        return self.sample_cache / f"{sample_key}.{parser}.v{PARSE_OUTPUT_VERSION}.{file_hash[:16]}.json"

    def store_sample_cache(self, sample_key: str, parser: str, file_hash: str, parsed: Path) -> None:
        target = self._cache_path(sample_key, parser, file_hash)
        safeio.copy_file(parsed, target, safeio.JSON_LIMIT)

    def load_sample_cache(self, sample_key: str, parser: str, file_hash: str) -> dict | None:
        try:
            return safeio.read_json(self._cache_path(sample_key, parser, file_hash))
        except (OSError, ValueError):
            return None
