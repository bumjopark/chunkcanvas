import { Tiktoken } from 'js-tiktoken/lite';
import cl100k from 'js-tiktoken/ranks/cl100k_base';
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
  DEFAULT_SETTINGS,
  characterSplit,
  documentText,
  generateChunks,
  locateChunks,
  rangeSegments,
  rangesToBoundaries,
  recursiveCharacterSplit,
  runStrategy,
  strategyMetrics,
  tokenSplit,
  validatePlan,
  type StrategyId,
  type Tokenizer,
} from '../src';
import { block, lined, words } from './fixtures';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/langchain-splitters.json', import.meta.url), 'utf-8')) as {
  text: string;
  cases: Record<string, string[]>;
};
const enc = new Tiktoken(cl100k);
const tokenizer: Tokenizer = { name: 'cl100k_base', count: (t) => enc.encode(t).length, encode: (t) => enc.encode(t), decode: (ids) => enc.decode(ids) };

describe('LangChain parity', () => {
  const text = fixture.text;
  it.each([
    ['recursive-chars-120-30', () => recursiveCharacterSplit(text, { chunkSize: 120, chunkOverlap: 30 })],
    ['recursive-chars-300-0', () => recursiveCharacterSplit(text, { chunkSize: 300, chunkOverlap: 0 })],
    ['recursive-chars-40-10', () => recursiveCharacterSplit(text, { chunkSize: 40, chunkOverlap: 10 })],
    ['character-200-50', () => characterSplit(text, { chunkSize: 200, chunkOverlap: 50 })],
    ['recursive-tokens-60-10', () => recursiveCharacterSplit(text, { chunkSize: 60, chunkOverlap: 10, length: tokenizer.count })],
    ['token-50-10', () => tokenSplit(text, { chunkSize: 50, chunkOverlap: 10, tokenizer }).chunks],
  ])('%s matches langchain-text-splitters', (name, run) => {
    expect(run()).toEqual(fixture.cases[name]);
  });

  it('locates every recursive chunk in the source text', () => {
    const chunks = fixture.cases['recursive-chars-120-30']!;
    const ranges = locateChunks(text, chunks);
    ranges.forEach((r, i) => expect(text.slice(r.start, r.end)).toBe(chunks[i]));
    ranges.slice(1).forEach((r, i) => expect(r.start).toBeGreaterThan(ranges[i]!.start));
  });

  it('gives token windows character ranges', () => {
    const { chunks, ranges } = tokenSplit('Plain ASCII text. '.repeat(40), { chunkSize: 20, chunkOverlap: 5, tokenizer });
    ranges.forEach((r, i) => expect('Plain ASCII text. '.repeat(40).slice(r.start, r.end)).toBe(chunks[i]));
  });
});

describe('strategies on a parsed document', () => {
  const blocks = [
    { ...block('b1', 'title', 'Guide'), markdown: '# Guide' },
    lined('b2', 'First sentence of the intro. Second sentence keeps going here. Third one ends the intro.'),
    { ...block('b3', 'section_header', 'Part A'), markdown: '## Part A', level: 1 },
    lined('b4', 'Alpha text in part A. More alpha text follows now.'),
    block('b5', 'table', '| a | b |\n| --- | --- |\n| 1 | 2 |'),
    { ...block('b6', 'section_header', 'Part B'), markdown: '## Part B', level: 1 },
    lined('b7', 'Beta text in part B. Final words here.'),
  ];
  const settings = DEFAULT_SETTINGS;
  const doc = documentText(blocks, settings);

  it('builds the document text with spans', () => {
    expect(doc.text.startsWith('# Guide\n\nFirst sentence')).toBe(true);
    for (const s of doc.spans) {
      const b = blocks.find((x) => x.id === s.blockId)!;
      expect(doc.text.slice(s.start, s.end)).toBe(b.markdown ?? b.text);
    }
  });

  it.each<StrategyId>(['structure', 'recursive', 'character', 'token', 'sentence', 'markdown'])('%s converts to a valid editable plan', (id) => {
    const params = { chunkSize: id === 'recursive' || id === 'character' ? 80 : 20, chunkOverlap: id === 'recursive' ? 20 : 0, unit: 'chars' as const, headerLevel: 1 };
    const result = runStrategy(id, { ...params, unit: id === 'token' || id === 'sentence' || id === 'structure' ? 'tokens' : 'chars' }, blocks, settings, tokenizer);
    expect(result.ranges.length).toBeGreaterThan(1);
    const boundaries = rangesToBoundaries(blocks, settings, doc, result.ranges);
    const plan = { documentId: 'd', blocksFingerprint: 'f', revision: 0, boundaries, settings };
    expect(validatePlan(blocks, plan)).toEqual([]);
    const chunks = generateChunks(blocks, plan, words);
    // Non-overlapping partition: as many chunks as the splitter made, minus cuts that snapped together.
    expect(chunks.length).toBeLessThanOrEqual(result.ranges.length);
    expect(chunks.length).toBeGreaterThan(1);
  });

  it('markdown headers cut before headings up to the level', () => {
    const r = runStrategy('markdown', { chunkSize: 0, chunkOverlap: 0, unit: 'tokens', headerLevel: 1 }, blocks, settings, tokenizer);
    expect(r.texts.map((t) => t.split('\n')[0])).toEqual(['# Guide', '## Part A', '## Part B']);
  });

  it('sentence packing never cuts inside a sentence', () => {
    const r = runStrategy('sentence', { chunkSize: 12, chunkOverlap: 0, unit: 'tokens', headerLevel: 1 }, blocks, settings, tokenizer);
    const m = strategyMetrics(r, blocks, tokenizer.count);
    expect(m.midSentenceCuts).toBe(0);
  });

  it('flags mid-sentence cuts, split tables and overlap for small character windows', () => {
    const r = runStrategy('recursive', { chunkSize: 30, chunkOverlap: 10, unit: 'chars', headerLevel: 1 }, blocks, settings, tokenizer);
    const m = strategyMetrics(r, blocks, tokenizer.count);
    expect(m.midSentenceCuts).toBeGreaterThan(0);
    expect(m.overlapTokens).toBeGreaterThan(0);
    expect(m.chunks).toBe(r.ranges.length);
    const s = runStrategy('structure', { chunkSize: 500, chunkOverlap: 0, unit: 'tokens', headerLevel: 1 }, blocks, settings, tokenizer);
    expect(strategyMetrics(s, blocks, tokenizer.count)).toMatchObject({ midSentenceCuts: 0, splitTables: 0, orphanHeadings: 0 });
  });

  it('maps a document range back to block segments', () => {
    const s4 = doc.spans.find((s) => s.blockId === 'b4')!;
    const segs = rangeSegments(blocks, doc, { start: s4.start + 6, end: s4.end + 5 });
    expect(segs[0]).toEqual({ blockId: 'b4', start: 6, end: blocks[3]!.text.length });
    expect(segs[1]!.blockId).toBe('b5');
  });
});
