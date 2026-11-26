import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';

const A = 'A'.repeat(24), B = 'B'.repeat(24);
const pdf = readFileSync(new URL('../../../samples/chunking-field-guide.pdf', import.meta.url));
const settings = { targetTokens: 500, maxTokens: 800, overlapTokens: 0, structureAware: true,
  keepHeadingWithBody: true, excludeFurniture: true };
const expiresAt = new Date(Date.now() + 3600000).toISOString();
const blocks = ['Cover page.', 'Middle paragraph.', 'End paragraph.'].map((text, i) => ({
  id: `b${i}`, readingOrder: i, type: 'paragraph', text,
  regions: [{ pageIndex: 0, bbox: { x0: .1, y0: .1 + i * .2, x1: .9, y1: .2 + i * .2 } }], source: { parser: 'test' },
}));
const payload = (id: string) => ({ id, filename: `${id[0]}.pdf`, status: 'completed', parser: 'pymupdf', pageCount: 2,
  expiresAt, createdAt: new Date().toISOString(), revision: 0, sample: false, notices: [], error: null,
  parsed: { schemaVersion: 1, offsetEncoding: 'utf16', document: { id, filename: 'review.pdf', fileHash: 'h', pageCount: 2,
    parser: 'pymupdf', parserVersion: '1' }, pages: [0, 1].map((index) => ({ index, width: 595, height: 842 })),
    blocks, blocksFingerprint: 'f' },
  splitPlan: { documentId: id, blocksFingerprint: 'f', revision: 0, settings, boundaries: [{ blockId: 'b0' }, { blockId: 'b1' }] },
});

async function fixtures(page: Page, save: Parameters<Page['route']>[1], getPayload = payload) {
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    let data;
    if (path === '/api/config') data = { version: 'test', defaultParser: 'pymupdf', parsers: [],
      limits: { maxUploadMb: 10, maxPages: 30, truncateLongPdfs: false, documentTtlSeconds: 3600 }, formats: ['pdf'], extensions: ['.pdf'] };
    else if (path === '/api/samples') data = [];
    else if (path.endsWith('/file')) return route.fulfill({ contentType: 'application/pdf', body: pdf });
    else if (path.endsWith('/split-plan')) return save(route, route.request());
    else data = getPayload(path.includes(A) ? A : B);
    return route.fulfill({ json: data });
  });
  await page.addInitScript(({ A, B, expiresAt }) => localStorage.setItem('chunkcanvas.documents',
    JSON.stringify([A, B].map((id) => ({ id, token: 'review', filename: `${id[0]}.pdf`, expiresAt })))), { A, B, expiresAt });
  await page.goto(`/d/${A}`);
  await expect(page.locator('[data-chunk-row="0"]')).toBeVisible();
}

async function deleteFirst(page: Page) {
  await page.locator('[data-chunk-row="0"]').click();
  await page.getByRole('button', { name: 'Delete chunk', exact: true }).click();
}

for (const status of [200, 404, 409]) {
  test(`late ${status} save from A cannot change B or retain A's PDF worker`, async ({ page }) => {
    let release!: () => void, started!: () => void, bRevision: number | undefined;
    const held = new Promise<void>((r) => { release = r; });
    const seen = new Promise<void>((r) => { started = r; });
    await fixtures(page, async (route) => {
      if (route.request().url().includes(A)) {
        started(); await held;
        return route.fulfill({ status, json: status === 200 ? { revision: 99 }
          : { detail: { code: status === 409 ? 'stale_revision' : 'not_found', revision: 99, splitPlan: payload(A).splitPlan } } });
      }
      bRevision = route.request().postDataJSON().baseRevision;
      return route.fulfill({ json: { revision: 1 } });
    });
    try {
      await deleteFirst(page); await seen;
      page.once('dialog', dialog => dialog.accept());
      await page.evaluate((id) => { history.pushState(null, '', `/d/${id}`); dispatchEvent(new PopStateEvent('popstate')); }, B);
      await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'saved');
      const response = page.waitForResponse((r) => r.url().includes(A) && r.url().endsWith('/split-plan'));
      release(); await response;
      await page.waitForTimeout(100); // let the old fetch's JSON continuation run
      if (status === 200) expect(await page.evaluate(id => Object.keys(localStorage)
        .filter(k => k.startsWith(`chunkcanvas.draft.${id}.`)).length, A)).toBe(0);
      await expect.poll(() => page.workers().length).toBe(1);
      await deleteFirst(page);
      await expect.poll(() => bRevision).toBe(0);
      await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'saved');
      await page.locator('[data-chunk-row="1"]').click();
      await expect(page.getByRole('button', { name: 'Merge with next', exact: true })).toBeEnabled();
    } finally { release(); }
  });
}

