"""Parse one document. Runs as a subprocess (optionally inside bubblewrap without network).

Reads <doc-dir>/job.json and source.pdf; writes progress.json while working, then either
parsed.json or error.json. Error messages are generic and never include document text.
"""
import argparse
import json
import os
import sys
from pathlib import Path


def write_atomic(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


# An image page with less text than this (OCR of a sign in a photo) is a photo.
PHOTO_TEXT = 40


def _photos_as_pictures(parsed) -> None:
    """A page of an uploaded image with no picture found and hardly any text ("Br" read off a sign)
    is a photo: one picture over the page, which the vision model can say what it shows."""
    from .models import DocumentBlock
    from .parsers.base import blocks_fingerprint

    photos = {p for p in range(len(parsed.pages))
              if not any(b.type == "picture" and b.regions and b.regions[0].page_index == p for b in parsed.blocks)
              and sum(len(b.text.replace(" ", "")) for b in parsed.blocks if b.regions and b.regions[0].page_index == p) < PHOTO_TEXT}
    if not photos:
        return
    kept = [b for b in parsed.blocks if not (b.regions and b.regions[0].page_index in photos)]
    for p in sorted(photos):
        # What OCR read off it (a sign's word) stays, as the picture's own text.
        text = " ".join(b.text for b in parsed.blocks if b.regions and b.regions[0].page_index == p and b.text.strip())
        kept.append(DocumentBlock.model_validate({
            "id": "b0000", "readingOrder": 0, "type": "picture", "text": text,
            "regions": [{"pageIndex": p, "bbox": {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}}],
            "source": {"parser": parsed.document.parser}}))
    kept.sort(key=lambda b: (b.regions[0].page_index if b.regions else 0, b.reading_order))
    for i, b in enumerate(kept):
        b.id, b.reading_order = f"b{i:04d}", i
    parsed.blocks = kept
    parsed.blocks_fingerprint = blocks_fingerprint(parsed.document.parser, parsed.document.parser_version, parsed.blocks)


def main() -> int:
    from .sandbox import limit_self

    limit_self()
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc-dir", required=True)
    ap.add_argument("--parser", required=True, choices=["hybrid", "docling", "docling-ocr", "pymupdf"])
    ap.add_argument("--models", required=True)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--table-mode", choices=["fast", "accurate"], default="fast")
    # Hybrid only: also OCR pictures (pages without a text layer inside a digital document).
    ap.add_argument("--ocr", action="store_true")
    ap.add_argument("--formula-seconds", type=float, default=0)
    # Also cut out up to this many picture regions for the vision-language model step.
    ap.add_argument("--figures", type=int, default=0)
    args = ap.parse_args()

    doc_dir = Path(args.doc_dir)
    job = json.loads((doc_dir / "job.json").read_text(encoding="utf-8"))

    def progress(done: int, total: int) -> None:
        write_atomic(doc_dir / "progress.json", json.dumps({"done": done, "total": total}))

    def fail(code: str, message: str, exit_code: int) -> int:
        write_atomic(doc_dir / "error.json", json.dumps({"code": code, "message": message}))
        return exit_code

    from .parsers.base import ParseError

    try:
        source = doc_dir / "source.pdf"
        furniture = doc_dir / "source-furniture.json"
        templates = []
        if furniture.exists() and furniture.stat().st_size <= 1024 * 1024:
            raw_templates = json.loads(furniture.read_text(encoding="utf-8"))
            if isinstance(raw_templates, list):
                templates = raw_templates[:256]
                job["_sourceFurniture"] = templates
        if args.parser == "pymupdf":
            from .parsers import pymupdf_parser

            parsed = pymupdf_parser.parse(source, file_meta=job, progress=progress)
        elif args.parser == "hybrid":
            from .parsers import hybrid_parser

            parsed = hybrid_parser.parse(source, file_meta=job, models_dir=Path(args.models),
                                         threads=args.threads, progress=progress, table_mode=args.table_mode,
                                         ocr=args.ocr, formula_seconds=args.formula_seconds)
        else:
            from .parsers import docling_parser

            parsed = docling_parser.parse(
                source,
                file_meta=job,
                models_dir=Path(args.models),
                threads=args.threads,
                ocr=args.parser == "docling-ocr",
                progress=progress,
                table_mode=args.table_mode,
                formula_seconds=args.formula_seconds,
            )
        # Pictures cut out for the vision model step are read after this job, so a document of
        # pictures only (a chart sheet, a scanned figure) is not a failure then.
        cut = 0
        from .formats import IMAGES

        if job.get("sourceFormat") in IMAGES:
            _photos_as_pictures(parsed)
        if args.figures:
            from .parsers.figures import write_figures

            cut = write_figures(source, parsed, doc_dir / "figures", args.figures)
        if not cut and not any(b.text.strip() for b in parsed.blocks):
            if args.parser == "docling-ocr":
                return fail("no_text", "No text was found, even with OCR.", 2)
            return fail(
                "no_text",
                "No text was found. The PDF may be scanned; try the OCR parser if it is available.",
                2,
            )
        parse_notices = []
        tables = doc_dir / "source-tables.json"
        if tables.exists():
            from .parsers.base import SHEET_FORMATS
            from .parsers.source_tables import apply_source_tables, last_stats as table_stats

            fmt = job.get("sourceFormat")
            # Sheets and tables written from CSV or tab-separated text may be wider than the paper.
            conversion = doc_dir / "conversion.json"
            notices = json.loads(conversion.read_text()).get("notices", []) if conversion.exists() else []
            csv_header = any(n in notices for n in ("csv_header_inferred", "csv_header_unverified"))
            put_back = apply_source_tables(parsed, json.loads(tables.read_text(encoding="utf-8")),
                                           sheets=fmt in SHEET_FORMATS, pdf=source,
                                           columns_overflow=fmt in SHEET_FORMATS or fmt in ("csv", "txt"),
                                           repeat_header=csv_header or fmt not in ("csv", "txt"),
                                           source_furniture=templates, strict_records=csv_header or fmt == "csv")
            print(f"source tables: {put_back}", file=sys.stderr)
            if table_stats["uncertain_blocks"]:
                parse_notices.append("source_tables_partial")
        words = doc_dir / "source-words.json"
        if words.exists():
            from .parsers.source_words import rejoin

            print(f"rejoined words: {rejoin(parsed, set(json.loads(words.read_text(encoding='utf-8'))))}", file=sys.stderr)
        from .parsers import formulas
        from .parsers.base import add_table_lines, add_text_lines, normalize_line_geometry

        add_table_lines(parsed)
        add_text_lines(parsed)
        normalize_line_geometry(parsed)

        from .parsers.heading_recognition import recognize_headings

        recognize_headings(parsed, source)
        from .parsers.heading_structure import assign_heading_levels

        assign_heading_levels(parsed, source)
        add_text_lines(parsed)
        normalize_line_geometry(parsed)

        if cut:
            manifest = doc_dir / "figures" / "figures.json"
            spec = json.loads(manifest.read_text(encoding="utf-8"))
            spec["blocksFingerprint"] = parsed.blocks_fingerprint
            write_atomic(manifest, json.dumps(spec, ensure_ascii=False))

        if formulas.last_stats.get("skipped"):
            parse_notices.append("formulas_partial")
        if parse_notices:
            write_atomic(doc_dir / "parse-notices.json", json.dumps(parse_notices))
        write_atomic(doc_dir / "parsed.json", parsed.model_dump_json(by_alias=True, exclude_none=True))
        return 0
    except ParseError as exc:
        return fail(exc.code, exc.message, 2)
    except MemoryError:
        return fail("out_of_memory", "The parser ran out of memory on this PDF.", 3)
    except Exception as exc:  # noqa: BLE001
        # Only the exception type goes to stderr: messages can quote document content.
        print(f"parse failed: {type(exc).__name__}", file=sys.stderr)
        return fail("parse_failed", "The parser failed on this PDF.", 1)


if __name__ == "__main__":
    sys.exit(main())
