"""Unit-тесты для coach.py — детерминированная логика тренера.

Запуск:
  .venv/bin/python -m unittest tests.test_coach -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from garmin_backup_bot import coach  # noqa: E402


class TestZoneCalc(unittest.TestCase):
    def test_garmin_zone_secs_from_iso_strings(self):
        a = {
            "hrz_1_time": "00:01:00",
            "hrz_2_time": "00:02:00",
            "hrz_3_time": "00:30:00",
            "hrz_4_time": "00:05:00",
            "hrz_5_time": "00:00:00.000000",
        }
        z = coach.garmin_zone_secs(a)
        self.assertEqual(z, (60.0, 120.0, 1800.0, 300.0, 0.0))

    def test_garmin_zone_secs_none_when_all_missing(self):
        self.assertIsNone(coach.garmin_zone_secs({}))

    def test_z1_z3_percent_typical_easy_run(self):
        # 30 мин в Z3, 5 мин в Z4 → Z1-Z3 = 30/35 ≈ 86%
        pct = coach.z1_z3_percent((0, 0, 1800.0, 300.0, 0))
        self.assertEqual(pct, 86)

    def test_z1_z3_percent_none_when_empty(self):
        self.assertIsNone(coach.z1_z3_percent(None))
        self.assertIsNone(coach.z1_z3_percent((0, 0, 0, 0, 0)))

    def test_primary_zone(self):
        # больше всего в Z3
        self.assertEqual(coach.primary_zone((60, 60, 1800, 300, 0)), 3)
        # больше всего в Z4
        self.assertEqual(coach.primary_zone((10, 10, 200, 600, 50)), 4)
        # пусто
        self.assertIsNone(coach.primary_zone(None))
        self.assertIsNone(coach.primary_zone((0, 0, 0, 0, 0)))


class TestEightyTwentyBand(unittest.TestCase):
    def test_known_phases(self):
        self.assertEqual(coach.eighty_twenty_band("recovery"), (80, 100))
        self.assertEqual(coach.eighty_twenty_band("base"), (80, 100))
        self.assertEqual(coach.eighty_twenty_band("build"), (65, 80))
        self.assertEqual(coach.eighty_twenty_band("peak"), (50, 75))
        self.assertEqual(coach.eighty_twenty_band("taper"), (75, 90))

    def test_unknown_phase_returns_none(self):
        self.assertIsNone(coach.eighty_twenty_band(None))
        self.assertIsNone(coach.eighty_twenty_band(""))
        self.assertIsNone(coach.eighty_twenty_band("garbage"))

    def test_phase_case_insensitive(self):
        self.assertEqual(coach.eighty_twenty_band("BUILD"), (65, 80))

    def test_classify_z1_z3(self):
        # build: 65-80
        self.assertEqual(coach.classify_z1_z3(70, "build"), "in_band")
        self.assertEqual(coach.classify_z1_z3(60, "build"), "below")
        self.assertEqual(coach.classify_z1_z3(85, "build"), "above")
        # unknown phase
        self.assertEqual(coach.classify_z1_z3(70, None), "unknown_phase")
        # missing data
        self.assertEqual(coach.classify_z1_z3(None, "build"), "no_data")


class TestCadence(unittest.TestCase):
    def test_easy_pace_norm(self):
        # темп 6:30 → норма 155-165
        label, val = coach.cadence_verdict(6.5, 160)
        self.assertEqual(label, "norm")
        self.assertEqual(val, 160)

    def test_easy_pace_low(self):
        label, val = coach.cadence_verdict(6.5, 150)
        self.assertEqual(label, "low")

    def test_fast_pace_high(self):
        label, val = coach.cadence_verdict(4.5, 188)
        self.assertEqual(label, "high")

    def test_no_data(self):
        label, val = coach.cadence_verdict(None, 170)
        self.assertEqual(label, "no_data")


class TestGpsAnomalies(unittest.TestCase):
    def test_returns_empty_for_few_splits(self):
        splits = [{"km": 1, "pace": "5:00", "avg_hr": 150}] * 4
        self.assertEqual(coach.detect_gps_anomalies(splits), [])

    def test_detects_outlier_with_low_hr(self):
        # все сплиты 5:00 HR 150, один 3:30 HR 145 — должен флагнуться
        splits = [{"km": i+1, "pace": "5:00", "avg_hr": 150} for i in range(6)]
        splits[3] = {"km": 4, "pace": "3:30", "avg_hr": 145}
        anomalies = coach.detect_gps_anomalies(splits)
        self.assertEqual(len(anomalies), 1)
        self.assertIn("km4", anomalies[0].reason)
        self.assertIn("3:30", anomalies[0].reason)

    def test_no_false_positive_if_hr_matches(self):
        # темп быстрее, но HR пропорционально выше — это реальный sprint, не GPS
        splits = [{"km": i+1, "pace": "5:00", "avg_hr": 150} for i in range(6)]
        splits[3] = {"km": 4, "pace": "3:30", "avg_hr": 175}
        anomalies = coach.detect_gps_anomalies(splits)
        self.assertEqual(len(anomalies), 0)

    def test_no_data(self):
        self.assertEqual(coach.detect_gps_anomalies(None), [])
        self.assertEqual(coach.detect_gps_anomalies([]), [])


class TestWeekFacts(unittest.TestCase):
    def _make_run(self, day: str, dist: float, z3_min: int = 30, z4_min: int = 0, tl: float = 50, km_splits=None):
        a = {
            "sport": "running",
            "start_time": f"{day}T08:00:00",
            "distance": dist,
            "training_load": tl,
            "hrz_1_time": "00:00:00",
            "hrz_2_time": "00:02:00",
            "hrz_3_time": f"00:{z3_min:02d}:00",
            "hrz_4_time": f"00:{z4_min:02d}:00",
            "hrz_5_time": "00:00:00",
        }
        if km_splits:
            a["km_splits"] = km_splits
        return a

    def test_basic_aggregation(self):
        activities = [
            self._make_run("2026-06-15", 10.0, z3_min=40, z4_min=5),
            self._make_run("2026-06-16", 8.0, z3_min=30, z4_min=10),
            self._make_run("2026-06-17", 12.0, z3_min=45, z4_min=0),
        ]
        wf = coach.compute_week_facts(
            activities=activities,
            week_start=date(2026, 6, 15),
            week_end=date(2026, 6, 21),
            plan_meta={"plan_text": "план...", "week_type": "build"},
            profile={"weekly_km_target": 50},
        )
        self.assertEqual(wf.sessions_running, 3)
        self.assertAlmostEqual(wf.km_running, 30.0)
        self.assertEqual(wf.phase, "build")
        self.assertEqual(wf.z1_z3_band, (65, 80))
        # z3 secs total = 40+30+45 = 115 мин; z4 = 15 мин; z2 = 6 мин → total = 136 → z123 ≈ 89%
        self.assertEqual(wf.z1_z3_verdict, "above")
        self.assertEqual(wf.norm_km, 50.0)
        self.assertEqual(wf.plan_adherence, "matched")  # 3 different days

    def test_walking_does_not_inflate_running_km(self):
        """Главный регресс-кейс из жалобы Алины: 66 vs 56."""
        activities = [
            {"sport": "running", "start_time": "2026-06-15T08", "distance": 7.05},
            {"sport": "walking", "start_time": "2026-06-15T18", "distance": 7.28},
            {"sport": "running", "start_time": "2026-06-16T08", "distance": 11.91},
            {"sport": "walking", "start_time": "2026-06-18T08", "distance": 0.58},
            {"sport": "running", "start_time": "2026-06-19T08", "distance": 10.51},
            {"sport": "running", "start_time": "2026-06-21T08", "distance": 16.56},
        ]
        wf = coach.compute_week_facts(
            activities=activities,
            week_start=date(2026, 6, 15),
            week_end=date(2026, 6, 21),
            plan_meta=None,
            profile=None,
        )
        # бег только: 7.05 + 11.91 + 10.51 + 16.56 = 46.03 (плюс 17.06 пробежки в тесте нет)
        self.assertAlmostEqual(wf.km_running, 46.03)
        self.assertEqual(wf.sessions_running, 4)
        self.assertEqual(wf.sessions_total, 6)

    def test_no_plan_no_norm(self):
        activities = [self._make_run("2026-06-15", 10.0)]
        wf = coach.compute_week_facts(
            activities=activities,
            week_start=date(2026, 6, 15),
            week_end=date(2026, 6, 21),
            plan_meta=None,
            profile=None,
        )
        self.assertIsNone(wf.phase)
        self.assertIsNone(wf.z1_z3_band)
        self.assertEqual(wf.z1_z3_verdict, "unknown_phase")
        self.assertIsNone(wf.norm_km)
        self.assertEqual(wf.plan_adherence, "no_plan")

    def test_norm_km_only_from_profile(self):
        """Главное правило: норма берётся ТОЛЬКО из weekly_km_target. Без него — None."""
        wf = coach.compute_week_facts(
            activities=[],
            week_start=date(2026, 6, 15),
            week_end=date(2026, 6, 21),
            plan_meta=None,
            profile={"lthr": 175, "weight_kg": 85},  # нет weekly_km_target
        )
        self.assertIsNone(wf.norm_km)


class TestRecoveryStatus(unittest.TestCase):
    def test_good_when_all_clean(self):
        rs = coach.compute_recovery_status({
            "sleep_last_night": {
                "deep_sleep_secs": 4500,  # 1.25h
                "rem_sleep_secs": 6000,   # 1.67h
                "total_sleep_secs": 28800,  # 8h
            },
            "resting_hr": {"last": 48, "avg_7d": 50},
            "body_battery": {"min": 70, "max": 95},
            "hrv": {"status": "BALANCED"},
            "fitness": {"tsb": 3, "acwr": 1.0},
        })
        self.assertEqual(rs.label, "good")
        self.assertTrue(rs.safe_to_train_hard)
        self.assertEqual(rs.drivers, [])

    def test_caution_when_deep_sleep_low(self):
        rs = coach.compute_recovery_status({
            "sleep_last_night": {"deep_sleep_secs": 2400, "rem_sleep_secs": 6000, "total_sleep_secs": 28800},
            "resting_hr": {"last": 48, "avg_7d": 50},
        })
        self.assertEqual(rs.label, "caution")
        self.assertTrue(rs.safe_to_train_hard)
        self.assertTrue(any("deep" in d for d in rs.drivers))

    def test_poor_when_rhr_spike(self):
        rs = coach.compute_recovery_status({
            "sleep_last_night": {"deep_sleep_secs": 4500, "rem_sleep_secs": 6000, "total_sleep_secs": 28800},
            "resting_hr": {"last": 56, "avg_7d": 50},  # +6 от базы
        })
        self.assertEqual(rs.label, "poor")
        self.assertFalse(rs.safe_to_train_hard)
        self.assertTrue(any("RHR" in d for d in rs.drivers))

    def test_alarm_when_rr_spike(self):
        rs = coach.compute_recovery_status({
            "sleep_last_night": {
                "deep_sleep_secs": 4500, "rem_sleep_secs": 6000, "total_sleep_secs": 28800,
                "avg_rr": 17.0,
            },
            "avg_rr_baseline_7d": 14.0,  # +3.0
            "resting_hr": {"last": 48, "avg_7d": 50},
        })
        self.assertEqual(rs.label, "alarm")
        self.assertFalse(rs.safe_to_train_hard)

    def test_tsb_overload(self):
        rs = coach.compute_recovery_status({
            "fitness": {"tsb": -30, "acwr": 1.0},
        })
        self.assertEqual(rs.label, "poor")
        self.assertFalse(rs.safe_to_train_hard)

    def test_acwr_overload(self):
        rs = coach.compute_recovery_status({
            "fitness": {"tsb": 0, "acwr": 1.7},
        })
        self.assertEqual(rs.label, "poor")
        self.assertFalse(rs.safe_to_train_hard)


class TestWorkoutFacts(unittest.TestCase):
    def test_easy_run_facts(self):
        a = {
            "activity_id": 1234,
            "sport": "running",
            "start_time": "2026-06-19T08:00:00",
            "distance": 10.5,
            "avg_speed": 10.0,  # 6:00/km
            "avg_hr": 150,
            "max_hr": 175,
            "avg_cadence": 80,  # 160 step/min
            "hrz_1_time": "00:00:00",
            "hrz_2_time": "00:05:00",
            "hrz_3_time": "00:50:00",
            "hrz_4_time": "00:08:00",
            "hrz_5_time": "00:00:00",
            "training_effect": 3.2,
            "anaerobic_training_effect": 0.5,
        }
        wf = coach.compute_workout_facts(a)
        self.assertEqual(wf.sport, "running")
        self.assertEqual(wf.pace_str, "6:00")
        self.assertEqual(wf.primary_zone, 3)
        # 5+50+8 = 63 минуты Z2-Z4; Z2+Z3 = 55; (55+0)/63 ≈ 87%
        self.assertGreaterEqual(wf.z1_z3_pct or 0, 80)
        self.assertEqual(wf.cadence_value, 160)
        self.assertEqual(wf.cadence_verdict, "norm")
        self.assertEqual(wf.intensity_class, "easy")

    def test_long_run(self):
        a = {
            "activity_id": 9, "sport": "running",
            "start_time": "2026-06-21T08", "distance": 16.56,
            "avg_speed": 9.5,
            "hrz_3_time": "01:30:00", "hrz_4_time": "00:10:00",
        }
        wf = coach.compute_workout_facts(a)
        self.assertEqual(wf.intensity_class, "long")

    def test_race_via_name(self):
        a = {
            "activity_id": 1, "sport": "running",
            "start_time": "2026-06-20", "distance": 10.0,
            "name": "Ночной забег 10К",
            "avg_speed": 12.0,
        }
        wf = coach.compute_workout_facts(a)
        self.assertEqual(wf.intensity_class, "race")


class TestMorningFacts(unittest.TestCase):
    def test_morning_basic(self):
        metrics = {
            "date": "2026-06-22",
            "sleep_last_night": {
                "deep_sleep_secs": 4500, "rem_sleep_secs": 6000, "total_sleep_secs": 28800,
            },
            "resting_hr": {"last": 50, "avg_7d": 50},
            "body_battery": {"min": 80, "max": 95},
            "hrv": {"status": "BALANCED"},
            "fitness": {"tsb": 2, "acwr": 1.0},
            "activities_28d": [
                {"sport": "running", "start_time": "2026-06-21T08", "distance": 16.56, "name": "лонг"},
            ],
        }
        mf = coach.compute_morning_facts(metrics, today=date(2026, 6, 22))
        self.assertEqual(mf.recovery.label, "good")
        self.assertAlmostEqual(mf.deep_sleep_h, 1.25, places=2)
        self.assertEqual(mf.rhr, 50)
        self.assertEqual(mf.rhr_delta, 0)
        self.assertIn("16.6 км", mf.yesterday_brief)

    def test_morning_no_data(self):
        # P0.5: пустой metrics НЕ должен давать ложно-успокаивающий "good"
        mf = coach.compute_morning_facts({}, today=date(2026, 6, 22))
        self.assertEqual(mf.recovery.label, "no_data")
        self.assertFalse(mf.recovery.safe_to_train_hard)
        self.assertTrue(any("синхронизация" in d for d in mf.recovery.drivers))
        self.assertIsNone(mf.sleep_total_h)


class TestPlanDates(unittest.TestCase):
    """Регресс инцидента 05-06.07.2026: сдвиг дат плана на +1 день."""

    TODAY = date(2026, 7, 5)  # воскресенье, Алина сохраняет план на следующую неделю

    def test_correct_plan_passes(self):
        plan = "Пн 06.07 — бег 7 км\nВт 07.07 — отдых\nВс 12.07 — лонг 21 км"
        c = coach.check_plan_dates(plan, self.TODAY)
        self.assertTrue(c.ok)
        self.assertEqual(c.week_start, date(2026, 7, 6))
        self.assertEqual(c.pairs_found, 3)

    def test_alina_shift_detected(self):
        # реальный баг: все даты +1 (Пн 07.07 вместо Пн 06.07)
        plan = "Пн 07.07 — бег\nВт 08.07 — бег\nВс 13.07 — лонг"
        c = coach.check_plan_dates(plan, self.TODAY)
        self.assertFalse(c.ok)
        self.assertEqual(len(c.errors), 3)
        self.assertIn("07.07 это Вт", c.errors[0])
        # hint предлагает правильный маппинг недели, куда попадает большинство дат
        self.assertIn("Пн 06.07", c.hint)
        self.assertIn("Вс 12.07", c.hint)

    def test_no_dates_ok_without_week(self):
        c = coach.check_plan_dates("Пн — бег, Вт — отдых", self.TODAY)
        self.assertTrue(c.ok)
        self.assertIsNone(c.week_start)
        self.assertEqual(c.pairs_found, 0)

    def test_mixed_weeks_rejected(self):
        plan = "Пн 06.07 — бег\nВт 14.07 — бег"
        c = coach.check_plan_dates(plan, self.TODAY)
        self.assertFalse(c.ok)

    def test_year_inference_across_new_year(self):
        c = coach.check_plan_dates("Пн 28.12 — бег\nВс 03.01 — лонг", date(2026, 12, 27))
        self.assertTrue(c.ok)
        self.assertEqual(c.week_start, date(2026, 12, 28))

    def test_fraction_not_parsed_as_date(self):
        # «5.6 ккал» / «Пт 5/6 порции» не должны ловиться как даты
        c = coach.check_plan_dates("Пн 06.07 — гель 5.6 ккал", self.TODAY)
        self.assertTrue(c.ok)
        self.assertEqual(c.pairs_found, 1)

    def test_fix_plan_dates_rewrites(self):
        plan = "Пн 07.07 — бег\nВт 08.07 — отдых\nВс 13.07 — лонг"
        fixed, n = coach.fix_plan_dates(plan, date(2026, 7, 6))
        self.assertEqual(n, 3)
        self.assertIn("Пн 06.07", fixed)
        self.assertIn("Вс 12.07", fixed)

    def test_fix_plan_dates_noop_when_correct(self):
        plan = "Пн 06.07 — бег\nВс 12.07 — лонг"
        fixed, n = coach.fix_plan_dates(plan, date(2026, 7, 6))
        self.assertEqual(n, 0)
        self.assertEqual(fixed, plan)


class TestPrimaryActivity(unittest.TestCase):
    """Регресс жалобы Алины 09.07.2026: разбор доставался 16-мин заминке."""

    ALINA_DAY = [  # newest first, как отдаёт collect_recent_activities
        {"start_time": "2026-07-09T09:59:32.0", "name": "Корр и заминка", "sport": "indoor_cardio",
         "distance": 0.0, "elapsed_time": "00:16:03"},
        {"start_time": "2026-07-09T09:02:40.0", "name": "функц. силовая", "sport": "indoor_cardio",
         "distance": 0.0, "elapsed_time": "00:56:03"},
        {"start_time": "2026-07-09T08:02:37.0", "name": "Бег до тренировок", "sport": "running",
         "distance": 6.0, "elapsed_time": "00:36:46"},
        {"start_time": "2026-07-08T15:28:09.0", "name": "Ходьба", "sport": "walking",
         "distance": 4.2, "elapsed_time": "00:49:11"},
    ]

    def test_run_becomes_primary(self):
        out = coach.reorder_primary_activity(list(self.ALINA_DAY))
        self.assertEqual(out[0]["name"], "Бег до тренировок")
        self.assertEqual(len(out), 4)
        # прошлые дни не рассматриваются как кандидаты
        self.assertEqual(out[-1]["name"], "Ходьба")

    def test_no_runs_longest_wins(self):
        day = [a for a in self.ALINA_DAY if a["sport"] != "running"][:2]
        out = coach.reorder_primary_activity(list(day))
        self.assertEqual(out[0]["name"], "функц. силовая")

    def test_single_activity_unchanged(self):
        one = [self.ALINA_DAY[2]]
        self.assertEqual(coach.reorder_primary_activity(list(one)), one)

    def test_latest_already_primary_unchanged(self):
        acts = [self.ALINA_DAY[2], self.ALINA_DAY[3]]  # бег и вчерашняя ходьба
        self.assertEqual(coach.reorder_primary_activity(list(acts))[0]["name"], "Бег до тренировок")

    def test_empty(self):
        self.assertEqual(coach.reorder_primary_activity([]), [])


class TestPlanLineForDate(unittest.TestCase):
    """Регресс 10.07.2026: «пробежек нет по плану» при плановом беге 8 км."""

    PLAN = """📅 ПЛАН НА НЕДЕЛЮ — Развивающая: аэробная база

