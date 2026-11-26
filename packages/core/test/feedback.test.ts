import { expect, it } from 'vitest';
import { DEFAULT_SETTINGS, normalizeChunkSettings, normalizeSavedPlan, importSplitPlan, toSplitPlanFile,
  buildJsonExport, toJsonl, validatePlan, type SplitPlan } from '../src';
import { block, doc, lined, words } from './fixtures';
const d = doc([lined('b1', 'alpha beta gamma'), block('b2', 'paragraph', 'Atomic without lines'),
  block('b3', 'paragraph', 'End'), block('b4', 'page_footer', 'Footer')]);
const plan: SplitPlan = { documentId: 'doc1', blocksFingerprint: 'fp1', revision: 7,
  settings: DEFAULT_SETTINGS, boundaries: [{ blockId: 'b1' }], excluded: [{ blockId: 'b3', start: 0, end: 3 }] };
it('allowlists settings in preferences, imported plans, and server plans', () => {
  const settings = { ...DEFAULT_SETTINGS, tokenizer: 'old extra key' };
  expect(normalizeChunkSettings(settings)).toEqual(DEFAULT_SETTINGS);
  expect(importSplitPlan(d, plan, { ...toSplitPlanFile(d, plan), settings }).plan.settings).toEqual(DEFAULT_SETTINGS);
  const loaded = normalizeSavedPlan(d, { ...plan, settings })!;
  expect(loaded.plan.settings).toEqual(DEFAULT_SETTINGS);
  expect(loaded.changed).toBe(true);
});
it('normalizes server order/furniture/last boundary while preserving revision and deletions', () => {
  const loaded = normalizeSavedPlan(d, { ...plan, boundaries: ['b2','b4','b1','b3'].map(blockId => ({ blockId })) })!;
  expect(loaded.plan.boundaries).toEqual([{blockId:'b1'}, {blockId:'b2'}]);
  expect(loaded.plan.excluded).toEqual(plan.excluded);
  expect(loaded.plan.revision).toBe(7);
  expect(validatePlan(d.blocks, loaded.plan)).toEqual([]);
  expect(loaded.changed).toBe(true);
});
it('reports interior cuts which cannot retain their positions instead of snapping them', () => {
  const raw = { ...toSplitPlanFile(d, plan), boundaries: [{blockId:'b2',offset:4}] };
  const imported = importSplitPlan(d, plan, raw);
  expect(imported.plan.boundaries).toEqual([]);
  expect(imported.warnings.join()).toContain('original positions');
  expect(normalizeSavedPlan(d, {...plan,boundaries:raw.boundaries})!.plan.boundaries).toEqual([]);
});
it('exports a stored API plan through Core normalization, including excluded furniture', () => {
  const raw = {...plan, boundaries:[{blockId:'b3'},{blockId:'b4'},{blockId:'b1'}]};
  expect(() => buildJsonExport(d,raw,words,{tokenizer:'words'})).toThrow();
  const ready = normalizeSavedPlan(d,raw)!;
  expect(ready.changed).toBe(true);
  expect(ready.warnings.length).toBeGreaterThan(0);
  expect(() => buildJsonExport(d,ready.plan,words,{tokenizer:'words'})).not.toThrow();
});
it('supports sharing without content hashes and declares UTF-16 ranges, including JSONL', () => {
  const file = toSplitPlanFile(d, plan, {includeAnchors:false});
  expect(file).not.toHaveProperty('blockAnchors');
  expect(file.offsetEncoding).toBe('utf16');
  const exp = buildJsonExport(d, plan, words, {tokenizer:'words'});
  expect(exp.offsetEncoding).toBe('utf16');
  expect(JSON.parse(toJsonl(exp).split('\n')[0]!).metadata.offsetEncoding).toBe('utf16');
  expect(importSplitPlan(d, plan, {...file,offsetEncoding:undefined}).plan.boundaries).toEqual(plan.boundaries);
  expect(() => importSplitPlan(d, plan, {...file,offsetEncoding:'codepoint'})).toThrow(/encoding/);
});
