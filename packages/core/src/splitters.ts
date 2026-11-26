/*
 * Common text splitters, re-implemented so their output can be shown on the original pages.
 *
 * `recursiveCharacterSplit`, `characterSplit` and `tokenSplit` follow LangChain's
 * RecursiveCharacterTextSplitter, CharacterTextSplitter and TokenTextSplitter
 * (langchain-text-splitters 1.x) and are checked against its real output in the tests.
 * Lengths in characters count Unicode code points, like Python's len().
 */
import { autoSplit, blockMarkdown, chunkableBlocks, chunkSegments, isHeading, normalizeBoundaries, BLOCK_SEPARATOR } from './plan';
import { markdownPrefix, sentenceStarts } from './text';
import type { Boundary, ChunkSettings, DocumentBlock, SplitPlan, TokenCounter, Tokenizer } from './types';

export type LengthFn = (text: string) => number;

export const codePointLength: LengthFn = (text) => {
  let n = 0;
  for (const _ of text) n++;
  return n;
};

/* LangChain-compatible splitters ----------------------------------------------------------------- */

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** `_split_text_with_regex` with keep_separator=True (separator kept at the start of pieces). */
function splitKeepingSeparator(text: string, separator: string, keep: boolean): string[] {
  let splits: string[];
  if (separator) {
    if (keep) {
      const parts = text.split(new RegExp(`(${escapeRegExp(separator)})`));
      const joined: string[] = [];
      for (let i = 1; i < parts.length; i += 2) joined.push(parts[i]! + (parts[i + 1] ?? ''));
      if (parts.length % 2 === 0) joined.push(...parts.slice(-1));
      splits = [parts[0]!, ...joined];
    } else {
      splits = text.split(separator);
    }
  } else {
    splits = [...text];
  }
  return splits.filter((s) => s.length > 0);
}

function joinDocs(docs: string[], separator: string): string | null {
  const text = docs.join(separator).trim();
  return text || null;
}

/** `TextSplitter._merge_splits`. */
function mergeSplits(splits: string[], separator: string, chunkSize: number, chunkOverlap: number, length: LengthFn): string[] {
  const sepLen = length(separator);
  const docs: string[] = [];
  let current: string[] = [];
  let total = 0;
  for (const d of splits) {
    const len = length(d);
    if (total + len + (current.length > 0 ? sepLen : 0) > chunkSize) {
      if (current.length > 0) {
        const doc = joinDocs(current, separator);
        if (doc !== null) docs.push(doc);
        while (total > chunkOverlap || (total + len + (current.length > 0 ? sepLen : 0) > chunkSize && total > 0)) {
          total -= length(current[0]!) + (current.length > 1 ? sepLen : 0);
          current = current.slice(1);
        }
      }
    }
    current.push(d);
    total += len + (current.length > 1 ? sepLen : 0);
  }
  const doc = joinDocs(current, separator);
  if (doc !== null) docs.push(doc);
  return docs;
}

export interface CharacterSplitOptions {
  chunkSize: number;
  chunkOverlap: number;
  length?: LengthFn;
}

function checkSizes({ chunkSize, chunkOverlap }: CharacterSplitOptions): void {
  if (chunkSize <= 0) throw new Error('chunkSize must be > 0');
  if (chunkOverlap < 0 || chunkOverlap > chunkSize) throw new Error('chunkOverlap must be between 0 and chunkSize');
}

