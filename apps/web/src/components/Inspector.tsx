import { canSplitInside, deleteChunk, mergeChunks, restoreChunk, splitChunk, splitPoints } from '@chunkcanvas/core';
import { AlertTriangle, ArrowDownToLine, ArrowUpToLine, Check, Copy, Crosshair, MapPinOff, RotateCcw, Scissors, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { chunkSolid } from '../lib/colors';
import { useT } from '../lib/i18n';
import { Markdown } from '../lib/markdown';
import { useChunkView, useEditor, useSelectedChunk } from '../store';
import { Button, Kbd, cx } from './ui';

const TYPE_LABEL: Record<string, string> = {
  title: 'Title',
  section_header: 'Heading',
  paragraph: 'Text',
  list_item: 'List',
  table: 'Table',
  picture: 'Picture',
  caption: 'Caption',
  formula: 'Formula',
  code: 'Code',
  footnote: 'Footnote',
  page_header: 'Header',
  page_footer: 'Footer',
  other: 'Other',
};

export function Inspector() {
  return useEditor((s) => s.strategy !== null) ? <PreviewInspector /> : <PlanInspector />;
}

function PreviewInspector() {
  const strategy = useEditor((s) => s.strategy)!;
  const previewSelected = useEditor((s) => s.previewSelected);
  const c = previewSelected !== null ? strategy.chunks[previewSelected] : undefined;
  return c ? <PreviewDetails key={c.index} index={c.index} /> : <EmptyInspector />;
}

function PlanInspector() {
  const t = useT();
  const view = useChunkView();
  const index = useSelectedChunk();
  const chunk = index !== null ? view?.chunks[index] : undefined;
  if (!view || !chunk) return <EmptyInspector />;
  return <ChunkDetails key={chunk.id} index={chunk.index} total={view.kept} lastIndex={view.chunks.length - 1} t={t} />;
}

function EmptyInspector() {
  const t = useT();
  return (
    <div className="flex h-full flex-col gap-4 p-5 text-[13px] text-zinc-500">
      <p>{t('selectChunk')}</p>
      <p className="leading-relaxed">{t('help')}</p>
      <p className="leading-relaxed">{t('innerHelp')}</p>
      <dl className="grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-2 text-xs">
        <dt>
          <Kbd>Ctrl</Kbd> <Kbd>Z</Kbd>
        </dt>
        <dd>{t('undo')}</dd>
        <dt>
          <Kbd>Ctrl</Kbd> <Kbd>Shift</Kbd> <Kbd>Z</Kbd>
        </dt>
        <dd>{t('redo')}</dd>
        <dt>
          <Kbd>↑</Kbd> <Kbd>↓</Kbd>
        </dt>
        <dd>{t('prevNextChunk')}</dd>
        <dt>
          <Kbd>Del</Kbd>
        </dt>
        <dd>{t('deleteKeyHelp')}</dd>
        <dt>
          <Kbd>+</Kbd> <Kbd>−</Kbd>
        </dt>
        <dd>
          {t('zoomIn')} / {t('zoomOut')}
        </dd>
      </dl>
    </div>
  );
}

function ChunkDetails({ index, total, lastIndex, t }: { index: number; total: number; lastIndex: number; t: ReturnType<typeof useT> }) {
  const view = useChunkView()!;
  const chunk = view.chunks[index]!;
  const settings = useEditor((s) => s.history!.present.settings);
  const blocks = useEditor((s) => s.doc!.parsed.blocks);
  const apply = useEditor((s) => s.apply);
  const select = useEditor((s) => s.select);
  const scrollTo = useEditor((s) => s.scrollTo);
  const selected = useEditor((s) => s.selected);
  const [tab, setTab] = useState<'preview' | 'text' | 'blocks'>('preview');
  const [copied, setCopied] = useState(false);
  const pct = Math.min(100, (chunk.tokenCount / settings.maxTokens) * 100);
  const label = view.labels[index];

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(chunk.text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 space-y-3 border-b border-zinc-200 p-4 dark:border-zinc-800">
        <div className="flex items-center gap-2">
          <span className="size-3 rounded-full" style={{ background: chunk.deleted ? 'rgb(161,161,170)' : chunkSolid(index) }} />
          <h2 className="text-[15px] font-semibold">
            {label === null ? (
              t('deletedChunk')
            ) : (
              <>
                {t('chunk')} {label}
                <span className="font-normal text-zinc-400"> / {total}</span>
              </>
            )}
          </h2>
          <span className="ml-auto font-mono text-[11px] text-zinc-400">{chunk.id}</span>
        </div>
        {blocks.some((b) => chunk.blockIds.includes(b.id) && b.lines?.length && b.lineGeometry === 'estimated') && (
          <p className="text-xs text-amber-700 dark:text-amber-400">{t('approximatePositions')}</p>
        )}
        {chunk.deleted && (
          <div className="flex items-center gap-2 rounded-md border border-dashed border-zinc-300 px-3 py-2 text-xs text-zinc-500 dark:border-zinc-700">
            <Trash2 className="size-3.5 shrink-0" />
            <span className="flex-1">{t('deletedNote')}</span>
            <Button data-restore-chunk onClick={() => apply((p) => restoreChunk(p, blocks, index))}>
              <RotateCcw className="size-3.5" />
              {t('restoreChunk')}
            </Button>
          </div>
        )}
        {!chunk.deleted && chunk.excluded.length > 0 && (
          <div className="flex items-center gap-2 text-xs text-zinc-500">
            <span className="flex-1">{t('partlyDeleted')}</span>
            <Button variant="ghost" onClick={() => apply((p) => restoreChunk(p, blocks, index))}>
              <RotateCcw className="size-3.5" />
              {t('restorePart')}
            </Button>
          </div>
        )}
        <div className="flex items-baseline gap-3 text-xs text-zinc-500">
          <span>
            <b className="text-base font-semibold text-zinc-900 tabular-nums dark:text-zinc-100">{chunk.tokenCount}</b> {t('tokens')}
          </span>
          <span>
            {chunk.pageRange
              ? chunk.pageRange[0] === chunk.pageRange[1]
                ? `${t('page')} ${chunk.pageRange[0]}`
                : `${t('pages')} ${chunk.pageRange[0]}–${chunk.pageRange[1]}`
              : t('noLocation')}
          </span>
          <span>
            {chunk.blockIds.length} {t('blocks').toLowerCase()}
          </span>
        </div>
        <div className="relative h-1.5 rounded-full bg-zinc-200 dark:bg-zinc-800">
          <div
            className={cx('h-full rounded-full', chunk.overMax ? 'bg-red-500' : chunk.overTarget ? 'bg-amber-500' : 'bg-emerald-500')}
            style={{ width: `${pct}%` }}
          />
          <div
            className="absolute -top-0.5 h-2.5 w-px bg-zinc-600 dark:bg-zinc-300"
            style={{ left: `${(settings.targetTokens / settings.maxTokens) * 100}%` }}
            title={`${t('target')}: ${settings.targetTokens}`}
          />
        </div>
        {(chunk.overTarget || chunk.overMax) && (
          <p
            className={cx(
              'flex items-start gap-1.5 text-xs',
              chunk.overMax ? 'text-red-600 dark:text-red-400' : 'text-amber-700 dark:text-amber-500',
            )}
          >
            <AlertTriangle className="mt-px size-3.5 shrink-0" />
            {chunk.overMax
              ? chunk.blockIds.length === 1
                ? t('singleBlockOverMax')
                : t('overMax', { n: settings.maxTokens })
              : t('overTarget', { n: settings.targetTokens })}
          </p>
        )}
        <div className="flex flex-wrap gap-1.5">
          <Button disabled={index === 0} onClick={() => apply((p) => mergeChunks(p, blocks, index - 1))}>
            <ArrowUpToLine className="size-3.5" />
            {t('mergePrev')}
          </Button>
          <Button disabled={index === lastIndex} onClick={() => apply((p) => mergeChunks(p, blocks, index))}>
            <ArrowDownToLine className="size-3.5" />
            {t('mergeNext')}
          </Button>
          <Button variant="ghost" onClick={copy}>
            {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
            {copied ? t('copied') : t('copy')}
          </Button>
          {!chunk.deleted && (
            <Button variant="ghost" data-delete-chunk title={`${t('deleteChunk')} (Del)`} onClick={() => apply((p) => deleteChunk(p, blocks, index))}>
              <Trash2 className="size-3.5" />
              {t('deleteChunk')}
            </Button>
          )}
        </div>
      </div>
      <div className="flex shrink-0 gap-1 border-b border-zinc-200 px-3 pt-2 dark:border-zinc-800">
        {(['preview', 'text', 'blocks'] as const).map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => setTab(k)}
            className={cx(
              '-mb-px border-b-2 px-2 pb-2 text-[13px] font-medium',
              tab === k
                ? 'border-accent-600 text-zinc-900 dark:text-zinc-100'
                : 'border-transparent text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200',
            )}
          >
            {t(k)}
            {k === 'blocks' && <span className="ml-1 text-zinc-400">{chunk.segments.length}</span>}
          </button>
        ))}
      </div>
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        {tab === 'preview' && (
          <div className="p-4 text-[13px] text-zinc-800 dark:text-zinc-200">
            <Markdown text={chunk.text} />
          </div>
        )}
        {tab === 'text' && (
          <pre className="p-4 font-mono text-xs leading-relaxed break-words whitespace-pre-wrap text-zinc-800 dark:text-zinc-200">
            {chunk.text}
          </pre>
        )}
        {tab === 'blocks' && (
          <ol className="p-2">
            {chunk.segments.map((seg, i) => {
              const b = view.blockById.get(seg.blockId)!;
              const partial = seg.start > 0 || seg.end < b.text.length;
              const lastSeg = i === chunk.segments.length - 1;
              const inner = canSplitInside(b) ? splitPoints(b).filter((k) => k > seg.start && k < seg.end) : [];
              const edges = [seg.start, ...inner, seg.end];
              const isSelected = selected?.blockId === seg.blockId && selected.offset >= seg.start && selected.offset < seg.end;
              return (
                <li key={`${seg.blockId}@${seg.start}`} className="group">
                  <div
                    className={cx(
                      'rounded-md px-2 py-2',
                      isSelected ? 'bg-accent-50 dark:bg-accent-700/15' : 'hover:bg-zinc-100 dark:hover:bg-zinc-800/60',
                    )}
                  >
                    <div className="flex items-center gap-2 text-[11px]">
                      <span className="rounded bg-zinc-200 px-1.5 py-px font-medium text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
                        {TYPE_LABEL[b.type] ?? b.type}
                      </span>
                      <span className="font-mono text-zinc-400">{b.id}</span>
                      {partial && <span className="text-zinc-400">{t('partOf', { start: seg.start, end: seg.end })}</span>}
                      {b.regions.length ? (
                        <span className="text-zinc-400">
                          {t('page')} {b.regions[0]!.pageIndex + 1}
                        </span>
                      ) : (
                        <span className="flex items-center gap-1 text-amber-600">
                          <MapPinOff className="size-3" />
                          {t('noLocation')}
                        </span>
                      )}
                      {b.source.ref === 'recovered' && <span className="rounded bg-amber-100 px-1 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">{t('recovered')}</span>}
                      <span className="ml-auto flex gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                        {b.regions.length > 0 && (
                          <button
                            type="button"
                            title={t('goTo')}
                            aria-label={t('goTo')}
                            onClick={() => {
                              select(seg.blockId, false, seg.start);
                              scrollTo(seg.blockId, seg.start);
                            }}
                            className="rounded p-1 text-zinc-500 hover:bg-zinc-200 hover:text-zinc-900 dark:hover:bg-zinc-700 dark:hover:text-zinc-100"
                          >
                            <Crosshair className="size-3.5" />
                          </button>
                        )}
                      </span>
                    </div>
                    <p className="mt-1 text-xs leading-snug text-zinc-700 dark:text-zinc-300">
                      {b.text
                        ? edges.slice(0, -1).map((from, k) => (
                            <span key={from}>
                              {k > 0 && (
                                <button
                                  type="button"
                                  title={t('splitSentence')}
                                  aria-label={t('splitSentence')}
                                  data-inspector-split={`${seg.blockId}@${from}`}
                                  onClick={() => apply((p) => splitChunk(p, blocks, { blockId: seg.blockId, offset: from }))}
                                  className="mx-0.5 inline-flex translate-y-0.5 items-center rounded px-0.5 text-zinc-400 hover:bg-accent-100 hover:text-accent-700 dark:hover:bg-accent-700/30"
                                >
                                  <Scissors className="size-3" />
                                </button>
                              )}
                              {b.text.slice(from, edges[k + 1])}
                            </span>
                          ))
                        : '—'}
                    </p>
                  </div>
                  {!lastSeg && seg.end >= b.text.length && (
                    <button
                      type="button"
                      onClick={() => apply((p) => splitChunk(p, blocks, { blockId: seg.blockId }))}
                      className="mx-2 my-0.5 flex w-[calc(100%-1rem)] items-center gap-1.5 rounded border border-dashed border-transparent px-2 py-0.5 text-[11px] text-zinc-400 hover:border-accent-400 hover:text-accent-600 dark:hover:text-accent-400"
                    >
                      <Scissors className="size-3" />
                      {t('splitAfter')}
                    </button>
                  )}
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </div>
  );
}

/** A chunk from a splitter preview: read-only text, no edit actions. */
function PreviewDetails({ index }: { index: number }) {
  const t = useT();
  const strategy = useEditor((s) => s.strategy)!;
  const c = strategy.chunks[index]!;
  const [tab, setTab] = useState<'preview' | 'text'>('text');
  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 space-y-2 border-b border-zinc-200 p-4 dark:border-zinc-800">
        <div className="flex items-center gap-2">
          <span className="size-3 rounded-full" style={{ background: chunkSolid(index) }} />
          <h2 className="text-[15px] font-semibold">
            {t('chunk')} {index + 1}
            <span className="font-normal text-zinc-400"> / {strategy.chunks.length}</span>
          </h2>
        </div>
        <div className="text-xs text-zinc-500">
          <b className="text-base font-semibold text-zinc-900 tabular-nums dark:text-zinc-100">{c.tokenCount}</b> {t('tokens')} ·{' '}
          {t('previewing', { name: t(`sName_${strategy.id}` as 'sName_structure') })}
        </div>
      </div>
      <div className="flex shrink-0 gap-1 border-b border-zinc-200 px-3 pt-2 dark:border-zinc-800">
        {(['text', 'preview'] as const).map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => setTab(k)}
            className={cx(
              '-mb-px border-b-2 px-2 pb-2 text-[13px] font-medium',
              tab === k ? 'border-accent-600 text-zinc-900 dark:text-zinc-100' : 'border-transparent text-zinc-500',
            )}
          >
            {t(k)}
          </button>
        ))}
      </div>
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        {tab === 'text' ? (
          <pre className="p-4 font-mono text-xs leading-relaxed break-words whitespace-pre-wrap text-zinc-800 dark:text-zinc-200">{c.text}</pre>
        ) : (
          <div className="p-4 text-[13px] text-zinc-800 dark:text-zinc-200">
            <Markdown text={c.text} />
          </div>
        )}
      </div>
    </div>
  );
}
