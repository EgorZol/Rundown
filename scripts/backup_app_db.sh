#!/usr/bin/env bash
# Daily backup of data/app.db with 14-day rotation.
# Использует python sqlite3 .backup() — WAL-safe, не требует остановки бота.
# Запускается systemd-user timer (см. deploy/systemd/garmin-backup-db.{service,timer}).

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/evg/garmin-analysis-and-backup}"
PYTHON="${PROJECT_DIR}/.venv/bin/python"
SRC="${PROJECT_DIR}/data/app.db"
BACKUP_DIR="${PROJECT_DIR}/data/backups"
KEEP_DAYS="${KEEP_DAYS:-14}"

mkdir -p "${BACKUP_DIR}"

if [[ ! -f "${SRC}" ]]; then
    echo "backup: ${SRC} не найден, пропускаю" >&2
    exit 0
fi

STAMP="$(date +%F)"  # 2026-06-22
OUT="${BACKUP_DIR}/app-${STAMP}.db"

# WAL-safe online backup через sqlite3.Connection.backup()
"${PYTHON}" - <<EOF
import sqlite3, sys
src = sqlite3.connect("${SRC}")
dst = sqlite3.connect("${OUT}")
src.backup(dst)
dst.close(); src.close()
# Verify backup opens and has tables
v = sqlite3.connect("${OUT}")
n = v.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
v.close()
print(f"backup verified: {n} schema rows")
EOF

SIZE="$(du -h "${OUT}" | cut -f1)"
echo "backup: ${OUT} (${SIZE})"

# Rotation
find "${BACKUP_DIR}" -maxdepth 1 -name 'app-*.db' -mtime "+${KEEP_DAYS}" -delete -print