test('validation errors stop retrying until another edit', async ({ page }) => {
  let calls = 0;
  await fixtures(page, async (route) => {
    calls++;
    await route.fulfill({ status: 422, json: { detail: { code: 'invalid_plan', message: 'Review validation failure' } } });
  });
  await deleteFirst(page);
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'error');
  await page.waitForTimeout(4500);
  expect(calls).toBe(1);
});

test('HTTP origins without randomUUID can open the editor', async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(crypto, 'randomUUID', { value: undefined, configurable: true }));
  await fixtures(page, route => route.fulfill({ json: { revision: 1 } }));
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'saved');
});

test('recovery saved in a new tab leaves no abandoned draft after reload', async ({ page }) => {
  let saved: any = null, revision = 0;
  const raw = { documentId: A, blocksFingerprint: 'f', settings, boundaries: [{ blockId: 'b0' }], revision: 0 };
  await page.addInitScript(({ raw, expiresAt }) => {
    if (!sessionStorage.getItem('seeded-recovery')) {
      localStorage.setItem(`chunkcanvas.draft.${raw.documentId}.f.closed-tab`, JSON.stringify({
        plan: raw, baseRevision: 0, expiresAt, updatedAt: Date.now(),
      }));
      sessionStorage.setItem('seeded-recovery', 'yes');
    }
  }, { raw, expiresAt });
  await fixtures(page, async route => {
    const body = route.request().postDataJSON();
    saved = { ...body.plan, revision: ++revision };
    return route.fulfill({ json: { revision } });
  }, id => id === A ? { ...payload(A), revision, splitPlan: saved } : payload(id));
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'saved');
  expect(await page.evaluate(() => Object.keys(localStorage).filter(k => k.startsWith('chunkcanvas.draft.')))).toEqual([]);
  await page.reload();
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'saved');
  await expect(page.getByRole('button', { name: 'Keep mine', exact: true })).toHaveCount(0);
  expect((await exportedPlan(page)).boundaries).toEqual(raw.boundaries);
});

test('a lost save response is reconciled with identical server content before offering recovery', async ({ page }) => {
  let calls = 0;
  await page.addInitScript(({ plan, expiresAt }) => localStorage.setItem(`chunkcanvas.draft.${plan.documentId}.f.closed-tab`,
    JSON.stringify({ plan, baseRevision: 0, expiresAt, updatedAt: Date.now() })), { plan: payload(A).splitPlan, expiresAt });
  await fixtures(page, route => { calls++; return route.fulfill({ json: { revision: 10 } }); }, id => ({ ...payload(id),
    revision: 9, splitPlan: { ...payload(id).splitPlan, excluded: [], revision: 9 } }));
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'saved');
  await expect(page.getByTestId('recovery-bar')).toHaveCount(0);
  expect(await page.evaluate(() => Object.keys(localStorage).filter(k => k.startsWith('chunkcanvas.draft.')))).toEqual([]);
  expect(calls).toBe(0);
});

