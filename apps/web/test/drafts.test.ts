import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_SETTINGS, type SplitPlan } from '@chunkcanvas/core';

class MemoryStorage {
  getItem(key: string): string | null { return Object.hasOwn(this, key) ? (this as any)[key] : null; }
  setItem(key: string, value: string) {
    Object.defineProperty(this, key, { value: String(value), configurable: true, writable: true, enumerable: true });
  }
  removeItem(key: string) { delete (this as any)[key]; }
}
let storage: MemoryStorage;
const expires = () => new Date(Date.now() + 3600000).toISOString();
const plan = (): SplitPlan => ({ documentId: 'document', blocksFingerprint: 'fingerprint',
  settings: { ...DEFAULT_SETTINGS }, boundaries: [{ blockId: 'first' }], revision: 0 });
const normalized = (p: SplitPlan): SplitPlan => ({ revision: 9, excluded: [],
  boundaries: p.boundaries.map(b => ({ offset: b.offset, blockId: b.blockId })),
  settings: Object.fromEntries(Object.entries(p.settings).reverse()) as SplitPlan['settings'],
  blocksFingerprint: p.blocksFingerprint, documentId: p.documentId });
async function tab() {
  vi.resetModules();
  vi.stubGlobal('sessionStorage', new MemoryStorage());
  return import('../src/lib/drafts');
}
const keys = () => Object.keys(storage).filter(k => k.startsWith('chunkcanvas.draft.'));
beforeEach(() => { storage = new MemoryStorage(); vi.stubGlobal('localStorage', storage); });
afterEach(() => { vi.unstubAllGlobals(); vi.resetModules(); });

