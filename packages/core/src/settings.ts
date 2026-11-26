import type { ChunkSettings } from './types';

/** The editing contract shared by settings, imports and saved preferences. */
export function isChunkSettings(value: unknown): value is ChunkSettings {
  if (!value || typeof value !== 'object') return false;
  const s = value as ChunkSettings;
  return Number.isInteger(s.targetTokens) && s.targetTokens >= 20 && s.targetTokens <= 8000
    && Number.isInteger(s.maxTokens) && s.maxTokens >= s.targetTokens && s.maxTokens <= 16000
    && Number.isInteger(s.overlapTokens) && s.overlapTokens >= 0 && s.overlapTokens <= 2000
    && typeof s.structureAware === 'boolean' && typeof s.keepHeadingWithBody === 'boolean'
    && typeof s.excludeFurniture === 'boolean';
}

/** Copy only the public settings contract. Extra keys must never reach API writes. */
export function normalizeChunkSettings(value: unknown): ChunkSettings | null {
  if (!value || typeof value !== 'object') return null;
  const candidate = { overlapTokens: 0, ...value };
  if (!isChunkSettings(candidate)) return null;
  const { targetTokens, maxTokens, overlapTokens, structureAware, keepHeadingWithBody, excludeFurniture } = candidate;
  return { targetTokens, maxTokens, overlapTokens, structureAware, keepHeadingWithBody, excludeFurniture };
}
