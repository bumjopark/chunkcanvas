import {
  addBoundary,
  boundaryMoveRange,
  canSplitInside,
  moveBoundary,
  rangeSegments,
  removeBoundary,
  splitPoints,
  type Boundary,
  type DocumentBlock,
} from '@chunkcanvas/core';
import { GripVertical, Plus, Scissors, X } from 'lucide-react';
import type { PDFDocumentProxy, RenderTask } from 'pdfjs-dist';
import { createContext, memo, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { chunkFill, chunkSolid, chunkStroke } from '../lib/colors';
import { caretAt, computeGaps, rangeRects, type Gap, type PageRect } from '../lib/geometry';
import { useT } from '../lib/i18n';
import type { PdfHandle } from '../lib/pdf';
import { useChunkView, useEditor, useSelectedChunk, type ChunkView } from '../store';
import { cx } from './ui';

const PAGE_GAP = 16;
const PADDING = 24;

export const boundaryKey = (b: Boundary): string => (b.offset === undefined ? b.blockId : `${b.blockId}@${b.offset}`);

/** A place a boundary can go, with where it is drawn. Inner positions have x0 === x1. */
interface Candidate {
  boundary: Boundary;
  pageIndex: number;
  x0: number;
  x1: number;
  y: number;
}

function gapCandidates(gaps: Gap[]): Candidate[] {
  return gaps.map((g) => ({ boundary: { blockId: g.afterBlockId }, pageIndex: g.pageIndex, x0: g.x0, x1: g.x1, y: g.y }));
}

/** Sentence starts (row starts in tables) inside splittable blocks. */
function innerCandidates(seq: readonly DocumentBlock[]): Candidate[] {
  const out: Candidate[] = [];
  for (const b of seq) {
    if (!canSplitInside(b)) continue;
    for (const offset of splitPoints(b)) {
      const c = caretAt(b, offset);
      if (c) out.push({ boundary: { blockId: b.id, offset }, pageIndex: c.pageIndex, x0: c.x0, x1: c.x0, y: (c.y0 + c.y1) / 2 });
    }
  }
  return out;
}

interface DragApi {
  start: (from: Boundary, e: React.PointerEvent) => void;
  dragging: string | null;
}

const DragContext = createContext<DragApi>({ start: () => {}, dragging: null });

export function PdfView({ handle }: { handle: PdfHandle }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const pageRefs = useRef<(HTMLDivElement | null)[]>([]);
  const zoom = useEditor((s) => s.zoom);
  const fitWidth = useEditor((s) => s.fitWidth);
  const setZoom = useEditor((s) => s.setZoom);
  const scrollRequest = useEditor((s) => s.scrollRequest);
  const view = useChunkView();
  const [dragging, setDragging] = useState<string | null>(null);
  const maxWidth = useMemo(() => Math.max(...handle.sizes.map((s) => s.width)), [handle.sizes]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el || !fitWidth) return;
    const fit = () => setZoom((el.clientWidth - PADDING * 2) / maxWidth, true);
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [fitWidth, maxWidth, setZoom]);

  useEffect(() => {
    if (!scrollRequest || !view) return;
    const block = view.blockById.get(scrollRequest.blockId);
    if (!block) return;
    const caret = scrollRequest.offset > 0 ? caretAt(block, scrollRequest.offset) : null;
    const target = caret ?? (block.regions[0] ? { pageIndex: block.regions[0].pageIndex, y0: block.regions[0].bbox.y0 } : null);
    const page = target && pageRefs.current[target.pageIndex];
    const container = containerRef.current;
    if (!target || !page || !container) return;
    const top = page.offsetTop + target.y0 * page.offsetHeight - 96;
    container.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
  }, [scrollRequest]);

  const start = useCallback((from: Boundary, e: React.PointerEvent) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const state = useEditor.getState();
    const plan = state.history?.present;
    const blocks = state.doc?.parsed.blocks;
    if (!plan || !blocks) return;
    const range = boundaryMoveRange(plan, blocks, from);
    if (!range) return;
    const candidates = [...gapCandidates(computeGaps(range.seq)), ...innerCandidates(range.seq)].filter((c) =>
      range.allows(c.boundary),
    );
    let current = from;
    setDragging(boundaryKey(from));
    document.body.style.cursor = 'grabbing';

    const nearest = (x: number, y: number): Candidate | undefined => {
      let best: Candidate | undefined;
      let bestDist = Infinity;
      for (const c of candidates) {
        const page = pageRefs.current[c.pageIndex];
        if (!page) continue;
        const r = page.getBoundingClientRect();
        const cy = r.top + c.y * r.height;
        const cx0 = r.left + c.x0 * r.width;
        const cx1 = r.left + c.x1 * r.width;
        const dx = x < cx0 ? cx0 - x : x > cx1 ? x - cx1 : 0;
        const d = Math.abs(y - cy) + dx * 0.6;
        if (d < bestDist) {
          bestDist = d;
          best = c;
        }
      }
      return best;
    };

    const onMove = (ev: PointerEvent) => {
      const container = containerRef.current;
      if (container) {
        const r = container.getBoundingClientRect();
        if (ev.clientY < r.top + 48) container.scrollBy(0, -14);
        else if (ev.clientY > r.bottom - 48) container.scrollBy(0, 14);
      }
      const best = nearest(ev.clientX, ev.clientY);
      if (!best || boundaryKey(best.boundary) === boundaryKey(current)) return;
      current = best.boundary;
      setDragging(boundaryKey(current));
      useEditor
        .getState()
        .setPreview(boundaryKey(current) === boundaryKey(from) ? null : moveBoundary(plan, blocks, from, current));
    };
    const finish = (commit: boolean) => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onCancel);
      window.removeEventListener('keydown', onKey);
      document.body.style.cursor = '';
      setDragging(null);
      const st = useEditor.getState();
      if (commit && boundaryKey(current) !== boundaryKey(from)) {
        const target = current;
        st.apply((p) => moveBoundary(p, blocks, from, target));
      } else {
        st.setPreview(null);
      }
    };
    const onUp = () => finish(true);
    const onCancel = () => finish(false);
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === 'Escape') finish(false);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onCancel);
    window.addEventListener('keydown', onKey);
  }, []);

  const drag = useMemo(() => ({ start, dragging }), [start, dragging]);
  const gaps = useMemo(() => (view ? computeGaps(view.seq) : []), [view]);
  const gapsByPage = useMemo(() => {
    const m = new Map<number, Gap[]>();
    for (const g of gaps) m.set(g.pageIndex, [...(m.get(g.pageIndex) ?? []), g]);
    return m;
  }, [gaps]);

  return (
    <DragContext.Provider value={drag}>
      <div
        ref={containerRef}
        className="scroll-thin relative h-full overflow-auto bg-zinc-200/70 dark:bg-zinc-900"
        data-testid="pdf-scroll"
      >
        <div className="mx-auto flex w-max flex-col items-center" style={{ padding: PADDING, gap: PAGE_GAP }}>
          {handle.sizes.map((size, i) => (
            <PageView
              key={i}
              rootRef={containerRef}
              index={i}
              pdf={handle.pdf}
              width={size.width * zoom}
              height={size.height * zoom}
              zoom={zoom}
              gaps={gapsByPage.get(i) ?? []}
              setRef={(el) => {
                pageRefs.current[i] = el;
              }}
            />
          ))}
        </div>
      </div>
    </DragContext.Provider>
  );
}

