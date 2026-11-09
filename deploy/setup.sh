#!/bin/sh
# Installs Docker on a fresh Debian or Ubuntu server and starts the RoHS / REACH Checker behind Caddy (HTTPS).
# Run it again at any time: it keeps the existing settings (.env) and data.
#
# Usage: sudo ./setup.sh <domain>        e.g. sudo ./setup.sh rohs-checker.duckdns.org
set -eu

DOMAIN="${1:?Usage: sudo ./setup.sh <domain>}"
APP_DIR=/opt/rohs-checker
SRC_DIR=$(cd "$(dirname "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this script with sudo." >&2
    exit 1
fi

# 1. Swap file: the free e2-micro VM only has 1 GB of memory
if [ -z "$(swapon --show)" ]; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# 2. Docker Engine and the Compose plugin, from Docker's official repository; cron for the backups
if ! command -v docker >/dev/null 2>&1; then
    apt-get update
    apt-get install -y ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    # shellcheck disable=SC1091  # provided by the server's OS
    . /etc/os-release
    curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
if ! command -v cron >/dev/null 2>&1; then
    apt-get update
    apt-get install -y cron
fi

# 3. Application files; .env is created on the first run only, with a random admin token
mkdir -p "$APP_DIR"
cp "$SRC_DIR/docker-compose.yml" "$SRC_DIR/Caddyfile" "$SRC_DIR/backup.sh" "$SRC_DIR/restore.sh" "$APP_DIR/"
chmod +x "$APP_DIR/backup.sh" "$APP_DIR/restore.sh"
if [ ! -f "$APP_DIR/.env" ]; then
    token=$(od -An -tx1 -N24 /dev/urandom | tr -d ' \n')
    umask 077
    printf 'DOMAIN=%s\nAPP_VERSION=latest\nRRC_ADMIN_TOKEN=%s\nANTHROPIC_API_KEY=\n' "$DOMAIN" "$token" > "$APP_DIR/.env"
    umask 022
    echo "Admin token (needed to edit the database), also kept in $APP_DIR/.env:"
    echo "    $token"
else
    sed -i "s|^DOMAIN=.*|DOMAIN=$DOMAIN|" "$APP_DIR/.env"
fi

# 4. Start the stack and back up the database every night at 03:00
cd "$APP_DIR"
docker compose pull
docker compose up -d
echo "0 3 * * * root $APP_DIR/backup.sh >> /var/log/rohs-checker-backup.log 2>&1" > /etc/cron.d/rohs-checker-backup

echo "Done: https://$DOMAIN (the first certificate can take a minute)."
