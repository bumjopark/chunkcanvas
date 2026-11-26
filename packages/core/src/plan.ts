import { canSplitInside, isTextBoundary, markdownPrefix, repeatedHeader, splitPoints, tableRowStarts } from './text';
import type {
  BlockType,
  Boundary,
  Chunk,
  ChunkSettings,
  DocumentBlock,
  Segment,
  SplitPlan,
  TokenCounter,
} from './types';

export const DEFAULT_SETTINGS: ChunkSettings = {
  targetTokens: 500,
  maxTokens: 800,
  overlapTokens: 0,
  structureAware: true,
  keepHeadingWithBody: true,
  excludeFurniture: true,
};

const HEADING_TYPES: ReadonlySet<BlockType> = new Set(['title', 'section_header']);
const FURNITURE_TYPES: ReadonlySet<BlockType> = new Set(['page_header', 'page_footer']);
const FIGURE_TYPES: ReadonlySet<BlockType> = new Set(['table', 'picture']);

/** Separator placed between blocks in chunk text. */
export const BLOCK_SEPARATOR = '\n\n';

export function isHeading(block: DocumentBlock): boolean {
  return HEADING_TYPES.has(block.type);
}

export function isFurniture(block: DocumentBlock): boolean {
  return FURNITURE_TYPES.has(block.type);
}

/** The ordered blocks that take part in chunking under these settings. */
export function chunkableBlocks(blocks: readonly DocumentBlock[], settings: ChunkSettings): DocumentBlock[] {
  return orderedBlocks(blocks, settings).seq;
}

/**
 * Blocks in reading order, with the notes at the foot of the pages moved after all the rest:
 * they are no part of the text they interrupt. `notesFrom` is where they start.
 */
function orderedBlocks(blocks: readonly DocumentBlock[], settings: ChunkSettings): { seq: DocumentBlock[]; notesFrom: number } {
  const read = blocks
    .filter((b) => !(settings.excludeFurniture && isFurniture(b)))
    .slice()
    .sort((a, b) => a.readingOrder - b.readingOrder);
  const notes = read.map((_, i) => isPageNote(read, i));
  const body = read.filter((_, i) => !notes[i]);
  return { seq: [...body, ...read.filter((_, i) => notes[i])], notesFrom: body.length };
}

/**
 * Whether a footnote is a note at the foot of a page rather than a note under a table or picture
 * ("* p < .05" right below the table, maybe after its caption or other notes), which stays with it.
 */
function isPageNote(read: readonly DocumentBlock[], i: number): boolean {
  if (read[i]!.type !== 'footnote') return false;
  for (let j = i - 1; j >= 0; j--) {
    const above = read[j]!.regions.at(-1);
    const below = read[j + 1]!.regions[0];
    const gap = above && below && above.pageIndex === below.pageIndex ? below.bbox.y0 - above.bbox.y1 : Infinity;
    if (gap < -0.02 || gap > 0.05) return true;
    if (FIGURE_TYPES.has(read[j]!.type)) return false;
    if (read[j]!.type !== 'caption' && read[j]!.type !== 'footnote') return true;
  }
  return true;
}

export function blockMarkdown(block: DocumentBlock): string {
  return block.markdown ?? block.text;
}

/** Text of part of a block as it appears in a chunk. A block's tail drops its markdown prefix. */
export function segmentText(block: DocumentBlock, start: number, end: number): string {
  if (start <= 0 && end >= block.text.length) return blockMarkdown(block);
  const part = block.text.slice(start, end);
  if (start <= 0) return `${markdownPrefix(block) ?? ''}${part.trimEnd()}`;
  // A table cut between rows: every piece still says what its columns are.
  const header = block.type === 'table' ? repeatedHeader(block.text) : '';
  return header ? `${header}\n${part.trim()}` : part.trim();
}

export function joinBlocks(blocks: readonly DocumentBlock[]): string {
  return blocks
    .map(blockMarkdown)
    .filter((s) => s.trim().length > 0)
    .join(BLOCK_SEPARATOR);
}

/**
 * Blocks that stay together unless a chunk would exceed max: a caption and the table/picture next
 * to it, a display formula with the sentence that introduces it ("…is given by") and with a
 * sentence that continues after it ("where x is…"), and text ending in a colon with the list,
 * table or text it announces ("Our contributions are:").
 */
function glued(prev: DocumentBlock, next: DocumentBlock, prevSide: number, nextSide: number): boolean {
  return (
    // A caption goes with the figure on its side (captionSides), as do the captions beside it there.
    (prev.type === 'caption' && prevSide > 0 && (FIGURE_TYPES.has(next.type) || nextSide > 0)) ||
    (next.type === 'caption' && nextSide < 0 && (FIGURE_TYPES.has(prev.type) || prevSide < 0)) ||
    (next.type === 'formula' && LEAD_IN_TYPES.has(prev.type)) ||
    (prev.type === 'formula' && LEAD_IN_TYPES.has(next.type) && CONTINUATION.test(next.text)) ||
    ((prev.type === 'paragraph' || prev.type === 'list_item') && /[:：]\s*$/.test(prev.text) && ANNOUNCED_TYPES.has(next.type))
  );
}

