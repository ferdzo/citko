// PDF.js's legacy build is the officially supported compatibility build: it bundles the polyfills
// (e.g. Map.prototype.getOrInsertComputed) that the modern build assumes the browser already ships.
// Without it, the viewer throws at construction on currently released browsers.
import { getDocument, GlobalWorkerOptions, type PDFDocumentLoadingTask, type PDFDocumentProxy } from 'pdfjs-dist/legacy/build/pdf.mjs';
import { EventBus, PDFFindController, PDFLinkService, PDFViewer, ScrollMode } from 'pdfjs-dist/legacy/web/pdf_viewer.mjs';
import workerUrl from 'pdfjs-dist/legacy/build/pdf.worker.min.mjs?url';
import type { Annotation, Locator, ReaderPreferences } from '../types';
import { clamp, colors, type Chapter, type EngineHooks, type Found, type ReaderEngine } from './engine';

GlobalWorkerOptions.workerSrc = workerUrl;
type OutlineItem = { title: string; dest: string | unknown[] | null; items: OutlineItem[] };

export class PdfEngine implements ReaderEngine {
  chapters: Chapter[] = [];
  current: { locator: Locator; progress: number } | null = null;
  private container: HTMLDivElement | null = null;
  private viewer: PDFViewer | null = null;
  private bus: EventBus | null = null;
  private find: PDFFindController | null = null;
  private link: PDFLinkService | null = null;
  private document: PDFDocumentProxy | null = null;
  private task: PDFDocumentLoadingTask | null = null;
  private blobUrl: string | null = null;
  private annotations: Annotation[] = [];
  private stopped = false;
  private searchId = 0;
  private prefs: ReaderPreferences;
  private outline = new Map<string, string | unknown[]>();
  private starts: number[] | null = null;
  private lastLocator: Locator | null = null;
  constructor(private hooks: EngineHooks, prefs: ReaderPreferences) { this.prefs = prefs; }

