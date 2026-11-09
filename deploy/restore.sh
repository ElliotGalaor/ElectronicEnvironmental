#!/bin/sh
# Restores the database from a backup made by backup.sh. The app keeps running.
#
# Usage: ./restore.sh backups/components-2026-11-10_030000.db
set -eu
FILE="${1:?Usage: ./restore.sh <backup file>}"
[ -f "$FILE" ] || { echo "File not found: $FILE" >&2; exit 1; }
cd "$(dirname "$0")"
case "$FILE" in /*) ;; *) FILE="$OLDPWD/$FILE" ;; esac

docker compose cp "$FILE" app:/tmp/restore.db
docker compose exec -T app python -c "
import sqlite3
src, dst = sqlite3.connect('/tmp/restore.db'), sqlite3.connect('/data/components.db')
src.backup(dst)
dst.close()
"
docker compose exec -T -u root app rm -f /tmp/restore.db
echo "Database restored from $FILE"
