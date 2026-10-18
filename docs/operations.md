# Deployment and operations

## Production deployment

Requirements: Docker Engine with Compose v2, a public DNS name pointing to this host, and inbound TCP 80/443 allowed for Caddy certificate issuance/renewal. Keep `.env` private; it contains database, storage and setup credentials. Generate independent secrets:

```sh
umask 077
cp .env.example .env
openssl rand -hex 32 # POSTGRES_PASSWORD
openssl rand -hex 32 # SETUP_TOKEN
openssl rand -base64 32 # FS_MASTER_KEY (32 random bytes)
openssl rand -hex 24 # FS_ROOT_PASSWORD
```

Put those values and your actual site in `.env`. Set `SITE_ADDRESS=books.example.com`, `APP_ENV=production`, and `ALLOWED_ORIGINS=https://books.example.com`. Origins are exact browser origins (scheme/host/port), not URLs with paths. `FS_MASTER_KEY` encrypts stored object data and is irreplaceable: retain it in an approved secret manager and make protected recovery copies. Never substitute a newly generated key for an existing fs data volume. Do not publish database, API, or fs ports directly.

Before starting the application, provision a dedicated bucket-scoped S3 application key/secret for bucket `citko` through the official fs administration mechanism for the pinned release. Do not use root credentials for app access in normal operation. If administration requires the API, temporarily set `ADMIN_API_ENABLED=true`, then start only `docker compose up -d fs`; administer via the official supported method from inside the private Docker network. Do not publish fs port or use undocumented commands. Create the scoped key, put it in `S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY`, disable the admin API, and recreate fs with `docker compose up -d --force-recreate fs`. Verify fs health, then proceed with the full stack. The root account remains only for storage administration.

```sh
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 fs storage-init api worker migrate caddy
```

Compose runs the pinned official image `ghcr.io/ferdzo/fs@sha256:70b2fb1f979690eb4c33d4ec11c07a5e15f90577996aac645658f00a91ef7d6b` (upstream 0.2.4, commit `e442265688695281933d808ee0008adc8266e31e`). Its `/healthz` check gates one-shot `storage-init` (`python -m citko.cli init-storage`), which idempotently provisions/checks the bucket. Alembic migration is one-shot; API and worker wait for it and successful storage initialization. Verify `https://books.example.com/api/v1/health/ready`. Create the first administrator once through `POST /api/v1/auth/setup` with JSON `{email,password,name}`, header `X-Setup-Token: <SETUP_TOKEN>`, and an allowed Origin. Password must be at least 12 characters. The token is not printed by deployment. Subsequent users use the authorized admin interface/CLI when implemented. Remove/rotate setup token after initial provisioning and restart API.

`ADMIN_API_ENABLED` defaults to false. It is not needed by API/worker and should stay disabled after bootstrap. Never retain root access keys in the API or worker.

Set `APP_ENV=development` only for isolated local HTTP use. The API then makes cookies non-Secure, and this must not be used publicly or behind a TLS-terminating proxy. Local settings: `SITE_ADDRESS=:80`, `ALLOWED_ORIGINS=http://localhost:8080`, `HTTP_PORT=8080`, `HTTPS_PORT=8443`.

## Architecture and security

Caddy is the only published application service. It routes `/api/*` unchanged to API and all other paths to frontend; SPA fallback is the frontend server's responsibility and API file requests retain Range behavior. Caddy persists certificates in `caddy_data`. Postgres, fs and apps share an internal network; worker cannot reach arbitrary internet destinations or the host. Caddy alone additionally joins the outbound edge network. There is no Docker socket mount. `postgres_data` and `fs_data` persist Postgres and fs `/data`. Fs port 2600 has no host publication; API/worker use the S3 adapter and API remains the authorization boundary.

The fs image is pinned to the official upstream digest, runs UID 10001, read-only root, bounded `/tmp`, no capabilities, `no-new-privileges`, and CPU/memory/PID limits. Worker has equivalent confinement. Conversion, if ever enabled, belongs only in the restricted worker. The configured CSP blocks document scripts and `unsafe-eval`, while allowing blob URLs needed by reader workers/documents; test against the actual frontend before exposure. HSTS is emitted; browsers cache it.