/** LangChain RecursiveCharacterTextSplitter (separators ["\n\n", "\n", " ", ""], keep_separator=True). */
export function recursiveCharacterSplit(
  text: string,
  options: CharacterSplitOptions & { separators?: string[] },
): string[] {
  checkSizes(options);
  const length = options.length ?? codePointLength;
  const { chunkSize, chunkOverlap } = options;
  const run = (input: string, separators: string[]): string[] => {
    const final: string[] = [];
    let separator = separators[separators.length - 1]!;
    let rest: string[] = [];
    for (let i = 0; i < separators.length; i++) {
      const s = separators[i]!;
      if (!s) {
        separator = s;
        break;
      }
      if (input.includes(s)) {
        separator = s;
        rest = separators.slice(i + 1);
        break;
      }
    }
    const splits = splitKeepingSeparator(input, separator, true);
    let good: string[] = [];
    for (const s of splits) {
      if (length(s) < chunkSize) {
        good.push(s);
      } else {
        if (good.length) {
          final.push(...mergeSplits(good, '', chunkSize, chunkOverlap, length));
          good = [];
        }
        if (!rest.length) final.push(s);
        else final.push(...run(s, rest));
      }
    }
    if (good.length) final.push(...mergeSplits(good, '', chunkSize, chunkOverlap, length));
    return final;
  };
  return run(text, options.separators ?? ['\n\n', '\n', ' ', '']);
}

/** LangChain CharacterTextSplitter (keep_separator=False: the separator is re-inserted when merging). */
export function characterSplit(text: string, options: CharacterSplitOptions & { separator?: string }): string[] {
  checkSizes(options);
  const separator = options.separator ?? '\n\n';
  const splits = splitKeepingSeparator(text, separator, false);
  return mergeSplits(splits, separator, options.chunkSize, options.chunkOverlap, options.length ?? codePointLength);
}

export interface TextRange {
  start: number;
  end: number;
}

/**
 * LangChain TokenTextSplitter: fixed windows of token ids. Returns the decoded strings and their
 * character ranges (from decoding the token prefix, which stays exact unless a window boundary
 * falls inside a multi-byte character).
 */
export function tokenSplit(
  text: string,
  options: { chunkSize: number; chunkOverlap: number; tokenizer: Tokenizer },
): { chunks: string[]; ranges: TextRange[] } {
  const { chunkSize, chunkOverlap, tokenizer } = options;
  if (chunkSize <= chunkOverlap) throw new Error('chunkSize must be greater than chunkOverlap');
  const ids = tokenizer.encode(text);
  const chunks: string[] = [];
  const ranges: TextRange[] = [];
  const offsetOf = (n: number) => Math.min(text.length, tokenizer.decode(ids.slice(0, n)).length);
  let start = 0;
  while (start < ids.length) {
    const end = Math.min(start + chunkSize, ids.length);
    const decoded = tokenizer.decode(ids.slice(start, end));
    if (decoded) {
      chunks.push(decoded);
      ranges.push({ start: offsetOf(start), end: offsetOf(end) });
    }
    if (end === ids.length) break;
    start += chunkSize - chunkOverlap;
  }
  return { chunks, ranges };
}

/** Find each chunk in the text, in order (like LangChain's add_start_index). */
export function locateChunks(text: string, chunks: string[]): TextRange[] {
  const ranges: TextRange[] = [];
  let from = 0;
  for (const chunk of chunks) {
    let start = text.indexOf(chunk, from);
    if (start < 0) start = text.indexOf(chunk);
    if (start < 0) start = from;
    ranges.push({ start, end: Math.min(text.length, start + chunk.length) });
    from = start + 1;
  }
  return ranges;
}

/* The document as one text, and back ------------------------------------------------------------- */

export interface DocSpan {
  blockId: string;
  /** Where the block's markdown starts and ends in the document text. */
  start: number;
  end: number;
  /** Length of the markdown prefix (e.g. "## ") before block.text begins. */
  prefix: number;
}

export interface DocText {
  text: string;
  spans: DocSpan[];
}

/** Chunkable blocks' markdown joined with blank lines: the text a splitter sees. */
export function documentText(blocks: readonly DocumentBlock[], settings: ChunkSettings): DocText {
  const spans: DocSpan[] = [];
  let text = '';
  for (const b of chunkableBlocks(blocks, settings)) {
    const md = blockMarkdown(b);
    if (!md.trim()) continue;
    if (text) text += BLOCK_SEPARATOR;
    const prefix = markdownPrefix(b);
    spans.push({ blockId: b.id, start: text.length, end: text.length + md.length, prefix: prefix === null ? -1 : prefix.length });
    text += md;
  }
  return { text, spans };
}

