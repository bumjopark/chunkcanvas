import {
  buildJsonExport,
  importSplitPlan,
  isChunkSettings,
  resetSplitPlan,
  toJsonl,
  toMarkdown,
  toSplitPlanFile,
  type ChunkSettings,
} from '@chunkcanvas/core';
import {
  AlertCircle,
  Check,
  CloudOff,
  Download,
  Eye,
  EyeOff,
  FileText,
  Languages,
  LayoutGrid,
  Loader2,
  Maximize2,
  Moon,
  Redo2,
  SlidersHorizontal,
  Sun,
  Trash2,
  Undo2,
  Upload,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { ParserInfo } from '../lib/api';
import { baseName, downloadText } from '../lib/download';
import { useLang, useT } from '../lib/i18n';
import { TOKENIZERS, loadTokenizer, type TokenizerName } from '../lib/tokens';
import { useEditor, writePref } from '../store';
import { Button, IconButton, MenuItem, Popover, Switch, cx } from './ui';

export function Logo({ compact = false }: { compact?: boolean }) {
  return (
    <a href="/" className="flex shrink-0 items-center gap-2 font-semibold tracking-tight">
      <img src="/favicon.svg" alt="" className="size-6" />
      {!compact && <span>ChunkCanvas</span>}
    </a>
  );
}

export function ThemeLangButtons() {
  const t = useT();
  const toggleLang = useLang((s) => s.toggle);
  const [dark, setDark] = useState(() => document.documentElement.classList.contains('dark'));
  const toggleTheme = () => {
    const next = !dark;
    document.documentElement.classList.toggle('dark', next);
    try {
      localStorage.setItem('chunkcanvas.theme', next ? 'dark' : 'light');
    } catch {
      /* ignore */
    }
    setDark(next);
  };
  return (
    <>
      <IconButton label={t('language')} onClick={toggleLang}>
        <Languages className="size-4" />
      </IconButton>
      <IconButton label={t('theme')} onClick={toggleTheme}>
        {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
      </IconButton>
    </>
  );
}

export function Toolbar({
  parsers,
  onDelete,
}: {
  parsers: ParserInfo[];
  onDelete: () => void;
}) {
  const t = useT();
  const doc = useEditor((s) => s.doc)!;
  const canUndo = useEditor((s) => (s.history?.past.length ?? 0) > 0);
  const canRedo = useEditor((s) => (s.history?.future.length ?? 0) > 0);
  const undo = useEditor((s) => s.undo);
  const redo = useEditor((s) => s.redo);
  const zoom = useEditor((s) => s.zoom);
  const fitWidth = useEditor((s) => s.fitWidth);
  const setZoom = useEditor((s) => s.setZoom);
  const showChunks = useEditor((s) => s.showChunks);
  const showBlocks = useEditor((s) => s.showBlocks);
  const toggle = useEditor((s) => s.toggle);
  const current = parsers.find((p) => p.id === doc.parser);

  return (
    <header className="flex h-12 shrink-0 items-center gap-2 border-b border-zinc-200 bg-white px-3 dark:border-zinc-800 dark:bg-zinc-950">
      <Logo compact />
      <div className="mx-1 h-5 w-px bg-zinc-200 dark:bg-zinc-800" />
      <FileText className="size-4 shrink-0 text-zinc-400" />
      <span className="max-w-[18rem] truncate text-[13px] font-medium" title={doc.filename}>
        {doc.filename}
      </span>
      <span
        data-testid="parser-badge"
        title={current?.description}
        className="rounded-md border border-zinc-200 px-2 py-0.5 text-xs text-zinc-600 dark:border-zinc-800 dark:text-zinc-400"
      >
        {current?.label ?? doc.parser}
      </span>

      <div className="ml-3 flex items-center gap-0.5">
        <IconButton label={`${t('undo')} (Ctrl+Z)`} disabled={!canUndo} onClick={undo}>
          <Undo2 className="size-4" />
        </IconButton>
        <IconButton label={`${t('redo')} (Ctrl+Shift+Z)`} disabled={!canRedo} onClick={redo}>
          <Redo2 className="size-4" />
        </IconButton>
        <div className="mx-1 h-5 w-px bg-zinc-200 dark:bg-zinc-800" />
        <IconButton label={t('zoomOut')} onClick={() => setZoom(zoom / 1.2)}>
          <ZoomOut className="size-4" />
        </IconButton>
        <button
          type="button"
          onClick={() => setZoom(zoom, true)}
          title={t('fitWidth')}
          className={cx(
            'w-12 rounded-md py-1 text-center text-xs tabular-nums hover:bg-zinc-200/70 dark:hover:bg-zinc-800',
            fitWidth && 'text-accent-600 dark:text-accent-400',
          )}
        >
          {Math.round(zoom * 100)}%
        </button>
        <IconButton label={t('zoomIn')} onClick={() => setZoom(zoom * 1.2)}>
          <ZoomIn className="size-4" />
        </IconButton>
        <IconButton label={t('fitWidth')} active={fitWidth} onClick={() => setZoom(zoom, true)}>
          <Maximize2 className="size-4" />
        </IconButton>
        <div className="mx-1 h-5 w-px bg-zinc-200 dark:bg-zinc-800" />
        <IconButton label={t('showChunks')} active={showChunks} onClick={() => toggle('showChunks')}>
          {showChunks ? <Eye className="size-4" /> : <EyeOff className="size-4" />}
        </IconButton>
        <IconButton label={t('showBlocks')} active={showBlocks} onClick={() => toggle('showBlocks')}>
          <LayoutGrid className="size-4" />
        </IconButton>
      </div>

      <div className="ml-auto flex items-center gap-1">
        <SaveIndicator />
        <Expiry expiresAt={doc.expiresAt} />
        <SettingsMenu />
        <ExportMenu />
        <ThemeLangButtons />
        <IconButton label={t('deleteDoc')} onClick={onDelete} className="hover:text-red-600">
          <Trash2 className="size-4" />
        </IconButton>
      </div>
    </header>
  );
}

function SaveIndicator() {
  const t = useT();
  const status = useEditor((s) => s.saveStatus);
  const icon =
    status === 'saved' ? (
      <Check className="size-3.5" />
    ) : status === 'saving' || status === 'dirty' ? (
      <Loader2 className="size-3.5 animate-spin" />
    ) : status === 'error' ? (
      <CloudOff className="size-3.5" />
    ) : (
      <AlertCircle className="size-3.5" />
    );
  const label = {
    saved: t('saved'),
    dirty: t('saving'),
    saving: t('saving'),
    error: t('saveError'),
    conflict: t('unsaved'),
    expired: t('unsaved'),
  }[status];
  return (
    <span
      data-testid="save-status"
      data-status={status}
      className={cx(
        'mr-1 flex items-center gap-1 text-xs',
        status === 'error' || status === 'conflict' || status === 'expired' ? 'text-red-600 dark:text-red-400' : 'text-zinc-500',
      )}
    >
      {icon}
      {label}
    </span>
  );
}

function Expiry({ expiresAt }: { expiresAt: string }) {
  const t = useT();
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);
  const minutes = Math.max(0, Math.ceil((Date.parse(expiresAt) - now) / 60_000));
  return (
    <span className={cx('mr-1 hidden text-xs xl:inline', minutes <= 10 ? 'text-amber-600' : 'text-zinc-400')}>
      {t('expiresIn', { m: minutes })}
    </span>
  );
}

function SettingsMenu() {
  const t = useT();
  const settings = useEditor((s) => s.history!.present.settings);
  return (
    <Popover
      align="right"
      width="w-72"
      trigger={({ toggle, open }) => (
        <Button variant={open ? 'secondary' : 'ghost'} onClick={toggle}>
          <SlidersHorizontal className="size-3.5" />
          {t('settings')}
        </Button>
      )}
    >
      {(close) => <SettingsForm initial={settings} onDone={close} />}
    </Popover>
  );
}

function SettingsForm({ initial, onDone }: { initial: ChunkSettings; onDone: () => void }) {
  const t = useT();
  const apply = useEditor((s) => s.apply);
  const blocks = useEditor((s) => s.doc!.parsed.blocks);
  const count = useEditor((s) => s.count);
  const [s, setS] = useState(initial);
  const invalid = !isChunkSettings(s);
  const num = (key: 'targetTokens' | 'maxTokens', label: string) => (
    <label className="flex items-center justify-between gap-3 py-1 text-[13px]">
      <span>{label}</span>
      <input
        type="number"
        min={20}
        max={16000}
        step={50}
        value={s[key]}
        onChange={(e) => setS({ ...s, [key]: Number(e.target.value) })}
        className="w-24 rounded-md border border-zinc-300 bg-white px-2 py-1 text-right text-[13px] tabular-nums dark:border-zinc-700 dark:bg-zinc-950"
      />
    </label>
  );
  return (
    <div className="space-y-1 p-1.5">
      {num('targetTokens', t('target'))}
      {num('maxTokens', t('max'))}
      <Switch checked={s.structureAware} onChange={(v) => setS({ ...s, structureAware: v })} label={t('structureAware')} />
      <Switch checked={s.keepHeadingWithBody} onChange={(v) => setS({ ...s, keepHeadingWithBody: v })} label={t('keepHeading')} />
      <Switch checked={s.excludeFurniture} onChange={(v) => setS({ ...s, excludeFurniture: v })} label={t('excludeFurniture')} />
      <Button
        variant="primary"
        className="mt-2 w-full"
        disabled={invalid}
        onClick={() => {
          writePref('settings', s);
          apply((p) => resetSplitPlan(p, blocks, count, s));
          onDone();
        }}
      >
        {t('rechunk')}
      </Button>
      <p className="text-[11px] leading-snug text-zinc-500">{t('rechunkHint')}</p>
      <div className="mt-2 space-y-2 border-t border-zinc-200 pt-2 dark:border-zinc-800">
        <TokenizerSelect />
        <OverlapInput />
      </div>
    </div>
  );
}

export function TokenizerSelect() {
  const t = useT();
  const current = useEditor((s) => s.tokenizer?.name ?? 'cl100k_base');
  const setTokenizer = useEditor((s) => s.setTokenizer);
  const [busy, setBusy] = useState(false);
  return (
    <label className="block text-[13px]">
      <span className="mb-1 flex items-center gap-2">
        {t('tokenizer')} {busy && <Loader2 className="size-3 animate-spin" />}
      </span>
      <select
        value={current}
        data-testid="tokenizer-select"
        onChange={async (e) => {
          setBusy(true);
          setTokenizer(await loadTokenizer(e.target.value as TokenizerName));
          setBusy(false);
        }}
        className="w-full rounded-md border border-zinc-300 bg-white px-2 py-1 text-[13px] dark:border-zinc-700 dark:bg-zinc-950"
      >
        {TOKENIZERS.map((k) => (
          <option key={k.name} value={k.name}>
            {k.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function OverlapInput() {
  const t = useT();
  const overlap = useEditor((s) => s.history!.present.settings.overlapTokens);
  const apply = useEditor((s) => s.apply);
  const [value, setValue] = useState(String(overlap));
  useEffect(() => setValue(String(overlap)), [overlap]);
  const commit = () => {
    const n = Math.max(0, Math.min(2000, Math.round(Number(value) || 0)));
    setValue(String(n));
    if (n !== overlap) apply((p) => ({ ...p, settings: { ...p.settings, overlapTokens: n } }));
  };
  return (
    <label className="block text-[13px]">
      <span className="flex items-center justify-between gap-3">
        <span>{t('overlap')}</span>
        <input
          type="number"
          min={0}
          max={2000}
          step={10}
          value={value}
          data-testid="overlap-input"
          onChange={(e) => setValue(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === 'Enter' && commit()}
          className="w-24 rounded-md border border-zinc-300 bg-white px-2 py-1 text-right text-[13px] tabular-nums dark:border-zinc-700 dark:bg-zinc-950"
        />
      </span>
      <span className="mt-1 block text-[11px] leading-snug text-zinc-500">{t('overlapHint')}</span>
    </label>
  );
}

function ExportMenu() {
  const t = useT();
  const fileRef = useRef<HTMLInputElement>(null);
  const notify = useEditor((s) => s.notify);

  const [includeAnchors, setIncludeAnchors] = useState(true);
  const run = (kind: 'json' | 'jsonl' | 'md' | 'plan') => {
    const st = useEditor.getState();
    const doc = st.doc!;
    const plan = { ...st.history!.present, revision: st.serverRevision };
    const name = baseName(doc.filename);
    try {
      if (kind === 'plan') {
        downloadText(`${name}.split-plan.json`, JSON.stringify(toSplitPlanFile(doc.parsed, plan, { includeAnchors }), null, 2), 'application/json');
        return;
      }
      const exp = buildJsonExport(doc.parsed, plan, st.count, { tokenizer: st.tokenizer?.name ?? 'cl100k_base', encoder: st.tokenizer ?? undefined });
      if (kind === 'json') downloadText(`${name}.chunks.json`, JSON.stringify(exp, null, 2), 'application/json');
      if (kind === 'jsonl') downloadText(`${name}.chunks.jsonl`, toJsonl(exp), 'application/jsonl');
      if (kind === 'md') downloadText(`${name}.chunks.md`, toMarkdown(exp), 'text/markdown');
    } catch (e) {
      notify((e as Error).message);
    }
  };

  const onImport = async (file: File) => {
    const st = useEditor.getState();
    try {
      const raw = JSON.parse(await file.text());
      const current = useEditor.getState();
      if (!st.doc || current.doc !== st.doc || !current.history) return;
      const { plan, warnings } = importSplitPlan(current.doc.parsed, current.history.present, raw);
      current.apply(() => plan);
      notify([t('importDone'), ...warnings].join(' '));
    } catch (e) {
      if (useEditor.getState().doc === st.doc) notify((e as Error).message);
    }
  };

  return (
    <Popover
      align="right"
      width="w-56"
      trigger={({ toggle }) => (
        <Button variant="primary" onClick={toggle} data-testid="export-button">
          <Download className="size-3.5" />
          {t('exportLabel')}
        </Button>
      )}
    >
      {(close) => (
        <div>
          {(
            [
              ['jsonl', t('exportJsonl')],
              ['json', t('exportJson')],
              ['md', t('exportMd')],
              ['plan', t('exportPlan')],
            ] as const
          ).map(([k, label]) => (
            <MenuItem
              key={k}
              data-export={k}
              onClick={() => {
                close();
                run(k);
              }}
            >
              <Download className="size-3.5 text-zinc-400" />
              {label}
            </MenuItem>
          ))}
          <div className="px-2 py-1">
            <Switch checked={includeAnchors} onChange={setIncludeAnchors} label={t('includeAnchors')} />
            <p className="text-[11px] text-zinc-500">{t('anchorHint')}</p>
          </div>
          <div className="my-1 h-px bg-zinc-200 dark:bg-zinc-800" />
          <MenuItem onClick={() => fileRef.current?.click()}>
            <Upload className="size-3.5 text-zinc-400" />
            {t('importPlan')}
          </MenuItem>
          <input
            ref={fileRef}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = '';
              close();
              if (f) void onImport(f);
            }}
          />
        </div>
      )}
    </Popover>
  );
}