Compose limits are starting defaults, not capacity guarantees. Monitor disk, memory, CPU, fs/Postgres health, Caddy renewals, and rotated container logs. Configure host alerts and protect backups. Do not assume logs contain no sensitive data.

## Upgrade and rollback

1. Read release and migration notes; take and validate a coordinated backup first.
2. Update source, then `docker compose build` and `docker compose up -d --build`. The one-shot migration must succeed before apps start. The pinned fs image changes only with an intentional reviewed digest update.
3. Confirm `docker compose ps`, logs and `/api/v1/health/ready`; exercise login, upload and file-range retrieval.
4. Keep previous source/images and backup. Roll back application versions only if migrations are backward-compatible. Schema downgrade is not automatic and can lose data; test a full database+fs restoration in isolation. Never first-test restore on production.

Do not change PostgreSQL major version without its documented data upgrade procedure.

## Coordinated backup and restore

Metadata and objects must be from one point in time. `scripts/backup.sh` captures the running `FS_MASTER_KEY`, stops API/worker and fs to quiesce writes, dumps Postgres and archives the complete fs `/data`, then restarts services:

```sh
scripts/backup.sh /secure/backups/citko-$(date -u +%Y%m%dT%H%M%SZ)
scripts/restore-check.sh /secure/backups/citko-YYYYMMDDTHHMMSSZ
```

Destination must not exist. Backup includes private books and the storage master key: keep owner-only permissions, encrypt at rest, store off-host, and restrict access. `.env` and Caddy certificates are not included. Store database credentials, fs root credentials, and scoped S3 keys separately in a secret manager. Test recovery periodically.

Restore only to a fresh isolated Compose project with empty `postgres_data` and `fs_data`. First run `restore-check.sh`; recover the exact `FS_MASTER_KEY` value from `fs-master-key` into that isolated `.env` before starting fs. Then start only the target `db` and `fs`. Keep API and worker stopped. These commands overwrite their target data; verify project name, volume emptiness, and backup path before each command:

```sh
docker compose exec -T db sh -ec 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < BACKUP_DIRECTORY/database.dump
docker compose run --rm --no-deps --entrypoint sh fs -ec 'tar -C / -xf -' < BACKUP_DIRECTORY/fs-data.tar
```

Start the full stack and test storage readiness and representative book reads/ranges in staging before production recovery. For production, establish a maintenance window and retain the failed deployment volumes independently before replacement. The original fs master key is required to decrypt data. Never restore over the only production copy.

Alternatively, `scripts/restore.sh BACKUP_DIRECTORY ISOLATED_ENV_FILE citko-restore-UNIQUE_SUFFIX` performs this restoration into new project-named volumes. The env file must contain normal Compose credentials; the script supplies the backed-up master key without printing it. It rejects an existing project or either existing volume, prompts for the exact project name, starts only Postgres/fs, validates the archive, restores both datasets, and leaves API/worker stopped. Example: `scripts/restore.sh /secure/backups/citko-... /secure/restore/citko.env citko-restore-20261018a`. Use a unique `citko-restore-...` project and test it before any cutover; never point it at production. `restore-check.sh` is a read-only structure/format check and requires the selected project's DB service running.

## Maintenance

Monitor storage volume and database disk space. No generic orphan-object cleanup is provided: reconcile candidate objects with DB references and application retention semantics first. Respect tombstone semantics. Browser-offline records are evicted by users through client quota controls; server operators cannot safely remove those copies. Coordinate requests with users and applicable law.

## Licensing and conversion

Citko is MIT licensed. The pinned upstream fs image is MIT; Foliate MIT, PDF.js Apache-2.0, and optional Calibre GPL-3.0. Preserve notices and assess obligations for exact release artifacts. Calibre conversion is optional, resource-constrained, isolated from outbound network, and must not circumvent DRM. Anna's Archive is only an optional user-directed external link if present; there is no scraping or service integration.
