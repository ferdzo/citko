# Backend acceptance blockers

Preliminary independent audit, not final Gate 2. Fix these before release; existing backend is not accepted.

## Security and transaction integrity
- annotation.put global-ID existing row needs owner restriction; immutable book/rendition on edits; no foreign current leakage.
- Lock/refresh user **before** receipt lookup/entity CAS. Identity map must refresh. Enforce unique(user_id,revision) changes.
- Assign/flush revision before serializing COMPLETE snapshots; new entities get revision, not only existing current variable. Position update also publishes book progress snapshot. All workers follow same ordering.
- Conflicts return authoritative current and durable original receipt; per-op forbidden/not-found failures must not abort unrelated batch. Concurrent replay one durable effect, changed-payload ID reuse rejected.
- Strict missing/invalid Origin denied unsafe; login throttle; atomic initial setup advisory lock or singleton table. Session absolute+idle expiry, revocation.
- Never run current tests against normal DATABASE_URL: test currently drops all tables. Require explicit isolated TEST_DATABASE_URL with test marker guard before any destructive cleanup.
- Explicit bounded Pydantic schemas for metadata, ops, UUIDs, locations/anchors per format, representation ownership, activity. Parse openedAt timestamps correctly.

## Contract/functionality
- JSONB for tags/collection IDs or relational joins; current JSON.contains generates invalid Postgres LIKE.
- Complete missing cover/files/import/collection PATCH DELETE/annotation export/book sessions; auth/me csrfToken.
- Activity per-user event idempotency and per-session sequence uniqueness; estimates historical dates validated, no client time ordering. Accurate streak/window/session summaries/goals from preferences, no literal placeholder values.
- Worker lease expiry/reclaim/fencing, retry/failure/success/deletion all publish consistent revisions. Metadata cover/title/author/publisher/language/date/identifiers using mature libs. Bounded archives and parsers isolated/time-limited. Preserve original; add formats real support not fabricated.
- Stream and range object data, never full file into RAM for range request, suffix and If-Range semantics correct. API validates type/content, not extension alone.
- Split independently testable services/schemas/storage from route handlers sensibly, no unnecessary abstraction.

## Required storage replacement
User explicitly requests GitHub ferdzo/fs official image, not local build. Verified upstream0.2.4 commit e442265688695281933d808ee0008adc8266e31e, image `ghcr.io/ferdzo/fs@sha256:70b2fb1f979690eb4c33d4ec11c07a5e15f90577996aac645658f00a91ef7d6b`.

Common backend environment contract (coordinate ops):
```
STORAGE_BACKEND=s3
S3_ENDPOINT_URL=http://fs:2600
S3_REGION=us-east-1
S3_BUCKET=citko
S3_ACCESS_KEY_ID=<application credential>
S3_SECRET_ACCESS_KEY=<secret>
```
Path-style SigV4, boto31.43.114 runtime verified; request_checksum_calculation/response_checksum_validation='when_required'. Use opaque immutable keys scoped user/book/file; DB never blobs or server paths. API authorizes and proxies streaming ranges rather than public S3. Worker accesses through same adapter. Readiness checks storage bucket; bucket provisioning via CLI separate startup command, explicit idempotent behavior. Local adapter optional only development, not production default.

Real restricted official image runtime passed create/head bucket, put/head/get, first/middle/suffix206, unsatisfiable416, multipart complete/list/delete, anonymous403, restart durability. Probe endpoint runtime metadata `/tmp/opencode/citko-fs-probe/runtime.json`; private env path there, never expose credential contents. Test infrastructure may use this ephemeral service; production uses Compose private fs network.

## Evidence
Real isolated Postgres tests: two-user auth/ownership resources including annotation overwrite; setup race/login throttle/origin CSRF; duplicate replay concurrent+changed payload; CAS competing requests; complete snapshot revisions+book progress; failed/reclaimed ingestion and hash duplicate; archive bounds/malformed/encrypted input; supported format reader path; S3 streaming/range and unavailable error; collections filters; annotation exports; active segment dedup/session/streak goals. Never mark mocks as real storage/engine execution.
