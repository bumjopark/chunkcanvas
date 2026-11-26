import { DEFAULT_SETTINGS, createSplitPlan, deleteChunk, normalizeChunkSettings, normalizeSavedPlan, upgradeSplitPlan, type ChunkSettings, type SplitPlan } from '@chunkcanvas/core';
import { useEffect, useState, type ReactNode } from 'react';
import { ApiError, api, type DocumentPayload, type ParserInfo } from '../lib/api';
import { acknowledgeDraft, adoptDraft, backupOriginalPlan, clearDocumentDrafts, originalPlan, readDraft, samePlanContent, writeDraft } from '../lib/drafts';
import { downloadText } from '../lib/download';
import { useT } from '../lib/i18n';
import { openPdf, type PdfHandle } from '../lib/pdf';
import { loadTokenizer, savedTokenizer } from '../lib/tokens';
import { chunkView, useEditor } from '../store';
import { Inspector } from './Inspector';
import { Navigator } from './Navigator';
import { PdfView } from './PdfView';
import { editFromStrategy } from './Strategies';
import { Toolbar } from './Toolbar';
import { Button, Spinner } from './ui';

function savedSettings(): ChunkSettings {
  try {
    const s = JSON.parse(localStorage.getItem('chunkcanvas.settings') ?? 'null') as ChunkSettings | null;
    const candidate = { ...DEFAULT_SETTINGS, ...s };
    const normalized = normalizeChunkSettings(candidate);
    if (normalized) return normalized;
  } catch {
    /* ignore */
  }
  return DEFAULT_SETTINGS;
}

