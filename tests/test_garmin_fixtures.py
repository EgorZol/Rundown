"""Офлайн-тесты конвейера синка на garth-фикстурах (реальные ответы Garmin API).

FakeClient раздаёт записанные JSON по путям — реальные pydantic-модели garth
и ВЕСЬ код трансформации run_health_sync/run_activity_sync работают как в бою,
но без сети. Это страховка под распил garmin_service.py и пиннинг выстраданных
правил (NOT NULL-дефолты, steps_activities только для бега, конверсии единиц).

Фикстуры: tests/fixtures/garth/ (scripts/record_garth_fixtures.py, вычищены).
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from garmin_backup_bot.garmin_service import GarminService  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "garth"
UID = 100500
FROZEN_TODAY = date(2026, 7, 10)  # дата записи фикстур — тесты не протухают

ROUTES = [
    ("dailySleepData", "sleep.json"),
    ("socialProfile", "social_profile.json"),
    ("dailyStress", "daily_stress.json"),
    ("hrv-service", "hrv.json"),
    ("usersummary-service", "daily_summary.json"),
    ("weight-service", "weight.json"),
    ("personal-information", "personal_info.json"),
    ("activitylist-service", "activity_list.json"),
    ("hrTimeInZones", "activity_zones.json"),
    ("/laps", "activity_laps.json"),
    ("/splits", "activity_splits.json"),
    ("activity-service/activity/", "activity_detail.json"),
]


class FakeClient:
    """Раздаёт фикстуры по подстроке пути; неизвестный путь = ошибка теста."""

    username = "user"  # garth SleepData строит путь через client.username

    def __init__(self):
        self.requests: list[str] = []

    def connectapi(self, path, **kwargs):
        self.requests.append(path)
        if "activitylist-service" in path and (kwargs.get("params") or {}).get("start", 0) > 0:
            return []  # только одна страница активностей
        for needle, fname in ROUTES:
            if needle in path:
                return json.loads((FIX / fname).read_text())
        raise AssertionError(f"нет фикстуры для пути: {path}")

    def load(self, *_):  # pragma: no cover — токены не нужны
        pass


class FixtureSyncTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.svc = GarminService(workdir_root=root, exports_dir=root / "exports")
        self.client = FakeClient()
        self.svc._garth_login = lambda *a, **kw: self.client
        self.dbs = root / str(UID) / "DBs"

        class _FrozenDate(date):
            @classmethod
            def today(cls):
                return FROZEN_TODAY

        # синк-код живёт в garmin_sync (распил 10.07), date патчим там
        self._date_patch = patch("garmin_backup_bot.garmin_sync.date", _FrozenDate)
        self._date_patch.start()
        # первичный синк на 25 дней вместо 365 — быстро и покрывает даты фикстур
        self._env_patch = patch.dict("os.environ", {"GARMIN_START_DATE": "2026-06-15"})
        self._env_patch.start()

    def tearDown(self):
        self._date_patch.stop()
        self._env_patch.stop()
        self._tmp.cleanup()

    def _q(self, db: str, sql: str):
        with sqlite3.connect(self.dbs / db) as c:
            return c.execute(sql).fetchall()


class TestHealthSyncOnFixtures(FixtureSyncTestCase):
    def test_full_health_pipeline(self):
        self.svc.run_health_sync(UID, "user@example.com", "pw")

        # сон: секунды из DTO → строки "HH:MM:SS"
        rows = self._q("garmin.db", "SELECT day, total_sleep, deep_sleep FROM sleep")
        self.assertGreaterEqual(len(rows), 1)
        self.assertRegex(rows[0][1], r"^\d{1,2}:\d{2}:\d{2}$")

        # дневная сводка: RHR/BB/SpO2 замаплены из camelCase JSON
        ds = self._q("garmin.db",
                     "SELECT COUNT(*), MAX(rhr), MAX(bb_max), MAX(spo2_min) FROM daily_summary")[0]
        self.assertGreater(ds[0], 20)          # день-за-днём по диапазону
        self.assertIsNotNone(ds[1])
        self.assertIsNotNone(ds[2])

        # вес: фикстура записана в период без взвешиваний — пусто, но БЕЗ крэша
        self.assertEqual(self._q("garmin.db", "SELECT COUNT(*) FROM weight")[0][0], 0)

        # HRV уходит в JSON-файлы
        hrv_files = list((self.dbs.parent / "HRV").glob("hrv_*.json"))
        self.assertGreaterEqual(len(hrv_files), 1)
        payload = json.loads(hrv_files[0].read_text())
        self.assertIn("status", payload)

        # фитнес-профиль из personal-information (вес граммы → кг)
        prof = json.loads((self.dbs.parent / "fitness_profile.json").read_text()) \
            if (self.dbs.parent / "fitness_profile.json").exists() else None
        if prof:
            self.assertLess(prof.get("weight_kg", 0), 200)


class TestActivitySyncOnFixtures(FixtureSyncTestCase):
    def test_full_activity_pipeline(self):
        self.svc.run_activity_sync(UID, "user@example.com", "pw")

        acts = self._q("garmin_activities.db",
                       "SELECT activity_id, sport, distance, avg_hr, elapsed_time, "
                       "hrz_1_time, avg_speed FROM activities")
        self.assertGreaterEqual(len(acts), 1)
        a = acts[0]
        # NOT NULL-дефолты: время зон никогда не NULL (инцидент IntegrityError)
        self.assertIsNotNone(a[5])
        # м/с → км/ч
        if a[6]:
            self.assertLess(a[6], 30)

        laps = self._q("garmin_activities.db", "SELECT COUNT(*) FROM activity_laps")[0][0]
        self.assertGreater(laps, 0)

        # steps_activities — только бег/ходьба (инцидент IntegrityError на йоге)
        # правило: бег/ходьба ИЛИ активность с реальными шагами (инцидент: йога без шагов)
        run_walk = ("running", "walking", "trail_running", "track_running",
                    "treadmill_running", "indoor_running", "hiking")
        sports = dict(self._q("garmin_activities.db", "SELECT activity_id, sport FROM activities"))
        for sid, steps in self._q("garmin_activities.db",
                                  "SELECT activity_id, steps FROM steps_activities"):
            self.assertTrue(sports.get(sid) in run_walk or steps,
                            f"{sports.get(sid)} в steps_activities без шагов")

    def test_offline_guarantee(self):
        """Ни одного неизвестного пути — весь синк покрыт фикстурами."""
        self.svc.run_activity_sync(UID, "user@example.com", "pw")
        self.assertGreater(len(self.client.requests), 3)


class TestNullIntensityDay(FixtureSyncTestCase):
    """Регресс 10.07: день без часов (null-минуты) ронял ВЕСЬ health-синк
    на legacy-схемах с NOT NULL. Теперь — жёсткий дефолт и per-day изоляция."""

    def test_null_minutes_written_as_zero_time(self):
        base = json.loads((FIX / "daily_summary.json").read_text())
        base["moderateIntensityMinutes"] = None
        base["vigorousIntensityMinutes"] = None
        base["intensityMinutesGoal"] = None
        orig = self.client.connectapi

        def patched(path, **kw):
            if "usersummary-service" in path:
                return base
            return orig(path, **kw)

        self.client.connectapi = patched
        self.svc.run_health_sync(UID, "u@e.com", "pw")
        rows = self._q("garmin.db",
                       "SELECT moderate_activity_time, vigorous_activity_time, "
                       "intensity_time_goal FROM daily_summary LIMIT 1")
        self.assertEqual(rows[0], ("00:00:00.000000",) * 3)


if __name__ == "__main__":
    unittest.main()
