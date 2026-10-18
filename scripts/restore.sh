#!/usr/bin/env sh
set -eu

if [ "$#" -ne 3 ]; then
  echo "Usage: $0 BACKUP_DIRECTORY ISOLATED_ENV_FILE citko-restore-UNIQUE_NAME" >&2
  exit 2
fi
backup=$1
env_file=$2
project=$3
case "$project" in
  citko-restore-*) suffix=${project#citko-restore-} ;;
  *) echo 'Project must begin with citko-restore- and contain a unique safe suffix' >&2; exit 2 ;;
esac
case "$suffix" in
  ''|*[!a-z0-9_-]*) echo 'Project suffix may contain only lowercase letters, digits, hyphen, underscore' >&2; exit 2 ;;
esac
[ -d "$backup" ] && [ -r "$env_file" ] || { echo 'Backup directory or env file is unavailable' >&2; exit 1; }
for f in database.dump fs-data.tar fs-master-key; do
  [ -s "$backup/$f" ] || { echo "Missing or empty backup component: $f" >&2; exit 1; }
done
tar -tf "$backup/fs-data.tar" | grep -q '^data/' || { echo 'Invalid fs data archive' >&2; exit 1; }

# Force the exact backed-up key into Compose's environment without printing it.
IFS= read -r FS_MASTER_KEY < "$backup/fs-master-key" || [ -n "${FS_MASTER_KEY:-}" ]
[ -n "$FS_MASTER_KEY" ] || { echo 'Empty backed-up FS master key' >&2; exit 1; }
export FS_MASTER_KEY
compose() { docker compose --project-name "$project" --env-file "$env_file" "$@"; }

# Only ever create a new, uniquely named target project; never reuse volumes.
if [ -n "$(compose ps -a -q 2>/dev/null || true)" ]; then
  echo "Refusing project with existing containers: $project" >&2
  exit 1
fi
for volume in "${project}_postgres_data" "${project}_fs_data"; do
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "Refusing existing volume: $volume" >&2
    exit 1
  fi
done
printf 'This creates new volumes for %s and restores database and encrypted objects into them. Type the project name to continue: ' "$project" >&2
IFS= read -r answer
[ "$answer" = "$project" ] || { echo 'Confirmation did not match; no restore performed' >&2; exit 1; }

compose up -d db fs
fs_container="${project}-fs-1"
db_container="${project}-db-1"
wait_healthy() {
  service=$1 container=$2
  i=0
  while [ "$i" -lt 60 ]; do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || true)
    [ "$status" = healthy ] && return 0
    [ "$status" = unhealthy ] && break
    i=$((i + 1))
    sleep 2
  done
  echo "Service $service did not become healthy" >&2
  return 1
}
wait_healthy db "$db_container"
wait_healthy fs "$fs_container"
compose exec -T db sh -ec 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --list' < "$backup/database.dump" >/dev/null

# The project and both named volumes were just created above. Restore DB, then
# stop fs while replacing its persistent data tree to avoid concurrent access.
compose exec -T db sh -ec 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$backup/database.dump"
compose stop fs
compose run --rm --no-deps --entrypoint sh fs -ec 'tar -C / -xf -' < "$backup/fs-data.tar"
compose start fs
wait_healthy fs "$fs_container"
echo "Restore completed to new isolated project $project. API/worker were not started; validate before any cutover."
