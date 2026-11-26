"""Quick structural check of an uploaded PDF, run in a short-lived (optionally sandboxed)
subprocess so a malformed file cannot crash the API. Prints one JSON object."""
import json
import sys


def _text_layer(pdf, pages: int) -> tuple[bool, int]:
    """Whether the PDF has a usable text layer, and how many of its pages are pictures of text.

    A page needs OCR when it has almost no text and is mostly covered by images (a scan, a
    photographed slide), or has no text at all but a sizeable picture; a short note or a blank
    page is not a scan. Scans often carry a few stray
    characters (page numbers), so it takes enough pages with real text, not any text."""
    import pypdfium2 as pdfium

    with_text = total = image_heavy = 0
    for i in range(pages):
        page = pdf[i]
        textpage = page.get_textpage()
        chars = len("".join(textpage.get_text_range().split()))
        textpage.close()
        width, height = page.get_size()
        covered = 0.0
        for obj in page.get_objects(filter=(pdfium.raw.FPDF_PAGEOBJ_IMAGE,), max_depth=2):
            left, bottom, right, top = obj.get_bounds()
            covered += max(0.0, min(right, width) - max(left, 0)) * max(0.0, min(top, height) - max(bottom, 0))
        page.close()
        total += chars
        with_text += chars >= 30
        # Mostly a picture with a few stray characters, or no text at all beside a sizeable picture.
        image_heavy += chars < 30 and covered >= 0.5 * width * height or chars == 0 and covered >= 0.1 * width * height
    if with_text >= max(1, 0.3 * pages) or (pages <= 3 and total >= 15):
        return True, image_heavy
    return image_heavy < max(1, 0.5 * pages), image_heavy


def inspect(path: str, keep_pages: int | None = None, keep_pages_without_text: int | None = None) -> dict:
    """Validate the PDF and report whether it has a text layer. With keep_pages, rewrite it in
    place with only its first pages (keep_pages_without_text for PDFs that will need OCR)."""
    import os

    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            return {"ok": False, "code": "encrypted_pdf", "message": "The PDF is password-protected."}
        return {"ok": False, "code": "corrupt_pdf", "message": "The PDF is damaged or not a valid PDF."}
    try:
        pages = len(pdf)
        if pages == 0:
            return {"ok": False, "code": "empty_pdf", "message": "The PDF has no pages."}
        has_text, scans = _text_layer(pdf, min(pages, keep_pages or 20))
        keep = keep_pages if has_text else (keep_pages_without_text or keep_pages)
        if keep is None or pages <= keep:
            return {"ok": True, "pageCount": pages, "hasText": has_text, "scanPages": scans}
        kept = pdfium.PdfDocument.new()
        try:
            kept.import_pages(pdf, pages=list(range(keep)))
            tmp = f"{path}.part"
            kept.save(tmp)
        finally:
            kept.close()
        os.replace(tmp, path)
        return {"ok": True, "pageCount": keep, "sourcePageCount": pages, "hasText": has_text, "scanPages": scans}
    finally:
        pdf.close()


if __name__ == "__main__":
    from .sandbox import limit_self

    limit_self()
    keep = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] != "-" else None
    keep_without_text = int(sys.argv[3]) if len(sys.argv) > 3 else None
    print(json.dumps(inspect(sys.argv[1], keep, keep_without_text)))
