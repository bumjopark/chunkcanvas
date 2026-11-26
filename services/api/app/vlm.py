"""Pictures read by a vision-language model, for visitors who unlocked it with the password.

OCR gives a picture only scattered words (axis ticks, a few labels); the model reads the whole
region instead: the text in it and what it shows (chart values and trends, how a diagram's parts
connect). It runs in the API process after the sandboxed parse, which already cut each picture
out to a PNG (figures/figures.json); only those images, their caption and the sentence before them
leave the server. The answer replaces the picture's text, marked as a description.
"""
import base64
import asyncio
import datetime
import json
import logging
import threading
import time
import httpx
from pathlib import Path

from . import safeio
from .config import Settings
from .models import ParsedDocument
from .parsers.base import blocks_fingerprint

log = logging.getLogger("chunkcanvas.vlm")

LANGUAGES = {"ko": "Korean", "ja": "Japanese", "zh": "Chinese",
             "en": "the language of the caption and surrounding text (English when there is none)"}
# Section names the answer uses, and the label that marks it in chunk text.
SECTIONS = {
    "ko": ("그림 속 글자", "설명", "그림 설명"),
    # The Japanese and Chinese for the English ones.
    "ja": ("\u56f3\u4e2d\u306e\u6587\u5b57", "\u8aac\u660e", "\u56f3\u306e\u8aac\u660e"),
    "zh": ("\u56fe\u4e2d\u6587\u5b57", "\u8bf4\u660e", "\u56fe\u7247\u8bf4\u660e"),
    "en": ("Text in the figure", "Description", "Figure description"),
}
SYSTEM = (
    "You read a figure cut out of a document so that it can be found by text search. First write every "
    "legible piece of text inside the image, exactly as written (not the caption, which is given separately). "
    "Then describe what the figure shows: for a chart, its type, axes with units, series and the values or "
    "trends that can be read; for a table or form, its rows and columns; for a diagram, its parts and how they "
    "connect; for a photo or drawing, what it depicts. Say nothing that cannot be seen in the image. Write short "
    "labels such as reference numbers, panel letters and axis ticks on one line, separated by commas. Plain "
    "text, no markdown, at most about 200 words. Answer in {language}, keeping text from the image as written. "
    "Use two sections: '{text}:' (omit it when the image has no text) and '{desc}:'."
)

_usage_lock = threading.Lock()


def _reserve(settings: Settings, wanted: int) -> int:
    """Take up to `wanted` pictures from today's allowance; returns how many were granted."""
    if settings.vlm_daily_images <= 0:
        return wanted
    # Resolve the trusted administrator-selected root, never parser artifact links.
    path = settings.data_dir.resolve() / "vlm-usage.json"
    today = datetime.date.today().isoformat()
    with _usage_lock:
        try:
            usage = safeio.read_json(path, 8192)
        except FileNotFoundError:
            usage = {}
        except (OSError, ValueError):
            log.warning("Cannot read picture allowance; requests deferred")
            return 0
        if not isinstance(usage, dict):
            return 0
        used = usage.get(today, 0)
        if not isinstance(used, int) or used < 0:
            return 0
        granted = max(0, min(wanted, settings.vlm_daily_images - used))
        safeio.write_json(path, {today: used + granted})
        return granted


def _without_empty_sections(answer: str, names: tuple[str, ...]) -> str:
    """The answer without a section heading that has nothing under it ("Text in the figure:" for a
    photo without text)."""
    lines = answer.strip().split("\n")
    out = []
    for i, ln in enumerate(lines):
        bare = ln.strip()
        is_heading = bare.endswith((":", "：")) and bare[:-1].strip() in names
        following = next((x.strip() for x in lines[i + 1:] if x.strip()), "")
        if is_heading and (not following or any(following.startswith(n) and following[len(n):].lstrip()[:1] in (":", "：")
                                                for n in names)):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _without_runs(answer: str) -> str:
    """A label listed many times in a row ("Expense Row, Expense Row, ...", one per sheet row)
    written once with its count."""
    def line(text: str) -> str:
        runs: list[list] = []
        for item in text.split(", "):
            if runs and runs[-1][0] == item:
                runs[-1][1] += 1
            else:
                runs.append([item, 1])
        return ", ".join(f"{item} (×{n})" if n >= 3 else ", ".join([item] * n) for item, n in runs)

    return "\n".join(line(ln) for ln in answer.split("\n"))


