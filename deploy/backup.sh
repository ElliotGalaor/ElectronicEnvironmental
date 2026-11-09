#!/bin/sh
# Copies the SQLite database to backups/ (consistent copy, made while the app is running).
# The 14 most recent copies are kept. setup.sh runs it every night.
set -eu
cd "$(dirname "$0")"
mkdir -p backups
stamp=$(date +%Y-%m-%d_%H%M%S)

docker compose exec -T app python -c "
import sqlite3
src, dst = sqlite3.connect('/data/components.db'), sqlite3.connect('/data/backup.db')
src.backup(dst)
dst.close()
"
docker compose cp app:/data/backup.db "backups/components-$stamp.db"
docker compose exec -T app rm -f /data/backup.db

# shellcheck disable=SC2012  # file names are generated above: no spaces or special characters
ls -1t backups/components-*.db | tail -n +15 | xargs -r rm --
echo "Backup saved: backups/components-$stamp.db"
