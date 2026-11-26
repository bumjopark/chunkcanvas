import type { ParsedDocument, SplitPlan } from '@chunkcanvas/core';

export type DocStatus = 'queued' | 'processing' | 'completed' | 'failed';

export interface ParserInfo {
  id: string;
  label: string;
  description: string;
  maxPages: number;
  version: string;
}

export interface AppConfig {
  version: string;
  defaultParser: string;
  parsers: ParserInfo[];
  limits: { maxUploadMb: number; maxPages: number; truncateLongPdfs: boolean; documentTtlSeconds: number };
  repoUrl: string | null;
  /** Accepted upload formats and their file extensions. */
  formats: string[];
  extensions: string[];
  /** Picture reading by a vision-language model, for visitors with the password. */
  vlm?: { available: boolean; model: string };
}

export interface Sample {
  key: string;
  title: string;
  language: string;
  description: string;
  /** Gallery tab: a file type ("pdf", "hwp", "img", ...), or "guide" for the basic samples. */
  group?: string;
  /** Format of the original file ("pdf", "hwpx", "pptx", ...). */
  format: string;
}

export interface StatusPayload {
  id: string;
  status: DocStatus;
  parser: string;
  filename: string;
  pageCount: number;
  progress: { done: number; total: number; stage: 'converting' | 'parsing' } | null;
  queuePosition: number | null;
  error: { code: string; message: string } | null;
  createdAt: string;
  expiresAt: string;
  revision: number;
  sample: boolean;
  /** Pages in the uploaded file when only the first pageCount pages were kept. */
  sourcePageCount: number | null;
  /** How processing differed from the request: "auto_ocr", "mixed_ocr", "ocr_skipped", "formulas_partial", "vlm_figures", "vlm_partial", "text_fallback", "page_limit", "csv_rows_truncated", "csv_records". */
  notices: string[];
  /** Original format when the upload was converted to PDF (e.g. "docx"); null for PDFs. */
  sourceFormat: string | null;
}

export interface DocumentPayload extends StatusPayload {
  parsed: ParsedDocument | null;
  splitPlan: SplitPlan | null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly detail: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit & { token?: string } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.token) headers.set('Authorization', `Bearer ${init.token}`);
  let res: Response;
  try {
    res = await fetch(path, { ...init, headers });
  } catch {
    throw new ApiError(0, 'network', 'Could not reach the server.');
  }
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const d = (body?.detail ?? {}) as Record<string, unknown>;
    const message = typeof d.message === 'string' ? d.message : `Request failed (${res.status}).`;
    throw new ApiError(res.status, typeof d.code === 'string' ? d.code : 'error', message, d);
  }
  return body as T;
}

export const api = {
  config: () => request<AppConfig>('/api/config'),
  samples: () => request<Sample[]>('/api/samples'),
  fromSample: (key: string, parser: string) =>
    request<StatusPayload & { accessToken: string }>(
      `/api/samples/${encodeURIComponent(key)}/documents?parser=${encodeURIComponent(parser)}`,
      { method: 'POST' },
    ),
  upload: (file: File, parser: string, vlmPassword?: string | null) =>
    request<StatusPayload & { accessToken: string }>(`/api/documents?parser=${encodeURIComponent(parser)}`, {
      method: 'POST',
      body: file,
      headers: {
        'Content-Type': file.type || 'application/octet-stream',
        'X-Filename': encodeURIComponent(file.name),
        ...(vlmPassword ? { 'X-VLM-Password': vlmPassword } : {}),
      },
    }),
  vlmUnlock: (password: string) =>
    request<{ ok: boolean; model: string }>('/api/vlm/unlock', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    }),
  status: (id: string, token: string) => request<StatusPayload>(`/api/documents/${id}/status`, { token }),
  document: (id: string, token: string) => request<DocumentPayload>(`/api/documents/${id}`, { token }),
  file: async (id: string, token: string, signal?: AbortSignal): Promise<ArrayBuffer> => {
    const res = await fetch(`/api/documents/${id}/file`, { signal, headers: { Authorization: `Bearer ${token}` } });
    if (!res.ok) throw new ApiError(res.status, 'file', 'Could not load the PDF.');
    return res.arrayBuffer();
  },
  savePlan: (id: string, token: string, baseRevision: number, plan: SplitPlan) =>
    request<{ revision: number }>(`/api/documents/${id}/split-plan`, {
      method: 'PUT',
      token,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ baseRevision, plan }),
    }),
  reparse: (id: string, token: string, parser: string) =>
    request<StatusPayload>(`/api/documents/${id}/parse?parser=${encodeURIComponent(parser)}`, { method: 'POST', token }),
  remove: (id: string, token: string) => request<void>(`/api/documents/${id}`, { method: 'DELETE', token }),
};