test('loading the server plan discards the recovered ancestor without resurrecting deletions', async ({ page }) => {
  let calls = 0;
  const raw = { ...payload(A).splitPlan, excluded: [{ blockId: 'b1', start: 0, end: blocks[1]!.text.length }] };
  await page.addInitScript(({ raw, expiresAt }) => {
    if (!sessionStorage.getItem('seeded-recovery')) {
      localStorage.setItem(`chunkcanvas.draft.${raw.documentId}.f.closed-tab`, JSON.stringify({
        plan: raw, baseRevision: 0, expiresAt, updatedAt: Date.now(),
      }));
      sessionStorage.setItem('seeded-recovery', 'yes');
    }
  }, { raw, expiresAt });
  await fixtures(page, route => { calls++; return route.fulfill({ json: { revision: 3 } }); }, id => ({
    ...payload(id), revision: 2, splitPlan: { ...payload(id).splitPlan, revision: 2 },
  }));
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'conflict');
  await expect(page.locator('[data-deleted-row]')).toHaveCount(1);
  await page.getByRole('button', { name: 'Load theirs', exact: true }).click();
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'saved');
  await expect(page.locator('[data-deleted-row]')).toHaveCount(0);
  await page.reload();
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'saved');
  expect((await exportedPlan(page)).excluded).toEqual([]);
  expect(calls).toBe(0);
});

test('an HTTP request timeout retries without requiring another edit', async ({ page }) => {
  let calls = 0;
  await fixtures(page, route => ++calls === 1
    ? route.fulfill({ status: 408, json: { detail: { code: 'request_timeout' } } })
    : route.fulfill({ json: { revision: 1 } }));
  await deleteFirst(page);
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'saved', { timeout: 10000 });
  expect(calls).toBe(2);
});

async function exportedPlan(page: Page) {
  await page.getByTestId('export-button').click();
  const [download] = await Promise.all([page.waitForEvent('download'), page.click('[data-export="plan"]')]);
  return JSON.parse(readFileSync((await download.path())!, 'utf-8'));
}

async function importPlan(page: Page, plan: unknown) {
  await page.getByTestId('export-button').click();
  await page.locator('input[type="file"][accept="application/json,.json"]').setInputFiles({
    name: 'review.split-plan.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(plan)),
  });
}

test('a changed parse imports content anchors without importing deletions', async ({ page }) => {
  let revision = 0;
  await fixtures(page, async (route) => route.fulfill({ json: { revision: ++revision } }));
  await deleteFirst(page);
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'saved');
  const file = await exportedPlan(page);
  expect(file.excluded.length).toBeGreaterThan(0);
  expect(file.blockAnchors).toHaveLength(2);
  expect(file.blockAnchors[0].textHash).toMatch(/^[a-f0-9]{64}$/);
  expect(file.blockAnchors[0].text).toBeUndefined();
  await page.keyboard.press('Control+z');
  await expect(page.locator('[data-deleted-row]')).toHaveCount(0);
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'saved');
  file.blocksFingerprint = 'older-parse';
  file.boundaries = file.boundaries.slice(0, 1);
  file.boundaries.forEach((b: { blockId: string }) => { b.blockId = `old_${b.blockId}`; });
  file.blockAnchors.forEach((b: { blockId: string }) => { b.blockId = `old_${b.blockId}`; });
  await importPlan(page, file);
  await expect(page.getByText(/Only boundaries with unique, identical block content/)).toBeVisible();
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'saved');
  const restored = await exportedPlan(page);
  expect(restored.boundaries).toEqual([{ blockId: 'b0' }]);
  expect(restored.excluded).toEqual([]);
});

test('a file import finishing after document navigation cannot overwrite the next document', async ({ page }) => {
  let bSaves = 0;
  await fixtures(page, async (route) => {
    if (route.request().url().includes(B)) bSaves++;
    return route.fulfill({ json: { revision: 1 } });
  });
  await page.evaluate(() => {
    const original = File.prototype.text;
    File.prototype.text = async function () {
      const text = await original.call(this);
      await new Promise<void>((resolve) => { (window as any).releaseReviewImport = resolve; });
      return text;
    };
  });
  const file = { schema: 'chunkcanvas.split-plan', schemaVersion: 2, fileHash: 'h', blocksFingerprint: 'f',
    settings, boundaries: [], excluded: [{ blockId: 'b0', start: 0, end: blocks[0]!.text.length }] };
  await importPlan(page, file);
  await page.waitForFunction(() => typeof (window as any).releaseReviewImport === 'function');
  await page.evaluate((id) => { history.pushState(null, '', `/d/${id}`); dispatchEvent(new PopStateEvent('popstate')); }, B);
  await expect(page.locator('span[title="B.pdf"]')).toBeVisible();
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status', 'saved');
  await page.evaluate(() => (window as any).releaseReviewImport());
  await page.waitForTimeout(800); // an incorrect apply would trigger the autosave debounce
  const untouched = await exportedPlan(page);
  expect(untouched.documentId).toBe(B);
  expect(untouched.boundaries).toEqual(payload(B).splitPlan.boundaries);
  expect(untouched.excluded).toEqual([]);
  expect(bSaves).toBe(0);
});

