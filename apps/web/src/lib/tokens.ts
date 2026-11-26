import { memoizeCounter, type Tokenizer } from '@chunkcanvas/core';

export type TokenizerName = 'cl100k_base' | 'o200k_base';
export const TOKENIZERS: { name: TokenizerName; label: string }[] = [
  { name: 'cl100k_base', label: 'cl100k_base (GPT-4, text-embedding-3)' },
  { name: 'o200k_base', label: 'o200k_base (GPT-4o; fewer tokens for Korean/CJK)' },
];

const loading = new Map<TokenizerName, Promise<Tokenizer>>();

/** Loads BPE ranks lazily (about 1 MB each) and returns a tokenizer with a memoized counter. */
export function loadTokenizer(name: TokenizerName): Promise<Tokenizer> {
  let p = loading.get(name);
  if (!p) {
    const ranks = name === 'o200k_base' ? import('js-tiktoken/ranks/o200k_base') : import('js-tiktoken/ranks/cl100k_base');
    p = Promise.all([import('js-tiktoken/lite'), ranks]).then(([{ Tiktoken }, r]) => {
      const enc = new Tiktoken(r.default);
      return {
        name,
        count: memoizeCounter((text) => enc.encode(text, [], []).length, 20000),
        encode: (text: string) => enc.encode(text, [], []),
        decode: (ids: number[]) => enc.decode(ids),
      };
    });
    loading.set(name, p);
  }
  return p;
}

export function savedTokenizer(): TokenizerName {
  try {
    const v = JSON.parse(localStorage.getItem('chunkcanvas.tokenizer') ?? 'null');
    if (v === 'o200k_base' || v === 'cl100k_base') return v;
  } catch {
    /* ignore */
  }
  return 'cl100k_base';
}
