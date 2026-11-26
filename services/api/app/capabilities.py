"""What this server can do with a document, shared by the upload endpoint and the job runner."""
import importlib.util

from .config import Settings
from .formats import ALL, FIXED, IMAGES, REQUIRES_ISOLATION


def ocr_available(settings: Settings) -> bool:
    return (
        settings.ocr_enabled
        and importlib.util.find_spec("onnxruntime") is not None
        and (settings.models_dir / "RapidOcr").exists()
    )


def page_limit(settings: Settings, parser: str) -> int:
    return settings.max_ocr_pages if parser == "docling-ocr" else settings.max_pages


def accepted_formats(settings: Settings) -> set[str]:
    """PDF, images and XPS always; everything else needs LibreOffice for rendering."""
    if settings.soffice_path:
        return ALL - REQUIRES_ISOLATION if settings.parser_sandbox == "none" else set(ALL)
    return {"pdf"} | IMAGES | FIXED


def inspect_limits(settings: Settings, parser: str) -> tuple[int | None, int | None]:
    """Pages to keep for documents with and without a text layer (None: keep all)."""
    if not settings.truncate_long_pdfs:
        return None, None
    can_ocr = ocr_available(settings) and parser != "docling-ocr"
    return page_limit(settings, parser), page_limit(settings, "docling-ocr") if can_ocr else None


def route_by_text_layer(settings: Settings, parser: str, info: dict) -> tuple[str, str | None]:
    """PDFs without a text layer (scans, image-only exports) go to the OCR parser when available.
    A digital document with some pages that are pictures of text keeps the hybrid parser, which
    then also runs OCR on pictures ("mixed_ocr")."""
    if not ocr_available(settings) or parser == "docling-ocr":
        return parser, None
    if not info.get("hasText", True):
        return "docling-ocr", "auto_ocr"
    if parser == "hybrid" and info.get("scanPages", 0) > 0:
        return parser, "mixed_ocr"
    return parser, None