export function Editor({
  payload,
  token,
  parsers,
  repoUrl,
  onGone,
  onDelete,
}: {
  payload: DocumentPayload;
  token: string;
  parsers: ParserInfo[];
  repoUrl: string | null;
  onGone: () => void;
  onDelete: () => void;
}) {
  const t = useT();
  const [pdf, setPdf] = useState<PdfHandle | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [original, setOriginal] = useState<SplitPlan | null>(null);
  const [draftFailed, setDraftFailed] = useState(false);
  const [recovered, setRecovered] = useState(false);
  const [normalizationPending, setNormalizationPending] = useState(false);
  const ready = useEditor((s) => s.doc?.id === payload.id && s.history !== null);
  const savedHere = useEditor((s) => s.doc?.id === payload.id && s.saveStatus === 'saved');
  useEffect(() => {
    if (savedHere) { setNormalizationPending(false); setRecovered(false); }
  }, [savedHere]);

  useEffect(() => {
    let cancelled = false;
    let loaded: PdfHandle | null = null;
    const controller = new AbortController();
    const parsed = payload.parsed!;
    Promise.all([loadTokenizer(savedTokenizer()), api.file(payload.id, token, controller.signal).then((data) => openPdf(data, controller.signal)).then((handle) => {
      loaded = handle;
      if (cancelled) void handle.destroy();
      return handle;
    })])
      .then(([tokenizer, handle]) => {
        if (cancelled) return;
        const saved = normalizeSavedPlan(parsed, payload.splitPlan);
        const rawSaved = upgradeSplitPlan(payload.splitPlan);
        if (saved?.changed && rawSaved) backupOriginalPlan(rawSaved, payload.expiresAt);
        setOriginal(saved?.changed ? rawSaved : originalPlan(payload.id, parsed.blocksFingerprint));
        setNormalizationPending(!!saved?.changed);
        let draft = readDraft(payload.id, parsed.blocksFingerprint, parsed);
        let restored = draft && normalizeSavedPlan(parsed, draft.plan);
        // A previous request can have reached the server even if its response was lost.
        if (draft && restored && saved && samePlanContent(restored.plan, saved.plan)) {
          acknowledgeDraft(draft.plan, payload.revision);
          draft = null;
          restored = null;
        }
        if (draft && restored) setDraftFailed(!adoptDraft(draft));
        const plan = restored?.plan ?? saved?.plan ?? createSplitPlan(payload.id, parsed.blocksFingerprint, parsed.blocks, savedSettings(), tokenizer.count);
        useEditor.getState().init(
          { id: payload.id, token, filename: payload.filename, parser: payload.parser, expiresAt: payload.expiresAt, parsed },
          plan, payload.revision, !!restored || !saved || saved.changed, tokenizer,
        );
        if (restored && draft) {
          setRecovered(true);
          if (draft.baseRevision !== payload.revision) useEditor.getState().setSave({
            saveStatus: 'conflict', conflict: {revision: payload.revision, plan: saved?.plan ?? null},
          });
        }
        setPdf(handle);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        cancelled = true;
        controller.abort();
        if (loaded) void loaded.destroy();
        if (e instanceof ApiError && e.status === 404) onGone();
        else setError((e as Error).message || 'Failed to load');
      });
    return () => {
      cancelled = true;
      controller.abort();
      if (loaded) void loaded.destroy();
      useEditor.getState().reset();
    };
  }, [payload.id, token]);

  useEffect(() => {
    const persist = () => {
      const st = useEditor.getState();
      if (st.doc?.id !== payload.id || !st.history) return;
      if (st.saveStatus === 'expired') clearDocumentDrafts(payload.id);
      else if (st.saveStatus !== 'saved') setDraftFailed(!writeDraft(st.history.present, st.conflict ? -1 : st.serverRevision, st.doc.expiresAt));
    };
    const unsubscribe = useEditor.subscribe((st, prev) => {
      if (st.history !== prev.history || st.saveStatus !== prev.saveStatus || st.serverRevision !== prev.serverRevision) persist();
    });
    const onUnload = (event: BeforeUnloadEvent) => {
      const st = useEditor.getState();
      if (st.doc?.id === payload.id && st.history && st.saveStatus !== 'saved') {
        persist();
        event.preventDefault();
        event.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', onUnload);
    return () => { unsubscribe(); window.removeEventListener('beforeunload', onUnload); };
  }, [payload.id]);
  useAutosave(payload.id, token);
  useShortcuts();

  if (error)
    return (
      <Centered>
        <p className="text-red-600">{error}</p>
      </Centered>
    );
  if (!pdf || !ready)
    return (
      <Centered>
        <Spinner className="text-accent-600" />
        <p className="text-zinc-500">{t('loadingEditor')}</p>
      </Centered>
    );

  return (
    <div className="flex h-full flex-col">
      <Toolbar parsers={parsers} onDelete={onDelete} />
      {(original || recovered || draftFailed) && <div data-testid="recovery-bar" className="flex shrink-0 items-center gap-3 border-b border-sky-200 bg-sky-50 px-4 py-2 text-[13px] text-sky-900 dark:border-sky-900 dark:bg-sky-950 dark:text-sky-200">
        <span className="flex-1">{draftFailed ? t('draftUnavailable') : normalizationPending ? t('normalizedPlan') : recovered ? t('draftRecovered') : t('originalPlanAvailable')}</span>
        {original && <Button onClick={() => downloadText('original-split-plan.json', JSON.stringify({ ...original, schema: 'chunkcanvas.split-plan', schemaVersion: 2, offsetEncoding: 'utf16', fileHash: payload.parsed!.document.fileHash }, null, 2), 'application/json')}>{t('downloadOriginalPlan')}</Button>}
      </div>}
      <ConflictBar />
      <ExpiredBar expiresAt={payload.expiresAt} />
      <DocumentNotes payload={payload} repoUrl={repoUrl} />
      {payload.sourcePageCount && payload.sourcePageCount > payload.pageCount ? (
        <div
          data-testid="truncated-bar"
          className="flex shrink-0 items-center gap-3 border-b border-sky-200 bg-sky-50 px-4 py-1.5 text-[13px] text-sky-900 dark:border-sky-900 dark:bg-sky-950/50 dark:text-sky-200"
        >
          <span className="flex-1">{t('truncatedBanner', { pages: payload.pageCount, total: payload.sourcePageCount })}</span>
          {repoUrl && (
            <a href={repoUrl} target="_blank" rel="noreferrer" className="font-medium underline underline-offset-2">
              {t('getCode')}
            </a>
          )}
        </div>
      ) : null}
      <div className="flex min-h-0 flex-1">
        <Panel side="left" storageKey="leftWidth" initial={300}>
          <Navigator />
        </Panel>
        <main className="flex min-w-0 flex-1 flex-col">
          <StrategyBar />
          <div className="min-h-0 flex-1">
            <PdfView handle={pdf} />
          </div>
        </main>
        <Panel side="right" storageKey="rightWidth" initial={400}>
          <Inspector />
        </Panel>
      </div>
      <Notice />
    </div>
  );
}

function Centered({ children }: { children: ReactNode }) {
  return <div className="flex h-full flex-col items-center justify-center gap-3 text-[13px]">{children}</div>;
}

/** Resizable side panel; width persists per browser. */
function Panel({ side, storageKey, initial, children }: { side: 'left' | 'right'; storageKey: string; initial: number; children: ReactNode }) {
  const [width, setWidth] = useState(() => {
    try {
      return Number(localStorage.getItem(`chunkcanvas.${storageKey}`)) || initial;
    } catch {
      return initial;
    }
  });
  const onDown = (e: React.PointerEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = width;
    let w = startW;
    const move = (ev: PointerEvent) => {
      w = Math.min(640, Math.max(220, startW + (side === 'left' ? ev.clientX - startX : startX - ev.clientX)));
      setWidth(w);
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      document.body.style.cursor = '';
      try {
        localStorage.setItem(`chunkcanvas.${storageKey}`, String(w));
      } catch {
        /* ignore */
      }
    };
    document.body.style.cursor = 'col-resize';
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  };
  const handle = (
    <div
      role="separator"
      aria-orientation="vertical"
      onPointerDown={onDown}
      className="w-1 shrink-0 cursor-col-resize bg-zinc-200 transition-colors hover:bg-accent-400 dark:bg-zinc-800 dark:hover:bg-accent-600"
    />
  );
  return (
    <>
      {side === 'right' && handle}
      <aside className="min-h-0 shrink-0 overflow-hidden bg-white dark:bg-zinc-950" style={{ width }}>
        {children}
      </aside>
      {side === 'left' && handle}
    </>
  );
}

function useAutosave(docId: string, token: string) {
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let inflight = false;
    const schedule = (delay = 600) => {
      if (disposed) return;
      clearTimeout(timer);
      timer = setTimeout(save, delay);
    };
    async function save() {
      if (disposed) return;
      const st = useEditor.getState();
      if (!st.doc || st.doc.id !== docId || !st.history) return;
      if (st.saveStatus === 'conflict' || st.saveStatus === 'saved' || st.saveStatus === 'expired') return;
      if (inflight) return schedule();
      const plan = st.history.present;
      inflight = true;
      st.setSave({ saveStatus: 'saving' });
      try {
        const { revision } = await api.savePlan(docId, token, st.serverRevision, { ...plan, revision: st.serverRevision });
        // The saved document still owns its recovery record after navigation. Only
        // matching content is cleared; newer edits and the next document remain intact.
        acknowledgeDraft(plan, revision);
        if (disposed || useEditor.getState().doc?.id !== docId) return;
        const now = useEditor.getState();
        const changed = now.history?.present !== plan;
        now.setSave({ serverRevision: revision, saveStatus: changed ? 'dirty' : 'saved' });
        if (changed) schedule();
      } catch (e) {
        if (disposed || useEditor.getState().doc?.id !== docId) return;
        const err = e as ApiError;
        if (err.code === 'stale_revision') {
          useEditor.getState().setSave({
            saveStatus: 'conflict',
            conflict: { revision: Number(err.detail.revision), plan: normalizeSavedPlan(st.doc.parsed, err.detail.splitPlan)?.plan ?? null },
          });
        } else if (err.status === 404) {
          // Expired on the server: keep the editor open so the work can still be exported.
          useEditor.getState().setSave({ saveStatus: 'expired' });
        } else {
          useEditor.getState().setSave({ saveStatus: 'error' });
          if (err.status === 0 || err.status === 408 || err.status === 429 || err.status >= 500) schedule(4000);
          else useEditor.getState().notify(err.message);
        }
      } finally {
        inflight = false;
      }
    }
    const unsub = useEditor.subscribe((s, prev) => {
      if (s.saveStatus === 'dirty' && (prev.saveStatus !== 'dirty' || s.history !== prev.history)) schedule();
    });
    if (useEditor.getState().saveStatus === 'dirty') schedule(0);
    return () => {
      disposed = true;
      unsub();
      clearTimeout(timer);
    };
  }, [docId, token]);
}

