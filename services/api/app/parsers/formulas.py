"""Display formulas read from their image as LaTeX (pix2text-mfr 1.5, MIT, CPU).

The text layer of a formula is often unusable: math fonts without a Unicode map turn
"d_p(A) := {g ∈ G}" into "dppAq :“ tg P G", scans have no text layer at all, and even good
fonts give operands in drawing order. A formula recognizer reads what is printed instead.
Its LaTeX replaces the text-layer text only when it looks sound; otherwise the block keeps
what it had.
"""
import gc
import re
import sys
import time
from collections import Counter
from pathlib import Path

MODEL = "pix2text-mfr-1.5"
DPI = 200
BATCH = 8
MAX_TOKENS = 320
# Characters a math font only yields when its encoding was lost (accented Latin, curly quotes).
GARBLED = re.compile(r"[\u00c0-\u024f\u2018-\u201f\u00b4\u00a8\u00b8\u02c6-\u02dd]")
_CONTROL = re.compile(r"\\[A-Za-z]+")
# Equation numbers in the text layer; operands come in drawing order, so not necessarily last.
_TAG = re.compile(r"\((\d{1,3}(?:\.\d{1,3})?[a-z]?)\)")
_UNSUPPORTED = re.compile(r"\\(?:hfill|hspace\*?\s*\{[^}]*\}|vspace\*?\s*\{[^}]*\})")


def available(models_dir: Path) -> bool:
    return (models_dir / MODEL / "model.safetensors").exists()


def compact_latex(tokens: str) -> str:
    """The model emits space-separated tokens ("\\mathrm { H e s s }"); join them, keeping a space
    where a control word would otherwise run into a letter and after a control space ("\\ ")."""
    out = ""
    for tok in tokens.split():
        if out and (out.endswith("\\") and not out.endswith("\\\\") or re.search(r"\\[A-Za-z]+$", out) and tok[0].isalpha()):
            out += " "
        out += tok
    return _UNSUPPORTED.sub("", out).strip()


def _sound(raw: str) -> bool:
    toks = raw.split()
    groups = raw.replace("\\{", "").replace("\\}", "")  # \{ and \} are printed braces, unpaired in "\left\{ ... \right."
    if not toks or groups.count("{") != groups.count("}") or len(toks) >= MAX_TOKENS - 5:
        return False  # unbalanced, or out of tokens: almost always a loop
    # A loop of one or two tokens ("~ ~ ~ ~", "\\ \\ \\") is a failed read.
    for period in (1, 2):
        run = 0
        for i in range(period, len(toks)):
            run = run + 1 if toks[i] == toks[i - period] else 0
            if run >= 16 * period:
                return False
    return True


def _alnum(text: str) -> Counter:
    return Counter(c for c in text if c.isascii() and c.isalnum())


def agrees(latex: str, text: str) -> bool:
    """Letters and digits of the LaTeX (commands removed) against the text layer's."""
    a, b = _alnum(_CONTROL.sub(" ", latex)), _alnum(text)
    if not b:
        return True
    return sum((a & b).values()) / max(sum(a.values()), sum(b.values())) >= 0.5


def _crop(page, bbox: dict):
    import pymupdf
    from PIL import Image, ImageOps

    W, H = page.rect.width, page.rect.height
    clip = pymupdf.Rect(bbox["x0"] * W - 3, bbox["y0"] * H - 3, bbox["x1"] * W + 3, bbox["y1"] * H + 3)
    pix = page.get_pixmap(clip=clip * ~page.rotation_matrix, dpi=DPI)
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    mask = ImageOps.invert(im.convert("L")).point(lambda v: 255 if v > 40 else 0)
    ink = mask.getbbox()
    if not ink:
        return im, False
    x0, y0, x1, y1 = ink
    # An equation number far to the right ("... (10)") squeezes the formula when the crop is
    # scaled to the model's square input; read the formula alone and add the number back.
    cols = mask.crop(ink).resize((x1 - x0, 1), 4).getdata()  # 4 = box filter: any ink per column
    gaps, start = [], None
    for x, v in enumerate(list(cols) + [255]):
        if v == 0 and start is None:
            start = x
        elif v and start is not None:
            gaps.append((x - start, start, x))
            start = None
    tagged = False
    if gaps:
        width, g0, g1 = max(gaps)
        if width > 0.06 * (x1 - x0) and (x1 - x0) - g1 < 0.2 * (x1 - x0):
            x1, tagged = x0 + g0, True
    im = im.crop((max(0, x0 - 6), max(0, y0 - 6), min(im.width, x1 + 6), min(im.height, y1 + 6)))
    return im, tagged


