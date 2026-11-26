import { describe, expect, it } from 'vitest';
import {
  DEFAULT_SETTINGS,
  addBoundary,
  buildJsonExport,
  deleteChunk,
  generateChunks,
  importSplitPlan,
  mergeChunks,
  resetSplitPlan,
  restoreChunk,
  toJsonl,
  toMarkdown,
  toSplitPlanFile,
  type SplitPlan,
  upgradeSplitPlan,
} from '../src';
import { at, block, doc, lined, words } from './fixtures';

const blocks = [
  { ...block('b1', 'section_header', 'Intro'), markdown: '## Intro' },
  block('b2', 'paragraph', 'First paragraph here.'),
  block('b3', 'table', '| a | b |', 1),
  block('b4', 'paragraph', 'Closing words.', 2),
];
const d = doc(blocks);
const base: SplitPlan = {
  documentId: 'doc1',
  blocksFingerprint: 'fp1',
  revision: 4,
  boundaries: [{ blockId: 'b2' }],
  settings: { ...DEFAULT_SETTINGS, targetTokens: 50, maxTokens: 80 },
};
const opts = { tokenizer: 'words', generatedAt: '2026-01-01T00:00:00.000Z' };

describe('exports', () => {
  it('matches the editor chunks exactly', () => {
    const exp = buildJsonExport(d, base, words, opts);
    const chunks = generateChunks(blocks, base, words);
    expect(exp.chunks.map((c) => [c.text, c.tokenCount, c.blockIds])).toEqual(chunks.map((c) => [c.text, c.tokenCount, c.blockIds]));
    expect(exp.chunks.flatMap((c) => c.blockIds)).toEqual(['b1', 'b2', 'b3', 'b4']);
    expect(exp.chunks[1]).toMatchObject({ pageStart: 2, pageEnd: 3 });
    expect(exp.chunks[0]!.sources[0]).toMatchObject({ blockId: 'b1', type: 'section_header', start: 0, end: 5 });
    expect(exp.chunks[1]!.segments).toEqual([{ blockId: 'b3', start: 0, end: 9 }, { blockId: 'b4', start: 0, end: 14 }]);
    expect(exp).toMatchObject({ schema: 'chunkcanvas.chunks', schemaVersion: 3, tokenizer: 'words' });
    expect(exp.document.blocksFingerprint).toBe('fp1');
  });

  it('refuses to export an invalid plan', () => {
    expect(() => buildJsonExport(d, { ...base, boundaries: [{ blockId: 'b4' }] }, words, opts)).toThrow(/invalid/);
  });

  it('writes one JSON object per line', () => {
    const exp = buildJsonExport(d, base, words, opts);
    const lines = toJsonl(exp).trimEnd().split('\n');
    expect(lines).toHaveLength(2);
    const first = JSON.parse(lines[0]!);
    expect(first).toMatchObject({ id: 'doc1#0', text: '## Intro\n\nFirst paragraph here.' });
    expect(first.metadata).toMatchObject({ sourceFile: 'a.pdf', pageStart: 1, pageEnd: 1, tokenizer: 'words', blockIds: ['b1', 'b2'] });
  });

  it('writes readable markdown', () => {
    const md = toMarkdown(buildJsonExport(d, base, words, opts));
    expect(md).toContain('# a.pdf');
    expect(md).toContain('## Chunk 2');
    expect(md).toContain('pages 2–3');
    expect(md).toContain('Closing words.');
  });
});

describe('overlap', () => {
  const enc = { name: 'chars', count: (t: string) => t.length, encode: (t: string) => [...t].map((c) => c.codePointAt(0)!), decode: (ids: number[]) => String.fromCodePoint(...ids) };
  it('prefixes each chunk with the end of the previous one', () => {
    const plan = { ...base, settings: { ...base.settings, overlapTokens: 6 } };
    const exp = buildJsonExport(d, plan, words, { ...opts, encoder: enc });
    expect(exp.chunks[0]!.overlapTokens).toBe(0);
    expect(exp.chunks[1]!.text.startsWith('here.\n\n| a | b |')).toBe(true);
    expect(exp.chunks[1]!.overlapTokens).toBe(5);
    expect(JSON.parse(toJsonl(exp).split('\n')[1]!).metadata.overlapTokens).toBe(5);
  });
  it('is skipped without an encoder', () => {
    const plan = { ...base, settings: { ...base.settings, overlapTokens: 6 } };
    expect(buildJsonExport(d, plan, words, opts).chunks[1]!.overlapTokens).toBe(0);
  });
});

