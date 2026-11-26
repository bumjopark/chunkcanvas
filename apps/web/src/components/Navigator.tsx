import { restoreChunk, type Chunk } from '@chunkcanvas/core';
import { AlertTriangle, MapPinOff, RotateCcw, Trash2 } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { chunkSolid } from '../lib/colors';
import { useT } from '../lib/i18n';
import { type ChunkView, useChunkView, useEditor, useSelectedChunk } from '../store';
import { StrategiesPanel } from './Strategies';
import { cx } from './ui';

function preview(text: string): string {
  return text
    .replace(/^#+\s+/gm, '')
    .replace(/^\|?[\s|:-]*-{3,}[\s|:-]*$/gm, '')
    .replace(/\|/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 140);
}

export function Navigator() {
  const t = useT();
  const [tab, setTab] = useState<'chunks' | 'outline' | 'strategies'>('chunks');
  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 gap-1 border-b border-zinc-200 px-2 pt-2 dark:border-zinc-800">
        {(['chunks', 'outline', 'strategies'] as const).map((k) => (
          <button
            key={k}
            type="button"
            data-tab={k}
            onClick={() => setTab(k)}
            className={cx(
              '-mb-px border-b-2 px-2.5 pb-2 text-[13px] font-medium',
              tab === k
                ? 'border-accent-600 text-zinc-900 dark:text-zinc-100'
                : 'border-transparent text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200',
            )}
          >
            {t(k)}
          </button>
        ))}
      </div>
      {tab === 'chunks' ? <ChunkList /> : tab === 'outline' ? <Outline /> : <StrategiesPanel />}
    </div>
  );
}

function ChunkList() {
  return useEditor((s) => s.strategy) ? <PreviewChunkList /> : <PlanChunkList />;
}

function PreviewChunkList() {
  const t = useT();
  const strategy = useEditor((s) => s.strategy)!;
  const selected = useEditor((s) => s.previewSelected);
  const selectPreview = useEditor((s) => s.selectPreview);
  const setHover = useEditor((s) => s.setHoverChunk);
  const max = Math.max(1, ...strategy.chunks.map((c) => c.tokenCount));
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 border-b border-zinc-200 px-3 py-2 text-xs text-amber-700 dark:border-zinc-800 dark:text-amber-400">
        {t('previewing', { name: t(`sName_${strategy.id}` as 'sName_structure') })} · {t('previewReadOnly')}
      </div>
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto p-1.5" onPointerLeave={() => setHover(null)}>
        {strategy.chunks.map((c) => (
          <button
            key={c.index}
            type="button"
            data-chunk-row={c.index}
            data-preview-row
            onClick={() => selectPreview(c.index, true)}
            onPointerEnter={() => setHover(c.index)}
            className={cx(
              'mb-0.5 block w-full rounded-md border px-2.5 py-2 text-left',
              selected === c.index ? 'border-accent-300 bg-accent-50 dark:border-accent-700 dark:bg-accent-700/15' : 'border-transparent hover:bg-zinc-100 dark:hover:bg-zinc-800/60',
            )}
          >
            <div className="flex items-center gap-2 text-xs">
              <span className="size-2.5 shrink-0 rounded-full" style={{ background: chunkSolid(c.index) }} />
              <span className="font-semibold text-zinc-800 dark:text-zinc-200">#{c.index + 1}</span>
              <span className="text-zinc-500">{c.pageRange ? (c.pageRange[0] === c.pageRange[1] ? `${t('page')} ${c.pageRange[0]}` : `${t('pages')} ${c.pageRange[0]}–${c.pageRange[1]}`) : t('noLocation')}</span>
              <span className="ml-auto text-zinc-500 tabular-nums">{c.tokenCount}</span>
            </div>
            <div className="mt-1.5 h-1 rounded-full bg-zinc-200 dark:bg-zinc-800">
              <div className="h-full rounded-full bg-zinc-400 dark:bg-zinc-500" style={{ width: `${(c.tokenCount / max) * 100}%` }} />
            </div>
            <div className="mt-1.5 line-clamp-2 text-xs leading-snug text-zinc-600 dark:text-zinc-400">{preview(c.text)}</div>
          </button>
        ))}
      </div>
    </div>
  );
}

