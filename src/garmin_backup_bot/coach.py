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

    Логика порядка: alarm > poor > caution > good > no_data.
    Если ни одного источника данных нет — возвращает no_data, чтобы LLM
    не давал ложно-успокаивающий вердикт «всё отлично» свежим юзерам.
    """
    drivers: list[str] = []
    label = "good"
    safe_hard = True

    # Проверяем, есть ли хоть какие-то источники данных. Если все ключевые
    # поля пустые — это значит «утренняя сводка не подтянулась», не «всё ок».
    has_data = any([
        (metrics.get("sleep_last_night") or metrics.get("sleep")),
        (metrics.get("resting_hr") or {}).get("last") is not None,
        (metrics.get("body_battery") or {}).get("min") is not None,
        (metrics.get("hrv") or {}).get("status"),
        (metrics.get("fitness") or {}).get("tsb") is not None,
        (metrics.get("fitness") or {}).get("acwr") is not None,
        bool(metrics.get("feelings")),
    ])
    if not has_data:
        return RecoveryStatus(
            label="no_data",
            drivers=["нет утренних метрик — синхронизация не подтянула sleep/RHR/BB/HRV"],
            safe_to_train_hard=False,
        )

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
    # База RHR — медиана 7д (rhr_median_baseline), как и в overload_verdict;
    # avg_7d/baseline — фолбэк для метрик без daily_trend_7d
    rhr_baseline = (rhr_median_baseline(metrics.get("daily_trend_7d") or [])
                    or (metrics.get("resting_hr") or {}).get("avg_7d")
                    or (metrics.get("resting_hr") or {}).get("baseline"))
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


# ============================================================================
# ДАТЫ ПЛАНА НЕДЕЛИ — валидация и авто-коррекция пар «Пн DD.MM».
# Инцидент 05-06.07.2026: Claude сдвинул все даты плана на +1 день
# (Пн 07.07 вместо Пн 06.07) и план сохранился не в ту неделю.
# ============================================================================

import re as _re

WEEKDAY_ABBRS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_WEEKDAY_IDX = {abbr: i for i, abbr in enumerate(WEEKDAY_ABBRS)}
_PLAN_DAY_RE = _re.compile(
    r"\b(Пн|Вт|Ср|Чт|Пт|Сб|Вс)\.?\s+(\d{1,2})\.(\d{1,2})(?!\.\d)"
)


@dataclass
class PlanDatesCheck:
    """Результат проверки пар «день недели + дата» в тексте плана."""
    ok: bool
    week_start: date | None          # понедельник недели плана (если ok и даты есть)
    errors: list[str] = field(default_factory=list)
    hint: str = ""                   # правильный маппинг Пн..Вс для недели плана
    pairs_found: int = 0


def _infer_year(day: int, month: int, today: date) -> date | None:
    """DD.MM → date, год подбирается ближайший к today (окно ±200 дней)."""
    best = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    if best is not None and abs((best - today).days) > 200:
        return None
    return best


def check_plan_dates(plan_text: str, today: date) -> PlanDatesCheck:
    """Проверяет, что каждая дата в плане приходится на названный день недели.

    Если всё сходится — возвращает week_start (понедельник недели плана;
    можно сохранять план и на СЛЕДУЮЩУЮ неделю). Если нет — список ошибок
    и hint с правильным маппингом, который скармливается Claude как tool-error.
    План без дат — ok, week_start=None (вызывающий подставит текущую неделю).
    """
    pairs: list[tuple[str, date]] = []
    errors: list[str] = []
    for m in _PLAN_DAY_RE.finditer(plan_text):
        abbr, dd, mm = m.group(1), int(m.group(2)), int(m.group(3))
        d = _infer_year(dd, mm, today)
        if d is None:
            errors.append(f"«{abbr} {dd:02d}.{mm:02d}» — дата вне разумного окна от сегодня")
            continue
        pairs.append((abbr, d))
        if d.weekday() != _WEEKDAY_IDX[abbr]:
            real = WEEKDAY_ABBRS[d.weekday()]
            errors.append(f"«{abbr} {d.strftime('%d.%m')}» — но {d.strftime('%d.%m')} это {real}")

    if not pairs:
        return PlanDatesCheck(ok=not errors, week_start=None, errors=errors)

    # Неделя-подсказка: по большинству реальных дат (какой неделе они принадлежат)
    week_counts: dict[date, int] = {}
    for _, d in pairs:
        ws = d - timedelta(days=d.weekday())
        week_counts[ws] = week_counts.get(ws, 0) + 1
    ws_hint = max(week_counts, key=lambda k: week_counts[k])
    hint = ", ".join(
        f"{WEEKDAY_ABBRS[i]} {(ws_hint + timedelta(days=i)).strftime('%d.%m')}"
        for i in range(7)
    )

    if errors:
        return PlanDatesCheck(ok=False, week_start=None, errors=errors,
                              hint=hint, pairs_found=len(pairs))

    week_starts = {d - timedelta(days=d.weekday()) for _, d in pairs}
    if len(week_starts) > 1:
        return PlanDatesCheck(
            ok=False, week_start=None,
            errors=[f"даты плана попадают в разные недели: {sorted(week_starts)}"],
            hint=hint, pairs_found=len(pairs),
        )
    return PlanDatesCheck(ok=True, week_start=ws_hint, hint=hint, pairs_found=len(pairs))


def fix_plan_dates(plan_text: str, week_start: date) -> tuple[str, int]:
    """Детерминированно переписывает даты в парах «Пн DD.MM» под известный week_start.

    Для планов, которые генерирует сам бот (кнопка «📅 План»), неделя известна
    заранее — дате в тексте доверять не нужно, её можно просто вычислить.
    Возвращает (исправленный_текст, число_замен).
    """
    fixes = 0

    def _sub(m: "_re.Match[str]") -> str:
        nonlocal fixes
        abbr = m.group(1)
        correct = week_start + timedelta(days=_WEEKDAY_IDX[abbr])
        current = f"{int(m.group(2)):02d}.{int(m.group(3)):02d}"
        want = correct.strftime("%d.%m")
        if current != want:
            fixes += 1
        return f"{abbr} {want}"

    return _PLAN_DAY_RE.sub(_sub, plan_text), fixes


# ============================================================================
# ВЫБОР «ОСНОВНОЙ» АКТИВНОСТИ ДНЯ для «🏃 Разбор».
# Жалоба Алины 09.07.2026: при нескольких тренировках в день (бег → силовая →
# заминка) разбор доставался последней по времени — 16-минутной заминке.
# ============================================================================

RUN_SPORTS = frozenset((
    "running", "trail_running", "track_running",
    "treadmill_running", "indoor_running",
))


def _elapsed_secs(a: dict) -> int:
    """'H:MM:SS' → секунды; мусор/None → 0."""
    raw = a.get("elapsed_time") or ""
    parts = str(raw).split(":")
    try:
        parts = [int(float(p)) for p in parts]
    except ValueError:
        return 0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return 0


def reorder_primary_activity(activities: list[dict]) -> list[dict]:
    """Ставит «основную» активность последнего дня на позицию #1 (⟵ ОСНОВНАЯ).

    Правило: в пределах самого свежего дня с активностями основная — бег
    с максимальной дистанцией; если бега не было — самая долгая активность.
    Порядок остальных не меняется. Пустой список → пустой список.
    """
    if not activities:
        return activities
    last_day = (activities[0].get("start_time") or "")[:10]
    day_acts = [a for a in activities if (a.get("start_time") or "")[:10] == last_day]
    runs = [a for a in day_acts if a.get("sport") in RUN_SPORTS]
    if runs:
        primary = max(runs, key=lambda a: a.get("distance") or 0)
    else:
        primary = max(day_acts, key=_elapsed_secs)
    if primary is activities[0]:
        return activities
    return [primary] + [a for a in activities if a is not primary]


def plan_line_for_date(plan_text: str | None, target: date) -> str | None:
    """Строка плана недели для конкретной даты (по паре «Пн DD.MM»).

    Инцидент 10.07.2026: утренний отчёт реконструировал «что сегодня по плану»
    из истории чата и заявил «пробежек нет по плану», хотя в плане стоял бег
    8 км. Строку дня извлекает код — модель получает готовую метку
    [ПЛАН НА СЕГОДНЯ] и ничего не сопоставляет сама.
    """
    if not plan_text:
        return None
    for line in plan_text.splitlines():
        m = _PLAN_DAY_RE.search(line)
        if not m:
            continue
        d = _infer_year(int(m.group(2)), int(m.group(3)), target)
        if d == target:
            return line.strip()
    return None


# ============================================================================
# ЕДИНЫЙ ВЕРДИКТ ПЕРЕГРУЗА (hard-safety) — используют и утро (recovery_status),
# и план (determine_week_type). До 10.07.2026 правила были продублированы
# в plan_builder с другой базой RHR (среднее vs медиана) — два источника истины.
# ============================================================================

BB_CRIT_DAYS = 3              # BB<BB_LOW столько дней подряд → перегруз
HRV_COMBO_BB = 60             # HRV UNBALANCED + bb_max ниже этого → перегруз
VO2_DROPS_FOR_RECOVERY = 3    # падений VO2max подряд → перегруз
ECONOMY_DROP_PCT = 5.0        # падение экономичности за 4 нед, %
OVERLOAD_VOLUME_FACTOR = 0.6  # объём recovery-недели


@dataclass
class OverloadVerdict:
    reason: str
    volume_factor: float = OVERLOAD_VOLUME_FACTOR


def rhr_median_baseline(daily_trend: list[dict]) -> float | None:
    """База RHR = медиана 7 дней (устойчива к выбросам). Единственное определение."""
    vals = sorted(d.get("rhr") for d in daily_trend if d.get("rhr"))
    return vals[len(vals) // 2] if vals else None


def overload_verdict(
    metrics: dict,
    activities_28d: list[dict] | None = None,
    feelings: list[dict] | None = None,
) -> OverloadVerdict | None:
    """Правила принудительного восстановления. None = перегруза нет.

    Порядок = приоритет. Все пороги — константы этого модуля.
    """
    daily_trend = metrics.get("daily_trend_7d", []) or []

    # 1. RHR-спайк: выше медианы 7д на RHR_SPIKE_BPM
    rhr_today = (metrics.get("resting_hr") or {}).get("resting_heart_rate") \
        or (metrics.get("resting_hr") or {}).get("last")
    rhr_base = rhr_median_baseline(daily_trend) if len(daily_trend) >= 3 else None
    if rhr_today and rhr_base and rhr_today > rhr_base + RHR_SPIKE_BPM:
        return OverloadVerdict(
            f"ЧСС покоя {rhr_today} — на {rhr_today - rhr_base:.0f} уд/мин выше нормы "
            f"({rhr_base:.0f}) — принудительное восстановление")

    # 2. BB критически низкий несколько дней
    low_bb_days = sum(1 for d in daily_trend if (d.get("bb_max") or 100) < BB_LOW)
    if low_bb_days >= BB_CRIT_DAYS:
        return OverloadVerdict(f"BB ниже {BB_LOW} в течение {low_bb_days} дней — принудительное восстановление")

    # 3. HRV UNBALANCED + низкий BB
    hrv_status = (metrics.get("hrv") or {}).get("status", "")
    bb_today = (metrics.get("daily_summary") or {}).get("bb_max", 100)
    if hrv_status == "UNBALANCED" and bb_today < HRV_COMBO_BB:
        return OverloadVerdict("HRV UNBALANCED + BB низкий — признак перегрузки")

    # 4. ЧД ночью: рост над средней 7д — ранний маркер болезни
    sleep_trend = metrics.get("sleep_trend_7d") or []
    rr_vals = [d.get("avg_rr") for d in sleep_trend if d.get("avg_rr")]
    if len(rr_vals) >= 3:
        rr_avg = sum(rr_vals) / len(rr_vals)
        if rr_vals[-1] > rr_avg + RR_SPIKE_BPM:
            return OverloadVerdict(
                f"ЧД ночью {rr_vals[-1]:.1f} — на {rr_vals[-1] - rr_avg:.1f} вд/мин выше нормы "
                f"({rr_avg:.1f}) — ранний маркер болезни/перегрузки")

    # 5. Самочувствие ≤2 два дня подряд
    if feelings and len(feelings) >= 2:
        consecutive_low = 0
        for f in reversed([f["score"] for f in feelings[-3:]]):
            if f <= 2:
                consecutive_low += 1
            else:
                break
        if consecutive_low >= 2:
            return OverloadVerdict(
                f"Самочувствие ≤2 уже {consecutive_low} дня подряд — принудительное восстановление")

    # 6. Недосып: средний сон <SLEEP_TOTAL_MIN_H за 3 ночи
    sleep_hours: list[float] = []
    for d in sleep_trend:
        ts = d.get("total_sleep")
        if not ts:
            continue
        try:
            if isinstance(ts, str) and ":" in ts:
                p = ts.split(":")
                h = float(p[0]) + float(p[1]) / 60
            else:
                h = float(ts) / 3600
            if h > 0:
                sleep_hours.append(h)
        except (ValueError, IndexError):
            pass
    if len(sleep_hours) >= 3:
        avg_sl = sum(sleep_hours[-3:]) / 3
        if avg_sl < SLEEP_TOTAL_MIN_H:
            return OverloadVerdict(
                f"Средний сон {avg_sl:.1f}ч за последние 3 ночи (<{SLEEP_TOTAL_MIN_H}ч) — "
                "недовосстановление, принудительный отдых")

    # 7. VO2max падает VO2_DROPS_FOR_RECOVERY замера подряд
    vo2_hist = metrics.get("vo2max_history") or []
    if len(vo2_hist) >= VO2_DROPS_FOR_RECOVERY + 1:
        last = sorted(vo2_hist, key=lambda e: e["date"])[-(VO2_DROPS_FOR_RECOVERY + 1):]
        drops = sum(1 for i in range(1, len(last))
                    if (last[i].get("vo2_max") or 0) < (last[i - 1].get("vo2_max") or 0))
        if drops >= VO2_DROPS_FOR_RECOVERY:
            return OverloadVerdict(
                f"VO2max падает {VO2_DROPS_FOR_RECOVERY}+ замера подряд "
                f"({last[0].get('vo2_max', '?')} → {last[-1].get('vo2_max', '?')}) — "
                "признак перетренированности, принудительное восстановление")

    # 8. Экономичность бега (скорость/пульс на аэробных) падает за 4 недели
    runs = [a for a in (activities_28d or []) if a.get("sport") == "running"]
    econ: list[tuple[str, float]] = []
    for a in runs:
        spd, hr = a.get("avg_speed"), a.get("avg_hr")
        if not spd or spd <= 0 or not hr or hr <= 0:
            continue
        zs = garmin_zone_secs(a)
        if zs:
            total = sum(zs)
            if total > 0 and (zs[0] + zs[1] + zs[2]) / total < 0.75:
                continue  # неаэробная — не сравниваем
        elif (a.get("distance") or 0) > 12:
            continue
        econ.append((a.get("start_time", "")[:10], spd / hr * 1000))
    if len(econ) >= 4:
        econ.sort(key=lambda x: x[0])
        half = len(econ) // 2
        first = sum(e[1] for e in econ[:half]) / half
        second = sum(e[1] for e in econ[half:]) / (len(econ) - half)
        delta = (second - first) / first * 100 if first > 0 else 0
        if delta < -ECONOMY_DROP_PCT:
            return OverloadVerdict(
                f"Экономичность бега снижается ({delta:+.1f}% за 4 нед.) — "
                "признак накопленной усталости, принудительное восстановление")

    return None


# ── Пробелы в данных юзера: детерминированный чек-лист + троттлинг подсказок ─
# Философия прежняя: ЧТО просить и КОГДА — решает код; бот лишь показывает
# готовую строку-подсказку (одну за раз) в подвале Утра/Плана.

NUDGE_REPEAT_DAYS = 7          # повтор той же подсказки не чаще раза в неделю
NUDGE_REPEAT_DAYS_NEWBIE = 2   # пока нет ни цели, ни анкеты — напоминаем чаще
NUDGE_MAX_SHOWN = 2            # после стольких показов...
NUDGE_SNOOZE_DAYS = 30         # ...замолкаем про этот пробел на месяц


@dataclass(frozen=True)
class DataGap:
    key: str
    hint: str


def data_gaps(
    *,
    goal: str | None,
    has_future_races: bool,
    profile: dict,
    lthr: float | None,
    weight_kg: float | None,
) -> list[DataGap]:
    """Пробелы в данных по убыванию важности.

    lthr/weight_kg передавать УЖЕ объединёнными (Garmin + ручной override,
    как в metrics["fitness_profile"]) — просим только то, чего нет нигде.
    """
    gaps: list[DataGap] = []
    if not (goal or "").strip():
        gaps.append(DataGap(
            "goal",
            "у тебя не задана тренировочная цель — без неё план и прогресс работают вслепую. "
            "Просто скажи, например: «моя цель — полумарафон из 1:45»"))
    elif not has_future_races:
        gaps.append(DataGap(
            "race",
            "в календаре нет будущих стартов — с гонкой план выстроит фазы подготовки. "
            "Скажи: «добавь Московский марафон 27.09, цель 3:30»"))
    if not profile.get("available_days"):
        gaps.append(DataGap(
            "available_days",
            "я не знаю, в какие дни ты можешь бегать — план будет точнее. "
            "Нажми 📋 Профиль — анкета доспросит недостающее"))
    if not profile.get("location_name"):
        gaps.append(DataGap(
            "location",
            "не указан город — без него в плане нет прогноза погоды. Нажми 📋 Профиль"))
    if lthr is None:
        gaps.append(DataGap(
            "lthr",
            "нет лактатного порога (LTHR) — с ним пульсовые зоны точнее. "
            "Скажи: «мой порог 172» (как измерить — спроси меня)"))
    if weight_kg is None:
        gaps.append(DataGap(
            "weight",
            "нет веса — он нужен для калорий и оценки темпов. Скажи: «мой вес 72.5»"))
    return gaps


def pick_nudge(
    gaps: list[DataGap],
    history: dict[str, tuple[int, str | None]],
    today: date,
    repeat_days: int = NUDGE_REPEAT_DAYS,
) -> DataGap | None:
    """Первый пробел, который сейчас можно показать (history: key → (показов, последний ISO))."""
    for gap in gaps:
        shown, last_iso = history.get(gap.key, (0, None))
        if shown == 0 or not last_iso:
            return gap
        try:
            last = date.fromisoformat(str(last_iso)[:10])
        except ValueError:
            return gap
        wait = NUDGE_SNOOZE_DAYS if shown >= NUDGE_MAX_SHOWN else repeat_days
        if (today - last).days >= wait:
            return gap
    return None


# ── Подписки: чистая логика доступа (тарифы и цены — bot_payments.py) ────────

PLAN_COACH = "coach"            # всё: отчёты, план, QA, еда
PLAN_CALORIES = "calories"      # только домен еды
PLAN_TRIAL = "trial"            # 7 дней полного «Тренера» новым юзерам
PLAN_FREE_FOREVER = "free_forever"  # грандфазеринг ранних юзеров
TRIAL_DAYS = 7


def access_level(sub: dict | None, today: date) -> str:
    """Уровень доступа юзера: 'coach' | 'calories' | 'none'.

    sub = {'plan': ..., 'paid_until': ISO|None} или None (юзер без записи —
    триал ему заводит бот при первом обращении).
    """
    if not sub:
        return "none"
    plan = sub.get("plan")
    if plan == PLAN_FREE_FOREVER:
        return "coach"
    paid_until = sub.get("paid_until")
    if not paid_until:
        return "none"
    try:
        active = date.fromisoformat(str(paid_until)[:10]) >= today
    except ValueError:
        return "none"
    if not active:
        return "none"
    if plan in (PLAN_COACH, PLAN_TRIAL):
        return "coach"
    if plan == PLAN_CALORIES:
        return "calories"
    return "none"


def has_access(sub: dict | None, today: date, need: str = "coach") -> bool:
    """need: 'coach' (отчёты/QA) или 'any' (достаточно тарифа «Калории»)."""
    level = access_level(sub, today)
    if need == "coach":
        return level == "coach"
    return level in ("coach", "calories")
