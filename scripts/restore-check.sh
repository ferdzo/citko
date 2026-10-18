#!/usr/bin/env sh
set -eu
if [ "$#" -ne 1 ] || [ ! -d "$1" ]; then
  echo "Usage: $0 BACKUP_DIRECTORY" >&2
  exit 2
fi
for f in database.dump fs-data.tar fs-master-key; do
  [ -s "$1/$f" ] || { echo "Missing or empty $1/$f" >&2; exit 1; }
done
docker compose exec -T db sh -ec 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --list' < "$1/database.dump" >/dev/null
tar -tf "$1/fs-data.tar" | grep -q '^data/' || { echo 'Archive has no data/ tree' >&2; exit 1; }
key_bytes=$(wc -c < "$1/fs-master-key" | tr -d ' ')
[ "$key_bytes" -ge 40 ] || { echo 'FS master key backup is unexpectedly short' >&2; exit 1; }
echo "Backup structure is readable; restore is NOT performed. See docs/operations.md."
