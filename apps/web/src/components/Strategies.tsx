import {
  STRATEGY_DEFAULTS,
  isChunkSettings,
  planResult,
  rangesToBoundaries,
  runStrategy,
  strategyMetrics,
  type StrategyId,
  type StrategyMetrics,
  type StrategyParams,
  type StrategyResult,
} from '@chunkcanvas/core';
import { Eye, EyeOff, Pencil } from 'lucide-react';
import { useDeferredValue, useMemo, useState } from 'react';
import { useT, translate, useLang } from '../lib/i18n';
import { useEditor } from '../store';
import { Button, cx } from './ui';

export const STRATEGY_IDS: StrategyId[] = ['structure', 'recursive', 'character', 'token', 'sentence', 'markdown'];
const LANGCHAIN_PARITY = new Set<StrategyId>(['recursive', 'character', 'token']);

/** Replace the editable plan with a splitter result (one undo step), carrying its overlap to export. */
export function editFromStrategy(id: StrategyId, params: StrategyParams, result: StrategyResult): void {
  const st = useEditor.getState();
  const blocks = st.doc?.parsed.blocks;
  const plan = st.history?.present;
  if (!blocks || !plan) return;
  const metrics = strategyMetrics(result, blocks, st.count);
  const overlapTokens = metrics.chunks > 1 ? Math.round(metrics.overlapTokens / (metrics.chunks - 1)) : 0;
  const settings =
    id === 'structure'
      ? { ...plan.settings, targetTokens: params.chunkSize, maxTokens: Math.max(plan.settings.maxTokens, params.chunkSize) }
      : plan.settings;
  const boundaries = rangesToBoundaries(blocks, settings, result.doc, result.ranges);
  if (!isChunkSettings({ ...settings, overlapTokens: Math.min(2000, overlapTokens) })) {
    st.notify(translate(useLang.getState().lang, 'invalidChunkSize'));
    return;
  }
  st.apply((p) => ({ ...p, boundaries, settings: { ...settings, overlapTokens: Math.min(2000, overlapTokens) } }));
  st.closeStrategy();
}

