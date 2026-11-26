"""Text a font gives wrong letters for, read again by OCR.

Some PDFs set a few lines (captions, a running head) in a font whose Unicode map is broken: the
text layer then holds stray letters of unrelated scripts ("�ᯱഭ�� ,*4"). Those blocks are rendered
and read by the same OCR models the OCR parser uses.
"""
import time
import unicodedata
from collections import Counter
from pathlib import Path

from .spacing import restore_spacing

# Per document: OCR is slow on CPU (about two seconds a block).
MAX_BLOCKS = 40
SECONDS = 90
TYPES = {"paragraph", "list_item", "caption", "footnote", "section_header", "title", "page_header", "page_footer"}
# Scripts documents mix in for real (a Latin term in Korean text, Greek and math letters, a quote
# in Arabic); the stray letters of a broken font come from others (Thai, Malayalam, Tagalog...).
COMMON = {"LATIN", "HANGUL", "CJK", "HIRAGANA", "KATAKANA", "GREEK", "CYRILLIC", "ARABIC", "HEBREW", "MATHEMATICAL",
          "DOUBLE-STRUCK", "SCRIPT", "FULLWIDTH", "HALFWIDTH", "CIRCLED", "PARENTHESIZED", "MODIFIER", "SUPERSCRIPT",
          "SUBSCRIPT", "ROMAN", "DEVANAGARI", "THAI", "COPTIC"}


def _script(c: str) -> str:
    if c == "�":
        return "?"
    try:
        return unicodedata.name(c).split()[0]
    except ValueError:
        return "?"


def garbled_blocks(blocks: list[dict]) -> list[dict]:
    """Blocks mostly made of replacement characters and letters of scripts the document hardly
    uses elsewhere (judged against the document itself, so a Thai document is not garbled)."""
    letters = Counter(_script(c) for b in blocks for c in b["text"] if c.isalpha() or c == "�")
    total = sum(letters.values()) or 1
    rare = {s for s, n in letters.items() if n < 0.02 * total and s not in COMMON} | {"?"}
    out = []
    for b in blocks:
        if b["type"] not in TYPES or not b["regions"]:
            continue
        own = [_script(c) for c in b["text"] if c.isalpha() or c == "�"]
        if len(own) >= 4 and sum(s in rare for s in own) > 0.3 * len(own):
            out.append(b)
    return out


def read_garbled(pdf, blocks: list[dict], models_dir: Path, threads: int, stats: dict) -> None:
    """OCR the garbled blocks' regions and take what it reads (within MAX_BLOCKS and SECONDS)."""
    todo = garbled_blocks(blocks)[:MAX_BLOCKS]
    korean = models_dir / "RapidOcr-korean" / "korean_PP-OCRv5_rec_mobile.onnx"
    detect = models_dir / "RapidOcr" / "PP-OCRv6_det_small.onnx"
    if not todo or not korean.exists() or not detect.exists():
        return
    import numpy as np
    import pymupdf
    from rapidocr import RapidOCR

    ocr = RapidOCR(params={
        "Global.log_level": "error", "Global.use_cls": False,
        "Det.model_path": str(detect), "Rec.model_path": str(korean),
        "Rec.rec_keys_path": str(korean.parent / "ppocrv5_korean_dict.txt"),
        "Cls.model_path": str(models_dir / "RapidOcr" / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
        "EngineConfig.onnxruntime.intra_op_num_threads": threads,
    })
    deadline = time.monotonic() + SECONDS
    for b in todo:
        if time.monotonic() > deadline:
            break
        parts = []
        for region in b["regions"]:
            page = pdf[region["pageIndex"]]
            W, H = page.rect.width, page.rect.height
            box = region["bbox"]
            clip = pymupdf.Rect(box["x0"] * W - 2, box["y0"] * H - 2, box["x1"] * W + 2, box["y1"] * H + 2) * ~page.rotation_matrix
            pix = page.get_pixmap(clip=clip, dpi=200)
            image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            result = ocr(image[:, :, :3] if pix.n == 4 else image, use_cls=False)
            parts += list(result.txts or ())
        text = restore_spacing(" ".join(parts).strip())
        if text:
            b["text"], b["lines"] = text, None
            stats["garbled"] = stats.get("garbled", 0) + 1
