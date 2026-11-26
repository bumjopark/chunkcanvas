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
CC_WEB_DIST="$PWD/../../apps/web/dist" CC_VLM_OPEN=true CC_API_PWD= \
CC_OPENAI_BASE_URL=http://127.0.0.1:1/v1 CC_OPENAI_API_KEY=recording-only \
.venv/bin/python -m app.serve_isolated --port 8465
```

With FFmpeg on your path, run these commands from the checkout root in another terminal:

```bash
npx playwright install chromium
node scripts/record-walkthrough.mjs http://127.0.0.1:8465
```

The script records an English 1280 × 820 viewport. It checks the upload limits,
opens the public slide deck and clicks between two blocks to split a chunk.
It captures the mouse while dragging the new boundary and checks that both
the token count and the preview text change before release. It then opens the
Text and Blocks tabs and writes the looping GIF to `docs/images/walkthrough.gif`.
The sample already contains its figure descriptions.

## Ground rules

- All chunking logic lives in `packages/core` (pure TypeScript, no React), including the
  splitters. The server never re-implements it; it only stores and validates split plans.
- Parsers convert their output to the common `ParsedDocument` model in
  `services/api/app/models.py`. Never send parser-specific formats to the frontend, and never
  invent coordinates for blocks the parser could not locate.
- Coordinates are normalized 0..1 in the displayed page frame (CropBox origin, /Rotate applied).
- Keep logs free of document text and file names.
- Add a test with every bug fix.
