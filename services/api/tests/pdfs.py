"""Build small PDFs for tests on the fly (no binary fixtures in the repo)."""
import pymupdf


def text_pdf(pages: int = 1, rotate: int = 0, crop: tuple | None = None, text: str | None = None) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 90), f"Heading {i + 1}", fontsize=20)
        body = text or "This is a paragraph of body text. It talks about chunking documents for retrieval."
        page.insert_textbox(pymupdf.Rect(72, 120, 520, 300), body, fontsize=10)
        if crop:
            page.set_cropbox(pymupdf.Rect(*crop))
        if rotate:
            page.set_rotation(rotate)
    return doc.tobytes()


def encrypted_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret")
    return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="owner")


def blank_pdf(pages: int = 1) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    return doc.tobytes()


def picture_pdf() -> bytes:
    """A paragraph and a large raster picture below it."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(72, 72, 520, 160), "Results are summarized in the chart below.", fontsize=11)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 240, 160), 0)
    pix.set_rect(pix.irect, (200, 40, 40))
    page.insert_image(pymupdf.Rect(72, 200, 432, 440), pixmap=pix)
    return doc.tobytes()
