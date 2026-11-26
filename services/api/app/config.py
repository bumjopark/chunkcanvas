from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing_extensions import Annotated

API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parents[1]


class Settings(BaseSettings):
    """Every value can be set with a CC_ environment variable, e.g. CC_MAX_PAGES=50."""

    model_config = SettingsConfigDict(env_prefix="CC_", env_file=None, extra="ignore")

    data_dir: Path = REPO_ROOT / "data"
    samples_dir: Path = REPO_ROOT / "samples"
    # Optional gallery of ready-made samples (scripts/build_samples.py), shown next to the basic ones.
    gallery_dir: Path = REPO_ROOT / "gallery"
    # The two basic samples in samples/ (also used by the tests); a site with a gallery can hide them.
    basic_samples: bool = True
    models_dir: Path = API_ROOT / ".models"
    # Built frontend (apps/web/dist). When set, the API also serves the web app.
    web_dist: Path | None = None

    # Public URL, e.g. https://chunkcanvas.example.com. Plain-HTTP requests forwarded by a
    # trusted proxy are redirected here.
    public_origin: str | None = None
    allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    # Read the client address from CF-Connecting-IP / X-Forwarded-For. Only enable when the
    # API is reachable solely through a local reverse proxy or tunnel.
    trust_proxy_headers: bool = False
    # The trusted proxy must replace this header; never fall back to a different one.
    proxy_ip_header: Literal["cf-connecting-ip", "x-forwarded-for"] = "cf-connecting-ip"
    # Allow the Cloudflare Web Analytics beacon that Cloudflare injects into proxied pages.
    csp_allow_cloudflare_insights: bool = False

    max_upload_mb: int = 10
    max_pages: int = 30
    # Instead of rejecting longer PDFs, keep only their first max_pages pages (public demos).
    truncate_long_pdfs: bool = False
    # Repository link shown to visitors, e.g. next to the truncation notice.
    repo_url: str | None = None
    max_ocr_pages: int = 10
    document_ttl_seconds: int = 3600
    max_queued_jobs: int = 20
    max_active_documents_per_client: int = 5
    max_sample_documents_per_client: int = 20
    max_stored_documents: int = 1000
    max_stored_sample_documents: int = Field(default=200, ge=0)
    inspection_concurrency: int = Field(default=1, ge=1, le=8)
    upload_timeout_seconds: int = Field(default=120, ge=1, le=300)
    upload_idle_seconds: float = Field(default=15, gt=0, le=300)
    upload_grace_seconds: float = Field(default=10, gt=0, le=60)
    upload_min_bytes_per_second: int = Field(default=4096, ge=1)
    # Receiving a body is cheap (it streams to disk and holds no parser slot), so the
    # global cap is generous; per-client and per-network caps stop one site from
    # holding them all. A /48 is one organisation's IPv6 allocation.
    max_uploads_in_progress: int = Field(default=32, ge=1, le=200)
    max_uploads_in_progress_per_client: int = Field(default=2, ge=1, le=100)
    max_uploads_in_progress_per_network: int = Field(default=4, ge=1, le=200)
    uploads_per_client_per_hour: int = 20
    samples_per_client_per_hour: int = 60
    ipv6_rate_prefix: int = Field(default=56, ge=48, le=128)
    ipv6_network_prefix: int = Field(default=48, ge=32, le=128)
    requests_per_client_per_minute: int = 600

    worker_concurrency: int = 1
    job_timeout_seconds: int = 900
    # Docling table-structure model. "fast" took half the time of "accurate" on table-heavy papers
    # with the same cells in our tests; use "accurate" when CPU time is not a concern.
    table_mode: Literal["fast", "accurate"] = "fast"
    parser_threads: int = 4
    parser_sandbox: Literal["none", "bwrap", "landlock"] = "bwrap"
    # LibreOffice for Office, HTML, Markdown, text, CSV and Hangul uploads. Found automatically in
    # .tools/libreoffice or on PATH; without it only PDFs and images are accepted.
    soffice: Path | None = None
    convert_timeout_seconds: int = 150
    ocr_enabled: bool = True
    # Hybrid parser: also OCR pictures in digital documents (screenshots, scanned figures, slides
    # made of images). Took a 18-page image-heavy deck from 8k to 19k characters, at up to 5x the
    # parse time; pages that are pictures of text are OCR'd either way.
    ocr_pictures: bool = True
    # Display formulas are read from their image as LaTeX (pix2text-mfr, ~1-2 s each on CPU) for
    # at most this long per document; the rest keep their text-layer text. 0 turns it off.
    formula_seconds: int = 300
    default_parser: Literal["hybrid", "docling", "pymupdf"] = "hybrid"

    # Allow password-free opt-in on an individual upload using the operator's provider key.
    vlm_open: bool = False
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    vlm_password: SecretStr | None = Field(None, validation_alias=AliasChoices("CC_VLM_PASSWORD", "CC_API_PWD"))
    vlm_model: str = "gpt-6-luna"
    # "low" read charts as well as the default at half the time; "none" copied captions into the
    # text it claimed to see in the image.
    vlm_reasoning_effort: str | None = "low"
    # Per document, to bound its time: about 5 s a picture, vlm_concurrency at once.
    vlm_max_figures: int = 80
    vlm_seconds: int = 300
    vlm_concurrency: int = 8
    # Optional ceiling on pictures sent per day over all visitors (0: none), should the password
    # be shared more widely.
    vlm_daily_images: int = 0

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        return [v.strip() for v in value.split(",") if v.strip()] if isinstance(value, str) else value

    @property
    def soffice_path(self) -> Path | None:
        if self.soffice:
            return self.soffice.resolve() if self.soffice.exists() else None
        bundled = sorted((REPO_ROOT / ".tools/libreoffice/root/opt").glob("libreoffice*/program/soffice"))
        if bundled:
            return bundled[-1].resolve()
        import shutil

        found = shutil.which("soffice") or shutil.which("libreoffice")
        return Path(found) if found else None

    @property
    def vlm_enabled(self) -> bool:
        return bool(self.openai_api_key and self.openai_api_key.get_secret_value()
                    and (self.vlm_open or (self.vlm_password and self.vlm_password.get_secret_value())))

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def sample_cache_dir(self) -> Path:
        return self.data_dir / "sample-cache"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "chunkcanvas.sqlite3"


@lru_cache
def get_settings() -> Settings:
    return Settings()
