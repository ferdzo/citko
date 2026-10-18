import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, Bookmark, BookOpen, Check, ChevronLeft, ChevronRight, Highlighter, List, Maximize2, Minimize2, Moon, MoreHorizontal, Search, Settings2, StickyNote, Sun, Trash2, X, ZoomIn, ZoomOut } from 'lucide-react';
import type { Annotation, Book, Locator, Position, ReaderPreferences } from '../types';
import { chapterSpans, clamp, effectivePreferences, progressSegments, spanAt, spanFill, validLocator, type Chapter, type Found, type ReaderEngine, type Selection } from './engine';
import { FoliateEngine } from './foliate';
import { PdfEngine } from './pdf';
import 'pdfjs-dist/legacy/web/pdf_viewer.css';
import './reader.css';

export interface ReaderScreenProps {
  book: Book;
  source: string | Blob;
  initialPosition: Position | null;
  otherPositions: Position[];
  annotations: Annotation[];
  preferences: ReaderPreferences;
  onPosition(locator: Locator, progress: number): void;
  onAnnotation(value: Omit<Annotation, 'revision' | 'deleted' | 'createdAt'>): void;
  onAnnotationDelete(id: string): void;
  onPreferences(value: ReaderPreferences): void;
  onActivity(activeMs: number): void;
  onClose(): void;
}
type Panel = 'contents' | 'search' | 'notes' | 'settings' | null;
const formatPosition = (locator: Locator) => locator.type === 'pdf' ? `Page ${locator.page + 1}` : 'Saved reading place';

