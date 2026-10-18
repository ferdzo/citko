import type { Annotation, Locator, ReaderPreferences } from '../types';
import { clamp, colors, flattenChapters, type Chapter, type EngineHooks, type Found, type ReaderEngine } from './engine';

type FoliateDoc = Document & { defaultView: Window };
type FoliateView = HTMLElement & {
  book: { toc?: { label?: string; href?: string; subitems?: unknown[] }[]; sections: { id?: string }[]; destroy?: () => void };
  renderer: { setStyles?: (css: string) => void; setAttribute: (name: string, value: string) => void;
    getContents: () => { doc: FoliateDoc; index: number }[] };
  open: (file: Blob) => Promise<void>;
  init: (opts: { lastLocation?: string }) => Promise<void>;
  close: () => void;
  next: () => Promise<void>; prev: () => Promise<void>; goTo: (target: string) => Promise<unknown>;
  /** Cumulative book fraction at the start of every spine section (length = sections + 1). */
  getSectionFractions?: () => number[];
  getCFI: (index: number, range: Range) => string;
  addAnnotation: (annotation: { value: string }, remove?: boolean) => Promise<unknown>;
  search: (opts: { query: string }) => AsyncIterable<string | { progress?: number; label?: string; subitems?: { cfi: string; excerpt: { pre: string; match: string; post: string } }[] }>;
  clearSearch: () => void;
};
type Relocate = { cfi?: string; fraction?: number };

const family = (name: string) => ({ Georgia: 'Georgia, "Times New Roman", serif', 'Literary serif': '"Iowan Old Style", "Baskerville", Georgia, serif',
  'Modern serif': '"Palatino Linotype", Palatino, Georgia, serif', Sans: '"Avenir Next", "Segoe UI", sans-serif', Mono: 'ui-monospace, monospace' })[name] ?? 'Georgia, serif';

export class FoliateEngine implements ReaderEngine {
  chapters: Chapter[] = [];
  current: { locator: Locator; progress: number } | null = null;
  private view: FoliateView | null = null;
  private cancelled = false;
  private docs = new Map<Document, () => void>();
  private annotations: Annotation[] = [];
  private prefs: ReaderPreferences;
  private alive = true;
  constructor(private hooks: EngineHooks, preferences: ReaderPreferences, public extension: string) { this.prefs = preferences; }

