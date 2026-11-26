import { describe, expect, it } from 'vitest';
import {
  DEFAULT_SETTINGS,
  addBoundary,
  autoSplit,
  boundaryMoveRange,
  canRedo,
  canUndo,
  chunkableBlocks,
  createHistory,
  createSplitPlan,
  generateChunks,
  mergeChunks,
  moveBoundary,
  pushHistory,
  redo,
  removeBoundary,
  resetSplitPlan,
  splitChunk,
  undo,
  validatePlan,
  type Boundary,
  type ChunkSettings,
  type DocumentBlock,
  type SplitPlan,
} from '../src';
import { after, at, block, lined, lorem, words } from './fixtures';

const settings: ChunkSettings = { ...DEFAULT_SETTINGS, targetTokens: 10, maxTokens: 20 };

function plan(boundaries: (string | Boundary)[], s: ChunkSettings = settings): SplitPlan {
  return {
    documentId: 'doc1',
    blocksFingerprint: 'fp1',
    revision: 0,
    boundaries: boundaries.map((b) => (typeof b === 'string' ? { blockId: b } : b)),
    settings: s,
  };
}

const splitIds = (...args: Parameters<typeof autoSplit>) => autoSplit(...args).map((b) => b.blockId);
const ids = (p: SplitPlan) => p.boundaries.map((b) => (b.offset === undefined ? b.blockId : `${b.blockId}@${b.offset}`));

const five = ['b1', 'b2', 'b3', 'b4', 'b5'].map((id) => block(id, 'paragraph', `${id} text`));

/** The core invariant: every chunkable block appears exactly once, in reading order, in a non-empty chunk. */
function expectPartition(blocks: DocumentBlock[], p: SplitPlan) {
  const chunks = generateChunks(blocks, p, words);
  expect(chunks.every((c) => c.blockIds.length > 0)).toBe(true);
  expect(chunks.flatMap((c) => c.blockIds)).toEqual(chunkableBlocks(blocks, p.settings).map((b) => b.id));
  expect(validatePlan(blocks, p)).toEqual([]);
  return chunks;
}

describe('generateChunks', () => {
  it('produces one chunk without boundaries', () => {
    const chunks = expectPartition(five, plan([]));
    expect(chunks).toHaveLength(1);
    expect(chunks[0]!.text).toBe('b1 text\n\nb2 text\n\nb3 text\n\nb4 text\n\nb5 text');
    expect(chunks[0]!.tokenCount).toBe(10);
  });

  it('splits after each boundary and orders by reading order, not array order', () => {
    const shuffled = [five[3]!, five[0]!, five[4]!, five[2]!, five[1]!];
    const chunks = expectPartition(shuffled, plan(['b2', 'b4']));
    expect(chunks.map((c) => c.blockIds)).toEqual([['b1', 'b2'], ['b3', 'b4'], ['b5']]);
    expect(chunks.map((c) => c.index)).toEqual([0, 1, 2]);
  });

  it('reports page ranges, and null when no block has a location', () => {
    const blocks = [block('b1', 'paragraph', 'a', 0), block('b2', 'paragraph', 'b', 2)];
    expect(generateChunks(blocks, plan([]), words)[0]!.pageRange).toEqual([1, 3]);
    const unlocated = blocks.map((b) => ({ ...b, regions: [] }));
    expect(generateChunks(unlocated, plan([]), words)[0]!.pageRange).toBeNull();
  });

  it('flags chunks over target and over max', () => {
    const blocks = [block('b1', 'paragraph', lorem(15)), block('b2', 'table', lorem(25))];
    const [a, b] = generateChunks(blocks, plan(['b1']), words);
    expect([a!.overTarget, a!.overMax]).toEqual([true, false]);
    expect([b!.overTarget, b!.overMax]).toEqual([true, true]);
  });

  it('leaves page furniture out when excluded', () => {
    const blocks = [block('h0', 'page_header', 'Header', 0, 0), ...five.slice(0, 2), block('f9', 'page_footer', '1 / 3', 0, 9)];
    const chunks = expectPartition(blocks, plan([]));
    expect(chunks[0]!.blockIds).toEqual(['b1', 'b2']);
    // A boundary after an excluded block is ignored rather than producing an empty chunk.
    expect(generateChunks(blocks, plan(['h0']), words)).toHaveLength(1);
    const kept = expectPartition(blocks, plan([], { ...settings, excludeFurniture: false }));
    expect(kept[0]!.blockIds).toEqual(['h0', 'b1', 'b2', 'f9']);
  });

  it('uses markdown when a block provides it', () => {
    const blocks = [{ ...block('b1', 'section_header', 'Intro'), markdown: '## Intro' }, block('b2', 'paragraph', 'Body')];
    expect(generateChunks(blocks, plan([]), words)[0]!.text).toBe('## Intro\n\nBody');
  });
});