export function StrategiesPanel() {
  const t = useT();
  const doc = useEditor((s) => s.doc);
  const plan = useEditor((s) => s.history?.present);
  const tokenizer = useEditor((s) => s.tokenizer);
  const active = useEditor((s) => s.strategy);
  const [params, setParams] = useState<Record<StrategyId, StrategyParams>>(() => ({
    ...STRATEGY_DEFAULTS,
    structure: { ...STRATEGY_DEFAULTS.structure, chunkSize: plan?.settings.targetTokens ?? 500 },
  }));
  const deferred = useDeferredValue(params);

  const results = useMemo(() => {
    if (!doc || !plan || !tokenizer) return null;
    const out = {} as Record<StrategyId, { result: StrategyResult; metrics: StrategyMetrics } | { error: string }>;
    for (const id of STRATEGY_IDS) {
      try {
        const result = runStrategy(id, deferred[id], doc.parsed.blocks, plan.settings, tokenizer);
        out[id] = { result, metrics: strategyMetrics(result, doc.parsed.blocks, tokenizer.count) };
      } catch (e) {
        out[id] = { error: (e as Error).message };
      }
    }
    return out;
  }, [doc, plan?.settings, tokenizer, deferred]);

  const current = useMemo(() => {
    if (!doc || !plan || !tokenizer) return null;
    return strategyMetrics(planResult(doc.parsed.blocks, plan), doc.parsed.blocks, tokenizer.count);
  }, [doc, plan, tokenizer]);

  if (!results || !current) return null;
  return (
    <div className="scroll-thin min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
      <p className="text-xs leading-relaxed text-zinc-500">{t('strategiesIntro')}</p>
      <div className="rounded-lg border border-accent-200 bg-accent-50/60 p-3 dark:border-accent-800 dark:bg-accent-700/10">
        <div className="text-[13px] font-medium">{t('yourEdits')}</div>
        <Metrics m={current} />
      </div>
      {STRATEGY_IDS.map((id) => {
        const r = results[id];
        const p = params[id];
        const isActive = active?.id === id;
        const set = (patch: Partial<StrategyParams>) => setParams((all) => ({ ...all, [id]: { ...all[id], ...patch } }));
        return (
          <section
            key={id}
            data-strategy={id}
            className={cx(
              'rounded-lg border p-3 transition-colors',
              isActive ? 'border-accent-500 ring-1 ring-accent-500' : 'border-zinc-200 dark:border-zinc-800',
            )}
          >
            <div className="flex items-start justify-between gap-2">
              <div>
                <h3 className="text-[13px] font-medium">{t(`sName_${id}` as 'sName_structure')}</h3>
                <p className="mt-0.5 text-[11px] leading-snug text-zinc-500">{t(`sDesc_${id}` as 'sDesc_structure')}</p>
                {LANGCHAIN_PARITY.has(id) && <p className="mt-0.5 text-[11px] text-emerald-700 dark:text-emerald-400">✓ {t('sameAsLangchain')}</p>}
              </div>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[12px]">
              {id !== 'markdown' && (
                <NumberField label={t('chunkSize')} value={p.chunkSize} onChange={(v) => set({ chunkSize: v })} min={id === 'structure' ? 20 : 1} max={id === 'structure' ? 8000 : undefined} />
              )}
              {(id === 'recursive' || id === 'character' || id === 'token') && (
                <NumberField label={t('chunkOverlap')} value={p.chunkOverlap} onChange={(v) => set({ chunkOverlap: v })} min={0} />
              )}
              {(id === 'recursive' || id === 'character') && (
                <label className="flex items-center gap-1">
                  <span className="text-zinc-500">{t('unit')}</span>
                  <select
                    value={p.unit}
                    onChange={(e) => set({ unit: e.target.value as 'chars' | 'tokens' })}
                    className="rounded border border-zinc-300 bg-white px-1 py-0.5 dark:border-zinc-700 dark:bg-zinc-950"
                  >
                    <option value="chars">{t('chars')}</option>
                    <option value="tokens">{t('tokensUnit')}</option>
                  </select>
                </label>
              )}
              {id !== 'markdown' && (id === 'token' || id === 'sentence' || id === 'structure') && <span className="text-zinc-400">{t('tokensUnit')}</span>}
              {id === 'markdown' && (
                <NumberField label={t('headerLevel')} value={p.headerLevel} onChange={(v) => set({ headerLevel: v })} min={1} max={6} />
              )}
            </div>
            {'error' in r ? (
              <p className="mt-2 text-xs text-red-600">{r.error}</p>
            ) : (
              <>
                <Metrics m={r.metrics} />
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <Button
                    data-strategy-preview={id}
                    variant={isActive ? 'secondary' : 'ghost'}
                    onClick={() => (isActive ? useEditor.getState().closeStrategy() : useEditor.getState().showStrategy(id, p, r.result))}
                  >
                    {isActive ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
                    {isActive ? t('closePreview') : t('previewAction')}
                  </Button>
                  <Button data-strategy-edit={id} variant="ghost" onClick={() => editFromStrategy(id, p, r.result)}>
                    <Pencil className="size-3.5" />
                    {t('editFrom')}
                  </Button>
                </div>
              </>
            )}
          </section>
        );
      })}
      <p className="text-[11px] leading-snug text-zinc-400">{t('splitterInput')}</p>
    </div>
  );
}

function NumberField({ label, value, onChange, min, max }: { label: string; value: number; onChange: (v: number) => void; min: number; max?: number }) {
  return (
    <label className="flex items-center gap-1">
      <span className="text-zinc-500">{label}</span>
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        onChange={(e) => {
          const v = Math.round(Number(e.target.value));
          if (Number.isFinite(v) && v >= min && (max === undefined || v <= max)) onChange(v);
        }}
        className="w-16 rounded border border-zinc-300 bg-white px-1 py-0.5 text-right tabular-nums dark:border-zinc-700 dark:bg-zinc-950"
      />
    </label>
  );
}

function Metrics({ m }: { m: StrategyMetrics }) {
  const t = useT();
  const bad = 'text-red-600 dark:text-red-400';
  return (
    <div className="mt-2 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-zinc-600 tabular-nums dark:text-zinc-400" data-metrics>
      <span>
        <b className="text-zinc-900 dark:text-zinc-100">{m.chunks}</b> {t('chunks').toLowerCase()}
      </span>
      <span>
        avg {m.avgTokens} · max {m.maxTokens} {t('tokens')}
      </span>
      <span className={cx(m.midSentenceCuts > 0 && bad)} data-metric="mid">
        {m.midSentenceCuts} {t('mMid')}
      </span>
      <span className={cx(m.orphanHeadings > 0 && bad)}>
        {m.orphanHeadings} {t('mOrphan')}
      </span>
      <span className={cx(m.splitTables > 0 && bad)}>
        {m.splitTables} {t('mTables')}
      </span>
      {m.overlapTokens > 0 && (
        <span>
          {m.overlapTokens} {t('mOverlap')}
        </span>
      )}
    </div>
  );
}
