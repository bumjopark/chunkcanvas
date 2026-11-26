import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';

const samplePdf = new URL('../../../samples/chunking-guide-ko.pdf', import.meta.url).pathname;

async function showGuides(page: Page) {
  const tab = page.locator('[data-sample-group="guide"]');
  if (await tab.count()) await tab.click();
}

async function openSample(page: Page, key = 'chunking-field-guide') {
  await page.goto('/');
  await expect(page.getByTestId('auto-pipeline')).toBeVisible();
  await showGuides(page);
  await page.click(`[data-sample="${key}"]`);
  await expect(page.locator('[data-chunk-row]').first()).toBeVisible({ timeout: 90_000 });
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'saved');
}

const chunkCount = (page: Page) => page.locator('[data-chunk-row]').count();
const saved = (page: Page) => expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'saved');

test('home page offers upload with automatic parsing, and samples', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('dropzone')).toBeVisible();
  await expect(page.getByTestId('dropzone')).toContainText('longer documents are cut to the first 1 pages');
  await expect(page.getByTestId('auto-pipeline')).toBeVisible();
  await expect(page.locator('[data-parser]')).toHaveCount(0);
  await showGuides(page);
  await expect(page.locator('[data-sample]')).toHaveCount(2);
  await expect(page.getByText('deleted automatically after 1 hour')).toBeVisible();
});

test('chunk overlays stay aligned with blocks when zooming', async ({ page }) => {
  await openSample(page);
  const region = page.locator('[data-block]').first();
  const pageEl = page.locator('[data-page="0"]');
  const relative = async () => {
    const r = (await region.boundingBox())!;
    const p = (await pageEl.boundingBox())!;
    return [(r.x - p.x) / p.width, (r.y - p.y) / p.height, r.width / p.width];
  };
  const before = await relative();
  await page.getByRole('button', { name: 'Zoom in' }).click();
  await page.getByRole('button', { name: 'Zoom in' }).click();
  const after = await relative();
  // Region padding is a few CSS pixels, so allow a small tolerance.
  for (let i = 0; i < 3; i++) expect(Math.abs(after[i]! - before[i]!)).toBeLessThan(0.01);
});

test('add, remove, undo and redo boundaries; edits persist across reload', async ({ page }) => {
  await openSample(page);
  const n = await chunkCount(page);
  const boundaries = page.locator('[data-boundary]');
  await expect(boundaries).toHaveCount(n - 1);

  const zone = page.locator('[data-split-after]').nth(3);
  await zone.scrollIntoViewIfNeeded();
  await zone.click();
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n + 1);
  await saved(page);

  await page.reload();
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n + 1, { timeout: 30_000 });

  const handle = page.locator('[data-boundary]').first();
  await handle.scrollIntoViewIfNeeded();
  await handle.hover();
  await handle.getByRole('button', { name: /Remove boundary/ }).click();
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n);

  await page.keyboard.press('Control+z');
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n + 1);
  await page.keyboard.press('Control+Shift+z');
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n);
  await saved(page);
});

test('dragging a boundary moves blocks between neighbouring chunks', async ({ page }) => {
  await openSample(page);
  const n = await chunkCount(page);
  const firstTokens = async () => Number(await page.locator('[data-chunk-row="0"] .tabular-nums').innerText());
  const before = await firstTokens();

  const boundary = page.locator('[data-boundary]').first();
  await boundary.scrollIntoViewIfNeeded();
  const grip = boundary.getByRole('button', { name: /Drag/ });
  const box = (await grip.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  // Move up by roughly one paragraph so the boundary snaps to an earlier gap.
  for (let i = 1; i <= 10; i++) await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2 - i * 12);
  await page.mouse.up();

  await expect(page.locator('[data-chunk-row]')).toHaveCount(n);
  await expect.poll(firstTokens).toBeLessThan(before);
  await page.keyboard.press('Control+z');
  await expect.poll(firstTokens).toBe(before);
});

test('select a chunk on the page, inspect and merge it', async ({ page }) => {
  await openSample(page);
  const n = await chunkCount(page);
  await page.locator('[data-block][data-chunk="1"]').first().click();
  await expect(page.getByRole('heading', { name: /Chunk 2/ })).toBeVisible();
  await page.getByRole('button', { name: 'Merge with next' }).click();
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n - 1);
  await page.getByRole('button', { name: /^Text$/ }).click();
  await expect(page.locator('pre').first()).toContainText('Common failure modes');
});