describe('boundary edits', () => {
  it('adds a boundary and keeps boundaries sorted', () => {
    const p = addBoundary(addBoundary(plan([]), five, after('b4')), five, after('b2'));
    expect(ids(p)).toEqual(['b2', 'b4']);
    expectPartition(five, p);
  });

  it('refuses duplicate, trailing and unknown boundaries without changing the plan', () => {
    const p = plan(['b2']);
    expect(addBoundary(p, five, after('b2'))).toBe(p);
    expect(addBoundary(p, five, after('b5'))).toBe(p);
    expect(addBoundary(p, five, after('nope'))).toBe(p);
  });

  it('removes a boundary, merging the two chunks', () => {
    const p = removeBoundary(plan(['b2', 'b4']), after('b2'));
    expect(generateChunks(five, p, words).map((c) => c.blockIds)).toEqual([['b1', 'b2', 'b3', 'b4'], ['b5']]);
    const same = plan(['b2']);
    expect(removeBoundary(same, after('b3'))).toBe(same);
  });

  it('moves a boundary only between its neighbours', () => {
    const p = plan(['b1', 'b3']);
    const range = boundaryMoveRange(p, five, after('b3'))!;
    expect(['b1', 'b2', 'b3', 'b4', 'b5'].map((id) => range.allows(after(id)))).toEqual([false, true, true, true, false]);
    expect(ids(moveBoundary(p, five, after('b3'), after('b4')))).toEqual(['b1', 'b4']);
    expect(ids(moveBoundary(p, five, after('b3'), after('b2')))).toEqual(['b1', 'b2']);
    // Onto or past the previous boundary would empty a chunk.
    expect(moveBoundary(p, five, after('b3'), after('b1'))).toBe(p);
    expect(moveBoundary(p, five, after('b1'), after('b3'))).toBe(p);
    // Never after the last block.
    expect(moveBoundary(p, five, after('b3'), after('b5'))).toBe(p);
    expect(moveBoundary(p, five, after('missing'), after('b2'))).toBe(p);
  });

  it('merges a chunk with the next one and splits a chunk after a block', () => {
    const p = plan(['b1', 'b3']);
    expect(ids(mergeChunks(p, five, 0))).toEqual(['b3']);
    expect(mergeChunks(p, five, 2)).toBe(p);
    expect(ids(splitChunk(p, five, after('b4')))).toEqual(['b1', 'b3', 'b4']);
  });

  it('updates chunk text and token counts after an edit', () => {
    const before = generateChunks(five, plan(['b2']), words);
    const moved = generateChunks(five, moveBoundary(plan(['b2']), five, after('b2'), after('b3')), words);
    expect(before.map((c) => c.tokenCount)).toEqual([4, 6]);
    expect(moved.map((c) => c.tokenCount)).toEqual([6, 4]);
    expect(moved[0]!.text).toContain('b3 text');
  });

  it('keeps the partition invariant under a long random edit sequence', () => {
    const blocks = Array.from({ length: 40 }, (_, i) => block(`b${i + 1}`, i % 7 === 0 ? 'section_header' : 'paragraph', lorem(1 + (i % 5))));
    let p = plan([]);
    let seed = 7;
    const rand = (n: number) => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed % n;
    };
    for (let step = 0; step < 2000; step++) {
      const id = `b${rand(42) + 1}`;
      const op = rand(4);
      if (op === 0) p = addBoundary(p, blocks, after(id));
      else if (op === 1) p = removeBoundary(p, p.boundaries[rand(p.boundaries.length + 1)] ?? after(id));
      else if (op === 2) p = moveBoundary(p, blocks, p.boundaries[rand(p.boundaries.length + 1)] ?? after(id), after(id));
      else p = mergeChunks(p, blocks, rand(10));
      expectPartition(blocks, p);
    }
  });
});

