from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _mask_email(value: str) -> str:
    """Маскировка email/логина Garmin для логов — не светим PII в journalctl."""
    if not value:
        return "<empty>"
    if "@" in value:
        local, _, domain = value.partition("@")
        return f"{local[:1]}***@{domain}"
    return f"{value[:1]}***"


@dataclass
class BackupResult:
    output_dir: Path


@dataclass(frozen=True)
class SyncSummary:
    last_sync_at: str | None
    sleep_rows: int
    sleep_from: str | None
    sleep_to: str | None
    workouts_rows: int
    workouts_from: str | None
    workouts_to: str | None


class GarminService:
    def __init__(self, workdir_root: Path, exports_dir: Path) -> None:
        self._workdir_root = workdir_root
        self._exports_dir = exports_dir
        self._workdir_root.mkdir(parents=True, exist_ok=True)
        self._exports_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _secs_to_time_str(secs: float | None) -> str | None:
        if secs is None:
            return None
        total_secs = int(round(secs))
        h = total_secs // 3600
        m = (total_secs % 3600) // 60
        s = total_secs % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    @staticmethod
    def _secs_to_time_str_precise(secs: float | None) -> str | None:
        if secs is None:
            return None
        total_secs = int(secs)
        ms = int(round((secs - total_secs) * 1000))
        h = total_secs // 3600
        m = (total_secs % 3600) // 60
        s = total_secs % 60
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}000"

    @staticmethod
    def _secs_to_pace_str(secs: float | None) -> str | None:
        if secs is None or secs <= 0:
            return None
        total_secs = int(secs)
        ms = int(round((secs - total_secs) * 1000000))
        h = total_secs // 3600
        m = (total_secs % 3600) // 60
        s = total_secs % 60
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:06d}"

    def _connect_api_with_retry(self, garth_client, path: str, params: dict | None = None, retries: int = 3, delay: float = 1.0) -> Any:
        from garth.exc import GarthHTTPError
        import time
        for attempt in range(retries):
            try:
                time.sleep(0.1)  # small throttle
                return garth_client.connectapi(path, params=params)
            except GarthHTTPError as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code == 429:
                    wait_time = delay * (2 ** attempt)
                    logger.warning("Garmin Connect rate limit (429) for %s. Waiting %.1fs (attempt %d/%d)", path, wait_time, attempt + 1, retries)
                    time.sleep(wait_time)
                else:
                    raise exc
        raise RuntimeError(f"Garmin Connect rate limit exceeded (429) for {path} after retries.")

    def _init_user_dbs(self, user_id: int) -> None:
        """Create the user's DBs folder and create the tables if they don't exist."""
        dbs_dir = self._workdir_root / str(user_id) / "DBs"
        dbs_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. garmin.db
        garmin_db = dbs_dir / "garmin.db"
        with sqlite3.connect(garmin_db, timeout=5) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            # sleep
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sleep (
                    day DATE PRIMARY KEY,
                    start DATETIME,
                    end DATETIME,
                    total_sleep TIME,
                    deep_sleep TIME,
                    light_sleep TIME,
                    rem_sleep TIME,
                    awake TIME,
                    avg_spo2 FLOAT,
                    avg_rr FLOAT,
                    avg_stress FLOAT,
                    score INTEGER,
                    qualifier VARCHAR
                )
                """
            )
            # resting_hr
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS resting_hr (
                    day DATE PRIMARY KEY,
                    resting_heart_rate FLOAT
                )
                """
            )
            # daily_summary
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_summary (
                    day DATE PRIMARY KEY,
                    hr_min INTEGER,
                    hr_max INTEGER,
                    rhr INTEGER,
                    stress_avg INTEGER,
                    step_goal INTEGER,
                    steps INTEGER,
                    moderate_activity_time TIME,
                    vigorous_activity_time TIME,
                    intensity_time_goal TIME,
                    floors_up FLOAT,
                    floors_down FLOAT,
                    floors_goal FLOAT,
                    distance FLOAT,
                    calories_goal INTEGER,
                    calories_total INTEGER,
                    calories_bmr INTEGER,
                    calories_active INTEGER,
                    calories_consumed INTEGER,
                    hydration_goal INTEGER,
                    hydration_intake INTEGER,
                    sweat_loss INTEGER,
                    spo2_avg FLOAT,
                    spo2_min FLOAT,
                    rr_waking_avg FLOAT,
                    rr_max FLOAT,
                    rr_min FLOAT,
                    bb_charged INTEGER,
                    bb_max INTEGER,
                    bb_min INTEGER,
                    description VARCHAR
                )
                """
            )
            # weight
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS weight (
                    day DATE PRIMARY KEY,
                    weight FLOAT
                )
                """
            )
            
        # 2. garmin_activities.db
        activities_db = dbs_dir / "garmin_activities.db"
        with sqlite3.connect(activities_db, timeout=5) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            # activities
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS activities (
                    activity_id VARCHAR PRIMARY KEY,
                    name VARCHAR,
                    description VARCHAR,
                    type VARCHAR,
                    course_id INTEGER,
                    laps INTEGER,
                    sport VARCHAR,
                    sub_sport VARCHAR,
                    device_serial_number INTEGER,
                    self_eval_feel VARCHAR,
                    self_eval_effort VARCHAR,
                    training_load FLOAT,
                    training_effect FLOAT,
                    anaerobic_training_effect FLOAT,
                    start_time DATETIME,
                    stop_time DATETIME,
                    elapsed_time TIME,
                    moving_time TIME,
                    distance FLOAT,
                    cycles FLOAT,
                    avg_hr INTEGER,
                    max_hr INTEGER,
                    avg_rr FLOAT,
                    max_rr FLOAT,
                    calories INTEGER,
                    avg_cadence INTEGER,
                    max_cadence INTEGER,
                    avg_speed FLOAT,
                    max_speed FLOAT,
                    ascent FLOAT,
                    descent FLOAT,
                    max_temperature FLOAT,
                    min_temperature FLOAT,
                    avg_temperature FLOAT,
                    start_lat FLOAT,
                    start_long FLOAT,
                    stop_lat FLOAT,
                    stop_long FLOAT,
                    hr_zones_method VARCHAR(18),
                    hrz_1_hr INTEGER,
                    hrz_2_hr INTEGER,
                    hrz_3_hr INTEGER,
                    hrz_4_hr INTEGER,
                    hrz_5_hr INTEGER,
                    hrz_1_time TIME,
                    hrz_2_time TIME,
                    hrz_3_time TIME,
                    hrz_4_time TIME,
                    hrz_5_time TIME
                )
                """
            )
            # activity_laps
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_laps (
                    activity_id VARCHAR,
                    lap INTEGER,
                    start_time DATETIME,
                    stop_time DATETIME,
                    elapsed_time TIME,
                    moving_time TIME,
                    distance FLOAT,
                    cycles FLOAT,
                    avg_hr INTEGER,
                    max_hr INTEGER,
                    avg_rr FLOAT,
                    max_rr FLOAT,
                    calories INTEGER,
                    avg_cadence INTEGER,
                    max_cadence INTEGER,
                    avg_speed FLOAT,
                    max_speed FLOAT,
                    ascent FLOAT,
                    descent FLOAT,
                    max_temperature FLOAT,
                    min_temperature FLOAT,
                    avg_temperature FLOAT,
                    start_lat FLOAT,
                    start_long FLOAT,
                    stop_lat FLOAT,
                    stop_long FLOAT,
                    hr_zones_method VARCHAR(18),
                    hrz_1_hr INTEGER,
                    hrz_2_hr INTEGER,
                    hrz_3_hr INTEGER,
                    hrz_4_hr INTEGER,
                    hrz_5_hr INTEGER,
                    hrz_1_time TIME,
                    hrz_2_time TIME,
                    hrz_3_time TIME,
                    hrz_4_time TIME,
                    hrz_5_time TIME,
                    PRIMARY KEY (activity_id, lap)
                )
                """
            )
            # steps_activities
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS steps_activities (
                    steps INTEGER,
                    avg_pace TIME,
                    avg_moving_pace TIME,
                    max_pace TIME,
                    avg_steps_per_min INTEGER,
                    max_steps_per_min INTEGER,
                    avg_step_length FLOAT,
                    avg_vertical_ratio FLOAT,
                    avg_vertical_oscillation FLOAT,
                    avg_gct_balance FLOAT,
                    avg_ground_contact_time TIME,
                    avg_stance_time_percent FLOAT,
                    vo2_max FLOAT,
                    activity_id VARCHAR PRIMARY KEY
                )
                """
            )

    def is_initial_sync_pending(self, user_id: int) -> bool:
        """True, если у юзера ещё не было health-синка (garmin.db пуст).

        Первичный синк тяжёлый (~365 дней, сотни HTTP-запросов к Garmin) —
        bot.py пускает такие строго по одному, чтобы не словить 429/бан IP.
        """
        garmin_db = self._workdir_root / str(user_id) / "DBs" / "garmin.db"
        if not garmin_db.exists():
            return True
        try:
            with sqlite3.connect(garmin_db, timeout=5) as conn:
                row = conn.execute("SELECT COUNT(*) FROM sleep").fetchone()
                return not (row and row[0] > 0)
        except Exception:
            return True

    def _get_sync_range(self, user_id: int) -> tuple[date, date]:
        """Determine sync start and end dates.
        Initial sync uses GARMIN_START_DATE env, incremental uses last 14 days."""
        today = date.today()

        if not self.is_initial_sync_pending(user_id):
            start_date = today - timedelta(days=14)
            logger.info("Incremental sync: syncing last 14 days (%s to %s)", start_date, today)
            return start_date, today
            
        # Initial sync from GARMIN_START_DATE
        raw_start = (os.getenv("GARMIN_START_DATE", "") or "").strip()
        if raw_start:
            try:
                start_date = datetime.strptime(raw_start, "%Y-%m-%d").date()
                logger.info("Initial sync: syncing from GARMIN_START_DATE=%s to %s", start_date, today)
                return start_date, today
            except ValueError:
                pass
        
        start_date = today - timedelta(days=365)
        logger.info("Initial sync: syncing last 365 days (%s to %s)", start_date, today)
        return start_date, today

    def run_health_sync(self, user_id: int, username: str, password: str) -> BackupResult:
        """Sync sleep, RHR, weight and daily summary (no activities) in pure Python, then fetch HRV."""
        output_dir = (self._workdir_root / str(user_id)).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        self._init_user_dbs(user_id)
        
        from garth import SleepData, WeightData, UserProfile, DailyBodyBatteryStress  # type: ignore

        garth_client = self._garth_login(username, password, output_dir)
        start_date, end_date = self._get_sync_range(user_id)
        delta_days = (end_date - start_date).days + 1

        # 1. Sync Sleep list in one request
        logger.info("Fetching sleep list for %d days...", delta_days)
        try:
            sleep_list = SleepData.list(end=end_date, days=delta_days, client=garth_client)
        except Exception as exc:
            logger.warning("Failed to fetch sleep list: %s", exc)
            sleep_list = []
            
        garmin_db = output_dir / "DBs" / "garmin.db"
        with sqlite3.connect(garmin_db, timeout=5) as conn:
            for s in sleep_list:
                dto = s.daily_sleep_dto
                day_iso = dto.calendar_date.isoformat()
                score = dto.sleep_scores.overall.value if getattr(dto, "sleep_scores", None) and dto.sleep_scores.overall else None
                conn.execute(
                    """
                    INSERT OR REPLACE INTO sleep (
                        day, start, end, total_sleep, deep_sleep, light_sleep, rem_sleep, awake,
                        avg_spo2, avg_rr, avg_stress, score, qualifier
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        day_iso,
                        dto.sleep_start.strftime("%Y-%m-%d %H:%M:%S.000000") if dto.sleep_start else None,
                        dto.sleep_end.strftime("%Y-%m-%d %H:%M:%S.000000") if dto.sleep_end else None,
                        self._secs_to_time_str(dto.sleep_time_seconds),
                        self._secs_to_time_str(dto.deep_sleep_seconds),
                        self._secs_to_time_str(dto.light_sleep_seconds),
                        self._secs_to_time_str(dto.rem_sleep_seconds),
                        self._secs_to_time_str(dto.awake_sleep_seconds),
                        dto.average_sp_o2_value,
                        dto.average_respiration_value,
                        dto.avg_sleep_stress,
                        score,
                        dto.sleep_score_feedback
                    )
                )

        # 2. Sync Weight list in one request
        logger.info("Fetching weight list...")
        try:
            weight_list = WeightData.list(end=end_date, days=delta_days, client=garth_client)
        except Exception as exc:
            logger.warning("Failed to fetch weight list: %s", exc)
            weight_list = []
            
        with sqlite3.connect(garmin_db, timeout=5) as conn:
            for w in weight_list:
                day_iso = w.calendar_date.isoformat()
                conn.execute(
                    "INSERT OR REPLACE INTO weight (day, weight) VALUES (?, ?)",
                    (day_iso, w.weight / 1000.0)
                )

        # 3. Sync Daily Summary & Stress (day-by-day)
        logger.info("Fetching daily summaries day-by-day...")
        profile = UserProfile.get(client=garth_client)
        display_name = profile.display_name
        
        for i in range(delta_days):
            day = end_date - timedelta(days=i)
            day_iso = day.isoformat()
            
            avg_stress = max_stress = bb_max = bb_min = bb_charged = None
            try:
                bb = DailyBodyBatteryStress.get(day_iso, client=garth_client)
                if bb:
                    avg_stress = bb.avg_stress_level
                    max_stress = bb.max_stress_level
                    bb_max = bb.max_body_battery
                    bb_min = bb.min_body_battery
                    bb_charged = bb.body_battery_change
            except Exception as exc:
                logger.debug("Failed to get stress/bb for %s: %s", day_iso, exc)
                
            rhr = min_hr = max_hr = calories_active = calories_total = calories_bmr = None
            calories_goal = distance_km = steps = step_goal = spo2_avg = spo2_min = None
            rr_waking_avg = rr_max = rr_min = floors_up = floors_down = floors_goal = None
            moderate_min = vigorous_min = intensity_goal = sweat_loss = None
            hydration_goal = hydration_intake = description = None
            
            try:
                summary = self._connect_api_with_retry(garth_client, f"/usersummary-service/usersummary/daily/{display_name}", params={"calendarDate": day_iso})
                if summary:
                    rhr = summary.get("restingHeartRate")
                    min_hr = summary.get("minHeartRate") or summary.get("minAvgHeartRate")
                    max_hr = summary.get("maxHeartRate")
                    calories_active = summary.get("activeKilocalories") or summary.get("wellnessActiveKilocalories")
                    calories_total = summary.get("totalKilocalories") or summary.get("wellnessKilocalories")
                    calories_bmr = summary.get("bmrKilocalories")
                    calories_goal = summary.get("netCalorieGoal")
                    dist_m = summary.get("totalDistanceMeters") or summary.get("wellnessDistanceMeters")
                    if dist_m is not None:
                        distance_km = dist_m / 1000.0
                    steps = summary.get("totalSteps")
                    step_goal = summary.get("dailyStepGoal")
                    spo2_avg = summary.get("averageSpo2")
                    spo2_min = summary.get("lowestSpo2")
                    rr_waking_avg = summary.get("avgWakingRespirationValue")
                    rr_max = summary.get("highestRespirationValue")
                    rr_min = summary.get("lowestRespirationValue")
                    floors_up = summary.get("floorsAscended")
                    floors_down = summary.get("floorsDescended")
                    floors_goal = summary.get("userFloorsAscendedGoal")
                    moderate_min = summary.get("moderateIntensityMinutes")
                    vigorous_min = summary.get("vigorousIntensityMinutes")
                    intensity_goal = summary.get("intensityMinutesGoal")
                    sweat_loss = summary.get("sweatLoss")
                    hydration_goal = summary.get("hydrationGoal")
                    hydration_intake = summary.get("hydrationIntake")
                    description = summary.get("wellnessDescription")
            except Exception as exc:
                logger.debug("Failed to get daily summary for %s: %s", day_iso, exc)
                
            with sqlite3.connect(garmin_db, timeout=5) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO daily_summary (
                        day, hr_min, hr_max, rhr, stress_avg, step_goal, steps,
                        moderate_activity_time, vigorous_activity_time, intensity_time_goal,
                        floors_up, floors_down, floors_goal, distance,
                        calories_goal, calories_total, calories_bmr, calories_active, calories_consumed,
                        hydration_goal, hydration_intake, sweat_loss, spo2_avg, spo2_min,
                        rr_waking_avg, rr_max, rr_min, bb_charged, bb_max, bb_min, description
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        day_iso, min_hr, max_hr, rhr, avg_stress, step_goal, steps,
                        self._secs_to_time_str((moderate_min * 60) if moderate_min is not None else None),
                        self._secs_to_time_str((vigorous_min * 60) if vigorous_min is not None else None),
                        self._secs_to_time_str((intensity_goal * 60) if intensity_goal is not None else None),
                        floors_up, floors_down, floors_goal, distance_km,
                        calories_goal, calories_total, calories_bmr, calories_active, None,
                        hydration_goal, hydration_intake, sweat_loss, spo2_avg, spo2_min,
                        rr_waking_avg, rr_max, rr_min, bb_charged, bb_max, bb_min, description
                    )
                )
                if rhr is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO resting_hr (day, resting_heart_rate) VALUES (?, ?)",
                        (day_iso, float(rhr))
                    )

        # 4. Fetch HRV
        try:
            self._fetch_and_store_hrv(username, password, output_dir)
        except Exception as exc:
            logger.warning("HRV fetch failed (non-fatal): %s", exc)
            
        self._refresh_user_analytics_db(user_id=user_id, output_dir=output_dir)
        return BackupResult(output_dir=output_dir)

    def run_activity_sync(self, user_id: int, username: str, password: str) -> BackupResult:
        """Sync activities list, details, zones, laps, and splits in pure Python."""
        output_dir = (self._workdir_root / str(user_id)).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        self._init_user_dbs(user_id)
        
        garth_client = self._garth_login(username, password, output_dir)
        start_date, end_date = self._get_sync_range(user_id)
        
        logger.info("Fetching activities list paging since %s...", start_date)
        activities = []
        start = 0
        limit = 50
        while True:
            try:
                page = garth_client.connectapi("/activitylist-service/activities/search/activities", params={"start": start, "limit": limit})
            except Exception as exc:
                logger.warning("Failed to fetch activities page start=%d: %s", start, exc)
                break
            if not page:
                break
            added_any = False
            for act in page:
                start_time_str = act.get("startTimeLocal")
                if not start_time_str:
                    continue
                try:
                    act_date = datetime.strptime(start_time_str.split(".")[0], "%Y-%m-%d %H:%M:%S").date()
                except ValueError:
                    try:
                        act_date = datetime.strptime(start_time_str.split("T")[0], "%Y-%m-%d").date()
                    except ValueError:
                        continue
                if act_date >= start_date:
                    activities.append(act)
                    added_any = True
            if not added_any or len(page) < limit:
                break
            start += limit
            
        logger.info("Found %d activities in sync range.", len(activities))
        
        activities_db = output_dir / "DBs" / "garmin_activities.db"
        with sqlite3.connect(activities_db, timeout=5) as conn:
            for act in activities:
                activity_id = act.get("activityId")
                if not activity_id:
                    continue
                
                detail = {}
                try:
                    detail = self._connect_api_with_retry(garth_client,f"/activity-service/activity/{activity_id}")
                except Exception as exc:
                    logger.warning("Failed to get activity detail for %s: %s", activity_id, exc)
                    
                zones = []
                try:
                    zones = self._connect_api_with_retry(garth_client,f"/activity-service/activity/{activity_id}/hrTimeInZones")
                except Exception as exc:
                    logger.debug("Failed to get HR zones for activity %s: %s", activity_id, exc)
                    
                hrz_time = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0}
                hrz_hr = {1: None, 2: None, 3: None, 4: None, 5: None}
                for z in (zones or []):
                    num = z.get("zoneNumber")
                    if num in hrz_time:
                        hrz_time[num] = z.get("secsInZone") or 0.0
                        hrz_hr[num] = z.get("zoneLowBoundary")
                        
                lap_dtos = []
                try:
                    laps_resp = self._connect_api_with_retry(garth_client,f"/activity-service/activity/{activity_id}/laps")
                    lap_dtos = (laps_resp or {}).get("lapDTOs") or []
                except Exception as exc:
                    logger.debug("Failed to get laps for activity %s: %s", activity_id, exc)
                    
                # Fetch and save kilometer splits to JSON
                try:
                    splits_resp = self._connect_api_with_retry(garth_client,f"/activity-service/activity/{activity_id}/splits")
                    lap_splits = splits_resp.get("lapDTOs") or []
                    km_splits = []
                    for idx, lap in enumerate(lap_splits):
                        dist_m = lap.get("distance") or 0.0
                        if idx == len(lap_splits) - 1 and dist_m < 200:
                            continue
                        dur_s = lap.get("duration") or 0.0
                        avg_hr = lap.get("averageHR")
                        avg_cad = round(lap.get("averageRunCadence")) if lap.get("averageRunCadence") else None
                        
                        pace_str = None
                        if dist_m > 0 and dur_s > 0:
                            pace_min = (dur_s / 60.0) / (dist_m / 1000.0)
                            pace_str = f"{int(pace_min)}:{int((pace_min % 1) * 60):02d}"
                            
                        km_splits.append({
                            "km": idx + 1,
                            "pace": pace_str,
                            "avg_hr": avg_hr,
                            "avg_cadence": avg_cad,
                        })
                    splits_dir = output_dir / "splits"
                    splits_dir.mkdir(parents=True, exist_ok=True)
                    (splits_dir / f"{activity_id}.json").write_text(json.dumps(km_splits, ensure_ascii=False))
                except Exception as exc:
                    logger.debug("Failed to process splits for activity %s: %s", activity_id, exc)

                summary = detail.get("summaryDTO") or act
                metadata = detail.get("metadataDTO") or {}
                
                start_time_local = summary.get("startTimeLocal")
                if start_time_local and len(start_time_local) == 19:
                    start_time_local += ".000000"
                stop_time_local = summary.get("startTimeLocal")
                
                avg_speed = None
                max_speed = None
                speed_m_s = summary.get("averageSpeed") or summary.get("averageMovingSpeed")
                if speed_m_s is not None:
                    avg_speed = speed_m_s * 3.6
                max_speed_m_s = summary.get("maxSpeed")
                if max_speed_m_s is not None:
                    max_speed = max_speed_m_s * 3.6
                    
                conn.execute(
                    """
                    INSERT OR REPLACE INTO activities (
                        activity_id, name, description, type, course_id, laps, sport, sub_sport,
                        device_serial_number, self_eval_feel, self_eval_effort,
                        training_load, training_effect, anaerobic_training_effect,
                        start_time, stop_time, elapsed_time, moving_time, distance, cycles,
                        avg_hr, max_hr, avg_rr, max_rr, calories, avg_cadence, max_cadence,
                        avg_speed, max_speed, ascent, descent, max_temperature, min_temperature, avg_temperature,
                        start_lat, start_long, stop_lat, stop_long, hr_zones_method,
                        hrz_1_hr, hrz_2_hr, hrz_3_hr, hrz_4_hr, hrz_5_hr,
                        hrz_1_time, hrz_2_time, hrz_3_time, hrz_4_time, hrz_5_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(activity_id),
                        summary.get("activityName") or act.get("activityName"),
                        summary.get("description"),
                        (act.get("activityType") or {}).get("typeKey") or (summary.get("activityTypeDTO") or {}).get("typeKey"),
                        summary.get("courseId"),
                        len(lap_dtos) if lap_dtos else act.get("laps"),
                        (act.get("activityType") or {}).get("typeKey") or (summary.get("activityTypeDTO") or {}).get("typeKey"),
                        (act.get("activityType") or {}).get("parentTypeKey") or (summary.get("activityTypeDTO") or {}).get("parentTypeKey"),
                        metadata.get("deviceMetaDataDTO", {}).get("deviceId"),
                        summary.get("directWorkoutFeel"),
                        summary.get("directWorkoutRpe"),
                        summary.get("activityTrainingLoad") or act.get("activityTrainingLoad"),
                        summary.get("trainingEffect") or act.get("trainingEffect"),
                        summary.get("anaerobicTrainingEffect") or act.get("anaerobicTrainingEffect"),
                        start_time_local,
                        stop_time_local,
                        self._secs_to_time_str(summary.get("duration")) or "00:00:00",
                        self._secs_to_time_str(summary.get("movingDuration")) or "00:00:00",
                        (summary.get("distance") or 0.0) / 1000.0,
                        summary.get("cycles") or summary.get("steps"),
                        summary.get("averageHR") or act.get("averageHR"),
                        summary.get("maxHR") or act.get("maxHR"),
                        summary.get("averageRespirationValue"),
                        summary.get("maxRespirationValue"),
                        summary.get("calories") or act.get("calories"),
                        round(summary.get("averageRunCadence") / 2.0) if summary.get("averageRunCadence") else None,
                        round(summary.get("maxRunCadence") / 2.0) if summary.get("maxRunCadence") else None,
                        avg_speed,
                        max_speed,
                        summary.get("elevationGain"),
                        summary.get("elevationLoss"),
                        summary.get("maxTemperature"),
                        summary.get("minTemperature"),
                        summary.get("avgTemperature"),
                        summary.get("startLatitude"),
                        summary.get("startLongitude"),
                        summary.get("endLatitude"),
                        summary.get("endLongitude"),
                        "custom" if zones else None,
                        hrz_hr[1], hrz_hr[2], hrz_hr[3], hrz_hr[4], hrz_hr[5],
                        self._secs_to_time_str_precise(hrz_time[1]) or "00:00:00.000000",
                        self._secs_to_time_str_precise(hrz_time[2]) or "00:00:00.000000",
                        self._secs_to_time_str_precise(hrz_time[3]) or "00:00:00.000000",
                        self._secs_to_time_str_precise(hrz_time[4]) or "00:00:00.000000",
                        self._secs_to_time_str_precise(hrz_time[5]) or "00:00:00.000000"
                    )
                )
                
                spm = summary.get("averageRunCadence") or act.get("averageRunCadence")
                max_spm = summary.get("maxRunCadence") or act.get("maxRunCadence")
                stride_length_mm = None
                stride_len_cm = summary.get("strideLength")
                if stride_len_cm is not None:
                    stride_length_mm = stride_len_cm * 10.0
                vert_osc_mm = None
                vert_osc_cm = summary.get("verticalOscillation")
                if vert_osc_cm is not None:
                    vert_osc_mm = vert_osc_cm * 10.0
                gct_secs = None
                gct_ms = summary.get("groundContactTime")
                if gct_ms is not None:
                    gct_secs = gct_ms / 1000.0
                    
                duration = summary.get("duration") or 0.0
                moving_duration = summary.get("movingDuration") or duration
                dist_m = summary.get("distance") or 0.0
                dist_km = dist_m / 1000.0
                
                avg_pace_secs = (duration / dist_km) if dist_km > 0 else None
                avg_moving_pace_secs = (moving_duration / dist_km) if dist_km > 0 else None
                
                max_speed_m_s = summary.get("maxSpeed")
                max_pace_secs = (1000.0 / max_speed_m_s) if max_speed_m_s and max_speed_m_s > 0 else None
                
                vo2 = act.get("vO2MaxValue") or summary.get("vo2Max")
                
                sport_key = (act.get("activityType") or {}).get("typeKey") or (summary.get("activityTypeDTO") or {}).get("typeKey")
                has_steps = (summary.get("steps") or summary.get("cycles") or spm or max_spm)
                if sport_key in ("running", "walking", "hiking", "street_running", "track_running", "trail_running", "treadmill_running") or has_steps:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO steps_activities (
                            steps, avg_pace, avg_moving_pace, max_pace,
                            avg_steps_per_min, max_steps_per_min, avg_step_length,
                            avg_vertical_ratio, avg_vertical_oscillation, avg_gct_balance,
                            avg_ground_contact_time, avg_stance_time_percent, vo2_max, activity_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            summary.get("steps") or summary.get("cycles") or 0,
                            self._secs_to_pace_str(avg_pace_secs) or "00:00:00.000000",
                            self._secs_to_pace_str(avg_moving_pace_secs) or "00:00:00.000000",
                            self._secs_to_pace_str(max_pace_secs) or "00:00:00.000000",
                            round(spm) if spm else None,
                            round(max_spm) if max_spm else None,
                            stride_length_mm,
                            summary.get("verticalRatio"),
                            vert_osc_mm,
                            None,
                            self._secs_to_time_str_precise(gct_secs) or "00:00:00.000000",
                            None,
                            vo2,
                            str(activity_id)
                        )
                    )
                
                for i, lap in enumerate(lap_dtos):
                    lap_id = i + 1
                    lap_duration = lap.get("duration") or 0.0
                    lap_dist_m = lap.get("distance") or 0.0
                    lap_dist_km = lap_dist_m / 1000.0
                    
                    lap_avg_speed = None
                    lap_max_speed = None
                    lap_speed_m_s = lap.get("averageSpeed")
                    if lap_speed_m_s is not None:
                        lap_avg_speed = lap_speed_m_s * 3.6
                    lap_max_speed_m_s = lap.get("maxSpeed")
                    if lap_max_speed_m_s is not None:
                        lap_max_speed = lap_max_speed_m_s * 3.6
                        
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO activity_laps (
                            activity_id, lap, start_time, stop_time, elapsed_time, moving_time, distance,
                            cycles, avg_hr, max_hr, avg_rr, max_rr, calories, avg_cadence, max_cadence,
                            avg_speed, max_speed, ascent, descent, max_temperature, min_temperature, avg_temperature,
                            start_lat, start_long, stop_lat, stop_long, hr_zones_method,
                            hrz_1_hr, hrz_2_hr, hrz_3_hr, hrz_4_hr, hrz_5_hr,
                            hrz_1_time, hrz_2_time, hrz_3_time, hrz_4_time, hrz_5_time
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(activity_id), lap_id,
                            lap.get("startTimeGMT"),
                            lap.get("startTimeGMT"),
                            self._secs_to_time_str(lap.get("duration")) or "00:00:00",
                            self._secs_to_time_str(lap.get("movingDuration")) or "00:00:00",
                            lap_dist_km,
                            lap.get("steps") or lap.get("cycles"),
                            lap.get("averageHR"),
                            lap.get("maxHR"),
                            None, None,
                            lap.get("calories"),
                            round(lap.get("averageRunCadence") / 2.0) if lap.get("averageRunCadence") else None,
                            round(lap.get("maxRunCadence") / 2.0) if lap.get("maxRunCadence") else None,
                            lap_avg_speed,
                            lap_max_speed,
                            lap.get("elevationGain"),
                            lap.get("elevationLoss"),
                            lap.get("maxTemperature"),
                            lap.get("minTemperature"),
                            lap.get("avgTemperature"),
                            lap.get("startLatitude"),
                            lap.get("startLongitude"),
                            lap.get("endLatitude"),
                            lap.get("endLongitude"),
                            None, None, None, None, None, None, "00:00:00.000000", "00:00:00.000000", "00:00:00.000000", "00:00:00.000000", "00:00:00.000000"
                        )
                    )
                    
        # 5. Fetch weather and laps JSON files for recent activities to keep backward compatibility
        try:
            self._fetch_activity_laps(username, password, output_dir, user_id)
        except Exception as exc:
            logger.warning("Activity JSON files fetch failed (non-fatal): %s", exc)
            
        self._refresh_user_analytics_db(user_id=user_id, output_dir=output_dir)
        return BackupResult(output_dir=output_dir)

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
                    "WHERE sport = 'running' AND max_hr IS NOT NULL AND max_hr < 220"
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
                "FROM activities WHERE sport = 'running' AND distance IS NOT NULL "
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

    def _garmin_db_path(self, user_id: int, db_name: str) -> Path | None:
        path = self._workdir_root / str(user_id) / "DBs" / db_name
        return path if path.exists() else None

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

    def _garth_login(self, username: str, password: str, output_dir: Path):
        """Login via garth, reusing cached OAuth tokens to avoid SSO rate limits (429).

        Возвращает изолированный garth.Client (НЕ модульный синглтон): модульные
        garth.configure/resume/login мутируют глобальную сессию, и параллельные
        синки разных юзеров затирали бы друг другу авторизацию.
        """
        from garth import Client as GarthClient  # type: ignore
        import json as _json
        import time as _time

        token_dir = output_dir / ".garth_tokens"
        token_dir.mkdir(parents=True, exist_ok=True)
        client = GarthClient(domain="garmin.com")

        # Check token expiry from file — no network call to avoid triggering 429
        oauth2_file = token_dir / "oauth2_token.json"
        if oauth2_file.exists():
            try:
                token_data = _json.loads(oauth2_file.read_text())
                expires_at = token_data.get("expires_at", 0)
                # Keep a 5-minute buffer before expiry
                if expires_at > _time.time() + 300:
                    client.load(str(token_dir))
                    logger.debug("garth: reused cached session for %s (expires in %.0fs)", _mask_email(username), expires_at - _time.time())
                    return client
                else:
                    logger.info("garth: token expired or expiring soon, re-logging in")
            except Exception as exc:
                logger.info("garth: could not read cached token (%s), re-logging in", exc)

        # Full login and cache the new tokens
        client.login(username, password)
        client.dump(str(token_dir))
        logger.info("garth: logged in and saved tokens for %s", _mask_email(username))
        return client

    def _fetch_and_store_hrv(self, username: str, password: str, output_dir: Path) -> None:
        """Login via garth and fetch HRV data for last 7 days, save to JSON files."""
        from garth import HRVData  # type: ignore

        hrv_dir = output_dir / "HRV"
        hrv_dir.mkdir(parents=True, exist_ok=True)

        garth_client = self._garth_login(username, password, output_dir)

        today = date.today()
        for delta in range(7):
            day = today - timedelta(days=delta)
            out = hrv_dir / f"hrv_{day.isoformat()}.json"
            # Skip days that already have data (today always re-fetched since it may update)
            if delta > 0 and out.exists():
                continue
            try:
                hrv = HRVData.get(day, client=garth_client)
                if hrv is None:
                    continue
                s = hrv.hrv_summary
                payload = {
                    "date": day.isoformat(),
                    "last_night_avg": s.last_night_avg,
                    "weekly_avg": s.weekly_avg,
                    "last_night_5_min_high": s.last_night_5_min_high,
                    "status": s.status,
                    "feedback_phrase": s.feedback_phrase,
                    "baseline_low_upper": s.baseline.low_upper if s.baseline else None,
                    "baseline_balanced_low": s.baseline.balanced_low if s.baseline else None,
                    "baseline_balanced_upper": s.baseline.balanced_upper if s.baseline else None,
                }
                out.write_text(json.dumps(payload, ensure_ascii=False))
                logger.info("HRV saved for %s: avg=%s status=%s", day, payload["last_night_avg"], payload["status"])
            except Exception as exc:
                logger.debug("HRV fetch for %s failed: %s", day, exc)

        # Fetch VO2max, LTHR, age and weight from biometric profile (same garth session)
        try:
            info = garth_client.connectapi("/userprofile-service/userprofile/personal-information")
            bio = (info or {}).get("biometricProfile") or {}
            user_info = (info or {}).get("userInfo") or {}
            weight_g = bio.get("weight")
            fitness_profile = {
                "vo2_max": bio.get("vo2Max"),
                "lthr": bio.get("lactateThresholdHeartRate"),
                "hr_max": bio.get("maxHeartRate"),
                "age": user_info.get("age"),
                "height_cm": bio.get("height"),
                "weight_kg": round(weight_g / 1000, 1) if weight_g else None,
                "fetched_at": today.isoformat(),
            }
            fp_file = output_dir / "fitness_profile.json"
            fp_file.write_text(json.dumps(fitness_profile, ensure_ascii=False))
            logger.info(
                "Fitness profile saved: age=%s weight=%skg vo2max=%s lthr=%s",
                fitness_profile["age"], fitness_profile["weight_kg"],
                fitness_profile["vo2_max"], fitness_profile["lthr"],
            )
            # Append to VO2max history (one entry per day, no duplicates)
            if fitness_profile.get("vo2_max") is not None:
                history_file = output_dir / "vo2max_history.json"
                try:
                    history: list[dict] = json.loads(history_file.read_text()) if history_file.exists() else []
                except Exception:
                    history = []
                today_str = today.isoformat()
                history = [e for e in history if e.get("date") != today_str]
                history.append({"date": today_str, "vo2_max": fitness_profile["vo2_max"]})
                history.sort(key=lambda e: e["date"])
                history_file.write_text(json.dumps(history, ensure_ascii=False))
        except Exception as exc:
            logger.warning("Fitness profile fetch failed (non-fatal): %s", exc)

    def _fetch_activity_laps(self, username: str, password: str, output_dir: Path, user_id: int) -> None:
        """Fetch lap data from Garmin Connect API for recent activities and save as JSON."""
        garth_client = self._garth_login(username, password, output_dir)

        laps_dir = output_dir / "laps"
        laps_dir.mkdir(parents=True, exist_ok=True)

        # Find recent activity IDs from the DB (last 14 days)
        acts_db = self._garmin_db_path(user_id, "garmin_activities.db")
        if not acts_db:
            return
        cutoff = (date.today() - timedelta(days=14)).isoformat()
        with sqlite3.connect(acts_db, timeout=5) as conn:
            rows = conn.execute(
                "SELECT activity_id, name FROM activities WHERE DATE(start_time) >= ? ORDER BY start_time DESC",
                (cutoff,),
            ).fetchall()

        weather_dir = output_dir / "weather"
        weather_dir.mkdir(parents=True, exist_ok=True)

        for activity_id, name in rows:
            lap_file = laps_dir / f"{activity_id}.json"
            weather_file = weather_dir / f"{activity_id}.json"

            # Fetch laps if not cached
            if not lap_file.exists():
                try:
                    data = garth_client.connectapi(f"/activity-service/activity/{activity_id}/laps")
                    lap_dtos = (data or {}).get("lapDTOs") or []
                    laps = []
                    for i, lap in enumerate(lap_dtos):
                        dist_m = lap.get("distance") or 0
                        dur_s = lap.get("duration") or 0
                        avg_hr = lap.get("averageHR")
                        max_hr = lap.get("maxHR")
                        avg_speed = lap.get("averageSpeed")  # m/s
                        pace_str = None
                        if avg_speed and avg_speed > 0:
                            pace_min = 1000 / avg_speed / 60
                            pace_str = f"{int(pace_min)}:{int((pace_min % 1) * 60):02d}"
                        laps.append({
                            "lap": i + 1,
                            "distance_m": round(dist_m),
                            "duration_s": round(dur_s),
                            "avg_hr": avg_hr,
                            "max_hr": max_hr,
                            "pace": pace_str,
                        })
                    lap_file.write_text(json.dumps(laps, ensure_ascii=False))
                    logger.info("Laps saved for activity %s (%s): %d laps", activity_id, name, len(laps))
                except Exception as exc:
                    logger.debug("Laps fetch for activity %s failed: %s", activity_id, exc)

            # Fetch weather if not cached
            if not weather_file.exists():
                try:
                    detail = garth_client.connectapi(f"/activity-service/activity/{activity_id}")
                    weather = {}
                    for key in ("weatherStartCondition", "weatherEndCondition"):
                        cond = (detail or {}).get(key) or {}
                        if cond:
                            temp_c = cond.get("temperature")
                            apparent_c = cond.get("apparentTemperature")
                            humidity = cond.get("relativeHumidity")
                            wind_kph = cond.get("windSpeed")
                            desc = (cond.get("weatherTypePhrases") or [None])[0]
                            weather[key] = {
                                "temp_c": round(temp_c, 1) if temp_c is not None else None,
                                "apparent_c": round(apparent_c, 1) if apparent_c is not None else None,
                                "humidity_pct": humidity,
                                "wind_kph": round(wind_kph, 1) if wind_kph is not None else None,
                                "description": desc,
                            }
                    if weather:
                        weather_file.write_text(json.dumps(weather, ensure_ascii=False))
                        logger.info("Weather saved for activity %s: %s", activity_id, weather.get("weatherStartCondition"))
                except Exception as exc:
                    logger.debug("Weather fetch for activity %s failed: %s", activity_id, exc)

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
                # Compute per-km splits from activity_records
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

    def _analytics_db_path_for_user(self, user_id: int) -> Path:
        return self._workdir_root / str(user_id) / "CoachData" / "coach_metrics.db"

    def _refresh_user_analytics_db(self, user_id: int, output_dir: Path) -> None:
        dbs_dir = output_dir / "DBs"
        if not dbs_dir.exists():
            # Backward-compat: previous runs could create nested structure when base_dir was relative.
            dbs_dir = self._find_dbs_dir(output_dir)
        if not dbs_dir or not dbs_dir.exists():
            raise RuntimeError("Garmin DB files were not created after sync (no DBs dir found).")
        analytics_dir = output_dir / "CoachData"
        analytics_dir.mkdir(parents=True, exist_ok=True)
        analytics_db = analytics_dir / "coach_metrics.db"

        sleep_db = self._find_db_with_table(dbs_dir, "sleep")
        workouts_db = self._find_db_with_table(dbs_dir, "activities")
        if not sleep_db and not workouts_db:
            raise RuntimeError("No sleep/activities tables found in Garmin DBs after sync.")

        with sqlite3.connect(analytics_db, timeout=5) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_sync_at TEXT NOT NULL,
                    source_dbs TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coach_sleep (
                    day TEXT PRIMARY KEY,
                    start_time TEXT,
                    end_time TEXT,
                    total_sleep TEXT,
                    deep_sleep TEXT,
                    light_sleep TEXT,
                    rem_sleep TEXT,
                    awake TEXT,
                    score INTEGER,
                    qualifier TEXT,
                    avg_spo2 REAL,
                    avg_rr REAL,
                    avg_stress REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coach_workouts (
                    activity_id TEXT PRIMARY KEY,
                    start_time TEXT,
                    start_date TEXT,
                    stop_time TEXT,
                    name TEXT,
                    sport TEXT,
                    sub_sport TEXT,
                    distance REAL,
                    calories INTEGER,
                    avg_hr INTEGER,
                    max_hr INTEGER,
                    avg_speed REAL,
                    moving_time TEXT,
                    elapsed_time TEXT,
                    training_load REAL,
                    training_effect REAL,
                    anaerobic_training_effect REAL
                )
                """
            )
            conn.execute("DELETE FROM coach_sleep")
            conn.execute("DELETE FROM coach_workouts")

            if sleep_db:
                with sqlite3.connect(sleep_db, timeout=5) as src:
                    rows = src.execute(
                        """
                        SELECT
                            DATE(day) AS day,
                            start AS start_time,
                            "end" AS end_time,
                            total_sleep,
                            deep_sleep,
                            light_sleep,
                            rem_sleep,
                            awake,
                            score,
                            qualifier,
                            avg_spo2,
                            avg_rr,
                            avg_stress
                        FROM sleep
                        """
                    ).fetchall()
                conn.executemany(
                    """
                    INSERT INTO coach_sleep (
                        day, start_time, end_time, total_sleep, deep_sleep, light_sleep, rem_sleep,
                        awake, score, qualifier, avg_spo2, avg_rr, avg_stress
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

            if workouts_db:
                with sqlite3.connect(workouts_db, timeout=5) as src:
                    rows = src.execute(
                        """
                        SELECT
                            activity_id,
                            start_time,
                            DATE(start_time) AS start_date,
                            stop_time,
                            name,
                            sport,
                            sub_sport,
                            distance,
                            calories,
                            avg_hr,
                            max_hr,
                            avg_speed,
                            moving_time,
                            elapsed_time,
                            training_load,
                            training_effect,
                            anaerobic_training_effect
                        FROM activities
                        """
                    ).fetchall()
                conn.executemany(
                    """
                    INSERT INTO coach_workouts (
                        activity_id, start_time, start_date, stop_time, name, sport, sub_sport, distance,
                        calories, avg_hr, max_hr, avg_speed, moving_time, elapsed_time,
                        training_load, training_effect, anaerobic_training_effect
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

            source_files = sorted(p.name for p in dbs_dir.glob("*.db"))
            conn.execute("DELETE FROM sync_meta WHERE id = 1")
            conn.execute(
                "INSERT INTO sync_meta (id, last_sync_at, source_dbs) VALUES (1, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), json.dumps(source_files)),
            )
            conn.commit()

    def _find_db_with_table(self, dbs_dir: Path, table_name: str) -> Path | None:
        for db in sorted(dbs_dir.glob("*.db")):
            with sqlite3.connect(db, timeout=5) as conn:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                    (table_name,),
                ).fetchone()
            if row:
                return db
        return None

    def _find_dbs_dir(self, output_dir: Path) -> Path | None:
        # Search a few levels deep for a plausible DBs directory.
        # We keep it shallow to avoid scanning huge trees.
        candidates: list[Path] = []
        for depth in (1, 2, 3, 4, 5):
            for p in output_dir.glob("*/" * (depth - 1) + "DBs"):
                if p.is_dir():
                    candidates.append(p)
        for p in sorted(candidates, key=lambda x: len(str(x))):
            if any(p.glob("*.db")):
                return p
        return candidates[0] if candidates else None
