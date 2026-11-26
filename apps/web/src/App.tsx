import { AlertTriangle, FileX } from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { Editor } from './components/Editor';
import { Home } from './components/Home';
import { Logo, ThemeLangButtons } from './components/Toolbar';
import { Button, Spinner } from './components/ui';
import { ApiError, api, type AppConfig, type DocumentPayload, type Sample, type StatusPayload } from './lib/api';
import { docs } from './lib/docs';
import { useEditor } from './store';
import { formatDuration, translate, useLang, useT } from './lib/i18n';

function confirmNavigation(): boolean {
  const st = useEditor.getState();
  return !st.doc || !st.history || st.saveStatus === 'saved' || confirm(translate(useLang.getState().lang, 'leaveUnsaved'));
}

function usePath(): [string, (path: string) => void] {
  const [path, setPath] = useState(location.pathname);
  const current = useRef({path: location.pathname, index: Number(history.state?.ccIndex ?? 0)});
  useEffect(() => {
    history.replaceState({...history.state, ccIndex: current.current.index}, '', location.href);
    let restoring = false;
    const onPop = () => {
      if (restoring) { restoring = false; return; }
      const next = {path: location.pathname, index: Number(history.state?.ccIndex ?? current.current.index)};
      if (next.path !== current.current.path && !confirmNavigation()) {
        const delta = current.current.index-next.index;
        if (delta) { restoring = true; history.go(delta); }
        else history.replaceState({...history.state, ccIndex:current.current.index}, '', current.current.path);
        return;
      }
      current.current = next;
      setPath(next.path);
    };
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);
  const navigate = useCallback((to: string) => {
    if (to !== current.current.path && !confirmNavigation()) return;
    if (to !== location.pathname) {
      current.current = {path:to, index:current.current.index+1};
      history.pushState({ccIndex:current.current.index}, '', to);
    }
    setPath(to);
  }, []);
  return [path, navigate];
}

export function App() {
  const [path, navigate] = usePath();
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [samples, setSamples] = useState<Sample[]>([]);
  const [error, setError] = useState<string | null>(null);
  const lang = useLang((s) => s.lang);

  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  useEffect(() => {
    Promise.all([api.config(), api.samples()])
      .then(([c, s]) => {
        setConfig(c);
        setSamples(s);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  if (error) return <Screen icon={<AlertTriangle className="size-8 text-red-500" />} title={error} />;
  if (!config) return <Screen icon={<Spinner className="size-6 text-accent-600" />} />;

  const match = /^\/d\/([A-Za-z0-9_-]{16,64})\/?$/.exec(path);
  if (match) return <DocumentRoute key={match[1]} id={match[1]!} config={config} navigate={navigate} />;
  return <Home config={config} samples={samples} navigate={navigate} />;
}

function Screen({ icon, title, body, children }: { icon?: ReactNode; title?: string; body?: string; children?: ReactNode }) {
  return (
    <div className="flex h-full flex-col">
      <header className="flex h-12 shrink-0 items-center gap-2 px-4">
        <Logo />
        <div className="ml-auto flex">
          <ThemeLangButtons />
        </div>
      </header>
      <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 pb-24 text-center">
        {icon}
        {title && <h1 className="text-lg font-semibold">{title}</h1>}
        {body && <p className="max-w-md text-[13px] text-zinc-500">{body}</p>}
        {children}
      </div>
    </div>
  );
}

type RouteState =
  | { kind: 'loading' }
  | { kind: 'waiting'; status: StatusPayload }
  | { kind: 'failed'; status: StatusPayload }
  | { kind: 'ready'; doc: DocumentPayload }
  | { kind: 'gone' };

function DocumentRoute({ id, config, navigate }: { id: string; config: AppConfig; navigate: (p: string) => void }) {
  const t = useT();
  const lang = useLang((s) => s.lang);
  const stored = docs.get(id);
  const [state, setState] = useState<RouteState>(stored ? { kind: 'loading' } : { kind: 'gone' });
  const [poll, setPoll] = useState(0);

  const gone = useCallback(() => {
    docs.remove(id);
    setState({ kind: 'gone' });
  }, [id]);

  useEffect(() => {
    if (!stored) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const status = await api.status(id, stored.token);
        if (cancelled) return;
        if (status.status === 'completed') {
          const doc = await api.document(id, stored.token);
          if (!cancelled) {
            docs.save({ ...stored, expiresAt: doc.expiresAt, filename: doc.filename });
            setState({ kind: 'ready', doc });
          }
          return;
        }
        if (status.status === 'failed') {
          setState({ kind: 'failed', status });
          return;
        }
        setState({ kind: 'waiting', status });
        timer = setTimeout(tick, 1000);
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 404) gone();
        else timer = setTimeout(tick, 3000);
      }
    };
    void tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [id, stored?.token, poll, gone]);

  const reparse = async (parser: string) => {
    if (!stored) return;
    try {
      await api.reparse(id, stored.token, parser);
      setState({ kind: 'loading' });
      setPoll((n) => n + 1);
    } catch (e) {
      alert((e as Error).message);
    }
  };

  const remove = async (confirmFirst = true) => {
    if (!stored || (confirmFirst && !confirm(t('deleteConfirm')))) return;
    try {
      await api.remove(id, stored.token);
    } catch {
      /* already gone */
    }
    docs.remove(id);
    useEditor.getState().reset();
    navigate('/');
  };

  const ttl = formatDuration(lang, config.limits.documentTtlSeconds);
  switch (state.kind) {
    case 'gone':
      return (
        <Screen icon={<FileX className="size-8 text-zinc-400" />} title={t('notFound')} body={t('notFoundBody', { ttl })}>
          <Button variant="primary" onClick={() => navigate('/')}>
            {t('backHome')}
          </Button>
        </Screen>
      );
    case 'loading':
      return <Screen icon={<Spinner className="size-6 text-accent-600" />} />;
    case 'waiting': {
      const s = state.status;
      const pct = s.progress ? (s.progress.done / Math.max(1, s.progress.total)) * 100 : 0;
      return (
        <Screen
          icon={<Spinner className="size-6 text-accent-600" />}
          title={
            s.status === 'queued'
              ? t('queued')
              : s.progress?.stage === 'converting'
                ? t('converting', { format: (s.sourceFormat ?? '').toUpperCase() })
                : t('processing')
          }
          body={s.filename}
        >
          <div className="w-72">
            <div className="h-1.5 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800">
              <div
                className="h-full rounded-full bg-accent-600 transition-[width] duration-500"
                style={{ width: `${s.status === 'queued' ? 0 : Math.max(4, pct)}%` }}
              />
            </div>
            <p className="mt-2 text-xs text-zinc-500" data-testid="progress-text">
              {s.status === 'queued'
                ? s.queuePosition
                  ? t('queuedAhead', { n: s.queuePosition })
                  : t('preparing')
                : s.progress && s.progress.stage !== 'converting'
                  ? t('pagesDone', { done: s.progress.done, total: s.progress.total })
                  : t('preparing')}
              {' · '}
              {config.parsers.find((p) => p.id === s.parser)?.label ?? s.parser}
            </p>
            {s.notices.includes('auto_ocr') && <p className="mt-2 text-xs text-sky-700 dark:text-sky-400">{t('autoOcr')}</p>}
            {s.sourcePageCount && (
              <p className="mt-2 text-xs text-sky-700 dark:text-sky-400">
                {t('truncatedBanner', { pages: s.pageCount, total: s.sourcePageCount })}
              </p>
            )}
          </div>
          <Button variant="ghost" onClick={() => void remove(false)}>
            {t('cancel')}
          </Button>
        </Screen>
      );
    }
    case 'failed': {
      const s = state.status;
      return (
        <Screen icon={<AlertTriangle className="size-8 text-red-500" />} title={t('failed')} body={s.error?.message ?? ''}>
          <div className="flex flex-wrap justify-center gap-2">
            {config.parsers
              .filter((p) => p.id !== s.parser && s.pageCount <= p.maxPages)
              .map((p) => (
                <Button key={p.id} onClick={() => void reparse(p.id)}>
                  {t('tryOtherParser')}: {p.label}
                </Button>
              ))}
            <Button variant="ghost" onClick={() => void remove(false)}>
              {t('backHome')}
            </Button>
          </div>
        </Screen>
      );
    }
    case 'ready':
      return (
        <Editor
          payload={state.doc}
          token={stored!.token}
          parsers={config.parsers.filter((p) => state.doc.pageCount <= p.maxPages)}
          repoUrl={config.repoUrl}
          onGone={gone}
          onDelete={() => void remove(true)}
        />
      );
  }
}
