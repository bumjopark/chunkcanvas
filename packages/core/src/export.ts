import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex } from '@noble/hashes/utils.js';
import { chunkableBlocks, generateChunks, keptSegments, normalizeBoundaries, normalizeExcluded, validatePlan } from './plan';
import type { Boundary, Chunk, ChunkSettings, DocumentBlock, ParsedDocument, Segment, SplitPlan, TokenCounter, Tokenizer } from './types';
import { normalizeChunkSettings } from './settings';

import { documentSections, type ExportedSection, type HeadingPathEntry } from './sections';
export type { ExportedSection, HeadingPathEntry } from './sections';

export const EXPORT_SCHEMA_VERSION = 3;
export const SPLIT_PLAN_SCHEMA_VERSION = 2;

export interface ExportOptions {
  /** Name of the tokenizer behind `countTokens`, recorded in every export. */
  tokenizer: string;
  generatedAt?: string;
  /** Required to apply settings.overlapTokens; without it exports have no overlap. */
  encoder?: Tokenizer;
}

export interface ExportedChunk {
  headingPath: HeadingPathEntry[];
  sectionIds: string[];
  id: string;
  index: number;
  text: string;
  tokenCount: number;
  pageStart: number | null;
  pageEnd: number | null;
  /** Tokens at the start of `text` repeated from the end of the previous chunk. */
  overlapTokens: number;
  blockIds: string[];
  /** Character ranges of block.text in this chunk; a block split by a boundary appears in two chunks. */
  segments: Segment[];
  sources: { blockId: string; type: string; start: number; end: number; regions: DocumentBlock['regions']; lineGeometry?: DocumentBlock['lineGeometry'] }[];
}

export interface JsonExport {
  schema: 'chunkcanvas.chunks';
  schemaVersion: number;
  /** All block offsets and segment ranges count UTF-16 code units. */
  offsetEncoding?: 'utf16';
  generatedAt: string;
  tokenizer: string;
  document: ParsedDocument['document'] & { blocksFingerprint: string };
  settings: ChunkSettings;
  boundaries: Boundary[];
  /** Text deleted in the editor, left out of every chunk. */
  excluded: Segment[];
  sections: ExportedSection[];
  chunks: ExportedChunk[];
}

export interface SplitPlanFile {
  schema: 'chunkcanvas.split-plan';
  schemaVersion: number;
  /** All block offsets and segment ranges count UTF-16 code units. */
  offsetEncoding?: 'utf16';
  documentId: string;
  fileHash: string;
  blocksFingerprint: string;
  revision: number;
  boundaries: Boundary[];
  settings: ChunkSettings;
  /** Deleted text (optional: files saved before chunks could be deleted have none). */
  excluded?: Segment[];
  /** Optional content hashes for rematching. Short source text can be guessed from hashes. */
  blockAnchors?: { blockId: string; type: DocumentBlock['type']; textHash: string }[];
}

function overlapTail(text: string, limit: number, enc: Tokenizer): string {
  const ids = enc.encode(text).slice(-limit);
  // A token may start in the middle of a UTF-8 character. Decode only suffixes
  // which are also literal suffixes of the source, never replacement characters.
  for (let start = 0; start < ids.length; start++) {
    const decoded = enc.decode(ids.slice(start));
    if (text.endsWith(decoded) && enc.encode(decoded.trimStart()).length <= limit) return decoded.trimStart();
  }
  return '';
}

const contentKey = (b: { type: string; text: string }) => JSON.stringify([b.type, b.text]);
const textHash = (text: string) => bytesToHex(sha256(new TextEncoder().encode(text)));
const anchorKey = (a: { type: string; textHash: string }) => JSON.stringify([a.type, a.textHash]);

/** A chunk as exported: numbered among the chunks kept, without its deleted parts. */
function exportChunk(chunk: Chunk, index: number, byId: Map<string, DocumentBlock>, structure: ReturnType<typeof documentSections>): ExportedChunk {
  const segments = keptSegments(chunk);
  return {
    ...structure.forSegments(segments),
    id: chunk.id,
    index,
    text: chunk.text,
    tokenCount: chunk.tokenCount,
    pageStart: chunk.pageRange?.[0] ?? null,
    pageEnd: chunk.pageRange?.[1] ?? null,
    overlapTokens: 0,
    blockIds: [...new Set(segments.map((s) => s.blockId))],
    segments,
    sources: segments.map((s) => {
      const b = byId.get(s.blockId)!;
      return { blockId: s.blockId, type: b.type, start: s.start, end: s.end, regions: b.regions,
        ...(b.lineGeometry ? { lineGeometry: b.lineGeometry } : {}) };
    }),
  };
}