describe('split plan files', () => {
  it('round-trips', () => {
    const edited = addBoundary(base, blocks, { blockId: 'b3' });
    const file = JSON.parse(JSON.stringify(toSplitPlanFile(d, edited)));
    const { plan, warnings } = importSplitPlan(d, base, file);
    expect(plan.boundaries).toEqual([{ blockId: 'b2' }, { blockId: 'b3' }]);
    expect(plan.settings).toEqual(edited.settings);
    expect(warnings).toEqual([]);
    expect(file.blockAnchors.every((a: Record<string, unknown>) => !('text' in a) && /^[a-f0-9]{64}$/.test(String(a.textHash)))).toBe(true);
  });

  it('rejects files for another PDF or another format', () => {
    const file = toSplitPlanFile(d, base);
    expect(() => importSplitPlan(d, base, { ...file, fileHash: 'other' })).toThrow(/different PDF/);
    expect(() => importSplitPlan(d, base, { hello: 1 })).toThrow(/Not a ChunkCanvas/);
    expect(() => importSplitPlan(d, base, { ...file, schemaVersion: 9 })).toThrow(/version/);
  });

  it('keeps matching boundaries from a different parse and warns', () => {
    const file = { ...toSplitPlanFile(d, base), blocksFingerprint: 'old', boundaries: [{ blockId: 'b2' }, { blockId: 'gone' }, { blockId: 'b4' }] };
    const { plan, warnings } = importSplitPlan(d, base, file);
    expect(plan.boundaries).toEqual([{ blockId: 'b2' }]);
    expect(warnings).toHaveLength(2);
  });

  it('imports an intentionally unsplit plan after a parse change without restoring deleted ranges', () => {
    const file = { ...toSplitPlanFile(d, base), blocksFingerprint: 'old', boundaries: [],
      excluded: [{blockId: 'b1', start: 0, end: 3}] };
    const result = importSplitPlan(d, base, file);
    expect(result.plan.boundaries).toEqual([]);
    expect(result.plan.excluded).toEqual([]);
    expect(result.warnings.some(w => w.includes('No boundaries'))).toBe(false);
  });

  it('accepts version 1 files with block boundaries', () => {
    const v1 = { schema: 'chunkcanvas.split-plan', schemaVersion: 1, fileHash: 'hash1', blocksFingerprint: 'fp1', boundaryAfterBlockIds: ['b3'], settings: base.settings };
    expect(importSplitPlan(d, base, v1).plan.boundaries).toEqual([{ blockId: 'b3' }]);
  });

  it('does not use conflicting or repeated source anchors to remap a boundary', () => {
    const file = toSplitPlanFile(d, base);
    const anchor = file.blockAnchors![0]!;
    const current = { ...base, boundaries: [{ blockId: 'b3' }] };
    const ambiguous = { ...file, blocksFingerprint: 'old',
      blockAnchors: [anchor, { ...anchor, textHash: '0'.repeat(64) }, anchor] };
    const result = importSplitPlan(d, current, ambiguous);
    expect(result.plan.boundaries).toEqual(current.boundaries);
    expect(result.warnings.some((w) => w.includes('No boundaries could be matched'))).toBe(true);
  });
});

describe('upgradeSplitPlan', () => {
  it('converts plans saved before inner boundaries existed', () => {
    const old = { documentId: 'doc1', blocksFingerprint: 'fp1', revision: 3, boundaryAfterBlockIds: ['b2'], settings: base.settings };
    expect(upgradeSplitPlan(old)).toEqual({ documentId: 'doc1', blocksFingerprint: 'fp1', revision: 3, boundaries: [{ blockId: 'b2' }], settings: base.settings });
  });
  it('keeps current plans and rejects malformed ones', () => {
    expect(upgradeSplitPlan(base)).toBe(base);
    expect(upgradeSplitPlan(null)).toBeNull();
    expect(upgradeSplitPlan({ documentId: 'x' })).toBeNull();
    expect(upgradeSplitPlan({ ...base, boundaries: [{ nope: 1 }] })).toBeNull();
  });
});

