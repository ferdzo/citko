Foliate.js source pinned to johnfactotum/foliate-js commit
78914aef4466eb960965702401634c2cb348e9b1 (MIT; see LICENSE).
Only runtime modules used by the reflowable/fixed-layout engines are included.
vendor/zip.js is bundled @zip.js/zip.js 2.7.52 (see vendor/ZIPJS-LICENSE).
vendor/fflate.js is bundled fflate 0.8.2 (see vendor/FFLATE-LICENSE).

Local security/cleanup changes: iframe sandboxes omit allow-scripts; View.close()
destroys the book to revoke resource blob URLs; PDF import is disabled in favor
of separately pinned pdfjs-dist 6.3.289. No author-supplied JavaScript runs.
Also guard TOC label lookup for books without TOC progress and use a valid
locale for searches in books without a language identifier; preserve native
touch text selection before handling page swipes.
Readers should prevent outbound media loads with a restrictive Content-Security-
Policy; see the integration requirements in frontend/src/reader/README.md.