/** Throws if the plan is inconsistent, so a broken plan can never be exported silently. */
export function buildJsonExport(
  doc: ParsedDocument,
  plan: SplitPlan,
  countTokens: TokenCounter,
  options: ExportOptions,
): JsonExport {
  const issues = validatePlan(doc.blocks, plan);
  if (issues.length) throw new Error(`Split plan is invalid: ${issues.join('; ')}`);
  const byId = new Map(doc.blocks.map((b) => [b.id, b]));
  const all = generateChunks(doc.blocks, plan, countTokens);
  const chunks = all.filter((c) => !c.deleted);
  const structure = documentSections(doc.blocks);
  const exported = chunks.map((c, i) => exportChunk(c, i, byId, structure));
  const overlap = plan.settings.overlapTokens;
  if (overlap > 0 && options.encoder) {
    const enc = options.encoder;
    for (let i = exported.length - 1; i > 0; i--) {
      const prev = chunks[i - 1]!;
      // Overlap repeats the text right before a chunk: none after a deleted chunk, whose text never shows.
      if (all[chunks[i]!.index - 1]?.deleted) continue;
      const tail = overlapTail(prev.text, overlap, enc);
      if (!tail) continue;
      const kept = keptSegments(prev);
      const last = kept[kept.length - 1]!;
      const lastBlock = byId.get(last.blockId)!;
      const joiner = last.end >= lastBlock.text.length ? '\n\n' : lastBlock.type === 'table' ? '\n' : ' ';
      const c = exported[i]!;
      c.text = `${tail}${joiner}${c.text}`;
      c.tokenCount = countTokens(c.text);
      c.overlapTokens = enc.encode(tail).length;
    }
  }
  return {
    schema: 'chunkcanvas.chunks',
    schemaVersion: EXPORT_SCHEMA_VERSION,
    offsetEncoding: 'utf16',
    generatedAt: options.generatedAt ?? new Date().toISOString(),
    tokenizer: options.tokenizer,
    document: { ...doc.document, blocksFingerprint: doc.blocksFingerprint },
    settings: plan.settings,
    boundaries: plan.boundaries,
    excluded: normalizeExcluded(doc.blocks, plan.excluded),
    sections: structure.sections,
    chunks: exported,
  };
}

/** One chunk per line: `{ id, text, metadata }`, ready for most indexing pipelines. */
export function toJsonl(exp: JsonExport): string {
  return exp.chunks
    .map((c) =>
      JSON.stringify({
        id: `${exp.document.id}#${c.index}`,
        text: c.text,
        metadata: {
          documentId: exp.document.id,
          sourceFile: exp.document.filename,
          fileHash: exp.document.fileHash,
          parser: exp.document.parser,
          chunkId: c.id,
          chunkIndex: c.index,
          pageStart: c.pageStart,
          pageEnd: c.pageEnd,
          tokenCount: c.tokenCount,
          overlapTokens: c.overlapTokens,
          tokenizer: exp.tokenizer,
          blockIds: c.blockIds,
          headingPath: c.headingPath.map((h) => h.text),
          headingBlockIds: c.headingPath.map((h) => h.blockId),
          sectionIds: c.sectionIds,
          segments: c.segments,
          offsetEncoding: exp.offsetEncoding,
        },
      }),
    )
    .join('\n')
    .concat(exp.chunks.length ? '\n' : '');
}

function pagesLabel(c: ExportedChunk): string {
  if (c.pageStart === null) return 'location unknown';
  return c.pageStart === c.pageEnd ? `page ${c.pageStart}` : `pages ${c.pageStart}–${c.pageEnd}`;
}

export function toMarkdown(exp: JsonExport): string {
  const head = [
    `# ${exp.document.filename}`,
    '',
    `${exp.chunks.length} chunks · tokenizer ${exp.tokenizer} · target ${exp.settings.targetTokens} / max ${exp.settings.maxTokens} tokens`,
  ];
  const body = exp.chunks.map((c) =>
    [`## Chunk ${c.index + 1}`, '', `<!-- ${c.id} · ${pagesLabel(c)} · ${c.tokenCount} tokens -->`, '', c.text].join('\n'),
  );
  return [...head, '', body.join('\n\n---\n\n'), ''].join('\n');
}

