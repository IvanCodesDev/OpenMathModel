/**
 * PDF 文本抽取。复用论文阅读器已经引入的 pdfjs-dist，按需动态加载，
 * 没有拖 PDF 进来的用户不必为这份 worker 买单。
 */

import { MAX_PDF_PAGES } from "./limits";
import { tidyText } from "./text-decode";

export interface PdfExtraction {
  text: string;
  pages: number;
  /** 实际抽取的页数，触顶时小于 pages */
  extractedPages: number;
}

export async function extractPdfText(bytes: Uint8Array): Promise<PdfExtraction> {
  const [{ getDocument, GlobalWorkerOptions }, { default: workerUrl }] = await Promise.all([
    import("pdfjs-dist"),
    import("pdfjs-dist/build/pdf.worker.min.mjs?url"),
  ]);
  GlobalWorkerOptions.workerSrc = workerUrl;

  // pdfjs 会接管并分离传入的缓冲区，这里给它一份副本，原文件仍要留着上传。
  const pdf = await getDocument({ data: bytes.slice() }).promise;
  try {
    const extractedPages = Math.min(pdf.numPages, MAX_PDF_PAGES);
    const pages: string[] = [];
    for (let pageNumber = 1; pageNumber <= extractedPages; pageNumber += 1) {
      const page = await pdf.getPage(pageNumber);
      const content = await page.getTextContent();
      const lines: string[] = [];
      let line = "";
      for (const item of content.items) {
        if (!("str" in item)) continue;
        line += item.str;
        if (item.hasEOL) {
          lines.push(line);
          line = "";
        }
      }
      if (line) lines.push(line);
      page.cleanup();
      pages.push(lines.join("\n"));
    }
    return { text: tidyText(pages.join("\n\n")), pages: pdf.numPages, extractedPages };
  } finally {
    await pdf.destroy();
  }
}