function useShortcuts() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (target.closest('input, textarea, select, [contenteditable="true"]')) return;
      const st = useEditor.getState();
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key.toLowerCase() === 'z') {
        e.preventDefault();
        if (e.shiftKey) st.redo();
        else st.undo();
      } else if (mod && e.key.toLowerCase() === 'y') {
        e.preventDefault();
        st.redo();
      } else if (!mod && (e.key === '+' || e.key === '=')) {
        st.setZoom(st.zoom * 1.2);
      } else if (!mod && e.key === '-') {
        st.setZoom(st.zoom / 1.2);
      } else if (!mod && e.key === '0') {
        st.setZoom(st.zoom, true);
      } else if (e.key === 'Escape') {
        st.select(null);
      } else if (!mod && (e.key === 'Delete' || e.key === 'Backspace')) {
        // The selected chunk is left out of the export (undo or Restore brings it back).
        if (!st.history || !st.doc || !st.selected || st.strategy) return;
        const view = chunkView(st.doc.parsed, st.history.present, st.count);
        const index = view.chunkAt(st.selected.blockId, st.selected.offset);
        if (index === undefined || view.chunks[index]!.deleted) return;
        e.preventDefault();
        const blocks = st.doc.parsed.blocks;
        st.apply((p) => deleteChunk(p, blocks, index));
      } else if (!mod && (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'j' || e.key === 'k')) {
        if (!st.history || !st.doc) return;
        e.preventDefault();
        const view = chunkView(st.doc.parsed, st.history.present, st.count);
        const current = st.selected ? (view.chunkAt(st.selected.blockId, st.selected.offset) ?? -1) : -1;
        const down = e.key === 'ArrowDown' || e.key === 'j';
        const next = view.chunks[down ? Math.min(view.chunks.length - 1, current + 1) : Math.max(0, current - 1)];
        if (next) st.select(next.segments[0]!.blockId, true, next.segments[0]!.start);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
}

