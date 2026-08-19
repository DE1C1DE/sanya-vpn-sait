#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="/opt/tri-bukvy"
BACKUP_DIR="${1:-/var/backups/tri-bukvy}"
install -d -m 0700 "${BACKUP_DIR}"
stamp="$(date +%Y%m%d-%H%M%S)"
sqlite3 "${APP_DIR}/data.sqlite3" ".backup '${BACKUP_DIR}/data-${stamp}.sqlite3'"
chmod 600 "${BACKUP_DIR}/data-${stamp}.sqlite3"
echo "Создана резервная копия: ${BACKUP_DIR}/data-${stamp}.sqlite3"