const PageView = memo(function PageView({
  rootRef,
  index,
  pdf,
  width,
  height,
  zoom,
  gaps,
  setRef,
}: {
  rootRef: React.RefObject<HTMLDivElement | null>;
  index: number;
  pdf: PDFDocumentProxy;
  width: number;
  height: number;
  zoom: number;
  gaps: Gap[];
  setRef: (el: HTMLDivElement | null) => void;
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const io = new IntersectionObserver(([entry]) => setVisible(!!entry?.isIntersecting), { root: rootRef.current, rootMargin: '1200px 0px' });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  useEffect(() => {
    if (!visible) {
      const canvas = canvasRef.current;
      if (canvas) canvas.width = canvas.height = 0;
      return;
    }
    let task: RenderTask | null = null;
    let cancelled = false;
    const timer = setTimeout(async () => {
      let page;
      try {
        page = await pdf.getPage(index + 1);
      } catch {
        return; // The document can be destroyed while this page is loading.
      }
      const canvas = canvasRef.current;
      if (cancelled || !canvas) return;
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const viewport = page.getViewport({ scale: zoom * dpr });
      const off = document.createElement('canvas');
      off.width = Math.floor(viewport.width);
      off.height = Math.floor(viewport.height);
      task = page.render({ canvas: off, viewport });
      try {
        await task.promise;
      } catch {
        return;
      }
      if (cancelled) return;
      // Swap in the finished bitmap so zooming never flashes a blank page.
      canvas.width = off.width;
      canvas.height = off.height;
      canvas.getContext('2d')?.drawImage(off, 0, 0);
      off.width = off.height = 0;
    }, 80);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      task?.cancel();
    };
  }, [visible, zoom, pdf, index]);

  return (
    <div className="flex flex-col items-center gap-1">
      <div
        ref={(el) => {
          wrapRef.current = el;
          setRef(el);
        }}
        data-page={index}
        className="relative bg-white shadow-sm ring-1 ring-zinc-900/10 select-none"
        style={{ width, height }}
      >
        <canvas ref={canvasRef} className="absolute inset-0" style={{ width, height }} />
        <PageOverlay index={index} width={width} height={height} gaps={gaps} />
      </div>
      <div className="text-[11px] text-zinc-500 tabular-nums">{index + 1}</div>
    </div>
  );
});

