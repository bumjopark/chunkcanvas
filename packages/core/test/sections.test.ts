import { describe, expect, it } from 'vitest';
import { buildJsonExport, DEFAULT_SETTINGS, generateChunks, importSplitPlan, toJsonl, toSplitPlanFile,
  type DocumentBlock, type JsonExport, type SplitPlan } from '../src';
import { block, doc, lined, words } from './fixtures';

function heading(id: string, text: string, level: number): DocumentBlock {
  return { ...block(id, level === 0 ? 'title' : 'section_header', text), level,
    markdown: '#'.repeat(Math.min(6, level + 1)) + ' ' + text };
}
const blocks = [heading('b0', 'Manual', 0), heading('b1', 'Setup', 1),
  lined('b2', 'Read the setup carefully.'), heading('b3', 'Checks', 2),
  lined('b4', 'Check each setting.'), heading('b5', 'Operation', 1),
  lined('b6', 'Start the service.')];
const plan: SplitPlan = { documentId: 'doc1', blocksFingerprint: 'fp1', revision: 1,
  settings: { ...DEFAULT_SETTINGS, keepHeadingWithBody: false }, boundaries: [] };
const opts = { tokenizer: 'words' };
const ids = (c: JsonExport['chunks'][number]) => c.headingPath.map((h) => h.blockId);
function assertPaths(exp: JsonExport) {
  const sections = new Map(exp.sections.map((s) => [s.blockId, s]));
  for (const c of exp.chunks) {
    let parent: string | null = null;
    let level = -1;
    for (const h of c.headingPath) {
      expect(sections.get(h.blockId)?.parentBlockId).toBe(parent);
      expect(h.level).toBeGreaterThan(level);
      parent = h.blockId; level = h.level;
    }
  }
}