describe('splitting inside a block', () => {
  const para = lined('b2', 'First sentence here. Second one follows. Third ends it.');
  const blocks = [block('b1', 'section_header', 'Intro'), para, block('b3', 'paragraph', 'Tail text')];

  it('cuts a block at a character offset into two chunks', () => {
    const p = addBoundary(plan([]), blocks, at('b2', 21));
    expect(ids(p)).toEqual(['b2@21']);
    const chunks = generateChunks(blocks, p, words);
    expect(chunks.map((c) => c.segments)).toEqual([
      [
        { blockId: 'b1', start: 0, end: 5 },
        { blockId: 'b2', start: 0, end: 21 },
      ],
      [
        { blockId: 'b2', start: 21, end: para.text.length },
        { blockId: 'b3', start: 0, end: 9 },
      ],
    ]);
    expect(chunks[0]!.text).toBe('Intro\n\nFirst sentence here.');
    expect(chunks[1]!.text).toBe('Second one follows. Third ends it.\n\nTail text');
    expect(chunks[1]!.id).toBe('chunk_b2@21');
    expect(chunks.map((c) => c.blockIds)).toEqual([['b1', 'b2'], ['b2', 'b3']]);
  });

  it('keeps list markers only on the first piece', () => {
    const item = { ...lined('b1', 'Alpha beta. Gamma delta.'), type: 'list_item' as const, markdown: '- Alpha beta. Gamma delta.' };
    const chunks = generateChunks([item, block('b2', 'paragraph', 'x')], plan([at('b1', 12)]), words);
    expect(chunks.map((c) => c.text)).toEqual(['- Alpha beta.', 'Gamma delta.\n\nx']);
  });

  it('refuses offsets in atomic blocks, at edges and duplicates', () => {
    const p = plan([]);
    expect(addBoundary(p, blocks, at('b1', 2))).toBe(p);
    expect(addBoundary(p, blocks, at('b3', 4))).toBe(p);
    expect(addBoundary(p, blocks, at('b2', 0))).toBe(p);
    expect(addBoundary(p, blocks, at('b2', para.text.length))).toBe(p);
    const once = addBoundary(p, blocks, at('b2', 21));
    expect(addBoundary(once, blocks, at('b2', 21))).toBe(once);
  });

  it('normalizes edge offsets to block cuts', () => {
    expect(ids(plan([at('b2', 0), at('b2', 999), at('b2', 21)]))).toEqual(['b2@0', 'b2@999', 'b2@21']);
    const chunks = generateChunks(blocks, plan([at('b2', 0), at('b2', 999), at('b2', 21)]), words);
    expect(chunks.map((c) => c.segments.map((s) => `${s.blockId}:${s.start}-${s.end}`))).toEqual([
      ['b1:0-5'],
      ['b2:0-21'],
      ['b2:21-55'],
      ['b3:0-9'],
    ]);
  });

  it('moves between inner and block positions, never past neighbours', () => {
    const p = plan(['b1', at('b2', 21)]);
    expect(ids(moveBoundary(p, blocks, at('b2', 21), at('b2', 41)))).toEqual(['b1', 'b2@41']);
    expect(ids(moveBoundary(p, blocks, at('b2', 21), after('b2')))).toEqual(['b1', 'b2']);
    expect(moveBoundary(p, blocks, at('b2', 21), after('b1'))).toBe(p);
    expect(ids(moveBoundary(p, blocks, after('b1'), at('b2', 10)))).toEqual(['b2@10', 'b2@21']);
    expect(moveBoundary(p, blocks, after('b1'), at('b2', 30))).toBe(p);
  });

  it('merges across an inner cut and validates offsets', () => {
    const p = plan(['b1', at('b2', 21)]);
    expect(ids(mergeChunks(p, blocks, 1))).toEqual(['b1']);
    expect(validatePlan(blocks, p)).toEqual([]);
    expect(validatePlan(blocks, plan([at('b2', 0)]))).toEqual(['invalid offset b2@0']);
    expect(validatePlan(blocks, plan([at('b2', 30), at('b2', 21)]))).toEqual(['boundary b2@21 out of reading order']);
  });

  it('keeps the partition invariant with random inner and block cuts', () => {
    const many = Array.from({ length: 12 }, (_, i) =>
      i % 3 === 0 ? block(`b${i + 1}`, 'section_header', `H${i}`) : lined(`b${i + 1}`, `Sentence ${i} one. Sentence ${i} two. Sentence ${i} three.`),
    );
    let p = plan([]);
    let seed = 11;
    const rand = (n: number) => (seed = (seed * 1103515245 + 12345) % 2147483648) % n;
    for (let step = 0; step < 1500; step++) {
      const target = many[rand(many.length)]!;
      const pos = rand(2) ? after(target.id) : at(target.id, 1 + rand(target.text.length + 2));
      const op = rand(4);
      if (op === 0) p = addBoundary(p, many, pos);
      else if (op === 1) p = removeBoundary(p, p.boundaries[rand(p.boundaries.length + 1)] ?? pos);
      else if (op === 2) p = moveBoundary(p, many, p.boundaries[rand(p.boundaries.length + 1)] ?? pos, pos);
      else p = mergeChunks(p, many, rand(8));
      const chunks = generateChunks(many, p, words);
      // Every character of every block is covered exactly once, in order.
      for (const b of many) {
        const parts = chunks.flatMap((c) => c.segments).filter((s) => s.blockId === b.id);
        expect(parts[0]!.start).toBe(0);
        expect(parts[parts.length - 1]!.end).toBe(b.text.length);
        parts.forEach((s, k) => k > 0 && expect(s.start).toBe(parts[k - 1]!.end));
        parts.forEach((s) => expect(s.end).toBeGreaterThan(s.start));
      }
      expect(validatePlan(many, p)).toEqual([]);
    }
  }, 20_000); // 1500 random edits: slow on a busy machine

  it('auto-split cuts a single sentence longer than the maximum after commas', () => {
    const claim = lined('b1', Array.from({ length: 12 }, (_, i) => `clause number ${i} goes on,`).join(' ') + ' and so it ends.');
    const bs = [claim, block('b2', 'paragraph', 'end')];
    const p = plan(autoSplit(bs, { ...settings, structureAware: false, targetTokens: 20, maxTokens: 30 }, words));
    const chunks = generateChunks(bs, p, words);
    expect(chunks.length).toBeGreaterThan(2);
    for (const c of chunks.slice(0, -2)) expect(c.text.endsWith(',')).toBe(true);
    expect(chunks.every((c) => c.tokenCount <= 30)).toBe(true);
  });

  it('auto-split cuts a long CJK sentence after its full-width commas', () => {
    const chars = (text: string): number => [...text].length;
    const claim = lined('b1', Array.from({ length: 10 }, (_, i) => `\u7b2c${i}\u9805の\u69cb\u6210を\u542bみ、`).join('') + '\u7d42わる。');
    const bs = [claim, block('b2', 'paragraph', 'end')];
    const p = plan(autoSplit(bs, { ...settings, structureAware: false, targetTokens: 25, maxTokens: 40 }, chars));
    const chunks = generateChunks(bs, p, chars);
    expect(chunks.length).toBeGreaterThan(2);
    expect(chunks[0]!.text.endsWith('、')).toBe(true);
    expect(chunks.every((c) => c.tokenCount <= 40)).toBe(true);
  });

  it('cuts a long table between rows and repeats its header in every piece', () => {
    const rows = Array.from({ length: 12 }, (_, i) => `| r${i} | a b c |`);
    const text = ['| key | value |', '| --- | --- |', ...rows].join('\n');
    const table: DocumentBlock = { ...lined('b1', text), type: 'table', markdown: text };
    const bs = [table, block('b2', 'paragraph', 'end')];
    const p = plan(autoSplit(bs, { ...settings, structureAware: false, targetTokens: 20, maxTokens: 30 }, words));
    expect(validatePlan(bs, p)).toEqual([]);
    const chunks = generateChunks(bs, p, words);
    const pieces = chunks.filter((c) => c.blockIds.includes('b1'));
    expect(pieces.length).toBeGreaterThan(2);
    for (const c of pieces) {
      expect(c.text.startsWith('| key | value |\n| --- | --- |\n| r')).toBe(true);
      expect(c.tokenCount).toBeLessThanOrEqual(30);
    }
    // Every row is in exactly one piece.
    expect(pieces.map((c) => c.text.split('\n').slice(2)).flat().filter((l) => l && !l.startsWith('end'))).toEqual(rows);
  });

  it('cuts a long table that has its caption with it', () => {
    const rows = Array.from({ length: 12 }, (_, i) => `| r${i} | a b c |`);
    const text = ['| key | value |', '| --- | --- |', ...rows].join('\n');
    const table: DocumentBlock = { ...lined('b2', text), type: 'table', markdown: text };
    const bs = [block('b1', 'caption', 'Table 1. Values'), table, block('b3', 'paragraph', 'end')];
    const p = plan(autoSplit(bs, { ...settings, structureAware: false, targetTokens: 20, maxTokens: 30 }, words));
    const chunks = generateChunks(bs, p, words);
    expect(chunks[0]!.text.startsWith('Table 1. Values\n\n| key | value |')).toBe(true);
    expect(chunks.every((c) => c.tokenCount <= 30)).toBe(true);
  });

  it('cuts a table that only its caption under it pushes past the maximum', () => {
    const text = ['| key | value |', '| --- | --- |', '| r0 | a b c |', '| r1 | a b c |'].join('\n');
    const table: DocumentBlock = { ...lined('b1', text), type: 'table', markdown: text };
    const bs = [table, block('b2', 'caption', 'Table 1. Values of the run'), block('b3', 'paragraph', 'end')];
    const p = plan(autoSplit(bs, { ...settings, structureAware: false, targetTokens: 20, maxTokens: 30 }, words));
    const chunks = generateChunks(bs, p, words);
    expect(chunks.every((c) => c.tokenCount <= 30)).toBe(true);
    expect(chunks.find((c) => c.blockIds.includes('b2'))!.text).toContain('| r1 | a b c |');
  });

  it('cuts a long table that has its heading and caption with it', () => {
    const rows = Array.from({ length: 12 }, (_, i) => `| r${i} | a b c |`);
    const text = ['| key | value |', '| --- | --- |', ...rows].join('\n');
    const table: DocumentBlock = { ...lined('b3', text), type: 'table', markdown: text };
    const bs = [block('b1', 'section_header', 'Results'), block('b2', 'caption', 'Table 1. Values'), table, block('b4', 'paragraph', 'end')];
    const p = plan(autoSplit(bs, { ...settings, structureAware: false, targetTokens: 20, maxTokens: 30 }, words));
    const chunks = generateChunks(bs, p, words);
    expect(chunks[0]!.text.startsWith('Results\n\nTable 1. Values\n\n| key | value |')).toBe(true);
    expect(chunks.every((c) => c.tokenCount <= 30)).toBe(true);
  });

  it('sizes the first piece of a long table by what is left in its chunk', () => {
    const rows = Array.from({ length: 12 }, (_, i) => `| r${i} | a b c |`);
    const text = ['| key | value |', '| --- | --- |', ...rows].join('\n');
    const table: DocumentBlock = { ...lined('b3', text), type: 'table', markdown: text };
    const bs = [block('b1', 'paragraph', lorem(12)), block('b2', 'caption', 'Table 1. Values'), table, block('b4', 'paragraph', 'end')];
    const p = plan(autoSplit(bs, { ...settings, structureAware: false, targetTokens: 20, maxTokens: 30 }, words));
    const chunks = generateChunks(bs, p, words);
    expect(chunks.every((c) => c.tokenCount <= 30)).toBe(true);
    // The caption goes with the table, not with the paragraph before it.
    expect(chunks.find((c) => c.blockIds.includes('b2'))!.blockIds).toContain('b3');
  });

  it('cuts a table only at a row start, and leaves tables without lines whole', () => {
    const text = ['| k | v |', '| --- | --- |', '| a | 1 |', '| b | 2 |', '| c | 3 |'].join('\n');
    const table: DocumentBlock = { ...lined('b1', text), type: 'table', markdown: text };
    const bs = [table, block('b2', 'paragraph', 'end')];
    const rowB = text.indexOf('| b');
    expect(splitChunk(plan([]), bs, { blockId: 'b1', offset: rowB }).boundaries).toEqual([{ blockId: 'b1', offset: rowB }]);
    // Inside a row, or before the first data row, is not a place to cut.
    expect(splitChunk(plan([]), bs, { blockId: 'b1', offset: rowB + 3 }).boundaries).toEqual([]);
    expect(splitChunk(plan([]), bs, { blockId: 'b1', offset: text.indexOf('| a') }).boundaries).toEqual([]);
    // A saved plan with an offset inside a row is read as the nearest row start.
    expect(validatePlan(bs, plan([{ blockId: 'b1', offset: rowB + 3 }]))).not.toEqual([]);
    const plain: DocumentBlock = { ...block('b1', 'table', text), markdown: text };
    expect(splitChunk(plan([]), [plain, bs[1]!], { blockId: 'b1', offset: rowB }).boundaries).toEqual([]);
  });

  it('auto-split breaks an oversized paragraph at sentence ends', () => {
    const long = lined('b1', Array.from({ length: 12 }, (_, i) => `Sentence number ${i} has five words.`).join(' '));
    const bs = [long, block('b2', 'paragraph', 'end')];
    const p = plan(autoSplit(bs, { ...settings, structureAware: false, targetTokens: 20, maxTokens: 30 }, words));
    const chunks = generateChunks(bs, p, words);
    expect(chunks.length).toBeGreaterThan(2);
    for (const c of chunks.slice(0, -1)) expect(c.text.endsWith('.')).toBe(true);
    expect(chunks.every((c) => c.tokenCount <= 30)).toBe(true);
  });
});