describe('draft ownership and acknowledgement', () => {
  it('acknowledges equivalent server content despite object order, revision and absent empty deletions', async () => {
    const a = await tab(); a.writeDraft(plan(), 0, expires());
    const b = await tab(); b.acknowledgeDraft(normalized(plan()), 9);
    expect(keys()).toEqual([]);
    expect(b.readDraft('document', 'fingerprint')).toBeNull();
  });

  it('does not acknowledge another edit, a newer base revision or another document', async () => {
    const a = await tab(); a.writeDraft(plan(), 20, expires());
    const b = await tab(); b.writeDraft({ ...plan(), boundaries: [] }, 0, expires());
    const c = await tab(); c.writeDraft({ ...plan(), documentId: 'other' }, 0, expires());
    c.acknowledgeDraft(normalized(plan()), 9);
    expect(keys()).toHaveLength(3);
  });

  it('adopts a recovered draft so subsequent changes and saving cannot leave its ancestor behind', async () => {
    const a = await tab(); a.writeDraft(plan(), 0, expires());
    const b = await tab(); const recovered = b.readDraft('document', 'fingerprint')!;
    expect(b.adoptDraft(recovered)).toBe(true);
    expect(keys()).toHaveLength(1);
    const edited = { ...plan(), boundaries: [] };
    b.writeDraft(edited, 0, expires());
    b.acknowledgeDraft(normalized(edited), 9);
    const c = await tab();
    expect(c.readDraft('document', 'fingerprint')).toBeNull();
  });

  it('preserves an independently updated source draft during recovery transfer', async () => {
    const a = await tab(); a.writeDraft(plan(), 0, expires());
    const b = await tab(); const recovered = b.readDraft('document', 'fingerprint')!;
    a.writeDraft({ ...plan(), boundaries: [] }, 0, expires());
    expect(b.adoptDraft(recovered)).toBe(true);
    b.acknowledgeDraft(plan(), 1);
    expect(keys()).toHaveLength(1);
    expect(a.readDraft('document', 'fingerprint')!.plan.boundaries).toEqual([]);
  });

  it('keeps a concurrent edit written between acknowledgement inspection and removal', async () => {
    const a = await tab(); a.writeDraft(plan(), 0, expires());
    const b = await tab();
    const remove = storage.removeItem.bind(storage);
    vi.spyOn(storage, 'removeItem').mockImplementationOnce(key => {
      a.writeDraft({ ...plan(), boundaries: [] }, 0, expires());
      remove(key);
    });
    b.acknowledgeDraft(plan(), 1);
    expect(keys()).toHaveLength(1);
    expect(a.readDraft('document', 'fingerprint')!.plan.boundaries).toEqual([]);
  });

  it('returns the exact storage snapshot that supplied a recovery plan', async () => {
    const a = await tab(); a.writeDraft(plan(), 0, expires());
    const key = keys()[0];
    const b = await tab(); const get = storage.getItem.bind(storage);
    let reads = 0;
    vi.spyOn(storage, 'getItem').mockImplementation(k => {
      const value = get(k);
      if (k === key && ++reads === 2) a.writeDraft({ ...plan(), boundaries: [] }, 0, expires());
      return value;
    });
    const recovered = b.readDraft('document', 'fingerprint')!;
    expect(recovered).not.toBeNull();
    expect(JSON.parse(recovered.storedValue).plan).toEqual(recovered.plan);
    expect(b.adoptDraft(recovered)).toBe(true);
    b.acknowledgeDraft(plan(), 1);
    expect(a.readDraft('document', 'fingerprint')!.plan.boundaries).toEqual([]);
  });

  it('does not share draft ownership when a new tab inherits session storage', async () => {
    const a = await tab(); a.writeDraft(plan(), 0, expires());
    // Browsers can clone sessionStorage when a tab is opened or duplicated.
    vi.resetModules();
    const b = await import('../src/lib/drafts');
    b.writeDraft({ ...plan(), boundaries: [] }, 0, expires());
    expect(keys()).toHaveLength(2);
    expect(a.readDraft('document', 'fingerprint')!.plan.boundaries).toEqual(plan().boundaries);
    expect(b.readDraft('document', 'fingerprint')!.plan.boundaries).toEqual([]);
  });

  it('retains the recovery source if storing the adopted copy fails', async () => {
    const a = await tab(); a.writeDraft(plan(), 0, expires());
    const b = await tab(); const recovered = b.readDraft('document', 'fingerprint')!;
    vi.spyOn(storage, 'setItem').mockImplementationOnce(() => { throw new Error('quota'); });
    expect(b.adoptDraft(recovered)).toBe(false);
    expect(storage.getItem(recovered.storageKey)).toBe(recovered.storedValue);
  });

  it('rejects a mismatched or malformed newer draft without hiding a valid copy', async () => {
    const a = await tab(); a.writeDraft(plan(), 0, expires());
    for (const [key, bad] of [['wrong-id', { ...plan(), documentId: 'other' }], ['bad-settings', { ...plan(), settings: null }]]) {
      storage.setItem(`chunkcanvas.draft.document.fingerprint.${key}`, JSON.stringify({ plan: bad,
        baseRevision: 0, expiresAt: expires(), updatedAt: Date.now() + 10000 }));
    }
    const b = await tab();
    expect(b.readDraft('document', 'fingerprint')!.plan).toEqual(plan());
    expect(keys()).toHaveLength(1);
  });

  it('keeps the original normalized-plan backup independently of acknowledged drafts', async () => {
    const a = await tab(); a.backupOriginalPlan(plan(), expires()); a.writeDraft(plan(), 0, expires());
    a.acknowledgeDraft(normalized(plan()), 9);
    expect(a.readDraft('document', 'fingerprint')).toBeNull();
    expect(a.originalPlan('document', 'fingerprint')).toEqual(plan());
  });
});

it('loads on HTTP without randomUUID, including when session storage is unavailable', async () => {
  const getRandomValues = globalThis.crypto.getRandomValues.bind(globalThis.crypto);
  vi.stubGlobal('crypto', { getRandomValues });
  vi.resetModules();
  vi.stubGlobal('sessionStorage', { getItem() { throw new Error('disabled'); } });
  const drafts = await import('../src/lib/drafts');
  expect(drafts.writeDraft(plan(), 0, expires())).toBe(true);
  expect(drafts.readDraft('document', 'fingerprint')!.plan).toEqual(plan());
});
