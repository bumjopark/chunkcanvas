import type { BlockRegion, DocumentBlock } from '@chunkcanvas/core';

/** Where a boundary after a block is drawn: a horizontal segment on one page, normalized coords. */
export interface Gap {
  afterBlockId: string;
  /** Index of the block in the chunkable sequence. */
  seqIndex: number;
  pageIndex: number;
  x0: number;
  x1: number;
  /** Line position. */
  y: number;
  /** Hit band around the line. */
  top: number;
  bottom: number;
}

const MIN_BAND = 0.006;

function lastRegion(b: DocumentBlock): BlockRegion | undefined {
  return b.regions[b.regions.length - 1];
}

function overlapsHorizontally(a: BlockRegion, b: BlockRegion): boolean {
  return Math.min(a.bbox.x1, b.bbox.x1) - Math.max(a.bbox.x0, b.bbox.x0) > 0.02;
}

/**
 * The gap after each block (except the last) that has a location. When the next block sits
 * directly below in the same column, the gap spans the space between them; otherwise (column or
 * page change) it is a band just under the block. Never a full-page-width line, so multi-column
 * layouts stay correct.
 */
export function computeGaps(seq: readonly DocumentBlock[]): Gap[] {
  const gaps: Gap[] = [];
  for (let i = 0; i < seq.length - 1; i++) {
    const r = lastRegion(seq[i]!);
    if (!r) continue;
    const next = seq[i + 1]!.regions[0];
    let { x0, x1 } = r.bbox;
    let y = r.bbox.y1 + 0.004;
    let top = r.bbox.y1 - MIN_BAND / 2;
    let bottom = r.bbox.y1 + MIN_BAND * 1.5;
    if (next && next.pageIndex === r.pageIndex && overlapsHorizontally(r, next) && next.bbox.y0 >= r.bbox.y1 - 0.004) {
      const space = next.bbox.y0 - r.bbox.y1;
      y = r.bbox.y1 + Math.max(0, space) / 2;
      top = Math.min(r.bbox.y1 - 0.002, y - MIN_BAND / 2);
      bottom = Math.max(next.bbox.y0 + 0.002, y + MIN_BAND / 2);
      x0 = Math.min(x0, next.bbox.x0);
      x1 = Math.max(x1, next.bbox.x1);
    }
    gaps.push({ afterBlockId: seq[i]!.id, seqIndex: i, pageIndex: r.pageIndex, x0, x1, y, top, bottom });
  }
  return gaps;
}

/** A rectangle on one page, normalized coordinates. */
export interface PageRect {
  pageIndex: number;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/** Where character `offset` of a block sits: a caret on one line. Null without line positions. */
export function caretAt(block: DocumentBlock, offset: number): PageRect | null {
  const lines = block.lines;
  if (!lines?.length) return null;
  const line = lines.find((l) => l.end > offset) ?? lines[lines.length - 1]!;
  const x = offset < line.start ? line.bbox.x0 : offset >= line.end ? line.bbox.x1 : line.xs[offset - line.start]!;
  return { pageIndex: line.pageIndex, x0: x, x1: x, y0: line.bbox.y0, y1: line.bbox.y1 };
}

/** Rectangles covering characters [start, end) of a block, one per line. */
export function rangeRects(block: DocumentBlock, start: number, end: number): PageRect[] {
  const out: PageRect[] = [];
  for (const l of block.lines ?? []) {
    if (l.end <= start || l.start >= end) continue;
    const x0 = start > l.start ? l.xs[start - l.start]! : l.bbox.x0;
    const x1 = end < l.end ? l.xs[end - l.start]! : l.bbox.x1;
    if (x1 > x0) out.push({ pageIndex: l.pageIndex, x0, x1, y0: l.bbox.y0, y1: l.bbox.y1 });
  }
  return out;
}