test('exports JSONL, JSON and Markdown that match the editor', async ({ page }) => {
  await openSample(page);
  const n = await chunkCount(page);
  const download = async (kind: string) => {
    await page.getByTestId('export-button').click();
    const [d] = await Promise.all([page.waitForEvent('download'), page.click(`[data-export="${kind}"]`)]);
    return readFileSync((await d.path())!, 'utf-8');
  };
  const jsonl = (await download('jsonl')).trim().split('\n').map((l) => JSON.parse(l));
  expect(jsonl).toHaveLength(n);
  expect(jsonl[0].metadata).toMatchObject({ chunkIndex: 0, tokenizer: 'cl100k_base', sourceFile: 'chunking-field-guide.pdf' });
  const json = JSON.parse(await download('json'));
  expect(json.chunks).toHaveLength(n);
  expect(json.chunks.map((c: { text: string }) => c.text)).toEqual(jsonl.map((c) => c.text));
  expect(json.schemaVersion).toBe(3);
  expect(json.sections.length).toBeGreaterThan(0);
  for (const [i, chunk] of json.chunks.entries()) {
    const pathIds = chunk.headingPath.map((h: { blockId: string }) => h.blockId);
    expect(jsonl[i].metadata.headingBlockIds).toEqual(pathIds);
    expect(jsonl[i].metadata.headingPath).toEqual(chunk.headingPath.map((h: { text: string }) => h.text));
    expect(jsonl[i].metadata.sectionIds).toEqual(chunk.sectionIds);
    let parent: string | null = null;
    for (const heading of chunk.headingPath) {
      const section = json.sections.find((s: { blockId: string }) => s.blockId === heading.blockId);
      expect(section.parentBlockId).toBe(parent);
      expect(section.level).toBe(heading.level);
      parent = heading.blockId;
    }
  }
  const md = await download('md');
  expect(md).toContain('## Chunk 1');
  const plan = JSON.parse(await download('plan'));
  expect(plan.schema).toBe('chunkcanvas.split-plan');
  expect(plan.boundaries).toHaveLength(n - 1);
  expect(plan.schemaVersion).toBe(2);
});

test('a deleted chunk leaves every export, stays deleted after reload, and comes back', async ({ page }) => {
  await openSample(page);
  const n = await chunkCount(page);
  const download = async (kind: string) => {
    await page.getByTestId('export-button').click();
    const [d] = await Promise.all([page.waitForEvent('download'), page.click(`[data-export="${kind}"]`)]);
    return readFileSync((await d.path())!, 'utf-8');
  };
  const before = (await download('jsonl')).trim().split('\n').map((l) => JSON.parse(l));
  const gone = before[1].text as string;

  await page.locator('[data-block][data-chunk="1"]').first().click();
  await expect(page.getByRole('heading', { name: /Chunk 2/ })).toBeVisible();
  await page.keyboard.press('Delete');
  await expect(page.getByRole('heading', { name: 'Deleted chunk' })).toBeVisible();
  await expect(page.locator('[data-deleted-row]')).toHaveCount(1);
  await expect(page.locator('[data-block][data-chunk="1"][data-deleted]').first()).toBeVisible();
  await saved(page);

  const jsonl = (await download('jsonl')).trim().split('\n').map((l) => JSON.parse(l));
  expect(jsonl).toHaveLength(n - 1);
  expect(jsonl.map((c) => c.metadata.chunkIndex)).toEqual([...Array(n - 1).keys()]);
  expect(jsonl.some((c) => c.text.includes(gone))).toBe(false);
  expect(await download('md')).not.toContain(gone);
  const plan = JSON.parse(await download('plan'));
  expect(plan.excluded.length).toBeGreaterThan(0);

  // Saved with the plan: still deleted after a reload.
  await page.reload();
  await expect(page.locator('[data-deleted-row]')).toHaveCount(1, { timeout: 30_000 });
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n);

  await page.locator('[data-restore="1"]').click();
  await expect(page.locator('[data-deleted-row]')).toHaveCount(0);
  await page.locator('[data-block][data-chunk="1"]').first().click();
  await page.getByRole('button', { name: 'Delete chunk' }).click();
  await expect(page.locator('[data-deleted-row]')).toHaveCount(1);
  await page.keyboard.press('Control+z');
  await expect(page.locator('[data-deleted-row]')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: /Chunk 2/ })).toBeVisible();
  await saved(page);
  expect(JSON.parse(await download('plan')).excluded).toEqual([]);
});

