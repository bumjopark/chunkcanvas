// Copy pdf.js CMaps, standard fonts, wasm decoders and ICC profiles into public/pdfjs so PDFs
// with non-embedded (e.g. CJK) fonts or JBIG2/JPX images render without any external CDN.
import { cpSync, existsSync, mkdirSync, rmSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';

const require = createRequire(import.meta.url);
const root = dirname(require.resolve('pdfjs-dist/package.json'));
const out = new URL('../public/pdfjs/', import.meta.url).pathname;
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
for (const dir of ['cmaps', 'standard_fonts', 'wasm', 'iccs']) {
  const src = join(root, dir);
  if (existsSync(src)) cpSync(src, join(out, dir), { recursive: true });
}
console.log('pdf.js assets copied to public/pdfjs');