interface Box {
  key: string;
  blockId: string;
  type: string;
  chunk: number;
  start: number;
  rect: PageRect;
  /** Whole-block boxes get a little padding; per-line boxes hug the text. */
  whole: boolean;
  /** Deleted text: hatched, still clickable so the chunk can be restored. */
  deleted: boolean;
}

interface Overlay {
  boxes: Box[];
  excluded: { key: string; type: string; rect: PageRect }[];
  /** Inline badges mark a chunk that starts mid-block, right at its first character. */
  badges: { chunk: number; x: number; y: number; inline: boolean }[];
  /** Block ids followed by a boundary, with the chunk the boundary ends. */
  afterBoundaries: Map<string, number>;
  inner: { boundary: Boundary; chunk: number; caret: PageRect; gripX: number }[];
  ticks: { boundary: Boundary; caret: PageRect }[];
}

function buildOverlay(view: ChunkView, blocks: readonly DocumentBlock[], index: number, selected: number | null): Overlay {
  const out: Overlay = { boxes: [], excluded: [], badges: [], afterBoundaries: new Map(), inner: [], ticks: [] };
  const regionOnPage = (b: DocumentBlock) => b.regions.find((r) => r.pageIndex === index);

  for (const b of blocks) {
    const pieces = view.pieces.get(b.id);
    if (!pieces) {
      b.regions.forEach((r, k) => r.pageIndex === index && out.excluded.push({ key: `${b.id}:${k}`, type: b.type, rect: { pageIndex: index, ...r.bbox } }));
      continue;
    }
    const whole = pieces.length === 1 && pieces[0]!.start === 0 && pieces[0]!.end >= b.text.length;
    if (whole || !b.lines?.length) {
      b.regions.forEach((r, k) => {
        if (r.pageIndex === index) {
          out.boxes.push({
            key: `${b.id}:${k}`,
            blockId: b.id,
            type: b.type,
            chunk: pieces[0]!.chunk,
            start: 0,
            rect: { pageIndex: index, ...r.bbox },
            whole: true,
            deleted: pieces.every((p) => p.deleted),
          });
        }
      });
      continue;
    }
    for (const p of pieces) {
      rangeRects(b, p.start, p.end).forEach((rect, k) => {
        if (rect.pageIndex === index) {
          out.boxes.push({ key: `${b.id}@${p.start}:${k}`, blockId: b.id, type: b.type, chunk: p.chunk, start: p.start, rect, whole: false, deleted: p.deleted });
        }
      });
    }
  }

  view.chunks.forEach((c, i) => {
    const first = c.segments.find((s) => view.blockById.get(s.blockId)!.regions.length) ?? c.segments[0]!;
    const fb = view.blockById.get(first.blockId)!;
    const anchor = first.start > 0 ? caretAt(fb, first.start) : fb.regions[0] ? { pageIndex: fb.regions[0].pageIndex, ...fb.regions[0].bbox } : null;
    if (anchor && anchor.pageIndex === index) out.badges.push({ chunk: i, x: anchor.x0, y: anchor.y0, inline: first.start > 0 });
    if (i === view.chunks.length - 1) return;
    const last = c.segments[c.segments.length - 1]!;
    const lb = view.blockById.get(last.blockId)!;
    if (last.end >= lb.text.length) {
      out.afterBoundaries.set(last.blockId, i);
    } else {
      const caret = caretAt(lb, last.end);
      if (caret && caret.pageIndex === index) {
        out.inner.push({ boundary: { blockId: lb.id, offset: last.end }, chunk: i, caret, gripX: regionOnPage(lb)?.bbox.x0 ?? caret.x0 });
      }
    }
  });

  if (selected !== null) {
    for (const s of view.chunks[selected]?.segments ?? []) {
      const b = view.blockById.get(s.blockId)!;
      if (!canSplitInside(b)) continue;
      for (const offset of splitPoints(b)) {
        if (offset <= s.start || offset >= s.end) continue;
        const caret = caretAt(b, offset);
        if (caret && caret.pageIndex === index) out.ticks.push({ boundary: { blockId: b.id, offset }, caret });
      }
    }
  }
  return out;
}

