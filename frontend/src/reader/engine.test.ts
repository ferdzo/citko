import { describe, expect, it } from 'vitest';
import { chapterSpans, clamp, effectivePreferences, flattenChapters, progressSegments, spanAt, spanFill, validLocator } from './engine';
import { defaultPreferences, type Book } from '../types';

const book = { id: 'book-1', content: { renditionId: 'edition-1', format: 'epub' } } as Book;

describe('reader boundaries', () => {
  it('keeps the selected edition locator type, never guesses a location in another format', () => {
    expect(validLocator({ type: 'epub', cfi: 'epubcfi(/6/4)' }, book)).toBe(true);
    expect(validLocator({ type: 'pdf', page: 0, y: 0 }, book)).toBe(false);
    expect(validLocator({ type: 'pdf', page: 0, y: 0 }, { ...book, content: { ...book.content!, format: 'pdf' } })).toBe(true);
  });
  it('applies per-book preferences without replacing global defaults or override records', () => {
    const global = { ...defaultPreferences, bookOverrides: { 'book-1': { fontSize: 23, flow: 'scrolled' as const }, 'book-2': { theme: 'dark' as const } } };
    const resolved = effectivePreferences(global, 'book-1');
    expect(resolved.fontSize).toBe(23);
    expect(resolved.flow).toBe('scrolled');
    expect(resolved.theme).toBe('light');
    expect(resolved.bookOverrides['book-2'].theme).toBe('dark');
    expect(global.fontSize).toBe(18);
  });
  it('preserves actual chapter destinations through nested contents', () => {
    expect(flattenChapters([{ label: 'Part one', href: 'a', subitems: [{ label: 'Chapter one', href: 'b' }] }])).toEqual([
      { label: 'Part one', href: 'a', depth: 0 }, { label: 'Chapter one', href: 'b', depth: 1 },
    ]);
  });
  it('bounds normalized progress', () => { expect(clamp(-.2, 0, 1)).toBe(0); expect(clamp(1.3, 0, 1)).toBe(1); });
});

describe('chapter progress', () => {
  const chapters = [
    { label: 'One', href: 'a', depth: 0 },
    { label: 'Two', href: 'b', depth: 0 },
    { label: 'Three', href: 'c', depth: 0 },
  ];
  it('sizes each chapter from its own start to the next one', () => {
    expect(chapterSpans(chapters, [0, .25, .6])).toEqual([
      { chapter: chapters[0], start: 0, end: .25 },
      { chapter: chapters[1], start: .25, end: .6 },
      { chapter: chapters[2], start: .6, end: 1 },
    ]);
  });
  it('reports nothing rather than guessing when boundaries do not match the chapter list', () => {
    expect(chapterSpans(chapters, [0, .5])).toEqual([]);
    expect(chapterSpans(chapters, null)).toEqual([]);
    expect(chapterSpans([], [0])).toEqual([]);
    // A single unusable boundary is coerced and then held monotonic, never dropped.
    expect(chapterSpans(chapters, [0, .5, Number.NaN]).map(span => span.start)).toEqual([0, .5, .5]);
  });
  it('forces monotonic boundaries so an unresolved destination cannot invert a span', () => {
    const spans = chapterSpans(chapters, [0, .8, .3]);
    expect(spans.map(span => span.start)).toEqual([0, .8, .8]);
    expect(spans.every(span => span.end >= span.start)).toBe(true);
  });
  it('collapses chapters sharing a boundary into one bar segment', () => {
    const nested = [...chapters, { label: 'Sub', href: 'd', depth: 1 }];
    expect(progressSegments(chapterSpans(nested, [0, .25, .6, .6]))).toEqual([
      { label: 'One', start: 0, end: .25 },
      { label: 'Two', start: .25, end: .6 },
      { label: 'Three', start: .6, end: 1 },
    ]);
  });
  it('locates the position and the fill within a chapter', () => {
    const spans = chapterSpans(chapters, [0, .25, .6]);
    expect(spanAt(spans, 0)).toBe(0);
    expect(spanAt(spans, .3)).toBe(1);
    expect(spanAt(spans, .99)).toBe(2);
    expect(spanAt([], .5)).toBe(-1);
    expect(spanFill(spans[1], .425)).toBeCloseTo(.5);
    expect(spanFill(spans[0], .5)).toBe(1);
    expect(spanFill(spans[2], .1)).toBe(0);
  });
});
