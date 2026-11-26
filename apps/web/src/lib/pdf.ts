import type { PDFDocumentProxy } from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';

export interface PdfHandle {
  pdf: PDFDocumentProxy;
  destroy: () => Promise<void>;
  /** Page sizes in CSS px at zoom 1 (PDF points, CropBox with /Rotate applied). */
  sizes: { width: number; height: number }[];
}

export async function openPdf(data: ArrayBuffer, signal?: AbortSignal): Promise<PdfHandle> {
  const pdfjs = await import('pdfjs-dist');
  pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;
  const task = pdfjs.getDocument({
    data,
    cMapUrl: '/pdfjs/cmaps/',
    cMapPacked: true,
    standardFontDataUrl: '/pdfjs/standard_fonts/',
    wasmUrl: '/pdfjs/wasm/',
    iccUrl: '/pdfjs/iccs/',
  });
  const cancel = () => { void task.destroy(); };
  signal?.addEventListener('abort', cancel, { once: true });
  try {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    const pdf = await task.promise;
    const sizes = [];
    for (let i = 1; i <= pdf.numPages; i++) {
      const page = await pdf.getPage(i);
      const vp = page.getViewport({ scale: 1 });
      sizes.push({ width: vp.width, height: vp.height });
    }
    return { pdf, sizes, destroy: () => task.destroy() };
  } catch (error) {
    await task.destroy();
    throw error;
  } finally {
    signal?.removeEventListener('abort', cancel);
  }
}