type OverlayProps = { index: number; width: number; height: number; gaps: Gap[] };

function PageOverlay(props: OverlayProps) {
  const previewing = useEditor((s) => s.strategy !== null);
  return previewing ? <PreviewOverlay {...props} /> : <EditOverlay {...props} />;
}

/** Rectangles for part of a block on one page: the whole region, or per line when partial. */
function partRects(b: DocumentBlock, start: number, end: number, index: number): PageRect[] {
  const whole = start <= 0 && end >= b.text.length;
  if (whole || !b.lines?.length) return b.regions.filter((r) => r.pageIndex === index).map((r) => ({ pageIndex: index, ...r.bbox }));
  return rangeRects(b, start, end).filter((r) => r.pageIndex === index);
}

/** A splitter preview: read-only chunk areas, with text shared by two chunks hatched. */
function PreviewOverlay({ index, width, height }: OverlayProps) {
  const strategy = useEditor((s) => s.strategy)!;
  const doc = useEditor((s) => s.doc)!;
  const selected = useEditor((s) => s.previewSelected);
  const hover = useEditor((s) => s.hoverChunk);
  const selectPreview = useEditor((s) => s.selectPreview);
  const setHover = useEditor((s) => s.setHoverChunk);
  const showChunks = useEditor((s) => s.showChunks);

  const { boxes, overlaps, badges } = useMemo(() => {
    const byId = new Map(doc.parsed.blocks.map((b) => [b.id, b]));
    const boxes: { key: string; chunk: number; rect: PageRect }[] = [];
    const overlaps: { key: string; rect: PageRect }[] = [];
    const badges: { chunk: number; x: number; y: number; inline: boolean }[] = [];
    const { ranges, doc: text } = strategy.result;
    for (const c of strategy.chunks) {
      c.segments.forEach((seg, k) => {
        partRects(byId.get(seg.blockId)!, seg.start, seg.end, index).forEach((rect, j) => boxes.push({ key: `${c.index}:${k}:${j}`, chunk: c.index, rect }));
      });
      const first = c.segments[0];
      const fb = first && byId.get(first.blockId);
      if (fb) {
        const anchor = first.start > 0 ? caretAt(fb, first.start) : fb.regions[0] ? { pageIndex: fb.regions[0].pageIndex, ...fb.regions[0].bbox } : null;
        if (anchor && anchor.pageIndex === index) badges.push({ chunk: c.index, x: anchor.x0, y: anchor.y0, inline: first.start > 0 });
      }
      const prev = ranges[c.index - 1];
      const range = ranges[c.index]!;
      if (prev && prev.end > range.start) {
        rangeSegments(doc.parsed.blocks, text, { start: range.start, end: prev.end }).forEach((seg, k) =>
          partRects(byId.get(seg.blockId)!, seg.start, seg.end, index).forEach((rect, j) => overlaps.push({ key: `o${c.index}:${k}:${j}`, rect })),
        );
      }
    }
    return { boxes, overlaps, badges };
  }, [strategy, doc, index]);

  if (!showChunks) return null;
  const X = (v: number) => v * width;
  const Y = (v: number) => v * height;
  return (
    <div className="absolute inset-0" onPointerLeave={() => setHover(null)} data-preview-overlay>
      {boxes.map(({ key, chunk, rect }) => {
        const state = selected === chunk ? 'selected' : hover === chunk ? 'hover' : 'idle';
        return (
          <div
            key={key}
            data-preview-chunk={chunk}
            onPointerEnter={() => setHover(chunk)}
            onClick={() => selectPreview(chunk)}
            className="absolute cursor-pointer rounded-[2px]"
            style={{
              left: X(rect.x0) - 1,
              top: Y(rect.y0) - 1,
              width: X(rect.x1 - rect.x0) + 2,
              height: Y(rect.y1 - rect.y0) + 2,
              background: chunkFill(chunk, state),
            }}
          />
        );
      })}
      {overlaps.map(({ key, rect }) => (
        <div
          key={key}
          data-overlap
          title="overlap"
          className="pointer-events-none absolute rounded-[2px] border-2 border-dashed border-amber-500 bg-[repeating-linear-gradient(135deg,transparent_0_4px,rgba(245,158,11,0.18)_4px_8px)]"
          style={{ left: X(rect.x0) - 2, top: Y(rect.y0) - 2, width: X(rect.x1 - rect.x0) + 4, height: Y(rect.y1 - rect.y0) + 4 }}
        />
      ))}
      {badges.map(({ chunk, x, y, inline }) => (
        <div
          key={`badge-${chunk}`}
          className="pointer-events-none absolute z-20 flex h-[18px] min-w-[18px] items-center justify-center rounded-full px-1 text-[10px] font-semibold text-white shadow-sm"
          style={inline ? { left: X(x) - 9, top: Y(y) - 20, background: chunkSolid(chunk) } : { left: Math.max(2, X(x) - 24), top: Y(y) - 2, background: chunkSolid(chunk) }}
        >
          {chunk + 1}
        </div>
      ))}
    </div>
  );
}

