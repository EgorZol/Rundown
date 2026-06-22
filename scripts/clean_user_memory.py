"""Одноразовая чистка user_memory_items от целевых/планово-гоночных строк.

Запуск:
  .venv/bin/python scripts/clean_user_memory.py            # dry-run (показать что попадёт под нож)
  .venv/bin/python scripts/clean_user_memory.py --apply    # реально удалить (is_active=0)

Логика — точно та же, что в bot._classify_bad_memory: если строка матчит
паттерн «цель/план/гонка с датой/LTHR/вес/часовой пояс» — она должна быть
в структурной таблице (training_goal, races, user_profile_overrides, …),
а не в user_memory_items.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Делаем модуль bot импортируемым без запуска бота —
# импортируем только статическую функцию-классификатор.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from garmin_backup_bot.bot import _classify_bad_memory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data" / "app.db"))
    parser.add_argument("--apply", action="store_true", help="Реально удалить (по умолчанию dry-run)")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "SELECT id, user_id, content FROM user_memory_items WHERE is_active = 1 ORDER BY user_id, id"
    ).fetchall()

    to_remove: list[tuple[int, int, str, str]] = []
    keep_count = 0
    for item_id, user_id, content in rows:
        reason = _classify_bad_memory(content)
        if reason:
            to_remove.append((item_id, user_id, content, reason))
        else:
            keep_count += 1

    print(f"Всего активных заметок: {len(rows)}")
    print(f"Останется после чистки: {keep_count}")
    print(f"Под нож: {len(to_remove)}")
    print()
    current_user: int | None = None
    for item_id, user_id, content, reason in to_remove:
        if user_id != current_user:
            print(f"\n--- user_id={user_id} ---")
            current_user = user_id
        snippet = content if len(content) <= 90 else content[:90] + "…"
        print(f"  #{item_id}  [{reason}]  {snippet}")

    if not args.apply:
        print("\n(dry-run — ничего не изменено; запусти с --apply, чтобы применить)")
        return 0

    if not to_remove:
        print("\nНечего удалять.")
        return 0

    ids = [str(r[0]) for r in to_remove]
    conn.execute(
        f"UPDATE user_memory_items SET is_active = 0 WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    )
    conn.commit()
    print(f"\n✅ Деактивировано {len(to_remove)} заметок.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
