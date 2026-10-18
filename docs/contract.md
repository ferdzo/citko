# Citko implementation contract

All API routes use `/api/v1`, same-origin cookies, JSON camelCase. IDs are UUID strings; timestamps ISO UTC. Errors: `{error:{code,message,details?}}`. Lists: `{items,total,page,pageSize}`. Server revisions are monotonically increasing **per user**, allocated with user row lock and committed with changes. No client-clock conflict ordering. Book deletion is recoverable tombstoning, not advertised permanent erasure.

## Authentication
- `GET /auth/setup` → `{required:boolean}`.
- `POST /auth/setup` `{email,password,name}` → first administrator, cookie, `{user,csrfToken}`. Atomic one-time setup protected by `SETUP_TOKEN` through `X-Setup-Token`; deployment prints instructions, not secrets. Password >=12 chars. Subsequent users via admin CLI.
- `POST /auth/login` `{email,password}` → `{user,csrfToken}`; `GET /auth/me` same (401 if absent).
- `POST /auth/logout` revokes session; `GET /auth/csrf` → `{csrfToken}`.
- User `{id,email,name}`. Session HttpOnly, SameSite=Lax, Secure outside explicit development. Unsafe authenticated requests require `X-CSRF-Token` and allowed Origin. Setup/login require allowed Origin. Rate-limit authentication.

## Library
- `POST /books` multipart `file` → 201 `{book,jobId}` (duplicate →409 with existing book ID). Frontend iterates multi-upload with byte progress.
- `GET /books` paginated `page=1&pageSize=48&q=&status=&format=&tag=&collectionId=&author=&favorite=&sort=added&direction=desc`. status here means readingStatus. sort title/author/added/opened/progress. Returns `{items:Book[],total,page,pageSize}`.
- `GET /books/{id}` → Book; `PATCH /books/{id}` `{baseRevision,...patch}` CAS →Book. Metadata editing via this endpoint, also sync `book.patch`.
- `DELETE /books/{id}?baseRevision=N` soft delete. Bulk mutations use individual sync ops; no hidden partial-success batch semantics.
- `GET /books/{id}/file` active rendition, authenticated, ranges/ETag; `/original` immutable original; `/cover` image. UUID ownership everywhere.
- `GET /imports` paginated jobs; `POST /imports/{id}/retry` failed job.
- `GET /library/export` JSON metadata; `POST /library/import` same exported JSON metadata only (no file fabrication), matched IDs owned by caller.
- `GET /collections` → `{items:Collection[]}`; `POST /collections` `{name,description?}`; `PATCH /collections/{id}` and DELETE. Collection `{id,name,description,bookCount}`. Book collectionIds via book.patch.

Book shape:
```
{id,title,author,description,publisher,language,publishedAt,identifiers:{},tags:[],collectionIds:[],favorite:false,
 status:"queued"|"processing"|"ready"|"failed",error:null|string,
 readingStatus:"unread"|"reading"|"completed"|"abandoned",completedAt:null|string,
 progress:0,createdAt,openedAt:null|string,revision,coverUrl:null|string,
 formats:["epub"],content:null|{renditionId,format:"epub"|"pdf"|"mobi"|"fb2"|"cbz",sha256,size,url},
 files:[{id,format,size,original:boolean}]}
```
Formats may be advertised only when real engine/conversion path works. TXT can be normalized to EPUB by Calibre. Never circumvent DRM. One logical Book can have multiple BookFiles. `POST /books/{id}/files` adds a format without replacing immutable active rendition; reject incompatible accidental attachment. Originals preserved.

## Locations and annotations
Locator union: EPUB/reflowable `{type:"epub",cfi:string}`; PDF `{type:"pdf",page:number,y:number}` (zero-based page, normalized y). All positions refer to renditionId. PDF highlight anchor adds `rects:[{page,x,y,width,height}]` normalized page coordinates plus `quote,prefix?,suffix?`; EPUB anchor `{cfi,quote,prefix?,suffix?}`. Never relocate to guessed unrelated text.

Annotation `{id,bookId,renditionId,type:"highlight"|"note"|"bookmark",locator,anchor:null|object,quote:string,note:string,color:"yellow"|"green"|"blue"|"pink",revision,deleted,createdAt}`. `GET /annotations?bookId=&q=` paginated; `GET /annotations/export?format=json|markdown&bookId=` attachment. Writes through sync ops.

Position `{id,bookId,deviceId,renditionId,locator,progress,revision,updatedAt}`; materialized reading state Book.progress is latest **accepted explicit** position, never maximum; device position remains authoritative for resuming that device. Offer another device position explicitly, sorted by server revision (receipt order, not claim of last actual reading).