function EditOverlay({ index, width, height, gaps }: OverlayProps) {
  const t = useT();
  const view = useChunkView();
  const doc = useEditor((s) => s.doc);
  const showBlocks = useEditor((s) => s.showBlocks);
  const showChunks = useEditor((s) => s.showChunks);
  const hover = useEditor((s) => s.hoverChunk);
  const select = useEditor((s) => s.select);
  const setHover = useEditor((s) => s.setHoverChunk);
  const selected = useSelectedChunk();
  const { dragging } = useContext(DragContext);

  const overlay = useMemo(
    () => (view && doc ? buildOverlay(view, doc.parsed.blocks, index, selected) : null),
    [view, doc, index, selected],
  );
  const gripShifts = useMemo(() => {
    // Boundaries after tiny adjacent blocks can land on the same spot; move later grips left so
    // every boundary stays reachable.
    const shifts = new Map<string, number>();
    const placed: { x: number; y: number }[] = [];
    for (const g of gaps) {
      if (!overlay?.afterBoundaries.has(g.afterBlockId)) continue;
      const y = g.y * height;
      let x = g.x0 * width;
      let shift = 0;
      while (placed.some((p) => Math.abs(p.y - y) < 20 && Math.abs(p.x - x) < 22)) {
        shift += 1;
        x -= 24;
      }
      placed.push({ x, y });
      shifts.set(g.afterBlockId, shift);
    }
    return shifts;
  }, [gaps, overlay, width, height]);
  if (!overlay || (!showChunks && !showBlocks)) return null;
  const X = (v: number) => v * width;
  const Y = (v: number) => v * height;

  return (
    <div className="absolute inset-0" onPointerLeave={() => setHover(null)}>
      {overlay.excluded.map(({ key, type, rect }) => (
        <div
          key={key}
          title={`${type} (excluded)`}
          className="absolute rounded-[2px] border border-dashed border-zinc-400/70 bg-[repeating-linear-gradient(135deg,transparent_0_5px,rgba(113,113,122,0.10)_5px_10px)]"
          style={{ left: X(rect.x0), top: Y(rect.y0), width: X(rect.x1 - rect.x0), height: Y(rect.y1 - rect.y0) }}
        />
      ))}
      {overlay.boxes.map(({ key, blockId, type, chunk, start, rect, whole, deleted }) => {
        const state = selected === chunk ? 'selected' : hover === chunk ? 'hover' : 'idle';
        const pad = whole ? 2 : 1;
        return (
          <div
            key={key}
            data-block={blockId}
            data-chunk={chunk}
            data-deleted={deleted || undefined}
            title={deleted ? t('deleted') : showBlocks ? `${type} · ${blockId}` : undefined}
            onPointerEnter={() => setHover(chunk)}
            onClick={() => select(blockId, false, start)}
            className={cx(
              'absolute cursor-pointer rounded-[2px]',
              showBlocks && 'outline outline-1 outline-zinc-500/60',
              deleted && showChunks && 'border border-dashed bg-[repeating-linear-gradient(135deg,rgba(113,113,122,0.06)_0_5px,rgba(113,113,122,0.2)_5px_10px)]',
            )}
            style={{
              left: X(rect.x0) - pad,
              top: Y(rect.y0) - pad,
              width: X(rect.x1 - rect.x0) + pad * 2,
              height: Y(rect.y1 - rect.y0) + pad * 2,
              background: showChunks && !deleted ? chunkFill(chunk, state) : undefined,
              borderColor: deleted ? (state === 'idle' ? 'rgba(113,113,122,0.6)' : chunkStroke(chunk, true)) : undefined,
              boxShadow:
                showChunks && whole && !deleted
                  ? `inset 0 0 0 ${state === 'selected' ? 1.5 : 1}px ${chunkStroke(chunk, state === 'selected')}`
                  : undefined,
            }}
          />
        );
      })}
      {showChunks &&
        overlay.badges.map(({ chunk, x, y, inline }) => {
          // A deleted chunk has no number: the numbers are those of the export.
          const label = view!.labels[chunk];
          const background = label === null ? 'rgb(113,113,122)' : chunkSolid(chunk);
          return (
            <div
              key={`badge-${chunk}`}
              data-badge={chunk}
              className="pointer-events-none absolute z-20 flex h-[18px] min-w-[18px] items-center justify-center rounded-full px-1 text-[10px] font-semibold text-white shadow-sm"
              style={inline ? { left: X(x) - 9, top: Y(y) - 20, background } : { left: Math.max(2, X(x) - 24), top: Y(y) - 2, background }}
            >
              {label ?? '×'}
            </div>
          );
        })}
      {showChunks &&
        gaps.map((g) =>
          overlay.afterBoundaries.has(g.afterBlockId) ? (
            <BoundaryHandle
              key={g.afterBlockId}
              gap={g}
              chunk={overlay.afterBoundaries.get(g.afterBlockId)!}
              width={width}
              height={height}
              shift={gripShifts.get(g.afterBlockId) ?? 0}
            />
          ) : (
            !dragging && <SplitZone key={g.afterBlockId} gap={g} width={width} height={height} />
          ),
        )}
      {showChunks &&
        !dragging &&
        overlay.ticks.map(({ boundary, caret }) => (
          <SentenceTick key={boundaryKey(boundary)} boundary={boundary} caret={caret} width={width} height={height} />
        ))}
      {showChunks &&
        overlay.inner.map(({ boundary, chunk, caret, gripX }) => (
          <InnerHandle key={boundaryKey(boundary)} boundary={boundary} chunk={chunk} caret={caret} gripX={gripX} width={width} height={height} />
        ))}
    </div>
  );
}

