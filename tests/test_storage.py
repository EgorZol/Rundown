"""Тесты Storage на временной SQLite — CRUD, миграции, retention, гарды дублей."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from garmin_backup_bot.storage import Storage  # noqa: E402

UID = 100500


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "app.db")

    def tearDown(self):
        self._tmp.cleanup()


class TestSchema(StorageTestCase):
    def test_schema_created_and_migration_idempotent(self):
        # повторная инициализация (рестарт бота) не должна падать
        again = Storage(Path(self._tmp.name) / "app.db")
        with again._connect() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("garmin_credentials", "conversation_messages", "weekly_plans",
                  "races", "verified_facts", "user_memory_items", "food_entries",
                  "token_usage", "usage_events", "training_goal"):
            self.assertIn(t, tables)


class TestCredentialsAndTokens(StorageTestCase):
    def test_credentials_roundtrip_and_upsert(self):
        self.storage.upsert_credentials(UID, "a@b.c", "enc1")
        self.storage.upsert_credentials(UID, "a@b.c", "enc2")
        creds = self.storage.get_credentials(UID)
        self.assertEqual(creds.password_encrypted, "enc2")
        self.assertIsNone(self.storage.get_credentials(UID + 1))
        self.assertEqual(self.storage.get_all_credential_user_ids(), [UID])

    def test_web_token_one_shot(self):
        token = self.storage.issue_web_token(UID)
        self.assertEqual(self.storage.consume_web_token(token), UID)
        # второй раз тот же токен не срабатывает
        self.assertIsNone(self.storage.consume_web_token(token))

    def test_web_token_expired(self):
        token = self.storage.issue_web_token(UID, ttl_seconds=-1)
        self.assertIsNone(self.storage.consume_web_token(token))


class TestConversationHistory(StorageTestCase):
    def test_history_trimmed_to_keep_last(self):
        for i in range(70):
            self.storage.add_message(UID, "user", f"msg {i}", source="qa")
        with self.storage._connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM conversation_messages WHERE user_id=?", (UID,)
            ).fetchone()[0]
        self.assertEqual(n, 60)
        hist = self.storage.get_history(UID, limit=5)
        self.assertEqual(len(hist), 5)
        self.assertEqual(hist[-1]["content"], "msg 69")

    def test_history_source_filter_and_truncation(self):
        self.storage.add_message(UID, "assistant", "x" * 5000, source="morning")
        self.storage.add_message(UID, "user", "вопрос", source="qa")
        only_qa = self.storage.get_history(UID, sources=("qa",))
        self.assertEqual(len(only_qa), 1)
        both = self.storage.get_history(UID, sources=("morning", "qa"))
        self.assertEqual(len(both), 2)
        self.assertLess(len(both[0]["content"]), 1300)
        self.assertIn("сокращено", both[0]["content"])


class TestVerifiedFacts(StorageTestCase):
    def test_dedup_guard_same_fact_returns_existing_id(self):
        a = self.storage.add_verified_fact(UID, "2026-07-06", "бег 7 км")
        b = self.storage.add_verified_fact(UID, "2026-07-06", "бег 7 км")
        self.assertEqual(a, b)
        facts = self.storage.list_verified_facts(UID, since_date="2026-07-01")
        self.assertEqual(len(facts), 1)

    def test_retract_fact(self):
        fid = self.storage.add_verified_fact(UID, "2026-07-09", "бег 6 км")
        self.assertTrue(self.storage.deactivate_verified_fact(UID, fid))
        # повторный отзыв и чужой user_id — False
        self.assertFalse(self.storage.deactivate_verified_fact(UID, fid))
        self.assertFalse(self.storage.deactivate_verified_fact(UID + 1, fid))
        self.assertEqual(self.storage.list_verified_facts(UID, since_date="2026-07-01"), [])

    def test_different_date_or_text_is_new_fact(self):
        a = self.storage.add_verified_fact(UID, "2026-07-06", "бег 7 км")
        b = self.storage.add_verified_fact(UID, "2026-07-07", "бег 7 км")
        c = self.storage.add_verified_fact(UID, "2026-07-06", "бег 8 км")
        self.assertEqual(len({a, b, c}), 3)


class TestUserMemory(StorageTestCase):
    def test_dedup_substring_absorbed(self):
        first = self.storage.add_memory_item(UID, "GPS неточный — темп только вручную")
        dup = self.storage.add_memory_item(UID, "gps неточный — темп только вручную")
        self.assertIsNotNone(first)
        self.assertIsNone(dup)

    def test_superseding_note_deactivates_old(self):
        self.storage.add_memory_item(UID, "болит ахилл")
        new = self.storage.add_memory_item(UID, "болит ахилл — исключить прыжки и горки")
        self.assertIsNotNone(new)
        items = self.storage.list_memory_items(UID)
        self.assertEqual(len(items), 1)
        self.assertIn("исключить", items[0]["content"])


class TestPlans(StorageTestCase):
    def test_plan_upsert_per_week(self):
        self.storage.save_plan(UID, "2026-07-06", "план v1", "base")
        self.storage.save_plan(UID, "2026-07-06", "план v2", "build")
        self.storage.save_plan(UID, "2026-07-13", "план next", "base")
        self.assertEqual(self.storage.get_plan(UID, "2026-07-06")[0], "план v2")
        self.assertEqual(self.storage.get_plan(UID, "2026-07-13")[0], "план next")
        self.assertIsNone(self.storage.get_plan(UID, "2026-06-29"))
        meta = self.storage.get_plan_meta(UID, "2026-07-06")
        self.assertEqual(meta["week_type"], "build")

    def test_delete_plan(self):
        self.storage.save_plan(UID, "2026-07-20", "план", "recovery")
        self.assertTrue(self.storage.delete_plan(UID, "2026-07-20"))
        self.assertIsNone(self.storage.get_plan(UID, "2026-07-20"))
        self.assertFalse(self.storage.delete_plan(UID, "2026-07-20"))


class TestPlanPreferencesAndSafetyOverride(StorageTestCase):
    """Процесс 20.07.2026: пожелания атлета + снятие hard-safety на неделю."""

    def test_preferences_replace_and_clear(self):
        self.assertEqual(self.storage.get_plan_preferences(UID), "")
        self.storage.save_plan_preferences(UID, "интенсивные вт/чт, лонг вс")
        self.assertEqual(self.storage.get_plan_preferences(UID), "интенсивные вт/чт, лонг вс")
        self.storage.save_plan_preferences(UID, "объём 60 км")  # полная замена
        self.assertEqual(self.storage.get_plan_preferences(UID), "объём 60 км")
        self.storage.save_plan_preferences(UID, "  ")  # пустой текст = удаление
        self.assertEqual(self.storage.get_plan_preferences(UID), "")

    def test_preferences_per_user(self):
        self.storage.save_plan_preferences(UID, "моё")
        self.assertEqual(self.storage.get_plan_preferences(UID + 1), "")

    def test_safety_override_per_week(self):
        self.assertFalse(self.storage.has_safety_override(UID, "2026-07-20"))
        self.storage.save_safety_override(UID, "2026-07-20", reason="кнопка")
        self.assertTrue(self.storage.has_safety_override(UID, "2026-07-20"))
        # действует ТОЛЬКО на подтверждённую неделю
        self.assertFalse(self.storage.has_safety_override(UID, "2026-07-27"))
        self.assertFalse(self.storage.has_safety_override(UID + 1, "2026-07-20"))
        # повторное подтверждение не падает (idempotent)
        self.storage.save_safety_override(UID, "2026-07-20", reason="ещё раз")


class TestTokenUsage(StorageTestCase):
    def test_log_and_aggregate(self):
        self.storage.log_token_usage(UID, "ask_tools", "m1", 1000, 50, 800, 0)
        self.storage.log_token_usage(UID, "morning", "m1", 2000, 150)
        self.storage.log_token_usage(None, "nutrition", "m1", 500, 30)
        rows = self.storage.get_token_usage_stats(days=30)
        self.assertEqual(len(rows), 2)
        by_uid = {r["user_id"]: r for r in rows}
        self.assertEqual(by_uid[UID]["calls"], 2)
        self.assertEqual(by_uid[UID]["input_tokens"], 3000)
        self.assertEqual(by_uid[UID]["cache_read_tokens"], 800)
        self.assertEqual(by_uid[None]["output_tokens"], 30)


class TestUsageEvents(StorageTestCase):
    def test_last_event_at(self):
        self.assertIsNone(self.storage.last_event_at(UID))
        self.storage.track_event(UID, "morning")
        self.storage.track_event(UID, "question")
        last = self.storage.last_event_at(UID)
        self.assertIsNotNone(last)
        self.assertIn("T", last)  # ISO-формат
        self.assertIsNone(self.storage.last_event_at(UID + 1))


class TestRaces(StorageTestCase):
    def test_race_lifecycle(self):
        rid = self.storage.save_race(UID, "2026-09-27", "Марафон", 42.2, "3:29:00")
        races = self.storage.get_races(UID, from_date="2026-07-01")
        self.assertEqual(len(races), 1)
        self.assertTrue(self.storage.set_race_priority(UID, rid, True))
        self.assertTrue(self.storage.set_race_result(UID, rid, "3:31:12"))
        r = self.storage.get_races(UID)[0]
        self.assertEqual(r["is_priority"], 1)
        self.assertEqual(r["actual_time"], "3:31:12")
        # чужой user_id не может удалить гонку
        self.assertFalse(self.storage.delete_race(UID + 1, rid))
        self.assertTrue(self.storage.delete_race(UID, rid))


if __name__ == "__main__":
    unittest.main()


class TestHybridHistory(unittest.TestCase):
    """get_history(recent_full=N): свежие — полные, старшие — заголовки."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.st = Storage(Path(self._tmp.name) / "app.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_older_messages_capped(self):
        for i in range(6):
            self.st.add_message(1, "assistant", f"msg{i} " + "x" * 500, source="qa")
        h = self.st.get_history(1, limit=6, recent_full=2, older_cap=100)
        self.assertEqual(len(h), 6)
        for m in h[:4]:   # старшие 4 — обрезаны
            self.assertLess(len(m["content"]), 130)
            self.assertIn("[…сокращено]", m["content"])
        for m in h[4:]:   # свежие 2 — полные
            self.assertGreater(len(m["content"]), 400)

    def test_default_no_hybrid(self):
        self.st.add_message(1, "assistant", "y" * 500, source="qa")
        h = self.st.get_history(1, limit=5)
        self.assertGreater(len(h[0]["content"]), 400)