describe('deleted chunks', () => {
  const three = [
    block('b1', 'paragraph', 'Cover page words.'),
    block('b2', 'paragraph', 'Table of contents.', 1),
    block('b3', 'paragraph', 'The body text itself.', 2),
  ];
  const d3 = doc(three);
  const plan3: SplitPlan = { ...base, boundaries: [{ blockId: 'b1' }, { blockId: 'b2' }] };
  const enc = { name: 'chars', count: (t: string) => t.length, encode: (t: string) => [...t].map((c) => c.codePointAt(0)!), decode: (ids: number[]) => String.fromCodePoint(...ids) };

  it('are left out of every export, which numbers the chunks kept', () => {
    const plan = deleteChunk(plan3, three, 1);
    const exp = buildJsonExport(d3, { ...plan, settings: { ...plan.settings, overlapTokens: 8 } }, words, { ...opts, encoder: enc });
    expect(exp.chunks.map((c) => [c.index, c.blockIds])).toEqual([[0, ['b1']], [1, ['b3']]]);
    // No overlap repeats a deleted chunk, nor the text before it.
    const everything = [JSON.stringify(exp), toJsonl(exp), toMarkdown(exp)].join('\n');
    expect(everything).not.toContain('contents');
    expect(exp.chunks[1]!).toMatchObject({ text: 'The body text itself.', overlapTokens: 0 });
    expect(JSON.parse(toJsonl(exp).split('\n')[1]!).id).toBe('doc1#1');
    expect(exp.excluded).toEqual([{ blockId: 'b2', start: 0, end: 18 }]);
  });

  it('stay deleted whatever happens to the boundaries, until restored', () => {
    const deleted = deleteChunk(plan3, three, 1);
    expect(generateChunks(three, deleted, words).map((c) => c.deleted)).toEqual([false, true, false]);
    expect(deleteChunk(deleted, three, 1)).toBe(deleted);
    // Merged into the chunk after it, its text still stays out.
    const merged = mergeChunks(deleted, three, 1);
    const [, both] = generateChunks(three, merged, words);
    expect(both).toMatchObject({ deleted: false, text: 'The body text itself.', pageRange: [3, 3] });
    expect(both!.excluded).toEqual([{ blockId: 'b2', start: 0, end: 18 }]);
    // Reset to the automatic split: still out.
    expect(generateChunks(three, resetSplitPlan(deleted, three, words), words)[0]!.text).not.toContain('contents');
    // Restored, it comes back.
    const back = restoreChunk(merged, three, 1);
    expect(generateChunks(three, back, words)[1]!.text).toBe('Table of contents.\n\nThe body text itself.');
    expect(restoreChunk(back, three, 1)).toBe(back);
  });

  it('split inside a block keep only the deleted characters out', () => {
    const one = [lined('b1', 'First sentence here. Second sentence there.')];
    const split = addBoundary({ ...base, boundaries: [] }, one, at('b1', 21));
    const deleted = deleteChunk(split, one, 0);
    expect(generateChunks(one, deleted, words).map((c) => [c.deleted, c.text])).toEqual([[true, ''], [false, 'Second sentence there.']]);
    // Joined again, the chunk holds only the characters kept.
    const joined = generateChunks(one, { ...deleted, boundaries: [] }, words)[0]!;
    expect(joined).toMatchObject({ deleted: false, text: 'Second sentence there.', excluded: [{ blockId: 'b1', start: 0, end: 21 }] });
  });

  it('a block without text is deleted too', () => {
    const pics = [block('b1', 'paragraph', 'Words.'), block('b2', 'picture', '', 1)];
    const deleted = deleteChunk({ ...base, boundaries: [{ blockId: 'b1' }] }, pics, 1);
    expect(deleted.excluded).toEqual([{ blockId: 'b2', start: 0, end: 0 }]);
    expect(generateChunks(pics, deleted, words)[1]!.deleted).toBe(true);
  });

  it('go with the split plan file, matched to the parse', () => {
    const deleted = deleteChunk(plan3, three, 1);
    const file = JSON.parse(JSON.stringify(toSplitPlanFile(d3, deleted)));
    expect(importSplitPlan(d3, plan3, file).plan.excluded).toEqual([{ blockId: 'b2', start: 0, end: 18 }]);
    const stale = { ...file, excluded: [{ blockId: 'gone', start: 0, end: 4 }, { blockId: 'b3', start: 5, end: 999 }, 'junk'] };
    expect(importSplitPlan(d3, plan3, stale).plan.excluded).toEqual([{ blockId: 'b3', start: 5, end: 21 }]);
    const old = { ...file };
    delete old.excluded;
    expect(importSplitPlan(d3, deleted, old).plan.excluded).toEqual([]);
  });
});