function firstPage(block: DocumentBlock): number {
  return block.regions[0]?.pageIndex ?? -1;
}

function lastPage(block: DocumentBlock): number {
  return block.regions.at(-1)?.pageIndex ?? -1;
}

/** Whether a caption can be the one of this table or picture: "Table 3" names a table, "Fig. 7" a picture. */
function names(caption: DocumentBlock, figure: DocumentBlock): boolean {
  if (TABLE_CAPTION.test(caption.text)) return figure.type === 'table';
  if (PICTURE_CAPTION.test(caption.text)) return figure.type === 'picture';
  return true;
}

function labelled(caption: DocumentBlock): boolean {
  return TABLE_CAPTION.test(caption.text) || PICTURE_CAPTION.test(caption.text);
}

/** The number a labelled caption gives its figure: "2" for "Table 2:", "VII" for "TABLE VII", "4b" for "도면4b". */
function labelNumber(caption: DocumentBlock): string {
  const label = TABLE_CAPTION.exec(caption.text) ?? PICTURE_CAPTION.exec(caption.text);
  if (!label) return '';
  return /^\s*[.:：]?\s*([^\s.:：,)>\]〉】]*)/.exec(caption.text.slice(label[0].length))![1]!.toLowerCase();
}

/**
 * For each block of the sequence, which figure a caption goes with: -1 the one before it, 1 the
 * one after, 0 none (and for other blocks). A caption between two figures it can name goes with
 * the one on its page, and else with the one before it ("Fig. 7" under its picture) unless that
 * one has its own label already: one of another number ("Table 2" above its table, or under it,
 * then "Table 3" above the next) makes it the next one's; one of its number ("Fig. 4 …" then
 * "그림 4 …" under a picture) makes it the same one's. An unnumbered one is set like the caption
 * before it on its page: above its figure when that one was.
 */
function captionSides(seq: readonly DocumentBlock[]): number[] {
  const side = seq.map(() => 0);
  for (let s = 0; s < seq.length; s++) {
    if (seq[s]!.type !== 'caption') continue;
    let e = s;
    while (e + 1 < seq.length && seq[e + 1]!.type === 'caption') e++;
    const before = s > 0 && FIGURE_TYPES.has(seq[s - 1]!.type) ? seq[s - 1]! : undefined;
    const after = e + 1 < seq.length && FIGURE_TYPES.has(seq[e + 1]!.type) ? seq[e + 1]! : undefined;
    for (let k = s; k <= e; k++) {
      const caption = seq[k]!;
      const back = before !== undefined && names(caption, before);
      const ahead = after !== undefined && names(caption, after);
      side[k] = back ? -1 : ahead ? 1 : 0;
      if (!back || !ahead) continue;
      const page = firstPage(caption);
      if ((lastPage(before!) === page) !== (firstPage(after!) === page)) {
        side[k] = firstPage(after!) === page ? 1 : -1;
      } else if (labelled(caption)) {
        // Its labels: above it on its page (a label at the foot of the page before is none), and
        // under it in this row.
        const own: DocumentBlock[] = [];
        for (let j = s - 2; j >= 0 && seq[j]!.type === 'caption' && side[j] === 1; j--) {
          if (lastPage(seq[j]!) === firstPage(before!)) own.push(seq[j]!);
        }
        for (let j = s; j < k; j++) if (side[j] === -1) own.push(seq[j]!);
        const numbers = own.filter(labelled).map(labelNumber);
        if (numbers.length && !numbers.includes(labelNumber(caption))) side[k] = 1;
      } else if (s === e && !SOURCE_LINE.test(caption.text) && !SUBFIGURE.test(caption.text)) {
        // An unnamed title alone between two figures of its page is set where the unnamed title
        // before it on the page was: above a figure when that one opened its figure ("Field
        // curvature" over one plot, "Distortion" over the one beside it). A source or note line, or
        // a subfigure label ("(a) DC voltage" under its plot), stays with the figure above it.
        let j = s - 1;
        while (j >= 0 && !(seq[j]!.type === 'caption' && side[j] !== 0) && lastPage(seq[j]!) === page) j--;
        if (j >= 0 && side[j] === 1 && firstPage(seq[j]!) === page && !labelled(seq[j]!)) side[k] = 1;
      }
    }
    s = e;
  }
  return side;
}

