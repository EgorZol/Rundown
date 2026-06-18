from __future__ import annotations

import json as _json_mod
import logging
import urllib.request
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .analyst import HealthAnalyst
    from .garmin_service import GarminService

logger = logging.getLogger(__name__)

WEEK_TYPE_NAMES = {
    "recovery": "Разгрузочная",
    "base": "Базовая",
    "build": "Развивающая",
    "peak": "Пиковая",
    "taper": "Тейпер",
}

# Ключевые слова в notes, по которым гонка считается tune-up / B-расой
# (используется, если у юзера не отмечена is_priority ни на одной из ближайших гонок).
_TUNE_UP_NOTE_HINTS = (
    "бежать легк", "легко", "тест формы", "тренировочн", "tune-up", "tune up",
    "с женой", "по самочувств", "не разгоняться",
)


def _phase_windows(dist_km: float | None) -> tuple[int, int, int]:
    """Return (taper_max, peak_max, build_max) days for a race of given distance.

    Окна не пересекаются: 0..taper_max = тейпер, taper_max+1..peak_max = пик,
    peak_max+1..build_max = build. За пределами build_max работает обычная логика.
    """
    if dist_km is None:
        return 14, 28, 49
    d = float(dist_km)
    if d >= 80:
        return 21, 42, 84   # ультра 80К+
    if d >= 42:
        return 14, 28, 56   # марафон / 50К
    if d >= 21:
        return 10, 24, 49   # полумарафон
    if d >= 15:
        return 8, 21, 42
    if d >= 10:
        return 7, 21, 42    # 10К
    return 5, 14, 28        # 5К и короче


def _is_tune_up_by_notes(notes: str | None) -> bool:
    if not notes:
        return False
    low = notes.lower()
    return any(h in low for h in _TUNE_UP_NOTE_HINTS)


def _select_target_race(
    upcoming_races: list[dict],
    today: date,
    horizon_days: int = 56,
) -> tuple[dict | None, list[dict]]:
    """Pick the race we periodise for; return (target, other_races_until_target).

    Алгоритм:
    1. Если есть гонки с is_priority=True — берём ближайшую такую БЕЗ ограничения
       горизонтом (юзер сам пометил это A-расу).
    2. Иначе в горизонте берём ближайшую non-tune-up (фильтр по notes).
       Если в 14 днях после неё есть гонка ≥ её дистанции — пересаживаемся на ту
       (каскад: первая — tune-up для большей следующей).
    3. Если все ближайшие — tune-up по notes, берём ближайшую без фильтра.
    """
    horizon = today + timedelta(days=horizon_days)
    all_future: list[tuple[date, dict]] = []
    in_horizon: list[tuple[date, dict]] = []
    for r in upcoming_races:
        try:
            rd = date.fromisoformat(r["date"]) if r.get("date") else None
        except (TypeError, ValueError):
            rd = None
        if rd is None or rd < today:
            continue
        all_future.append((rd, r))
        if rd <= horizon:
            in_horizon.append((rd, r))
    if not all_future:
        return None, []
    all_future.sort(key=lambda x: x[0])
    in_horizon.sort(key=lambda x: x[0])

    # 1. is_priority — без ограничения горизонтом
    priority = [(d, r) for d, r in all_future if r.get("is_priority")]
    if priority:
        target_d, target = priority[0]
    elif not in_horizon:
        return None, []
    else:
        # 2. ближайшая non-tune-up
        non_tune = [(d, r) for d, r in in_horizon if not _is_tune_up_by_notes(r.get("notes"))]
        if non_tune:
            target_d, target = non_tune[0]
            # каскад: если в 14 дн после target есть равная или большая дистанция — пересаживаемся
            target_dist = target.get("distance_km") or 0
            for d, r in in_horizon:
                if d <= target_d:
                    continue
                if (d - target_d).days > 14:
                    break
                r_dist = r.get("distance_km") or 0
                if r_dist >= target_dist:
                    target_d, target = d, r
                    target_dist = r_dist
        else:
            # 3. все tune-up — берём ближайшую как есть
            target_d, target = in_horizon[0]

    others = [r for d, r in all_future if d < target_d]
    return target, others


