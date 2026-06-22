"""Разовая рассылка сообщения юзерам бота. Не конфликтует с polling-ботом
(send_message — отдельный endpoint).

Использование:
  .venv/bin/python scripts/broadcast.py --to 172354679 --message-file msg.txt
  .venv/bin/python scripts/broadcast.py --to 172354679,631939244 --message-file msg.txt
  .venv/bin/python scripts/broadcast.py --all-active --message-file msg.txt   # все привязавшие Garmin
  .venv/bin/python scripts/broadcast.py --to 172354679 --message-file msg.txt --dry-run

Формат сообщения: HTML (parse_mode=HTML).
**жирный** -> <b>жирный</b> конвертируется автоматически.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # type: ignore
from telegram import Bot  # type: ignore


def md_bold_to_html(text: str) -> str:
    """**жирный** -> <b>жирный</b>. Защищает <, > и & до конверсии."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    return text


def get_active_user_ids(db_path: Path) -> list[int]:
    """Все юзеры с привязанным Garmin."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT user_id FROM garmin_credentials ORDER BY user_id").fetchall()
    conn.close()
    return [r[0] for r in rows]


async def send_one(bot: Bot, chat_id: int, text: str, dry: bool) -> bool:
    if dry:
        print(f"[dry] would send to {chat_id} ({len(text)} chars)")
        return True
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                               disable_web_page_preview=True)
        print(f"  OK → {chat_id}")
        return True
    except Exception as exc:
        print(f"  FAIL → {chat_id}: {exc}")
        return False


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", help="user_id или список через запятую")
    parser.add_argument("--all-active", action="store_true", help="всем привязавшим Garmin")
    parser.add_argument("--message-file", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=str(ROOT / "data" / "app.db"))
    args = parser.parse_args()

    if not args.to and not args.all_active:
        parser.error("Укажи --to или --all-active")

    load_dotenv(ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN не найден в .env", file=sys.stderr)
        return 2

    raw = args.message_file.read_text(encoding="utf-8").strip()
    text = md_bold_to_html(raw)

    if args.all_active:
        ids = get_active_user_ids(Path(args.db))
    else:
        ids = [int(x.strip()) for x in args.to.split(",") if x.strip()]

    print(f"Получатели: {ids}")
    print(f"Длина сообщения: {len(text)} символов (HTML)")
    print()

    bot = Bot(token=token)
    ok = 0
    for uid in ids:
        if await send_one(bot, uid, text, args.dry_run):
            ok += 1
    print()
    print(f"Готово: {ok}/{len(ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