describe('autoSplit', () => {
  const s = (over: Partial<ChunkSettings>): ChunkSettings => ({ ...settings, ...over });

  it('packs blocks up to the target', () => {
    const blocks = Array.from({ length: 6 }, (_, i) => block(`b${i + 1}`, 'paragraph', lorem(4)));
    // 4 + 1 + 4 = 9 fits a target of 10; adding a third would make 14.
    expect(splitIds(blocks, s({ structureAware: false }), words)).toEqual(['b2', 'b4']);
  });

  it('never leaves a heading at the end of a chunk', () => {
    const blocks = [block('b1', 'paragraph', lorem(8)), block('b2', 'section_header', 'Next'), block('b3', 'paragraph', lorem(8))];
    expect(splitIds(blocks, s({ structureAware: false, keepHeadingWithBody: true }), words)).toEqual(['b1']);
    expect(splitIds(blocks, s({ structureAware: false, keepHeadingWithBody: false }), words)).toEqual(['b2']);
  });

  it('keeps a heading with a large block after it rather than leaving it alone', () => {
    // 1 + 1 + 15 is over the target of 10 but within the maximum of 20.
    const blocks = [block('b1', 'section_header', 'Drawing'), block('b2', 'picture', lorem(15)), block('b3', 'paragraph', lorem(3))];
    expect(splitIds(blocks, s({ structureAware: true }), words)).toEqual(['b2']);
    // Past the maximum it has to stand alone.
    const huge = [block('b1', 'section_header', 'Drawing'), block('b2', 'picture', lorem(25))];
    expect(splitIds(huge, s({ structureAware: true }), words)).toEqual(['b1']);
  });

  it('starts a new chunk at a heading once the chunk is substantial', () => {
    const blocks = [block('b1', 'paragraph', lorem(3)), block('b2', 'section_header', 'H'), block('b3', 'paragraph', lorem(2))];
    expect(splitIds(blocks, s({ structureAware: true }), words)).toEqual(['b1']);
    expect(splitIds(blocks, s({ structureAware: false }), words)).toEqual([]);
    // Two headings in a row stay together.
    const stacked = [block('b1', 'title', 'Doc'), block('b2', 'section_header', 'Part'), block('b3', 'paragraph', lorem(2))];
    expect(splitIds(stacked, s({ structureAware: true }), words)).toEqual([]);
  });

  it('keeps a caption with its table unless that exceeds max', () => {
    const blocks = [block('b1', 'paragraph', lorem(5)), block('b2', 'table', lorem(4)), block('b3', 'caption', lorem(3))];
    expect(splitIds(blocks, s({ structureAware: false }), words)).toEqual([]);
    const big = [block('b1', 'table', lorem(15)), block('b2', 'caption', lorem(6))];
    expect(splitIds(big, s({ structureAware: false }), words)).toEqual([]);
  });

  it('keeps a short caption with a table that is too large for one chunk', () => {
    // Caption above: the chunk breaks before the caption, not between caption and table.
    const above = [block('b1', 'paragraph', lorem(6)), block('b2', 'caption', lorem(2)), block('b3', 'table', lorem(25))];
    expect(splitIds(above, s({ structureAware: false }), words)).toEqual(['b1']);
    // Caption below: it stays in the table's chunk.
    const below = [block('b1', 'table', lorem(25)), block('b2', 'caption', lorem(2)), block('b3', 'paragraph', lorem(6))];
    expect(splitIds(below, s({ structureAware: false }), words)).toEqual(['b2']);
  });

  it('cuts inside a table its headings push past the maximum', () => {
    const rows = Array.from({ length: 3 }, (_, i) => `| r${i} | v |`);
    const text = ['| key | value |', '| --- | --- |', ...rows].join('\n');
    const table: DocumentBlock = { ...lined('b3', text), type: 'table', markdown: text };
    const bs = [block('b1', 'section_header', 'Targets'), block('b2', 'section_header', 'Twenty data sets'), table];
    // The table alone fits the maximum; with the two headings it goes with, it would not.
    const p = plan(autoSplit(bs, { ...settings, targetTokens: 20, maxTokens: 30 }, words));
    const chunks = generateChunks(bs, p, words);
    expect(chunks.every((c) => c.tokenCount <= 30)).toBe(true);
    expect(chunks[0]!.blockIds).toEqual(['b1', 'b2', 'b3']);
  });

  it('cuts a long run between two commas between words', () => {
    // A list of cited patents: no sentence end, one comma, far more words than the maximum.
    const text = `${lorem(4)}, ${lorem(60)}`;
    const item: DocumentBlock = { ...lined('b1', text), type: 'paragraph' };
    const chunks = generateChunks([item], plan(autoSplit([item], { ...settings, targetTokens: 20, maxTokens: 30 }, words)), words);
    expect(chunks.every((c) => c.tokenCount <= 30)).toBe(true);
  });

  it('cuts inside a long sentence its heading pushes past the maximum', () => {
    const item: DocumentBlock = { ...lined('b2', `${lorem(12)}, ${lorem(12)}`), type: 'list_item' };
    const bs = [block('b1', 'section_header', 'Means for solving the problem'), item];
    const chunks = generateChunks(bs, plan(autoSplit(bs, { ...settings, targetTokens: 20, maxTokens: 30 }, words)), words);
    expect(chunks.every((c) => c.tokenCount <= 30)).toBe(true);
    expect(chunks[0]!.blockIds).toEqual(['b1', 'b2']);
  });

  it('keeps a caption under a figure with that figure, not with the next one', () => {
    const blocks = [
      block('b1', 'picture', lorem(3)), block('b2', 'caption', `Fig. 7: ${lorem(1)}`),
      block('b3', 'picture', lorem(3)), block('b4', 'caption', `Fig. 8: ${lorem(1)}`),
    ];
    expect(splitIds(blocks, s({ structureAware: false }), words)).toEqual(['b2']);
    // A table's caption after a picture belongs to the table below it.
    const mixed = [block('b1', 'picture', lorem(9)), block('b2', 'caption', `Table 3: ${lorem(1)}`), block('b3', 'table', lorem(6))];
    expect(splitIds(mixed, s({ structureAware: false }), words)).toEqual(['b1']);
    // Two captions under a picture (one per language) are both its own.
    const twice = [
      block('b1', 'picture', lorem(3)), block('b2', 'caption', `Fig. 4. ${lorem(1)}`), block('b3', 'caption', `그림 4. ${lorem(1)}`),
      block('b4', 'picture', lorem(3)), block('b5', 'caption', `Fig. 5. ${lorem(1)}`),
    ];
    expect(splitIds(twice, s({ structureAware: false }), words)).toEqual(['b3']);
    // Unnumbered titles over the figures of a page: the one between two goes with the figure after
    // it, as the first one did...
    const titles = [
      block('b1', 'caption', 'Greek capitals()'), block('b2', 'picture', lorem(12)),
      block('b3', 'caption', 'Greek small letters()'), block('b4', 'picture', lorem(12)),
    ];
    expect(splitIds(titles, s({ structureAware: false, targetTokens: 16, maxTokens: 20 }), words)).toEqual(['b2']);
    // ...and with the one before when the first one was set under its figure.
    const under = [titles[1]!, titles[0]!, titles[3]!, titles[2]!, block('b5', 'picture', lorem(12))].map((b, i) => ({ ...b, id: `c${i}`, readingOrder: i }));
    expect(splitIds(under, s({ structureAware: false, targetTokens: 16, maxTokens: 20 }), words)).toEqual(['c1', 'c3']);
    // A subfigure label under its plot, after a numbered title above the figure, stays with its plot.
    const sub = [
      block('b1', 'caption', 'Fig. 4'), block('b2', 'picture', lorem(12)), block('b3', 'caption', '(a) DC voltage'),
      block('b4', 'picture', lorem(12)), block('b5', 'caption', '(b) DC power'),
    ];
    expect(splitIds(sub, s({ structureAware: false, targetTokens: 16, maxTokens: 20 }), words)).toEqual(['b3']);
  });

  it('keeps a caption above a table with that table when the tables before had theirs above too', () => {
    const blocks = [
      block('b1', 'paragraph', lorem(2)),
      block('b2', 'caption', `Table 2: ${lorem(1)}`), block('b3', 'table', lorem(5)),
      block('b4', 'caption', `Table 3: ${lorem(1)}`), block('b5', 'table', lorem(5)),
      block('b6', 'caption', `Table 4: ${lorem(1)}`), block('b7', 'table', lorem(5)),
    ];
    expect(splitIds(blocks, s({ structureAware: false }), words)).toEqual(['b3', 'b5']);
    // A note under a table, not labelled, stays with it.
    const noted = [
      block('b1', 'caption', `Table 2: ${lorem(1)}`), block('b2', 'table', lorem(6)), block('b3', 'caption', `Source: ${lorem(1)}`),
      block('b4', 'caption', `Table 3: ${lorem(1)}`), block('b5', 'table', lorem(6)),
    ];
    expect(splitIds(noted, s({ structureAware: false }), words)).toEqual(['b3']);
    // Two labels between two tables: the second, of another number, is the next table's.
    const paired = [
      block('b1', 'table', lorem(6)), block('b2', 'caption', `Table 1: ${lorem(1)}`),
      block('b3', 'caption', `Table 2: ${lorem(1)}`), block('b4', 'table', lorem(6)),
    ];
    expect(splitIds(paired, s({ structureAware: false }), words)).toEqual(['b2']);
    // A label left at the foot of the page before (after stray text) is no caption above a figure:
    // the figure's own label under it stays its own.
    const sheets = [
      block('b1', 'picture', lorem(3), 0), block('b2', 'paragraph', '400 600', 0), block('b3', 'caption', 'FIG. 4', 0),
      block('b4', 'picture', lorem(6), 1), block('b5', 'caption', 'FIG. 5', 1), block('b6', 'picture', lorem(6), 2),
    ];
    expect(splitIds(sheets, s({ structureAware: false }), words)).toEqual(['b5']);
    // A caption on the page of the figure after it only is that one's, whatever came before.
    const paged = [
      block('b1', 'picture', lorem(6), 0), block('b2', 'caption', `${lorem(1)} source`, 0),
      block('b3', 'caption', `${lorem(1)} label`, 1), block('b4', 'picture', lorem(6), 1),
    ];
    expect(splitIds(paged, s({ structureAware: false }), words)).toEqual(['b2']);
  });

  it('keeps a short caption with a table above it that is too large for any chunk', () => {
    const blocks = [
      // The chunk before is full: the caption starts the next one.
      block('b1', 'paragraph', lorem(19)), block('b2', 'caption', `Table 5: ${lorem(1)}`), block('b3', 'table', lorem(25)),
      block('b4', 'paragraph', lorem(3)),
    ];
    expect(splitIds(blocks, s({ structureAware: false }), words)).toEqual(['b1', 'b3']);
  });

  it('moves the sentence that introduces a formula along with it when the chunk is full', () => {
    const blocks = [
      block('b1', 'paragraph', lorem(14)),
      block('b2', 'paragraph', `${lorem(2)} we obtain:`),
      block('b3', 'formula', 'E = mc^2 + p'),
    ];
    // b1 + b2 fit the maximum (20); the formula would not: b2 goes on with it.
    expect(splitIds(blocks, s({ structureAware: false }), words)).toEqual(['b1']);
    // The same when the formula is full and its "where …" would not fit: all three go on.
    const tail = [
      block('b1', 'paragraph', lorem(10)),
      block('b2', 'paragraph', `${lorem(1)} we obtain:`),
      block('b3', 'formula', 'E = mc^2'),
      block('b4', 'paragraph', `where ${lorem(3)}`),
    ];
    expect(splitIds(tail, s({ structureAware: false }), words)).toEqual(['b1']);
  });

  it('keeps text ending in a colon with the list it announces', () => {
    const colon = (text: string) => block('b2', 'paragraph', `${lorem(2)} ${text}`);
    const blocks = (lead: DocumentBlock, item: number) => [block('b1', 'paragraph', lorem(5)), lead, block('b3', 'list_item', lorem(item))];
    // Past the target the list item stays with "…are:"; past the maximum "…are:" goes on with it.
    expect(splitIds(blocks(colon('contributions are:'), 6), s({ structureAware: false }), words)).toEqual([]);
    expect(splitIds(blocks(colon('contributions are:'), 12), s({ structureAware: false }), words)).toEqual(['b1']);
    // A chunk mostly full already ends before "…claimed is:" rather than growing past its list.
    const full = [block('b1', 'paragraph', lorem(8)), block('b2', 'paragraph', 'claims:'), block('b3', 'list_item', lorem(4))];
    expect(splitIds(full, s({ structureAware: false }), words)).toEqual(['b1']);
    // Without the colon nothing is announced.
    expect(splitIds(blocks(colon('contributions follow.'), 6), s({ structureAware: false }), words)).toEqual(['b2']);
    expect(splitIds(blocks(colon('contributions follow.'), 12), s({ structureAware: false }), words)).toEqual(['b2']);
  });

  it('starts a slide\'s chunk with the logo set before its title', () => {
    const slides = [
      block('b1', 'section_header', 'Overview', 0), block('b2', 'paragraph', lorem(14), 0),
      block('b3', 'picture', '', 1), block('b4', 'section_header', 'Units', 1), block('b5', 'paragraph', lorem(6), 1),
    ];
    expect(splitIds(slides, s({ targetTokens: 18, maxTokens: 25 }), words)).toEqual(['b2']);
    // A small slide (logo, title, a picture of a few words) stays a chunk of its own.
    const small = [
      block('b1', 'section_header', 'Overview', 0), block('b2', 'paragraph', lorem(45), 0),
      block('b3', 'picture', '', 1), block('b4', 'section_header', 'Units', 1), block('b5', 'picture', 'org chart', 1),
    ];
    expect(splitIds(small, s({ targetTokens: 50, maxTokens: 60 }), words)).toEqual(['b2']);
    // Nor is a logo left alone when its heading goes on into a section too large to share a chunk.
    const big = [
      block('b1', 'paragraph', lorem(16), 0), block('b2', 'picture', '', 1), block('b3', 'section_header', 'Units', 1),
      { ...lined('b4', Array.from({ length: 8 }, (_, k) => `Sentence ${k} has five words.`).join(' ')), regions: [{ pageIndex: 1, bbox: { x0: 0.1, y0: 0.3, x1: 0.9, y1: 0.9 } }] },
    ];
    const chunks = generateChunks(big, plan(autoSplit(big, s({ targetTokens: 18, maxTokens: 25 }), words)), words);
    expect(chunks[1]!.blockIds.slice(0, 3)).toEqual(['b2', 'b3', 'b4']);
    // A picture with text before a heading is content of its own and stays where it is.
    const content = small.map((b) => (b.id === 'b3' ? { ...b, text: 'Revenue by unit' } : b));
    expect(splitIds(content, s({ targetTokens: 60, maxTokens: 70 }), words)).toEqual(['b3']);
    expect(splitIds(small, s({ targetTokens: 60, maxTokens: 70 }), words)).toEqual(['b2']);
    // A picture ending a section on the heading's page stays where it is.
    const onePage = slides.map((b) => ({ ...b, regions: [{ ...b.regions[0]!, pageIndex: 0 }] }));
    expect(splitIds(onePage, s({ targetTokens: 18, maxTokens: 25 }), words)).toEqual(['b3']);
  });

  it('reads the notes at the foot of the pages after the text, in chunks of their own', () => {
    const placed = (b: DocumentBlock, y0: number, y1: number): DocumentBlock => ({ ...b, regions: [{ ...b.regions[0]!, bbox: { x0: 0.1, y0, x1: 0.9, y1 } }] });
    const blocks = [
      placed(block('b1', 'paragraph', lorem(8), 0), 0.1, 0.5), placed(block('b2', 'footnote', '* Supported by a grant', 0), 0.9, 0.95),
      placed(block('b3', 'paragraph', lorem(8), 1), 0.1, 0.5), placed(block('b4', 'footnote', '1) Author address', 1), 0.9, 0.95),
    ];
    const chunks = generateChunks(blocks, plan(autoSplit(blocks, s({ structureAware: false, targetTokens: 20, maxTokens: 30 }), words)), words);
    expect(chunks.map((c) => c.blockIds)).toEqual([['b1', 'b3'], ['b2', 'b4']]);
    // A note right under a table (after its caption) is the table's and stays with it.
    const table = [
      placed(block('b1', 'table', lorem(4), 0), 0.1, 0.3), placed(block('b2', 'caption', 'Table 1. Results', 0), 0.31, 0.33),
      placed(block('b3', 'footnote', '* p < .05', 0), 0.34, 0.36), placed(block('b4', 'footnote', '* A page note', 0), 0.9, 0.95),
      placed(block('b5', 'paragraph', lorem(4), 1), 0.1, 0.3),
    ];
    expect(chunkableBlocks(table, settings).map((b) => b.id)).toEqual(['b1', 'b2', 'b3', 'b5', 'b4']);
  });

  it('joins a chunk of a few tokens to a neighbour with room for it', () => {
    const tight = s({ structureAware: false, targetTokens: 18, maxTokens: 25 });
    // A lone "*" between two full chunks goes with the chunk before...
    const lone = [block('b1', 'paragraph', lorem(17)), block('b2', 'list_item', '*'), block('b3', 'paragraph', lorem(17))];
    expect(splitIds(lone, tight, words)).toEqual(['b2']);
    // ...or, with no room there, with the chunk after.
    const full = [block('b1', 'paragraph', lorem(24)), block('b2', 'list_item', '*'), block('b3', 'paragraph', lorem(17))];
    expect(splitIds(full, tight, words)).toEqual(['b1']);
    // A small section between two others stays apart.
    const section = [
      block('b1', 'paragraph', lorem(48)), block('b2', 'section_header', 'Code'), block('b3', 'paragraph', lorem(2)),
      block('b4', 'section_header', 'Data'), block('b5', 'paragraph', lorem(48)),
    ];
    expect(splitIds(section, s({ targetTokens: 50, maxTokens: 60 }), words)).toEqual(['b1', 'b3']);
    // A chunk with nothing to read (a picture without text) joins the chunk before even when it is full.
    const empty = [block('b1', 'paragraph', lorem(12)), block('b2', 'paragraph', lorem(12)), block('b3', 'picture', '')];
    expect(splitIds(empty, s({ structureAware: false, targetTokens: 25, maxTokens: 25 }), words)).toEqual([]);
    const two = [...empty, block('b4', 'picture', '')];
    expect(splitIds(two, s({ structureAware: false, targetTokens: 25, maxTokens: 25 }), words)).toEqual([]);
  });

  it('cuts a long run of formulas and their text after a formula, not before one', () => {
    const blocks = [
      block('b1', 'paragraph', `${lorem(4)} we have`), block('b2', 'formula', lorem(4)),
      block('b3', 'paragraph', `and ${lorem(3)} we get`), block('b4', 'formula', lorem(4)),
      block('b5', 'paragraph', `so ${lorem(2)} we have`), block('b6', 'formula', lorem(4)),
    ];
    // All of it glued and over the maximum (20): chunks end after a formula, never before one.
    expect(splitIds(blocks, s({ structureAware: false }), words)).toEqual(['b2', 'b4']);
  });

  it('keeps a Korean sentence that goes on after a formula with it', () => {
    const blocks = [
      block('b1', 'paragraph', `${lorem(8)} covariance update는`),
      block('b2', 'formula', 'E = mc^2'),
      block('b3', 'paragraph', `와 같다. ${lorem(2)}`),
      block('b4', 'paragraph', `이 식에서 ${lorem(8)}`),
    ];
    expect(splitIds(blocks, s({ structureAware: false }), words)).toEqual(['b3']);
    // The space after the particle is often lost in extraction.
    const joined = blocks.map((b) => (b.id === 'b3' ? { ...b, text: `가된다. ${lorem(2)}` } : b));
    expect(splitIds(joined, s({ structureAware: false }), words)).toEqual(['b3']);
  });

  it('keeps a display formula with the text that introduces and continues it', () => {
    const blocks = [
      block('b1', 'paragraph', `${lorem(8)} is given by`),
      block('b2', 'formula', 'E = mc^2'),
      block('b3', 'paragraph', `where ${lorem(2)}`),
      block('b4', 'paragraph', `Next ${lorem(8)}`),
    ];
    expect(splitIds(blocks, s({ structureAware: false }), words)).toEqual(['b3']);
    // "Here x is …" explains the formula's symbols as "where" does; "Here we …" starts anew.
    const here = (text: string) => blocks.map((b) => (b.id === 'b3' ? { ...b, text } : b));
    expect(splitIds(here('Here, x is y'), s({ structureAware: false }), words)).toEqual(['b3']);
    expect(splitIds(here(`Here we ${lorem(1)}`), s({ structureAware: false }), words)).toEqual(['b2', 'b3']);
    // Over max the formula still goes to its own chunk rather than overflowing.
    const big = [block('b1', 'paragraph', lorem(15)), block('b2', 'formula', lorem(8))];
    expect(splitIds(big, s({ structureAware: false }), words)).toEqual(['b1']);
  });

  it('isolates a block that is larger than max', () => {
    const blocks = [block('b1', 'paragraph', lorem(3)), block('b2', 'table', lorem(30)), block('b3', 'paragraph', lorem(3))];
    const p = plan(autoSplit(blocks, s({ structureAware: false }), words));
    const chunks = expectPartition(blocks, p);
    expect(chunks.map((c) => c.blockIds)).toEqual([['b1'], ['b2'], ['b3']]);
    expect(chunks[1]!.overMax).toBe(true);
  });

  it('creates and resets a plan', () => {
    const blocks = Array.from({ length: 6 }, (_, i) => block(`b${i + 1}`, 'paragraph', lorem(4)));
    const created = createSplitPlan('doc1', 'fp1', blocks, s({ structureAware: false }), words);
    expect(ids(created)).toEqual(['b2', 'b4']);
    const edited = removeBoundary(created, after('b2'));
    const reset = resetSplitPlan(edited, blocks, words);
    expect(ids(reset)).toEqual(['b2', 'b4']);
    const bigger = resetSplitPlan(edited, blocks, words, s({ structureAware: false, targetTokens: 14 }));
    expect(ids(bigger)).toEqual(['b3']);
    expect(bigger.settings.targetTokens).toBe(14);
  });
});

