"""Тесты безопасности SQL-инструмента Claude (tools.py).

Регрессия здесь = запись в БД из LLM или утечка данных между пользователями,
поэтому каждый инвариант закреплён тестом.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from garmin_backup_bot.storage import Storage  # noqa: E402
from garmin_backup_bot.tools import build_tool_schemas, make_sql_runner  # noqa: E402

UID, OTHER = 111, 222


class ToolsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        # app.db со схемой бота: две гонки разных юзеров + секреты
        storage = Storage(base / "app.db")
        storage.save_race(UID, "2026-09-27", "Марафон свой", 42.2)
        storage.save_race(OTHER, "2026-10-04", "Марафон чужой", 42.2)
        storage.upsert_credentials(UID, "user@garmin.com", "SECRET_ENCRYPTED")
        # имитация per-user garmin.db
        gpath = base / "garmin.db"
        with sqlite3.connect(gpath) as g:
            g.execute("CREATE TABLE sleep (day TEXT, score INTEGER)")
            g.executemany("INSERT INTO sleep VALUES (?, ?)",
                          [(f"2026-07-{i:02d}", 80 + i) for i in range(1, 7)])
        self.run_sql = make_sql_runner(
            {"app": str(base / "app.db"), "garmin": str(gpath)}, UID
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_select_works(self):
        out = self.run_sql("garmin", "SELECT day, score FROM sleep ORDER BY day LIMIT 2")
        self.assertIn("2026-07-01", out)

    def test_writes_blocked(self):
        for sql in ("DELETE FROM sleep", "UPDATE sleep SET score=0",
                    "INSERT INTO sleep VALUES ('x', 1)", "DROP TABLE sleep",
                    "PRAGMA writable_schema=1"):
            out = self.run_sql("garmin", sql)
            self.assertIn("ошибка", out, sql)
        # данные не изменились
        out = self.run_sql("garmin", "SELECT COUNT(*) AS n FROM sleep")
        self.assertIn("'n': 6", out)

    def test_pragma_table_info_allowed(self):
        out = self.run_sql("garmin", "PRAGMA TABLE_INFO(sleep)")
        self.assertIn("score", out)

    def test_app_view_isolated_by_user(self):
        out = self.run_sql("app", "SELECT name FROM races")
        self.assertIn("Марафон свой", out)
        self.assertNotIn("чужой", out)

    def test_secret_tables_invisible(self):
        for tbl in ("garmin_credentials", "web_tokens"):
            out = self.run_sql("app", f"SELECT * FROM {tbl}")
            self.assertIn("ошибка", out, tbl)
        # и содержимое секретов не достижимо в принципе
        out = self.run_sql("app", "SELECT name FROM sqlite_master WHERE type='table'")
        self.assertNotIn("credentials", out)

    def test_unknown_db_key(self):
        self.assertIn("не найдена", self.run_sql("nope", "SELECT 1"))

    def test_result_capped_at_200_rows(self):
        with sqlite3.connect(Path(self._tmp.name) / "garmin.db") as g:
            g.executemany("INSERT INTO sleep VALUES (?, ?)",
                          [(f"d{i}", i) for i in range(300)])
        out = self.run_sql("garmin", "SELECT day FROM sleep")
        self.assertEqual(out.count("'day'"), 200)


class TestToolSchemas(unittest.TestCase):
    def test_read_only_without_callbacks(self):
        names = [t["name"] for t in build_tool_schemas()]
        self.assertEqual(names, ["query_health_db", "query_activities_db", "query_app_db"])

    def test_write_tools_added_with_callbacks(self):
        wt = {k: (lambda **kw: "OK") for k in
              ("confirm_fact", "remember_note", "forget_note",
               "set_race_result", "record_feeling", "set_training_goal",
               "add_race", "delete_race", "set_race_priority", "retract_fact",
               "invoke_action", "set_weight", "set_lthr", "set_timezone", "set_experience")}
        names = [t["name"] for t in build_tool_schemas(save_plan_fn=lambda p, w: "OK",
                                                       write_tools=wt)]
        self.assertEqual(len(names), 19)
        self.assertIn("retract_fact", names)
        self.assertIn("invoke_action", names)
        self.assertIn("set_weight", names)
        self.assertIn("save_weekly_plan", names)
        self.assertIn("add_race", names)
        self.assertIn("delete_race", names)
        self.assertIn("set_race_priority", names)
        for t in build_tool_schemas(save_plan_fn=lambda p, w: "OK", write_tools=wt):
            self.assertIn("input_schema", t)
            self.assertIn("description", t)


class TestWriteToolDispatch(unittest.TestCase):
    """call_write_tool: sync-коллбеки зовутся напрямую, корутины await'ятся.

    Регресс 10.07.2026: async-коллбек оборачивался в asyncio.run() внутри
    работающего loop → RuntimeError → молчаливый fallback (авто-парсинг
    гонок из цели не работал никогда).
    """

    def test_sync_and_async_callbacks(self):
        import asyncio
        from garmin_backup_bot.analyst import call_write_tool

        def sync_fn(x: int) -> str:
            return f"sync {x}"

        async def async_fn(x: int) -> str:
            await asyncio.sleep(0)
            return f"async {x}"

        async def main():
            r1 = await call_write_tool(sync_fn, {"x": 1})
            r2 = await call_write_tool(async_fn, {"x": 2})
            return r1, r2

        r1, r2 = asyncio.run(main())
        self.assertEqual(r1, "sync 1")
        self.assertEqual(r2, "async 2")


if __name__ == "__main__":
    unittest.main()
