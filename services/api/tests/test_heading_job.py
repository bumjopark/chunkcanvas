"""The real subprocess pipeline must emit the same tree used by export."""
import json
import os
import subprocess
import sys

import pymupdf

from app.parsers import pymupdf_parser


def test_parse_job_preserves_block_identity_and_emits_source_outline_depth(tmp_path):
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    page.insert_text((60, 80), "Overview", fontsize=12, fontname="hebo")
    page.insert_text((60, 110), "Ordinary body text describing the overview.", fontsize=10)
    page.insert_text((60, 160), "Details", fontsize=12, fontname="hebo")
    page.insert_text((60, 190), "Ordinary body text describing the details.", fontsize=10)
    pdf.set_toc([[1, "Overview", 1], [2, "Details", 1]])
    source = tmp_path / "source.pdf"; pdf.save(source); pdf.close()
    meta = dict(id="test", filename="test.pdf", fileHash="test", sourceFormat="pdf")
    (tmp_path / "job.json").write_text(json.dumps(meta))
    original = pymupdf_parser.parse(source, file_meta=meta, progress=lambda *_: None)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CC_", "OPENAI"))}
    result = subprocess.run([sys.executable, "-m", "app.parse_job", "--doc-dir", str(tmp_path),
                             "--parser", "pymupdf", "--models", str(tmp_path), "--threads", "1"],
                            capture_output=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr.decode()
    parsed = json.loads((tmp_path / "parsed.json").read_text())
    assert [(b["id"], b["type"], b["text"], b["readingOrder"]) for b in parsed["blocks"]] == [
        (b.id, b.type, b.text, b.reading_order) for b in original.blocks]
    assert parsed["blocksFingerprint"] == original.blocks_fingerprint
    assert [(b["text"], b["level"]) for b in parsed["blocks"] if b["type"] == "section_header"] == [
        ("Overview", 1), ("Details", 2)]