  async open(host: HTMLElement, source: string | Blob, initial: Locator | null) {
    // Foliate's zip loader works on Blob; a remote file is downloaded once, never for page turns.
    const response = typeof source === 'string' ? await fetch(source, { credentials: 'same-origin' }) : null;
    if (response && !response.ok) throw new Error(`Could not open book (${response.status}).`);
    const blob = response ? await response.blob() : source as Blob;
    if (!this.alive) return;
    // @ts-expect-error Vendored pinned JavaScript does not publish TypeScript declarations.
    await import('../../vendor/foliate/view.js');
    if (!this.alive) return;
    const view = document.createElement('foliate-view') as FoliateView;
    this.view = view;
    view.addEventListener('relocate', this.relocated as EventListener);
    view.addEventListener('load', this.loaded as EventListener);
    view.addEventListener('create-overlay', this.overlay as EventListener);
    view.addEventListener('draw-annotation', this.draw as EventListener);
    view.addEventListener('show-annotation', this.annotationClicked as EventListener);
    // Never open arbitrary URLs supplied by the publication, including javascript: links.
    view.addEventListener('external-link', this.preventExternal);
    host.append(view);
    await view.open(new File([blob], 'book.' + this.extension));
    if (!this.alive) return;
    this.chapters = flattenChapters(view.book.toc);
    this.applyPreferences(this.prefs);
    await view.init(initial?.type === 'epub' ? { lastLocation: initial.cfi } : {});
  }
  private preventExternal = (e: Event) => e.preventDefault();
  private relocated = (event: CustomEvent<Relocate>) => {
    if (!this.alive || !event.detail?.cfi) return;
    const locator: Locator = { type: 'epub', cfi: event.detail.cfi };
    const progress = clamp(event.detail.fraction ?? 0, 0, 1);
    this.current = { locator, progress };
    this.hooks.onPosition(locator, progress);
  };
  private loaded = (event: CustomEvent<{ doc: FoliateDoc; index: number }>) => {
    const { doc, index } = event.detail;
    for (const [old, cleanup] of this.docs) if (old !== doc) { cleanup(); this.docs.delete(old); }
    const activity = () => this.hooks.onInteract();
    const select = () => {
      activity();
      const selection = doc.getSelection();
      if (!selection || selection.isCollapsed || !selection.rangeCount) return;
      const range = selection.getRangeAt(0);
      const quote = selection.toString().trim().slice(0, 4000);
      if (!quote || !this.view) return;
      try {
        const cfi = this.view.getCFI(index, range);
        this.hooks.onSelection({ locator: { type: 'epub', cfi }, anchor: { cfi, quote }, quote });
      } catch { /* Ignore transient selections across unloaded chapters. */ }
    };
    doc.addEventListener('pointerup', select);
    doc.addEventListener('keyup', select);
    doc.addEventListener('pointerdown', activity);
    const key = (event: KeyboardEvent) => { activity(); this.hooks.onKey(event); };
    doc.addEventListener('keydown', key);
    doc.addEventListener('touchstart', activity, { passive: true });
    this.docs.set(doc, () => { doc.removeEventListener('pointerup', select); doc.removeEventListener('keyup', select);
      doc.removeEventListener('pointerdown', activity); doc.removeEventListener('keydown', key); doc.removeEventListener('touchstart', activity); });
  };
  private overlay = () => { requestAnimationFrame(() => {
    if (!this.alive) return;
    for (const item of this.annotations) if (item.type !== 'bookmark' && item.locator.type === 'epub') void this.view?.addAnnotation({ value: item.locator.cfi }).catch(() => {});
  }); };
  private draw = (event: CustomEvent<{ annotation: { value: string }; draw: (fn: (rects: DOMRectList) => SVGElement) => void }>) => {
    const annotation = this.annotations.find(a => a.locator.type === 'epub' && a.locator.cfi === event.detail.annotation.value);
    if (!annotation) return;
    event.detail.draw(rects => {
      const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      g.setAttribute('fill', colors[annotation.color]); g.setAttribute('opacity', '.43');
      for (const r of Array.from(rects)) {
        const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        for (const [key, value] of Object.entries({ x: r.left, y: r.top, width: r.width, height: r.height })) rect.setAttribute(key, String(value));
        g.append(rect);
      }
      return g;
    });
  };
  private annotationClicked = (event: CustomEvent<{ value: string }>) => {
    const found = this.annotations.find(a => a.locator.type === 'epub' && a.locator.cfi === event.detail.value);
    if (found) this.hooks.onAnnotationClick(found.id);
  };
  setAnnotations(items: Annotation[]) {
    for (const old of this.annotations) if (old.locator.type === 'epub') void this.view?.addAnnotation({ value: old.locator.cfi }, true).catch(() => {});
    this.annotations = items.filter(a => !a.deleted);
    for (const item of this.annotations) if (item.locator.type === 'epub' && item.type !== 'bookmark') void this.view?.addAnnotation({ value: item.locator.cfi }).catch(() => {});
  }
  applyPreferences(prefs: ReaderPreferences) {
    this.prefs = prefs;
    const renderer = this.view?.renderer;
    if (!renderer) return;
    renderer.setAttribute('flow', prefs.flow);
    renderer.setAttribute('max-inline-size', `${clamp(prefs.maxWidth, 360, 1200)}px`);
    renderer.setAttribute('margin', `${clamp(prefs.margin, 4, 90)}px`);
    renderer.setAttribute('max-column-count', '1');
    const bg = prefs.theme === 'dark' ? '#181818' : prefs.theme === 'sepia' ? '#eee3cc' : '#ffffff';
    const ink = prefs.theme === 'dark' ? '#f2f2f2' : '#171717';
    renderer.setStyles?.(`:root,body { background: ${bg} !important; color: ${ink} !important; }
      body { font-family: ${family(prefs.fontFamily)} !important; font-size: ${clamp(prefs.fontSize, 12, 36)}px !important;
        line-height: ${clamp(prefs.lineHeight, 1.2, 2.4)} !important; }
      p { margin-block: 0 ${clamp(prefs.paragraphSpacing, 0, 3)}em !important; }
       ::selection { background: #d8d8d8; color: #171717; }`);
  }
  next() { return this.view?.next(); }
  prev() { return this.view?.prev(); }
  async go(locator: Locator) { if (locator.type === 'epub') await this.view?.goTo(locator.cfi); }
  async goChapter(chapter: Chapter) { await this.view?.goTo(chapter.href); }
  /**
   * Chapter boundaries in the same fraction space Foliate reports `relocate` progress in.
   * Foliate sizes sections from the EPUB zip entry table, so these are available immediately after
   * open and stay stable as the reader moves — no need to load every section first.
   */
  chapterStarts(): number[] | null {
    const view = this.view;
    const fractions = view?.getSectionFractions?.() ?? [];
    const sections = view?.book?.sections ?? [];
    if (!this.chapters.length || !fractions.length || !sections.length) return null;
    let last = 0;
    return this.chapters.map(chapter => {
      const href = chapter.href.split('#')[0];
      const candidates = href ? [href, decodeURI(href)] : [];
      // A TOC href resolves to the spine section whose id is that href; nested entries in a file we
      // already located inherit its start, which is the correct approximation for a sub-heading.
      let index = sections.findIndex(section => !!section.id && candidates.includes(section.id));
      if (index < 0) index = sections.findIndex(section => !!section.id && href && section.id.endsWith(href));
      if (index < 0) return last;
      last = clamp(fractions[index] ?? last, 0, 1);
      return last;
    });
  }
  async search(query: string, onResult: (found: Found) => void, onProgress: (fraction: number) => void) {
    this.cancelled = false;
    const view = this.view;
    if (!view || !query.trim()) return;
    for await (const result of view.search({ query: query.trim() })) {
      if (this.cancelled || !this.alive) { view.clearSearch(); return; }
      if (typeof result === 'string') continue;
      if (typeof result.progress === 'number') onProgress(result.progress);
      for (const item of result.subitems ?? []) onResult({ label: result.label || 'In the book', excerpt: `${item.excerpt.pre}${item.excerpt.match}${item.excerpt.post}`, locator: { type: 'epub', cfi: item.cfi } });
      // Yield to painting/input between chapters.
      await new Promise(resolve => setTimeout(resolve, 0));
    }
  }
  cancelSearch() { this.cancelled = true; this.view?.clearSearch(); }
  close() {
    this.alive = false; this.cancelSearch();
    for (const cleanup of this.docs.values()) cleanup(); this.docs.clear();
    if (this.view) {
      this.view.removeEventListener('relocate', this.relocated as EventListener);
      this.view.removeEventListener('load', this.loaded as EventListener);
      this.view.removeEventListener('create-overlay', this.overlay as EventListener);
      this.view.removeEventListener('draw-annotation', this.draw as EventListener);
      this.view.removeEventListener('show-annotation', this.annotationClicked as EventListener);
      this.view.removeEventListener('external-link', this.preventExternal);
      this.view.close(); this.view.remove(); this.view = null;
    }
  }
}
