"""Тесты determine_week_type — окна объёма и делегирование hard-safety в coach.

Ревью 10.07.2026: объёмные правила переведены со скользящих 7-дневных окон
на завершённые календарные недели (те же, что видит юзер в «📋 Итоги»);
правила перегруза консолидированы в coach.overload_verdict.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from garmin_backup_bot import coach  # noqa: E402
from garmin_backup_bot.plan_builder import WeeklyPlanBuilder  # noqa: E402

TODAY = date(2026, 7, 8)  # среда


def _run(day: date, km: float, tl: float = 30.0) -> dict:
    return {"start_time": f"{day.isoformat()}T08:00:00.0", "sport": "running",
            "distance": km, "training_load": tl, "avg_hr": 140, "avg_speed": 10.0}


def _week_runs(monday: date, kms: list[float]) -> list[dict]:
    return [_run(monday + timedelta(days=i), km) for i, km in enumerate(kms) if km]


class TestOverloadVerdict(unittest.TestCase):
    def test_no_signals_none(self):
        self.assertIsNone(coach.overload_verdict({"daily_trend_7d": [
            {"rhr": 50, "bb_max": 80} for _ in range(7)]}))

    def test_rhr_spike_uses_median(self):
        trend = [{"rhr": 50, "bb_max": 80} for _ in range(6)] + [{"rhr": 90, "bb_max": 80}]
        v = coach.overload_verdict({
            "daily_trend_7d": trend,
            "resting_hr": {"resting_heart_rate": 58},  # медиана 50 → +8 > порога 5
        })
        self.assertIsNotNone(v)
        self.assertIn("ЧСС покоя", v.reason)
        self.assertEqual(v.volume_factor, coach.OVERLOAD_VOLUME_FACTOR)

    def test_low_bb_three_days(self):
        trend = [{"rhr": 50, "bb_max": 40} for _ in range(3)] + [{"rhr": 50, "bb_max": 80}] * 4
        v = coach.overload_verdict({"daily_trend_7d": trend})
        self.assertIsNotNone(v)
        self.assertIn("BB", v.reason)

    def test_feelings_two_low_days(self):
        v = coach.overload_verdict(
            {"daily_trend_7d": [{"rhr": 50, "bb_max": 80}] * 7},
            feelings=[{"score": 4}, {"score": 2}, {"score": 1}],
        )
        self.assertIsNotNone(v)
        self.assertIn("Самочувствие", v.reason)

    def test_vo2_three_drops(self):
        hist = [{"date": f"2026-06-{d:02d}", "vo2_max": v}
                for d, v in ((1, 50.0), (8, 49.5), (15, 49.0), (22, 48.5))]
        v = coach.overload_verdict({"vo2max_history": hist,
                                    "daily_trend_7d": [{"rhr": 50, "bb_max": 80}] * 7})
        self.assertIsNotNone(v)
        self.assertIn("VO2max", v.reason)


class TestWeekTypeVolumeWindows(unittest.TestCase):
    """Объёмные правила считают ЗАВЕРШЁННЫЕ календарные недели."""

    def setUp(self):
        # analyst/service не нужны: до LLM determine_week_type не доходит
        self.b = WeeklyPlanBuilder(analyst=None, service=None)
        self.metrics_base = {
            "date": TODAY.isoformat(),
            "daily_trend_7d": [{"rhr": 50, "bb_max": 80}] * 7,
            "fitness": {"tsb": 0, "ctl": 40, "atl": 40},
        }

    def test_stable_full_weeks_build(self):
        this_monday = TODAY - timedelta(days=TODAY.weekday())
        last_week = _week_runs(this_monday - timedelta(days=7), [8, 0, 8, 0, 8, 0, 6])   # 30 км
        prev_week = _week_runs(this_monday - timedelta(days=14), [8, 0, 8, 0, 7, 0, 6])  # 29 км
        metrics = dict(self.metrics_base, activities_28d=last_week + prev_week)
        wt, reason, vf, safety = self.b.determine_week_type(metrics, last_week, [], feelings=[], past_races=[])
        self.assertEqual(wt, "build")
        self.assertIn("прошлая неделя 30", reason)
        self.assertLessEqual(vf, 1.10)
        self.assertIsNone(safety)

    def test_monday_not_empty_week(self):
        # В понедельник «текущая» скользящая неделя была бы пустой —
        # календарные окна сравнивают всё те же завершённые недели
        monday = date(2026, 7, 6)
        last_week = _week_runs(monday - timedelta(days=7), [8, 0, 8, 0, 8, 0, 6])
        prev_week = _week_runs(monday - timedelta(days=14), [8, 0, 8, 0, 7, 0, 6])
        metrics = dict(self.metrics_base, date=monday.isoformat(),
                       activities_28d=last_week + prev_week)
        wt, reason, _, _ = self.b.determine_week_type(metrics, last_week, [], feelings=[], past_races=[])
        self.assertEqual(wt, "build")

    def test_volume_jump_base(self):
        this_monday = TODAY - timedelta(days=TODAY.weekday())
        last_week = _week_runs(this_monday - timedelta(days=7), [12, 0, 12, 0, 12, 0, 12])  # 48
        prev_week = _week_runs(this_monday - timedelta(days=14), [8, 0, 8, 0, 8, 0, 6])     # 30
        metrics = dict(self.metrics_base, activities_28d=last_week + prev_week)
        wt, reason, _, _ = self.b.determine_week_type(metrics, last_week, [], feelings=[], past_races=[])
        self.assertEqual(wt, "base")
        self.assertIn("скачок", reason.lower())

    def test_overload_beats_race(self):
        # hard-safety через coach бьёт даже гонку через 10 дней
        trend = [{"rhr": 50, "bb_max": 40}] * 3 + [{"rhr": 50, "bb_max": 80}] * 4
        metrics = dict(self.metrics_base, daily_trend_7d=trend)
        races = [{"date": (TODAY + timedelta(days=10)).isoformat(), "name": "Гонка",
                  "distance_km": 21.1, "is_priority": 1}]
        wt, reason, vf, safety = self.b.determine_week_type(metrics, [], races, feelings=[], past_races=[])
        self.assertEqual(wt, "recovery")
        self.assertIn("BB", reason)
        self.assertIn("BB", safety)


class TestSafetyOverride(unittest.TestCase):
    """Процесс 20.07.2026: атлет осознанно снимает hard-safety на неделю."""

    def setUp(self):
        self.b = WeeklyPlanBuilder(analyst=None, service=None)
        # Сигнал перегруза: BB < 50 три дня
        self.trend = [{"rhr": 50, "bb_max": 40}] * 3 + [{"rhr": 50, "bb_max": 80}] * 4
        self.metrics = {
            "date": TODAY.isoformat(),
            "daily_trend_7d": self.trend,
            "fitness": {"tsb": 0, "ctl": 40, "atl": 40},
        }
        self.races = [{"date": (TODAY + timedelta(days=40)).isoformat(), "name": "Гонка",
                       "distance_km": 21.1, "is_priority": 1}]

    def test_override_lifts_recovery_keeps_reason(self):
        wt, reason, vf, safety = self.b.determine_week_type(
            self.metrics, [], self.races, feelings=[], past_races=[],
            safety_override=True,
        )
        self.assertNotEqual(wt, "recovery")  # периодизация: build (гонка через 40 дн)
        self.assertEqual(wt, "build")
        self.assertIn("снят атлетом", reason)
        self.assertIn("BB", safety)  # причина сигнала сохраняется для контекста LLM

    def test_override_caps_volume_at_one(self):
        _, _, vf, _ = self.b.determine_week_type(
            self.metrics, [], self.races, feelings=[], past_races=[],
            safety_override=True,
        )
        self.assertLessEqual(vf, 1.0)  # снятый флаг ≠ разрешение наращивать объём

    def test_no_override_still_recovery(self):
        wt, _, _, safety = self.b.determine_week_type(
            self.metrics, [], self.races, feelings=[], past_races=[],
        )
        self.assertEqual(wt, "recovery")
        self.assertIsNotNone(safety)

    def test_override_without_signal_noop(self):
        clean = dict(self.metrics, daily_trend_7d=[{"rhr": 50, "bb_max": 80}] * 7)
        wt, reason, _, safety = self.b.determine_week_type(
            clean, [], self.races, feelings=[], past_races=[], safety_override=True,
        )
        self.assertIsNone(safety)
        self.assertNotIn("снят атлетом", reason)


if __name__ == "__main__":
    unittest.main()
