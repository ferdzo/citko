import type { Annotation, Anchor, Book, Locator, ReaderPreferences } from '../types';

export type Chapter = { label: string; href: string; depth: number };
export type Found = { label: string; excerpt: string; locator: Locator };
export type Selection = { locator: Locator; anchor: Anchor; quote: string };
export type ReaderEngine = {
  open(host: HTMLElement, source: string | Blob, initial: Locator | null): Promise<void>;
  close(): void;
  next(): Promise<void> | void;
  prev(): Promise<void> | void;
  go(locator: Locator): Promise<void> | void;
  goChapter(chapter: Chapter): Promise<void> | void;
  search(query: string, onResult: (found: Found) => void, onProgress: (fraction: number) => void): Promise<void>;
  cancelSearch(): void;
  applyPreferences(prefs: ReaderPreferences): void;
  zoom?: (factor: number | 'width') => void;
  setAnnotations(annotations: Annotation[]): void;
  chapters: Chapter[];
  /** Book-wide fraction at which each chapter starts, aligned with `chapters`. Null when unknown. */
  chapterStarts(): number[] | null;
  current: { locator: Locator; progress: number } | null;
};
export type EngineHooks = {
  onPosition: (locator: Locator, progress: number) => void;
  onSelection: (selection: Selection) => void;
  onAnnotationClick: (id: string) => void;
  onInteract: () => void;
  onKey: (event: KeyboardEvent) => void;
};
export const clamp = (v: number, min: number, max: number) => Math.max(min, Math.min(max, v));
export const colors: Record<Annotation['color'], string> = {
  yellow: '#e4b75f', green: '#8aab83', blue: '#83aebf', pink: '#d89ba3',
};
export const flattenChapters = (items: { label?: string; href?: string; subitems?: unknown[] }[] | undefined, depth = 0): Chapter[] =>
  (items ?? []).flatMap(item => [
    ...(item.href ? [{ label: item.label || 'Untitled section', href: item.href, depth }] : []),
    ...flattenChapters(item.subitems as typeof items, depth + 1),
  ]);
export const effectivePreferences = (prefs: ReaderPreferences, bookId: string): ReaderPreferences =>
  ({ ...prefs, ...prefs.bookOverrides?.[bookId], bookOverrides: prefs.bookOverrides ?? {} });
export const validLocator = (locator: Locator | null | undefined, book: Book): locator is Locator =>
  !!locator && (book.content?.format === 'pdf' ? locator.type === 'pdf' : locator.type === 'epub');

/**
 * Per-chapter span in book-progress space. Both formats report progress as a 0..1 fraction of the
 * whole book, so a chapter owns the range from its own start to the next chapter's start.
 */
export type ChapterSpan = { chapter: Chapter; start: number; end: number };

export const chapterSpans = (chapters: Chapter[], starts: number[] | null | undefined): ChapterSpan[] => {
  if (!chapters.length || !starts || starts.length !== chapters.length) return [];
  const clean = starts.map(value => (Number.isFinite(value) ? clamp(value, 0, 1) : 0));
  // Publishers emit TOC entries in reading order; force monotonicity so a resolution miss can never
  // produce an inverted or negative-width span.
  for (let i = 1; i < clean.length; i++) if (clean[i] < clean[i - 1]) clean[i] = clean[i - 1];
  return chapters.map((chapter, i) => {
    const start = clean[i];
    let end = 1;
    for (let j = i + 1; j < clean.length; j++) if (clean[j] > start + 1e-9) { end = clean[j]; break; }
    return { chapter, start, end };
  });
};

/** Distinct chapter boundaries for the segmented progress bar, sized by span width. */
export type ProgressSegment = { label: string; start: number; end: number };

export const progressSegments = (spans: ChapterSpan[]): ProgressSegment[] => {
  const distinct: ProgressSegment[] = [];
  for (const span of spans) {
    const last = distinct[distinct.length - 1];
    // TOC entries inside one file (or several outline rows on one PDF page) share a start; they are
    // one visual segment, not a zero-width one.
    if (last && span.start <= last.start + 1e-9) continue;
    distinct.push({ label: span.chapter.label, start: span.start, end: 1 });
  }
  return distinct.map((segment, i, all) => ({ ...segment, end: i + 1 < all.length ? all[i + 1].start : 1 }));
};

/** Index of the segment or span containing `progress`; -1 when there is nothing to match. */
export const spanAt = (spans: { start: number; end: number }[], progress: number): number => {
  if (!spans.length) return -1;
  for (let i = spans.length - 1; i >= 0; i--) if (progress >= spans[i].start - 1e-9) return i;
  return 0;
};

/** How far through `span` a position is, 0..1 — the fill for one chapter's own indicator. */
export const spanFill = (span: { start: number; end: number }, progress: number): number =>
  clamp((progress - span.start) / Math.max(1e-6, span.end - span.start), 0, 1);