/** How the upload was processed: converted from another format, OCR, simplified, cut short. */
function DocumentNotes({ payload, repoUrl }: { payload: DocumentPayload; repoUrl: string | null }) {
  const t = useT();
  const format = (payload.sourceFormat ?? '').toUpperCase();
  const notes: { key: string; text: string; link?: boolean }[] = [];
  if (payload.parsed?.blocks.some(b => b.lines?.length && (!b.lineGeometry || b.lineGeometry === 'unverified')))
    notes.push({key: 'unverifiedPositions', text: t('unverifiedPositions')});
  if (payload.sourceFormat) notes.push({ key: 'converted', text: t('convertedFrom', { format }) });
  if (payload.notices.includes('auto_ocr')) notes.push({ key: 'ocr', text: t('autoOcr') });
  if (payload.notices.includes('mixed_ocr')) notes.push({ key: 'mixedOcr', text: t('mixedOcr') });
  if (payload.notices.includes('ocr_skipped')) notes.push({ key: 'ocrSkipped', text: t('ocrSkipped') });
  if (payload.notices.includes('formulas_partial')) notes.push({ key: 'formulasPartial', text: t('formulasPartial') });
  if (payload.notices.includes('vlm_figures')) notes.push({ key: 'vlmFigures', text: t('vlmFigures') });
  if (payload.notices.includes('vlm_partial')) notes.push({ key: 'vlmPartial', text: t('vlmPartial') });
  if (payload.notices.includes('pictures_omitted')) notes.push({ key: 'pictures', text: t('picturesOmitted') });
  if (payload.notices.includes('text_fallback')) notes.push({ key: 'text', text: t('textFallback') });
  if (payload.notices.includes('csv_rows_truncated')) notes.push({ key: 'csv', text: t('csvTruncated') });
  if (payload.notices.includes('csv_header_unverified')) notes.push({ key: 'csvHeader', text: t('csvHeaderUnverified') });
  if (payload.notices.includes('csv_header_inferred')) notes.push({ key: 'csvHeaderInferred', text: t('csvHeaderInferred') });
  if (payload.notices.includes('source_tables_partial')) notes.push({ key: 'sourceTables', text: t('sourceTablesPartial') });
  if (payload.notices.includes('csv_records')) notes.push({ key: 'csvRecords', text: t('csvRecords') });
  if (payload.notices.includes('page_limit')) notes.push({ key: 'limit', text: t('pageLimitUnknown', { pages: payload.pageCount }), link: true });
  if (!notes.length) return null;
  return (
    <div data-testid="document-notes" className="shrink-0 space-y-0.5 border-b border-sky-200 bg-sky-50 px-4 py-1.5 text-[13px] text-sky-900 dark:border-sky-900 dark:bg-sky-950/50 dark:text-sky-200">
      {notes.map((n) => (
        <p key={n.key} data-note={n.key} className="flex items-center gap-3">
          <span className="flex-1">{n.text}</span>
          {n.link && repoUrl && (
            <a href={repoUrl} target="_blank" rel="noreferrer" className="font-medium underline underline-offset-2">
              {t('getCode')}
            </a>
          )}
        </p>
      ))}
    </div>
  );
}

