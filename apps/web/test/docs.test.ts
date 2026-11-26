import { afterEach, expect, it, vi } from 'vitest';
import { docs } from '../src/lib/docs';

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

it('removes expired credentials on startup while retaining live documents', () => {
  const key = 'chunkcanvas.documents';
  const storage = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => storage.set(k, v),
  });
  vi.useFakeTimers();
  vi.setSystemTime(1000);
  const doc = { id: 'stale', filename: 'expired.pdf', token: 'expired-token', expiresAt: new Date(2000).toISOString() };
  const live = { ...doc, id: 'live', token: 'live-token', filename: 'live.pdf', expiresAt: new Date(4000).toISOString() };
  docs.save(doc);
  docs.save(live);
  vi.setSystemTime(2000);
  docs.prune();
  expect(JSON.parse(storage.get(key)!)).toEqual([live]);
  expect(docs.get('stale')).toBeUndefined();
  expect(docs.get('live')).toEqual(live);
});
