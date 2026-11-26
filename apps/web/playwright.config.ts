import { defineConfig } from '@playwright/test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Runs against CC_E2E_URL if set; otherwise starts the API (serving apps/web/dist) on port 8398
// with a throwaway data directory. Build the web app first: npm run build -w apps/web.
const port = Number(process.env.CC_E2E_PORT ?? 8398);
const external = process.env.CC_E2E_URL;
const data = mkdtempSync(join(tmpdir(), 'chunkcanvas-e2e-'));

export default defineConfig({
  testDir: 'e2e',
  timeout: 120_000,
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: external ?? `http://127.0.0.1:${port}`,
    viewport: { width: 1440, height: 900 },
    acceptDownloads: true,
  },
  webServer: external
    ? undefined
    : {
        command: `../../services/api/.venv/bin/uvicorn app.main:app --app-dir ../../services/api --host 127.0.0.1 --port ${port} --no-access-log`,
        url: `http://127.0.0.1:${port}/api/health`,
        reuseExistingServer: false,
        timeout: 60_000,
        env: {
          CC_DATA_DIR: data,
          CC_WEB_DIST: new URL('./dist', import.meta.url).pathname,
          CC_DEFAULT_PARSER: 'pymupdf',
          CC_ALLOWED_HOSTS: '127.0.0.1,localhost',
          CC_MAX_ACTIVE_DOCUMENTS_PER_CLIENT: '100',
          CC_MAX_SAMPLE_DOCUMENTS_PER_CLIENT: '100',
          CC_UPLOADS_PER_CLIENT_PER_HOUR: '100',
          CC_SAMPLES_PER_CLIENT_PER_HOUR: '1000',
          // Uploads keep only their first page, so the truncation notice is exercised (samples are exempt).
          CC_MAX_PAGES: '1',
          CC_TRUNCATE_LONG_PDFS: 'true',
          CC_REPO_URL: 'https://example.com/chunkcanvas',
          // Picture reading is offered; no test sends a picture, so the key is never used.
          CC_OPENAI_API_KEY: 'e2e-unused',
          CC_OPENAI_BASE_URL: 'http://127.0.0.1:1/v1',
          CC_API_PWD: 'e2e-pass',
        },
      },
});