  async open(host: HTMLElement, source: string | Blob, initial: Locator | null) {
    const container = document.createElement('div');
    container.className = 'citko-pdf-scroll';
    container.setAttribute('aria-label', 'PDF pages');
    container.tabIndex = 0;
    const pages = document.createElement('div'); pages.className = 'pdfViewer';
    container.append(pages); host.append(container); this.container = container;
    const bus = new EventBus(); this.bus = bus;
    const link = new PDFLinkService({ eventBus: bus }); this.link = link;
    link.externalLinkEnabled = false;
    const find = new PDFFindController({ eventBus: bus, linkService: link }); this.find = find;
    const viewer = new PDFViewer({ container, viewer: pages, eventBus: bus, linkService: link, findController: find, textLayerMode: 1,
      removePageBorders: false }); this.viewer = viewer;
    link.setViewer(viewer);
    bus.on('pagesinit', this.pagesReady);
    bus.on('pagechanging', this.changed);
    bus.on('pagerendered', this.renderAnnotations);
    container.addEventListener('scroll', this.changed, { passive: true });
    container.addEventListener('pointerup', this.selected);
    container.addEventListener('keyup', this.selected);
    container.addEventListener('pointerdown', this.interact);
    container.addEventListener('keydown', this.interact);
    // Keep the original authenticated URL for HTTP byte-range loading. Offline uses a local blob URL only.
    const url = typeof source === 'string' ? source : URL.createObjectURL(source);
    if (typeof source !== 'string') this.blobUrl = url;
    const task = getDocument({ url, withCredentials: true, rangeChunkSize: 65536 });
    this.task = task;
    const pdf = await task.promise;
    if (this.stopped) return;
    this.document = pdf;
    viewer.setDocument(pdf); link.setDocument(pdf); find.setDocument(pdf);
    this.chapters = [];
    try {
      const outline = await pdf.getOutline() as OutlineItem[] | null;
      if (this.stopped) return;
      const visit = (items: OutlineItem[], depth: number) => { for (const item of items) {
        const id = String(this.outline.size);
        if (item.dest) { this.outline.set(id, item.dest); this.chapters.push({ label: item.title, href: id, depth }); }
        visit(item.items ?? [], depth + 1);
      } };
      visit(outline ?? [], 0);
    } catch { /* A broken optional outline should not prevent page rendering. */ }
    await this.resolveChapterStarts(pdf);
    if (this.stopped) return;
    await viewer.firstPagePromise;
    if (this.stopped) return;
    this.applyPreferences(this.prefs);
    if (initial?.type === 'pdf') this.go(initial);
    else this.changed();
  }
  /**
   * Chapter boundaries in the same fraction space as PDF progress. Each outline entry points at a
   * destination whose page index gives the chapter's start; a destination that cannot be resolved
   * inherits the previous start, which the shared chapterSpans helper then keeps monotonic.
   */
  private async resolveChapterStarts(pdf: PDFDocumentProxy) {
    const total = Math.max(1, pdf.numPages);
    const starts: number[] = [];
    for (const chapter of this.chapters) {
      let page = -1;
      try {
        const destination = this.outline.get(chapter.href);
        const ref = Array.isArray(destination) ? destination[0] : destination;
        if (ref) page = await pdf.getPageIndex(ref as Parameters<PDFDocumentProxy['getPageIndex']>[0]);
      } catch { page = -1; }
      starts.push(page >= 0 ? clamp(page / total, 0, 1) : (starts[starts.length - 1] ?? 0));
    }
    this.starts = this.chapters.length ? starts : null;
  }
  chapterStarts() { return this.starts; }
  private pagesReady = () => { if (this.stopped) return; this.applyPreferences(this.prefs); };
  private pageElement(page: number) { return this.container?.querySelector<HTMLElement>(`.pdfViewer .page[data-page-number="${page + 1}"]`) ?? null; }
  private changed = () => {
    const container = this.container, viewer = this.viewer, doc = this.document;
    if (!container || !viewer || !doc || this.stopped) return;
    const page = clamp(viewer.currentPageNumber - 1, 0, doc.numPages - 1);
    const el = this.pageElement(page);
    const y = el ? clamp((container.scrollTop - el.offsetTop) / Math.max(1, el.offsetHeight), 0, 1) : 0;
    const locator: Locator = { type: 'pdf', page, y };
    if (this.lastLocator?.type === 'pdf' && this.lastLocator.page === page && Math.abs(this.lastLocator.y - y) < .005) return;
    this.lastLocator = locator;
    const progress = clamp((page + y) / doc.numPages, 0, 1);
    this.current = { locator, progress };
    this.hooks.onPosition(locator, progress);
  };
  private interact = () => this.hooks.onInteract();
  private selected = (event: Event) => {
    this.interact();
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount || !this.container) {
      if (event.type === 'pointerup') this.clickedAnnotation(event as PointerEvent);
      return;
    }
    const range = selection.getRangeAt(0);
    const quote = selection.toString().trim().slice(0, 4000);
    if (!quote) return;
    const rectangles: { page: number; x: number; y: number; width: number; height: number }[] = [];
    for (const rect of Array.from(range.getClientRects())) {
      if (!rect.width || !rect.height) continue;
      for (const page of Array.from(this.container.querySelectorAll<HTMLElement>('.pdfViewer .page'))) {
        if (!page.contains(range.startContainer) && !page.contains(range.endContainer) && !range.intersectsNode(page)) continue;
        const box = page.getBoundingClientRect();
        const left = Math.max(rect.left, box.left), top = Math.max(rect.top, box.top);
        const right = Math.min(rect.right, box.right), bottom = Math.min(rect.bottom, box.bottom);
        if (right <= left || bottom <= top) continue;
        rectangles.push({ page: Number(page.dataset.pageNumber) - 1,
          x: clamp((left - box.left) / box.width, 0, 1), y: clamp((top - box.top) / box.height, 0, 1),
          width: clamp((right - left) / box.width, 0, 1), height: clamp((bottom - top) / box.height, 0, 1) });
      }
    }
    if (!rectangles.length) return;
    const first = rectangles[0];
    this.hooks.onSelection({ locator: { type: 'pdf', page: first.page, y: first.y }, anchor: { rects: rectangles, quote }, quote });
  };
  private clickedAnnotation = (event: PointerEvent) => {
    const page = (event.target as HTMLElement).closest<HTMLElement>('.pdfViewer .page');
    if (!page) return;
    const box = page.getBoundingClientRect();
    const x = (event.clientX - box.left) / box.width, y = (event.clientY - box.top) / box.height;
    for (const a of [...this.annotations].reverse()) {
      if (!a.anchor || !('rects' in a.anchor)) continue;
      if (a.anchor.rects.some(r => r.page === Number(page.dataset.pageNumber) - 1 && x >= r.x && x <= r.x + r.width && y >= r.y && y <= r.y + r.height)) {
        this.hooks.onAnnotationClick(a.id); return;
      }
    }
  };
  private renderAnnotations = () => {
    if (!this.container) return;
    this.container.querySelectorAll('.citko-pdf-annotation').forEach(el => el.remove());
    for (const item of this.annotations) {
      if (!item.anchor || !('rects' in item.anchor) || item.type === 'bookmark') continue;
      for (const rect of item.anchor.rects) {
        const page = this.pageElement(rect.page);
        if (!page) continue;
        const mark = document.createElement('span'); mark.className = 'citko-pdf-annotation';
        mark.style.cssText = `left:${clamp(rect.x, 0, 1) * 100}%;top:${clamp(rect.y, 0, 1) * 100}%;width:${clamp(rect.width, 0, 1) * 100}%;height:${clamp(rect.height, 0, 1) * 100}%;background:${colors[item.color]}`;
        page.append(mark);
      }
    }
  };
  setAnnotations(items: Annotation[]) { this.annotations = items.filter(a => !a.deleted); this.renderAnnotations(); }
  applyPreferences(prefs: ReaderPreferences) {
    this.prefs = prefs;
    if (!this.viewer) return;
    this.viewer.scrollMode = prefs.pdfMode === 'single' ? ScrollMode.PAGE : ScrollMode.VERTICAL;
    this.viewer.currentScaleValue = 'page-width';
    this.renderAnnotations();
  }
  zoom(factor: number | 'width') {
    if (!this.viewer) return;
    if (factor === 'width') this.viewer.currentScaleValue = 'page-width';
    else this.viewer.currentScale = clamp(this.viewer.currentScale * factor, .35, 4);
    this.renderAnnotations();
  }
  go(locator: Locator) {
    if (locator.type !== 'pdf' || !this.viewer || !this.document || !this.container) return;
    const page = clamp(Math.floor(locator.page), 0, this.document.numPages - 1);
    const y = clamp(locator.y, 0, 1);
    this.viewer.currentPageNumber = page + 1;
    // Wait for PDFViewer to lay out page elements before restoring the exact normalized top offset.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const el = this.pageElement(page);
      if (!el || !this.container) return;
      this.container.scrollTop = el.offsetTop + y * el.offsetHeight;
      this.changed();
    }));
  }
  next() { if (!this.viewer || !this.document) return; this.go({ type: 'pdf', page: Math.min(this.document.numPages - 1, this.viewer.currentPageNumber), y: 0 }); }
  prev() { if (!this.viewer) return; this.go({ type: 'pdf', page: Math.max(0, this.viewer.currentPageNumber - 2), y: 0 }); }
  async goChapter(chapter: Chapter) {
    const dest = this.outline.get(chapter.href);
    if (dest) await this.link?.goToDestination(dest as Parameters<PDFLinkService['goToDestination']>[0]);
  }
  async search(query: string, onResult: (found: Found) => void, onProgress: (fraction: number) => void) {
    const id = ++this.searchId;
    if (!this.document || !query.trim()) return;
    this.bus?.dispatch('find', { source: this, query, type: '', caseSensitive: false, entireWord: false,
      highlightAll: true, findPrevious: false, matchDiacritics: false });
    const needle = query.trim().toLocaleLowerCase();
    for (let i = 0; i < this.document.numPages; i++) {
      if (this.stopped || id !== this.searchId) return;
      const page = await this.document.getPage(i + 1);
      const content = await page.getTextContent();
      const text = content.items.map(item => 'str' in item ? item.str : '').join(' ');
      let index = -1;
      while ((index = text.toLocaleLowerCase().indexOf(needle, index + 1)) >= 0) {
        onResult({ label: `Page ${i + 1}`, excerpt: text.slice(Math.max(0, index - 48), Math.min(text.length, index + needle.length + 70)), locator: { type: 'pdf', page: i, y: 0 } });
        if (id !== this.searchId) return;
      }
      onProgress((i + 1) / this.document.numPages);
      await new Promise(resolve => setTimeout(resolve, 0));
    }
  }
  cancelSearch() { this.searchId++; this.bus?.dispatch('findbarclose', { source: this }); }
  close() {
    this.stopped = true; this.cancelSearch();
    this.bus?.off('pagesinit', this.pagesReady); this.bus?.off('pagechanging', this.changed); this.bus?.off('pagerendered', this.renderAnnotations);
    this.container?.removeEventListener('scroll', this.changed);
    this.container?.removeEventListener('pointerup', this.selected); this.container?.removeEventListener('keyup', this.selected);
    this.container?.removeEventListener('pointerdown', this.interact); this.container?.removeEventListener('keydown', this.interact);
    this.viewer?.setDocument(null as unknown as PDFDocumentProxy);
    this.find?.setDocument(null as unknown as PDFDocumentProxy);
    this.link?.setDocument(null);
    void this.task?.destroy();
    this.container?.remove(); this.container = null; this.viewer = null; this.document = null;
    if (this.blobUrl) URL.revokeObjectURL(this.blobUrl);
  }
}
