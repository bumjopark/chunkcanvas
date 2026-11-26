"""Build ready-made samples: run each file through a ChunkCanvas instance and keep what it returns.

    services/api/.venv/bin/python scripts/build_samples.py LIST.json OUT_DIR BASE_URL [--no-vlm]

LIST.json is a list of {"key", "file", "title", "language", "description", "group"}; all but "file"
go into OUT_DIR/manifest.json, which the server reads from CC_GALLERY_DIR (default: gallery/).
For each sample OUT_DIR/<key>/ gets the display PDF (source.pdf), the parse (parsed.json) and
meta.json (parser, notices, source format, page counts, the original's name and hash). With CC_API_PWD set (or
in .env), pictures are read by the vision model as for a visitor who entered the password.
"""
import hashlib
import json
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import argparse
from datetime import datetime, timezone
import subprocess
from pathlib import Path

LIST: Path
OUT: Path
BASE: str
JOB_TIMEOUT = 1800
GENERATOR_REVISION = "unknown"
ROOT = Path(__file__).resolve().parents[1]


def password() -> str | None:
    import os

    if os.environ.get("CC_API_PWD"):
        return os.environ["CC_API_PWD"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("CC_API_PWD="):
                return line.split("=", 1)[1].strip()
    return None


def call(method: str, path: str, token: str | None = None, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def build(item: dict, pwd: str | None) -> dict:
    src = Path(item["file"])
    headers = {"x-filename": urllib.parse.quote(src.name), "content-type": "application/octet-stream"}
    if pwd:
        headers["x-vlm-password"] = pwd
    started = time.time()
    status, body = call("POST", "/api/documents", data=src.read_bytes(), headers=headers)
    if status != 201:
        return {"key": item["key"], "error": f"upload {status}: {body[:120]!r}"}
    doc = json.loads(body)
    token = doc["accessToken"]
    try:
        while True:
            if time.time() - started > JOB_TIMEOUT:
                return {"key": item["key"], "error": "gallery build deadline exceeded"}
            _, st = call("GET", f"/api/documents/{doc['id']}/status", token)
            st = json.loads(st)
            if st["status"] in ("completed", "failed"):
                break
            time.sleep(3)
        result = {"key": item["key"], "status": st["status"], "seconds": round(time.time() - started)}
        if st["status"] == "completed":
            _, full = call("GET", f"/api/documents/{doc['id']}", token)
            parsed = json.loads(full)["parsed"]
            _, pdf = call("GET", f"/api/documents/{doc['id']}/file", token)
            target = OUT / item["key"]
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True)
            (target / "source.pdf").write_bytes(pdf)
            parsed["document"]["filename"] = src.name
            (target / "parsed.json").write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf-8")
            meta = {k: st.get(k) for k in ("parser", "notices", "sourceFormat", "pageCount", "sourcePageCount")}
            meta |= {"originalName": src.name, "fileHash": hashlib.sha256(src.read_bytes()).hexdigest(),
                     "parserVersion": parsed["document"]["parserVersion"], "blocksFingerprint": parsed["blocksFingerprint"],
                     "offsetEncoding": parsed.get("offsetEncoding", "codepoint"),
                     "generatedAt": datetime.now(timezone.utc).isoformat(), "generatorRevision": GENERATOR_REVISION,
                     "vlmRequested": bool(pwd)}
            (target / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            result.update(meta)
        else:
            result["error"] = (st.get("error") or {}).get("code")
        return result
    finally:
        call("DELETE", f"/api/documents/{doc['id']}", token)


def main() -> None:
    global LIST, OUT, BASE, JOB_TIMEOUT, GENERATOR_REVISION
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("list", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("base_url", help="Explicit target, e.g. http://127.0.0.1:8312")
    parser.add_argument("--no-vlm", action="store_true", help="Do not read a password or request external picture reading")
    parser.add_argument("--job-timeout", type=int, default=1800)
    args = parser.parse_args()
    LIST, OUT, BASE, JOB_TIMEOUT = args.list, args.out, args.base_url.rstrip("/"), args.job_timeout
    try:
        GENERATOR_REVISION = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    items = json.loads(LIST.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    pwd = None if args.no_vlm else password()

    def safe(item: dict) -> dict:
        # One missing or broken file must not cost the other samples and the manifest.
        try:
            return build(item, pwd)
        except Exception as exc:  # noqa: BLE001
            return {"key": item["key"], "error": f"{type(exc).__name__}: {exc}"}

    # One parser job at a time keeps CPU/OCR timing comparable on small hosts.
    results = []
    for item in items:
        result = safe(item)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    done = {r["key"] for r in results if r.get("status") == "completed"}
    manifest = [{k: v for k, v in it.items() if k != "file"} | {"file": f"{it['key']}/source.pdf"}
                for it in items if it["key"] in done]
    (OUT / "build-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    if len(done) != len(items):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