describe('validatePlan', () => {
  it('reports unknown, duplicate, trailing and unordered boundaries', () => {
    expect(validatePlan(five, plan(['zz']))).toEqual(['unknown block zz']);
    expect(validatePlan(five, plan(['b2', 'b2']))).toEqual(['duplicate boundary b2']);
    expect(validatePlan(five, plan(['b5']))).toEqual(['boundary after the last block b5']);
    expect(validatePlan(five, plan([at('b2', 3)]))).toEqual(['invalid offset b2@3']);
    expect(validatePlan(five, plan(['b3', 'b1']))).toEqual(['boundary b1 out of reading order']);
  });
});

describe('history', () => {
  it('undoes and redoes, ignoring no-op edits', () => {
    const p0 = plan([]);
    let h = createHistory(p0);
    const p1 = addBoundary(p0, five, after('b2'));
    h = pushHistory(h, p1);
    h = pushHistory(h, addBoundary(p1, five, after('b2')));
    expect(h.past).toHaveLength(1);
    const p2 = addBoundary(p1, five, after('b4'));
    h = pushHistory(h, p2);
    h = undo(h);
    expect(h.present).toBe(p1);
    expect(canRedo(h)).toBe(true);
    h = undo(undo(h));
    expect(h.present).toBe(p0);
    expect(canUndo(h)).toBe(false);
    h = redo(redo(h));
    expect(h.present).toBe(p2);
    h = undo(h);
    h = pushHistory(h, removeBoundary(h.present, after('b2')));
    expect(canRedo(h)).toBe(false);
  });

  it('caps the number of undo steps', () => {
    let h = createHistory(plan([]));
    for (let i = 0; i < 10; i++) h = pushHistory(h, plan([`b${i}`]), 3);
    expect(h.past).toHaveLength(3);
  });
});
