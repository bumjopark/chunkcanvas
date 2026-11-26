# Contributing to ChunkCanvas

Thanks for helping. Bug reports with a sample PDF (or a description of its layout) are the most
useful contributions; layout problems are hard to fix without one.

## Development setup

The verified development environment is Ubuntu 24.04 x86-64 with Node.js 24 and Python 3.12.
Follow the [Ubuntu setup](README.md#install-on-ubuntu) for the converter and isolation prerequisites.

```bash
# Backend
python3 -m venv services/api/.venv
services/api/.venv/bin/pip --isolated install -r services/api/requirements-build.lock
services/api/.venv/bin/pip --isolated install --no-build-isolation -r services/api/requirements.lock
services/api/.venv/bin/pip install -r services/api/requirements-dev.txt
scripts/download-models.sh

# Frontend
npm ci
```

Run the API and the Vite dev server in two terminals:

```bash
cd services/api && .venv/bin/uvicorn app.main:app --port 8300 --reload
npm run dev        # http://localhost:5173, proxies /api to :8300
```

## Tests

```bash
npm test                                          # chunk engine (packages/core)
npm run typecheck
(cd services/api && .venv/bin/python -m pytest)   # API, parsers, limits
npm run build && (cd apps/web && npx playwright install chromium && npx playwright test)
```

The Playwright suite starts its own API on port 8398 with a throwaway data directory.
It exercises password access by default. To exercise the per-upload choice on an open
server, run this additional test from `apps/web`:

```bash
CC_VLM_OPEN=true npx playwright test --grep 'open AI picture reading'
```

The LangChain parity fixtures in `packages/core/test/fixtures` come from the real library:

```bash
services/api/.venv/bin/pip install -r services/api/requirements-dev.txt
services/api/.venv/bin/python scripts/gen_splitter_fixtures.py
```

## Recording the README walkthrough

Build the web app and choose a free loopback port on a separate checkout. From
`services/api`, run the service launcher with a temporary data directory and a
gallery containing the public `pptx-ohio-standards` sample. The launcher mounts
the gallery read-only.

```bash
CC_GALLERY_DIR=/absolute/path/to/public-gallery CC_DATA_DIR="$(mktemp -d)" \
CC_MAX_UPLOAD_MB=64 CC_MAX_PAGES=20 CC_TRUNCATE_LONG_PDFS=true \
CC_WEB_DIST="$PWD/../../apps/web/dist" CC_API_PWD= CC_OPENAI_API_KEY= \
.venv/bin/python -m app.serve_isolated --port 8465
```

Capture only the 1280 × 820 browser viewport in English. Check that the upload area
shows the 64 MB limit and the cutoff at the first 20 pages. Open **PPTX → Ohio’s New
Learning Standards**, select its title on the slide, then click the gap below the
last block on the cover. Check that **Session Objectives** starts chunk 2. Open
**Export → Chunks (JSONL)** and check the download contains ten records. Save the
sequence as a looping GIF at `docs/images/walkthrough.gif`.

## Ground rules

- All chunking logic lives in `packages/core` (pure TypeScript, no React), including the
  splitters. The server never re-implements it; it only stores and validates split plans.
- Parsers convert their output to the common `ParsedDocument` model in
  `services/api/app/models.py`. Never send parser-specific formats to the frontend, and never
  invent coordinates for blocks the parser could not locate.
- Coordinates are normalized 0..1 in the displayed page frame (CropBox origin, /Rotate applied).
- Keep logs free of document text and file names.
- Add a test with every bug fix.
