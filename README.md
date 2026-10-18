# Citko

Citko is a self-hosted personal ebook library and reading application. Its API, database, import worker, S3-compatible object store, and browser client run as separate Compose services; Caddy provides same-origin routing and public HTTPS. See [the implementation contract](docs/contract.md) for API and data semantics and [deployment/operations](docs/operations.md) for lifecycle instructions.

## Repository

- `backend/` — FastAPI service, persistence and (planned) import worker.
- `frontend/` — React/Vite client and reading interface.
- `compose.yaml`, `deploy/` — production-oriented Compose, pinned official fs storage image, and Caddy configuration.
- `scripts/` — coordinated Postgres/object-store backup and backup validation helpers.
- `docs/` — architecture contract and operator runbook.

## Capabilities and boundaries

The contract describes accounts, personal libraries, metadata, reading positions, annotations, sync, and offline client records. Actual support depends on the implementation and must be tested against the running release; do not infer that every contract item is already complete. File conversion is optional and belongs in a restricted worker, never in the HTTP API. DRM circumvention is not supported. Citko does not bundle or scrape Anna's Archive; a user-initiated external link, if present in the client, is not an integration or endorsement.

## Development

Copy `.env.example` to `.env`, generate secrets as in the deployment guide, then run `docker compose up --build`. For local HTTP only, set `APP_ENV=development`, `SITE_ADDRESS=:80`, `ALLOWED_ORIGINS=http://localhost:8080`, `HTTP_PORT=8080`, and `HTTPS_PORT=8443`. Open <http://localhost:8080>. Development mode intentionally disables Secure cookies and must not be exposed publicly.

Production setup, initial admin provisioning, upgrades, backups and recovery are documented in [docs/operations.md](docs/operations.md). Runtime and end-to-end status remain unverified until the backend/frontend implementation lanes have been integrated and tested.

## Licensing

The application is MIT licensed. Bundled or pinned dependencies retain their own licenses: the pinned official `ferdzo/fs` image is MIT, Foliate is MIT, PDF.js is Apache-2.0, and optional Calibre conversion is GPL-3.0. Consult shipped upstream notices and verify the actual versions included in a release before redistributing. Users are responsible for lawful handling of their books.