test('upload a PDF, wait for parsing and open the editor', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('file-input').setInputFiles({ name: '한글 문서.pdf', mimeType: 'application/pdf', buffer: readFileSync(samplePdf) });
  await expect(page.locator('[data-chunk-row]').first()).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText('한글 문서.pdf')).toBeVisible();
  await expect(page.locator('[data-block]').first()).toBeVisible();
  // The 2-page PDF was cut to the 1-page limit: one page rendered, a notice and the code link.
  await expect(page.getByTestId('truncated-bar')).toContainText('first 1 of 2 pages');
  await expect(page.getByTestId('truncated-bar').getByRole('link', { name: 'Get the code' })).toHaveAttribute('href', 'https://example.com/chunkcanvas');
  await expect(page.locator('[data-page]')).toHaveCount(1);
});

test('unknown or expired documents show a clear message', async ({ page }) => {
  await page.goto('/d/AAAAAAAAAAAAAAAAAAAAAAAA');
  await expect(page.getByText('This document is not available')).toBeVisible();
});

test('a document that expires mid-edit keeps the editor open for export', async ({ page }) => {
  await openSample(page);
  const id = page.url().split('/d/')[1]!;
  const token = await page.evaluate(
    (docId) => JSON.parse(localStorage.getItem('chunkcanvas.documents') ?? '[]').find((d: { id: string }) => d.id === docId).token,
    id,
  );
  const del = await page.request.delete(`/api/documents/${id}`, { headers: { Authorization: `Bearer ${token}` } });
  expect(del.status()).toBe(204);

  const n = await chunkCount(page);
  const zone = page.locator('[data-split-after]').nth(2);
  await zone.scrollIntoViewIfNeeded();
  await zone.click();
  await expect(page.getByRole('alert')).toContainText('expired');
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'expired');
  // Further edits stay local: the status and the banner must not flip back to "saving".
  const zone2 = page.locator('[data-split-after]').nth(6);
  await zone2.scrollIntoViewIfNeeded();
  await zone2.click();
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n + 2);
  await page.waitForTimeout(1500);
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'expired');
  await expect(page.getByRole('alert')).toBeVisible();
  await page.getByTestId('export-button').click();
  const [d] = await Promise.all([page.waitForEvent('download'), page.click('[data-export="jsonl"]')]);
  expect(readFileSync((await d.path())!, 'utf-8').trim().split('\n')).toHaveLength(n + 2);
});

test('sample copies can be removed from the recent list', async ({ page }) => {
  await openSample(page);
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Delete from server' }).first()).toBeVisible();
  const before = await page.getByRole('button', { name: 'Delete from server' }).count();
  expect(before).toBeGreaterThan(0);
  await page.getByRole('button', { name: 'Delete from server' }).first().click();
  await expect(page.getByRole('button', { name: 'Delete from server' })).toHaveCount(before - 1);
});