describe('section metadata', () => {
  it('exports one document tree and the first body path across sections, without changing chunk text', () => {
    const d = doc(blocks);
    const exp = buildJsonExport(d, plan, words, opts);
    expect(exp.schemaVersion).toBe(3);
    expect(exp.sections.map((s) => [s.blockId, s.level, s.parentBlockId, s.pageIndex])).toEqual([
      ['b0', 0, null, 0], ['b1', 1, 'b0', 0], ['b3', 2, 'b1', 0], ['b5', 1, 'b0', 0],
    ]);
    expect(ids(exp.chunks[0]!)).toEqual(['b0', 'b1']);
    expect(exp.chunks[0]!.sectionIds).toEqual(['b1', 'b3', 'b5']);
    expect(exp.chunks.map((c) => c.text)).toEqual(generateChunks(blocks, plan, words).map((c) => c.text));
    const metadata = JSON.parse(toJsonl(exp).trim()).metadata;
    expect(metadata.headingPath).toEqual(['Manual', 'Setup']);
    expect(metadata.headingBlockIds).toEqual(['b0', 'b1']);
    expect(metadata.sectionIds).toEqual(['b1', 'b3', 'b5']);
    assertPaths(exp);
  });

  it('keeps original ancestors when their entire chunks or only heading text are deleted', () => {
    const p = { ...plan, boundaries: [{ blockId: 'b1' }, { blockId: 'b2' }],
      excluded: blocks.slice(0, 2).map((b) => ({ blockId: b.id, start: 0, end: b.text.length })) };
    const exp = buildJsonExport(doc(blocks), p, words, opts);
    expect(exp.chunks).toHaveLength(2);
    expect(ids(exp.chunks[0]!)).toEqual(['b0', 'b1']);
    expect(exp.chunks[0]!.blockIds).toEqual(['b2']);
    expect(exp.chunks[0]!.text).not.toContain('Manual');
    expect(ids(exp.chunks[1]!)).toEqual(['b0', 'b1', 'b3']);
    expect(exp.sections).toHaveLength(4);
    assertPaths(exp);
  });

  it('uses the last heading in a heading-only chunk and the first body before later headings', () => {
    const p = { ...plan, boundaries: [{ blockId: 'b1' }, { blockId: 'b3' }] };
    const exp = buildJsonExport(doc(blocks), p, words, opts);
    expect(ids(exp.chunks[0]!)).toEqual(['b0', 'b1']);
    expect(exp.chunks[0]!.sectionIds).toEqual([]);
    expect(ids(exp.chunks[1]!)).toEqual(['b0', 'b1']);
    expect(exp.chunks[1]!.sectionIds).toEqual(['b1']);
    expect(ids(exp.chunks[2]!)).toEqual(['b0', 'b1', 'b3']);
    assertPaths(exp);
  });

  it('uses the same section on both sides of an interior split and ignores overlap for section ownership', () => {
    const p = { ...plan, boundaries: [{ blockId: 'b2', offset: 9 }, { blockId: 'b4' }],
      settings: { ...plan.settings, overlapTokens: 5 } };
    const encoder = { name: 'chars', count: (t: string) => t.length,
      encode: (t: string) => [...t].map((c) => c.charCodeAt(0)), decode: (v: number[]) => String.fromCharCode(...v) };
    const exp = buildJsonExport(doc(blocks), p, words, { ...opts, encoder });
    expect(ids(exp.chunks[0]!)).toEqual(['b0', 'b1']);
    expect(ids(exp.chunks[1]!)).toEqual(['b0', 'b1']);
    expect(ids(exp.chunks[2]!)).toEqual(['b0', 'b5']);
    expect(exp.chunks[2]!.sectionIds).toEqual(['b5']);
    expect(exp.chunks[2]!.overlapTokens).toBeGreaterThan(0);
    assertPaths(exp);
  });

  it('handles untitled documents, unsectioned body, empty documents and headings with unknown locations', () => {
    const bs = [block('b0', 'paragraph', 'Preface'), { ...heading('b1', 'Chapter', 1), regions: [] },
      block('b2', 'paragraph', 'Body')];
    const exp = buildJsonExport(doc(bs), { ...plan, boundaries: [{ blockId: 'b0' }] }, words, opts);
    expect(ids(exp.chunks[0]!)).toEqual([]);
    expect(exp.chunks[0]!.sectionIds).toEqual([]);
    expect(ids(exp.chunks[1]!)).toEqual(['b1']);
    expect(exp.sections[0]).toMatchObject({ parentBlockId: null, pageIndex: null });
    expect(buildJsonExport(doc([]), plan, words, opts)).toMatchObject({ sections: [], chunks: [] });
    assertPaths(exp);
  });

  it('uses reading order even when input blocks are shuffled and retains deep levels', () => {
    const bs = Array.from({ length: 9 }, (_, i) => heading(`b${i}`, `Depth ${i}`, i));
    bs.push(block('b9', 'paragraph', 'Deep body'));
    const exp = buildJsonExport(doc([...bs].reverse()), plan, words, opts);
    expect(exp.sections.map((s) => s.blockId)).toEqual(bs.slice(0, 9).map((b) => b.id));
    expect(exp.chunks[0]!.headingPath.map((s) => s.level)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8]);
    assertPaths(exp);
  });

  it('retains original section ownership when footnotes move to the end in editing order', () => {
    const note = { ...block('b2', 'footnote', 'First chapter note'),
      regions: [{ pageIndex: 0, bbox: { x0: 0.1, y0: 0.85, x1: 0.8, y1: 0.9 } }] };
    const bs = [heading('b0', 'First', 1), block('b1', 'paragraph', 'First body'), note,
      heading('b3', 'Second', 1), block('b4', 'paragraph', 'Second body')];
    const exp = buildJsonExport(doc(bs), plan, words, opts);
    expect(exp.chunks[0]!.blockIds.indexOf('b2')).toBeGreaterThan(exp.chunks[0]!.blockIds.indexOf('b4'));
    expect(exp.chunks[0]!.sectionIds).toEqual(['b0', 'b3']);
    assertPaths(exp);
  });

  it('keeps version 2 split plans compatible independently of chunk export schema version', () => {
    const d = doc(blocks);
    const saved = toSplitPlanFile(d, plan);
    expect(saved.schemaVersion).toBe(2);
    expect(importSplitPlan(d, plan, saved).warnings).toEqual([]);
    expect(buildJsonExport(d, plan, words, opts).schemaVersion).toBe(3);
  });

  it('rejects duplicate block IDs rather than constructing an ambiguous parent chain', () => {
    const bs = [heading('b0', 'Root', 0), heading('b0', 'Child', 1), block('b1', 'paragraph', 'Body')];
    expect(() => buildJsonExport(doc(bs), plan, words, opts)).toThrow();
  });
});