async def _ask(settings: Settings, image: bytes, figure: dict, lang: str, deadline: float,
               cancelled: threading.Event, client: httpx.AsyncClient) -> str | None:
    text_name, desc_name, _ = SECTIONS[lang]
    context = []
    if figure.get("caption"):
        context.append(f"Caption printed with the figure (context only): {figure['caption']}")
    if figure.get("context"):
        context.append(f"Text just before the figure (context only): {figure['context']}")
    body = {
        "model": settings.vlm_model,
        "messages": [
            {"role": "system", "content": SYSTEM.format(language=LANGUAGES[lang], text=text_name, desc=desc_name)},
            {"role": "user", "content": [
                {"type": "text", "text": "\n".join(context) or "No caption."},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image).decode()}},
            ]},
        ],
        "max_completion_tokens": 2000,
    }
    if settings.vlm_reasoning_effort:
        body["reasoning_effort"] = settings.vlm_reasoning_effort
    for attempt in range(2):
        if cancelled.is_set():
            return None
        timeout = deadline - time.monotonic()
        if timeout < 5:
            return None
        # Charge a picture only when its first HTTP attempt starts. Waiting tasks, missing
        # crops and cancellation before sending consume no allowance. Sent attempts are not refunded.
        if attempt == 0 and not _reserve(settings, 1):
            return None
        try:
            async with asyncio.timeout(min(120, timeout)):
                async with client.stream("POST", settings.openai_base_url.rstrip("/") + "/chat/completions",
                                         json=body, headers={"Authorization": f"Bearer {settings.openai_api_key.get_secret_value()}"},
                                         timeout=min(120, timeout)) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > 1024 * 1024:
                            raise ValueError("picture response exceeded the limit")
                        data.extend(chunk)
                    answer = json.loads(data)["choices"][0]["message"].get("content") or ""
                    if not isinstance(answer, str):
                        raise ValueError("picture response was not text")
            return _without_runs(_without_empty_sections(answer, (text_name, desc_name))) or None
        except httpx.HTTPStatusError as exc:
            # Status only: the body can echo the request.
            log.warning("vlm request failed with HTTP %s", exc.response.status_code)
            if exc.response.status_code not in (429, 500, 502, 503, 504) or attempt:
                return None
            await asyncio.sleep(3)
        except (httpx.HTTPError, OSError, TimeoutError, ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("vlm request failed: %s", type(exc).__name__)
            return None
    return None


def describe_figures(doc_dir: Path, settings: Settings, cancelled: threading.Event | None = None) -> list[str]:
    """Replace picture text in parsed.json with the model's reading. Returns notices to add."""
    cancelled = cancelled or threading.Event()
    if cancelled.is_set():
        return []
    manifest = doc_dir / "figures" / "figures.json"
    if not manifest.exists():
        return []
    spec = safeio.read_json(manifest)
    path = doc_dir / "parsed.json"
    doc = ParsedDocument.model_validate(safeio.read_json(path))
    if spec.get("blocksFingerprint") != doc.blocks_fingerprint:
        return ["vlm_partial"]
    picture_ids = {b.id for b in doc.blocks if b.type == "picture"}
    figures = [f for f in spec["figures"] if f["blockId"] in picture_ids][: settings.vlm_max_figures]
    lang = spec.get("language") if spec.get("language") in SECTIONS else "en"
    deadline = time.monotonic() + settings.vlm_seconds
    answers: dict[str, str] = {}
    async def run():
        concurrency = max(1, settings.vlm_concurrency)
        slots = asyncio.Semaphore(concurrency)
        limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
        async with httpx.AsyncClient(limits=limits) as client:
            async def ask(figure: dict):
                async with slots:
                    if cancelled.is_set() or time.monotonic() >= deadline:
                        return
                    try:
                        name = figure["file"]
                        if not isinstance(name, str) or Path(name).name != name or name in (".", ".."):
                            return
                        image = safeio.read_bytes(doc_dir / "figures" / name, 32 * 1024 * 1024)
                    except (OSError, ValueError, KeyError):
                        return
                    if answer := await _ask(settings, image, figure, lang, deadline, cancelled, client):
                        answers[figure["blockId"]] = answer

            tasks = [asyncio.create_task(ask(f)) for f in figures]
            async def watch():
                while not cancelled.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                for task in tasks:
                    task.cancel()
            watcher = asyncio.create_task(watch())
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                pass
            finally:
                for task in tasks:
                    task.cancel()
                watcher.cancel()
                await asyncio.gather(*tasks, watcher, return_exceptions=True)
    asyncio.run(run())
    log.info("vlm read %d of %d pictures", len(answers), len(figures))
    if cancelled.is_set() or not path.exists():
        return []
    if answers:
        current = ParsedDocument.model_validate(safeio.read_json(path))
        if current.blocks_fingerprint != doc.blocks_fingerprint:
            return ["vlm_partial"]
        label = SECTIONS[lang][2]
        for block in doc.blocks:
            if block.type == "picture" and block.id in answers:
                block.text = answers[block.id]
                block.markdown = f"[{label}]\n{block.text}"
                block.source.parser = settings.vlm_model
                block.lines = None
                block.line_geometry = "unavailable"
        doc.blocks_fingerprint = blocks_fingerprint(doc.document.parser, doc.document.parser_version, doc.blocks)
        safeio.write_json(path, doc.dump())
    notices = ["vlm_figures"] if answers else []
    if len(answers) < len(figures):
        notices.append("vlm_partial")
    return notices