test('split inside a paragraph at a sentence, then move it and export character offsets', async ({ page }) => {
  await openSample(page);
  const n = await chunkCount(page);
  await page.locator('[data-chunk-row="0"]').click();
  const ticks = page.locator('[data-split-inner]');
  await expect(ticks.first()).toBeVisible();
  await ticks.first().scrollIntoViewIfNeeded();
  await ticks.first().click();
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n + 1);
  const inner = page.locator('[data-boundary*="@"]');
  await expect(inner).toHaveCount(1);
  const before = await inner.getAttribute('data-boundary');
  await saved(page);

  await page.reload();
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n + 1, { timeout: 30_000 });
  await expect(page.locator('[data-boundary*="@"]')).toHaveAttribute('data-boundary', before!);

  // Drag the inner boundary further down: it snaps to another sentence or block gap.
  const keys = () => page.locator('[data-boundary]').evaluateAll((els) => els.map((e) => e.getAttribute('data-boundary')));
  const beforeKeys = await keys();
  const grip = page.locator('[data-boundary*="@"]').getByRole('button', { name: /Drag/ });
  await grip.scrollIntoViewIfNeeded();
  const box = (await grip.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  for (let i = 1; i <= 8; i++) await page.mouse.move(box.x + 120 + i * 10, box.y + box.height / 2 + i * 6);
  await page.mouse.up();
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n + 1);
  await expect.poll(keys).not.toEqual(beforeKeys);
  await page.keyboard.press('Control+z');
  await expect.poll(keys).toEqual(beforeKeys);

  await page.getByTestId('export-button').click();
  const [d] = await Promise.all([page.waitForEvent('download'), page.click('[data-export="jsonl"]')]);
  const lines = readFileSync((await d.path())!, 'utf-8').trim().split('\n').map((l) => JSON.parse(l));
  expect(lines).toHaveLength(n + 1);
  const all = lines.flatMap((l) => l.metadata.segments as { blockId: string; start: number; end: number }[]);
  // A block split in two appears in consecutive chunks with adjoining ranges.
  const splitBlock = all.find((s) => s.start > 0)!;
  const head = all.find((s) => s.blockId === splitBlock.blockId && s.end === splitBlock.start);
  expect(head).toBeTruthy();
});

test('a table is cut between rows and each piece repeats its header', async ({ page }) => {
  await page.goto('/');
  const rows = Array.from({ length: 12 }, (_, i) => `| 항목 ${i + 1} | 값 ${(i + 1) * 7} |`).join("\n");
  const markdown = `# 표 분할\n\n| 이름 | 값 |\n| --- | --- |\n${rows}\n`;
  await page.getByTestId('file-input').setInputFiles({ name: '표.md', mimeType: 'text/markdown', buffer: Buffer.from(markdown) });
  await expect(page.locator('[data-chunk-row]').first()).toBeVisible({ timeout: 120_000 });
  await saved(page);
  const n = await chunkCount(page);
  await page.locator('[data-chunk-row="0"]').click();
  const ticks = page.locator('[data-split-inner]');
  await expect(ticks.first()).toBeVisible();
  await ticks.nth(4).scrollIntoViewIfNeeded();
  await ticks.nth(4).click();
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n + 1);
  await saved(page);

  await page.getByTestId('export-button').click();
  const [d] = await Promise.all([page.waitForEvent('download'), page.click('[data-export="json"]')]);
  const exp = JSON.parse(readFileSync((await d.path())!, 'utf-8'));
  const pieces = (exp.chunks as { text: string }[]).filter((c) => c.text.includes('| 항목'));
  expect(pieces.length).toBe(2);
  for (const c of pieces) expect(c.text).toMatch(/\| 이름 \| 값 \|\n\| --- \| --- \|\n\| 항목/);
  // No row is lost or cut: together the pieces hold every row once.
  const held = pieces.flatMap((c) => c.text.split('\n').filter((l) => l.startsWith('| 항목')));
  expect(held).toEqual(rows.split('\n'));

  // Undo puts the table back in one chunk.
  await page.keyboard.press('Control+z');
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n);
});

test('the inspector splits a paragraph before a chosen sentence', async ({ page }) => {
  await openSample(page);
  const n = await chunkCount(page);
  await page.locator('[data-chunk-row="0"]').click();
  await page.getByRole('button', { name: /^Blocks/ }).click();
  await page.locator('[data-inspector-split]').first().click();
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n + 1);
  await page.keyboard.press('Control+z');
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n);
});

