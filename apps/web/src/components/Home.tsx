import { ArrowRight, FileText, FileUp, GitBranch, ImageIcon, ShieldCheck, Sparkles, X } from 'lucide-react';
import { useRef, useState } from 'react';
import { ApiError, api, type AppConfig, type Sample } from '../lib/api';
import { docs } from '../lib/docs';
import { formatDuration, useLang, useT } from '../lib/i18n';
import { Logo, ThemeLangButtons } from './Toolbar';
import { Spinner, cx } from './ui';

export function Home({ config, samples, navigate }: { config: AppConfig; samples: Sample[]; navigate: (path: string) => void }) {
  const t = useT();
  const lang = useLang((s) => s.lang);
  const parser = config.defaultParser;
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const [recent, setRecent] = useState(docs.list);
  const ttl = formatDuration(lang, config.limits.documentTtlSeconds);
  const openReading = config.vlm?.available && config.vlm.open;
  const [readThisFile, setReadThisFile] = useState(false);
  const [vlmPassword, setVlmPassword] = useState<string | null>(() =>
    config.vlm?.available && !config.vlm.open ? savedVlmPassword() : null);

  const start = async (work: () => ReturnType<typeof api.upload>, label: string) => {
    setBusy(label);
    setError(null);
    try {
      const res = await work();
      docs.save({ id: res.id, token: res.accessToken, filename: res.filename, expiresAt: res.expiresAt });
      navigate(`/d/${res.id}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      setBusy(null);
    }
  };

  const onFile = (file: File | undefined) => {
    if (!file) return;
    if (file.size > config.limits.maxUploadMb * 1024 * 1024) {
      setError(`${file.name}: > ${config.limits.maxUploadMb} MB`);
      return;
    }
    const reading = openReading ? readThisFile || null : vlmPassword;
    setReadThisFile(false);
    void start(() => api.upload(file, parser, reading), 'upload');
  };

  return (
    <div className="min-h-full bg-white dark:bg-zinc-950">
      <header className="flex h-14 w-full items-center gap-2 px-6 lg:px-10">
        <Logo />
        <div className="ml-auto flex items-center gap-1">
          {config.repoUrl && (
            <a
              href={config.repoUrl}
              className="hidden items-center gap-1.5 rounded-md px-2 py-1 text-[13px] text-zinc-600 hover:bg-zinc-100 sm:flex dark:text-zinc-400 dark:hover:bg-zinc-800"
            >
              <GitBranch className="size-4" />
              {t('github')}
            </a>
          )}
          <ThemeLangButtons />
        </div>
      </header>

      <main className="w-full px-6 pt-10 pb-16 lg:px-10">
        <div className="max-w-4xl">
          <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl 2xl:text-5xl">{t('heroTitle')}</h1>
          <p className="mt-3 max-w-3xl text-[15px] leading-relaxed text-zinc-600 2xl:text-base dark:text-zinc-400">{t('heroBody')}</p>
        </div>

        <div className="mt-8 grid gap-8 lg:grid-cols-[minmax(0,1fr)_360px] 2xl:grid-cols-[minmax(0,1fr)_440px]">
          <div>
            <button
              type="button"
              disabled={!!busy}
              onClick={() => input.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragOver(false);
                onFile(e.dataTransfer.files[0]);
              }}
              className={cx(
                'flex h-56 w-full flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed transition-colors lg:h-72 2xl:h-80',
                dragOver
                  ? 'border-accent-500 bg-accent-50 dark:bg-accent-700/10'
                  : 'border-zinc-300 bg-zinc-50 hover:border-accent-400 hover:bg-accent-50/50 dark:border-zinc-700 dark:bg-zinc-900 dark:hover:bg-zinc-900/60',
              )}
              data-testid="dropzone"
            >
              {busy === 'upload' ? (
                <>
                  <Spinner className="size-6 text-accent-600" />
                  <span className="text-[14px] font-medium">{t('uploading')}</span>
                </>
              ) : (
                <>
                  <span className="flex size-12 items-center justify-center rounded-full bg-accent-100 text-accent-700 dark:bg-accent-700/30 dark:text-accent-200">
                    <FileUp className="size-6" />
                  </span>
                  <span className="text-[15px] font-medium">{t('dropTitle')}</span>
                  <span className="max-w-xl text-center text-[13px] text-zinc-600 dark:text-zinc-400">{t('dropFormats')}</span>
                  <span className="text-[13px] text-zinc-500">
                    {t(config.limits.truncateLongPdfs ? 'dropLimitsTruncate' : 'dropLimits', {
                      mb: config.limits.maxUploadMb,
                      pages: config.limits.maxPages,
                    })}
                  </span>
                </>
              )}
            </button>
            <input
              ref={input}
              type="file"
              accept={config.extensions.join(',')}
              className="hidden"
              data-testid="file-input"
              onChange={(e) => {
                onFile(e.target.files?.[0]);
                e.target.value = '';
              }}
            />
            {error && <p data-testid="upload-error" className="mt-3 text-[13px] text-red-600 dark:text-red-400">{error}</p>}

            <p data-testid="auto-pipeline" className="mt-5 flex items-start gap-2 text-[13px] text-zinc-600 dark:text-zinc-400">
              <Sparkles className="mt-0.5 size-4 shrink-0 text-accent-600" />
              {t('autoPipeline')}
            </p>

            {config.vlm?.available && !openReading && (
              <VlmUnlock model={config.vlm.model} password={vlmPassword} onChange={setVlmPassword} />
            )}

            <p className="mt-5 flex items-start gap-2 text-[13px] text-zinc-500">
              <ShieldCheck className="mt-0.5 size-4 shrink-0 text-emerald-600" />
              <span id="upload-privacy">{t(config.vlm?.available ? 'vlmPrivacy' : 'privacy', { ttl })}</span>
            </p>
            {openReading && config.vlm && (
              <label className="mt-3 flex items-center gap-2 text-[13px] font-medium">
                <input
                  type="checkbox"
                  aria-describedby="upload-privacy"
                  checked={readThisFile}
                  onChange={(e) => setReadThisFile(e.target.checked)}
                />
                {t('vlmThisFile', { model: config.vlm.model })}
              </label>
            )}
          </div>

          <aside className="space-y-6">
            <SampleGallery samples={samples} busy={busy} onOpen={(key) => void start(() => api.fromSample(key, parser), key)} />

            {recent.length > 0 && (
              <section>
                <h2 className="mb-2 text-[13px] font-medium">{t('recent')}</h2>
                <ul className="space-y-1">
                  {recent.map((d) => (
                    <li key={d.id} className="flex items-center gap-1 rounded-md hover:bg-zinc-100 dark:hover:bg-zinc-900">
                      <a
                        href={`/d/${d.id}`}
                        onClick={(e) => {
                          e.preventDefault();
                          navigate(`/d/${d.id}`);
                        }}
                        className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-[13px]"
                      >
                        <FileText className="size-4 shrink-0 text-zinc-400" />
                        <span className="flex-1 truncate">{d.filename}</span>
                        <span className="text-xs text-zinc-400">
                          {Math.max(0, Math.ceil((Date.parse(d.expiresAt) - Date.now()) / 60000))}m
                        </span>
                      </a>
                      <button
                        type="button"
                        title={t('removeRecent')}
                        aria-label={t('removeRecent')}
                        onClick={() => {
                          void api.remove(d.id, d.token).catch(() => {});
                          docs.remove(d.id);
                          setRecent(docs.list());
                        }}
                        className="mr-1 rounded p-1 text-zinc-400 hover:bg-zinc-200 hover:text-red-600 dark:hover:bg-zinc-800"
                      >
                        <X className="size-3.5" />
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </aside>
        </div>

        <section className="mt-14 grid gap-6 border-t border-zinc-200 pt-8 text-[13px] text-zinc-600 sm:grid-cols-3 dark:border-zinc-800 dark:text-zinc-400">
          {[
            ['1', lang === 'ko' ? '구조 기반 자동 청킹' : 'Structure-aware first pass', lang === 'ko' ? '제목, 목록, 표, 읽기 순서를 인식해 의미 단위로 나눕니다.' : 'Headings, lists, tables and reading order drive the initial split.'],
            ['2', lang === 'ko' ? '원본 위에서 직접 편집' : 'Edit on the original page', lang === 'ko' ? '경계를 드래그하고, 블록 사이를 클릭해 나누고, ×로 병합하세요.' : 'Drag boundaries, click between blocks to split, × to merge. Undo anytime.'],
            ['3', lang === 'ko' ? 'RAG 파이프라인으로' : 'Take it to your RAG stack', lang === 'ko' ? '블록 ID와 페이지 범위가 담긴 JSONL, JSON, Markdown으로 내보냅니다.' : 'JSONL, JSON or Markdown with block ids, page ranges and token counts.'],
          ].map(([n, title, body]) => (
            <div key={n}>
              <div className="mb-2 flex size-6 items-center justify-center rounded-full bg-zinc-900 text-xs font-semibold text-white dark:bg-zinc-100 dark:text-zinc-900">
                {n}
              </div>
              <h3 className="font-medium text-zinc-900 dark:text-zinc-100">{title}</h3>
              <p className="mt-1 leading-relaxed">{body}</p>
            </div>
          ))}
        </section>
      </main>
    </div>
  );
}

const VLM_KEY = 'chunkcanvas.vlmPassword';

/** Kept for this browser tab only. */
function savedVlmPassword(): string | null {
  try {
    return sessionStorage.getItem(VLM_KEY);
  } catch {
    return null;
  }
}

function VlmUnlock({ model, password, onChange }: { model: string; password: string | null; onChange: (p: string | null) => void }) {
  const t = useT();
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const store = (p: string | null) => {
    try {
      if (p) sessionStorage.setItem(VLM_KEY, p);
      else sessionStorage.removeItem(VLM_KEY);
    } catch {
      /* private mode: kept in memory only */
    }
    onChange(p);
  };
  const unlock = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.vlmUnlock(value);
      store(value);
      setValue('');
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section data-testid="vlm" className="mt-5 rounded-lg border border-zinc-200 p-3 dark:border-zinc-800">
      <h3 className="flex items-center gap-1.5 text-[13px] font-medium">
        <ImageIcon className="size-4 text-accent-600" />
        {t('vlmTitle')}
      </h3>
      {password ? (
        <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400">
          <span data-testid="vlm-on">{t('vlmOn', { model })}</span>
          <button type="button" onClick={() => store(null)} className="rounded border border-zinc-300 px-2 py-0.5 hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-800">
            {t('vlmOff')}
          </button>
        </p>
      ) : (
        <form
          className="mt-1"
          onSubmit={(e) => {
            e.preventDefault();
            if (value) void unlock();
          }}
        >
          <p className="text-xs leading-snug text-zinc-500">{t('vlmHint', { model })}</p>
          <div className="mt-2 flex gap-2">
            <input
              type="password"
              autoComplete="off"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder={t('vlmPassword')}
              aria-label={t('vlmPassword')}
              data-testid="vlm-password"
              className="w-48 rounded-md border border-zinc-300 bg-white px-2 py-1 text-[13px] dark:border-zinc-700 dark:bg-zinc-900"
            />
            <button
              type="submit"
              disabled={busy || !value}
              className="rounded-md bg-accent-600 px-3 py-1 text-[13px] font-medium text-white hover:bg-accent-700 disabled:opacity-50"
            >
              {busy ? <Spinner /> : t('vlmUnlock')}
            </button>
          </div>
          {error && <p data-testid="vlm-error" className="mt-1 text-xs text-red-600 dark:text-red-400">{error}</p>}
        </form>
      )}
    </section>
  );
}

const GROUP_ORDER = ['pdf', 'hwp', 'hwpx', 'docx', 'pptx', 'xlsx', 'doc', 'ppt', 'xls', 'img', 'guide'];

function SampleGallery({ samples, busy, onOpen }: { samples: Sample[]; busy: string | null; onOpen: (key: string) => void }) {
  const t = useT();
  const groups = GROUP_ORDER.filter((g) => samples.some((s) => (s.group ?? 'guide') === g));
  const [group, setGroup] = useState(() => groups[0] ?? 'guide');
  const label = (g: string) => (g === 'guide' ? t('sampleGroupGuide') : g === 'img' ? t('sampleGroupImg') : g.toUpperCase());
  return (
    <section>
      <h2 className="text-[13px] font-medium">{t('orTrySample')}</h2>
      <p className="mt-1 mb-2 text-xs leading-snug text-zinc-500">{t('samplesHint')}</p>
      {groups.length > 1 && (
        <div className="mb-2 flex flex-wrap gap-1" role="tablist">
          {groups.map((g) => (
            <button
              key={g}
              type="button"
              role="tab"
              aria-selected={group === g}
              data-sample-group={g}
              onClick={() => setGroup(g)}
              className={cx(
                'rounded-md border px-2 py-0.5 text-xs transition-colors',
                group === g
                  ? 'border-accent-500 bg-accent-50 text-accent-700 dark:bg-accent-700/20 dark:text-accent-200'
                  : 'border-zinc-200 text-zinc-600 hover:border-zinc-300 dark:border-zinc-800 dark:text-zinc-400',
              )}
            >
              {label(g)}
            </button>
          ))}
        </div>
      )}
      <div className="space-y-2">
        {samples
          .filter((s) => (s.group ?? 'guide') === group)
          .map((s) => (
            <button
              key={s.key}
              type="button"
              disabled={!!busy}
              onClick={() => onOpen(s.key)}
              className="group flex w-full items-start gap-3 rounded-lg border border-zinc-200 p-3 text-left transition-colors hover:border-accent-400 hover:bg-accent-50/40 disabled:opacity-60 dark:border-zinc-800 dark:hover:bg-zinc-900"
              data-sample={s.key}
            >
              <FileText className="mt-0.5 size-5 shrink-0 text-zinc-400 group-hover:text-accent-600" />
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-1.5 text-[13px] font-medium">
                  <span className="truncate">{s.title}</span>
                  <span className="shrink-0 rounded bg-zinc-100 px-1 text-[10px] text-zinc-500 uppercase dark:bg-zinc-800">{s.format}</span>
                  <span className="shrink-0 rounded bg-zinc-100 px-1 text-[10px] text-zinc-500 uppercase dark:bg-zinc-800">{s.language}</span>
                </span>
                <span className="mt-0.5 block text-xs leading-snug text-zinc-500">{s.description}</span>
              </span>
              {busy === s.key ? <Spinner className="mt-1 size-3.5" /> : <ArrowRight className="mt-1 size-4 text-zinc-300 group-hover:text-accent-600" />}
            </button>
          ))}
      </div>
    </section>
  );
}