# Counts from the last call, for the parse job: "skipped" formulas ran out of time.
last_stats: dict = {}


def merge_formula_pieces(blocks: list[dict]) -> int:
    """One formula sometimes comes as several overlapping or touching boxes on the same line (a
    box for "E(e) =", one for its cases, one around both), each read on its own into fragments
    and duplicates. Merge them into one box per formula, in place; returns how many were merged."""
    merged = 0
    i = 0
    while i < len(blocks):
        a = blocks[i]
        if a["type"] != "formula" or len(a["regions"]) != 1:
            i += 1
            continue
        for j in range(len(blocks) - 1, i, -1):
            b = blocks[j]
            if b["type"] != "formula" or len(b["regions"]) != 1 or b["regions"][0]["pageIndex"] != a["regions"][0]["pageIndex"]:
                continue
            x, y = a["regions"][0]["bbox"], b["regions"][0]["bbox"]
            rows = min(x["y1"], y["y1"]) - max(x["y0"], y["y0"])
            gap = max(x["x0"], y["x0"]) - min(x["x1"], y["x1"])
            if rows < 0.5 * min(x["y1"] - x["y0"], y["y1"] - y["y0"]) or gap > 0.04:
                continue
            union = {"x0": min(x["x0"], y["x0"]), "y0": min(x["y0"], y["y0"]), "x1": max(x["x1"], y["x1"]), "y1": max(x["y1"], y["y1"])}
            # Text of the pieces left to right (text-layer math); a reading of the whole replaces it.
            parts = sorted([(x["x0"], a["text"]), (y["x0"], b["text"])])
            outer = [t for box, t in ((x, a["text"]), (y, b["text"])) if box == union and t]
            a["text"] = outer[0] if outer else " ".join(t for _, t in parts if t)
            a["regions"][0]["bbox"] = union
            a["_latex"] = False
            del blocks[j]
            merged += 1
        i += 1
    return merged


def read_formulas(path: Path, blocks: list[dict], models_dir: Path, threads: int, seconds: float) -> dict:
    """Fill formula blocks with LaTeX in place. Returns counts for the job log."""
    pieces = merge_formula_pieces(blocks)
    todo = [b for b in blocks if b["type"] == "formula" and b["regions"]]
    stats = last_stats
    stats.clear()
    stats.update(formulas=len(todo), merged=pieces, latex=0, kept=0, failed=0, skipped=0)
    if not todo or seconds <= 0 or not available(models_dir):
        return stats
    started = time.time()
    try:
        import numpy as np
        import pymupdf
        import torch
        from tokenizers import Tokenizer
        from transformers import VisionEncoderDecoderModel
    except ImportError:
        return stats

    gc.collect()  # Docling's models are out of scope by now; give their memory back first.
    torch.set_num_threads(max(1, threads))
    model = VisionEncoderDecoderModel.from_pretrained(models_dir / MODEL).eval()
    tok = Tokenizer.from_file(str(models_dir / MODEL / "tokenizer.json"))
    pdf = pymupdf.open(path)

    def pixels(block):
        r = block["regions"][0]
        im, block["_tagged"] = _crop(pdf[r["pageIndex"]], r["bbox"])
        im = im.resize((384, 384), 3)  # 3 = bicubic
        return ((np.asarray(im, dtype=np.float32) / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)

    for i in range(0, len(todo), BATCH):
        if time.time() - started > seconds:
            stats["skipped"] += len(todo) - i
            break
        batch = todo[i:i + BATCH]
        x = torch.from_numpy(np.stack([pixels(b) for b in batch]))
        with torch.inference_mode():
            ids = model.generate(pixel_values=x, max_new_tokens=MAX_TOKENS, do_sample=False, num_beams=1,
                                 use_cache=True, decoder_start_token_id=1, eos_token_id=2, pad_token_id=0)
        for block, seq in zip(batch, ids.tolist()):
            raw = tok.decode(seq, skip_special_tokens=True)
            latex = compact_latex(raw)
            if not _sound(raw) or not latex:
                stats["failed"] += 1
                continue
            text = block["text"]
            # A clean text layer outvotes an image read that disagrees with it (a misread, or a
            # sentence the layout model took for a formula); a garbled one never does.
            if text and not GARBLED.search(text) and not agrees(latex, text):
                stats["kept"] += 1
                continue
            numbers = _TAG.findall(text) if block.pop("_tagged", False) else []
            block["text"], block["_latex"] = f"{latex} \\tag{{{numbers[-1]}}}" if numbers else latex, True
            stats["latex"] += 1
    print(f"formulas: {stats} in {time.time() - started:.0f}s", file=sys.stderr)
    return stats