/** The block position at a document offset, as a boundary where a new chunk would start there. */
function boundaryAt(doc: DocText, byId: Map<string, DocumentBlock>, offset: number): Boundary | null {
  for (let i = 0; i < doc.spans.length; i++) {
    const s = doc.spans[i]!;
    if (offset > s.end) continue;
    const block = byId.get(s.blockId)!;
    const local = offset - s.start - Math.max(0, s.prefix);
    if (offset <= s.start || local <= 0) return i > 0 ? { blockId: doc.spans[i - 1]!.blockId } : null;
    if (offset >= s.end || local >= block.text.length) return { blockId: s.blockId };
    // Offsets in atomic blocks are snapped to a block edge by normalizeBoundaries.
    return { blockId: s.blockId, offset: local };
  }
  return null;
}

/**
 * Turn possibly overlapping splitter ranges into editable boundaries: chunk i begins where its new
 * text begins (after the previous chunk's end when they overlap). Cuts inside atomic blocks snap
 * to the nearer block edge.
 */
export function rangesToBoundaries(blocks: readonly DocumentBlock[], settings: ChunkSettings, doc: DocText, ranges: TextRange[]): Boundary[] {
  const byId = new Map(blocks.map((b) => [b.id, b]));
  const cuts: Boundary[] = [];
  for (let i = 1; i < ranges.length; i++) {
    const at = boundaryAt(doc, byId, Math.max(ranges[i]!.start, ranges[i - 1]!.end));
    if (at) cuts.push(at);
  }
  return normalizeBoundaries(chunkableBlocks(blocks, settings), cuts);
}

/** Character ranges of the chunks a plan produces, in document-text coordinates. */
export function planRanges(blocks: readonly DocumentBlock[], plan: SplitPlan, doc: DocText): TextRange[] {
  const span = new Map(doc.spans.map((s) => [s.blockId, s]));
  const byId = new Map(blocks.map((b) => [b.id, b]));
  const toDoc = (blockId: string, offset: number, edge: 'start' | 'end'): number | null => {
    const s = span.get(blockId);
    if (!s) return null;
    const len = byId.get(blockId)!.text.length;
    if (edge === 'start' && offset <= 0) return s.start;
    if (edge === 'end' && offset >= len) return s.end;
    return s.start + Math.max(0, s.prefix) + offset;
  };
  return chunkSegments(blocks, plan)
    .map((segments) => {
      const first = segments.find((g) => span.has(g.blockId));
      const last = [...segments].reverse().find((g) => span.has(g.blockId));
      if (!first || !last) return null;
      return { start: toDoc(first.blockId, first.start, 'start')!, end: toDoc(last.blockId, last.end, 'end')! };
    })
    .filter((r): r is TextRange => r !== null);
}

/** Which parts of which blocks a document range covers. */
export function rangeSegments(blocks: readonly DocumentBlock[], doc: DocText, range: TextRange): { blockId: string; start: number; end: number }[] {
  const byId = new Map(blocks.map((b) => [b.id, b]));
  const out: { blockId: string; start: number; end: number }[] = [];
  for (const s of doc.spans) {
    if (s.end <= range.start || s.start >= range.end) continue;
    const block = byId.get(s.blockId)!;
    const base = s.start + Math.max(0, s.prefix);
    const start = Math.max(0, Math.min(block.text.length, range.start - base));
    const end = Math.max(0, Math.min(block.text.length, range.end - base));
    if (end > start || (block.text.length === 0 && range.start <= s.start)) out.push({ blockId: s.blockId, start, end });
  }
  return out;
}

/* Strategies ------------------------------------------------------------------------------------- */

