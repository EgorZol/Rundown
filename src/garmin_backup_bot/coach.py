"""Детерминированная логика тренера — расчёты, пороги и вердикты.

Этот модуль НЕ зависит от LLM. Всё считается в коде на основе данных из БД
или агрегированных метрик. Цель — снять с Claude арифметику и применение
порогов, оставить ему только язык, эмпатию и тактический совет.

Все функции pure: одинаковый вход → одинаковый выход. Покрыты unittest'ами.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

# ============================================================================
# КОНСТАНТЫ — единственное место, где живут пороги.
# ============================================================================

# Сон
DEEP_SLEEP_MIN_H = 1.0        # норма глубокого сна, абсолют
REM_MIN_H = 1.5               # норма REM, абсолют
SLEEP_TOTAL_MIN_H = 6.5       # общий сон, ниже — пометка

# ЧСС, дыхание
RHR_SPIKE_BPM = 5             # рост пульса покоя от 7-дн базы
RR_SPIKE_BPM = 2.0            # рост частоты дыхания ночью

# Body Battery
BB_LOW = 50                   # ≤50 — низкий заряд

# TSB (форма)
TSB_FRESH = 5                 # >+5 = свеж/пик
TSB_NEUTRAL_LO = -10          # -10..+5 = норма
TSB_BUILDING_LO = -25         # -25..-10 = фаза нагрузки
# < -25                       # риск перегрузки

# ACWR (фитнес/усталость отношение)
ACWR_LOW = 0.8                # <0.8 = мало стимула
ACWR_NEUTRAL_HI = 1.2         # 0.8..1.2 = норма
ACWR_HIGH = 1.5               # >1.5 = перегрузка

# Тренировки
CARDIAC_DRIFT_THRESHOLD = 0.05   # 5% — порог cardiac drift
GPS_PACE_OUTLIER_RATIO = 0.20    # 20% — сплит быстрее среднего → GPS-аномалия
HEAT_TEMP_C = 27                 # >27 = жара
TE_OVERLOAD = 4.8                # Training Effect аэроб порог перегрузки
TE_DEVELOPING = 3.0              # 3-4 = развитие
TL_HEAVY = 100                   # Training Load >100 = тяжёлая нагрузка
WALK_PCT_PAUSE = 8               # >8% времени в паузах — пометка усталости

# 80/20 по фазе цикла. Каждой фазе соответствует допустимая полоса
# Z1-Z3 % от объёма недели. Эти числа единственный источник «нормы»
# поляризации в проекте — менять только здесь, ничего больше не править.
PHASE_BANDS: dict[str, tuple[int, int]] = {
    "recovery": (80, 100),
    "base":     (80, 100),
    "build":    (65, 80),
    "peak":     (50, 75),
    "taper":    (75, 90),
}

# Каденс по темпу (шаг/мин). pace_min_per_km → (lo, hi).
def cadence_band(pace_min_per_km: float) -> tuple[int, int]:
    if pace_min_per_km > 6.0:
        return (155, 165)
    if pace_min_per_km > 5.0:
        return (162, 172)
    return (170, 180)


# ============================================================================
# DATACLASSES — структуры, которые передаются в LLM как готовые факты.
# ============================================================================


@dataclass(frozen=True)
class GpsAnomaly:
    """Подозрительный сплит, где темп не соответствует пульсу."""
    split_km: int | None     # номер км (если из km_splits)
    pace: str                # отображаемый темп
    avg_hr: int | None
    reason: str              # «темп 3:30, HR 142 — на 22% быстрее среднего»


@dataclass(frozen=True)
class RecoveryStatus:
    """Сводный вердикт восстановления — итог утреннего сравнения с порогами."""
    label: str               # "good" | "caution" | "poor" | "alarm"
    drivers: list[str]       # ["RHR +6 над базой", "deep sleep 0.8ч <1.0"]
    safe_to_train_hard: bool # ложь при alarm/poor

    def to_prompt_block(self) -> str:
        head = f"recovery: {self.label}  ·  train_hard_safe: {self.safe_to_train_hard}"
        if not self.drivers:
            return head
        return head + "\n  drivers:\n    - " + "\n    - ".join(self.drivers)


@dataclass(frozen=True)
class WeekFacts:
    """Объёмы и оценки за календарную неделю."""
    week_start: date
    week_end: date
    km_running: float
    sessions_running: int
    sessions_total: int
    total_tl: float
    z1_z3_pct: int | None             # % времени в Z1-Z3 от всего HR-времени бега
    phase: str | None                 # week_type из weekly_plans
    z1_z3_band: tuple[int, int] | None  # допустимая полоса для phase
    z1_z3_verdict: str                # "in_band" | "below" | "above" | "unknown_phase"
    norm_km: float | None             # weekly_km_target из профиля
    plan_adherence: str               # "matched" | "deviating" | "no_plan" | "unknown"
    gps_anomalies: list[GpsAnomaly] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        lines = [
            f"week: {self.week_start.isoformat()}..{self.week_end.isoformat()}",
            f"km_running: {self.km_running:.1f}",
            f"sessions: {self.sessions_running} running / {self.sessions_total} total",
            f"total_tl: {self.total_tl:.0f}",
        ]
        if self.z1_z3_pct is not None:
            band = (
                f"  band_for_{self.phase}: {self.z1_z3_band[0]}-{self.z1_z3_band[1]}%"
                if self.z1_z3_band else "  band: unknown_phase"
            )
            lines.append(f"z1_z3_pct: {self.z1_z3_pct}%  ·  verdict: {self.z1_z3_verdict}\n{band}")
        if self.norm_km is not None:
            lines.append(f"norm_km_per_week: {self.norm_km:.0f}")
        else:
            lines.append("norm_km_per_week: not_set  (НЕ выдумывай, скажи «норма не задана»)")
        lines.append(f"plan_adherence: {self.plan_adherence}")
        if self.gps_anomalies:
            lines.append("gps_anomalies:")
            for a in self.gps_anomalies:
                lines.append(f"  - {a.reason}")
        return "\n".join(lines)


@dataclass(frozen=True)
class WorkoutFacts:
    """Факты по конкретной тренировке."""
    activity_id: Any                  # int или str (Garmin id)
    sport: str
    start_date: str                   # YYYY-MM-DD
    distance_km: float | None
    pace_min: float | None            # минуты на км
    pace_str: str                     # "5:42" или "—"
    avg_hr: int | None
    max_hr: int | None
    zone_secs: tuple[float, float, float, float, float] | None
    z1_z3_pct: int | None
    cardiac_drift_pct: float | None
    walk_pct: float | None
    cadence_verdict: str              # "low" | "norm" | "high" | "no_data"
    cadence_value: int | None         # шаг/мин
    primary_zone: int | None          # 1..5 — где провёл больше всего времени
    intensity_class: str              # "easy" | "tempo" | "interval" | "long" | "race" | "unknown"
    gps_anomalies: list[GpsAnomaly] = field(default_factory=list)
    heat_warning: bool = False
    te_aerobic: float | None = None
    te_anaerobic: float | None = None

    def to_prompt_block(self) -> str:
        lines = [
            f"activity_id: {self.activity_id}",
            f"date: {self.start_date}  ·  sport: {self.sport}",
            f"distance_km: {self.distance_km}  ·  pace: {self.pace_str}/km",
            f"avg_hr: {self.avg_hr}  ·  max_hr: {self.max_hr}",
            f"intensity_class: {self.intensity_class}  ·  primary_zone: Z{self.primary_zone or '?'}",
        ]
        if self.zone_secs:
            lines.append(
                "zone_minutes: " + " / ".join(
                    f"Z{i+1} {round(s/60)}" for i, s in enumerate(self.zone_secs)
                ) + f"  ·  z1_z3_pct: {self.z1_z3_pct}%"
            )
        if self.cardiac_drift_pct is not None:
            flag = " ⚠️" if self.cardiac_drift_pct > CARDIAC_DRIFT_THRESHOLD * 100 else ""
            lines.append(f"cardiac_drift: {self.cardiac_drift_pct:+.1f}%{flag}")
        if self.walk_pct is not None and self.walk_pct > WALK_PCT_PAUSE:
            lines.append(f"walk_pct: {self.walk_pct:.0f}% (много пауз — усталость?)")
        if self.cadence_value is not None:
            lines.append(f"cadence: {self.cadence_value} step/min ({self.cadence_verdict})")
        if self.heat_warning:
            lines.append("heat: жара >27°C — HR на 5-10 уд/мин выше нормы")
        if self.te_aerobic is not None:
            lines.append(f"TE aerobic: {self.te_aerobic:.1f}  ·  TE anaerobic: {self.te_anaerobic or 0:.1f}")
        if self.gps_anomalies:
            lines.append("gps_anomalies:")
            for a in self.gps_anomalies:
                lines.append(f"  - {a.reason}")
        return "\n".join(lines)


@dataclass(frozen=True)
class MorningFacts:
    """Сводка утра для брифа: метрики + RecoveryStatus + краткий контекст вчера."""
    date: date
    sleep_total_h: float | None
    deep_sleep_h: float | None
    rem_h: float | None
    rhr: int | None
    rhr_baseline: int | None
    rhr_delta: int | None
    bb_min: int | None
    bb_max: int | None
    hrv_status: str | None
    avg_rr: float | None
    avg_rr_baseline: float | None
    avg_rr_delta: float | None
    tsb: float | None
    acwr: float | None
    recovery: RecoveryStatus
    yesterday_brief: str              # «10.5 км темповая Z4 (activity 1234)» или ""

    def to_prompt_block(self) -> str:
        lines = ["MORNING FACTS (источник истины, считал не ты — не пересчитывай):"]
        lines.append(self.recovery.to_prompt_block())
        if self.sleep_total_h is not None:
            sleep_line = f"sleep_total: {self.sleep_total_h:.1f}h"
            if self.deep_sleep_h is not None:
                sleep_line += f"  ·  deep: {self.deep_sleep_h:.1f}h" + (
                    " (<норма 1.0h)" if self.deep_sleep_h < DEEP_SLEEP_MIN_H else " (в норме)"
                )
            if self.rem_h is not None:
                sleep_line += f"  ·  REM: {self.rem_h:.1f}h" + (
                    " (<норма 1.5h)" if self.rem_h < REM_MIN_H else " (в норме)"
                )
            lines.append(sleep_line)
        if self.rhr is not None:
            tail = ""
            if self.rhr_delta is not None:
                tail = f"  ·  delta_vs_baseline: {self.rhr_delta:+d}" + (
                    " (spike — недовосстановление)" if self.rhr_delta >= RHR_SPIKE_BPM else ""
                )
            lines.append(f"rhr: {self.rhr}" + tail)
        if self.bb_min is not None:
            lines.append(f"bb: min {self.bb_min} · max {self.bb_max}" + (
                " (низкий заряд)" if self.bb_min < BB_LOW else ""
            ))
        if self.hrv_status:
            lines.append(f"hrv_status: {self.hrv_status}")
        if self.avg_rr is not None:
            tail = ""
            if self.avg_rr_delta is not None and self.avg_rr_delta >= RR_SPIKE_BPM:
                tail = f"  ·  delta {self.avg_rr_delta:+.1f} (>+2 — ранний маркер болезни/перегрузки)"
            lines.append(f"avg_rr: {self.avg_rr}{tail}")
        if self.tsb is not None:
            phase = ("свеж" if self.tsb > TSB_FRESH else
                     "норма" if self.tsb > TSB_NEUTRAL_LO else
                     "нагрузка" if self.tsb > TSB_BUILDING_LO else
                     "перегруз")
            lines.append(f"tsb: {self.tsb:+.0f} ({phase})")
        if self.acwr is not None:
            band = ("мало" if self.acwr < ACWR_LOW else
                    "норма" if self.acwr <= ACWR_NEUTRAL_HI else
                    "повышенная" if self.acwr <= ACWR_HIGH else
                    "перегруз")
            lines.append(f"acwr: {self.acwr:.2f} ({band})")
        if self.yesterday_brief:
            lines.append(f"yesterday: {self.yesterday_brief}")
        return "\n".join(lines)


# ============================================================================
# ЧИСТЫЕ ФУНКЦИИ — расчёты, ничего наружу не пишут.
# ============================================================================


def eighty_twenty_band(phase: str | None) -> tuple[int, int] | None:
    """Возвращает (lo, hi) допустимой полосы Z1-Z3% или None для unknown phase."""
    if not phase:
        return None
    return PHASE_BANDS.get(phase.lower())


def classify_z1_z3(pct: int | None, phase: str | None) -> str:
    """Вердикт по 80/20 на основе фазы. Не выдумывает, если фазы нет."""
    if pct is None:
        return "no_data"
    band = eighty_twenty_band(phase)
    if band is None:
        return "unknown_phase"
    lo, hi = band
    if pct < lo:
        return "below"
    if pct > hi:
        return "above"
    return "in_band"


def garmin_zone_secs(activity: dict) -> tuple[float, float, float, float, float] | None:
    """Извлекает hrz_1_time..hrz_5_time как секунды. None если ни одной нет."""
    out = []
    have_any = False
    for i in range(1, 6):
        raw = activity.get(f"hrz_{i}_time")
        secs = _time_to_secs(raw)
        if raw is not None:
            have_any = True
        out.append(secs)
    if not have_any:
        return None
    return tuple(out)  # type: ignore[return-value]


def _time_to_secs(val: Any) -> float:
    """«HH:MM:SS.fff» / число / None → секунды."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val)
    if not s or s == "00:00:00":
        return 0.0
    try:
        parts = s.split(":")
        if len(parts) == 3:
            h, m, sec = parts
            return int(h) * 3600 + int(m) * 60 + float(sec)
        if len(parts) == 2:
            m, sec = parts
            return int(m) * 60 + float(sec)
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def z1_z3_percent(zone_secs: tuple[float, ...] | None) -> int | None:
    """Возвращает целое % Z1+Z2+Z3 от суммы всех зон, или None."""
    if not zone_secs:
        return None
    total = sum(zone_secs)
    if total <= 0:
        return None
    z123 = zone_secs[0] + zone_secs[1] + zone_secs[2]
    return round(z123 / total * 100)