export function toSplitPlanFile(doc: ParsedDocument, plan: SplitPlan, options: { includeAnchors?: boolean } = {}): SplitPlanFile {
  const counts = new Map<string, number>();
  for (const b of doc.blocks) counts.set(contentKey(b), (counts.get(contentKey(b)) ?? 0) + 1);
  const cutIds = new Set(plan.boundaries.map((b) => b.blockId));
  return {
    schema: 'chunkcanvas.split-plan',
    schemaVersion: SPLIT_PLAN_SCHEMA_VERSION,
    offsetEncoding: 'utf16',
    documentId: doc.document.id,
    fileHash: doc.document.fileHash,
    blocksFingerprint: doc.blocksFingerprint,
    revision: plan.revision,
    boundaries: plan.boundaries,
    settings: plan.settings,
    excluded: normalizeExcluded(doc.blocks, plan.excluded),
    ...(options.includeAnchors === false ? {} : { blockAnchors: doc.blocks.filter((b) => cutIds.has(b.id) && counts.get(contentKey(b)) === 1)
      .map(({ id: blockId, type, text }) => ({ blockId, type, textHash: textHash(text) })) }),
  };
}

export interface ImportResult {
  plan: SplitPlan;
  warnings: string[];
}

function isBoundary(v: unknown): v is Boundary {
  if (!v || typeof v !== 'object') return false;
  const b = v as Record<string, unknown>;
  return typeof b.blockId === 'string' && (b.offset === undefined || (typeof b.offset === 'number' && Number.isInteger(b.offset)));
}

function isSegment(v: unknown): v is Segment {
  if (!v || typeof v !== 'object') return false;
  const s = v as Record<string, unknown>;
  return typeof s.blockId === 'string' && Number.isInteger(s.start) && Number.isInteger(s.end);
}

/**
 * Re-apply an exported split plan to a parse. The file must come from the same PDF; if the
 * parse differs, boundaries are matched only by unique, identical block type and text.
 * Version 1 files (block boundaries only) are accepted too.
 */
export function importSplitPlan(doc: ParsedDocument, current: SplitPlan, raw: unknown): ImportResult {
  if (!raw || typeof raw !== 'object') throw new Error('Not a split plan file.');
  const file = raw as Partial<SplitPlanFile> & { boundaryAfterBlockIds?: unknown };
  if (file.offsetEncoding !== undefined && file.offsetEncoding !== 'utf16') throw new Error('Unsupported offset encoding.');
  if (file.schema !== 'chunkcanvas.split-plan') throw new Error('Not a ChunkCanvas split plan file.');
  let wanted: Boundary[];
  if (file.schemaVersion === 1) {
    const ids = file.boundaryAfterBlockIds;
    if (!Array.isArray(ids) || !ids.every((x) => typeof x === 'string')) throw new Error('Split plan has no boundary list.');
    wanted = ids.map((blockId) => ({ blockId }));
  } else if (file.schemaVersion === SPLIT_PLAN_SCHEMA_VERSION) {
    if (!Array.isArray(file.boundaries) || !file.boundaries.every(isBoundary)) throw new Error('Split plan has no boundary list.');
    wanted = file.boundaries.map((b) => (b.offset === undefined ? { blockId: b.blockId } : { blockId: b.blockId, offset: b.offset }));
  } else {
    throw new Error(`Unsupported split plan version ${file.schemaVersion}.`);
  }
  if (file.fileHash !== doc.document.fileHash) throw new Error('This split plan was made for a different PDF.');

  const warnings: string[] = [];
  const importedSettings = normalizeChunkSettings(file.settings);
  const settings = importedSettings ?? normalizeChunkSettings(current.settings)!;
  if (!importedSettings) warnings.push('Settings were missing or invalid; kept the current settings.');
  else if (Object.keys(file.settings!).some((k) => !Object.hasOwn(importedSettings, k))) warnings.push('Unknown settings were ignored.');

  const seq = chunkableBlocks(doc.blocks, settings);
  const sameParse = file.blocksFingerprint === doc.blocksFingerprint;
  const originalCount = wanted.length;
  if (!sameParse) {
    const targets = new Map<string, DocumentBlock[]>();
    for (const b of seq) {
      const key = anchorKey({ type: b.type, textHash: textHash(b.text) });
      targets.set(key, [...(targets.get(key) ?? []), b]);
    }
    const matches = new Map<string, string>();
    const anchors = Array.isArray(file.blockAnchors) ? file.blockAnchors : [];
    const seen = new Set<string>();
    for (const a of anchors) {
      if (!a || typeof a.blockId !== 'string' || typeof a.type !== 'string' || typeof a.textHash !== 'string'
        || !/^[a-f0-9]{64}$/.test(a.textHash)) continue;
      if (seen.has(a.blockId)) {
        matches.delete(a.blockId);
        continue;
      }
      seen.add(a.blockId);
      const found = targets.get(anchorKey(a));
      if (found?.length === 1) matches.set(a.blockId, found[0]!.id);
    }
    wanted = wanted.flatMap((b) => matches.has(b.blockId) ? [{ ...b, blockId: matches.get(b.blockId)! }] : []);
    warnings.push('This is a different parse. Only boundaries with unique, identical block content were matched; imported deletions were not applied.');
  }
  let boundaries = preservedBoundaries(seq, wanted);
  if (boundaries.length === wanted.length && JSON.stringify(boundaries) !== JSON.stringify(wanted)) warnings.push('Boundaries were put in reading order or canonicalized at the same text position.');
  const dropped = originalCount - boundaries.length;
  if (dropped > 0) warnings.push(`${dropped} boundar${dropped === 1 ? 'y' : 'ies'} could not be kept at their original positions and were dropped.`);
  if (!sameParse && originalCount > 0 && !boundaries.length) {
    boundaries = preservedBoundaries(seq, current.boundaries);
    warnings.push('No boundaries could be matched; kept the current boundaries.');
  }
  const excluded = sameParse
    ? (Array.isArray(file.excluded) ? normalizeExcluded(doc.blocks, file.excluded.filter(isSegment)) : [])
    : normalizeExcluded(doc.blocks, current.excluded);

  return { plan: { ...current, settings, boundaries, excluded }, warnings };
}

