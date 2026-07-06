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

# ── Per-user Garmin БД + JSON (splits/laps/HRV) ──────────────────────────────
# До 2026-07-06 не бэкапились вовсе. .garth_tokens сознательно НЕ копируем:
# это секреты, восстановимые повторным логином.
USERS_DIR="${PROJECT_DIR}/data/users"
USERS_KEEP_DAYS="${USERS_KEEP_DAYS:-7}"
USERS_OUT="${BACKUP_DIR}/users/${STAMP}"

if [[ -d "${USERS_DIR}" ]]; then
    "${PYTHON}" - <<EOF
import shutil, sqlite3
from pathlib import Path

users_dir = Path("${USERS_DIR}")
out_root = Path("${USERS_OUT}")
n_db = n_json = 0
for user_dir in sorted(p for p in users_dir.iterdir() if p.is_dir()):
    dst = out_root / user_dir.name
    # SQLite — только через .backup() (WAL-safe при работающем боте)
    for db in sorted((user_dir / "DBs").glob("*.db")):
        (dst / "DBs").mkdir(parents=True, exist_ok=True)
        src_c = sqlite3.connect(db)
        dst_c = sqlite3.connect(dst / "DBs" / db.name)
        src_c.backup(dst_c)
        dst_c.close(); src_c.close()
        n_db += 1
    # JSON-каталоги — обычное копирование
    for sub in ("splits", "laps", "HRV"):
        src_sub = user_dir / sub
        if src_sub.is_dir():
            shutil.copytree(src_sub, dst / sub, dirs_exist_ok=True)
            n_json += sum(1 for _ in (dst / sub).iterdir())
print(f"users backup: {n_db} БД, {n_json} JSON-файлов")
EOF
    echo "users backup: ${USERS_OUT} ($(du -sh "${USERS_OUT}" | cut -f1))"
    # Rotation: каталоги старше USERS_KEEP_DAYS дней
    find "${BACKUP_DIR}/users" -maxdepth 1 -mindepth 1 -type d -mtime "+${USERS_KEEP_DAYS}" -print -exec rm -rf {} +
fi