def primary_zone(zone_secs: tuple[float, ...] | None) -> int | None:
    """Номер зоны (1..5), где провёл больше всего времени."""
    if not zone_secs:
        return None
    if sum(zone_secs) <= 0:
        return None
    return max(range(5), key=lambda i: zone_secs[i]) + 1


def cadence_verdict(pace_min: float | None, cadence_value: int | None) -> tuple[str, int | None]:
    """Возвращает (label, value): low/norm/high/no_data."""
    if cadence_value is None or pace_min is None or pace_min <= 0:
        return ("no_data", cadence_value)
    lo, hi = cadence_band(pace_min)
    if cadence_value < lo - 2:
        return ("low", cadence_value)
    if cadence_value > hi + 5:
        return ("high", cadence_value)
    return ("norm", cadence_value)


def intensity_class(activity: dict, primary_z: int | None, z1_z3_pct: int | None) -> str:
    """Грубая классификация на основе зон и дистанции."""
    sport = activity.get("sport") or ""
    if sport != "running":
        return "unknown"
    dist = activity.get("distance") or 0
    name = (activity.get("name") or "").lower()
    if "забег" in name or "race" in name or "marathon" in name or "полумарафон" in name:
        return "race"
    if primary_z is None:
        return "unknown"
    if primary_z >= 4:
        # высокая интенсивность
        if dist >= 8 and z1_z3_pct is not None and z1_z3_pct >= 50:
            return "tempo"  # длинный темп
        return "interval"
    if dist >= 16:
        return "long"
    return "easy"


