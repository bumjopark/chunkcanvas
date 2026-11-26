import type { DocumentBlock, Segment } from './types';

export interface HeadingPathEntry {
  blockId: string;
  text: string;
  level: number;
}

export interface ExportedSection extends HeadingPathEntry {
  parentBlockId: string | null;
  /** Zero-based page index, or null when the heading has no location. */
  pageIndex: number | null;
}

const isHeading = (b: DocumentBlock) => b.type === 'title' || b.type === 'section_header';
const isFurniture = (b: DocumentBlock) => b.type === 'page_header' || b.type === 'page_footer';

/**
 * Read the parser's levels, without another numbering/font heuristic. This uses
 * the original document order: filtering/deleting headings and moving footnotes
 * in the editor must not change which section the remaining body belongs to.
 */
export function documentSections(blocks: readonly DocumentBlock[]) {
  const sections: ExportedSection[] = [];
  const byId = new Map(blocks.map((b) => [b.id, b]));
  if (byId.size !== blocks.length) throw new Error('Document block IDs must be unique.');
  const owner = new Map<string, ExportedSection | undefined>();
  const bySectionId = new Map<string, ExportedSection>();
  const sectionOrder = new Map<string, number>();
  const stack: ExportedSection[] = [];
  for (const b of [...blocks].sort((a, z) => a.readingOrder - z.readingOrder)) {
    if (isHeading(b)) {
      // Legacy parses can have missing levels. Preserve any valid level; a
      // legacy gap is not grounds for silently inventing a different parent.
      const level = b.type === 'title' ? 0 : Number.isInteger(b.level) && b.level! > 0 ? b.level! : 1;
      while (stack.length && stack[stack.length - 1]!.level >= level) stack.pop();
      const s: ExportedSection = {
        blockId: b.id, text: b.text, level,
        parentBlockId: stack[stack.length - 1]?.blockId ?? null,
        pageIndex: b.regions[0]?.pageIndex ?? null,
      };
      sectionOrder.set(b.id, sections.length);
      bySectionId.set(b.id, s);
      sections.push(s);
      stack.push(s);
    }
    owner.set(b.id, stack[stack.length - 1]);
  }

  function path(s: ExportedSection | undefined): HeadingPathEntry[] {
    const entries: HeadingPathEntry[] = [];
    while (s) {
      entries.push({ blockId: s.blockId, text: s.text, level: s.level });
      s = s.parentBlockId === null ? undefined : bySectionId.get(s.parentBlockId);
    }
    return entries.reverse();
  }

  function forSegments(segments: readonly Segment[]): { headingPath: HeadingPathEntry[]; sectionIds: string[] } {
    const bodySections = new Set<string>();
    let firstBody: DocumentBlock | undefined;
    let lastHeading: DocumentBlock | undefined;
    for (const segment of segments) {
      const b = byId.get(segment.blockId)!;
      if (isHeading(b)) lastHeading = b;
      else if (!isFurniture(b)) {
        firstBody ??= b;
        const s = owner.get(b.id);
        if (s) bodySections.add(s.blockId);
      }
    }
    // Heading-only chunks use their last heading. Furniture alone carries no
    // document section: a running header is not that section's body.
    const anchor = firstBody ?? lastHeading;
    return {
      headingPath: path(anchor ? owner.get(anchor.id) : undefined),
      sectionIds: [...bodySections].sort((a, b) => sectionOrder.get(a)! - sectionOrder.get(b)!),
    };
  }
  return { sections, forSegments };
}