test('unsaved deletions survive reload and a changed server revision asks before overwriting', async ({ page }) => {
  let calls = 0;
  await fixtures(page, async route => {
    calls++;
    return route.fulfill({status:422,json:{detail:{code:'invalid_plan',message:'Temporarily invalid'}}});
  });
  await deleteFirst(page);
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status','error');
  expect(await page.evaluate(() => Object.keys(localStorage).filter(k => k.startsWith('chunkcanvas.draft.')).length)).toBe(1);
  page.once('dialog', dialog => dialog.accept());
  await page.reload();
  await expect(page.locator('[data-deleted-row]')).toHaveCount(1);
  await expect(page.getByTestId('recovery-bar')).toContainText('recovered');
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status','error');
  await page.route(`**/api/documents/${A}`, route => route.fulfill({json:{...payload(A),revision:10}}));
  const before = calls;
  page.once('dialog', dialog => dialog.accept());
  await page.reload();
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status','conflict');
  await page.waitForTimeout(800);
  expect(calls).toBe(before);
  await expect(page.locator('[data-deleted-row]')).toHaveCount(1);
});

test('declining internal navigation retains the URL and unsaved document', async ({ page }) => {
  await fixtures(page, route => route.fulfill({status:422,json:{detail:{code:'invalid_plan',message:'Invalid'}}}));
  await deleteFirst(page);
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status','error');
  page.once('dialog', dialog => dialog.dismiss());
  await page.evaluate((id) => {history.pushState({ccIndex:1},'',`/d/${id}`);dispatchEvent(new PopStateEvent('popstate'));},B);
  await expect(page).toHaveURL(new RegExp(`/d/${A}$`));
  await expect(page.locator('[data-deleted-row]')).toHaveCount(1);
});

test('server plans and saved preferences drop unknown settings; original malformed plan stays downloadable', async ({ page }) => {
  let saved: any;
  await fixtures(page, route => {saved=route.request().postDataJSON();return route.fulfill({json:{revision:1}});});
  await page.route(`**/api/documents/${A}`, route => route.fulfill({json:{...payload(A),splitPlan:{
    ...payload(A).splitPlan,settings:{...settings,tokenizer:'extra'},boundaries:[{blockId:'b1'},{blockId:'b0'},{blockId:'b2'}],
  }}}));
  await page.reload();
  await expect(page.getByTestId('recovery-bar')).toBeVisible();
  await expect(page.getByTestId('save-status')).toHaveAttribute('data-status','saved');
  expect(saved.plan.settings).not.toHaveProperty('tokenizer');
  await expect(page.getByTestId('recovery-bar')).toContainText('backup of the original plan');
  expect(saved.plan.boundaries).toEqual([{blockId:'b0'},{blockId:'b1'}]);
  const [download]=await Promise.all([page.waitForEvent('download'),page.getByRole('button',{name:'Download original plan'}).click()]);
  const original=JSON.parse(readFileSync((await download.path())!,'utf8'));
  expect(original.boundaries).toHaveLength(3);
  expect(original.schema).toBe('chunkcanvas.split-plan');
});