test('compare splitters, preview one with its overlap, then edit from it', async ({ page }) => {
  await openSample(page);
  const n = await chunkCount(page);
  await page.click('[data-tab="strategies"]');
  await expect(page.locator('[data-strategy]')).toHaveCount(6);
  const recursive = page.locator('[data-strategy="recursive"]');
  await recursive.locator('input').nth(0).fill('400');
  await recursive.locator('input').nth(1).fill('80');
  await page.click('[data-strategy-preview="recursive"]');
  await expect(page.getByTestId('strategy-bar')).toBeVisible();
  await expect(page.locator('[data-overlap]').first()).toBeVisible();
  const barText = await page.getByTestId('strategy-bar').innerText();
  const previewCount = Number(/(\d+) chunks/.exec(barText)![1]);
  await page.click('[data-tab="chunks"]');
  await expect(page.locator('[data-preview-row]')).toHaveCount(previewCount);
  // Preview is read-only: no boundary handles.
  await expect(page.locator('[data-boundary]')).toHaveCount(0);

  await page.getByTestId('strategy-bar').getByRole('button', { name: 'Edit from this split' }).click();
  await expect(page.getByTestId('strategy-bar')).toHaveCount(0);
  const edited = await chunkCount(page);
  expect(edited).toBeGreaterThan(n);
  expect(edited).toBeLessThanOrEqual(previewCount);
  await saved(page);

  // The splitter's overlap carries over to exports.
  await page.getByTestId('export-button').click();
  const [d] = await Promise.all([page.waitForEvent('download'), page.click('[data-export="jsonl"]')]);
  const lines = readFileSync((await d.path())!, 'utf-8').trim().split('\n').map((l) => JSON.parse(l));
  expect(lines).toHaveLength(edited);
  expect(lines[1].metadata.overlapTokens).toBeGreaterThan(0);

  await page.keyboard.press('Control+z');
  await expect(page.locator('[data-chunk-row]')).toHaveCount(n);
});

test('switching the tokenizer recounts every chunk', async ({ page }) => {
  await openSample(page, 'chunking-guide-ko');
  const first = () => page.locator('[data-chunk-row="0"] .tabular-nums').innerText().then(Number);
  const before = await first();
  await page.getByRole('button', { name: 'Chunking' }).click();
  await page.getByTestId('tokenizer-select').selectOption('o200k_base');
  await expect.poll(first).toBeLessThan(before);
  await expect(page.getByText('Token counts use o200k_base.')).toBeVisible();
});

test('non-PDF uploads are converted and edited like PDFs', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('dropzone')).toContainText('HWP/HWPX');
  const markdown = '# 변환 테스트\n\n첫 문단입니다. 두 번째 문장도 있습니다.\n\n## 둘째 절\n\n- 항목 하나\n- 항목 둘\n';
  await page.getByTestId('file-input').setInputFiles({ name: '노트.md', mimeType: 'text/markdown', buffer: Buffer.from(markdown) });
  await expect(page.locator('[data-chunk-row]').first()).toBeVisible({ timeout: 120_000 });
  await expect(page.getByTestId('document-notes')).toContainText('Converted from MD');
  await expect(page.getByText('노트.md')).toBeVisible();
  await expect(page.locator('[data-block]').first()).toBeVisible();
  await page.getByTestId('export-button').click();
  const [d] = await Promise.all([page.waitForEvent('download'), page.click('[data-export="json"]')]);
  const exp = JSON.parse(readFileSync((await d.path())!, 'utf-8'));
  expect(exp.document.sourceFormat).toBe('md');
  expect(exp.chunks.map((c: { text: string }) => c.text).join('\n')).toContain('변환 테스트');
});

test('AI picture reading is unlocked with the password and stays on for the tab', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('auto-pipeline')).toBeVisible();
  const section = page.getByTestId('vlm');
  test.skip(!(await section.isVisible()), 'picture reading not configured on this server');
  await page.getByTestId('vlm-password').fill('wrong');
  await section.getByRole('button').click();
  await expect(page.getByTestId('vlm-error')).toBeVisible();
  await page.getByTestId('vlm-password').fill('e2e-pass');
  await section.getByRole('button').click();
  await expect(page.getByTestId('vlm-on')).toBeVisible();
  await page.reload();
  await expect(page.getByTestId('vlm-on')).toBeVisible();
  // An upload carries the password; a document without pictures sends nothing out.
  await page.getByTestId('file-input').setInputFiles(samplePdf);
  await expect(page.locator('[data-chunk-row]').first()).toBeVisible({ timeout: 180_000 });
});

test('gallery samples open at once, grouped by file type', async ({ page }) => {
  await page.goto('/');
  const hwpx = page.locator('[data-sample-group="hwpx"]');
  // The sample list loads after the page: wait for it before deciding there is no gallery.
  await page.locator('[data-sample-group]').first().waitFor({ timeout: 15_000 }).catch(() => {});
  test.skip(!(await hwpx.count()), 'no sample gallery on this server');
  await hwpx.click();
  const first = page.locator('[data-sample]').first();
  await expect(first).toContainText('hwpx');
  await first.click();
  await expect(page.locator('[data-chunk-row]').first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('document-notes')).toContainText('HWPX');
});
