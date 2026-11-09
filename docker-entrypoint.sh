#!/bin/sh
# On first start (empty volume), load the demo components.
set -e
if [ ! -f "$RRC_DB_PATH" ]; then
    echo "No database found: loading the demo data into $RRC_DB_PATH"
    python cli.py seed
fi
exec "$@"