function Grip({ boundary, chunk, style }: { boundary: Boundary; chunk: number; style: React.CSSProperties }) {
  const t = useT();
  const { start, dragging } = useContext(DragContext);
  return (
    <button
      type="button"
      aria-label={t('dragBoundary')}
      title={t('dragBoundary')}
      onPointerDown={(e) => start(boundary, e)}
      className={cx(
        'pointer-events-auto absolute flex h-[18px] w-[20px] cursor-grab touch-none items-center justify-center rounded-md text-white shadow ring-1 ring-white/60 active:cursor-grabbing',
        dragging === boundaryKey(boundary) && 'scale-110',
      )}
      style={{ background: chunkSolid(chunk), ...style }}
    >
      <GripVertical className="size-3.5" strokeWidth={2.5} />
    </button>
  );
}

function RemoveButton({ boundary, style }: { boundary: Boundary; style: React.CSSProperties }) {
  const t = useT();
  const apply = useEditor((s) => s.apply);
  return (
    <button
      type="button"
      aria-label={t('removeBoundary')}
      title={t('removeBoundary')}
      onClick={() => apply((p) => removeBoundary(p, boundary))}
      className="pointer-events-auto absolute flex size-[18px] items-center justify-center rounded-full bg-white text-zinc-700 opacity-0 shadow ring-1 ring-zinc-300 transition-opacity group-hover:opacity-100 hover:bg-red-50 hover:text-red-600 focus-visible:opacity-100"
      style={style}
    >
      <X className="size-3" strokeWidth={3} />
    </button>
  );
}

