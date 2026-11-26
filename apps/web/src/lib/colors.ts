/** Chunk colors chosen to stay distinguishable on white PDF pages in both themes. */
const HUES = [221, 32, 152, 280, 0, 190, 48, 330];

export function chunkHue(index: number): number {
  return HUES[index % HUES.length]!;
}

export function chunkFill(index: number, strength: 'idle' | 'hover' | 'selected'): string {
  const alpha = strength === 'selected' ? 0.3 : strength === 'hover' ? 0.22 : 0.13;
  return `hsla(${chunkHue(index)}, 85%, 50%, ${alpha})`;
}

export function chunkStroke(index: number, strong = false): string {
  return `hsla(${chunkHue(index)}, 80%, ${strong ? 38 : 45}%, ${strong ? 0.95 : 0.55})`;
}

export function chunkSolid(index: number): string {
  return `hsl(${chunkHue(index)}, 75%, 42%)`;
}
