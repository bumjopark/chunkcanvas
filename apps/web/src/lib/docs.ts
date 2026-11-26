import { clearDocumentDrafts } from './drafts';
/** Access tokens for this browser's documents. They never leave the browser except as auth headers. */
export interface StoredDoc {
  id: string;
  token: string;
  filename: string;
  expiresAt: string;
}

const KEY = 'chunkcanvas.documents';

function read(): StoredDoc[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const list: unknown = JSON.parse(raw);
    if (!Array.isArray(list)) return [];
    const now = Date.now();
    const live = list.filter((d): d is StoredDoc => d && typeof d.id === 'string'
      && typeof d.token === 'string' && typeof d.filename === 'string' && Date.parse(d.expiresAt) > now);
    if (live.length !== list.length && localStorage.getItem(KEY) === raw) write(live);
    return live;
  } catch {
    return [];
  }
}

function write(list: StoredDoc[]): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(list));
  } catch {
    /* storage unavailable: the document still works for this tab */
  }
}

const memory = new Map<string, StoredDoc>();

export const docs = {
  prune: (): void => { read(); },
  list: (): StoredDoc[] => read().sort((a, b) => b.expiresAt.localeCompare(a.expiresAt)),
  get: (id: string): StoredDoc | undefined => {
    const stored = read().find((d) => d.id === id);
    if (stored) return stored;
    const cached = memory.get(id);
    if (cached && Date.parse(cached.expiresAt) > Date.now()) return cached;
    memory.delete(id);
    return undefined;
  },
  save: (doc: StoredDoc): void => {
    memory.set(doc.id, doc);
    write([doc, ...read().filter((d) => d.id !== doc.id)].slice(0, 20));
  },
  remove: (id: string): void => {
    clearDocumentDrafts(id);
    memory.delete(id);
    write(read().filter((d) => d.id !== id));
  },
};