function BoundaryHandle({ gap, chunk, width, height, shift }: { gap: Gap; chunk: number; width: number; height: number; shift: number }) {
  const { dragging } = useContext(DragContext);
  const boundary = { blockId: gap.afterBlockId };
  const active = dragging === gap.afterBlockId;
  const lineWidth = (gap.x1 - gap.x0) * width;
  return (
    // Only the grip, the remove button and the line itself take the pointer, so a handle never
    // covers a neighbouring handle or split zone when blocks are close together.
    <div
      className="group pointer-events-none absolute z-20"
      style={{ left: gap.x0 * width - 22, top: gap.y * height - 9, width: lineWidth + 44, height: 18 }}
      data-boundary={gap.afterBlockId}
    >
      <div className="pointer-events-auto absolute top-1/2 h-[8px] -translate-y-1/2" style={{ left: 22, width: lineWidth }}>
        <div
          className={cx(
            'absolute top-1/2 h-[2px] w-full -translate-y-1/2 rounded-full transition-[height]',
            active ? 'h-[3px]' : 'group-hover:h-[3px]',
          )}
          style={{ background: chunkSolid(chunk) }}
        />
      </div>
      <Grip boundary={boundary} chunk={chunk} style={{ left: -24 * shift, top: 0 }} />
      <RemoveButton boundary={boundary} style={{ right: 0, top: 0 }} />
    </div>
  );
}

