#!/bin/sh
# Au premier démarrage (volume vide), charge les composants de démonstration.
set -e
if [ ! -f "$RRC_DB_PATH" ]; then
    echo "Base absente : chargement des données de démonstration dans $RRC_DB_PATH"
    python cli.py seed
fi
exec "$@"
