"""Тесты GarminService — определение первичности синка по доменам.

Регресс инцидента Саши (07.2026): активити-синк считал себя инкрементальным,
потому что health-синк минутой раньше заполнил sleep — история активностей
за год так и не скачалась.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from garmin_backup_bot.garmin_service import GarminService  # noqa: E402

UID = 100500


class SyncDomainTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.svc = GarminService(workdir_root=self.root, exports_dir=self.root / "exports")
        self.dbs = self.root / str(UID) / "DBs"
        self.dbs.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_health_db(self, rows: int):
        with sqlite3.connect(self.dbs / "garmin.db") as c:
            c.execute("CREATE TABLE sleep (day TEXT)")
            c.executemany("INSERT INTO sleep VALUES (?)", [(f"d{i}",) for i in range(rows)])

    def _make_activities_db(self, rows: int):
        with sqlite3.connect(self.dbs / "garmin_activities.db") as c:
            c.execute("CREATE TABLE activities (activity_id INTEGER)")
            c.executemany("INSERT INTO activities VALUES (?)", [(i,) for i in range(rows)])

    def test_fresh_user_both_domains_initial(self):
        self.assertTrue(self.svc._domain_empty(UID, "health"))
        self.assertTrue(self.svc._domain_empty(UID, "activities"))
        self.assertTrue(self.svc.is_initial_sync_pending(UID))

    def test_sasha_scenario_health_filled_activities_still_initial(self):
        # health-синк уже прошёл (sleep заполнен), активностей ещё нет —
        # раньше активити-синк тут ошибочно становился инкрементальным
        self._make_health_db(rows=10)
        self.assertFalse(self.svc._domain_empty(UID, "health"))
        self.assertTrue(self.svc._domain_empty(UID, "activities"))
        self.assertTrue(self.svc.is_initial_sync_pending(UID))
        # и диапазон синка активностей — первичный (год), а не 14 дней
        start, end = self.svc._get_sync_range(UID, domain="activities")
        self.assertGreater((end - start).days, 300)
        # а health — инкрементальный
        start_h, end_h = self.svc._get_sync_range(UID, domain="health")
        self.assertEqual((end_h - start_h).days, 14)

    def test_both_filled_incremental(self):
        self._make_health_db(rows=5)
        self._make_activities_db(rows=5)
        self.assertFalse(self.svc.is_initial_sync_pending(UID))
        for domain in ("health", "activities"):
            start, end = self.svc._get_sync_range(UID, domain=domain)
            self.assertEqual((end - start).days, 14)


if __name__ == "__main__":
    unittest.main()