/** A boundary inside a block: a caret between two characters, with its grip in the left margin. */
function InnerHandle({
  boundary,
  chunk,
  caret,
  gripX,
  width,
  height,
}: {
  boundary: Boundary;
  chunk: number;
  caret: PageRect;
  gripX: number;
  width: number;
  height: number;
}) {
  const left = gripX * width - 24;
  const x = caret.x0 * width;
  const top = caret.y0 * height;
  const h = (caret.y1 - caret.y0) * height;
  return (
    <div className="group pointer-events-none absolute z-20" style={{ left, top: top - 3, width: x - left + 2, height: h + 6 }} data-boundary={boundaryKey(boundary)}>
      <div
        className="pointer-events-none absolute border-t border-dashed opacity-60"
        style={{ left: 20, width: x - left - 20, top: (h + 6) / 2, borderColor: chunkSolid(chunk) }}
      />
      <div className="pointer-events-none absolute w-[3px] rounded-full" style={{ left: x - left - 1, top: 0, height: h + 6, background: chunkSolid(chunk) }} />
      <Grip boundary={boundary} chunk={chunk} style={{ left: 0, top: (h + 6) / 2 - 9 }} />
      <RemoveButton boundary={boundary} style={{ left: -22, top: (h + 6) / 2 - 9 }} />
    </div>
  );
}

function SplitZone({ gap, width, height }: { gap: Gap; width: number; height: number }) {
  const t = useT();
  const apply = useEditor((s) => s.apply);
  const blocks = useEditor((s) => s.doc?.parsed.blocks);
  const top = gap.top * height;
  const h = Math.max(8, (gap.bottom - gap.top) * height);
  return (
    <button
      type="button"
      aria-label={t('addBoundaryHint')}
      data-split-after={gap.afterBlockId}
      onClick={(e) => {
        e.stopPropagation();
        if (blocks) apply((p) => addBoundary(p, blocks, { blockId: gap.afterBlockId }));
      }}
      className="group absolute z-10 cursor-row-resize"
      style={{ left: gap.x0 * width, top, width: (gap.x1 - gap.x0) * width, height: h }}
    >
      <span
        className="pointer-events-none absolute right-0 left-0 border-t-2 border-dashed border-accent-500 opacity-0 transition-opacity group-hover:opacity-100"
        style={{ top: gap.y * height - top - 1 }}
      />
      <span
        className="pointer-events-none absolute right-0 z-10 flex -translate-y-1/2 items-center gap-1 rounded-full bg-accent-600 px-2 py-0.5 text-[11px] font-medium whitespace-nowrap text-white opacity-0 shadow transition-opacity group-hover:opacity-100"
        style={{ top: gap.y * height - top }}
      >
        <Plus className="size-3" strokeWidth={3} />
        {t('addBoundaryHint')}
      </span>
    </button>
  );
}

/** A sentence start inside the selected chunk where the chunk can be split. */
function SentenceTick({ boundary, caret, width, height }: { boundary: Boundary; caret: PageRect; width: number; height: number }) {
  const t = useT();
  const apply = useEditor((s) => s.apply);
  const blocks = useEditor((s) => s.doc?.parsed.blocks);
  const h = (caret.y1 - caret.y0) * height;
  return (
    <button
      type="button"
      aria-label={t('splitSentence')}
      title={t('splitSentence')}
      data-split-inner={boundaryKey(boundary)}
      onClick={(e) => {
        e.stopPropagation();
        if (blocks) apply((p) => addBoundary(p, blocks, boundary));
      }}
      className="group absolute z-10 flex w-[12px] cursor-col-resize justify-center"
      style={{ left: caret.x0 * width - 6, top: caret.y0 * height - 2, height: h + 4 }}
    >
      <span className="h-full w-[2px] rounded-full bg-accent-500/45 transition-colors group-hover:bg-accent-600" />
      <span className="pointer-events-none absolute bottom-full mb-1 hidden items-center gap-1 rounded-full bg-accent-600 px-2 py-0.5 text-[11px] font-medium whitespace-nowrap text-white shadow group-hover:flex">
        <Scissors className="size-3" />
        {t('splitSentence')}
      </span>
    </button>
  );
}
