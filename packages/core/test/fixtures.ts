import type { BlockType, DocumentBlock, ParsedDocument } from '../src';

/** Whitespace tokenizer: deterministic and easy to reason about in tests. */
export const words = (text: string): number => text.split(/\s+/).filter(Boolean).length;

export function block(id: string, type: BlockType, text: string, page = 0, order?: number): DocumentBlock {
  return {
    id,
    readingOrder: order ?? Number(id.replace(/\D/g, '')),
    type,
    text,
    regions: [{ pageIndex: page, bbox: { x0: 0.1, y0: 0.1, x1: 0.9, y1: 0.2 } }],
    source: { parser: 'test' },
  };
}

export const lorem = (n: number): string => Array.from({ length: n }, (_, i) => `w${i}`).join(' ');

export function doc(blocks: DocumentBlock[]): ParsedDocument {
  return {
    schemaVersion: 1,
    document: { id: 'doc1', filename: 'a.pdf', fileHash: 'hash1', pageCount: 3, parser: 'test', parserVersion: '1' },
    pages: [0, 1, 2].map((index) => ({ index, width: 595, height: 842 })),
    blocks,
    blocksFingerprint: 'fp1',
  };
}

/** A paragraph with character positions on one line, so it can be split inside. */
export function lined(id: string, text: string, page = 0, order?: number): DocumentBlock {
  const b = block(id, 'paragraph', text, page, order);
  return {
    ...b,
    lines: [{ pageIndex: page, bbox: { x0: 0.1, y0: 0.1, x1: 0.9, y1: 0.12 }, start: 0, end: text.length, xs: [...text].map((_, i) => 0.1 + (0.8 * i) / text.length) }],
  };
}

export const after = (blockId: string) => ({ blockId });
export const at = (blockId: string, offset: number) => ({ blockId, offset });