/** A subfigure's label: "(a) DC voltage", "b) Front", "(2) Top view". */
const SUBFIGURE = /^\s*\(?\s*(?:[a-z]|[ivx]{1,4}|\d{1,2})\s*\)/i;
/** A source or note line under a figure ("Source: ...", "(\ucd9c\ucc98) ...", "* p < .05"). */
const SOURCE_LINE = /^\s*[(（[]?\s*(?:sources?|notes?|\ucd9c\ucc98|\uc790\ub8cc|\uc8fc\s*[:)）]|\u51fa\u5178|\u8cc7\u6599|\u6ce8|[*※])/i;
const TABLE_CAPTION = /^\s*[<[(〈【]?\s*(?:table|tab\.|표|\u8868)/i;
const PICTURE_CAPTION = /^\s*[<[(〈【]?\s*(?:fig(?:ure)?\b|fig\.|그림|도면|도표|사진|\u56f3(?!\u8868)|\u56fe|\u5716)/i;
const LEAD_IN_TYPES: ReadonlySet<BlockType> = new Set(['paragraph', 'list_item', 'formula']);
/** What text ending in a colon introduces. */
const ANNOUNCED_TYPES: ReadonlySet<BlockType> = new Set(['paragraph', 'list_item', 'table', 'code', 'picture']);
/** Captions up to this size follow their table or picture even past the maximum. */
const CAPTION_TOKENS = 150;
/**
 * Text that carries on the sentence around a formula rather than starting a new one: lower case,
 * "where", "Here x is …" explaining its symbols, or a Korean particle that attaches to the formula ("[식] 와 같다", "를 계산한다", "이다.").
 */
const CONTINUATION = new RegExp(
  '^\\s*(?:[a-z(,;]|[Ww]here\\b|with\\b|[Hh]ere,?\\s+(?:\\S+\\s+){0,6}?(?:is|are|denotes?|stands?)\\b|여기서|이때|단,'
  + '|(?:와|과|을|를|은|는|가|의|에|에서|로|으로|이다|이고|이며|이면|이므로|이라|라고)(?=[\\s,.)])'
  // Extraction often loses the space after the particle ("가된다", "이된다"): a particle with a verb
  // after it still goes on ("이 논문에서", "가장" do not).
  + '|(?:가|이|와|과|을|를|은|는|로|으로)\\s?(?:된|되|됨|될|같|표현|정의|나타|주어|계산|얻|구하|구할|성립|만족|쓸|쓰|놓|근사))',
);

/* Positions ---------------------------------------------------------------------------------- */

/** A cut as [sequence index, character offset]; Infinity means after the whole block. */
type Pos = readonly [number, number];

function cmp(a: Pos, b: Pos): number {
  return a[0] - b[0] || (a[1] === b[1] ? 0 : a[1] < b[1] ? -1 : 1);
}

function indexMap(seq: readonly DocumentBlock[]): Map<string, number> {
  return new Map(seq.map((b, i) => [b.id, i]));
}

/** Canonical position of a boundary in this sequence, or null if it cannot be a valid cut. */
function toPos(seq: readonly DocumentBlock[], index: Map<string, number>, b: Boundary): Pos | null {
  const i = index.get(b.blockId);
  if (i === undefined) return null;
  const block = seq[i]!;
  const len = block.text.length;
  if (b.offset !== undefined && b.offset > 0 && b.offset < len && !isTextBoundary(block.text, b.offset)) return null;
  let pos: Pos;
  if (b.offset === undefined || b.offset >= len) pos = [i, Infinity];
  else if (b.offset <= 0) pos = [i - 1, Infinity];
  else if (!canSplitInside(block)) pos = b.offset < len / 2 ? [i - 1, Infinity] : [i, Infinity];
  else if (block.type === 'table') {
    // Never inside a row: the nearest row start, or the table's edge when it has too few rows.
    const rows = tableRowStarts(block.text);
    const near = rows.reduce<number | undefined>((best, r) => (best === undefined || Math.abs(r - b.offset!) < Math.abs(best - b.offset!) ? r : best), undefined);
    pos = near !== undefined ? [i, near] : b.offset < len / 2 ? [i - 1, Infinity] : [i, Infinity];
  } else pos = [i, b.offset];
  if (pos[0] < 0) return null;
  if (pos[0] === seq.length - 1 && pos[1] === Infinity) return null;
  return pos;
}

function fromPos(seq: readonly DocumentBlock[], pos: Pos): Boundary {
  const id = seq[pos[0]]!.id;
  return pos[1] === Infinity ? { blockId: id } : { blockId: id, offset: pos[1] };
}

export function sameBoundary(a: Boundary, b: Boundary): boolean {
  return a.blockId === b.blockId && (a.offset ?? null) === (b.offset ?? null);
}

/** Valid, de-duplicated boundaries in reading order. Offsets at block edges become block cuts. */
export function normalizeBoundaries(seq: readonly DocumentBlock[], boundaries: readonly Boundary[]): Boundary[] {
  const index = indexMap(seq);
  const positions = boundaries
    .map((b) => toPos(seq, index, b))
    .filter((p): p is Pos => p !== null)
    .sort(cmp);
  return positions.filter((p, i) => i === 0 || cmp(positions[i - 1]!, p) !== 0).map((p) => fromPos(seq, p));
}

/* Auto-split ---------------------------------------------------------------------------------- */

/**
 * Structure-aware greedy split. Block token counts are summed (plus one per separator) as an
 * estimate; displayed chunk counts are always measured on the real chunk text. A single block
 * larger than the maximum is then split at sentence ends when its characters are known.
 */
export function autoSplit(
  blocks: readonly DocumentBlock[],
  settings: ChunkSettings,
  countTokens: TokenCounter,
): Boundary[] {
  const { seq, notesFrom } = orderedBlocks(blocks, settings);
  const tokens = seq.map((b) => countTokens(blockMarkdown(b)));
  const minBeforeHeadingSplit = Math.max(1, Math.floor(settings.targetTokens * 0.25));
  const lead = leadingBlocks(seq);
  const allLead = (a: number, b: number) => lead.slice(a, b).every(Boolean);
  const side = captionSides(seq);
  // Whether the block at k goes together with the one before it.
  const together = (k: number) => k > 0 && glued(seq[k - 1]!, seq[k]!, side[k - 1]!, side[k]!);
  const cuts: number[] = [];
  let start = 0;
  let current = 0;

  for (let i = 0; i < seq.length; i++) {
    const block = seq[i]!;
    const t = tokens[i]!;
    // The notes at the foot of the pages start chunks of their own.
    if (i === notesFrom && i > start) {
      cuts.push(i - 1);
      start = i;
    }
    if (i === start) {
      current = t;
      continue;
    }
    const added = current + 1 + t;
    const forced = added > settings.maxTokens;
    let split = forced || added > settings.targetTokens;
    if (!split && settings.structureAware && isHeading(block) && current >= minBeforeHeadingSplit) split = true;
    // Past the target, blocks that go together are not parted: the chunk ends before them when it
    // is mostly filled already, and takes them in otherwise.
    let runStart = i;
    if (split && !forced && together(i)) {
      while (runStart - 1 > start && together(runStart)) runStart--;
      let kept = 0;
      for (let j = start; j < runStart; j++) kept += tokens[j]! + (j > start ? 1 : 0);
      const fills = !together(runStart) && kept >= 0.75 * settings.targetTokens
        && !allLead(start, runStart);
      if (!fills) split = false;
    }
    // A chunk of headings alone says nothing: they go with what follows, past the target.
    if (split && !forced && settings.keepHeadingWithBody && allLead(start, i)) split = false;
    // A block that does not fit with its captions or headings is cut inside: a chunk of only those
    // goes on into it (a caption after other text moves to it below, as for any figure).
    if (forced && canSplitInside(block)
        && seq.slice(start, i).every((b, m) => (settings.keepHeadingWithBody && lead[start + m]) || (b.type === 'caption' && side[start + m] === 1))) {
      split = false;
    }
    // A short caption before a table or picture too large for any chunk still goes with it.
    if (forced && !canSplitInside(block) && t > settings.maxTokens && current <= CAPTION_TOKENS
        && seq.slice(start, i).every((b, m) => b.type === 'caption' && side[start + m] === 1)) split = false;
    // A short caption after a table or picture too large to share a chunk still goes with it.
    if (forced && block.type === 'caption' && together(i) && t <= CAPTION_TOKENS) split = false;
    if (!split) {
      current = added;
      continue;
    }

    let cut = forced ? i : runStart;
    // A full chunk ends before the blocks that go together with this one: a caption before its
    // figure, the sentence that introduces a formula ("…we obtain:"), the formula a "where …"
    // continues. Only when the chunk holds nothing else are they parted.
    if (forced && together(i)) {
      let k = i;
      while (k - 1 > start && together(k)) k--;
      let run = 0;
      for (let j = k; j <= i; j++) run += tokens[j]! + (j > k ? 1 : 0);
      // ...when the run fits a chunk (or is a caption with the figure it names, which is cut
      // inside or stands alone anyway) and what stays behind is more than headings.
      const fits = run <= settings.maxTokens || seq.slice(k, i).every((b, m) => b.type === 'caption' || lead[k + m]);
      if (!together(k) && fits && !allLead(start, k)) cut = k;
      else if (k - 1 === start && block.type === 'formula') {
        // The whole chunk is one run of formulas and their text: it ends after the last formula
        // before this one, so the sentence that introduces this formula goes on with it.
        let j = i - 1;
        while (j > start + 1 && seq[j - 1]!.type !== 'formula') j--;
        let rest = 0;
        for (let m = j; m <= i; m++) rest += tokens[m]! + (m > j ? 1 : 0);
        if (j > start + 1 && seq[j - 1]!.type === 'formula' && seq[j]!.type !== 'formula' && rest <= settings.maxTokens) cut = j;
      }
    }
    if (settings.keepHeadingWithBody) {
      while (cut - 1 > start && lead[cut - 1]) cut--;
    }
    cuts.push(cut - 1);
    start = cut;
    current = 0;
    for (let j = cut; j <= i; j++) current += tokens[j]! + (j > cut ? 1 : 0);
  }

  mergeSmallChunks(seq, tokens, lead, cuts, settings, notesFrom);

  const boundaries: Boundary[] = [];
  let from = 0;
  for (const end of [...cuts, seq.length - 1]) {
    // A block larger than the maximum is cut inside, also when a caption or heading goes with it;
    // so is the one block of a chunk that only its headings and captions (before or after it) push
    // past the maximum, or the last one of a chunk of only those.
    let total = 0;
    for (let j = from; j <= end; j++) total += tokens[j]! + (j > from ? 1 : 0);
    const body: number[] = [];
    for (let j = from; j <= end; j++) if (!lead[j] && seq[j]!.type !== 'caption') body.push(j);
    const inside = body.length === 1 ? body[0]! : body.length === 0 ? end : -1;
    const gluedOver = total > settings.maxTokens && end > from && inside >= 0 && canSplitInside(seq[inside]!);
    for (let j = from; j <= end; j++) {
      if (tokens[j]! > settings.maxTokens || (gluedOver && j === inside)) {
        // Its first piece shares the chunk with the blocks before it.
        let before = 0;
        for (let k = from; k < j; k++) before += tokens[k]! + 1;
        boundaries.push(...splitLongBlock(seq[j]!, settings, countTokens, before));
      }
    }
    if (end < seq.length - 1) boundaries.push({ blockId: seq[end]!.id });
    from = end + 1;
  }
  return boundaries;
}

/**
 * Blocks that go with what follows them: headings, and the pictures with nothing to read that open
 * the page of a heading (a slide's logo set before its title).
 */
function leadingBlocks(seq: readonly DocumentBlock[]): boolean[] {
  const lead = seq.map(isHeading);
  for (let h = 0; h < seq.length; h++) {
    if (!isHeading(seq[h]!)) continue;
    let k = h;
    while (k > 0 && seq[k - 1]!.type === 'picture' && !seq[k - 1]!.text.trim() && firstPage(seq[k - 1]!) === firstPage(seq[h]!)) k--;
    if (k < h && (k === 0 || lastPage(seq[k - 1]!) < firstPage(seq[k]!))) lead.fill(true, k, h);
  }
  return lead;
}

/**
 * A chunk of a few tokens left between two full ones (a list's first item, a lone "*") joins a
 * neighbour with room for it: the chunk before, or the one after when it opens with a heading (or
 * the chunk before has no room). A chunk that opens with a heading never joins the one before;
 * nor does any join a chunk that opens with a heading. A neighbour that is one block too large
 * for any chunk is cut inside anyway, so it has room.
 */
function mergeSmallChunks(
  seq: readonly DocumentBlock[],
  tokens: readonly number[],
  lead: readonly boolean[],
  cuts: number[],
  settings: ChunkSettings,
  notesFrom: number,
): void {
  const small = Math.floor(settings.targetTokens * 0.12);
  const size = (a: number, b: number) => {
    let t = 0;
    for (let j = a; j <= b; j++) t += tokens[j]! + (j > a ? 1 : 0);
    return t;
  };
  const opensWithHeading = (a: number) => settings.structureAware && lead[a]!;
  const room = (a: number, b: number, extra: number) =>
    size(a, b) + 1 + extra <= settings.maxTokens || (a === b && tokens[a]! > settings.maxTokens && canSplitInside(seq[a]!));
  for (let c = 0; c <= cuts.length && cuts.length > 0; c++) {
    const a = c === 0 ? 0 : cuts[c - 1]! + 1;
    const b = c < cuts.length ? cuts[c]! : seq.length - 1;
    const t = size(a, b);
    if (t >= small) continue;
    const prev = c > 0 ? [c === 1 ? 0 : cuts[c - 2]! + 1, cuts[c - 1]!] as const : null;
    const next = c < cuts.length ? [b + 1, c + 1 < cuts.length ? cuts[c + 1]! : seq.length - 1] as const : null;
    // A chunk with nothing to read (pictures without text) adds nothing to a neighbour: it goes
    // with the next one when it leads it, else with the one before.
    const empty = seq.slice(a, b + 1).every((x) => !blockMarkdown(x).trim());
    // The text and the notes at the foot of its pages never share a chunk.
    const intoNext = next !== null && next[0] !== notesFrom
      && (empty ? lead[a]! || !prev : !opensWithHeading(next[0]) && room(next[0], next[1], t));
    if (prev && a !== notesFrom && (empty ? !intoNext : !opensWithHeading(a) && room(prev[0], prev[1], t))) {
      cuts.splice(c - 1, 1);
      c -= 2;
    } else if (intoNext) {
      cuts.splice(c, 1);
      c -= 1;
    }
  }
}

/**
 * Where a sentence longer than the maximum may still be cut (a patent claim or a Korean patent
 * paragraph is a single sentence): after a comma, semicolon or colon, and failing those, between
 * words.
 */
function insideSentence(text: string, from: number, to: number, limit: number, countTokens: TokenCounter): number[] {
  if (countTokens(text.slice(from, to)) <= limit) return [];
  // After a comma, semicolon or colon (full-width marks of Chinese and Japanese are not followed
  // by a space); a stretch between those still too long is cut between words.
  const at = (pattern: RegExp, a: number, b: number) =>
    [...text.slice(a, b).matchAll(pattern)].map((m) => a + m.index + m[0].length).filter((p) => p > a && p < b);
  const marks = at(/[,;:]\s+|[，；：、]/g, from, to);
  const edges = [from, ...marks, to];
  const cuts = [...marks];
  for (let k = 0; k + 1 < edges.length; k++) {
    if (countTokens(text.slice(edges[k]!, edges[k + 1]!)) > limit) cuts.push(...at(/\s+/g, edges[k]!, edges[k + 1]!));
  }
  return cuts.sort((a, b) => a - b);
}

/** Pack a long block's sentences into pieces of at most the target size. */
function splitLongBlock(block: DocumentBlock, settings: ChunkSettings, countTokens: TokenCounter, before = 0): Boundary[] {
  if (!canSplitInside(block)) return [];
  const sentences = [0, ...splitPoints(block), block.text.length];
  const edges =
    block.type === 'table'
      ? sentences
      : sentences.flatMap((start, k) =>
          // The first sentence shares its chunk with what comes before the block.
          k + 1 < sentences.length
            ? [start, ...insideSentence(block.text, start, sentences[k + 1]!, settings.maxTokens - (k === 0 ? before : 0), countTokens)]
            : [start],
        );
  const out: Boundary[] = [];
  // Later pieces of a table repeat its header.
  const header = block.type === 'table' && repeatedHeader(block.text) ? countTokens(repeatedHeader(block.text)) + 1 : 0;
  let pieceStart = 0;
  let limit = settings.targetTokens - before;
  for (let k = 1; k < edges.length - 1; k++) {
    const next = edges[k + 1]!;
    if (countTokens(block.text.slice(pieceStart, next)) > limit) {
      out.push({ blockId: block.id, offset: edges[k]! });
      pieceStart = edges[k]!;
      limit = settings.targetTokens - header;
    }
  }
  return out;
}

export function createSplitPlan(
  documentId: string,
  blocksFingerprint: string,
  blocks: readonly DocumentBlock[],
  settings: ChunkSettings,
  countTokens: TokenCounter,
): SplitPlan {
  return {
    documentId,
    blocksFingerprint,
    revision: 0,
    boundaries: autoSplit(blocks, settings, countTokens),
    settings: { ...settings },
  };
}

/* Edits --------------------------------------------------------------------------------------- */
/*
 * Edit operations are pure. Each returns the same plan object when the edit is not allowed
 * or changes nothing, so callers can skip history entries with a reference check.
 * `revision` is the server's save counter and is left for the caller to manage.
 */

function withBoundaries(plan: SplitPlan, boundaries: Boundary[]): SplitPlan {
  return { ...plan, boundaries };
}

export function addBoundary(plan: SplitPlan, blocks: readonly DocumentBlock[], at: Boundary): SplitPlan {
  const seq = chunkableBlocks(blocks, plan.settings);
  const current = normalizeBoundaries(seq, plan.boundaries);
  const [added] = normalizeBoundaries(seq, [at]);
  if (!added || !sameBoundary(added, at) || current.some((b) => sameBoundary(b, added))) return plan;
  return withBoundaries(plan, normalizeBoundaries(seq, [...current, added]));
}

export function removeBoundary(plan: SplitPlan, at: Boundary): SplitPlan {
  if (!plan.boundaries.some((b) => sameBoundary(b, at))) return plan;
  return withBoundaries(
    plan,
    plan.boundaries.filter((b) => !sameBoundary(b, at)),
  );
}

export interface MoveRange {
  seq: DocumentBlock[];
  /** Whether a boundary may move to `to` without emptying a neighbouring chunk. */
  allows: (to: Boundary) => boolean;
}

/** The positions a boundary may move to: strictly between its neighbouring boundaries. */
export function boundaryMoveRange(plan: SplitPlan, blocks: readonly DocumentBlock[], from: Boundary): MoveRange | null {
  const seq = chunkableBlocks(blocks, plan.settings);
  const index = indexMap(seq);
  const positions = normalizeBoundaries(seq, plan.boundaries).map((b) => toPos(seq, index, b)!);
  const fromPos = toPos(seq, index, from);
  const at = fromPos ? positions.findIndex((p) => cmp(p, fromPos) === 0) : -1;
  if (at < 0) return null;
  const lower: Pos = at > 0 ? positions[at - 1]! : [-1, Infinity];
  const upper: Pos = at < positions.length - 1 ? positions[at + 1]! : [seq.length - 1, Infinity];
  return {
    seq,
    allows: (to) => {
      const p = toPos(seq, index, to);
      return !!p && cmp(lower, p) < 0 && cmp(p, upper) < 0;
    },
  };
}

export function moveBoundary(plan: SplitPlan, blocks: readonly DocumentBlock[], from: Boundary, to: Boundary): SplitPlan {
  if (sameBoundary(from, to)) return plan;
  const range = boundaryMoveRange(plan, blocks, from);
  if (!range || !range.allows(to)) return plan;
  const [target] = normalizeBoundaries(range.seq, [to]);
  if (!target) return plan;
  return withBoundaries(
    plan,
    normalizeBoundaries(
      range.seq,
      plan.boundaries.map((b) => (sameBoundary(b, from) ? target : b)),
    ),
  );
}

/** Merge chunk `chunkIndex` with the chunk after it. */
export function mergeChunks(plan: SplitPlan, blocks: readonly DocumentBlock[], chunkIndex: number): SplitPlan {
  const seq = chunkableBlocks(blocks, plan.settings);
  const boundary = normalizeBoundaries(seq, plan.boundaries)[chunkIndex];
  return boundary ? removeBoundary(withBoundaries(plan, normalizeBoundaries(seq, plan.boundaries)), boundary) : plan;
}

/** Split the chunk containing the position: after a whole block, or before a character. */
export function splitChunk(plan: SplitPlan, blocks: readonly DocumentBlock[], at: Boundary): SplitPlan {
  return addBoundary(plan, blocks, at);
}

/* Deleted text ---------------------------------------------------------------------------------- */

type Mask = Map<string, [number, number][]>;

/** Ranges per block, merged and in order; unknown blocks and empty ranges of blocks with text dropped. */
function maskOf(blocks: readonly DocumentBlock[], ranges: readonly Segment[] | undefined): Mask {
  const texts = new Map(blocks.map((b) => [b.id, b.text]));
  const mask: Mask = new Map();
  for (const r of ranges ?? []) {
    const text = texts.get(r.blockId);
    if (text === undefined) continue;
    const n = text.length;
    const start = Math.max(0, Math.min(r.start, n));
    const end = Math.max(start, Math.min(r.end, n));
    if (!isTextBoundary(text, start) || !isTextBoundary(text, end)) continue;
    if (end === start && n > 0) continue;
    mask.set(r.blockId, [...(mask.get(r.blockId) ?? []), [start, end]]);
  }
  for (const [id, list] of mask) {
    list.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const merged: [number, number][] = [];
    for (const [a, b] of list) {
      const last = merged.at(-1);
      if (last && a <= last[1]) last[1] = Math.max(last[1], b);
      else merged.push([a, b]);
    }
    mask.set(id, merged);
  }
  return mask;
}

/** Excluded ranges in reading order, as stored in a plan. */
function maskRanges(blocks: readonly DocumentBlock[], mask: Mask): Segment[] {
  return blocks.flatMap((b) => (mask.get(b.id) ?? []).map(([start, end]) => ({ blockId: b.id, start, end })));
}

/** The parts of a segment outside the mask, and the parts inside it. */
function splitByMask(seg: Segment, mask: Mask): { kept: Segment[]; masked: Segment[] } {
  const list = mask.get(seg.blockId);
  if (!list) return { kept: [seg], masked: [] };
  if (seg.start === seg.end) {
    const hit = list.some(([a, b]) => a <= seg.start && seg.end <= b);
    return hit ? { kept: [], masked: [seg] } : { kept: [seg], masked: [] };
  }
  const kept: Segment[] = [];
  const masked: Segment[] = [];
  let at = seg.start;
  for (const [a, b] of list) {
    if (b <= at || a >= seg.end) continue;
    if (a > at) kept.push({ blockId: seg.blockId, start: at, end: a });
    masked.push({ blockId: seg.blockId, start: Math.max(a, at), end: Math.min(b, seg.end) });
    at = Math.min(b, seg.end);
  }
  if (at < seg.end) kept.push({ blockId: seg.blockId, start: at, end: seg.end });
  return { kept, masked };
}

function sameRanges(a: readonly Segment[], b: readonly Segment[]): boolean {
  return a.length === b.length && a.every((x, i) => x.blockId === b[i]!.blockId && x.start === b[i]!.start && x.end === b[i]!.end);
}

function withExcluded(plan: SplitPlan, blocks: readonly DocumentBlock[], ranges: Segment[]): SplitPlan {
  const before = maskRanges(blocks, maskOf(blocks, plan.excluded));
  const after = maskRanges(blocks, maskOf(blocks, ranges));
  return sameRanges(before, after) ? plan : { ...plan, excluded: after };
}

/** Delete chunk `chunkIndex`: its text is left out of every chunk and export until restored. */
export function deleteChunk(plan: SplitPlan, blocks: readonly DocumentBlock[], chunkIndex: number): SplitPlan {
  const segments = chunkSegments(blocks, plan)[chunkIndex];
  return segments ? withExcluded(plan, blocks, [...(plan.excluded ?? []), ...segments]) : plan;
}

/** Bring back the excluded text of chunk `chunkIndex`. */
export function restoreChunk(plan: SplitPlan, blocks: readonly DocumentBlock[], chunkIndex: number): SplitPlan {
  const segments = chunkSegments(blocks, plan)[chunkIndex];
  if (!segments) return plan;
  const span = maskOf(blocks, segments);
  const left = maskRanges(blocks, maskOf(blocks, plan.excluded)).flatMap((r) => splitByMask(r, span).kept);
  return withExcluded(plan, blocks, left);
}

export function resetSplitPlan(
  plan: SplitPlan,
  blocks: readonly DocumentBlock[],
  countTokens: TokenCounter,
  settings: ChunkSettings = plan.settings,
): SplitPlan {
  return { ...plan, settings: { ...settings }, boundaries: autoSplit(blocks, settings, countTokens) };
}

/* Chunks -------------------------------------------------------------------------------------- */

/** Pages covered by part of a block; uses line positions for partial segments when known. */
function segmentPages(block: DocumentBlock, start: number, end: number): number[] {
  const whole = start <= 0 && end >= block.text.length;
  if (!whole && block.lines?.length) {
    return block.lines.filter((l) => l.end > start && l.start < end).map((l) => l.pageIndex);
  }
  return block.regions.map((r) => r.pageIndex);
}

/** Segments of the chunkable sequence between consecutive boundaries. */
export function chunkSegments(blocks: readonly DocumentBlock[], plan: SplitPlan): Segment[][] {
  const seq = chunkableBlocks(blocks, plan.settings);
  const cuts = normalizeBoundaries(seq, plan.boundaries);
  const inner = new Map<string, number[]>();
  const after = new Set<string>();
  for (const c of cuts) {
    if (c.offset === undefined) after.add(c.blockId);
    else inner.set(c.blockId, [...(inner.get(c.blockId) ?? []), c.offset]);
  }
  const groups: Segment[][] = [];
  let current: Segment[] = [];
  for (const b of seq) {
    let start = 0;
    for (const offset of inner.get(b.id) ?? []) {
      current.push({ blockId: b.id, start, end: offset });
      groups.push(current);
      current = [];
      start = offset;
    }
    current.push({ blockId: b.id, start, end: b.text.length });
    if (after.has(b.id)) {
      groups.push(current);
      current = [];
    }
  }
  if (current.length) groups.push(current);
  return groups;
}

export function generateChunks(
  blocks: readonly DocumentBlock[],
  plan: SplitPlan,
  countTokens: TokenCounter,
): Chunk[] {
  const byId = new Map(blocks.map((b) => [b.id, b]));
  const mask = maskOf(blocks, plan.excluded);
  return chunkSegments(blocks, plan).map((segments, index) => {
    const parts = segments.map((s) => splitByMask(s, mask));
    const kept = parts.flatMap((p) => p.kept);
    const deleted = kept.length === 0;
    const text = kept
      .map((s) => segmentText(byId.get(s.blockId)!, s.start, s.end))
      .filter((t) => t.trim().length > 0)
      .join(BLOCK_SEPARATOR);
    const tokenCount = countTokens(text);
    // A deleted chunk still says where it is, so it can be found and restored.
    const pages = (deleted ? segments : kept).flatMap((s) => segmentPages(byId.get(s.blockId)!, s.start, s.end));
    const first = segments[0]!;
    return {
      id: first.start > 0 ? `chunk_${first.blockId}@${first.start}` : `chunk_${first.blockId}`,
      index,
      segments,
      blockIds: [...new Set(segments.map((s) => s.blockId))],
      text,
      tokenCount,
      pageRange: pages.length ? [Math.min(...pages) + 1, Math.max(...pages) + 1] : null,
      overTarget: tokenCount > plan.settings.targetTokens,
      overMax: tokenCount > plan.settings.maxTokens,
      deleted,
      excluded: parts.flatMap((p) => p.masked),
    };
  });
}

/** The parts of a chunk that are not excluded. */
export function keptSegments(chunk: Chunk): Segment[] {
  if (!chunk.excluded.length) return chunk.segments;
  const mask: Mask = new Map();
  for (const r of chunk.excluded) mask.set(r.blockId, [...(mask.get(r.blockId) ?? []), [r.start, r.end]]);
  return chunk.segments.flatMap((s) => splitByMask(s, mask).kept);
}

/** Excluded ranges of a plan, cleaned for this parse (unknown blocks and out-of-range offsets dropped). */
export function normalizeExcluded(blocks: readonly DocumentBlock[], ranges: readonly Segment[] | undefined): Segment[] {
  return maskRanges(blocks, maskOf(blocks, ranges));
}

/** Problems that would make a plan unsafe to export. Empty when the plan is consistent. */
export function validatePlan(blocks: readonly DocumentBlock[], plan: SplitPlan): string[] {
  const seq = chunkableBlocks(blocks, plan.settings);
  const index = indexMap(seq);
  const issues: string[] = [];
  let last: Pos | null = null;
  for (const b of plan.boundaries) {
    const label = b.offset === undefined ? b.blockId : `${b.blockId}@${b.offset}`;
    const i = index.get(b.blockId);
    if (i === undefined) {
      issues.push(`unknown block ${b.blockId}`);
      continue;
    }
    const block = seq[i]!;
    if (b.offset !== undefined && (b.offset <= 0 || b.offset >= block.text.length || !isTextBoundary(block.text, b.offset) || !canSplitInside(block)
        || (block.type === 'table' && !tableRowStarts(block.text).includes(b.offset)))) {
      issues.push(`invalid offset ${label}`);
      continue;
    }
    if (b.offset === undefined && i === seq.length - 1) {
      issues.push(`boundary after the last block ${label}`);
      continue;
    }
    const pos = toPos(seq, index, b)!;
    if (last && cmp(pos, last) === 0) issues.push(`duplicate boundary ${label}`);
    else if (last && cmp(pos, last) < 0) issues.push(`boundary ${label} out of reading order`);
    last = pos;
  }
  return issues;
}

/** Wrap a counter with a bounded cache; chunk texts repeat across edits. */
export function memoizeCounter(count: TokenCounter, maxEntries = 5000): TokenCounter {
  const cache = new Map<string, number>();
  return (text) => {
    const hit = cache.get(text);
    if (hit !== undefined) return hit;
    const n = count(text);
    if (cache.size >= maxEntries) cache.delete(cache.keys().next().value!);
    cache.set(text, n);
    return n;
  };
}