export type StrategyId = 'structure' | 'recursive' | 'character' | 'token' | 'sentence' | 'markdown';

export interface StrategyParams {
  chunkSize: number;
  chunkOverlap: number;
  /** Length unit for character-based splitters. */
  unit: 'chars' | 'tokens';
  /** Markdown header splitter: start a chunk at headings up to this level (1 = only "#"). */
  headerLevel: number;
}

export const STRATEGY_DEFAULTS: Record<StrategyId, StrategyParams> = {
  structure: { chunkSize: 500, chunkOverlap: 0, unit: 'tokens', headerLevel: 2 },
  recursive: { chunkSize: 1000, chunkOverlap: 200, unit: 'chars', headerLevel: 2 },
  character: { chunkSize: 1000, chunkOverlap: 200, unit: 'chars', headerLevel: 2 },
  token: { chunkSize: 256, chunkOverlap: 32, unit: 'tokens', headerLevel: 2 },
  sentence: { chunkSize: 256, chunkOverlap: 0, unit: 'tokens', headerLevel: 2 },
  markdown: { chunkSize: 0, chunkOverlap: 0, unit: 'tokens', headerLevel: 2 },
};

export interface StrategyResult {
  doc: DocText;
  /** Chunks as document ranges; consecutive ranges may overlap. */
  ranges: TextRange[];
  /** The chunk texts exactly as the splitter returns them. */
  texts: string[];
}

/** Pack whole sentences (never splitting one) into chunks of at most chunkSize tokens. */
function sentencePack(doc: DocText, blocks: readonly DocumentBlock[], chunkSize: number, count: TokenCounter): TextRange[] {
  const byId = new Map(blocks.map((b) => [b.id, b]));
  const units: TextRange[] = [];
  for (const s of doc.spans) {
    const block = byId.get(s.blockId)!;
    const base = s.start + Math.max(0, s.prefix);
    const starts = s.prefix >= 0 ? sentenceStarts(block.text) : [];
    const edges = [s.start, ...starts.map((k) => base + k), s.end];
    for (let i = 0; i < edges.length - 1; i++) units.push({ start: edges[i]!, end: edges[i + 1]! });
  }
  const ranges: TextRange[] = [];
  let cur: TextRange | null = null;
  for (const u of units) {
    if (cur && count(doc.text.slice(cur.start, u.end)) > chunkSize) {
      ranges.push(cur);
      cur = null;
    }
    cur = cur ? { start: cur.start, end: u.end } : { ...u };
  }
  if (cur) ranges.push(cur);
  return ranges;
}

function headerBoundaries(blocks: readonly DocumentBlock[], settings: ChunkSettings, level: number): Boundary[] {
  const seq = chunkableBlocks(blocks, settings);
  const cuts: Boundary[] = [];
  seq.forEach((b, i) => {
    const l = b.type === 'title' ? 0 : (b.level ?? 1);
    if (i > 0 && isHeading(b) && l <= level) cuts.push({ blockId: seq[i - 1]!.id });
  });
  return normalizeBoundaries(seq, cuts);
}

/** A plan seen as a splitter result, so edits can be compared with the presets. */
export function planResult(blocks: readonly DocumentBlock[], plan: SplitPlan): StrategyResult {
  const doc = documentText(blocks, plan.settings);
  const ranges = planRanges(blocks, plan, doc);
  return { doc, ranges, texts: ranges.map((r) => doc.text.slice(r.start, r.end)) };
}