/** The text a deleted chunk held, to recognise it by. */
function blocksText(view: ChunkView, c: Chunk): string {
  return c.segments.map((s) => view.blockById.get(s.blockId)!.text.slice(s.start, s.end)).join(' ');
}

function PlanChunkList() {
  const t = useT();
  const view = useChunkView();
  const settings = useEditor((s) => s.history?.present.settings);
  const blocks = useEditor((s) => s.doc?.parsed.blocks);
  const apply = useEditor((s) => s.apply);
  const select = useEditor((s) => s.select);
  const setHover = useEditor((s) => s.setHoverChunk);
  const hover = useEditor((s) => s.hoverChunk);
  const selected = useSelectedChunk();
  const listRef = useRef<HTMLDivElement>(null);
  const unlocated = useEditor((s) => s.doc?.parsed.blocks.filter((b) => !b.regions.length).length ?? 0);
  const tokenizerName = useEditor((s) => s.tokenizer?.name ?? 'cl100k_base');

  useEffect(() => {
    if (selected === null) return;
    listRef.current?.querySelector(`[data-chunk-row="${selected}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [selected]);

  const stats = useMemo(() => {
    if (!view || !settings) return null;
    const kept = view.chunks.filter((c) => !c.deleted);
    const total = kept.reduce((a, c) => a + c.tokenCount, 0);
    return {
      count: kept.length,
      deleted: view.chunks.length - kept.length,
      avg: Math.round(total / Math.max(1, kept.length)),
      over: kept.filter((c) => c.overTarget).length,
      maxTokens: Math.max(settings.maxTokens, ...kept.map((c) => c.tokenCount)),
    };
  }, [view, settings]);

  if (!view || !settings || !stats) return null;
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 space-y-0.5 border-b border-zinc-200 px-3 py-2 text-xs text-zinc-500 dark:border-zinc-800">
        <div className="flex justify-between">
          <span>
            <b className="font-semibold text-zinc-800 dark:text-zinc-200">{stats.count}</b> {t('chunks').toLowerCase()} · avg{' '}
            {stats.avg} {t('tokens')}
            {stats.deleted > 0 && <> · {t('deletedCount', { n: stats.deleted })}</>}
          </span>
          {stats.over > 0 && (
            <span className="flex items-center gap-1 text-amber-600 dark:text-amber-500">
              <AlertTriangle className="size-3" />
              {stats.over}
            </span>
          )}
        </div>
        <div>{t('tokenizerNote', { name: tokenizerName })}</div>
        {unlocated > 0 && (
          <div className="flex items-center gap-1">
            <MapPinOff className="size-3" />
            {t('unlocated', { n: unlocated })}
          </div>
        )}
      </div>
      <div ref={listRef} className="scroll-thin min-h-0 flex-1 overflow-y-auto p-1.5" onPointerLeave={() => setHover(null)}>
        {view.chunks.map((c) => {
          const pct = Math.min(100, (c.tokenCount / stats.maxTokens) * 100);
          const targetPct = (settings.targetTokens / stats.maxTokens) * 100;
          if (c.deleted) {
            return (
              <div
                key={c.id}
                data-chunk-row={c.index}
                data-deleted-row
                onPointerEnter={() => setHover(c.index)}
                className={cx(
                  'mb-0.5 flex w-full items-center gap-2 rounded-md border border-dashed px-2.5 py-1.5 text-xs text-zinc-400',
                  selected === c.index ? 'border-zinc-400 bg-zinc-100 dark:bg-zinc-800/60' : 'border-zinc-300 dark:border-zinc-700',
                )}
              >
                <button
                  type="button"
                  className="flex min-w-0 flex-1 items-center gap-2 text-left"
                  onClick={() => select(c.segments[0]!.blockId, true, c.segments[0]!.start)}
                >
                  <Trash2 className="size-3 shrink-0" />
                  <span className="shrink-0">{t('deleted')}</span>
                  <span className="truncate line-through">{preview(blocksText(view, c))}</span>
                </button>
                <button
                  type="button"
                  data-restore={c.index}
                  onClick={() => blocks && apply((p) => restoreChunk(p, blocks, c.index))}
                  className="flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-zinc-500 hover:bg-zinc-200 hover:text-zinc-900 dark:hover:bg-zinc-700 dark:hover:text-zinc-100"
                >
                  <RotateCcw className="size-3" />
                  {t('restoreChunk')}
                </button>
              </div>
            );
          }
          return (
            <button
              key={c.id}
              type="button"
              data-chunk-row={c.index}
              onClick={() => select(c.segments[0]!.blockId, true, c.segments[0]!.start)}
              onPointerEnter={() => setHover(c.index)}
              className={cx(
                'mb-0.5 block w-full rounded-md border px-2.5 py-2 text-left transition-colors',
                selected === c.index
                  ? 'border-accent-300 bg-accent-50 dark:border-accent-700 dark:bg-accent-700/15'
                  : hover === c.index
                    ? 'border-transparent bg-zinc-100 dark:bg-zinc-800/60'
                    : 'border-transparent hover:bg-zinc-100 dark:hover:bg-zinc-800/60',
              )}
            >
              <div className="flex items-center gap-2 text-xs">
                <span className="size-2.5 shrink-0 rounded-full" style={{ background: chunkSolid(c.index) }} />
                <span className="font-semibold text-zinc-800 dark:text-zinc-200">#{view.labels[c.index]}</span>
                <span className="text-zinc-500">
                  {c.pageRange
                    ? c.pageRange[0] === c.pageRange[1]
                      ? `${t('page')} ${c.pageRange[0]}`
                      : `${t('pages')} ${c.pageRange[0]}–${c.pageRange[1]}`
                    : t('noLocation')}
                </span>
                <span
                  className={cx(
                    'ml-auto tabular-nums',
                    c.overMax ? 'font-semibold text-red-600 dark:text-red-400' : c.overTarget ? 'text-amber-600 dark:text-amber-500' : 'text-zinc-500',
                  )}
                >
                  {c.tokenCount}
                </span>
              </div>
              <div className="relative mt-1.5 h-1 rounded-full bg-zinc-200 dark:bg-zinc-800">
                <div
                  className={cx('h-full rounded-full', c.overMax ? 'bg-red-500' : c.overTarget ? 'bg-amber-500' : 'bg-zinc-400 dark:bg-zinc-500')}
                  style={{ width: `${pct}%` }}
                />
                <div className="absolute -top-0.5 h-2 w-px bg-zinc-500" style={{ left: `${targetPct}%` }} />
              </div>
              <div className="mt-1.5 line-clamp-2 text-xs leading-snug text-zinc-600 dark:text-zinc-400">{preview(c.text)}</div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function Outline() {
  const t = useT();
  const view = useChunkView();
  const doc = useEditor((s) => s.doc);
  const select = useEditor((s) => s.select);
  const headings = useMemo(
    () => doc?.parsed.blocks.filter((b) => b.type === 'title' || b.type === 'section_header') ?? [],
    [doc],
  );
  if (!headings.length) return <p className="p-4 text-[13px] text-zinc-500">{t('noHeadings')}</p>;
  return (
    <div className="scroll-thin min-h-0 flex-1 overflow-y-auto p-1.5">
      {headings.map((h) => {
        const chunk = view?.chunkAt(h.id);
        return (
          <button
            key={h.id}
            type="button"
            onClick={() => select(h.id, true)}
            className="flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-[13px] hover:bg-zinc-100 dark:hover:bg-zinc-800/60"
            style={{ paddingLeft: 8 + Math.min(4, Math.max(0, (h.level ?? 1) - 1)) * 12 }}
          >
            <span className={cx('flex-1', h.type === 'title' && 'font-semibold')}>{h.text}</span>
            {chunk !== undefined && (
              <span className="mt-0.5 shrink-0 rounded px-1 text-[10px] font-semibold text-white" style={{ background: chunkSolid(chunk) }}>
                {chunk + 1}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