Пн 06.07: уже выполнено — бег 8.1 км ✅
Чт 09.07: уже выполнено — бег 6 км + 56 мин функциональная ✅
Пт 10.07: лёгкий бег 8 км Z3 (132–150 уд/мин) @6:00–6:10/км. Вечер: HIIT + сайкл
Сб 11.07: отдых или лёгкая растяжка
Вс 12.07: длинный бег 22 км @5:50–6:00/км Z3"""

    def test_friday_run_found(self):
        line = coach.plan_line_for_date(self.PLAN, date(2026, 7, 10))
        self.assertIn("лёгкий бег 8 км", line)
        self.assertTrue(line.startswith("Пт 10.07"))

    def test_saturday_rest_found(self):
        self.assertIn("отдых", coach.plan_line_for_date(self.PLAN, date(2026, 7, 11)))

    def test_completed_day_line_found(self):
        self.assertIn("уже выполнено", coach.plan_line_for_date(self.PLAN, date(2026, 7, 9)))

    def test_date_not_in_plan(self):
        self.assertIsNone(coach.plan_line_for_date(self.PLAN, date(2026, 7, 13)))

    def test_empty_plan(self):
        self.assertIsNone(coach.plan_line_for_date(None, date(2026, 7, 10)))
        self.assertIsNone(coach.plan_line_for_date("", date(2026, 7, 10)))


if __name__ == "__main__":
    unittest.main()


class TestDataGaps(unittest.TestCase):
    """coach.data_gaps + pick_nudge: чек-лист пробелов и троттлинг подсказок."""

    FULL_PROFILE = {"available_days": "[0,2,5]", "location_name": "Москва, Россия"}

    def test_no_gaps_when_everything_set(self):
        gaps = coach.data_gaps(goal="полумарафон из 1:45", has_future_races=True,
                               profile=self.FULL_PROFILE, lthr=172.0, weight_kg=72.5)
        self.assertEqual(gaps, [])

    def test_goal_is_top_priority(self):
        gaps = coach.data_gaps(goal=None, has_future_races=False, profile={},
                               lthr=None, weight_kg=None)
        self.assertEqual(gaps[0].key, "goal")
        self.assertEqual([g.key for g in gaps],
                         ["goal", "available_days", "location", "lthr", "weight"])

    def test_race_gap_only_when_goal_set(self):
        gaps = coach.data_gaps(goal="марафон", has_future_races=False,
                               profile=self.FULL_PROFILE, lthr=172.0, weight_kg=72.5)
        self.assertEqual([g.key for g in gaps], ["race"])

    def test_pick_first_never_shown(self):
        gaps = coach.data_gaps(goal=None, has_future_races=False, profile={},
                               lthr=None, weight_kg=None)
        picked = coach.pick_nudge(gaps, {}, date(2026, 7, 10))
        self.assertEqual(picked.key, "goal")

    def test_shown_today_throttled_next_gap_offered(self):
        gaps = coach.data_gaps(goal=None, has_future_races=False, profile={},
                               lthr=None, weight_kg=None)
        history = {"goal": (1, "2026-07-10T05:00:00+00:00")}
        picked = coach.pick_nudge(gaps, history, date(2026, 7, 10))
        self.assertEqual(picked.key, "available_days")

    def test_repeat_after_week(self):
        gaps = [coach.DataGap("goal", "…")]
        history = {"goal": (1, "2026-07-01T05:00:00+00:00")}
        self.assertIsNotNone(coach.pick_nudge(gaps, history, date(2026, 7, 10)))

    def test_snooze_month_after_two_shows(self):
        gaps = [coach.DataGap("goal", "…")]
        history = {"goal": (2, "2026-07-01T05:00:00+00:00")}
        self.assertIsNone(coach.pick_nudge(gaps, history, date(2026, 7, 10)))
        self.assertIsNotNone(coach.pick_nudge(gaps, history, date(2026, 8, 5)))

    def test_all_throttled_returns_none(self):
        gaps = coach.data_gaps(goal="цель", has_future_races=True,
                               profile=self.FULL_PROFILE, lthr=None, weight_kg=None)
        history = {"lthr": (1, "2026-07-09T05:00:00+00:00"),
                   "weight": (1, "2026-07-09T05:00:00+00:00")}
        self.assertIsNone(coach.pick_nudge(gaps, history, date(2026, 7, 10)))

    def test_newbie_repeat_days(self):
        gaps = [coach.DataGap("goal", "…")]
        history = {"goal": (1, "2026-07-08T05:00:00+00:00")}  # показано 2 дня назад
        # обычный ритм (7 дн) — рано; ритм новичка (2 дн) — уже можно
        self.assertIsNone(coach.pick_nudge(gaps, history, date(2026, 7, 10)))
        self.assertIsNotNone(coach.pick_nudge(
            gaps, history, date(2026, 7, 10),
            repeat_days=coach.NUDGE_REPEAT_DAYS_NEWBIE))


class TestSubscriptionAccess(unittest.TestCase):
    TODAY = date(2026, 7, 10)

    def test_no_subscription(self):
        self.assertEqual(coach.access_level(None, self.TODAY), "none")

    def test_free_forever_full_access_without_date(self):
        sub = {"plan": "free_forever", "paid_until": None}
        self.assertEqual(coach.access_level(sub, self.TODAY), "coach")
        self.assertTrue(coach.has_access(sub, self.TODAY, "coach"))

    def test_trial_active_and_expired(self):
        active = {"plan": "trial", "paid_until": "2026-07-12"}
        expired = {"plan": "trial", "paid_until": "2026-07-09"}
        self.assertEqual(coach.access_level(active, self.TODAY), "coach")
        self.assertEqual(coach.access_level(expired, self.TODAY), "none")

    def test_paid_until_inclusive(self):
        sub = {"plan": "coach", "paid_until": "2026-07-10"}
        self.assertEqual(coach.access_level(sub, self.TODAY), "coach")

    def test_calories_plan_scoping(self):
        sub = {"plan": "calories", "paid_until": "2026-08-01"}
        self.assertEqual(coach.access_level(sub, self.TODAY), "calories")
        self.assertFalse(coach.has_access(sub, self.TODAY, "coach"))
        self.assertTrue(coach.has_access(sub, self.TODAY, "any"))

    def test_garbage_paid_until(self):
        sub = {"plan": "coach", "paid_until": "когда-нибудь"}
        self.assertEqual(coach.access_level(sub, self.TODAY), "none")


class TestPlanWeekStart(unittest.TestCase):
    def test_weekdays_current_week(self):
        # пн 06.07 … сб 11.07 → понедельник текущей недели
        for d in range(6, 12):
            self.assertEqual(coach.plan_week_start(date(2026, 7, d)), date(2026, 7, 6), d)

    def test_sunday_next_week(self):
        # вс 12.07 → план на следующую неделю (13.07)
        self.assertEqual(coach.plan_week_start(date(2026, 7, 12)), date(2026, 7, 13))


class TestDayActivitiesMarker(unittest.TestCase):
    """Метка [АКТИВНОСТИ ДНЯ]: итог дня считает код (инцидент Алины 12.07)."""

    W1 = {"sport": "walking", "start_time": "2026-07-12 10:00", "distance": 6.4,
          "elapsed_time": "01:41:00"}
    W2 = {"sport": "walking", "start_time": "2026-07-12 15:00", "distance": 9.5,
          "elapsed_time": "01:49:00"}
    OLD = {"sport": "running", "start_time": "2026-07-10 08:00", "distance": 8.0,
           "elapsed_time": "00:48:00"}

    def test_two_walks_same_day(self):
        # порядок как после reorder: основная (длинная) первой
        m = coach.day_activities_marker([self.W2, self.W1, self.OLD])
        self.assertIn("[АКТИВНОСТИ ДНЯ 12.07]", m)
        self.assertIn("ходьба 6.4 км", m)
        self.assertIn("ходьба 9.5 км", m)
        self.assertIn("15.9 км", m)          # итог дня
        self.assertNotIn("бег 8.0", m)       # другой день не попал

    def test_single_activity_no_marker(self):
        self.assertIsNone(coach.day_activities_marker([self.W2, self.OLD]))

    def test_empty(self):
        self.assertIsNone(coach.day_activities_marker([]))


class TestHrvNightAware(unittest.TestCase):
    """UNBALANCED (недельный тренд) не роняет вердикт дня, если ночь в базе (15.07)."""

    BASE = {"sleep_last_night": {"total_sleep_secs": 7*3600}, "resting_hr": {},
            "body_battery": {"min": 80, "max": 100}}

    def test_unbalanced_night_in_base_keeps_verdict(self):
        m = dict(self.BASE, hrv={"status": "UNBALANCED", "last_night_avg": 67,
                                 "baseline_balanced_low": 58, "baseline_balanced_upper": 81})
        r = coach.compute_recovery_status(m)
        self.assertNotEqual(r.label, "poor")
        self.assertTrue(r.safe_to_train_hard)
        self.assertTrue(any("ночь 67 в базе" in d for d in r.drivers))

    def test_unbalanced_night_below_base_is_poor(self):
        m = dict(self.BASE, hrv={"status": "UNBALANCED", "last_night_avg": 51,
                                 "baseline_balanced_low": 58, "baseline_balanced_upper": 81})
        r = coach.compute_recovery_status(m)
        self.assertEqual(r.label, "poor")
        self.assertFalse(r.safe_to_train_hard)


class TestSessions8020(unittest.TestCase):
    """80/20 по сессиям считает код. Два реальных инцидента:
    15.07 «3 из 3 интенсивные» (владелец) и 16.07 «2 из 3» (Алина) —
    лёгкие беги с заходами в Z4 на 24-50% времени при низком TE."""

    TODAY = date(2026, 7, 16)

    def _run(self, day, te=3.5, ate=0.5, **zones):
        a = {"sport": "running", "start_time": f"2026-07-{day} 07:00",
             "training_effect": te, "anaerobic_training_effect": ate}
        a.update(zones)
        return a

    def test_easy_run_with_z4_drift_is_easy(self):
        # кейс владельца 14-15.07: 24-30% в Z4, TE 3.7/1.3 — лёгкая
        a = self._run("14", te=3.7, ate=1.3, hrz_1_time="00:00:42",
                      hrz_2_time="00:05:27", hrz_3_time="00:36:23", hrz_4_time="00:13:35")
        self.assertFalse(coach.run_is_intensive(a))

    def test_alina_half_z4_low_te_is_easy(self):
        # кейс Алины 14.07: ровно 50% в Z4, но TE 3.4/0.7 — лёгкая (не >50%)
        a = self._run("14", te=3.4, ate=0.7,
                      hrz_3_time="00:30:00", hrz_4_time="00:30:00")
        self.assertFalse(coach.run_is_intensive(a))

    def test_dominant_z4_is_intensive(self):
        # кейс владельца 12.07: 83% в Z4 — интенсивная независимо от TE
        a = self._run("12", te=3.0, ate=1.4, hrz_1_time="00:00:15",
                      hrz_2_time="00:01:09", hrz_3_time="00:14:04", hrz_4_time="01:14:56")
        self.assertTrue(coach.run_is_intensive(a))

    def test_high_aerobic_te_is_intensive(self):
        # темповая: доля Z4 мала (обрезки), но Garmin оценил TE 4.2
        a = self._run("13", te=4.2, ate=1.0, hrz_3_time="00:40:00", hrz_4_time="00:15:00")
        self.assertTrue(coach.run_is_intensive(a))

    def test_intervals_by_anaerobic_te(self):
        # интервалы: анаэробный TE 2.5 при любой картине зон
        self.assertTrue(coach.run_is_intensive(self._run("13", te=3.5, ate=2.5)))

    def test_owner_real_week(self):
        acts = [
            self._run("12", te=5.0, ate=1.4, hrz_3_time="00:14:04", hrz_4_time="01:14:56"),
            self._run("14", te=3.7, ate=1.3, hrz_3_time="00:36:23", hrz_4_time="00:13:35"),
            self._run("15", te=3.7, ate=0.2, hrz_3_time="00:25:58", hrz_4_time="00:13:46"),
            {"sport": "lap_swimming", "start_time": "2026-07-11 07:00"},
        ]
        line = coach.sessions_7d_line(acts, self.TODAY)
        self.assertIn("3 пробежек — лёгких 2, интенсивных 1", line)
        self.assertIn("ok", line)

    def test_two_of_three_violates(self):
        acts = [self._run("12", ate=2.5), self._run("14", te=4.5), self._run("15", te=3.0, ate=0.3)]
        self.assertIn("нарушен", coach.sessions_7d_line(acts, self.TODAY))

    def test_no_runs_empty(self):
        self.assertEqual(coach.sessions_7d_line([], self.TODAY), "")


class TestRecoveryRealShapes(unittest.TestCase):
    """Ревью 16.07: пороги recovery читали ключи, которых пайплайн не производит
    (total_sleep_secs, resting_hr.last, body_battery.min, fitness.acwr) — и молча
    не срабатывали НИКОГДА. Тесты на РЕАЛЬНЫХ формах collect_daily_metrics."""

    def _real_metrics(self, **over):
        m = {
            "sleep": {"day": "2026-07-16", "total_sleep": "5:58:00",
                      "deep_sleep": "0:50:00", "rem_sleep": "0:46:00", "avg_rr": 14.0},
            "resting_hr": {"day": "2026-07-16", "resting_heart_rate": 52.0},
            "rhr_trend_7d": [{"day": f"2026-07-{d:02d}", "resting_heart_rate": 44.0}
                             for d in range(9, 16)],
            "daily_summary": {"day": "2026-07-16", "bb_min": 22, "bb_max": 88},
            "sleep_trend_7d": [{"day": f"2026-07-{d:02d}", "avg_rr": 14.2}
                               for d in range(9, 16)],
            "hrv": {"status": "BALANCED", "last_night_avg": 65,
                    "baseline_balanced_low": 58, "baseline_balanced_upper": 81},
            "fitness": {"atl": 60.0, "ctl": 35.0, "tsb": -10.0},
        }
        m.update(over)
        return m

    def test_all_thresholds_fire_on_real_shapes(self):
        r = coach.compute_recovery_status(self._real_metrics())
        joined = " | ".join(r.drivers)
        self.assertIn("общий сон 6.0ч", joined)            # строка "5:58:00" распарсена
        self.assertIn("deep sleep 0.8ч", joined)
        self.assertIn("RHR 52 (+8 над базой 44)", joined)  # resting_heart_rate + тренд
        self.assertIn("Body Battery min 22", joined)       # из daily_summary
        self.assertIn("ACWR 1.71", joined)                 # выведен из atl/ctl
        self.assertEqual(r.label, "poor")
        self.assertFalse(r.safe_to_train_hard)

    def test_alarm_never_downgraded(self):
        # рост ЧД (alarm) + перегруз TSB (poor) → остаётся alarm
        m = self._real_metrics(fitness={"atl": 30, "ctl": 40, "tsb": -30.0})
        m["sleep"]["avg_rr"] = 17.0  # +2.8 к базе 14.2
        r = coach.compute_recovery_status(m)
        self.assertEqual(r.label, "alarm")

    def test_hrv_low_night_out_of_base_is_poor(self):
        m = self._real_metrics()
        m["sleep"] = {"day": "2026-07-16", "total_sleep": "8:00:00",
                      "deep_sleep": "1:30:00", "rem_sleep": "1:40:00"}
        m["resting_hr"]["resting_heart_rate"] = 44.0
        m["daily_summary"]["bb_min"] = 70
        m["fitness"] = {"atl": 30, "ctl": 35, "tsb": 0.0}
        m["hrv"] = {"status": "LOW", "last_night_avg": 50,
                    "baseline_balanced_low": 58, "baseline_balanced_upper": 81}
        r = coach.compute_recovery_status(m)
        self.assertEqual(r.label, "poor")
        self.assertFalse(r.safe_to_train_hard)

    def test_zero_sleep_is_zero_not_none(self):
        m = self._real_metrics()
        m["sleep"]["total_sleep"] = "0:00:00"
        r = coach.compute_recovery_status(m)
        self.assertIn("общий сон 0.0ч", " | ".join(r.drivers))

    def test_feelings_must_be_adjacent_days(self):
        base = {"sleep": {"day": "2026-07-16", "total_sleep": "8:00:00"},
                "feelings": [{"day": "2026-07-10", "score": 2},
                             {"day": "2026-07-16", "score": 2}]}
        self.assertNotEqual(coach.compute_recovery_status(base).label, "alarm")
        base["feelings"] = [{"day": "2026-07-15", "score": 2},
                            {"day": "2026-07-16", "score": 2}]
        self.assertEqual(coach.compute_recovery_status(base).label, "alarm")

    def test_morning_facts_uses_real_shapes(self):
        mf = coach.compute_morning_facts(self._real_metrics(), today=date(2026, 7, 16))
        self.assertEqual(mf.rhr, 52)
        self.assertEqual(mf.rhr_baseline, 44)
        self.assertEqual(mf.bb_min, 22)
        self.assertAlmostEqual(mf.sleep_total_h, 5.97, places=1)
        self.assertAlmostEqual(mf.acwr, 60/35, places=2)


class TestPlanDayRegexFullDates(unittest.TestCase):
    """Ревью 16.07: на «06.07.2026» regex бэктрекался в месяц «0» → ValueError/порча."""

    def test_full_date_skipped_not_corrupted(self):
        text = "Пн 06.07.2026: лёгкий бег 8 км"
        check = coach.check_plan_dates(text, today=date(2026, 7, 6))
        # полная дата с годом не матчится вовсе — текст не портится
        fixed, n = coach.fix_plan_dates(text, date(2026, 7, 6))
        self.assertEqual(fixed, text)
        self.assertEqual(n, 0)

    def test_short_date_still_matches(self):
        m = coach._PLAN_DAY_RE.search("Пн 06.07: бег")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(3), "07")
