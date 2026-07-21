"""Чтение метрик из per-user Garmin-БД (read-only часть GarminService).

GarminMetricsMixin: collect_* для утра/разбора/прогресса, рекорды, вес,
сводка синка. Запись/синк — garmin_sync.py, фасад — garmin_service.py.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncSummary:
    last_sync_at: str | None
    sleep_rows: int
    sleep_from: str | None
    sleep_to: str | None
    workouts_rows: int
    workouts_from: str | None
    workouts_to: str | None


class GarminMetricsMixin:
    """Хостится в GarminService: полагается на path/time-хелперы фасада."""

    def collect_daily_metrics(self, user_id: int, target_date: date) -> dict[str, Any] | None:
        garmin_db = self._garmin_db_path(user_id, "garmin.db")
        if not garmin_db:
            return None

        result: dict[str, Any] = {"date": target_date.isoformat()}
        date_iso = target_date.isoformat()

        with sqlite3.connect(garmin_db, timeout=5) as conn:
            conn.row_factory = sqlite3.Row

            # Sleep for target date
            row = conn.execute("SELECT * FROM sleep WHERE day = ?", (date_iso,)).fetchone()
            result["sleep"] = dict(row) if row else None

            # Daily summary for target date
            row = conn.execute("SELECT * FROM daily_summary WHERE day = ?", (date_iso,)).fetchone()
            result["daily_summary"] = dict(row) if row else None

            # Resting heart rate
            row = conn.execute("SELECT * FROM resting_hr WHERE day = ?", (date_iso,)).fetchone()
            result["resting_hr"] = dict(row) if row else None

            # Latest weight
            row = conn.execute("SELECT * FROM weight WHERE weight IS NOT NULL ORDER BY day DESC LIMIT 1").fetchone()
            result["weight"] = dict(row) if row else None

            # Состав тела с умных весов (таблица наполняется scaleconnect/sync_body.py).
            # fat_pct = 0 — брак измерения (весы не сняли импеданс), не показываем.
            try:
                row = conn.execute(
                    "SELECT * FROM body_composition WHERE fat_pct > 0 "
                    "ORDER BY day DESC LIMIT 1"
                ).fetchone()
                result["body_composition"] = dict(row) if row else None
            except sqlite3.OperationalError:
                result["body_composition"] = None  # таблицы нет — весы не подключены

            # 7-day sleep history for trends
            week_ago = (target_date - timedelta(days=6)).isoformat()
            rows = conn.execute(
                "SELECT day, score, total_sleep, deep_sleep, rem_sleep, avg_spo2, avg_stress, avg_rr "
                "FROM sleep WHERE day BETWEEN ? AND ? ORDER BY day",
                (week_ago, date_iso),
            ).fetchall()
            result["sleep_trend_7d"] = [dict(r) for r in rows]

            # 7-day daily summary history for trends
            rows = conn.execute(
                "SELECT day, rhr, stress_avg, steps, bb_max, bb_min, bb_charged, spo2_avg, "
                "hr_min, hr_max, calories_active, calories_total, calories_bmr, calories_goal "
                "FROM daily_summary WHERE day BETWEEN ? AND ? ORDER BY day",
                (week_ago, date_iso),
            ).fetchall()
            result["daily_trend_7d"] = [dict(r) for r in rows]

            # 7-day resting HR
            rows = conn.execute(
                "SELECT day, resting_heart_rate FROM resting_hr WHERE day BETWEEN ? AND ? ORDER BY day",
                (week_ago, date_iso),
            ).fetchall()
            result["rhr_trend_7d"] = [dict(r) for r in rows]

        # Activities from garmin_activities.db — last 28 days with HR zones + running dynamics
        activities_db = self._garmin_db_path(user_id, "garmin_activities.db")
        if activities_db:
            with sqlite3.connect(activities_db, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                four_weeks_ago = (target_date - timedelta(days=27)).isoformat()
                # Always include today's activities even if target_date is yesterday
                acts_end = max(date_iso, date.today().isoformat())
                rows = conn.execute(
                    "SELECT a.activity_id, a.sport, a.sub_sport, a.name, a.start_time, "
                    "a.distance, a.calories, a.avg_hr, a.max_hr, a.avg_speed, a.max_speed, "
                    "a.moving_time, a.elapsed_time, a.training_load, a.training_effect, "
                    "a.anaerobic_training_effect, a.self_eval_feel, a.self_eval_effort, "
                    "a.hrz_1_hr, a.hrz_2_hr, a.hrz_3_hr, a.hrz_4_hr, a.hrz_5_hr, "
                    "a.hrz_1_time, a.hrz_2_time, a.hrz_3_time, a.hrz_4_time, a.hrz_5_time, "
                    "s.vo2_max AS run_vo2max, s.avg_steps_per_min, s.avg_step_length, "
                    "s.avg_vertical_oscillation, s.avg_ground_contact_time, s.avg_vertical_ratio "
                    "FROM activities a "
                    "LEFT JOIN steps_activities s ON s.activity_id = a.activity_id "
                    "WHERE DATE(a.start_time) BETWEEN ? AND ? "
                    "ORDER BY a.start_time DESC",
                    (four_weeks_ago, acts_end),
                ).fetchall()
                result["activities_28d"] = [dict(r) for r in rows]
                two_weeks_ago = (target_date - timedelta(days=13)).isoformat()
                result["activities_14d"] = [
                    a for a in result["activities_28d"]
                    if a["start_time"] >= two_weeks_ago
                ]
                week_ago = (target_date - timedelta(days=6)).isoformat()
                result["activities_week"] = [
                    a for a in result["activities_28d"]
                    if a["start_time"] >= week_ago
                ]
                # Observed all-time max HR from running activities (more reliable than formula)
                row = conn.execute(
                    "SELECT MAX(max_hr) AS observed_hr_max FROM activities "
                    "WHERE sport IN ('running','street_running','trail_running','track_running','treadmill_running','indoor_running','virtual_run') AND max_hr IS NOT NULL AND max_hr < 220"
                ).fetchone()
                if row and row["observed_hr_max"]:
                    result["observed_hr_max"] = int(row["observed_hr_max"])
        else:
            result["activities_28d"] = []
            result["activities_14d"] = []
            result["activities_week"] = []

        # Check if we have at least sleep or daily summary
        if not result["sleep"] and not result["daily_summary"]:
            return None

        # HRV from JSON file saved during health sync
        # Garmin may not publish today's HRV until a few minutes/hours after waking;
        # fall back to yesterday's file if today's is not yet available.
        hrv_dir = self._workdir_root / str(user_id) / "HRV"
        hrv_file = hrv_dir / f"hrv_{date_iso}.json"
        if not hrv_file.exists():
            yesterday_iso = (target_date - timedelta(days=1)).isoformat()
            hrv_file = hrv_dir / f"hrv_{yesterday_iso}.json"
        if hrv_file.exists():
            try:
                result["hrv"] = json.loads(hrv_file.read_text())
            except Exception:
                result["hrv"] = None
        else:
            result["hrv"] = None

        # CTL / ATL / TSB (Performance Management Chart)
        result["fitness"] = self._compute_fitness_metrics(user_id, target_date)

        # VO2max + lactate threshold HR from biometric profile
        fp_file = self._workdir_root / str(user_id) / "fitness_profile.json"
        if fp_file.exists():
            try:
                result["fitness_profile"] = json.loads(fp_file.read_text())
            except Exception:
                result["fitness_profile"] = None
        else:
            result["fitness_profile"] = None


        # VO2max history from steps_activities (more complete than JSON file)
        if activities_db:
            try:
                with sqlite3.connect(activities_db, timeout=5) as conn:
                    rows = conn.execute(
                        "SELECT DATE(a.start_time) AS day, s.vo2_max "
                        "FROM steps_activities s "
                        "JOIN activities a ON s.activity_id = a.activity_id "
                        "WHERE s.vo2_max IS NOT NULL "
                        "ORDER BY a.start_time ASC"
                    ).fetchall()
                    # Deduplicate by date (keep last value per day)
                    seen: dict[str, float] = {}
                    for day, v in rows:
                        seen[day] = v
                    result["vo2max_history"] = [
                        {"date": d, "vo2_max": v} for d, v in sorted(seen.items())
                    ]
            except Exception:
                result["vo2max_history"] = []
        else:
            result["vo2max_history"] = []

        # Long-term weekly summary (last 26 weeks) for plan context
        summary_db = self._garmin_db_path(user_id, "garmin_summary.db")
        if summary_db:
            try:
                six_months_ago = (target_date - timedelta(days=182)).isoformat()
                with sqlite3.connect(summary_db, timeout=5) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT first_day, rhr_avg, sleep_avg, rem_sleep_avg, "
                        "stress_avg, steps, bb_max, bb_min "
                        "FROM weeks_summary WHERE first_day >= ? AND rhr_avg IS NOT NULL "
                        "ORDER BY first_day ASC",
                        (six_months_ago,),
                    ).fetchall()
                    result["weeks_summary"] = [dict(r) for r in rows]
                    # Monthly summary too
                    rows = conn.execute(
                        "SELECT first_day, rhr_avg, sleep_avg, steps, activities "
                        "FROM months_summary WHERE first_day >= ? AND rhr_avg IS NOT NULL "
                        "ORDER BY first_day ASC",
                        ((target_date - timedelta(days=365)).isoformat(),),
                    ).fetchall()
                    result["months_summary"] = [dict(r) for r in rows]
            except Exception:
                result["weeks_summary"] = []
                result["months_summary"] = []
        else:
            result["weeks_summary"] = []
            result["months_summary"] = []

        return result

    def _compute_fitness_metrics(self, user_id: int, target_date: date) -> dict[str, Any] | None:
        """Compute CTL (42d EMA), ATL (7d EMA) and TSB=CTL-ATL from activity training load."""
        activities_db = self._garmin_db_path(user_id, "garmin_activities.db")
        if not activities_db:
            return None
        cutoff = (target_date - timedelta(days=120)).isoformat()
        with sqlite3.connect(activities_db, timeout=5) as conn:
            rows = conn.execute(
                "SELECT DATE(start_time) AS day, SUM(training_load) AS daily_tl "
                "FROM activities WHERE DATE(start_time) BETWEEN ? AND ? "
                "GROUP BY DATE(start_time) ORDER BY day",
                (cutoff, target_date.isoformat()),
            ).fetchall()
        if not rows:
            return None
        daily_tl: dict[str, float] = {r[0]: float(r[1] or 0) for r in rows}
        ctl = atl = 0.0
        for i in range(120):
            day = (target_date - timedelta(days=119 - i)).isoformat()
            tl = daily_tl.get(day, 0.0)
            ctl = tl / 42 + ctl * (1 - 1 / 42)
            atl = tl / 7 + atl * (1 - 1 / 7)
        return {"ctl": round(ctl, 1), "atl": round(atl, 1), "tsb": round(ctl - atl, 1)}

    def collect_sleep_for_date(self, user_id: int, target_date: date) -> dict[str, Any] | None:
        garmin_db = self._garmin_db_path(user_id, "garmin.db")
        if not garmin_db:
            return None
        with sqlite3.connect(garmin_db, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM sleep WHERE day = ?", (target_date.isoformat(),)).fetchone()
        return dict(row) if row else None

    def collect_hrv_for_date(self, user_id: int, target_date: date) -> dict[str, Any] | None:
        """Read HRV JSON file for the given date (no fallback)."""
        hrv_file = self._workdir_root / str(user_id) / "HRV" / f"hrv_{target_date.isoformat()}.json"
        if hrv_file.exists():
            try:
                return json.loads(hrv_file.read_text())
            except Exception:
                pass
        return None

    def collect_weight_history(self, user_id: int, days: int = 90) -> list[dict[str, Any]]:
        """Return weight entries for the last N days."""
        garmin_db = self._garmin_db_path(user_id, "garmin.db")
        if not garmin_db:
            return []
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with sqlite3.connect(garmin_db, timeout=5) as conn:
            rows = conn.execute(
                "SELECT day, weight FROM weight WHERE day >= ? AND weight IS NOT NULL ORDER BY day",
                (cutoff,),
            ).fetchall()
        return [{"day": r[0], "weight": r[1]} for r in rows]

    def collect_body_composition_history(
        self, user_id: int, days: int = 180
    ) -> list[dict[str, Any]]:
        """История состава тела с умных весов. Пустой список, если весов нет."""
        garmin_db = self._garmin_db_path(user_id, "garmin.db")
        if not garmin_db:
            return []
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        try:
            with sqlite3.connect(garmin_db, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT day, weight, fat_pct, muscle_kg, muscle_pct, water_pct, "
                    "visceral_fat, bmr_kcal FROM body_composition "
                    "WHERE day >= ? AND fat_pct > 0 ORDER BY day",
                    (cutoff,),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    def collect_personal_records(self, user_id: int) -> list[dict[str, Any]]:
        """Return best running performances at standard distances from activity data."""
        activities_db = self._garmin_db_path(user_id, "garmin_activities.db")
        if not activities_db:
            return []
        records: list[dict[str, Any]] = []
        with sqlite3.connect(activities_db, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            # Best pace per standard distance bucket from actual activities
            # We look at running activities and find fastest at each distance range
            rows = conn.execute(
                "SELECT activity_id, name, start_time, distance, moving_time, avg_hr, avg_speed "
                "FROM activities WHERE sport IN ('running','street_running','trail_running','track_running','treadmill_running','indoor_running','virtual_run') AND distance IS NOT NULL "
                "AND moving_time IS NOT NULL ORDER BY start_time DESC"
            ).fetchall()

        # Distance buckets: 1km, 5km, 10km, 15km, 21km, 42km
        buckets = [
            ("1 км", 0.8, 1.5),
            ("5 км", 4.5, 5.5),
            ("10 км", 9.5, 10.5),
            ("15 км", 14.5, 16.0),
            ("Полумарафон", 20.0, 22.0),
            ("Марафон", 41.0, 43.0),
        ]
        for label, lo, hi in buckets:
            matching = [
                dict(r) for r in rows
                if lo <= (r["distance"] or 0) <= hi
            ]
            if not matching:
                continue
            # Find fastest by avg_speed (highest = fastest)
            best = max(matching, key=lambda a: a.get("avg_speed") or 0)
            secs = self._time_str_to_secs_static(best["moving_time"])
            if secs > 0:
                h = int(secs // 3600)
                m = int((secs % 3600) // 60)
                s = int(secs % 60)
                time_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
                pace = secs / best["distance"] / 60 if best["distance"] else 0
                pace_str = f"{int(pace)}:{int((pace % 1) * 60):02d}" if pace > 0 else "?"
                records.append({
                    "distance": label,
                    "time": time_str,
                    "pace": pace_str,
                    "date": best["start_time"][:10],
                    "name": best.get("name", ""),
                    "avg_hr": best.get("avg_hr"),
                    "dist_km": best.get("distance"),
                })
        return records

    @staticmethod
    def _time_str_to_secs_static(time_str) -> float:
        if not time_str:
            return 0.0
        try:
            s = str(time_str).split(".")[0]
            parts = s.split(":")
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
        except Exception:
            pass
        return 0.0

    def get_db_paths(self, user_id: int) -> dict[str, str]:
        """Return paths to available SQLite databases for this user."""
        paths = {}
        garmin = self._garmin_db_path(user_id, "garmin.db")
        if garmin:
            paths["garmin"] = str(garmin)
        activities = self._garmin_db_path(user_id, "garmin_activities.db")
        if activities:
            paths["activities"] = str(activities)
        return paths

    def get_sync_summary(self, user_id: int) -> SyncSummary | None:
        analytics_db = self._analytics_db_path_for_user(user_id)
        if not analytics_db.exists():
            return None

        def _safe_one(row):
            if not row:
                return None
            v = row[0]
            return None if v is None else str(v)

        with sqlite3.connect(analytics_db, timeout=5) as conn:
            last_sync_at = _safe_one(conn.execute("SELECT last_sync_at FROM sync_meta WHERE id = 1").fetchone())

            sleep_row = conn.execute(
                "SELECT COUNT(*), MIN(day), MAX(day) FROM coach_sleep"
            ).fetchone()
            sleep_rows = int((sleep_row[0] or 0) if sleep_row else 0)
            sleep_from = _safe_one((sleep_row[1],)) if sleep_row else None
            sleep_to = _safe_one((sleep_row[2],)) if sleep_row else None

            workouts_row = conn.execute(
                "SELECT COUNT(*), MIN(start_date), MAX(start_date) FROM coach_workouts"
            ).fetchone()
            workouts_rows = int((workouts_row[0] or 0) if workouts_row else 0)
            workouts_from = _safe_one((workouts_row[1],)) if workouts_row else None
            workouts_to = _safe_one((workouts_row[2],)) if workouts_row else None

        return SyncSummary(
            last_sync_at=last_sync_at,
            sleep_rows=sleep_rows,
            sleep_from=sleep_from,
            sleep_to=sleep_to,
            workouts_rows=workouts_rows,
            workouts_from=workouts_from,
            workouts_to=workouts_to,
        )

    def collect_recent_activities(self, user_id: int, days: int = 7) -> list[dict[str, Any]]:
        """Return activities from the last `days` days, newest first, with per-km splits."""
        activities_db = self._garmin_db_path(user_id, "garmin_activities.db")
        if not activities_db:
            return []
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with sqlite3.connect(activities_db, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            # Get only columns that exist in this garmindb version
            existing = {c[1] for c in conn.execute("PRAGMA table_info(activities)").fetchall()}
            wanted = [
                "activity_id", "sport", "sub_sport", "name", "start_time", "distance", "calories",
                "avg_hr", "max_hr", "avg_speed", "max_speed", "moving_time", "elapsed_time",
                "training_load", "training_effect", "anaerobic_training_effect",
                "avg_cadence", "max_cadence",
                "hrz_1_hr", "hrz_2_hr", "hrz_3_hr", "hrz_4_hr", "hrz_5_hr",
                "hrz_1_time", "hrz_2_time", "hrz_3_time", "hrz_4_time", "hrz_5_time",
                "ascent", "descent", "avg_temperature",
            ]
            cols = ", ".join(c for c in wanted if c in existing)
            rows = conn.execute(
                f"SELECT {cols} FROM activities WHERE DATE(start_time) >= ? ORDER BY start_time DESC",
                (cutoff,),
            ).fetchall()
            activities = [dict(r) for r in rows]

            # Attach per-km splits for each activity (from activity_records)
            has_records = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='activity_records'"
            ).fetchone()
            laps_dir = self._workdir_root / str(user_id) / "laps"
            splits_dir = self._workdir_root / str(user_id) / "splits"
            weather_dir = self._workdir_root / str(user_id) / "weather"
            for act in activities:
                act["km_splits"] = []
                act["laps"] = []
                act["weather"] = None
                if not act.get("activity_id"):
                    continue
                # Load laps from JSON (fetched via Garmin Connect API)
                lap_file = laps_dir / f"{act['activity_id']}.json"
                if lap_file.exists():
                    try:
                        act["laps"] = json.loads(lap_file.read_text())
                    except Exception:
                        pass
                # Load weather from JSON (fetched via Garmin Connect API)
                weather_file = weather_dir / f"{act['activity_id']}.json"
                if weather_file.exists():
                    try:
                        act["weather"] = json.loads(weather_file.read_text())
                    except Exception:
                        pass
                # Km-сплиты: сперва JSON от garth-синка (ревью 16.07: раньше файл
                # никто не читал, а activity_records после миграции пуста)
                split_file = splits_dir / f"{act['activity_id']}.json"
                if split_file.exists():
                    try:
                        act["km_splits"] = json.loads(split_file.read_text())
                    except Exception:
                        pass
                if act["km_splits"]:
                    continue
                # Fallback: посекундные точки GarminDB-эры (legacy-юзеры)
                if not has_records:
                    continue
                records = conn.execute(
                    "SELECT distance, hr, cadence FROM activity_records "
                    "WHERE activity_id=? ORDER BY timestamp",
                    (act["activity_id"],),
                ).fetchall()
                act["km_splits"] = self._compute_km_splits(records)

        return activities

    @staticmethod
    def _compute_km_splits(records) -> list[dict[str, Any]]:
        """Aggregate per-second records into 1km buckets with pace, avg HR and cadence."""
        from collections import defaultdict
        km_data: dict[int, dict] = defaultdict(lambda: {"hr": [], "cadence": [], "count": 0})
        for r in records:
            dist = r[0]  # distance in km
            if dist is None:
                continue
            km = int(dist)
            km_data[km]["count"] += 1
            hr = r[1]
            cad = r[2]
            if hr and hr > 0:
                km_data[km]["hr"].append(hr)
            if cad and cad > 0:
                km_data[km]["cadence"].append(cad)

        splits = []
        for km in sorted(km_data.keys()):
            d = km_data[km]
            secs = d["count"]
            # Skip the last partial km if it's less than 200m (< ~72 seconds at 6 min/km)
            if km == max(km_data.keys()) and secs < 72:
                continue
            avg_hr = round(sum(d["hr"]) / len(d["hr"])) if d["hr"] else None
            # garmindb stores cadence in cycles/min (one leg); ×2 = steps/min as shown in Garmin Connect
            avg_cad = round(sum(d["cadence"]) / len(d["cadence"]) * 2) if d["cadence"] else None
            pace_min = secs / 60
            pace_str = f"{int(pace_min)}:{int((pace_min % 1) * 60):02d}"
            splits.append({
                "km": km + 1,
                "pace": pace_str,
                "avg_hr": avg_hr,
                "avg_cadence": avg_cad,
            })
        return splits