/**
 * A saved plan in the current shape, upgrading plans from older versions
 * ({boundaryAfterBlockIds}); null for anything without a boundary list.
 */
export function upgradeSplitPlan(raw: unknown): SplitPlan | null {
  if (!raw || typeof raw !== 'object') return null;
  const plan = raw as SplitPlan & { boundaryAfterBlockIds?: unknown };
  if (Array.isArray(plan.boundaries) && plan.boundaries.every(isBoundary)) return plan;
  const ids = plan.boundaryAfterBlockIds;
  if (Array.isArray(ids) && ids.every((x) => typeof x === 'string')) {
    const { boundaryAfterBlockIds: _old, ...rest } = plan;
    return { ...rest, boundaries: ids.map((blockId) => ({ blockId })) };
  }
  return null;
}

/** Import/load must not snap an interior cut to a different row or block edge. */
function preservedBoundaries(seq: readonly DocumentBlock[], wanted: readonly Boundary[]): Boundary[] {
  const byId = new Map(seq.map((b) => [b.id, b]));
  return normalizeBoundaries(seq, wanted.filter((b) => {
    const block = byId.get(b.blockId);
    if (!block) return false;
    if (b.offset === undefined) return true;
    if (b.offset < 0 || b.offset > block.text.length) return false;
    if (b.offset === 0 || b.offset === block.text.length) return true;
    const [canonical] = normalizeBoundaries(seq, [b]);
    return canonical?.blockId === b.blockId && canonical.offset === b.offset;
  }));
}

/** Core owns editing order. API validates references/ranges without duplicating that order. */
export function normalizeSavedPlan(doc: ParsedDocument, raw: unknown): (ImportResult & { changed: boolean }) | null {
  const saved = upgradeSplitPlan(raw);
  if (!saved || saved.documentId !== doc.document.id || saved.blocksFingerprint !== doc.blocksFingerprint
      || !Number.isInteger(saved.revision) || saved.revision < 0) return null;
  const settings = normalizeChunkSettings(saved.settings);
  if (!settings) return null;
  const boundaries = preservedBoundaries(chunkableBlocks(doc.blocks, settings), saved.boundaries);
  const excluded = normalizeExcluded(doc.blocks, Array.isArray(saved.excluded) ? saved.excluded.filter(isSegment) : []);
  const plan: SplitPlan = { documentId: saved.documentId, blocksFingerprint: saved.blocksFingerprint,
    revision: saved.revision, settings, boundaries, excluded };
  const warnings: string[] = [];
  if (JSON.stringify(boundaries) !== JSON.stringify(saved.boundaries)) warnings.push('Saved boundaries were reordered or removed; no interior boundary was moved.');
  if (JSON.stringify(excluded) !== JSON.stringify(saved.excluded ?? [])) warnings.push('Invalid saved deletions were removed.');
  if (Object.keys(saved.settings).some((k) => !Object.hasOwn(settings, k))) warnings.push('Unknown settings were ignored.');
  return { plan, warnings, changed: warnings.length > 0 };
}