test('older line positions are explained once per document while changing chunks', async ({page}) => {
  await fixtures(page, route => route.fulfill({json:{revision:1}}), id => {
    const p = payload(id);
    return {...p, parsed:{...p.parsed, blocks: p.parsed.blocks.map(b => ({...b, lineGeometry:'unverified',
      lines:[{pageIndex:0, bbox:b.regions[0].bbox, start:0,end:b.text.length,xs:Array(b.text.length).fill(.1)}]}))}};
  });
  const notice = page.getByText('This older document has unverified line positions.', {exact:true});
  for (let i=0;i<3;i++) {
    await page.locator(`[data-chunk-row="${i}"]`).click();
    await expect(notice).toHaveCount(1);
    await expect(page.getByTestId('document-notes')).toContainText('unverified line positions');
  }
});

test('content matching hashes can be omitted from a shared plan', async ({ page }) => {
  await fixtures(page, route => route.fulfill({json:{revision:1}}));
  await page.getByTestId('export-button').click();
  await page.getByRole('switch',{name:'Include content matching hashes'}).click();
  const [download]=await Promise.all([page.waitForEvent('download'),page.click('[data-export="plan"]')]);
  const file=JSON.parse(readFileSync((await download.path())!,'utf8'));
  expect(file.blockAnchors).toBeUndefined();
  expect(file.offsetEncoding).toBe('utf16');
});

test('adjacent PDF pages are rendered before scrolling and distant bitmaps are released', async ({ page }) => {
  const longPdf=readFileSync(new URL('./fixtures/scroll.pdf',import.meta.url));
  await fixtures(page, route => route.fulfill({json:{revision:1}}));
  await page.route(`**/api/documents/${A}/file`,route=>route.fulfill({contentType:'application/pdf',body:longPdf}));
  await page.reload();
  await expect(page.locator('[data-page="11"]')).toHaveCount(1);
  const bitmap=(index:number)=>page.locator(`[data-page="${index}"] canvas`).evaluate((el:HTMLCanvasElement)=>el.width*el.height);
  await expect.poll(()=>bitmap(1)).toBeGreaterThan(0);
  const count=()=>page.locator('[data-page] canvas').evaluateAll(elements=>elements.filter(e=>(e as HTMLCanvasElement).width>0).length);
  expect(await count()).toBeLessThan(7);
  await page.getByTestId('pdf-scroll').evaluate(el=>el.scrollTop=el.scrollHeight);
  await expect.poll(()=>bitmap(11)).toBeGreaterThan(0);
  await expect.poll(()=>bitmap(0)).toBe(0);
  expect(await count()).toBeLessThan(7);
  await page.getByTestId('pdf-scroll').evaluate(el=>el.scrollTop=0);
  await expect.poll(()=>bitmap(1)).toBeGreaterThan(0);
  await expect.poll(()=>bitmap(11)).toBe(0);
});

test('opening the app removes expired names and credentials from browser storage', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('chunkcanvas.documents', JSON.stringify([
    { id: 'expired', token: 'expired-token', filename: 'expired-private-name.pdf', expiresAt: new Date(0).toISOString() },
    { id: 'current', token: 'current-token', filename: 'current.pdf', expiresAt: new Date(Date.now() + 3600000).toISOString() },
  ])));
  await page.goto('/');
  await expect.poll(() => page.evaluate(() => localStorage.getItem('chunkcanvas.documents'))).not.toContain('expired');
  const raw = await page.evaluate(() => JSON.parse(localStorage.getItem('chunkcanvas.documents')!));
  expect(raw.map((item: { id: string }) => item.id)).toEqual(['current']);
});

test('the picture-reading notice identifies the provider and preceding text in both languages', async ({ page }) => {
  for (const [lang, wording, offNotice] of [
    ['en', 'the paragraph immediately before it to the configured model provider', 'your document stays on this server'],
    ['ko', '그림 바로 앞 문단의 일부를 서버에 설정된 모델 제공자에게', '문서 내용은 이 서버에서만 처리됩니다'],
  ]) {
    await page.goto('/');
    await page.evaluate(value => localStorage.setItem('chunkcanvas.lang', value), lang);
    await page.reload();
    await expect(page.getByText(wording, { exact: false })).toBeVisible();
    await expect(page.getByText(offNotice, { exact: false })).toBeVisible();
  }
});