function StrategyBar() {
  const t = useT();
  const strategy = useEditor((s) => s.strategy);
  if (!strategy) return null;
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-amber-300 bg-amber-50 px-3 py-1.5 text-[13px] text-amber-900 dark:border-amber-800 dark:bg-amber-950/50 dark:text-amber-200" data-testid="strategy-bar">
      <span className="font-medium">{t('previewing', { name: t(`sName_${strategy.id}` as 'sName_structure') })}</span>
      <span className="text-amber-700 dark:text-amber-400">
        · {strategy.chunks.length} {t('chunks').toLowerCase()} · {t('previewReadOnly')}
      </span>
      <span className="ml-auto flex gap-1.5">
        <Button variant="primary" onClick={() => editFromStrategy(strategy.id, strategy.params, strategy.result)}>
          {t('editFrom')}
        </Button>
        <Button onClick={() => useEditor.getState().closeStrategy()}>{t('closePreview')}</Button>
      </span>
    </div>
  );
}

function ConflictBar() {
  const t = useT();
  const conflict = useEditor((s) => s.conflict);
  if (!conflict) return null;
  return (
    <div className="flex items-center gap-3 border-b border-amber-300 bg-amber-50 px-4 py-2 text-[13px] text-amber-900 dark:border-amber-800 dark:bg-amber-950/60 dark:text-amber-200">
      <span className="flex-1">{t('conflict')}</span>
      <Button onClick={() => useEditor.getState().setSave({ serverRevision: conflict.revision, saveStatus: 'dirty', conflict: null })}>
        {t('keepMine')}
      </Button>
      {conflict.plan && (
        <Button
          onClick={() => {
            const st = useEditor.getState();
            st.apply(() => conflict.plan!);
            st.setSave({ serverRevision: conflict.revision, saveStatus: 'saved', conflict: null });
            acknowledgeDraft(conflict.plan!, conflict.revision);
          }}
        >
          {t('loadTheirs')}
        </Button>
      )}
    </div>
  );
}

function ExpiredBar({ expiresAt }: { expiresAt: string }) {
  const t = useT();
  const status = useEditor((s) => s.saveStatus);
  const [past, setPast] = useState(() => Date.parse(expiresAt) <= Date.now());
  useEffect(() => {
    const id = setInterval(() => setPast(Date.parse(expiresAt) <= Date.now()), 15_000);
    return () => clearInterval(id);
  }, [expiresAt]);
  useEffect(() => {
    if (past) useEditor.getState().setSave({ saveStatus: 'expired' });
  }, [past]);
  if (!past && status !== 'expired') return null;
  return (
    <div
      role="alert"
      className="flex items-center gap-3 border-b border-red-300 bg-red-50 px-4 py-2 text-[13px] text-red-900 dark:border-red-900 dark:bg-red-950/60 dark:text-red-200"
    >
      {t('expiredBanner')}
    </div>
  );
}

function Notice() {
  const notice = useEditor((s) => s.notice);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    if (!notice) return;
    setVisible(true);
    const id = setTimeout(() => setVisible(false), 5000);
    return () => clearTimeout(id);
  }, [notice]);
  if (!notice || !visible) return null;
  return (
    <div
      role="status"
      className="fixed bottom-4 left-1/2 z-50 max-w-lg -translate-x-1/2 rounded-lg bg-zinc-900 px-4 py-2.5 text-[13px] text-white shadow-lg dark:bg-zinc-100 dark:text-zinc-900"
    >
      {notice.text}
    </div>
  );
}