## Synchronization
- `POST /devices` `{id,name}` registers stable client UUID → Device; `GET /devices` `{items}`.
- `GET /sync/changes?after=0&limit=500` → `{changes:[{revision,entity,id,value}],nextCursor,hasMore}`. Full snapshots/tombstones. Entities book,position,annotation,preferences,stats,collection. Atomic local apply + cursor. Initial metadata sync paginate fully; library API itself is server paginated for normal browsing.
- `POST /sync/ops` `{deviceId,ops:[{opId,kind,baseRevision?,...}]}` → `{results:[{opId,status:"applied"|"conflict"|"rejected",revision?,current?,code?,message?}]}`. Ordered independent transactions. Unique `(user,device,opId)`, payload hash detects ID reuse; operation receipt and effect in same transaction. All writers lock user row when producing revisions, including worker.
- `position.put`: `{bookId,renditionId,baseRevision:null|N,value:{locator,progress}}` uses device's position revision; position ID `${bookId}:${deviceId}`.
- `annotation.put`: `{annotationId,baseRevision:null|N,value:{bookId,renditionId,type,locator,anchor,quote,note,color}}`.
- `annotation.delete`: `{annotationId,baseRevision:N}` tombstone.
- `book.patch`: `{bookId,baseRevision:N,value:{...metadata/readingStatus/completedAt/favorite/tags/collectionIds/openedAt}}`.
- `book.delete`: `{bookId,baseRevision:N}`.
- `preferences.put`: `{baseRevision:null|N,value:ReaderPreferences}`.
- `activity.append`: `{eventId,bookId,sessionId,sequence,activeMs,localDate,timezoneOffset}` max 60000 ms/segment, unique user/event, monotonic session sequence. localDate is explicitly user-device-reported historical date, not trusted ordering. Server receipt time retained. Statistics identify estimates; receipt date fallback for invalid dates. No double-counting replay.
- Conflicts persist original outcome; keep-mine uses a **new ID** and returned current revision. Pending conflicting annotation remains locally available; offer use-server/keep-mine/copy text, never discard silently.

## Preferences / analytics
- `GET /preferences` → `{revision,value:ReaderPreferences}`; updates via sync.
- ReaderPreferences `{theme:"light"|"dark"|"sepia",fontFamily:string,fontSize:number,lineHeight:number,paragraphSpacing:number,margin:number,maxWidth:number,flow:"paginated"|"scrolled",pdfMode:"continuous"|"single",goalMinutes:number,goalBooks:number,bookOverrides:Record<string,Partial<ReaderPreferences>>}`; avoid recursive override nesting in implementation types.
- `GET /stats?days=365` → `{totalActiveMs,booksStarted,booksCompleted,currentStreak,daily:[{date,activeMs}],byBook:[{bookId,title,activeMs}],recentSessions:[{id,bookId,activeMs,receivedAt}],goalMinutes,goalBooks}`. `GET /books/{id}/sessions` paginated actual accepted session summaries. Active time only visible+recent interaction, measured monotonic, bounded segments persisted periodically.
- `GET /health` liveness; `/health/ready` verifies database/storage.

## Frontend boundaries
`frontend/` React TS Vite strict + Tailwind, TanStack Query, Dexie, router, lucide icons. UI designer owns app outside `src/reader/`; reader specialist owns only `frontend/src/reader/` and `frontend/vendor/` and communicates dependencies, no package.json edits. Shared bridge is **ReaderScreen props**:
```
interface ReaderScreenProps {
 book: Book;
 source: string | Blob; // online URL supports range; offline Blob explicitly downloaded
 initialPosition: Position | null;
 otherPositions: Position[];
 annotations: Annotation[];
 preferences: ReaderPreferences;
 onPosition(locator: Locator, progress: number): void;
 onAnnotation(value: Omit<Annotation,"revision"|"deleted"|"createdAt">): void;
 onAnnotationDelete(id: string): void;
 onPreferences(value: ReaderPreferences): void;
 onActivity(activeMs: number): void;
 onClose(): void;
}
```
Export `ReaderScreen` default from `src/reader/ReaderScreen.tsx`. Shared types provided by designer in `src/types.ts` following above shapes. Reader specialist may keep internal structural types and import shared types once present. Designer supplies route loader, source resolution, annotations/state sync callbacks. Reader owns all in-reader UI/controls and visual polish; follows shell design warm ivory/ink/forest, serif reading typography.

Offline records account namespaced. Dexie stores entity snapshots, queue, verified downloaded blobs keyed rendition hash, preferences and cursor; stable device UUID. Flush on reconnect and periodic retry, local persistence before saved status. Debounce positions and flush visibility/navigation. Service worker app-shell only, no automatic authenticated API/book cache. Explicit quota/persistence/eviction messaging and removal. Logout online clears account local data only after pending-edit warning; expired session retains queued edits until same account login.

## Dependencies and operational boundaries
Foliate MIT pin `78914aef4466eb960965702401634c2cb348e9b1` (vendor actual source and notices); PDF.js Apache2 `6.3.289` verify npm availability then exact pin. Actual engine APIs researched; no mock reader. Foliate CSP must prevent document scripts; package production build test required. Conversion optional isolated Calibre worker, no network/resource-limited/unprivileged. App code MIT unless dependency obligations dictate otherwise. Optional Anna's Archive external search link in settings/details only; no purported stable API or scraping.

Backend owns `backend/` including migrations/tests/Dockerfile/pyproject. UI owns `frontend/` excluding reader/vendor and including package.json/Dockerfile. Operations owns root compose, Caddy, scripts, docs except this contract; frontend/backend specialists supply actual launch commands. Parent owns root Git operations and gate record. No specialist commits while lanes run.