def detect_gps_anomalies(
    km_splits: list[dict] | None,
    avg_hr_workout: int | None = None,
) -> list[GpsAnomaly]:
    """Помечает км-сплиты, где темп существенно быстрее остальных при HR на том же уровне.

    Эвристика: если темп сплита быстрее медианы на >20% и HR <= медианы — флаг.
    Возвращает пустой список если данных мало (<5 сплитов).
    """
    if not km_splits or len(km_splits) < 5:
        return []
    # paces в секундах
    paces_secs: list[float] = []
    hrs: list[int | None] = []
    for s in km_splits:
        pace_str = s.get("pace") or ""
        secs = _pace_str_to_secs(pace_str)
        paces_secs.append(secs)
        hrs.append(s.get("avg_hr"))
    valid = [p for p in paces_secs if p > 0]
    if not valid:
        return []
    median = sorted(valid)[len(valid) // 2]
    valid_hrs = [h for h in hrs if h is not None]
    median_hr = sorted(valid_hrs)[len(valid_hrs) // 2] if valid_hrs else None
    out: list[GpsAnomaly] = []
    for i, (p, h) in enumerate(zip(paces_secs, hrs)):
        if p <= 0:
            continue
        if p < median * (1 - GPS_PACE_OUTLIER_RATIO):
            # темп быстрее на >20%. Если HR не выше — подозреваем GPS/спуск.
            if median_hr is None or (h is not None and h <= median_hr + 3):
                ratio = (median - p) / median * 100
                pace_disp = km_splits[i].get("pace") or "?"
                hr_disp = h if h is not None else "?"
                out.append(GpsAnomaly(
                    split_km=km_splits[i].get("km") or (i + 1),
                    pace=pace_disp,
                    avg_hr=h,
                    reason=f"km{km_splits[i].get('km') or i+1}: темп {pace_disp}, HR {hr_disp} — на {ratio:.0f}% быстрее медианы при равном/меньшем HR (GPS-глюк или спуск?)",
                ))
    return out


def _pace_str_to_secs(pace: str) -> float:
    """«5:42» → 342.0 сек/км. 0 если не распарсилось."""
    if not pace:
        return 0.0
    try:
        parts = pace.split(":")
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
    except (ValueError, TypeError):
        pass
    return 0.0


def filter_running_in_window(
    activities: list[dict], week_start: date, week_end: date
) -> list[dict]:
    """Отбирает беговые активности в окне [week_start..week_end] по дате старта."""
    out = []
    ws, we = week_start.isoformat(), week_end.isoformat()
    for a in activities or []:
        if a.get("sport") != "running":
            continue
        day = (a.get("start_time") or "")[:10]
        if ws <= day <= we:
            out.append(a)
    return out


def filter_all_in_window(
    activities: list[dict], week_start: date, week_end: date
) -> list[dict]:
    """Все активности (включая ходьбу/кросс) в окне."""
    out = []
    ws, we = week_start.isoformat(), week_end.isoformat()
    for a in activities or []:
        day = (a.get("start_time") or "")[:10]
        if ws <= day <= we:
            out.append(a)
    return out


def plan_adherence(week_runs: list[dict], plan_text: str | None) -> str:
    """Грубая оценка: matched/deviating/no_plan/unknown.

    Если plan_text пустой — no_plan.
    Если есть план и есть хотя бы 60% дней с тренировкой — matched.
    Считаем дни недели где была бегаловая активность, делим на 5 (типовой план).
    """
    if not plan_text:
        return "no_plan"
    if not week_runs:
        return "deviating"
    distinct_days = {(a.get("start_time") or "")[:10] for a in week_runs}
    if len(distinct_days) >= 3:
        return "matched"
    return "deviating"


def compute_week_facts(
    activities: list[dict],
    week_start: date,
    week_end: date,
    plan_meta: dict | None,
    profile: dict | None,
) -> WeekFacts:
    """Главный агрегатор недели. Все цифры считаются здесь, не в промпте."""
    week_runs = filter_running_in_window(activities, week_start, week_end)
    week_all = filter_all_in_window(activities, week_start, week_end)
    km_running = sum((a.get("distance") or 0) for a in week_runs)
    total_tl = sum((a.get("training_load") or 0) for a in week_all)

    # Z1-Z3 % по сумме секунд во всех беговых активностях
    z_secs = [0.0] * 5
    for a in week_runs:
        gs = garmin_zone_secs(a)
        if gs:
            for i in range(5):
                z_secs[i] += gs[i]
    z_total = sum(z_secs)
    z123_pct: int | None = None
    if z_total > 0:
        z123_pct = round((z_secs[0] + z_secs[1] + z_secs[2]) / z_total * 100)

    phase = (plan_meta or {}).get("week_type") if plan_meta else None
    band = eighty_twenty_band(phase)
    verdict = classify_z1_z3(z123_pct, phase)

    norm_km_raw = (profile or {}).get("weekly_km_target")
    norm_km: float | None = float(norm_km_raw) if norm_km_raw else None

    adherence = plan_adherence(week_runs, (plan_meta or {}).get("plan_text"))

    # GPS-аномалии: пройдём по km_splits каждой беговой
    anomalies: list[GpsAnomaly] = []
    for a in week_runs:
        splits = a.get("km_splits") or []
        anomalies.extend(detect_gps_anomalies(splits, avg_hr_workout=a.get("avg_hr")))

    return WeekFacts(
        week_start=week_start,
        week_end=week_end,
        km_running=km_running,
        sessions_running=len(week_runs),
        sessions_total=len(week_all),
        total_tl=total_tl,
        z1_z3_pct=z123_pct,
        phase=phase,
        z1_z3_band=band,
        z1_z3_verdict=verdict,
        norm_km=norm_km,
        plan_adherence=adherence,
        gps_anomalies=anomalies,
    )


def _heat_check(activity: dict) -> bool:
    weather = activity.get("weather") or {}
    w_start = weather.get("weatherStartCondition") or {}
    t = w_start.get("temp_c")
    if t is None:
        t = activity.get("avg_temperature")
        if t == 127.0:  # garmindb sentinel
            return False
    return bool(t and t > HEAT_TEMP_C)


def _pace_min_per_km(activity: dict) -> tuple[float | None, str]:
    """Возвращает (минуты_на_км, форматированную строку)."""
    avg_speed = activity.get("avg_speed")  # km/h
    if avg_speed and avg_speed > 0:
        pace = 60.0 / avg_speed
        return pace, f"{int(pace)}:{int((pace % 1) * 60):02d}"
    return None, "—"


def _cardiac_drift(km_splits: list[dict] | None) -> float | None:
    """Cardiac drift = % разница HR между второй и первой половиной."""
    if not km_splits or len(km_splits) < 6:
        return None
    half = len(km_splits) // 2
    first = [s.get("avg_hr") for s in km_splits[:half] if s.get("avg_hr")]
    second = [s.get("avg_hr") for s in km_splits[half:] if s.get("avg_hr")]
    if not first or not second:
        return None
    avg1 = sum(first) / len(first)
    avg2 = sum(second) / len(second)
    if avg1 <= 0:
        return None
    return (avg2 - avg1) / avg1 * 100


def compute_workout_facts(
    activity: dict,
    week_facts: WeekFacts | None = None,
) -> WorkoutFacts:
    """Факты по одной тренировке (основной, обычно activity #1)."""
    pace_min, pace_str = _pace_min_per_km(activity)
    zsecs = garmin_zone_secs(activity)
    z123_pct = z1_z3_percent(zsecs)
    pz = primary_zone(zsecs)
    cad_value = activity.get("avg_cadence")
    cad_steps = int(cad_value * 2) if cad_value else None
    cad_label, _ = cadence_verdict(pace_min, cad_steps)
    walk_pct: float | None = None
    moving = _time_to_secs(activity.get("moving_time"))
    elapsed = _time_to_secs(activity.get("elapsed_time"))
    if moving > 0 and elapsed > moving + 60:
        walk_pct = (elapsed - moving) / elapsed * 100
    drift_pct = _cardiac_drift(activity.get("km_splits"))
    cls = intensity_class(activity, pz, z123_pct)
    gps = detect_gps_anomalies(activity.get("km_splits"), avg_hr_workout=activity.get("avg_hr"))

    return WorkoutFacts(
        activity_id=activity.get("activity_id"),
        sport=activity.get("sport") or "unknown",
        start_date=(activity.get("start_time") or "")[:10],
        distance_km=activity.get("distance"),
        pace_min=pace_min,
        pace_str=pace_str,
        avg_hr=activity.get("avg_hr"),
        max_hr=activity.get("max_hr"),
        zone_secs=zsecs,
        z1_z3_pct=z123_pct,
        cardiac_drift_pct=drift_pct,
        walk_pct=walk_pct,
        cadence_verdict=cad_label,
        cadence_value=cad_steps,
        primary_zone=pz,
        intensity_class=cls,
        gps_anomalies=gps,
        heat_warning=_heat_check(activity),
        te_aerobic=activity.get("training_effect"),
        te_anaerobic=activity.get("anaerobic_training_effect"),
    )


def compute_recovery_status(metrics: dict) -> RecoveryStatus:
    """Применяет пороги к метрикам утра и выдаёт сводный вердикт.

    Логика порядка: alarm > poor > caution > good.
    """
    drivers: list[str] = []
    label = "good"
    safe_hard = True

    sleep = metrics.get("sleep_last_night") or metrics.get("sleep") or {}
    deep_h = _hours_from_minutes(sleep.get("deep_sleep_secs"))
    rem_h = _hours_from_minutes(sleep.get("rem_sleep_secs"))
    total_h = _hours_from_minutes(sleep.get("total_sleep_secs"))

    if deep_h is not None and deep_h < DEEP_SLEEP_MIN_H:
        drivers.append(f"deep sleep {deep_h:.1f}ч <норма {DEEP_SLEEP_MIN_H}ч")
        label = "caution"
    if rem_h is not None and rem_h < REM_MIN_H:
        drivers.append(f"REM {rem_h:.1f}ч <норма {REM_MIN_H}ч")
        label = "caution"
    if total_h is not None and total_h < SLEEP_TOTAL_MIN_H:
        drivers.append(f"общий сон {total_h:.1f}ч <{SLEEP_TOTAL_MIN_H}")
        label = "caution"

    rhr = (metrics.get("resting_hr") or {}).get("last")
    rhr_baseline = (metrics.get("resting_hr") or {}).get("avg_7d") or (metrics.get("resting_hr") or {}).get("baseline")
    if rhr is not None and rhr_baseline:
        delta = rhr - rhr_baseline
        if delta >= RHR_SPIKE_BPM:
            drivers.append(f"RHR {rhr} (+{delta} над базой {rhr_baseline}) — недовосстановление")
            label = "poor"
            safe_hard = False

    bb = metrics.get("body_battery") or {}
    bb_min = bb.get("min") or bb.get("min_24h")
    if bb_min is not None and bb_min < BB_LOW:
        drivers.append(f"Body Battery min {bb_min} (низкий заряд)")
        if label == "good":
            label = "caution"

    hrv = metrics.get("hrv") or {}
    hrv_status = hrv.get("status")
    if hrv_status and hrv_status.upper() in ("UNBALANCED", "LOW"):
        drivers.append(f"HRV {hrv_status}")
        label = "poor" if hrv_status.upper() == "UNBALANCED" else label
        safe_hard = False if hrv_status.upper() == "UNBALANCED" else safe_hard

    avg_rr = (sleep.get("avg_rr") or (metrics.get("avg_rr") if isinstance(metrics.get("avg_rr"), (int, float)) else None))
    avg_rr_base = (metrics.get("avg_rr_baseline_7d") or sleep.get("avg_rr_baseline_7d"))
    if avg_rr is not None and avg_rr_base:
        rr_delta = avg_rr - avg_rr_base
        if rr_delta >= RR_SPIKE_BPM:
            drivers.append(f"avg_rr {avg_rr:.1f} (+{rr_delta:.1f}) — ранний маркер болезни/перегрузки")
            label = "alarm"
            safe_hard = False

    # TSB
    fitness = metrics.get("fitness") or {}
    tsb = fitness.get("tsb")
    if isinstance(tsb, (int, float)) and tsb < TSB_BUILDING_LO:
        drivers.append(f"TSB {tsb:+.0f} (<{TSB_BUILDING_LO} — перегруз)")
        label = "poor"
        safe_hard = False

    acwr_v = fitness.get("acwr")
    if isinstance(acwr_v, (int, float)) and acwr_v > ACWR_HIGH:
        drivers.append(f"ACWR {acwr_v:.2f} (>{ACWR_HIGH} — перегруз)")
        label = "poor"
        safe_hard = False

    # subjective feeling
    feelings = metrics.get("feelings") or []
    if len(feelings) >= 2:
        last_two = sorted(feelings, key=lambda f: f.get("day", ""))[-2:]
        if all((f.get("score") or 5) <= 2 for f in last_two):
            drivers.append("самочувствие ≤2 два дня подряд")
            label = "alarm"
            safe_hard = False

    if metrics.get("overload_signal"):
        drivers.append("сигнал перегрузки в данных")
        label = "alarm"
        safe_hard = False

    return RecoveryStatus(label=label, drivers=drivers, safe_to_train_hard=safe_hard)


def _hours_from_minutes(val: Any) -> float | None:
    """Поддерживает: float секунды, строку «HH:MM:SS», или None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        # garminDB обычно даёт секунды
        return float(val) / 3600.0
    secs = _time_to_secs(val)
    return secs / 3600.0 if secs > 0 else None


def compute_morning_facts(metrics: dict, today: date | None = None) -> MorningFacts:
    """Сводит всё утро в один объект для подачи в analyze() как готовый блок."""
    today = today or date.today()
    sleep = metrics.get("sleep_last_night") or metrics.get("sleep") or {}
    rhr_block = metrics.get("resting_hr") or {}
    bb = metrics.get("body_battery") or {}
    hrv = metrics.get("hrv") or {}
    fitness = metrics.get("fitness") or {}

    rhr = rhr_block.get("last")
    rhr_base = rhr_block.get("avg_7d") or rhr_block.get("baseline")
    rhr_delta = (rhr - rhr_base) if (rhr is not None and rhr_base) else None

    avg_rr = sleep.get("avg_rr") or metrics.get("avg_rr")
    if not isinstance(avg_rr, (int, float)):
        avg_rr = None
    avg_rr_base = metrics.get("avg_rr_baseline_7d") or sleep.get("avg_rr_baseline_7d")
    if not isinstance(avg_rr_base, (int, float)):
        avg_rr_base = None
    avg_rr_delta = (avg_rr - avg_rr_base) if (avg_rr is not None and avg_rr_base is not None) else None

    # yesterday brief: ищем последнюю активность за вчера
    yesterday = today - timedelta(days=1)
    y_str = ""
    for a in metrics.get("activities_28d") or []:
        d = (a.get("start_time") or "")[:10]
        if d == yesterday.isoformat() and a.get("sport") == "running":
            dist = a.get("distance") or 0
            name = a.get("name") or "пробежка"
            y_str = f"{dist:.1f} км · {name}"
            break

    recovery = compute_recovery_status(metrics)

    return MorningFacts(
        date=today,
        sleep_total_h=_hours_from_minutes(sleep.get("total_sleep_secs")),
        deep_sleep_h=_hours_from_minutes(sleep.get("deep_sleep_secs")),
        rem_h=_hours_from_minutes(sleep.get("rem_sleep_secs")),
        rhr=rhr,
        rhr_baseline=rhr_base,
        rhr_delta=rhr_delta,
        bb_min=bb.get("min") or bb.get("min_24h"),
        bb_max=bb.get("max") or bb.get("max_24h"),
        hrv_status=hrv.get("status"),
        avg_rr=avg_rr,
        avg_rr_baseline=avg_rr_base,
        avg_rr_delta=avg_rr_delta,
        tsb=fitness.get("tsb") if isinstance(fitness.get("tsb"), (int, float)) else None,
        acwr=fitness.get("acwr") if isinstance(fitness.get("acwr"), (int, float)) else None,
        recovery=recovery,
        yesterday_brief=y_str,
    )
