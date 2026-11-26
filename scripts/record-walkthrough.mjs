import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { copyFileSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium, expect } from '@playwright/test';

const server = new URL(process.argv[2] ?? 'http://127.0.0.1:8465');
assert(server.protocol === 'http:' && server.hostname === '127.0.0.1' && server.port,
  'Use the loopback URL of a separate recording server.');
const browser = await chromium.launch();
const folder = mkdtempSync(join(tmpdir(), 'chunkcanvas-walkthrough-'));
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, locale: 'en-US', colorScheme: 'light' });
  await page.route(/^https?:/, route => new URL(route.request().url()).origin === server.origin
    ? route.continue() : route.abort());
  await page.goto(server.origin);
  await expect(page.getByText('Up to 64 MB · longer documents are cut to the first 20 pages')).toBeVisible();
  await expect(page.getByTestId('vlm-password')).toHaveCount(0);
  await page.evaluate(() => document.fonts.ready.then(() => {}));

  // Chromium omits its native cursor from screenshots. Mirror the real pointer events.
  await page.addStyleTag({ content: `
    #record-pointer { position: fixed; top: -12px; left: -12px; width: 26px; height: 26px;
      border: 2px solid #0876c7; border-radius: 50%; background: #fff9; transform: translate(-50px,-50px); }
    #record-pointer.held { background: #ffca57bb; border-color: #6c4000; }
    #record-pointer::after { content: ''; position: absolute; top: 10px; left: 10px; width: 13px; height: 18px;
      background: #172032; clip-path: polygon(0 0,100% 65%,55% 67%,36% 100%); }
    #record-caption { position: fixed; bottom: 0; left: 0; right: 0; padding: 7px 20px;
      background: #192138; color: white; font: 600 17px/1.35 system-ui, sans-serif; }
    #record-pointer, #record-caption { z-index: 2147483647; pointer-events: none; }` });
  await page.evaluate(() => {
    const pointer = document.createElement('div');
    pointer.id = 'record-pointer';
    const caption = document.createElement('div');
    caption.id = 'record-caption';
    document.body.append(pointer, caption);
    document.addEventListener('pointermove', e => {
      pointer.style.transform = `translate(${e.clientX}px, ${e.clientY}px)`;
    }, true);
    document.addEventListener('pointerdown', () => pointer.classList.add('held'), true);
    document.addEventListener('pointerup', () => pointer.classList.remove('held'), true);
  });

  const frames = [];
  async function capture(seconds, caption) {
    if (caption) await page.locator('#record-caption').evaluate((el, text) => { el.textContent = text; }, caption);
    const name = `frame-${frames.length}.png`;
    await page.screenshot({ path: join(folder, name) });
    frames.push({ name, seconds });
  }
  async function center(locator) {
    const box = await locator.boundingBox();
    assert(box, 'Expected a visible control');
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  }

  await capture(2, 'Open a public sample to edit its chunk boundaries.');
  const group = page.locator('[data-sample-group="pptx"]');
  await group.click();
  const sample = page.locator('[data-sample="pptx-ohio-standards"]');
  await sample.hover();
  await capture(1.2);
  await sample.click();
  const slide = page.locator('[data-page="1"]');
  await expect(page.locator('[data-page="0"] canvas').first()).toBeVisible();
  await expect(page.locator('[data-chunk-row]').first()).toBeVisible();
  await capture(1.2, 'The editor draws the existing chunks on the slides.');
  await slide.evaluate(el => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
  await expect(slide.locator('canvas').first()).toBeVisible();
  const gap = id => slide.locator(`[data-split-after="${id}"]`);
  const chunks = page.locator('[data-chunk-row]');
  const originalCount = await chunks.count();
  await gap('b0006').hover();
  await capture(1.8, 'Click between two blocks to create a new chunk.');
  await gap('b0006').click();
  await expect(chunks).toHaveCount(originalCount + 1);
  await page.locator('[data-block="b0007"]').first().click();

  const inspector = page.locator('aside').last();
  const preview = inspector.locator('.scroll-thin');
  const count = inspector.locator('b.tabular-nums');
  const originalText = await preview.innerText();
  const counts = new Set([await count.innerText()]);
  const from = await center(page.locator('[data-boundary="b0006"] button').first());
  const to = await center(gap('b0004'));
  await page.mouse.move(from.x, from.y);
  await capture(1.8, 'Select the new chunk. Preview shows its current text and token count.');
  await page.mouse.down();
  await capture(0.48, 'Drag the boundary up. Watch the text and tokens change on the right.');
  const steps = 36;
  for (let i = 1; i <= steps; i++) {
    await page.mouse.move(from.x + (to.x - from.x) * i / steps, from.y + (to.y - from.y) * i / steps);
    await capture(0.08);
    counts.add(await count.innerText());
  }
  assert(counts.size >= 2, 'The inspector token count must change before release.');
  assert.notEqual(await preview.innerText(), originalText);
  await expect(preview).toContainText('Overview of Ohio');
  await capture(1.2);
  await page.mouse.up();
  await capture(1.4, 'Preview shows the expanded chunk with its original formatting.');

  await inspector.getByRole('button', { name: 'Text', exact: true }).click();
  await expect(preview.locator('pre')).toContainText('Overview of Ohio');
  await capture(2.4, 'Text shows the plain text of the same chunk.');
  await inspector.getByRole('button', { name: /^Blocks/ }).click();
  await expect(preview.locator('ol > li').first()).toBeVisible();
  await capture(2.8, 'Blocks lists the source blocks in reading order.');

  const list = frames.flatMap(({ name, seconds }) => [`file ${name}`, `duration ${seconds}`]);
  list.push(`file ${frames.at(-1).name}`);
  writeFileSync(join(folder, 'frames.ffconcat'), list.join('\n') + '\n');
  execFileSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat', '-i', 'frames.ffconcat',
    '-filter_complex_threads', '1', '-filter_complex',
    '[0:v]split[a][b];[a]palettegen[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle',
    '-fps_mode', 'vfr', '-loop', '0', '-final_delay', '4', 'walkthrough.gif'], { cwd: folder });
  copyFileSync(join(folder, 'walkthrough.gif'), new URL('../docs/images/walkthrough.gif', import.meta.url));
  console.log(`${frames.length} captures; chunks ${originalCount} -> ${originalCount + 1}; tokens while dragging: ${[...counts].join(' -> ')}`);
} finally {
  await browser.close();
  rmSync(folder, { recursive: true, force: true });
}