export default function ReaderScreen(props: ReaderScreenProps) {
  const { book, source, initialPosition, otherPositions, annotations, preferences, onClose } = props;
  const host = useRef<HTMLDivElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const engine = useRef<ReaderEngine | null>(null);
  const latest = useRef(props); latest.current = props;
  const pending = useRef<{ locator: Locator; progress: number } | null>(null);
  const positionTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activity = useRef({ lastTick: performance.now(), lastTouch: performance.now(), accumulated: 0 });
  const searchSerial = useRef(0);
  const [panel, setPanel] = useState<Panel>(null);
  const [visible, setVisible] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [location, setLocation] = useState<{ locator: Locator; progress: number } | null>(null);
  const [chapters, setChapters] = useState<Chapter[]>([]);
  const [starts, setStarts] = useState<number[] | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [editing, setEditing] = useState<Annotation | null>(null);
  const [note, setNote] = useState('');
  const [color, setColor] = useState<Annotation['color']>('yellow');
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<Found[]>([]);
  const [searched, setSearched] = useState(0);
  const [fullscreen, setFullscreen] = useState(false);
  const [perBook, setPerBook] = useState(true);
  const prefs = effectivePreferences(preferences, book.id);
  const isPdf = book.content?.format === 'pdf';
  const other = otherPositions.filter(p => p.renditionId === book.content?.renditionId && p.locator.type === (isPdf ? 'pdf' : 'epub'))
    .sort((a, b) => b.revision - a.revision);
  // Chapter spans come from the engine once the book is open; both surfaces below degrade to the
  // plain progress bar when a book has no outline or chapter boundaries could not be resolved.
  const spans = useMemo(() => chapterSpans(chapters, starts), [chapters, starts]);
  const segments = useMemo(() => progressSegments(spans), [spans]);
  const bookProgress = location?.progress ?? initialPosition?.progress ?? 0;
  const currentSpan = spanAt(spans, bookProgress);
  const currentSegment = spanAt(segments, bookProgress);

  const flush = useCallback(() => {
    if (positionTimer.current) clearTimeout(positionTimer.current);
    positionTimer.current = null;
    if (pending.current) {
      latest.current.onPosition(pending.current.locator, pending.current.progress);
      pending.current = null;
    }
  }, []);
  const flushActivity = useCallback(() => {
    const state = activity.current;
    const ms = Math.min(60000, Math.floor(state.accumulated));
    if (ms > 0) { state.accumulated -= ms; latest.current.onActivity(ms); }
  }, []);
  const tickActivity = useCallback(() => {
    const state = activity.current, now = performance.now();
    const elapsed = Math.max(0, Math.min(10000, now - state.lastTick));
    if (document.visibilityState === 'visible' && now - state.lastTouch < 60000 && !error && !loading) state.accumulated += elapsed;
    state.lastTick = now;
  }, [error, loading]);
  const interact = useCallback(() => { tickActivity(); activity.current.lastTouch = performance.now(); }, [tickActivity]);
  const navigate = useCallback((action: 'next' | 'prev' | Locator | Chapter) => {
    flush(); interact(); setSelection(null); setEditing(null);
    if (action === 'next') void engine.current?.next();
    else if (action === 'prev') void engine.current?.prev();
    else if ('type' in action) void engine.current?.go(action);
    else void engine.current?.goChapter(action);
  }, [flush, interact]);
  const key = useCallback((event: KeyboardEvent) => {
    const target = event.target as HTMLElement;
    if (target?.closest('input,textarea,select,[contenteditable="true"]') || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'Escape') { setSelection(null); setEditing(null); setPanel(null); setVisible(true); return; }
    if (['ArrowRight', 'PageDown', ' '].includes(event.key)) { event.preventDefault(); navigate('next'); }
    if (['ArrowLeft', 'PageUp'].includes(event.key)) { event.preventDefault(); navigate('prev'); }
  }, [navigate]);

  useEffect(() => {
    const mount = host.current;
    if (!mount) return;
    let active = true;
    const format = book.content?.format;
    const start = initialPosition && initialPosition.renditionId === book.content?.renditionId && validLocator(initialPosition.locator, book) ? initialPosition.locator : null;
    const hooks = {
      onPosition: (locator: Locator, progress: number) => {
        if (!active) return;
        pending.current = { locator, progress };
        setLocation({ locator, progress });
        if (positionTimer.current) clearTimeout(positionTimer.current);
        positionTimer.current = setTimeout(flush, 900);
      },
      onSelection: (value: Selection) => { if (active) { setSelection(value); setEditing(null); setVisible(true); } },
      onAnnotationClick: (id: string) => {
        const item = latest.current.annotations.find(a => a.id === id);
        if (item) { setEditing(item); setNote(item.note); setColor(item.color); setSelection(null); setVisible(true); }
      },
      onInteract: () => { activity.current.lastTouch = performance.now(); },
      onKey: (event: KeyboardEvent) => key(event),
    };
    const instance: ReaderEngine = format === 'pdf' ? new PdfEngine(hooks, prefs) : new FoliateEngine(hooks, prefs, format ?? 'epub');
    engine.current = instance;
    setLoading(true); setError('');
    void instance.open(mount, source, start).then(() => {
      if (!active) return;
      setChapters([...instance.chapters]);
      setStarts(instance.chapterStarts());
      instance.setAnnotations(latest.current.annotations);
      setLoading(false);
      activity.current.lastTick = performance.now();
    }).catch((reason: unknown) => { if (active) { setLoading(false); setError(reason instanceof Error ? reason.message : 'This edition could not be opened.'); } });
    return () => { active = false; flush(); flushActivity(); instance.close(); if (engine.current === instance) engine.current = null; };
    // Engine is bound to the rendition, not changes to global settings or position snapshots.
  }, [book.id, book.content?.renditionId, source]);
  useEffect(() => { engine.current?.applyPreferences(prefs); }, [prefs.theme, prefs.fontFamily, prefs.fontSize, prefs.lineHeight, prefs.paragraphSpacing, prefs.margin, prefs.maxWidth, prefs.flow, prefs.pdfMode]);
  useEffect(() => { engine.current?.setAnnotations(annotations); }, [annotations]);
  useEffect(() => {
    const interval = setInterval(() => { tickActivity(); flushActivity(); }, 10000);
    const visibility = () => { tickActivity(); if (document.visibilityState !== 'visible') { flush(); flushActivity(); } else activity.current.lastTick = performance.now(); };
    const leaving = () => { tickActivity(); flush(); flushActivity(); };
    document.addEventListener('visibilitychange', visibility);
    window.addEventListener('pagehide', leaving);
    return () => { clearInterval(interval); document.removeEventListener('visibilitychange', visibility); window.removeEventListener('pagehide', leaving); };
  }, [tickActivity, flushActivity, flush]);
  useEffect(() => { document.addEventListener('keydown', key); return () => document.removeEventListener('keydown', key); }, [key]);
  useEffect(() => { const listener = () => setFullscreen(document.fullscreenElement === root.current); document.addEventListener('fullscreenchange', listener); return () => document.removeEventListener('fullscreenchange', listener); }, []);
  useEffect(() => {
    if (panel !== 'search' || !query.trim()) { engine.current?.cancelSearch(); setResults([]); setSearched(0); return; }
    const serial = ++searchSerial.current;
    engine.current?.cancelSearch(); setResults([]); setSearched(0);
    const delay = setTimeout(() => void engine.current?.search(query, found => {
      if (serial === searchSerial.current) setResults(prev => prev.length < 500 ? [...prev, found] : prev);
    }, progress => { if (serial === searchSerial.current) setSearched(progress); }).catch(() => {}), 320);
    return () => { ++searchSerial.current; clearTimeout(delay); engine.current?.cancelSearch(); };
  }, [panel, query]);

  const save = (value: Selection | Annotation, asNote: boolean) => {
    const current = 'id' in value ? value : null;
    const selected = current ? { locator: current.locator, anchor: current.anchor, quote: current.quote } : value;
    latest.current.onAnnotation({ id: current?.id ?? crypto.randomUUID(), bookId: book.id, renditionId: book.content?.renditionId ?? '',
      type: asNote ? 'note' : 'highlight', locator: selected.locator, anchor: selected.anchor, quote: selected.quote,
      note: asNote ? note.trim() : current?.note ?? '', color });
    setSelection(null); setEditing(null); setNote('');
    // The local sync layer will publish the saved annotation back through props.
  };
  const bookmark = () => {
    const here = location ?? engine.current?.current;
    if (!here) return;
    latest.current.onAnnotation({ id: crypto.randomUUID(), bookId: book.id, renditionId: book.content?.renditionId ?? '',
      type: 'bookmark', locator: here.locator, anchor: null, quote: '', note: '', color: 'green' });
  };
  const update = (patch: Partial<Omit<ReaderPreferences, 'bookOverrides'>>) => {
    const value = perBook ? { ...preferences, bookOverrides: { ...preferences.bookOverrides,
      [book.id]: { ...(preferences.bookOverrides?.[book.id] ?? {}), ...patch } } } : { ...preferences, ...patch };
    latest.current.onPreferences(value);
  };
  const close = () => { flush(); tickActivity(); flushActivity(); if (document.fullscreenElement === root.current) void document.exitFullscreen(); onClose(); };
  const changePanel = (next: Panel) => { setVisible(true); setSelection(null); setEditing(null); setPanel(panel === next ? null : next); interact(); };

  return <div ref={root} className={`citko-reader citko-reader--${prefs.theme} ${!visible && !panel ? 'citko-reader--quiet' : ''}`} onPointerDown={interact}>
    <header className="citko-reader__top" aria-label="Reader toolbar">
      <button className="citko-reader__icon" onClick={close} aria-label="Close reader" title="Back to library"><ArrowLeft size={21} /></button>
      <div className="citko-reader__identity"><strong title={book.title}>{book.title}</strong><small>{book.author || 'Unknown author'}</small></div>
      <div className="citko-reader__tools">
        <button className="citko-reader__icon" onClick={() => changePanel('contents')} aria-label="Table of contents" aria-pressed={panel === 'contents'} title="Contents"><List size={20} /></button>
        <button className="citko-reader__icon" onClick={() => changePanel('search')} aria-label="Search book" aria-pressed={panel === 'search'} title="Search"><Search size={20} /></button>
        <button className="citko-reader__icon" onClick={bookmark} aria-label="Bookmark this position" title="Bookmark"><Bookmark size={20} /></button>
        <button className="citko-reader__icon" onClick={() => changePanel('notes')} aria-label="Highlights and bookmarks" aria-pressed={panel === 'notes'} title="Notes"><Highlighter size={20} /></button>
        <button className="citko-reader__icon" onClick={() => changePanel('settings')} aria-label="Reading settings" aria-pressed={panel === 'settings'} title="Settings"><Settings2 size={20} /></button>
        <button className="citko-reader__icon citko-reader__desktop" onClick={() => { if (document.fullscreenElement === root.current) void document.exitFullscreen(); else void root.current?.requestFullscreen(); }} aria-label={fullscreen ? 'Exit full screen' : 'Full screen'} title="Full screen">{fullscreen ? <Minimize2 size={19} /> : <Maximize2 size={19} />}</button>
      </div>
    </header>

    <main className="citko-reader__stage" aria-label={`${book.title} reading content`}>
      <div className="citko-reader__paper" ref={host} />
      {!loading && !error && prefs.flow === 'paginated' && !isPdf && <><button className="citko-reader__edge citko-reader__edge--left" onClick={() => navigate('prev')} aria-label="Previous page"><ChevronLeft size={25} /></button><button className="citko-reader__edge citko-reader__edge--right" onClick={() => navigate('next')} aria-label="Next page"><ChevronRight size={25} /></button></>}
      {!loading && !error && !panel && <button className="citko-reader__reveal" onClick={() => setVisible(v => !v)} aria-label={visible ? 'Hide reading controls' : 'Show reading controls'} title="Toggle controls" />}
      {(loading || error) && <div className="citko-reader__loading" role={error ? 'alert' : 'status'}><div className="citko-reader__loading-mark"><BookOpen size={28} strokeWidth={1.5} /></div><span>{error ? 'Cannot open this book' : 'Opening book'}</span><p>{error || 'Loading content…'}</p>{error && <button className="citko-reader__text-button" onClick={close}>Back to library <ArrowRight size={16} /></button>}</div>}
    </main>

    {(selection || editing) && <div className="citko-reader__selection" role="dialog" aria-label={editing ? 'Edit annotation' : 'Annotate selection'}>
      <button className="citko-reader__icon citko-reader__selection-close" onClick={() => { setEditing(null); setSelection(null); }} aria-label="Dismiss selection"><X size={17} /></button>
      <span className="citko-reader__eyebrow">{editing ? 'EDIT ANNOTATION' : 'NEW ANNOTATION'}</span>
      <blockquote>{(editing?.quote || selection?.quote || '').slice(0, 190)}{(editing?.quote || selection?.quote || '').length > 190 ? '…' : ''}</blockquote>
      <div className="citko-reader__swatches" role="group" aria-label="Highlight color">{(['yellow', 'green', 'blue', 'pink'] as const).map(c => <button key={c} className={`citko-reader__swatch citko-reader__swatch--${c}`} aria-label={`${c} highlight`} aria-pressed={color === c} onClick={() => setColor(c)}>{color === c && <Check size={14} />}</button>)}</div>
      <label className="citko-reader__field-label" htmlFor="citko-note">Note <span>optional</span></label><textarea id="citko-note" rows={2} placeholder="Add a note" value={note} onChange={event => setNote(event.target.value)} />
      <div className="citko-reader__selection-actions"><button className="citko-reader__primary" onClick={() => save((editing || selection)!, Boolean(note.trim()) || editing?.type === 'note')}><Highlighter size={16} /> {editing ? 'Save changes' : note.trim() ? 'Save note' : 'Highlight'}</button>{editing && <button className="citko-reader__danger" onClick={() => { latest.current.onAnnotationDelete(editing.id); setEditing(null); }} aria-label="Delete annotation"><Trash2 size={16} /> Delete</button>}</div>
    </div>}

    <footer className="citko-reader__bottom" aria-label="Reading progress">
      <div className="citko-reader__bottom-side"><span className="citko-reader__eyebrow">{isPdf ? 'DOCUMENT' : 'EDITION'}</span><span>{(book.content?.format ?? 'book').toUpperCase()}</span></div>
      <button className="citko-reader__page-arrow" aria-label="Previous page" onClick={() => navigate('prev')}><ChevronLeft size={19} /></button>
      <div className="citko-reader__progress">
        <div className="citko-reader__progress-caption">
          <span>{segments.length > 1 ? `${currentSegment + 1} of ${segments.length} · ${segments[currentSegment].label}` : location?.locator.type === 'pdf' ? formatPosition(location.locator) : 'Your place'}</span>
          <span>{Math.round(bookProgress * 100)}%</span>
        </div>
        <div className={`citko-reader__track${segments.length > 1 ? ' citko-reader__track--chapters' : ''}`} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(bookProgress * 100)} aria-label="Book progress">
          {segments.length > 1 ? segments.map((segment, index) => <div key={`${index}-${segment.label}`} className={`citko-reader__segment${index === currentSegment ? ' citko-reader__segment--current' : ''}`} style={{ flexGrow: Math.max(1e-4, segment.end - segment.start) }} title={`${index + 1}. ${segment.label} — ${Math.round(spanFill(segment, bookProgress) * 100)}% read`}><i style={{ width: `${Math.round(spanFill(segment, bookProgress) * 100)}%` }} /></div>) : <span style={{ width: `${Math.round(bookProgress * 100)}%` }} />}
        </div>
        {segments.length > 1 && <div className="citko-reader__legend">
          <span><i className="citko-reader__key citko-reader__key--read" />Read</span>
          <span><i className="citko-reader__key citko-reader__key--unread" />Unread</span>
          <span className="citko-reader__legend-here">Chapter {currentSegment + 1} of {segments.length}</span>
        </div>}
      </div>
      <button className="citko-reader__page-arrow" aria-label="Next page" onClick={() => navigate('next')}><ChevronRight size={19} /></button>
       {isPdf ? <div className="citko-reader__zoom" role="group" aria-label="PDF zoom"><button title="Zoom out" aria-label="Zoom out" onClick={() => engine.current?.zoom?.(1 / 1.15)}><ZoomOut size={17} /></button><button title="Fit page width" onClick={() => engine.current?.zoom?.('width')}>Fit width</button><button title="Zoom in" aria-label="Zoom in" onClick={() => engine.current?.zoom?.(1.15)}><ZoomIn size={17} /></button></div> : <div className="citko-reader__bottom-side citko-reader__bottom-side--right"><MoreHorizontal size={20} /></div>}
    </footer>

    {panel && <><button className="citko-reader__scrim" onClick={() => setPanel(null)} aria-label="Dismiss panel" /><aside className="citko-reader__panel" aria-label={panel === 'contents' ? 'Contents' : panel === 'search' ? 'Search' : panel === 'notes' ? 'Your notes' : 'Reading settings'}>
      <div className="citko-reader__panel-head"><div><h2>{panel === 'contents' ? 'Contents' : panel === 'search' ? 'Search' : panel === 'notes' ? 'Notes & highlights' : 'Reading settings'}</h2></div><button className="citko-reader__icon" onClick={() => setPanel(null)} aria-label="Close panel"><X size={20} /></button></div>
      <div className="citko-reader__panel-body">
        {panel === 'contents' && <><p className="citko-reader__aside-intro">{book.title} <span>—</span> {book.author}</p>{chapters.length ? <nav aria-label="Chapters" className="citko-reader__toc">{chapters.map((chapter, i) => { const span = spans.length === chapters.length ? spans[i] : null; const fill = span ? spanFill(span, bookProgress) : 0; return <button key={`${chapter.href}-${i}`} aria-current={i === currentSpan ? 'true' : undefined} style={{ paddingLeft: `${16 + Math.min(3, chapter.depth) * 17}px` }} onClick={() => { navigate(chapter); setPanel(null); }}><span className="citko-reader__toc-index">{String(i + 1).padStart(2, '0')}</span><span>{chapter.label}</span>{span && <span className="citko-reader__toc-state"><span className="citko-reader__toc-meter"><i style={{ width: `${Math.round(fill * 100)}%` }} /></span><span className="citko-reader__toc-pct">{fill >= 1 ? 'Read' : fill > 0 ? `${Math.round(fill * 100)}%` : '—'}</span></span>}<ChevronRight size={15} /></button>; })}</nav> : <p className="citko-reader__empty">This book doesn't include a table of contents. Use the arrows to move through it.</p>}
          {other.length > 0 && <section className="citko-reader__other"><span className="citko-reader__eyebrow">FROM ANOTHER DEVICE</span><p>Your position here has not changed. Jump to another saved place only if you choose.</p>{other.map(p => <button key={p.id} onClick={() => { navigate(p.locator); setPanel(null); }}><span><strong>{formatPosition(p.locator)}</strong><small>{Math.round(p.progress * 100)}% · {new Date(p.updatedAt).toLocaleDateString()}</small></span><ArrowRight size={16} /></button>)}</section>}</>}
        {panel === 'search' && <><label className="citko-reader__search"><Search size={19} /><input autoFocus value={query} onChange={event => setQuery(event.target.value)} placeholder="Search within this book" aria-label="Search within book" type="search" /></label>{query.trim() && <p className="citko-reader__search-status" role="status">{results.length >= 500 ? 'Showing first 500 matches' : `${results.length} ${results.length === 1 ? 'passage' : 'passages'}`} · {Math.round(searched * 100)}% searched</p>}<div className="citko-reader__matches">{results.map((result, i) => <button key={`${i}-${result.label}`} onClick={() => { navigate(result.locator); setPanel(null); }}><span className="citko-reader__eyebrow">{result.label}</span><span>{result.excerpt}</span><ArrowRight size={16} /></button>)}</div>{!query && <p className="citko-reader__empty">Search the text of this book.</p>}</>}
        {panel === 'notes' && <>{annotations.filter(a => !a.deleted && a.renditionId === book.content?.renditionId).length ? <div className="citko-reader__notes">{annotations.filter(a => !a.deleted && a.renditionId === book.content?.renditionId).map(a => <article key={a.id} className={`citko-reader__note citko-reader__note--${a.color}`}><span className="citko-reader__eyebrow">{a.type === 'bookmark' ? 'BOOKMARK' : a.type === 'note' ? 'NOTE' : 'HIGHLIGHT'} · {formatPosition(a.locator)}</span>{a.quote && <blockquote>{a.quote}</blockquote>}{a.note && <p>{a.note}</p>}<div><button onClick={() => { navigate(a.locator); setPanel(null); }}>Go to passage <ArrowRight size={15} /></button>{a.type !== 'bookmark' && <button onClick={() => { setEditing(a); setNote(a.note); setColor(a.color); setPanel(null); }}>Edit</button>}<button onClick={() => latest.current.onAnnotationDelete(a.id)} aria-label="Delete annotation"><Trash2 size={16} /></button></div></article>)}</div> : <div className="citko-reader__empty citko-reader__empty--center"><StickyNote size={26} /><strong>No notes yet</strong><p>Select text to add a highlight or use the bookmark button to save your place.</p></div>}</>}
        {panel === 'settings' && <div className="citko-reader__settings"><section><span className="citko-reader__eyebrow">APPLY SETTINGS TO</span><div className="citko-reader__segmented"><button aria-pressed={perBook} onClick={() => setPerBook(true)}>This book</button><button aria-pressed={!perBook} onClick={() => setPerBook(false)}>All books</button></div></section><section><span className="citko-reader__eyebrow">THEME</span><div className="citko-reader__themes">{(['light', 'sepia', 'dark'] as const).map(t => <button key={t} aria-pressed={prefs.theme === t} className={`citko-reader__theme citko-reader__theme--${t}`} onClick={() => update({ theme: t })}>{t === 'dark' ? <Moon size={19} /> : <Sun size={19} />}<span>{t === 'light' ? 'Light' : t === 'sepia' ? 'Sepia' : 'Dark'}</span></button>)}</div></section>
          {!isPdf && <><section><label className="citko-reader__eyebrow" htmlFor="citko-font">TYPEFACE</label><select id="citko-font" value={['Georgia', 'Literary serif', 'Modern serif', 'Sans', 'Mono'].includes(prefs.fontFamily) ? prefs.fontFamily : 'Georgia'} onChange={e => update({ fontFamily: e.target.value })}>{['Georgia', 'Literary serif', 'Modern serif', 'Sans', 'Mono'].map(f => <option key={f}>{f}</option>)}</select></section><SettingRange label="Type size" value={prefs.fontSize} min={12} max={36} step={1} suffix="px" onChange={fontSize => update({ fontSize })} /><SettingRange label="Line spacing" value={prefs.lineHeight} min={1.2} max={2.4} step={0.05} suffix="×" onChange={lineHeight => update({ lineHeight })} /><SettingRange label="Paragraph space" value={prefs.paragraphSpacing} min={0} max={3} step={0.1} suffix="em" onChange={paragraphSpacing => update({ paragraphSpacing })} /><SettingRange label="Page margin" value={prefs.margin} min={4} max={90} step={2} suffix="px" onChange={margin => update({ margin })} /><SettingRange label="Text width" value={prefs.maxWidth} min={360} max={1200} step={20} suffix="px" onChange={maxWidth => update({ maxWidth })} /><section><span className="citko-reader__eyebrow">PAGE FLOW</span><div className="citko-reader__segmented"><button aria-pressed={prefs.flow === 'paginated'} onClick={() => update({ flow: 'paginated' })}>Turn pages</button><button aria-pressed={prefs.flow === 'scrolled'} onClick={() => update({ flow: 'scrolled' })}>Scroll</button></div></section></>}
          {isPdf && <section><span className="citko-reader__eyebrow">PDF LAYOUT</span><div className="citko-reader__segmented"><button aria-pressed={prefs.pdfMode === 'continuous'} onClick={() => update({ pdfMode: 'continuous' })}>Continuous</button><button aria-pressed={prefs.pdfMode === 'single'} onClick={() => update({ pdfMode: 'single' })}>Single page</button></div><p className="citko-reader__aside-intro">PDFs preserve their original typography. The viewer fits each page to your screen.</p></section>}
        </div>}
      </div>
    </aside></>}
  </div>;
}

function SettingRange({ label, value, min, max, step, suffix, onChange }: { label: string; value: number; min: number; max: number; step: number; suffix: string; onChange: (value: number) => void }) {
  return <section className="citko-reader__range"><label><span className="citko-reader__eyebrow">{label.toUpperCase()}</span><span>{Number(value.toFixed(2))}{suffix}</span></label><input aria-label={label} type="range" min={min} max={max} step={step} value={clamp(value, min, max)} onChange={event => onChange(Number(event.target.value))} /></section>;
}
