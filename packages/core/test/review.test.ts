import { describe, expect, it } from 'vitest';
import { DEFAULT_SETTINGS, buildJsonExport, importSplitPlan, isChunkSettings, isTextBoundary, normalizeBoundaries, toSplitPlanFile, validatePlan } from '../src';
import { block, doc, lined, words } from './fixtures';
import { Tiktoken } from 'js-tiktoken/lite';
import ranks from 'js-tiktoken/ranks/cl100k_base';

describe('review regressions', () => {
  it('does not split supplementary characters in half', () => {
    const b = lined('b0', '😀𝑋. B.');
    expect([0, 2, 4, 6, 8].every((p) => isTextBoundary(b.text, p))).toBe(true);
    expect(isTextBoundary(b.text, 1)).toBe(false);
    expect(isTextBoundary(b.text, 3)).toBe(false);
    expect(normalizeBoundaries([b], [{ blockId: b.id, offset: 1 }])).toEqual([]);
    expect(validatePlan([b], { documentId: 'd', blocksFingerprint: 'f', revision: 0,
      settings: DEFAULT_SETTINGS, boundaries: [{ blockId: b.id, offset: 1 }] })).not.toEqual([]);
  });

  it('remaps unique identical content but never transfers deletions across parses', () => {
    const old = doc([block('b0', 'paragraph', 'Cover'), block('b1', 'paragraph', 'Keep this exact text'), block('b2', 'paragraph', 'End')]);
    const plan = { documentId: 'd', blocksFingerprint: old.blocksFingerprint, revision: 0,
      settings: DEFAULT_SETTINGS, boundaries: [{ blockId: 'b1' }], excluded: [{ blockId: 'b0', start: 0, end: 5 }] };
    const saved = toSplitPlanFile(old, plan);
    const next = { ...doc([block('b0', 'paragraph', 'IMPORTANT'), block('b5', 'paragraph', 'Keep this exact text'), block('b9', 'paragraph', 'End')]), blocksFingerprint: 'new' };
    const current = { ...plan, blocksFingerprint: 'new', boundaries: [], excluded: [] };
    const imported = importSplitPlan(next, current, saved);
    expect(imported.plan.boundaries).toEqual([{ blockId: 'b5' }]);
    expect(imported.plan.excluded).toEqual([]);
    expect(importSplitPlan(next, current, { ...saved, blockAnchors: undefined }).plan.boundaries).toEqual([]);
    const duplicate = { ...next, blocks: [...next.blocks, block('b6', 'paragraph', 'Keep this exact text')] };
    expect(importSplitPlan(duplicate, current, saved).plan.boundaries).toEqual([]);
  });

  it('rejects invalid numeric settings', () => {
    expect(isChunkSettings(DEFAULT_SETTINGS)).toBe(true);
    for (const patch of [{ targetTokens: 9000 }, { targetTokens: 500.5 }, { maxTokens: 0 }, { overlapTokens: 'bad' }]) {
      expect(isChunkSettings({ ...DEFAULT_SETTINGS, ...patch })).toBe(false);
    }
  });

  it('exports overlap without manufacturing replacement characters', () => {
    const enc = new Tiktoken(ranks);
    const tokenizer = { name: 'cl100k_base', encode: (s: string) => enc.encode(s, [], []),
      decode: (ids: number[]) => enc.decode(ids), count: (s: string) => enc.encode(s, [], []).length };
    const source = doc([block('b0', 'paragraph', '안녕'), block('b1', 'paragraph', 'Next.')]);
    const exp = buildJsonExport(source, { documentId: 'd', blocksFingerprint: 'fp1', revision: 0,
      settings: { ...DEFAULT_SETTINGS, overlapTokens: 1 }, boundaries: [{ blockId: 'b0' }] }, words, { tokenizer: tokenizer.name, encoder: tokenizer });
    expect(exp.chunks[1]!.text).toBe('Next.');
    expect(tokenizer.count('<|endoftext|>')).toBeGreaterThan(0);
  });
});
