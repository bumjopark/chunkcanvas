"""A numbered heading survives the actual parse-job serialization boundary."""
import json
import os
import subprocess
import sys

import pymupdf


def test_parse_job_recovers_a_numbered_heading_from_the_light_parser(tmp_path):
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    page.insert_text((60, 80), "1. Setup", fontsize=11, fontname="hebo")
    page.insert_text((60, 115), "The operator checks every setting before starting the service.", fontsize=10.5)
    page.insert_text((60, 160), "1.1 Checks", fontsize=11, fontname="hebo")
    page.insert_text((60, 195), "The operator records the result and reviews the next step.", fontsize=10.5)
    pdf.save(tmp_path / "source.pdf"); pdf.close()
    (tmp_path / "job.json").write_text(json.dumps(dict(id="test", filename="test.pdf", fileHash="test", sourceFormat="pdf")))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CC_", "OPENAI"))}
    result = subprocess.run([sys.executable, "-m", "app.parse_job", "--doc-dir", str(tmp_path),
                             "--parser", "pymupdf", "--models", str(tmp_path), "--threads", "1"],
                            capture_output=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr.decode()
    parsed = json.loads((tmp_path / "parsed.json").read_text())
    assert [(b["text"], b["level"]) for b in parsed["blocks"] if b["type"] == "section_header"] == [
        ("1. Setup", 1), ("1.1 Checks", 2)]
    assert len(parsed["blocks"]) == 4
