import {
  canRedo,
  canUndo,
  chunkableBlocks,
  createHistory,
  generateChunks,
  pushHistory,
  rangeSegments,
  redo,
  undo,
  type Chunk,
  type DocumentBlock,
  type History,
  type ParsedDocument,
  type Segment,
  type SplitPlan,
  type StrategyId,
  type StrategyParams,
  type StrategyResult,
  type TokenCounter,
  type Tokenizer,
} from '@chunkcanvas/core';
import { create } from 'zustand';

export type SaveStatus = 'saved' | 'dirty' | 'saving' | 'error' | 'conflict' | 'expired';

export interface EditorDoc {
  id: string;
  token: string;
  filename: string;
  parser: string;
  expiresAt: string;
  parsed: ParsedDocument;
}

/** A chunk of a splitter preview: read-only, and it may overlap its neighbours. */
export interface PreviewChunk {
  index: number;
  text: string;
  tokenCount: number;
  segments: Segment[];
  pageRange: [number, number] | null;
}

export interface StrategyPreview {
  id: StrategyId;
  params: StrategyParams;
  result: StrategyResult;
  chunks: PreviewChunk[];
}

interface EditorState {
  doc: EditorDoc | null;
  count: TokenCounter;
  tokenizer: Tokenizer | null;
  /** Read-only splitter preview shown instead of the editable plan. */
  strategy: StrategyPreview | null;
  previewSelected: number | null;
  history: History<SplitPlan> | null;
  /** Plan shown while a boundary is being dragged; null otherwise. */
  preview: SplitPlan | null;
  /** A position in the document; the selected chunk is the one containing it. */
  selected: { blockId: string; offset: number } | null;
  hoverChunk: number | null;
  zoom: number;
  fitWidth: boolean;
  showBlocks: boolean;
  showChunks: boolean;
  serverRevision: number;
  saveStatus: SaveStatus;
  conflict: { revision: number; plan: SplitPlan | null } | null;
  scrollRequest: { blockId: string; offset: number; nonce: number } | null;
  notice: { text: string; nonce: number } | null;

  init: (doc: EditorDoc, plan: SplitPlan, serverRevision: number, dirty: boolean, tokenizer: Tokenizer) => void;
  setTokenizer: (tokenizer: Tokenizer) => void;
  showStrategy: (id: StrategyId, params: StrategyParams, result: StrategyResult) => void;
  closeStrategy: () => void;
  selectPreview: (index: number | null, scroll?: boolean) => void;
  reset: () => void;
  apply: (edit: (plan: SplitPlan) => SplitPlan) => void;
  setPreview: (plan: SplitPlan | null) => void;
  undo: () => void;
  redo: () => void;
  select: (blockId: string | null, scroll?: boolean, offset?: number) => void;
  setHoverChunk: (index: number | null) => void;
  setZoom: (zoom: number, fit?: boolean) => void;
  toggle: (key: 'showBlocks' | 'showChunks') => void;
  setSave: (patch: Partial<Pick<EditorState, 'serverRevision' | 'saveStatus' | 'conflict'>>) => void;
  scrollTo: (blockId: string, offset?: number) => void;
  notify: (text: string) => void;
}

function readPref<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(`chunkcanvas.${key}`);
    return v === null ? fallback : (JSON.parse(v) as T);
  } catch {
    return fallback;
  }
}

