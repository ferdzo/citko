# Reading engine integration

`ReaderScreen.tsx` exports the contract's default component. Its online `source`
should be the same-origin authenticated `/api/v1/books/{id}/file` URL, not a
cross-origin redirect. PDF.js uses this URL directly with HTTP range requests;
offline PDF Blob sources become local blob URLs. Foliate.js reads a remote
EPUB/MOBI/FB2/CBZ once into a Blob, then turns pages without network requests;
offline Blobs never request book bytes from the server. All position/annotation/
settings/activity callbacks are delegated to the shell's durable sync layer.

**CSP for the containing page**: do not enable `unsafe-eval`, ever. Set
`script-src 'self'` (no inline script), `worker-src 'self' blob:`,
`frame-src 'self' blob:`, `object-src 'none'`, `base-uri 'self'`,
`connect-src 'self'`, `img-src 'self' blob: data:`,
`font-src 'self' blob: data:`. Foliate inserts author CSS and PDF.js creates
inline styles, so **`style-src 'self' 'unsafe-inline'` is required**.
The reader's book iframes use `sandbox="allow-same-origin"` *without*
`allow-scripts`, even for fixed layout. Book scripts cannot execute. Safari /
WebKit may not deliver some events into those frames (the upstream sandbox
included `allow-scripts` specifically for a WebKit bug); this is preferable to
running arbitrary ebook code. Avoid `navigate-to` policies that allow book
resources to leave the site. External EPUB book links are prevented in JS.

PDF.js version **6.3.289** is an exact dependency supplied by the shell; the
worker is imported from the same exact package via Vite. PDF basic Latin text
and selectable text layer work; PDF documents requiring optional Adobe CMaps
may need a future explicit CMap asset pipeline for full search/copy in CJK.
No PDF JavaScript/scripting manager is installed; external PDF links are off.
Encrypted PDFs requiring a password prompt are not yet supported.

Pin and notices for Foliate, bundled zip.js and fflate are under
`frontend/vendor/foliate/`. Upstream source patches are listed there.
The UI's two fonts are self-hosted under `frontend/vendor/fonts/` with their
SIL OFL notices so reading remains offline and needs no Google Fonts CSP entry.
