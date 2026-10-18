#!/usr/bin/env sh
set -eu

# Quiesce writers and object storage so SQL, object data, and its encryption key
# describe the same point. The master key file is sensitive recovery material.
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 BACKUP_DIRECTORY (must not already exist)" >&2
  exit 2
fi
dest=$1
if [ -e "$dest" ]; then
  echo "Refusing to overwrite existing path: $dest" >&2
  exit 1
fi
mkdir -p "$(dirname "$dest")"
mkdir "$dest"
was_worker=0
was_api=0
was_fs=0
running() { [ -n "$(docker compose ps --status running -q "$1")" ]; }
restart_app() {
  [ "$was_fs" -eq 1 ] && docker compose start fs >/dev/null || true
  [ "$was_api" -eq 1 ] && docker compose start api >/dev/null || true
  [ "$was_worker" -eq 1 ] && docker compose start worker >/dev/null || true
}
trap restart_app EXIT HUP INT TERM
# Preserve exactly the services that were running before the backup.
running db || { echo 'Postgres service must be running' >&2; exit 1; }
running fs || { echo 'fs service must be running' >&2; exit 1; }
running worker && was_worker=1 || true
running api && was_api=1 || true
was_fs=1
# Capture the exact running encryption key; never reconstruct it from a new .env.
docker compose exec -T fs sh -ec 'printf "%s" "$FS_MASTER_KEY"' > "$dest/fs-master-key"
chmod 600 "$dest/fs-master-key"
if [ "$was_worker" -eq 1 ]; then docker compose stop worker; fi
if [ "$was_api" -eq 1 ]; then docker compose stop api; fi
docker compose stop fs
docker compose exec -T db sh -ec 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$dest/database.dump"
docker compose run --rm --no-deps --entrypoint sh fs -ec 'tar -C / -cf - data' > "$dest/fs-data.tar"
chmod 600 "$dest/database.dump" "$dest/fs-data.tar"
printf '%s\n' 'Citko coordinated backup' "created=$(date -u +%FT%TZ)" > "$dest/INFO"
chmod 600 "$dest/INFO"
echo "Backup written to $dest (contains private books and the storage master key; protect it as sensitive data)."