export function runStrategy(
  id: StrategyId,
  params: StrategyParams,
  blocks: readonly DocumentBlock[],
  settings: ChunkSettings,
  tokenizer: Tokenizer,
): StrategyResult {
  const doc = documentText(blocks, settings);
  const length = params.unit === 'tokens' ? tokenizer.count : codePointLength;
  const opts = { chunkSize: params.chunkSize, chunkOverlap: params.chunkOverlap, length };
  const fromPlan = (boundaries: Boundary[]) =>
    planResult(blocks, { documentId: '', blocksFingerprint: '', revision: 0, boundaries, settings });
  switch (id) {
    case 'structure':
      return fromPlan(autoSplit(blocks, { ...settings, targetTokens: params.chunkSize, maxTokens: Math.max(params.chunkSize, settings.maxTokens) }, tokenizer.count));
    case 'markdown':
      return fromPlan(headerBoundaries(blocks, settings, params.headerLevel));
    case 'recursive': {
      const texts = recursiveCharacterSplit(doc.text, opts);
      return { doc, texts, ranges: locateChunks(doc.text, texts) };
    }
    case 'character': {
      const texts = characterSplit(doc.text, opts);
      return { doc, texts, ranges: locateChunks(doc.text, texts) };
    }
    case 'token': {
      const { chunks, ranges } = tokenSplit(doc.text, { chunkSize: params.chunkSize, chunkOverlap: params.chunkOverlap, tokenizer });
      return { doc, texts: chunks, ranges };
    }
    case 'sentence': {
      const ranges = sentencePack(doc, blocks, params.chunkSize, tokenizer.count);
      return { doc, ranges, texts: ranges.map((r) => doc.text.slice(r.start, r.end).trim()) };
    }
  }
}

/* Quality signals -------------------------------------------------------------------------------- */

export interface StrategyMetrics {
  chunks: number;
  avgTokens: number;
  minTokens: number;
  maxTokens: number;
  /** Chunks (except the last) that end in the middle of a sentence. */
  midSentenceCuts: number;
  /** Chunks that end with a heading, cut off from the text it introduces. */
  orphanHeadings: number;
  /** Tables divided between two chunks. */
  splitTables: number;
  /** Total tokens repeated by overlap. */
  overlapTokens: number;
}

const TERMINAL = /[.!?。！？…:;"'”’)\]]\s*$/;

export function strategyMetrics(result: StrategyResult, blocks: readonly DocumentBlock[], count: TokenCounter): StrategyMetrics {
  const { doc, ranges, texts } = result;
  const byId = new Map(blocks.map((b) => [b.id, b]));
  const tokens = texts.map((t) => count(t));
  const spanAt = (offset: number) => doc.spans.find((s) => offset > s.start && offset < s.end);
  let midSentence = 0;
  let orphan = 0;
  const cutTables = new Set<string>();
  ranges.forEach((r, i) => {
    if (i < ranges.length - 1) {
      const inside = spanAt(r.end);
      if (inside && !TERMINAL.test(doc.text.slice(inside.start, r.end))) midSentence++;
      if (inside && byId.get(inside.blockId)!.type === 'table') cutTables.add(inside.blockId);
      const ending = doc.spans.filter((s) => s.end <= r.end && s.end > r.start);
      const lastBlock = ending.length ? byId.get(ending[ending.length - 1]!.blockId) : undefined;
      if (lastBlock && isHeading(lastBlock) && doc.text.slice(ending[ending.length - 1]!.end, r.end).trim() === '') orphan++;
    }
    const startInside = spanAt(r.start);
    if (i > 0 && startInside && byId.get(startInside.blockId)!.type === 'table') cutTables.add(startInside.blockId);
  });
  let overlap = 0;
  for (let i = 1; i < ranges.length; i++) {
    const o = ranges[i - 1]!.end - ranges[i]!.start;
    if (o > 0) overlap += count(doc.text.slice(ranges[i]!.start, ranges[i - 1]!.end));
  }
  return {
    chunks: ranges.length,
    avgTokens: tokens.length ? Math.round(tokens.reduce((a, b) => a + b, 0) / tokens.length) : 0,
    minTokens: tokens.length ? Math.min(...tokens) : 0,
    maxTokens: tokens.length ? Math.max(...tokens) : 0,
    midSentenceCuts: midSentence,
    orphanHeadings: orphan,
    splitTables: cutTables.size,
    overlapTokens: overlap,
  };
}