export function writePref(key: string, value: unknown): void {
  try {
    localStorage.setItem(`chunkcanvas.${key}`, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}

let nonce = 0;

/** After an edit the plan needs saving, unless saving is impossible (expired) or paused (conflict). */
function dirtyUnlessBlocked(status: SaveStatus): SaveStatus {
  return status === 'expired' || status === 'conflict' ? status : 'dirty';
}

export const useEditor = create<EditorState>((set, get) => ({
  doc: null,
  count: (t) => t.length,
  tokenizer: null,
  strategy: null,
  previewSelected: null,
  history: null,
  preview: null,
  selected: null,
  hoverChunk: null,
  zoom: 1,
  fitWidth: true,
  showBlocks: readPref('showBlocks', false),
  showChunks: true,
  serverRevision: 0,
  saveStatus: 'saved',
  conflict: null,
  scrollRequest: null,
  notice: null,

  init: (doc, plan, serverRevision, dirty, tokenizer) =>
    set({
      doc,
      count: tokenizer.count,
      tokenizer,
      strategy: null,
      previewSelected: null,
      history: createHistory(plan),
      preview: null,
      selected: null,
      hoverChunk: null,
      serverRevision,
      saveStatus: dirty ? 'dirty' : 'saved',
      conflict: null,
    }),
  reset: () => set({ doc: null, history: null, preview: null, selected: null, conflict: null, strategy: null }),
  setTokenizer: (tokenizer) => {
    writePref('tokenizer', tokenizer.name);
    set({ tokenizer, count: tokenizer.count, strategy: null, previewSelected: null });
  },
  showStrategy: (id, params, result) => {
    const { doc, count } = get();
    if (!doc) return;
    const blocks = doc.parsed.blocks;
    const byId = new Map(blocks.map((b) => [b.id, b]));
    const chunks = result.ranges.map((range, index) => {
      const segments = rangeSegments(blocks, result.doc, range);
      const pages = segments.flatMap((s) => byId.get(s.blockId)!.regions.map((r) => r.pageIndex));
      const text = result.texts[index] ?? '';
      return {
        index,
        text,
        tokenCount: count(text),
        segments,
        pageRange: pages.length ? ([Math.min(...pages) + 1, Math.max(...pages) + 1] as [number, number]) : null,
      };
    });
    set({ strategy: { id, params, result, chunks }, previewSelected: null, preview: null });
  },
  closeStrategy: () => set({ strategy: null, previewSelected: null }),
  selectPreview: (index, scroll = false) => {
    set({ previewSelected: index });
    const seg = index === null ? undefined : get().strategy?.chunks[index]?.segments[0];
    if (scroll && seg) get().scrollTo(seg.blockId, seg.start);
  },
  apply: (edit) => {
    const { history } = get();
    if (!history) return;
    const next = pushHistory(history, edit(history.present));
    if (next !== history) set({ history: next, preview: null, saveStatus: dirtyUnlessBlocked(get().saveStatus) });
    else set({ preview: null });
  },
  setPreview: (preview) => set({ preview }),
  undo: () => {
    const { history } = get();
    if (history && canUndo(history)) set({ history: undo(history), saveStatus: dirtyUnlessBlocked(get().saveStatus), preview: null });
  },
  redo: () => {
    const { history } = get();
    if (history && canRedo(history)) set({ history: redo(history), saveStatus: dirtyUnlessBlocked(get().saveStatus), preview: null });
  },
  select: (blockId, scroll = false, offset = 0) => {
    set({ selected: blockId ? { blockId, offset } : null });
    if (scroll && blockId) get().scrollTo(blockId, offset);
  },
  setHoverChunk: (hoverChunk) => set({ hoverChunk }),
  setZoom: (zoom, fit = false) => set({ zoom: Math.min(4, Math.max(0.25, zoom)), fitWidth: fit }),
  toggle: (key) =>
    set((s) => {
      writePref(key, !s[key]);
      return { [key]: !s[key] } as Partial<EditorState>;
    }),
  setSave: (patch) => set(patch),
  scrollTo: (blockId, offset = 0) => set({ scrollRequest: { blockId, offset, nonce: ++nonce } }),
  notify: (text) => set({ notice: { text, nonce: ++nonce } }),
}));

/* Derived data, cached per plan object so every component shares one computation. */

export interface Piece {
  start: number;
  end: number;
  chunk: number;
  /** Deleted text: drawn hatched, left out of the chunk. */
  deleted: boolean;
}

export interface ChunkView {
  chunks: Chunk[];
  /** Each chunk's number as exported (1-based, counting only the chunks kept); null when deleted. */
  labels: (number | null)[];
  kept: number;
  /** For each block, the character ranges that belong to each chunk. */
  pieces: Map<string, Piece[]>;
  seq: DocumentBlock[];
  blockById: Map<string, DocumentBlock>;
  /** Index of the chunk containing a position, if the block is chunked. */
  chunkAt: (blockId: string, offset?: number) => number | undefined;
}

// Cached per counter and plan: changing the tokenizer must recount every chunk.
const viewsByCounter = new WeakMap<TokenCounter, WeakMap<SplitPlan, ChunkView>>();

export function chunkView(parsed: ParsedDocument, plan: SplitPlan, count: TokenCounter): ChunkView {
  let views = viewsByCounter.get(count);
  if (!views) {
    views = new WeakMap();
    viewsByCounter.set(count, views);
  }
  let view = views.get(plan);
  if (!view) {
    const chunks = generateChunks(parsed.blocks, plan, count);
    const pieces = new Map<string, Piece[]>();
    const add = (blockId: string, piece: Piece) => pieces.set(blockId, [...(pieces.get(blockId) ?? []), piece]);
    for (const c of chunks) {
      for (const s of c.segments) {
        // Deleted parts of the segment, in order, and the kept text between them.
        const cut = c.excluded.filter((x) => x.blockId === s.blockId && x.start >= s.start && x.end <= s.end);
        let at = s.start;
        for (const x of cut) {
          if (x.start > at) add(s.blockId, { start: at, end: x.start, chunk: c.index, deleted: false });
          add(s.blockId, { start: x.start, end: x.end, chunk: c.index, deleted: true });
          at = x.end;
        }
        if (at < s.end || !cut.length) add(s.blockId, { start: at, end: s.end, chunk: c.index, deleted: false });
      }
    }
    let kept = 0;
    const labels = chunks.map((c) => (c.deleted ? null : ++kept));
    view = {
      chunks,
      labels,
      kept,
      pieces,
      seq: chunkableBlocks(parsed.blocks, plan.settings),
      blockById: new Map(parsed.blocks.map((b) => [b.id, b])),
      chunkAt: (blockId, offset = 0) => {
        const list = pieces.get(blockId);
        return (list?.find((p) => offset >= p.start && offset < p.end) ?? list?.[0])?.chunk;
      },
    };
    views.set(plan, view);
  }
  return view;
}

/** The plan to display: the drag preview if any, else the current history entry. */
export function useDisplayPlan(): SplitPlan | null {
  return useEditor((s) => s.preview ?? s.history?.present ?? null);
}

export function useChunkView(): ChunkView | null {
  const plan = useDisplayPlan();
  const parsed = useEditor((s) => s.doc?.parsed);
  const count = useEditor((s) => s.count);
  return plan && parsed ? chunkView(parsed, plan, count) : null;
}

export function useSelectedChunk(): number | null {
  const view = useChunkView();
  const selected = useEditor((s) => s.selected);
  if (!view || !selected) return null;
  return view.chunkAt(selected.blockId, selected.offset) ?? null;
}