def fetch_weather_forecast(lat: float, lon: float, days: int = 7) -> list[dict] | None:
    """Fetch daily weather forecast from Open-Meteo (free, no API key).

    Returns list of dicts: [{date, temp_max, temp_min, precipitation_mm, wind_max_kph, description}, ...]
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,"
        f"windspeed_10m_max,weathercode"
        f"&timezone=auto&forecast_days={days}"
    )
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = _json_mod.loads(resp.read())
        daily = data.get("daily") or {}
        dates = daily.get("time") or []
        if not dates:
            return None
        # WMO weather code descriptions (simplified)
        wmo = {
            0: "ясно", 1: "малооблачно", 2: "облачно", 3: "пасмурно",
            45: "туман", 48: "туман", 51: "морось", 53: "морось", 55: "морось",
            61: "дождь", 63: "дождь", 65: "сильный дождь",
            71: "снег", 73: "снег", 75: "сильный снег",
            80: "ливень", 81: "ливень", 82: "сильный ливень",
            85: "снегопад", 86: "снегопад", 95: "гроза", 96: "гроза с градом",
        }
        result = []
        for i, d in enumerate(dates):
            code = (daily.get("weathercode") or [None])[i]
            result.append({
                "date": d,
                "temp_max": (daily.get("temperature_2m_max") or [None])[i],
                "temp_min": (daily.get("temperature_2m_min") or [None])[i],
                "precipitation_mm": (daily.get("precipitation_sum") or [0])[i],
                "wind_max_kph": (daily.get("windspeed_10m_max") or [None])[i],
                "description": wmo.get(code, ""),
            })
        return result
    except Exception as exc:
        logger.warning("Weather forecast fetch failed: %s", exc)
        return None


class WeeklyPlanBuilder:
    def __init__(self, analyst: HealthAnalyst, service: GarminService) -> None:
        self._analyst = analyst
        self._service = service

    # ── Public ────────────────────────────────────────────────────────────────

    async def generate_plan(
        self,
        user_id: int,
        metrics: dict[str, Any],
        history: list[dict] | None = None,
        user_memory: str = "",
        training_goal: str = "",
        upcoming_races: list[dict] | None = None,
        feelings: list[dict] | None = None,
        previous_plan: str = "",
        past_races: list[dict] | None = None,
    ) -> tuple[str, str]:
        """Return (plan_text, week_type_key)."""
        activities = self._service.collect_recent_activities(user_id, days=14)
        activities_14d = metrics.get("activities_14d", [])

        week_type, reasoning, volume_factor = self.determine_week_type(
            metrics, activities_14d, upcoming_races or [], feelings=feelings,
            past_races=past_races or [],
        )
        paces = self.extract_real_paces(activities)
        # Enrich with VDOT-calculated paces from VO2max
        fp = metrics.get("fitness_profile") or {}
        vo2 = fp.get("vo2_max")
        if not vo2:
            vo2_hist = metrics.get("vo2max_history") or []
            if vo2_hist:
                vo2 = sorted(vo2_hist, key=lambda e: e["date"])[-1].get("vo2_max")
        if vo2:
            paces["vdot"] = self.calculate_vdot_paces(vo2)
            # Auto-correct VDOT if real easy pace diverges >15 sec/km
            if paces.get("easy") and paces["vdot"].get("easy"):
                adj = self._vdot_pace_adjustment(paces["easy"], paces["vdot"]["easy"], vo2)
                if adj:
                    paces["vdot"] = adj["corrected_paces"]
                    paces["vdot_note"] = adj["note"]
        context = self._build_context(metrics, activities_14d, week_type, reasoning, volume_factor, paces, training_goal, upcoming_races or [], previous_plan=previous_plan)

        plan_text = await self._analyst.analyze_plan(
            context, history=history, user_memory=user_memory,
            fitness_profile=metrics.get("fitness_profile"),
            garmin_zones=metrics.get("garmin_zones"),
        )
        return plan_text, week_type

    def determine_week_type(
        self,
        metrics: dict[str, Any],
        activities_14d: list[dict],
        upcoming_races: list[dict] | None = None,
        feelings: list[dict] | None = None,
        past_races: list[dict] | None = None,
    ) -> tuple[str, str, float]:
        """Return (type_key, reasoning, volume_factor).

        Safety-first: overtraining signals ALWAYS force recovery, even during race prep.
        """
        target_date_str = metrics.get("date", "")
        try:
            today = date.fromisoformat(target_date_str) if target_date_str else date.today()
        except ValueError:
            logger.warning("determine_week_type: invalid date '%s', using today", target_date_str)
            today = date.today()
        week_start = today - timedelta(days=6)
        prev_week_start = today - timedelta(days=13)

        cur_acts = [a for a in activities_14d if a.get("start_time", "") >= week_start.isoformat()]
        prev_acts = [
            a for a in activities_14d
            if prev_week_start.isoformat() <= a.get("start_time", "") < week_start.isoformat()
        ]

        cur_tl = sum(a.get("training_load") or 0 for a in cur_acts)
        prev_tl = sum(a.get("training_load") or 0 for a in prev_acts)
        cur_km = sum(a.get("distance") or 0 for a in cur_acts if a.get("sport") == "running")
        prev_km = sum(a.get("distance") or 0 for a in prev_acts if a.get("sport") == "running")

        daily_trend = metrics.get("daily_trend_7d", [])
        low_bb_days = sum(1 for d in daily_trend if (d.get("bb_max") or 100) < 50)
        hrv_status = (metrics.get("hrv") or {}).get("status", "")

        # ── HARD SAFETY RULES (highest priority — override everything including races) ──

        # 1. RHR spike: rise >5 bpm from 7-day baseline = overreaching
        rhr_data = metrics.get("resting_hr") or {}
        rhr_today = rhr_data.get("resting_heart_rate")
        rhr_baseline = None
        if daily_trend and len(daily_trend) >= 3:
            rhr_vals = sorted(d.get("rhr") for d in daily_trend if d.get("rhr"))
            if rhr_vals:
                rhr_baseline = rhr_vals[len(rhr_vals) // 2]  # median — resistant to outliers
        if rhr_today and rhr_baseline and rhr_today > rhr_baseline + 5:
            return "recovery", (
                f"ЧСС покоя {rhr_today} — на {rhr_today - rhr_baseline:.0f} уд/мин выше нормы "
                f"({rhr_baseline:.0f}) — принудительное восстановление"
            ), 0.6

        # 2. BB critically low for 3+ days
        if low_bb_days >= 3:
            return "recovery", f"BB ниже 50 в течение {low_bb_days} дней — принудительное восстановление", 0.6

        # 3. HRV UNBALANCED + low BB = overload
        bb_today = (metrics.get("daily_summary") or {}).get("bb_max", 100)
        if hrv_status == "UNBALANCED" and bb_today < 60:
            return "recovery", "HRV UNBALANCED + BB низкий — признак перегрузки", 0.6

        # 4. Breathing rate spike: >2 breaths/min above 7-day average = early illness/overload
        sleep_trend = metrics.get("sleep_trend_7d") or []
        rr_vals = [d.get("avg_rr") for d in sleep_trend if d.get("avg_rr")]
        if rr_vals and len(rr_vals) >= 3:
            rr_avg = sum(rr_vals) / len(rr_vals)
            rr_latest = rr_vals[-1]
            if rr_latest > rr_avg + 2:
                return "recovery", (
                    f"ЧД ночью {rr_latest:.1f} — на {rr_latest - rr_avg:.1f} вд/мин выше нормы "
                    f"({rr_avg:.1f}) — ранний маркер болезни/перегрузки"
                ), 0.6

        # 5. Subjective feelings ≤2 for 2+ consecutive days = forced rest
        if feelings and len(feelings) >= 2:
            recent_scores = [f["score"] for f in feelings[-3:]]
            consecutive_low = 0
            for s in reversed(recent_scores):
                if s <= 2:
                    consecutive_low += 1
                else:
                    break
            if consecutive_low >= 2:
                return "recovery", (
                    f"Самочувствие ≤2 уже {consecutive_low} дня подряд — "
                    "принудительное восстановление"
                ), 0.6

        # 6. Sleep deprivation: avg <6.5h over last 3+ days
        sleep_hours: list[float] = []
        for d in sleep_trend:
            ts = d.get("total_sleep")
            if ts:
                # total_sleep stored as "HH:MM:SS" or seconds
                try:
                    if isinstance(ts, str) and ":" in ts:
                        parts = ts.split(":")
                        h = float(parts[0]) + float(parts[1]) / 60
                    else:
                        h = float(ts) / 3600  # assume seconds
                    if h > 0:
                        sleep_hours.append(h)
                except (ValueError, IndexError):
                    pass
        if len(sleep_hours) >= 3:
            avg_sl = sum(sleep_hours[-3:]) / len(sleep_hours[-3:])
            if avg_sl < 6.5:
                return "recovery", (
                    f"Средний сон {avg_sl:.1f}ч за последние 3 ночи (<6.5ч) — "
                    "недовосстановление, принудительный отдых"
                ), 0.6

        # ── POST-RACE RECOVERY (1 day rest per 3km of race distance) ──
        if past_races:
            for race in past_races:
                race_date_str = race.get("date", "")
                race_dist = race.get("distance_km") or 0
                if not race_date_str or race_dist <= 0:
                    continue
                try:
                    race_d = date.fromisoformat(race_date_str)
                except ValueError:
                    continue
                recovery_days = max(3, round(race_dist / 3))  # min 3 days recovery
                days_since = (today - race_d).days
                if 0 < days_since <= recovery_days:
                    remaining = recovery_days - days_since
                    race_name = race.get("name", "старт")
                    # Progressive recovery: first half Z1-Z2 only, second half gradual Z3
                    halfway = recovery_days // 2
                    if days_since <= halfway:
                        phase_note = "фаза 1: только Z1-Z2 (лёгкий бег/ходьба), никаких интервалов"
                        vf = 0.4
                    else:
                        phase_note = "фаза 2: постепенный выход в Z3 (аэробный лёгкий бег), без Z4-Z5"
                        vf = 0.6
                    return "recovery", (
                        f"Восстановление после {race_name} ({race_dist:.0f} км, "
                        f"{days_since} дн. назад) — норма {recovery_days} дн. отдыха "
                        f"(осталось {remaining} дн.). {phase_note}"
                    ), vf

        # ── VO2max consecutive drop → forced recovery ──
        vo2_hist = metrics.get("vo2max_history") or []
        if len(vo2_hist) >= 4:
            vo2_sorted = sorted(vo2_hist, key=lambda e: e["date"])
            last4 = vo2_sorted[-4:]
            drops = sum(
                1 for i in range(1, len(last4))
                if (last4[i].get("vo2_max") or 0) < (last4[i - 1].get("vo2_max") or 0)
            )
            if drops >= 3:
                v_first = last4[0].get("vo2_max", "?")
                v_last = last4[-1].get("vo2_max", "?")
                return "recovery", (
                    f"VO2max падает 3+ замера подряд ({v_first} → {v_last}) — "
                    "признак перетренированности, принудительное восстановление"
                ), 0.6

        # 8. Running economy decline >5% over 4 weeks → recovery
        run_28d = [a for a in activities_14d if a.get("sport") == "running"]
        # Extend with activities from metrics if available (28d window)
        run_28d_full = [a for a in (metrics.get("activities_28d") or []) if a.get("sport") == "running"]
        if len(run_28d_full) > len(run_28d):
            run_28d = run_28d_full
        economy_vals: list[tuple[str, float]] = []
        for a in run_28d:
            avg_speed = a.get("avg_speed")
            avg_hr = a.get("avg_hr")
            if not avg_speed or avg_speed <= 0 or not avg_hr or avg_hr <= 0:
                continue
            zsecs = self._analyst._garmin_zone_secs(a)
            if zsecs:
                total_z = sum(zsecs)
                aero = zsecs[0] + zsecs[1] + zsecs[2]
                if total_z > 0 and aero / total_z < 0.75:
                    continue
            elif (a.get("distance") or 0) > 12:
                continue
            economy_vals.append((a.get("start_time", "")[:10], avg_speed / avg_hr * 1000))
        if len(economy_vals) >= 4:
            economy_vals.sort(key=lambda x: x[0])
            first_half = sum(e[1] for e in economy_vals[:len(economy_vals)//2]) / (len(economy_vals)//2)
            second_half = sum(e[1] for e in economy_vals[len(economy_vals)//2:]) / (len(economy_vals) - len(economy_vals)//2)
            econ_delta = (second_half - first_half) / first_half * 100 if first_half > 0 else 0
            if econ_delta < -5:
                return "recovery", (
                    f"Экономичность бега снижается ({econ_delta:+.1f}% за 4 нед.) — "
                    "признак накопленной усталости, принудительное восстановление"
                ), 0.6

        # ── Race calendar override — macrocycle periodization ──
        target_race, tune_ups = _select_target_race(upcoming_races or [], today)
        if target_race:
            try:
                race_date = date.fromisoformat(target_race["date"])
            except (ValueError, TypeError):
                race_date = None
        else:
            race_date = None
        if target_race and race_date:
            days_left = (race_date - today).days
            dist_km = target_race.get("distance_km")
            race_name = target_race.get("name", "старт")
            taper_max, peak_max, build_max = _phase_windows(dist_km)
            tune_str = ""
            if tune_ups:
                tu_names = ", ".join(
                    f"{r.get('name', '?')} {r.get('date', '')}" for r in tune_ups
                )
                tune_str = f" (по пути tune-up: {tu_names})"
            if 0 < days_left <= taper_max:
                vf = self._taper_volume_factor(dist_km, days_left)
                return "taper", (
                    f"Гонка через {days_left} дн. ({race_name}) — тейпер "
                    f"({vf:.0%} объёма){tune_str}"
                ), vf
            if taper_max < days_left <= peak_max:
                return "peak", (
                    f"Гонка через {days_left} дн. ({race_name}) — пик/специфика{tune_str}"
                ), 1.0
            if peak_max < days_left <= build_max:
                return "build", (
                    f"Гонка через {days_left} дн. ({race_name}) — развитие{tune_str}"
                ), 1.05
            # >build_max days: fall through to normal TSB/BB/HRV logic

        perf = metrics.get("fitness") or {}
        try:
            tsb = float(perf["tsb"]) if perf.get("tsb") is not None else None
            ctl = float(perf["ctl"]) if perf.get("ctl") is not None else None
            atl = float(perf["atl"]) if perf.get("atl") is not None else None
        except (ValueError, TypeError):
            tsb, ctl, atl = None, None, None

        # Recovery conditions (softer signals)
        if tsb is not None and tsb < -25:
            return "recovery", f"TSB {tsb:+.0f} — накопленная усталость (порог -25)", 0.6
        if prev_tl > 0 and cur_tl > prev_tl * 1.3:
            return "recovery", f"TL этой недели ({cur_tl:.0f}) на 30%+ выше прошлой ({prev_tl:.0f})", 0.6

        # ACWR check: 1.2-1.5 = warning (base week), >1.5 = forced recovery
        if atl is not None and ctl is not None and ctl > 0:
            acwr = atl / ctl
            if acwr > 1.5:
                return "recovery", f"ACWR {acwr:.2f} (>1.5) — острая перегрузка, нужна разгрузка", 0.6
            if acwr > 1.2:
                return "base", f"ACWR {acwr:.2f} (1.2-1.5) — повышенная нагрузка, без наращивания", 1.0

        # ── Edge case: no runs for >8 days ──
        run_acts = [a for a in activities_14d if a.get("sport") == "running"]
        if run_acts:
            last_run_date = max(a.get("start_time", "")[:10] for a in run_acts)
            days_since_run = (today - date.fromisoformat(last_run_date)).days
            if days_since_run > 8:
                return "base", (
                    f"Последний бег {days_since_run} дней назад — "
                    "мягкое возвращение к тренировкам (60-70% объёма)"
                ), 0.7
        elif activities_14d:
            # Has other activities but no runs at all in 14 days
            return "base", "Нет пробежек за 14 дней — мягкий старт", 0.7

        # Build conditions (steady progress, room to grow)
        if prev_km > 0 and cur_km <= prev_km * 1.05:
            # Don't build if TSB already negative
            if tsb is not None and tsb < -10:
                return "base", f"TSB {tsb:+.0f} — сначала восстановление, потом рост", 1.0
            # Progressive overload: 10% rule + absolute cap of 5km
            max_relative = prev_km * 0.10  # 10% rule
            max_absolute = 5.0  # hard cap 5 km/week
            safe_increase = min(max_relative, max_absolute)
            safe_factor = 1.0 + safe_increase / prev_km if prev_km > 0 else 1.08
            safe_factor = min(safe_factor, 1.10)  # never more than +10%
            return "build", (
                f"Объём стабильный ({cur_km:.0f} км vs {prev_km:.0f} км), "
                f"безопасный рост +{safe_increase:.0f} км (+{(safe_factor - 1) * 100:.0f}%)"
            ), safe_factor

        # Volume jump detection: current week >50% more than previous
        if prev_km > 0 and cur_km > prev_km * 1.5:
            return "base", (
                f"Резкий скачок объёма: {cur_km:.0f} км vs {prev_km:.0f} км (+{((cur_km/prev_km)-1)*100:.0f}%) — "
                "не наращивать дальше, риск травмы"
            ), 0.9

        return "base", "Стандартная базовая неделя", 1.0

    def extract_real_paces(self, activities: list[dict]) -> dict[str, str]:
        """Extract actual training paces from recent activities with km_splits.

        Easy pace: median of runs where >=80% HR-zone time is in Z1-Z3 (aerobic).
        Interval pace: median of top-5% fastest km splits (filters GPS artifacts).
        Tempo pace: top-third fastest runs 5-12 km.
        Long run pace: average of runs >12 km.
        """
        running = [a for a in activities if a.get("sport") == "running"]
        if not running:
            return {}

        paces: dict[str, str] = {}

        def _speed_to_pace(speed_kmh: float) -> str:
            p = 60.0 / speed_kmh
            return f"{int(p)}:{int((p % 1) * 60):02d}"

        # Easy pace: only runs where >=80% of zone time was Z1-Z3
        easy_speeds = []
        for a in running:
            if not a.get("avg_speed") or a["avg_speed"] <= 0:
                continue
            zsecs = self._analyst._garmin_zone_secs(a)
            if zsecs:
                total_z = sum(zsecs)
                aero = zsecs[0] + zsecs[1] + zsecs[2]
                if total_z > 0 and aero / total_z >= 0.80:
                    easy_speeds.append(a["avg_speed"])
            else:
                # No zone data — include if distance < 10 km (likely easy)
                if (a.get("distance") or 0) < 10:
                    easy_speeds.append(a["avg_speed"])
        if easy_speeds:
            easy_speeds.sort()
            paces["easy"] = _speed_to_pace(easy_speeds[len(easy_speeds) // 2])
        else:
            # Fallback: slowest third of all runs
            speeds = sorted(a["avg_speed"] for a in running if a.get("avg_speed") and a["avg_speed"] > 0)
            if speeds:
                slow_third = speeds[:max(1, len(speeds) // 3)]
                paces["easy"] = _speed_to_pace(slow_third[len(slow_third) // 2])

        # Long run pace: runs > 12 km
        long_runs = [a for a in running if (a.get("distance") or 0) > 12 and a.get("avg_speed")]
        if long_runs:
            avg_speed = sum(a["avg_speed"] for a in long_runs) / len(long_runs)
            paces["long"] = _speed_to_pace(avg_speed)

        # Interval pace: median of top 5% fastest km splits (not absolute min — filters GPS spikes)
        all_split_secs: list[int] = []
        for a in running:
            for split in a.get("km_splits", []):
                pace_str = split.get("pace", "")
                parts = pace_str.split(":")
                if len(parts) == 2:
                    try:
                        secs = int(parts[0]) * 60 + int(parts[1])
                        if 150 <= secs <= 600:  # 2:30-10:00/km — sane range
                            all_split_secs.append(secs)
                    except ValueError:
                        pass
        if all_split_secs:
            all_split_secs.sort()
            top5_count = max(1, len(all_split_secs) // 20)  # top 5%
            top5 = all_split_secs[:top5_count]
            median_fast = top5[len(top5) // 2]
            paces["interval"] = f"{median_fast // 60}:{median_fast % 60:02d}"

        # Tempo pace: between easy and interval (faster runs 5-12 km)
        tempo_runs = [
            a for a in running
            if 5 <= (a.get("distance") or 0) <= 12 and a.get("avg_speed")
        ]
        if tempo_runs:
            fast_runs = sorted(tempo_runs, key=lambda a: a["avg_speed"], reverse=True)
            top = fast_runs[: max(1, len(fast_runs) // 3)]
            avg_speed = sum(a["avg_speed"] for a in top) / len(top)
            paces["tempo"] = _speed_to_pace(avg_speed)

        return paces

    # ── Taper & periodization ────────────────────────────────────────────────

    @staticmethod
    def _taper_volume_factor(distance_km: float | None, days_left: int) -> float:
        """Volume multiplier based on race distance and days until race.

        Protocols (evidence-based):
        - 5K/10K: 4-5 day taper (Mujika, 2003)
        - Half marathon: 7-10 day taper, progressive
        - Marathon: 14-21 day taper (Pfitzinger), 75% → 50% → 30%
        """
        if not distance_km or distance_km <= 10:
            # Short taper: 4-5 days
            if days_left <= 1:
                return 0.30
            if days_left <= 3:
                return 0.50
            if days_left <= 5:
                return 0.70
            return 0.80
        if distance_km <= 21.1:
            # Medium taper: 7-10 days
            if days_left <= 2:
                return 0.30
            if days_left <= 5:
                return 0.45
            if days_left <= 7:
                return 0.55
            if days_left <= 10:
                return 0.65
            return 0.75
        # Marathon: 14-21 days (Pfitzinger)
        if days_left <= 3:
            return 0.25
        if days_left <= 7:
            return 0.30
        if days_left <= 14:
            return 0.50
        return 0.75

    @staticmethod
    def _taper_description(distance_km: float | None, days_left: int) -> str:
        if not distance_km or distance_km <= 10:
            return (
                "4-5 дней (Mujika): за 5 дн. 70%, за 3 дн. 50%, последний день 30%. "
                "Сохранить 1 короткое ускорение за 2-3 дня для нервно-мышечной остроты"
            )
        if distance_km <= 21.1:
            return (
                "7-10 дней: за 10 дн. 65%, за 7 дн. 55%, гоночная неделя 45%. "
                "Убрать длинный бег, 1 короткий темп (20 мин) за 5 дней до старта"
            )
        return (
            "14-21 день по Pfitzinger: за 3 нед. 75%, за 2 нед. 50%, гоночная неделя 30%. "
            "Последний длинный бег (16-20 км) за 16-18 дней до старта"
        )

    @staticmethod
    def _race_specificity_guidance(distance_km: float | None) -> str:
        """Distance-specific session prescriptions (Daniels methodology)."""
        if not distance_km:
            return ""
        if distance_km <= 5:
            return (
                "СПЕЦИФИКА ДИСТАНЦИИ (5 км):\n"
                "  Ключевые работы: VO2max интервалы (5×1000м в 95-100% vVO2max), "
                "фартлек 6-8×(2мин быстро / 2мин трусцой), повторы 200-400м для скорости\n"
                "  Длинный бег: 12-16 км, не нужны марафонские объёмы\n"
                "  Темповый бег: 3-5 км на пороговом темпе\n"
                "  Приоритет: скоростная выносливость > чистый объём"
            )
        if distance_km <= 10:
            return (
                "СПЕЦИФИКА ДИСТАНЦИИ (10 км):\n"
                "  Ключевые работы: пороговый бег 20-30 мин, "
                "VO2max интервалы 5-6×1000м, круиз-интервалы 3×10мин\n"
                "  Длинный бег: 15-20 км\n"
                "  Приоритет: лактатный порог + VO2max"
            )
        if distance_km <= 21.1:
            return (
                "СПЕЦИФИКА ДИСТАНЦИИ (полумарафон):\n"
                "  Ключевые работы: темповый бег 30-40 мин (88-92% LTHR), "
                "прогрессивный длинный бег (последние 5 км в целевом темпе), "
                "круиз-интервалы 4×10мин\n"
                "  Длинный бег: 18-24 км\n"
                "  Приоритет: выносливость на пороге + экономичность бега"
            )
        return (
            "СПЕЦИФИКА ДИСТАНЦИИ (марафон):\n"
            "  Ключевые работы: длинный бег 28-35 км (финальные 10-15 км в марафонском темпе), "
            "темповый бег 40-60 мин, MP-интервалы 3-4×5 км\n"
            "  Длинный бег: 28-35 км (ключевая тренировка недели)\n"
            "  Приоритет: аэробная выносливость + экономичность + работа на утомлении"
        )

    @staticmethod
    def calculate_vdot_paces(vo2max: float) -> dict[str, str]:
        """Calculate Daniels training paces from VO2max.

        Uses Daniels/Gilbert regression:
        vVO2max (m/min) ≈ 29.54 + 5.000663 * VO2max - 0.007546 * VO2max²
        """
        v = 29.54 + 5.000663 * vo2max - 0.007546 * vo2max ** 2
        v_kmh = v * 60 / 1000

        def _pace(frac: float) -> str:
            speed = v_kmh * frac
            if speed <= 0:
                return "?"
            p = 60.0 / speed
            return f"{int(p)}:{int((p % 1) * 60):02d}"

        return {
            "easy": f"{_pace(0.65)}–{_pace(0.74)}",
            "marathon": f"{_pace(0.78)}–{_pace(0.82)}",
            "tempo": f"{_pace(0.83)}–{_pace(0.88)}",
            "interval": f"{_pace(0.95)}–{_pace(1.0)}",
            "repetition": f"{_pace(1.05)}–{_pace(1.15)}",
        }

    @staticmethod
    def _vdot_pace_adjustment(real_easy: str, vdot_easy: str, vo2max: float) -> dict | None:
        """If real easy pace is >15 sec/km slower than VDOT easy, downscale all paces.

        Returns corrected paces dict + note, or None if no adjustment needed.
        """
        def _pace_to_secs(p: str) -> float:
            """Convert 'M:SS' or 'M:SS–M:SS' to seconds (use slower end of range)."""
            part = p.split("–")[-1].strip()  # take slower (right) end of range
            parts = part.split(":")
            return float(parts[0]) * 60 + float(parts[1])

        try:
            real_secs = _pace_to_secs(real_easy)
            vdot_secs = _pace_to_secs(vdot_easy)
        except (ValueError, IndexError):
            return None

        gap = real_secs - vdot_secs  # positive = real is slower
        if gap <= 15:
            return None  # within tolerance

        # Scale factor: how much slower the athlete actually is
        scale = real_secs / vdot_secs  # e.g., 1.05 if 5% slower
        corrected = WeeklyPlanBuilder.calculate_vdot_paces(vo2max)

        # Apply scale to all VDOT paces
        v = 29.54 + 5.000663 * vo2max - 0.007546 * vo2max ** 2
        v_kmh = v * 60 / 1000

        def _pace_scaled(frac: float) -> str:
            speed = v_kmh * frac / scale
            if speed <= 0:
                return "?"
            p = 60.0 / speed
            return f"{int(p)}:{int((p % 1) * 60):02d}"

        corrected = {
            "easy": f"{_pace_scaled(0.65)}–{_pace_scaled(0.74)}",
            "marathon": f"{_pace_scaled(0.78)}–{_pace_scaled(0.82)}",
            "tempo": f"{_pace_scaled(0.83)}–{_pace_scaled(0.88)}",
            "interval": f"{_pace_scaled(0.95)}–{_pace_scaled(1.0)}",
            "repetition": f"{_pace_scaled(1.05)}–{_pace_scaled(1.15)}",
        }
        return {
            "corrected_paces": corrected,
            "note": (
                f"⚠️ Реальный лёгкий темп ({real_easy}) медленнее VDOT на {gap:.0f} сек/км — "
                f"все расчётные темпы скорректированы вниз (×{scale:.2f}). "
                f"Garmin может завышать VO2max."
            ),
        }

    @staticmethod
    def predict_race_times(vo2max: float) -> dict[str, str]:
        """Predict race finish times from VO2max using full Daniels/Gilbert model.

        Uses the iterative approach:
        1. O2 cost of running: VO2 = -4.60 + 0.182258*v + 0.000104*v^2
        2. %VO2max sustainable: f(t) = 0.8 + 0.1894393*e^(-0.012778*t) + 0.2989558*e^(-0.1932605*t)
        3. Iterate: guess time → %VO2max → race VO2 → race velocity → new time
        """
        import math

        def _v_from_vo2(vo2: float) -> float:
            """Solve quadratic O2-cost equation for velocity (m/min)."""
            a, b = 0.000104, 0.182258
            c = -4.60 - vo2
            disc = b * b - 4 * a * c
            return (-b + math.sqrt(disc)) / (2 * a) if disc >= 0 else 0.0

        def _pct_vo2max(t_min: float) -> float:
            """Fraction of VO2max sustainable for t minutes (Daniels/Gilbert)."""
            return (0.8
                    + 0.1894393 * math.exp(-0.012778 * t_min)
                    + 0.2989558 * math.exp(-0.1932605 * t_min))

        def _fmt(total_min: float) -> str:
            if total_min < 60:
                m = int(total_min)
                s = int((total_min - m) * 60)
                return f"{m}:{s:02d}"
            h = int(total_min // 60)
            m = int(total_min % 60)
            s = int((total_min - int(total_min)) * 60)
            return f"{h}:{m:02d}:{s:02d}"

        vmax = _v_from_vo2(vo2max)
        if vmax <= 0:
            return {}

        distances = {
            "1 км": 1000,
            "5 км": 5000,
            "10 км": 10000,
            "Полумарафон": 21097.5,
            "Марафон": 42195,
        }

        result = {}
        for name, dist_m in distances.items():
            # Initial guess: time at vVO2max
            t = dist_m / vmax
            # Iterate to convergence
            for _ in range(15):
                pct = _pct_vo2max(t)
                race_vo2 = vo2max * pct
                race_v = _v_from_vo2(race_vo2)
                if race_v <= 0:
                    break
                t_new = dist_m / race_v
                if abs(t_new - t) < 0.01:
                    break
                t = t_new
            result[name] = _fmt(t)
        return result

    @staticmethod
    def compute_weekly_km_target(
        races: list[dict] | None,
        avg_weekly_km: float,
    ) -> tuple[float, str]:
        """Compute target weekly km for current week based on race schedule and periodization.

        Returns (target_km, phase_label) e.g. (42, "подводка к марафону 26 апр, 5 нед").
        Periodization model:
          >8 weeks: build (+5% per week, up to peak)
          5-8 weeks: peak volume
          3-4 weeks: soft taper (85%)
          2 weeks: taper (70%)
          1 week: hard taper (50%)
          race week: easy (30%)
        Peak volume depends on primary race distance:
          5k: 35-45 km, 10k: 40-50 km, HM: 50-60 km, marathon: 55-70 km
        """
        today = date.today()

        def _parse_dist(s: str) -> float:
            if not s:
                return 0.0
            s = s.lower().replace(",", ".").strip()
            if "марафон" in s or "marathon" in s or "42" in s:
                return 42.2
            if "полумарафон" in s or "half" in s or "21" in s:
                return 21.1
            if "10" in s:
                return 10.0
            if "5" in s:
                return 5.0
            try:
                import re
                m = re.search(r"[\d.]+", s)
                return float(m.group()) if m else 0.0
            except Exception:
                return 0.0

        upcoming = []
        for r in (races or []):
            try:
                rd = date.fromisoformat(r["date"])
                if rd >= today:
                    dist = _parse_dist(r.get("distance", ""))
                    upcoming.append({
                        "date": rd,
                        "days": (rd - today).days,
                        "dist_km": dist,
                        "name": r.get("name", "старт"),
                    })
            except Exception:
                pass

        if not upcoming:
            # No races — return current avg + 5% as a soft growth target
            return round(max(avg_weekly_km * 1.05, 30)), "набор базы (нет стартов)"

        # Primary race: longest distance; if tie — soonest
        primary = max(upcoming, key=lambda r: (r["dist_km"], -r["days"]))
        weeks = primary["days"] / 7
        dist = primary["dist_km"]
        name_short = primary["name"]
        date_str = primary["date"].strftime("%d.%m")

        # Peak volume for this race distance, anchored to current avg
        if dist >= 40:
            peak_km = max(avg_weekly_km, 55.0)
            peak_km = min(peak_km * 1.05, 75.0)
        elif dist >= 20:
            peak_km = max(avg_weekly_km, 45.0)
            peak_km = min(peak_km * 1.05, 65.0)
        elif dist >= 10:
            peak_km = max(avg_weekly_km, 40.0)
            peak_km = min(peak_km * 1.05, 55.0)
        else:
            peak_km = max(avg_weekly_km, 30.0)
            peak_km = min(peak_km * 1.05, 45.0)

        # Periodization factors
        if weeks <= 0.5:
            factor, phase = 0.25, "гоночная неделя"
        elif weeks <= 1:
            factor, phase = 0.35, "острая подводка"
        elif weeks <= 2:
            factor, phase = 0.55, "подводка"
        elif weeks <= 3:
            factor, phase = 0.70, "мягкая подводка"
        elif weeks <= 4:
            factor, phase = 0.85, "преподводочная"
        elif weeks <= 5:
            # Pre-peak deload for supercompensation
            factor, phase = 0.90, "предпиковая разгрузка"
        elif weeks <= 8:
            factor, phase = 1.0, "пиковый блок"
        else:
            # Build: ramp from 0.80 toward 1.0 as weeks approach peak (week 8)
            # weeks=9 → 0.97, weeks=12 → 0.88, weeks=16 → 0.80
            build_factor = max(0.80, 1.0 - (weeks - 8) * 0.025)
            factor, phase = build_factor, "набор объёма"

        target = round(peak_km * factor)
        label = f"{phase} к {name_short} ({date_str}, {primary['days']}д)"
        return target, label

    # ── Private ───────────────────────────────────────────────────────────────

    def _build_context(
        self,
        metrics: dict[str, Any],
        activities_14d: list[dict],
        week_type: str,
        reasoning: str,
        volume_factor: float,
        paces: dict[str, str],
        training_goal: str = "",
        upcoming_races: list[dict] | None = None,
        previous_plan: str = "",
    ) -> str:
        today = date.fromisoformat(metrics["date"]) if metrics.get("date") else date.today()
        week_start = today - timedelta(days=today.weekday())  # Monday

        parts = [f"=== ПЛАН НА НЕДЕЛЮ с {week_start.strftime('%d.%m')} ===\n"]
        if training_goal:
            parts.append(f"ГЛАВНАЯ ЦЕЛЬ АТЛЕТА: {training_goal}")

        # Race calendar — drives periodization phase
        if upcoming_races:
            race_lines = ["ПРЕДСТОЯЩИЕ СТАРТЫ (определяют фазу периодизации):"]
            for r in upcoming_races:
                race_date = date.fromisoformat(r["date"])
                days_left = (race_date - today).days
                weeks_left = days_left // 7
                dist = f" {r['distance_km']:.1f}км" if r.get("distance_km") else ""
                goal_t = f", цель {r['goal_time']}" if r.get("goal_time") else ""
                # Determine phase
                if days_left <= 7:
                    phase = "ГОНКА на этой неделе — тейпер/отдых"
                elif days_left <= 14:
                    phase = "тейпер — снизить объём до 60%, убрать интенсивность"
                elif days_left <= 28:
                    phase = "пик/специфика — специфичные для дистанции работы"
                elif days_left <= 56:
                    phase = "развитие — строить объём и качество"
                else:
                    phase = "базовая подготовка — аэробная база"
                race_lines.append(
                    f"  {r['date']} — {r['name']}{dist}{goal_t} "
                    f"[{weeks_left} нед. до старта | фаза: {phase}]"
                )
            parts.append("\n".join(race_lines))
            # Race-specific training guidance + taper protocol
            nearest = min(upcoming_races, key=lambda r: r["date"])
            nearest_date = date.fromisoformat(nearest["date"])
            nearest_days = (nearest_date - today).days
            dist_km = nearest.get("distance_km")
            specificity = self._race_specificity_guidance(dist_km)
            if specificity:
                parts.append(specificity)
            if nearest_days <= 21 and dist_km:
                parts.append(f"ПРОТОКОЛ ТЕЙПЕРА: {self._taper_description(dist_km, nearest_days)}")
        parts.append(f"Тип недели: {WEEK_TYPE_NAMES.get(week_type, week_type)}")
        parts.append(f"Обоснование: {reasoning}")
        parts.append(f"Поправочный коэффициент объёма: {volume_factor} (1.0 = базовый)")
        if week_type == "recovery":
            parts.append("[СИГНАЛ_ПЕРЕГРУЗКИ] — ЖЁСТКОЕ ПРАВИЛО: только Z1-Z3, никаких интервалов, объём 60%")

        # Dynamic weekly km target from race schedule
        dyn_target = metrics.get("weekly_km_target")
        dyn_label = metrics.get("weekly_km_target_label", "")
        if dyn_target:
            parts.append(f"ЦЕЛЕВОЙ ОБЪЁМ ТЕКУЩЕЙ НЕДЕЛИ: {dyn_target:.0f} км ({dyn_label})")

        # Current state
        ds = metrics.get("daily_summary") or {}
        rhr = metrics.get("resting_hr") or {}
        hrv = metrics.get("hrv") or {}
        parts.append(f"\nТЕКУЩЕЕ СОСТОЯНИЕ ({metrics.get('date', '?')}):")
        if ds.get("bb_max") is not None:
            parts.append(f"  BB уровень: {ds['bb_max']}/100")
        if hrv.get("last_night_avg"):
            bl = hrv.get("baseline_balanced_low", "?")
            bu = hrv.get("baseline_balanced_upper", "?")
            parts.append(f"  HRV: {hrv['last_night_avg']} мс (база {bl}–{bu}, статус {hrv.get('status', '?')})")
        if rhr.get("resting_heart_rate"):
            parts.append(f"  ЧСС покоя: {rhr['resting_heart_rate']}")
        # Sleep context — sub-optimal sleep affects plan even if it doesn't trigger hard recovery
        sleep = metrics.get("sleep_last_night") or metrics.get("sleep") or {}
        sleep_trend = metrics.get("sleep_trend_7d") or []
        if sleep.get("score") is not None:
            parts.append(f"  Сон: score {sleep['score']}")
        if len(sleep_trend) >= 3:
            recent_scores = [s.get("score") for s in sleep_trend[-3:] if s.get("score")]
            if recent_scores:
                avg_score = sum(recent_scores) / len(recent_scores)
                low_flag = " ⚠️ снижено качество сна" if avg_score < 70 else ""
                parts.append(f"  Сон avg 3 ночи: score {avg_score:.0f}{low_flag}")

        # Real paces
        real_labels = {"easy": "Лёгкий", "tempo": "Темповый", "interval": "Интервальный", "long": "Длинный бег"}
        real_paces = {k: v for k, v in paces.items() if k in real_labels}
        if real_paces:
            parts.append("\nРЕАЛЬНЫЕ ТЕМПЫ (из последних тренировок):")
            for key, label in real_labels.items():
                if key in real_paces:
                    parts.append(f"  {label}: {real_paces[key]}/км")
        # VDOT-calculated paces (from VO2max, Daniels methodology)
        vdot = paces.get("vdot")
        if vdot:
            parts.append("РАСЧЁТНЫЕ ТЕМПЫ (Daniels VDOT по VO2max):")
            vdot_labels = {
                "easy": "Лёгкий", "marathon": "Марафонский",
                "tempo": "Пороговый", "interval": "VO2max интервалы",
                "repetition": "Повторный",
            }
            for key, label in vdot_labels.items():
                if key in vdot:
                    parts.append(f"  {label}: {vdot[key]}/км")
            vdot_note = paces.get("vdot_note")
            if vdot_note:
                parts.append(f"  {vdot_note}")
            elif real_paces.get("easy"):
                parts.append("  (Реальный темп соответствует VDOT — расчётные темпы актуальны)")

        # Last 2 weeks load — use calendar week (Mon–today), not rolling 7 days
        week_start_iso = week_start.isoformat()  # Monday of current week
        prev_week_start = week_start - timedelta(days=7)
        prev_start_iso = prev_week_start.isoformat()
        cur_acts = [a for a in activities_14d if a.get("start_time", "") >= week_start_iso]
        prev_acts = [
            a for a in activities_14d
            if prev_start_iso <= a.get("start_time", "") < week_start_iso
        ]

        def _acts_summary(acts: list[dict]) -> str:
            run = [a for a in acts if a.get("sport") == "running"]
            km = sum(a.get("distance") or 0 for a in run)
            tl = sum(a.get("training_load") or 0 for a in acts)
            return f"{len(acts)} тренировок, бег {km:.0f} км, TL {tl:.0f}"

        parts.append(f"\nНАГРУЗКА ПОСЛЕДНИХ 2 НЕДЕЛЬ:")
        parts.append(f"  Текущие 7 дней: {_acts_summary(cur_acts)}")
        parts.append(f"  Предыдущие 7 дней: {_acts_summary(prev_acts)}")

        # Average weekly volume over last 4 weeks (from activities_28d)
        all_28d = metrics.get("activities_28d") or activities_14d
        run_28d = [a for a in all_28d if a.get("sport") == "running"]
        if run_28d:
            # Group runs by calendar week (ISO Monday-based)
            weekly_km: dict[str, float] = {}
            weekly_sessions: dict[str, int] = {}
            for a in run_28d:
                st = a.get("start_time", "")[:10]
                if not st:
                    continue
                d = date.fromisoformat(st)
                wk = (d - timedelta(days=d.weekday())).isoformat()
                weekly_km[wk] = weekly_km.get(wk, 0.0) + (a.get("distance") or 0)
                weekly_sessions[wk] = weekly_sessions.get(wk, 0) + 1
            if weekly_km:
                completed_weeks = [w for w in weekly_km if w < week_start.isoformat()]
                if completed_weeks:
                    avg_km = sum(weekly_km[w] for w in completed_weeks) / len(completed_weeks)
                    avg_sess = sum(weekly_sessions[w] for w in completed_weeks) / len(completed_weeks)
                    max_long = max((a.get("distance") or 0) for a in run_28d)
                    parts.append(
                        f"  Средний объём (завершённые недели): {avg_km:.0f} км/нед, "
                        f"{avg_sess:.1f} беговых сессий/нед, макс. длинный бег {max_long:.1f} км"
                    )

        # Hard-day clustering detection — warn if 2+ hard days back-to-back in recent data
        hard_dates = []
        for a in sorted(cur_acts + prev_acts, key=lambda x: x.get("start_time", "")):
            tl = a.get("training_load") or 0
            if tl >= 80:
                hard_dates.append(a.get("start_time", "")[:10])
        if len(hard_dates) >= 2:
            consecutive = 0
            for i in range(1, len(hard_dates)):
                try:
                    d1 = date.fromisoformat(hard_dates[i - 1])
                    d2 = date.fromisoformat(hard_dates[i])
                    if (d2 - d1).days == 1:
                        consecutive += 1
                except ValueError:
                    pass
            if consecutive > 0:
                parts.append(f"  ⚠️ КЛАСТЕР: {consecutive} пар(а) тяжёлых дней подряд (TL≥80) — избегать в новом плане!")

        # Available training days — prefer profile setting, fallback to auto-detected
        import json as _json
        day_names_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        fp = metrics.get("fitness_profile") or {}
        avail_days_raw = fp.get("available_days")
        if avail_days_raw:
            try:
                avail_days = _json.loads(avail_days_raw) if isinstance(avail_days_raw, str) else avail_days_raw
                parts.append(f"  ⚡ Доступные дни бега (из профиля): {', '.join(day_names_ru[d] for d in sorted(avail_days))} — ставь бег ТОЛЬКО на эти дни")
            except Exception:
                avail_days = None
        else:
            avail_days = None
        if not avail_days:
            all_28d = metrics.get("activities_28d") or activities_14d
            run_28d_for_days = [a for a in all_28d if a.get("sport") == "running" and a.get("start_time")]
            if run_28d_for_days:
                day_counts: dict[int, int] = {}
                for a in run_28d_for_days:
                    st = a.get("start_time", "")[:10]
                    if st:
                        d = date.fromisoformat(st)
                        day_counts[d.weekday()] = day_counts.get(d.weekday(), 0) + 1
                num_weeks = max(1, len(set(
                    (date.fromisoformat(a["start_time"][:10]) - timedelta(days=date.fromisoformat(a["start_time"][:10]).weekday())).isoformat()
                    for a in run_28d_for_days
                )))
                typical_days = [wd for wd, cnt in sorted(day_counts.items()) if cnt / num_weeks >= 0.5]
                if typical_days:
                    parts.append(f"  Обычные дни бега (авто): {', '.join(day_names_ru[d] for d in typical_days)}")

        # 80/20 balance of current week — Garmin native zones (Z1-Z3 aerobic, Z4-Z5 intensity)
        run_cur = [a for a in cur_acts if a.get("sport") == "running"]
        if run_cur:
            aero_secs = 0.0
            total_run_secs = 0.0
            for a in run_cur:
                zsecs = self._analyst._garmin_zone_secs(a)
                if zsecs:
                    aero_secs += zsecs[0] + zsecs[1] + zsecs[2]  # Z1+Z2+Z3
                total_run_secs += self._analyst._time_str_to_secs(a.get("moving_time"))
            if total_run_secs > 0:
                aero_pct = round(aero_secs / total_run_secs * 100)
                intens_pct = 100 - aero_pct
                balance_flag = "\u2713 \u0432 \u043d\u043e\u0440\u043c\u0435" if aero_pct >= 80 else "\u26a0\ufe0f \u043c\u0430\u043b\u043e \u0430\u044d\u0440\u043e\u0431\u043d\u043e\u0439 \u0440\u0430\u0431\u043e\u0442\u044b (<80%)"
                parts.append(
                    f"  80/20 \u0442\u0435\u043a\u0443\u0449\u0435\u0439 \u043d\u0435\u0434\u0435\u043b\u0438 (Garmin Z1\u20133 / Z4\u20135): {aero_pct}% / {intens_pct}% \u2014 {balance_flag}"
                )

        # Session-based 80/20 (Seiler's model) — Garmin native zones
        if len(run_cur) >= 2:
            easy_sessions = 0
            hard_sessions = 0
            for a in run_cur:
                zsecs = self._analyst._garmin_zone_secs(a)
                if zsecs:
                    aero = zsecs[0] + zsecs[1] + zsecs[2]
                    total_z = sum(zsecs)
                    hard = total_z > 0 and (total_z - aero) / total_z > 0.20
                else:
                    z123 = sum(
                        self._analyst._time_str_to_secs(a.get(f"hrz_{i}_time"))
                        for i in range(1, 4)
                    )
                    total = self._analyst._time_str_to_secs(a.get("moving_time"))
                    hard = total > 0 and (total - z123) / total > 0.20
                if hard:
                    hard_sessions += 1
                else:
                    easy_sessions += 1
            total_s = easy_sessions + hard_sessions
            easy_pct = round(easy_sessions / total_s * 100)
            sflag = "\u2713 \u0432 \u043d\u043e\u0440\u043c\u0435" if easy_pct >= 80 else "\u26a0\ufe0f \u0441\u043b\u0438\u0448\u043a\u043e\u043c \u043c\u043d\u043e\u0433\u043e \u0438\u043d\u0442\u0435\u043d\u0441\u0438\u0432\u043d\u044b\u0445 (<80% \u043b\u0451\u0433\u043a\u0438\u0445)"
            parts.append(
                f"  80/20 \u043f\u043e \u0441\u0435\u0441\u0441\u0438\u044f\u043c (Seiler, Garmin): {easy_sessions} \u043b\u0451\u0433\u043a\u0438\u0445 / {hard_sessions} \u0438\u043d\u0442\u0435\u043d\u0441\u0438\u0432\u043d\u044b\u0445 "
                f"({easy_pct}% \u043b\u0451\u0433\u043a\u0438\u0445) \u2014 {sflag}"
            )

        # Cross-training summary (current + previous week) — non-running load context
        from collections import Counter
        for label, acts_pool in [("текущей недели", cur_acts), ("прошлой недели", prev_acts)]:
            cross = [a for a in acts_pool if a.get("sport") and a.get("sport") != "running"]
            if cross:
                sport_counts = Counter(a.get("sport") for a in cross)
                sport_tl: dict[str, float] = {}
                for a in cross:
                    s = a.get("sport")
                    sport_tl[s] = sport_tl.get(s, 0) + (a.get("training_load") or 0)
                cross_parts = []
                for s, cnt in sport_counts.most_common():
                    tl = sport_tl.get(s, 0)
                    cross_parts.append(f"{s} {cnt}x (TL {tl:.0f})")
                parts.append(f"  Cross-training {label}: {', '.join(cross_parts)}")

        # Long-term weekly summary (last 26 weeks) — context for periodisation
        weeks = metrics.get("weeks_summary") or []
        if weeks:
            parts.append(f"\nТРЕНД 6 МЕСЯЦЕВ (понедельно, от старых к новым):")
            parts.append("  Неделя       | ЧСС покоя | Сон ч:мм | Стресс | BB макс")
            for w in weeks[-16:]:  # last 16 weeks in plan context
                def _hhm(val: Any) -> str:
                    if not val:
                        return "?"
                    s = str(val).split(".")[0].split(":")
                    try:
                        h, m = int(s[0]), int(s[1])
                        return f"{h}:{m:02d}"
                    except Exception:
                        return "?"
                rhr = f"{w['rhr_avg']:.0f}" if w.get("rhr_avg") else "?"
                sleep = _hhm(w.get("sleep_avg"))
                stress = f"{w['stress_avg']:.0f}" if w.get("stress_avg") else "?"
                bb = f"{w['bb_max']:.0f}" if w.get("bb_max") else "?"
                parts.append(f"  {w['first_day']}  | {rhr:>9} | {sleep:>8} | {stress:>6} | {bb:>7}")

        # VO2max trend for plan
        vo2_hist = metrics.get("vo2max_history") or []
        if len(vo2_hist) >= 3:
            vo2_hist_s = sorted(vo2_hist, key=lambda e: e["date"])
            recent_vo2 = vo2_hist_s[-6:]
            pts = ", ".join(f"{e['date']}: {e['vo2_max']}" for e in recent_vo2)
            first, last = vo2_hist_s[0], vo2_hist_s[-1]
            delta = round(last["vo2_max"] - first["vo2_max"], 1)
            parts.append(f"\nVO2max тренд: {first['vo2_max']} → {last['vo2_max']} ({'+' if delta >= 0 else ''}{delta}) | последние: {pts}")

        # ── Long run progression ──
        all_28d = metrics.get("activities_28d") or activities_14d
        run_28d = [a for a in all_28d if a.get("sport") == "running"]
        if run_28d:
            long_runs_sorted = sorted(
                [(a.get("distance") or 0, a.get("start_time", "")[:10]) for a in run_28d],
                key=lambda x: x[0], reverse=True,
            )
            max_long = long_runs_sorted[0][0] if long_runs_sorted else 0
            max_long_date = long_runs_sorted[0][1] if long_runs_sorted else "?"
            # Suggested next long run: +1-2 km from current max, capped at +2km
            next_long = round(min(max_long + 2, max_long * 1.1), 1) if max_long > 0 else 10
            if max_long > 0:
                parts.append(
                    f"\nДЛИННЫЙ БЕГ: макс. за 4 нед = {max_long:.1f} км ({max_long_date}), "
                    f"рекомендация на эту неделю ≤ {next_long:.1f} км"
                )

        # ── TE (Training Effect) aggregation ──
        te_stimulating = 0
        te_maintaining = 0
        te_easy = 0
        for a in (metrics.get("activities_14d") or []):
            te = a.get("training_effect") or a.get("aerobic_te")
            if te is None:
                continue
            te = float(te)
            if te >= 3.5:
                te_stimulating += 1
            elif te >= 2.0:
                te_maintaining += 1
            else:
                te_easy += 1
        if te_stimulating + te_maintaining + te_easy > 0:
            parts.append(
                f"  TE за 14 дн.: стимулирующих (≥3.5) {te_stimulating}, "
                f"поддерживающих (2-3.5) {te_maintaining}, лёгких (<2) {te_easy}"
            )

        # ── Cardiac drift detection (easy runs: HR rise >5% at stable pace) ──
        drift_flags: list[str] = []
        for a in run_28d[-5:]:  # last 5 runs
            splits = a.get("km_splits") or []
            if len(splits) < 4:
                continue
            # Check if pace is stable (within 15 sec/km) but HR rises >5%
            hrs = [s.get("avg_hr") for s in splits if s.get("avg_hr")]
            paces_s = []
            for s in splits:
                p = s.get("pace", "")
                parts_p = p.split(":")
                if len(parts_p) == 2:
                    try:
                        paces_s.append(int(parts_p[0]) * 60 + int(parts_p[1]))
                    except ValueError:
                        pass
            if len(hrs) >= 4 and len(paces_s) >= 4:
                # First and last third
                n = len(hrs)
                third = max(1, n // 3)
                hr_first = sum(hrs[:third]) / third
                hr_last = sum(hrs[-third:]) / third
                pace_first = sum(paces_s[:third]) / third
                pace_last = sum(paces_s[-third:]) / third
                if hr_first > 0 and abs(pace_last - pace_first) < 15:  # stable pace
                    drift_pct = (hr_last - hr_first) / hr_first * 100
                    if drift_pct > 5:
                        d = a.get("start_time", "")[:10]
                        drift_flags.append(f"{d}: drift +{drift_pct:.0f}%")
        if drift_flags:
            parts.append(
                f"  CARDIAC DRIFT: {', '.join(drift_flags)} — "
                "признак недовосстановления или перегрева"
            )

        # ── Quality session classification (Z4/Z5 work) ──
        quality_sessions: list[str] = []
        for a in (metrics.get("activities_14d") or []):
            if a.get("sport") != "running":
                continue
            zsecs = self._analyst._garmin_zone_secs(a)
            if not zsecs:
                continue
            z4 = zsecs[3]
            z5 = zsecs[4]
            total_z = sum(zsecs)
            if total_z <= 0 or (z4 + z5) / total_z < 0.10:
                continue  # not a quality session
            d = a.get("start_time", "")[:10]
            dist = a.get("distance") or 0
            if z5 > z4 and z5 > 120:  # >2min in Z5
                quality_sessions.append(f"{d}: VO2max ({dist:.1f} км, Z4 {z4/60:.0f}м Z5 {z5/60:.0f}м)")
            elif z4 > 300:  # >5min in Z4
                quality_sessions.append(f"{d}: темпо ({dist:.1f} км, Z4 {z4/60:.0f}м Z5 {z5/60:.0f}м)")
            else:
                quality_sessions.append(f"{d}: смешанная ({dist:.1f} км, Z4 {z4/60:.0f}м Z5 {z5/60:.0f}м)")
        if quality_sessions:
            parts.append(f"\nКАЧЕСТВЕННЫЕ СЕССИИ (14 дн.): {'; '.join(quality_sessions)}")

        # ── Running economy trend (pace/HR ratio over 4 weeks, easy runs only) ──
        economy_points: list[tuple[str, float]] = []
        for a in run_28d:
            avg_speed = a.get("avg_speed")
            avg_hr = a.get("avg_hr")
            if not avg_speed or avg_speed <= 0 or not avg_hr or avg_hr <= 0:
                continue
            # Only easy runs: check zone distribution
            zsecs = self._analyst._garmin_zone_secs(a)
            if zsecs:
                total_z = sum(zsecs)
                aero = zsecs[0] + zsecs[1] + zsecs[2]
                if total_z > 0 and aero / total_z < 0.75:
                    continue  # not easy enough
            elif (a.get("distance") or 0) > 12:
                continue  # skip long runs without zone data
            # Economy = speed (km/h) / HR (bpm) * 1000 — higher is better
            economy = avg_speed / avg_hr * 1000
            d = a.get("start_time", "")[:10]
            economy_points.append((d, economy))
        if len(economy_points) >= 3:
            economy_points.sort(key=lambda x: x[0])
            first_avg = sum(e[1] for e in economy_points[:2]) / 2
            last_avg = sum(e[1] for e in economy_points[-2:]) / 2
            delta_pct = (last_avg - first_avg) / first_avg * 100 if first_avg > 0 else 0
            trend_label = "улучшается" if delta_pct > 2 else ("снижается" if delta_pct < -2 else "стабильная")
            parts.append(
                f"  Экономичность бега (скорость/ЧСС): {trend_label} ({delta_pct:+.1f}% за 4 нед.)"
            )

        # Weather forecast for the week (Open-Meteo, free API)
        weather_by_date: dict[str, dict] = {}
        fp = metrics.get("fitness_profile") or {}
        loc_lat = fp.get("location_lat")
        loc_lon = fp.get("location_lon")
        if loc_lat and loc_lon:
            forecast = fetch_weather_forecast(loc_lat, loc_lon, days=7)
            if forecast:
                for w in forecast:
                    weather_by_date[w["date"]] = w

        # Dates for the week + what's already completed
        day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        parts.append(f"\nДАТЫ НЕДЕЛИ (уже выполненные отмечены ✅, остальные — план):")
        for i in range(7):
            day = week_start + timedelta(days=i)
            day_str = day.isoformat()
            # Weather suffix for this day
            w = weather_by_date.get(day_str)
            if w and w.get("temp_max") is not None:
                wx = f" | {w['temp_max']:.0f}/{w['temp_min']:.0f}°C"
                if w.get("description"):
                    wx += f" {w['description']}"
                if (w.get("precipitation_mm") or 0) > 1:
                    wx += f", осадки {w['precipitation_mm']:.0f}мм"
                if (w.get("wind_max_kph") or 0) > 30:
                    wx += f", ветер {w['wind_max_kph']:.0f}км/ч"
                if w["temp_max"] > 27:
                    wx += " ⚠️ жара"
                elif w["temp_min"] < -10:
                    wx += " ⚠️ мороз"
            else:
                wx = ""
            if day > today:
                parts.append(f"  {day_names[i]} {day.strftime('%d.%m')}: [план]{wx}")
            else:
                # Find runs on this exact day
                day_runs = [
                    a for a in activities_14d
                    if a.get("start_time", "").startswith(day_str) and a.get("sport") == "running"
                ]
                day_other = [
                    a for a in activities_14d
                    if a.get("start_time", "").startswith(day_str) and a.get("sport") != "running"
                ]
                if day_runs:
                    km = sum(a.get("distance") or 0 for a in day_runs)
                    tl = sum(a.get("training_load") or 0 for a in day_runs)
                    tl_str = f", TL {tl:.0f}" if tl else ""
                    parts.append(
                        f"  {day_names[i]} {day.strftime('%d.%m')}: ✅ бег {km:.1f} км{tl_str}"
                    )
                elif day_other:
                    sport = day_other[0].get("sport", "?")
                    other_tl = sum(a.get("training_load") or 0 for a in day_other)
                    other_tl_str = f", TL {other_tl:.0f}" if other_tl else ""
                    parts.append(f"  {day_names[i]} {day.strftime('%d.%m')}: ✅ {sport}{other_tl_str} (не бег)")
                else:
                    parts.append(f"  {day_names[i]} {day.strftime('%d.%m')}: отдых / нет данных")

        parts.append(
            "\n⚠️ ПРАВИЛО: дни с 'отдых / нет данных' — НЕ означают бег. "
            "Не дописывай 'уже выполнено' для дней без отметки ✅."
        )

        # Previous plan for continuity — so LLM sees what was already planned
        if previous_plan:
            parts.append(
                "\nПРЕДЫДУЩИЙ ПЛАН НА ЭТУ НЕДЕЛЮ (для преемственности, обнови с учётом новых данных):\n"
                + previous_plan[:800]
            )

        return "\n".join(parts)
