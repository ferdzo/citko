# Browser integration evidence

These tests drive the deployed web application and its real API, PostgreSQL,
worker, Foliate EPUB reader and PDF.js reader. They do not mock the API or
reader engines. Run against an isolated test deployment: uploads and setup
create persistent user data.

```sh
cd e2e
npm install
npx playwright install chromium
python3 fixtures/generate.py
BASE_URL=http://localhost:8080 \
  E2E_EMAIL=reader@example.test E2E_PASSWORD='citko-e2e-password-123' \
  SETUP_TOKEN='deployment-setup-token' npm test
```

`BASE_URL` defaults to `http://localhost:8080`. `SETUP_TOKEN` is needed only
when `/api/v1/auth/setup` reports that first-admin setup is still required.
The default email/password above are used only for that one-time setup; set
the same credentials for subsequent runs. Services must be started separately
with database, API and worker available. Tests wait on user-visible readiness
and API state rather than fixed sleeps.

The ownership test is skipped unless `E2E_OTHER_EMAIL` and
`E2E_OTHER_PASSWORD` identify a separately provisioned account. It requests
the primary account's file as that user and expects 403/404. Run harness
syntax/type validation with `npx tsc --noEmit`; run tests with `npm test`.
See `fixtures/README.md` for fixture licensing and regeneration.

The main scenario asserts real EPUB iframe text, accepted EPUB CFI position,
device-specific resume with an explicit other-device jump, PDF canvas/text
layer/search/page restoration, explicit offline download and reopen, offline
bookmark/highlight/note persistence followed by API-visible sync, plus sync-op
duplicate replay and stale-revision CAS conflict receipts. Browser screenshots
and traces are retained on failed tests. The additional ownership test requires
the second account described above. Use an isolated deployment because this
scenario mutates book metadata and adds annotations.
