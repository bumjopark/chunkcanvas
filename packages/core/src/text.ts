import type { BlockType, DocumentBlock } from './types';

/** A UTF-16 offset must not separate the halves of a supplementary character. */
export function isTextBoundary(text: string, offset: number): boolean {
  if (!Number.isInteger(offset) || offset < 0 || offset > text.length) return false;
  const before = text.charCodeAt(offset - 1), after = text.charCodeAt(offset);
  return !(before >= 0xd800 && before <= 0xdbff && after >= 0xdc00 && after <= 0xdfff);
}

/** Block types that may be split inside, given character positions. Others are atomic. */
export const SPLITTABLE_TYPES: ReadonlySet<BlockType> = new Set(['paragraph', 'list_item', 'caption', 'footnote', 'table']);

/** Markdown before the plain text, e.g. "- " for a list item; empty when markdown is not a prefix + text. */
export function markdownPrefix(block: DocumentBlock): string | null {
  if (block.markdown === undefined) return '';
  return block.markdown.endsWith(block.text) ? block.markdown.slice(0, block.markdown.length - block.text.length) : null;
}

export function canSplitInside(block: DocumentBlock): boolean {
  return (
    SPLITTABLE_TYPES.has(block.type) && !!block.lines?.length && block.text.length > 1 && markdownPrefix(block) !== null
  );
}

/** The header and separator lines of a Markdown table; empty when the text is not one. */
export function tableHeader(text: string): string {
  const lines = text.split('\n', 3);
  return lines.length === 3 && /^\|\s*:?-{3}/.test(lines[1]!) ? `${lines[0]}\n${lines[1]}` : '';
}

/**
 * The header lines later pieces of a table repeat: none when the first row is data shaped like
 * the rows below it (a list of cited patents "| 5,208,634 A | 5/1993 Ikemoto et al. |", a table
 * of contents) rather than column names, or every piece would repeat that one entry; none when
 * only one cell of it is filled.
 */
export function repeatedHeader(text: string): string {
  const header = tableHeader(text);
  if (!header) return '';
  const [first, , ...rest] = text.split('\n', 5);
  const data = rest.map(tableCells);
  const cells = tableCells(first!);
  const filled = cells.map((c, i) => [i, c] as const).filter(([, c]) => c);
  // A first row with one cell filled names no columns ("|  |  |  | 1 |" over a sheet's data, a
  // copyright line); a few filled cells over groups of columns still do ("|  | Qwen3-4B |  |").
  if (cells.length > 1 && filled.length < 2) return '';
  const numbers = filled.filter(([, c]) => /\d/.test(c));
  if (numbers.length < filled.length / 2) return header;
  const alike = numbers.filter(([i, c]) => data.some((row) => row[i] !== undefined && cellShape(row[i]!) === cellShape(c)));
  return alike.length >= Math.max(1, numbers.length / 2) ? '' : header;
}

function tableCells(line: string): string[] {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split(' | ').map((c) => c.trim());
}

/** Letters as "a" per word, digits as "9": "5,208,634 A" and "5,223,893 A" have the same shape. */
function cellShape(cell: string): string {
  return cell.replace(/\p{L}+/gu, 'a').replace(/\d/g, '9');
}

/** Starts of the rows of a Markdown table that may begin a piece: the second data row on. */
export function tableRowStarts(text: string): number[] {
  if (!tableHeader(text)) return [];
  const starts: number[] = [];
  let line = 0;
  for (let i = 0; i < text.length - 1; i++) {
    if (text[i] === '\n' && ++line >= 3) starts.push(i + 1);
  }
  return starts;
}

/** Offsets where a block may be cut inside: between rows of a table, at sentence starts elsewhere. */
export function splitPoints(block: DocumentBlock): number[] {
  if (!canSplitInside(block)) return [];
  return block.type === 'table' ? tableRowStarts(block.text) : sentenceStarts(block.text);
}

// Common abbreviations that end with a period but rarely end a sentence.
const ABBREVIATIONS = new Set([
  'mr', 'mrs', 'ms', 'dr', 'prof', 'sr', 'jr', 'st', 'vs', 'etc', 'fig', 'figs', 'no', 'nos', 'vol', 'vols', 'pp', 'p',
  'eq', 'eqs', 'ref', 'refs', 'sec', 'ch', 'cf', 'al', 'inc', 'ltd', 'co', 'corp', 'dept', 'approx', 'e.g', 'i.e', 'u.s',
  'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'sept', 'oct', 'nov', 'dec',
]);

const CLOSERS = `"'”’)]}」』）】〉》`;
// Full-width sentence ends used in Chinese and Japanese, which do not need a following space.
const CJK_END = /[。！？]/;
const LATIN_END = /[.!?…]/;

/**
 * Offsets (> 0) where a new sentence starts. Handles Latin-script and Korean punctuation followed
 * by a space (skipping abbreviations, initials and decimals) and CJK full-width punctuation
 * without spaces.
 */
export function sentenceStarts(text: string): number[] {
  const starts: number[] = [];
  for (let i = 0; i < text.length; i++) {
    const c = text[i]!;
    let end = -1;
    if (CJK_END.test(c)) {
      end = i + 1;
    } else if (LATIN_END.test(c)) {
      // Not a decimal point or part of "..." mid-word.
      if (c === '.' && /\d/.test(text[i + 1] ?? '')) continue;
      end = i + 1;
      if (c === '.') {
        const word = /([\p{L}.]+)$/u.exec(text.slice(Math.max(0, i - 12), i))?.[1] ?? '';
        if (ABBREVIATIONS.has(word.toLowerCase()) || /^\p{Lu}$/u.test(word)) continue;
      }
    } else {
      continue;
    }
    while (end < text.length && CLOSERS.includes(text[end]!)) end++;
    let next = end;
    const spaced = next < text.length && /\s/.test(text[next]!);
    while (next < text.length && /\s/.test(text[next]!)) next++;
    if (next >= text.length) break;
    // Latin-style ends need a space after them and should not continue in lowercase.
    if (!CJK_END.test(c) && (!spaced || /\p{Ll}/u.test(text[next]!))) continue;
    if (starts[starts.length - 1] !== next) starts.push(next);
    i = next - 1;
  }
  return starts;
}

/** Offsets where a new word starts (after whitespace), for fine-grained snapping. */
export function wordStarts(text: string): number[] {
  const out: number[] = [];
  for (let i = 1; i < text.length; i++) if (/\s/.test(text[i - 1]!) && !/\s/.test(text[i]!)) out.push(i);
  return out;
}
