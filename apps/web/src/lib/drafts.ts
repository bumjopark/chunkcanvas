import { normalizeChunkSettings, normalizeSavedPlan, upgradeSplitPlan, type ParsedDocument, type SplitPlan } from '@chunkcanvas/core';

const PREFIX = 'chunkcanvas.draft.';
const BACKUP = 'chunkcanvas.plan-backup.';
// getRandomValues is also available on ordinary HTTP origins. This ID only owns a
// local recovery record; it is not an access token or an authentication credential.
const newWriter = () => {
  try {
    return Array.from(crypto.getRandomValues(new Uint32Array(4)), n => n.toString(16).padStart(8, '0')).join('');
  } catch {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }
};
// A duplicated tab can inherit sessionStorage. Each loaded editor needs its own
// writer; after a reload the normal recovery transfer adopts the previous copy.
const writer = newWriter();
interface Draft {
  plan: SplitPlan;
  baseRevision: number;
  expiresAt: string;
  updatedAt: number;
}
function signature(plan: SplitPlan): string | null {
  const settings = normalizeChunkSettings(plan.settings);
  if (!settings || typeof plan.documentId !== 'string' || typeof plan.blocksFingerprint !== 'string'
      || !Number.isInteger(plan.revision) || plan.revision < 0
      || (plan.excluded !== undefined && (!Array.isArray(plan.excluded) || !plan.excluded.every(s => s
        && typeof s.blockId === 'string' && Number.isInteger(s.start) && Number.isInteger(s.end)
        && s.start >= 0 && s.end >= s.start)))) return null;
  // Property insertion order, absent empty deletions, and server revision are not edits.
  return JSON.stringify([plan.documentId, plan.blocksFingerprint,
    [settings.targetTokens, settings.maxTokens, settings.overlapTokens, settings.structureAware,
      settings.keepHeadingWithBody, settings.excludeFurniture],
    plan.boundaries.map(b => [b.blockId, b.offset ?? null]),
    (plan.excluded ?? []).map(s => [s.blockId, s.start, s.end])]);
}
export function samePlanContent(a: SplitPlan, b: SplitPlan): boolean {
  const left = signature(a);
  return left !== null && left === signature(b);
}
const keyFor = (plan: SplitPlan) => `${PREFIX}${plan.documentId}.${plan.blocksFingerprint}.${writer}`;
const ownedKey = (key: string, plan: SplitPlan) => key === keyFor(plan) || key.startsWith(`${keyFor(plan)}.`);
interface DraftSnapshot extends Draft { storageKey: string; storedValue: string }

function entries(prefix: string): [string, Draft, string][] {
  const found: [string, Draft, string][] = [];
  try {
    for (const key of Object.keys(localStorage).filter(k => k.startsWith(prefix))) {
      try {
        const storedValue = localStorage.getItem(key)!;
        const v = JSON.parse(storedValue);
        const plan = v && upgradeSplitPlan(v.plan);
        if (!plan || signature(plan) === null || !key.startsWith(`${prefix.startsWith(PREFIX) ? PREFIX : BACKUP}${plan.documentId}.${plan.blocksFingerprint}.`)
            || !Number.isFinite(Date.parse(v.expiresAt)) || Date.parse(v.expiresAt) <= Date.now()
            || !Number.isInteger(v.baseRevision) || v.baseRevision < -1 || !Number.isFinite(v.updatedAt)) {
          localStorage.removeItem(key);
        } else found.push([key, { ...v, plan }, storedValue]);
      } catch { localStorage.removeItem(key); }
    }
  } catch { /* unavailable storage must not prevent editing */ }
  return found;
}

export function readDraft(documentId: string, fingerprint: string, parsed?: ParsedDocument): DraftSnapshot | null {
  entries(PREFIX); // Remove expired drafts, including documents no longer listed in this browser.
  const candidates = entries(`${PREFIX}${documentId}.${fingerprint}.`)
    .filter(([, value]) => !parsed || normalizeSavedPlan(parsed, value.plan))
    .sort((a,b) => b[1].updatedAt-a[1].updatedAt);
  const chosen = candidates.find(([key, value]) => ownedKey(key, value.plan)) ?? candidates[0];
  if (!chosen) return null;
  return { ...chosen[1], storageKey: chosen[0], storedValue: chosen[2] };
}
/** Transfer the selected recovery copy only after this tab has durably stored it.
 * Later edits now replace that copy instead of leaving an abandoned ancestor in another tab.
 * An independently updated copy must survive the transfer. */
export function adoptDraft(draft: DraftSnapshot): boolean {
  if (ownedKey(draft.storageKey, draft.plan)) return true;
  if (!writeDraft(draft.plan, draft.baseRevision, draft.expiresAt)) return false;
  try {
    if (localStorage.getItem(draft.storageKey) === draft.storedValue) localStorage.removeItem(draft.storageKey);
  } catch { /* keep the source if it could not be checked */ }
  return true;
}
export function writeDraft(plan: SplitPlan, baseRevision: number, expiresAt: string): boolean {
  if (Date.parse(expiresAt) <= Date.now()) { clearDocumentDrafts(plan.documentId); return false; }
  try {
    const previous = Object.keys(localStorage).filter(key => ownedKey(key, plan));
    const value: Draft = {plan, baseRevision, expiresAt, updatedAt:Date.now()};
    // Keys are immutable snapshots. Another tab acknowledging an older snapshot
    // cannot delete an edit written between its inspection and removeItem call.
    localStorage.setItem(`${keyFor(plan)}.${newWriter()}`, JSON.stringify(value));
    for (const key of previous) localStorage.removeItem(key);
    return true;
  } catch { return false; }
}
/** Clear only acknowledged content; edits made in another tab survive this acknowledgement. */
export function acknowledgeDraft(plan: SplitPlan, revision: number): void {
  try {
    for (const [key, value, storedValue] of entries(`${PREFIX}${plan.documentId}.${plan.blocksFingerprint}.`)) {
      if (value.baseRevision <= revision && samePlanContent(value.plan, plan)
          && localStorage.getItem(key) === storedValue) localStorage.removeItem(key);
    }
  } catch { /* ignore */ }
}
export function clearDocumentDrafts(id: string): void {
  try {
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith(`${PREFIX}${id}.`) || key.startsWith(`${BACKUP}${id}.`)) localStorage.removeItem(key);
    }
  } catch { /* ignore */ }
}
/** Keep the original server plan until document expiry, even after saving its normalized form. */
export function backupOriginalPlan(plan: SplitPlan, expiresAt: string): void {
  try {
    const key = `${BACKUP}${plan.documentId}.${plan.blocksFingerprint}.${plan.revision}`;
    if (!localStorage.getItem(key)) localStorage.setItem(key, JSON.stringify({plan, expiresAt, baseRevision:plan.revision, updatedAt:Date.now()}));
  } catch { /* the live editor also keeps a downloadable copy */ }
}
export function originalPlan(id: string, fingerprint: string): SplitPlan | null {
  entries(BACKUP);
  return entries(`${BACKUP}${id}.${fingerprint}.`).sort((a,b) => b[1].updatedAt-a[1].updatedAt)[0]?.[1].plan ?? null;
}
