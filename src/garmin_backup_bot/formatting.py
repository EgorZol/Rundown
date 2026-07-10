"""Форматтеры контекста для Claude — блоки метрик, трендов, календаря.

Вынесены из analyst.py как mixin: методы используют атрибуты HealthAnalyst
(self._hr_zones, self._hr_max и т.п.), заданные в его __init__.
Здесь НЕТ вызовов API и промптов — только детерминированная сборка текста.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

logger = logging.getLogger(__name__)


class FormattingMixin:
    @staticmethod
    def _format_verified_facts_block(verified_facts: list[dict] | None) -> str:
        """Возвращает блок «ПОДТВЕРЖДЁННЫЕ ФАКТЫ» для системного промпта.

        Эти факты — overlay поверх Garmin-данных. Когда Claude видит расхождение
        между Garmin и фактом — должен использовать факт. Реально мутировать
        Garmin-таблицы нельзя (перезатрутся следующим синком).
        """
        if not verified_facts:
            return ""
        lines = [
            "\n🟢 ПОДТВЕРЖДЁННЫЕ АТЛЕТОМ ФАКТЫ (источник истины поверх Garmin — "
            "не оспаривай, не пересчитывай. Расхождение с БД = ошибка часов/трекинга, "
            "а не атлета):"
        ]
        for f in verified_facts:
            lines.append(f"  #{f['id']} {f['fact_date']}: {f['fact_text']}")
        return "\n".join(lines) + "\n"

    def format_header(self, metrics: dict[str, Any], tz=None) -> str:
        """Return a human-readable header showing which date the report covers."""
        sleep = metrics.get("sleep_last_night") or metrics.get("sleep") or {}
        start = sleep.get("start", "")
        end = sleep.get("end", "")
        day = metrics.get("date", "?")
        if start and end:
            try:
                from datetime import datetime as dt
                # Times are already converted to user's local timezone by _get_metrics.
                # Just parse and format — no further timezone conversion needed.
                s = dt.fromisoformat(str(start).split(".")[0])
                e = dt.fromisoformat(str(end).split(".")[0])
                return (
                    f"📅 Отчёт за {day}\n"
                    f"Сон: {s.strftime('%-d %b %H:%M')} → {e.strftime('%-d %b %H:%M')}\n"
                )
            except Exception:
                pass
        return f"📅 Отчёт за {day}\n"

    def _compute_hr_zones(self, fitness_profile: dict | None = None) -> tuple[dict, str]:
        """Return (zones_dict, method_label). zones_dict: {"Z1":(lo,hi), ..., "Z5":(lo,hi)}.
        Fallback only — used when Garmin zone boundaries are not available.
        HRmax priority: Garmin profile hr_max > observed from activities > Tanaka formula."""
        fp = fitness_profile or {}
        age = fp.get("age") or self._user_age
        tanaka_max = round(208 - 0.7 * age)
        # Priority: Garmin profile hr_max > observed from activities > Tanaka formula
        profile_max = fp.get("hr_max")
        observed_max = fp.get("observed_hr_max")
        if profile_max and profile_max > 100:
            hr_max = int(profile_max)
            hr_src = f"HRmax={hr_max} (профиль Garmin)"
        elif observed_max and observed_max > tanaka_max:
            hr_max = observed_max
            hr_src = f"HRmax={hr_max} (зафиксированный)"
        else:
            hr_max = tanaka_max
            hr_src = f"HRmax={hr_max} (форм��ла Танака)"
        lthr = fp.get("lthr")
        if lthr:
            # Friel 5-zone LTHR system (continuous, no gaps)
            b1 = round(lthr * 0.85)
            b2 = round(lthr * 0.90)
            b3 = round(lthr * 0.95)
            b4 = lthr
            z = {
                "Z1": (0, b1 - 1),
                "Z2": (b1, b2 - 1),
                "Z3": (b2, b3 - 1),
                "Z4": (b3, b4 - 1),
                "Z5": (b4, hr_max),
            }
            label = f"по LTHR={lthr:.0f} (метод Фрила)"
        else:
            z = {
                "Z1": (0,                       round(hr_max * 0.60)),
                "Z2": (round(hr_max * 0.60),    round(hr_max * 0.70)),
                "Z3": (round(hr_max * 0.70),    round(hr_max * 0.80)),
                "Z4": (round(hr_max * 0.80),    round(hr_max * 0.90)),
                "Z5": (round(hr_max * 0.90),    hr_max),
            }
            label = f"по {hr_src}"
        return z, label

    def _garmin_zone_secs(self, activity: dict) -> list[float] | None:
        """Единая реализация в coach.garmin_zone_secs; None если все нули (для рендера)."""
        from . import coach as _coach
        secs = _coach.garmin_zone_secs(activity)
        if secs is None or not any(s > 0 for s in secs):
            return None
        return list(secs)

    def _format_garmin_zones(self, activity: dict) -> str | None:
        """Format Garmin zone times as 'Z1 5м / Z2 38м / Z3 8м'."""
        secs = self._garmin_zone_secs(activity)
        if not secs:
            return None
        parts = []
        for i, s in enumerate(secs, 1):
            m = round(s / 60)
            if m > 0:
                parts.append(f"Z{i} {m}м")
        return " / ".join(parts) if parts else None

    def _user_context_block(self, fitness_profile: dict | None = None, garmin_zone_boundaries: dict | None = None) -> str:
        fp = fitness_profile or {}
        age = fp.get("age") or self._user_age
        hr_max = round(208 - 0.7 * age)

        # Prefer actual Garmin zone boundaries from the watch (what the athlete sees)
        # hrz_X_hr = FLOOR (lower bound) of zone X; ceiling = hrz_(X+1)_hr - 1
        gz = garmin_zone_boundaries
        if gz and gz.get("hrz_1_hr") and gz.get("hrz_5_hr"):
            zones_str = (
                f"Z1 {gz['hrz_1_hr']}-{gz['hrz_2_hr'] - 1} (разминка), "
                f"Z2 {gz['hrz_2_hr']}-{gz['hrz_3_hr'] - 1} (лёгкая), "
                f"Z3 {gz['hrz_3_hr']}-{gz['hrz_4_hr'] - 1} (АЭРОБНАЯ — основная зона лёгкого бега), "
                f"Z4 {gz['hrz_4_hr']}-{gz['hrz_5_hr'] - 1} (пороговая), "
                f"Z5 {gz['hrz_5_hr']}+ (анаэробная)"
            )
            zones_label = f"с часов Garmin"
        else:
            z, zones_label = self._compute_hr_zones(fp)
            zones_str = (
                f"Z1 {z['Z1'][0]}-{z['Z1'][1]} (разминка), "
                f"Z2 {z['Z2'][0]}-{z['Z2'][1]} (лёгкая), "
                f"Z3 {z['Z3'][0]}-{z['Z3'][1]} (аэробная), "
                f"Z4 {z['Z4'][0]}-{z['Z4'][1]} (пороговая), "
                f"Z5 {z['Z5'][0]}+ (анаэробная)"
            )

        profile_max = fp.get("hr_max")
        observed_max = fp.get("observed_hr_max")
        block = (
            f"\n\nПРОФИЛЬ СПОРТСМЕНА:\n"
            f"�� Возраст: {age} лет\n"
        )
        if fp.get("weight_kg") is not None:
            block += f"• Вес: {fp['weight_kg']} кг\n"
        if fp.get("height_cm") is not None:
            block += f"• Рост: {fp['height_cm']:.0f} см\n"
        if profile_max and profile_max > 100:
            block += f"• ЧССmax: {int(profile_max)} у��/мин (профиль Garmin)\n"
        elif observed_max:
            block += f"��� ЧССmax: {observed_max} уд/мин (зафиксированный)\n"
        else:
            block += f"• ЧССmax: {hr_max} уд/мин (формула Танака)\n"
        block += (
            f"• Зоны пульса ({zones_label}): {zones_str}\n"
        )
        user_km_target = fp.get("weekly_km_target") or self._weekly_km_target
        if user_km_target > 0:
            block += f"• Цель по бегу: {user_km_target:.0f} км/неделю\n"
        if fp.get("vo2_max") is not None:
            v = fp["vo2_max"]
            # ACSM age-adjusted VO2max percentile ranges (men)
            # Source: ACSM's Guidelines for Exercise Testing and Prescription
            if age < 30:
                thresholds = (55, 49, 44, 39)
            elif age < 40:
                thresholds = (52, 47, 42, 37)
            elif age < 50:
                thresholds = (49, 44, 39, 34)
            elif age < 60:
                thresholds = (43, 39, 35, 31)
            else:
                thresholds = (40, 36, 32, 28)
            if v >= thresholds[0]:
                level = "элитный любитель"
            elif v >= thresholds[1]:
                level = "отличный"
            elif v >= thresholds[2]:
                level = "хороший"
            elif v >= thresholds[3]:
                level = "средний"
            else:
                level = "ниже среднего"
            block += f"• VO2max: {v} мл/кг/мин ({level} для {age} лет)\n"
        if fp.get("lthr") is not None:
            lthr = fp["lthr"]
            block += (
                f"• LTHR (лактатный порог): {lthr} уд/мин — "
                f"граница Z3/Z4; темповый бег = {round(lthr * 0.95)}–{lthr} уд/мин\n"
            )
        gender = fp.get("gender")
        if gender:
            block += f"• Пол: {'мужской' if gender == 'male' else 'женский'}\n"
        exp = fp.get("running_experience_years")
        if exp is not None:
            if exp < 1:
                exp_label = "начинающий — консервативный рост объёма (макс +5%/нед)"
            elif exp < 3:
                exp_label = "любитель — стандартное правило 10%/нед"
            else:
                exp_label = "опытный — допустим рост до 12-15%/нед при хорошем восстановлении"
            block += f"• Беговой стаж: {exp:.0f} лет ({exp_label})\n"
        avail = fp.get("available_days")
        if avail:
            import json as _json
            try:
                days_list = _json.loads(avail) if isinstance(avail, str) else avail
                day_names_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
                block += f"• Доступные дни бега: {', '.join(day_names_ru[d] for d in sorted(days_list))} ({len(days_list)} дн/нед)\n"
            except Exception:
                pass
        wd_min = fp.get("max_session_min_weekday")
        we_min = fp.get("max_session_min_weekend")
        if wd_min or we_min:
            parts_t = []
            if wd_min:
                parts_t.append(f"будни {wd_min} мин")
            if we_min:
                parts_t.append(f"выходные {we_min} мин")
            block += f"• Макс. длительность тренировки: {', '.join(parts_t)}\n"
        injuries = fp.get("injuries")
        if injuries and injuries.lower() != "нет":
            block += f"• ⚠️ Травмы/ограничения: {injuries}\n"
        return block

    @staticmethod
    def _race_countdown(race_date_iso: str, today_d: "datetime.date") -> str:
        """Готовая фраза «суббота, послезавтра» — LLM не должен считать дни сам (off-by-one)."""
        from datetime import date as _date
        race_d = _date.fromisoformat(race_date_iso)
        days_left = (race_d - today_d).days
        names = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        wd = names[race_d.weekday()]
        if days_left == 0:
            return f"{wd}, СЕГОДНЯ"
        if days_left == 1:
            return f"{wd}, завтра"
        if days_left == 2:
            return f"{wd}, послезавтра"
        if days_left < 14:
            return f"{wd}, через {days_left} дн."
        return f"{wd}, через {days_left} дн. (~{days_left // 7} нед.)"

    @staticmethod
    def _calendar_block(anchor_date_str: str = "") -> str:
        """Return today + next 7 days with correct weekday names so Claude never miscalculates."""
        from datetime import date as _date
        try:
            today = _date.fromisoformat(anchor_date_str) if anchor_date_str else _date.today()
        except ValueError:
            today = _date.today()
        names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        lines = [f"КАЛЕНДАРЬ (используй только эти названия дней — не вычисляй самостоятельно):"]
        for i in range(8):
            d = today + datetime.timedelta(days=i)
            prefix = "Сегодня" if i == 0 else ("Завтра" if i == 1 else "")
            label = f"{names[d.weekday()]} {d.strftime('%d.%m')}"
            lines.append(f"  {label}" + (f" ← {prefix}" if prefix else ""))
        return "\n".join(lines)

    def _format_metrics_light(self, metrics: dict[str, Any]) -> str:
        """Lightweight context for ask() — just today's snapshot. Heavy history available via SQL tools."""
        parts: list[str] = []
        target_date = metrics.get("date", "?")
        parts.append(f"=== ДАННЫЕ GARMIN за {target_date} ===\n")
        # Календарь дней недели обязателен и в QA: без него Claude выводит день
        # недели сам и ошибается (инцидент 06.07.2026 — «сегодня воскресенье»
        # в понедельник; в QA был только ISO «Сегодня: 2026-07-06»).
        parts.append(self._calendar_block(target_date))

        # Fitness profile (zones, VO2max, LTHR) — always needed for interpretation
        fp = metrics.get("fitness_profile")
        if fp:
            parts.append(self._user_context_block(fp, garmin_zone_boundaries=metrics.get("garmin_zones")))

        # Today's sleep (single record)
        sleep_data = metrics.get("sleep_last_night") or metrics.get("sleep")
        if sleep_data:
            parts.append(self._format_sleep(sleep_data))

        # Today's daily summary
        ds = metrics.get("daily_summary")
        if ds:
            parts.append(self._format_daily_summary(ds))

        # HRV
        hrv = metrics.get("hrv")
        if hrv:
            parts.append(self._format_hrv(hrv))

        # Resting HR
        rhr = metrics.get("resting_hr")
        if rhr:
            parts.append(f"ПУЛЬС ПОКОЯ: {rhr.get('resting_heart_rate', '?')}")

        # Current weight
        weight = metrics.get("weight")
        if weight and weight.get("weight"):
            parts.append(f"ВЕС: {weight['weight']} кг ({weight.get('day', '?')})")

        # Fitness metrics (CTL/ATL/TSB)
        fitness = metrics.get("fitness")
        if fitness and fitness.get("ctl") is not None:
            parts.append(
                f"ФОРМА: CTL {fitness['ctl']:.1f}, ATL {fitness.get('atl', '?'):.1f}, "
                f"TSB {fitness.get('tsb', '?'):.1f}"
            )

        return "\n\n".join(p for p in parts if p)

    def _format_metrics(self, metrics: dict[str, Any]) -> str:
        parts: list[str] = []
        target_date = metrics.get("date", "?")
        parts.append(f"=== ДАННЫЕ GARMIN за {target_date} ===\n")
        parts.append(self._calendar_block(target_date))

        # Training goal — show prominently so model always sees it
        goal = metrics.get("training_goal", "")
        if goal:
            parts.append(f"ГЛАВНАЯ ЦЕЛЬ АТЛЕТА: {goal}")

        # Upcoming races — critical for periodization context
        races = metrics.get("upcoming_races") or []
        if races:
            from datetime import date as _date
            today_d = _date.fromisoformat(metrics.get("date", _date.today().isoformat()))
            race_lines = ["ПРЕДСТОЯЩИЕ СТАРТЫ (день и счёт уже вычислены — «завтра/послезавтра» бери из скобок, сам не пересчитывай):"]
            for r in races:
                dist = f" {r['distance_km']:.1f}км" if r.get("distance_km") else ""
                goal_t = f", цель {r['goal_time']}" if r.get("goal_time") else ""
                race_lines.append(
                    f"  {r['date']} — {r['name']}{dist}{goal_t} "
                    f"[до старта: {self._race_countdown(r['date'], today_d)}]"
                )
            parts.append("\n".join(race_lines))

        # План на сегодня/завтра — строки извлечены кодом (coach.plan_line_for_date),
        # модель НЕ должна искать их в тексте плана или истории сама
        if metrics.get("plan_missing"):
            parts.append("📋 [ПЛАНА НА ЭТУ НЕДЕЛЮ НЕТ — юзер его ещё не составил]")
        elif "plan_today_line" in metrics or "plan_tomorrow_line" in metrics:
            pl = ["📋 ПЛАН НЕДЕЛИ (строки дня извлечены кодом — задание на сегодня бери ТОЛЬКО отсюда):"]
            today_line = metrics.get("plan_today_line")
            pl.append(f"  [ПЛАН НА СЕГОДНЯ]: {today_line}" if today_line
                      else "  [ПЛАНА НА СЕГОДНЯ НЕТ — в плане нет строки на эту дату]")
            tomorrow_line = metrics.get("plan_tomorrow_line")
            if tomorrow_line:
                pl.append(f"  [ПЛАН НА ЗАВТРА]: {tomorrow_line}")
            if metrics.get("plan_week_type"):
                pl.append(f"  (тип недели: {metrics['plan_week_type']})")
            parts.append("\n".join(pl))

        # Sleep (last night — may be today's date if wake-detected)
        sleep_data = metrics.get("sleep_last_night") or metrics.get("sleep")
        if sleep_data:
            parts.append(self._format_sleep(sleep_data))

        # Daily summary
        ds = metrics.get("daily_summary")
        if ds:
            parts.append(self._format_daily_summary(ds))

        # HRV
        hrv = metrics.get("hrv")
        if hrv:
            parts.append(self._format_hrv(hrv))

        # Resting HR
        rhr = metrics.get("resting_hr")
        if rhr:
            parts.append(f"ПУЛЬС ПОКОЯ: {rhr.get('resting_heart_rate', '?')}")

        # Weight
        weight = metrics.get("weight")
        if weight and weight.get("weight"):
            parts.append(f"ВЕС: {weight['weight']} кг ({weight.get('day', '?')})")

        # Nutrition (yesterday) — relevant for recovery quality assessment
        food_yd = metrics.get("food_yesterday") or []
        if food_yd:
            total_cal = sum(e.get("calories", 0) for e in food_yd)
            total_p = sum(e.get("protein_g", 0) for e in food_yd)
            total_f = sum(e.get("fat_g", 0) for e in food_yd)
            total_c = sum(e.get("carbs_g", 0) for e in food_yd)
            cal_burned = (metrics.get("daily_summary") or {}).get("calories_total")
            balance_str = ""
            if cal_burned:
                balance = total_cal - cal_burned
                sign = "+" if balance >= 0 else ""
                balance_str = f", баланс {sign}{balance:.0f} ккал (vs {cal_burned:.0f} сожжено)"
            parts.append(
                f"ПИТАНИЕ (вчера, {len(food_yd)} записей): "
                f"{total_cal:.0f} ккал | Б {total_p:.0f}г Ж {total_f:.0f}г У {total_c:.0f}г"
                f"{balance_str}"
            )

        # Activities — 28 days for full context; detail for recent 7, brief for older
        activities = metrics.get("activities_28d") or metrics.get("activities_14d") or metrics.get("activities_week", [])
        if activities:
            parts.append(self._format_activities(activities, fitness_profile=metrics.get("fitness_profile")))

        # Subjective well-being (last 7 days)
        feelings = metrics.get("feelings") or []
        if feelings:
            labels = {1: "очень плохо", 2: "плохо", 3: "нормально", 4: "хорошо", 5: "отлично"}
            f_lines = ["САМОЧУВСТВИЕ (субъективно, 1-5):"]
            for f in feelings:
                label = labels.get(f["score"], str(f["score"]))
                note_str = f" — {f['note']}" if f.get("note") else ""
                f_lines.append(f"  {f['day']}: {f['score']}/5 ({label}){note_str}")
            # Compute composite overtraining signal
            sorted_f = sorted(feelings, key=lambda x: x["day"], reverse=True)
            recent_low = sum(1 for x in sorted_f[:3] if x["score"] <= 2)
            ds_bb = (metrics.get("daily_summary") or {}).get("bb_max", 100)
            hrv_status = (metrics.get("hrv") or {}).get("status", "")
            if recent_low >= 2 and (ds_bb < 55 or hrv_status == "UNBALANCED"):
                f_lines.append(
                    f"[СИГНАЛ_ПЕРЕГРУЗКИ] самочувствие ≤2 в {recent_low} из последних 3 дней"
                    f" + {'BB=' + str(ds_bb) if ds_bb < 55 else 'HRV=' + hrv_status}"
                    " — признаки накопленной усталости"
                )
            parts.append("\n".join(f_lines))

        # Trends
        parts.append(self._format_trends(metrics))

        # Athlete profile: zones, VO2max, LTHR — explicitly in data block so model sees it
        fp = metrics.get("fitness_profile") or {}
        if fp:
            parts.append(self._user_context_block(fp, garmin_zone_boundaries=metrics.get("garmin_zones")).strip())

        return "\n\n".join(parts)

    def _format_hrv(self, h: dict) -> str:
        lines = [f"HRV (вариабельность пульса, ночь {h.get('date', '?')}):"]
        if h.get("last_night_avg") is not None:
            lines.append(f"  Среднее за ночь: {h['last_night_avg']} мс")
        if h.get("weekly_avg") is not None:
            lines.append(f"  Недельное среднее: {h['weekly_avg']} мс")
        if h.get("last_night_5_min_high") is not None:
            lines.append(f"  Пик 5 мин за ночь: {h['last_night_5_min_high']} мс")
        bl, bu = h.get("baseline_balanced_low"), h.get("baseline_balanced_upper")
        if bl and bu:
            lines.append(f"  Личная база (норма): {bl}–{bu} мс")
        if h.get("status"):
            lines.append(f"  Статус: {h['status']}")
        if h.get("feedback_phrase"):
            lines.append(f"  Feedback: {h['feedback_phrase']}")
        return "\n".join(lines)

    def _format_sleep(self, s: dict) -> str:
        lines = [f"СОН (ночь, пробуждение {s.get('day', '?')}):"]
        for label, key in [
            ("Начало", "start"),
            ("Конец", "end"),
            ("Общее время", "total_sleep"),
            ("Глубокий сон", "deep_sleep"),
            ("Лёгкий сон", "light_sleep"),
            ("REM", "rem_sleep"),
            ("Пробуждения", "awake"),
        ]:
            val = s.get(key)
            if val is not None:
                lines.append(f"  {label}: {self._fmt_time(val)}")
        # Deep and REM — absolute hours + % for context
        total_secs = self._time_str_to_secs(s.get("total_sleep"))
        if total_secs > 0:
            deep_secs = self._time_str_to_secs(s.get("deep_sleep"))
            rem_secs = self._time_str_to_secs(s.get("rem_sleep"))
            if deep_secs > 0:
                deep_h = deep_secs / 3600
                deep_pct = round(deep_secs / total_secs * 100)
                deep_flag = " ⚠️ мало" if deep_h < 1.0 else ""
                lines.append(f"  Глубокий сон: {deep_h:.1f}ч ({deep_pct}%) (норма ≥1.0ч){deep_flag}")
            if rem_secs > 0:
                rem_h = rem_secs / 3600
                rem_pct = round(rem_secs / total_secs * 100)
                rem_flag = " ⚠️ мало" if rem_h < 1.5 else ""
                lines.append(f"  REM: {rem_h:.1f}ч ({rem_pct}%) (норма ≥1.5ч){rem_flag}")
        for label, key in [
            ("Оценка", "score"),
            ("Качество", "qualifier"),
            ("SpO2 средний", "avg_spo2"),
            ("Частота дыхания ночью (вд/мин)", "avg_rr"),
            ("Стресс во сне", "avg_stress"),
        ]:
            val = s.get(key)
            if val is not None:
                lines.append(f"  {label}: {val}")
        return "\n".join(lines)

    def _format_daily_summary(self, ds: dict) -> str:
        lines = [f"ДНЕВНЫЕ ПОКАЗАТЕЛИ ({ds.get('day', '?')}):"]
        mappings = [
            ("Пульс мин", "hr_min"),
            ("Пульс макс", "hr_max"),
            ("Пульс покоя", "rhr"),
            ("Шаги", "steps"),
            ("Цель шагов", "step_goal"),
            ("Расстояние", "distance"),
            ("Этажи вверх", "floors_up"),
            ("Этажи вниз", "floors_down"),
            ("Калории всего", "calories_total"),
            ("Калории BMR", "calories_bmr"),
            ("Калории активные", "calories_active"),
            ("Стресс средний", "stress_avg"),
            ("Body Battery макс", "bb_max"),
            ("Body Battery мин", "bb_min"),
            ("Body Battery заряд", "bb_charged"),
            ("SpO2 средний", "spo2_avg"),
            ("SpO2 мин", "spo2_min"),
            ("Дыхание среднее", "rr_waking_avg"),
            ("Дыхание макс", "rr_max"),
            ("Дыхание мин", "rr_min"),
            ("Умеренная активность", "moderate_activity_time"),
            ("Интенсивная активность", "vigorous_activity_time"),
        ]
        for label, key in mappings:
            val = ds.get(key)
            if val is not None:
                lines.append(f"  {label}: {val}")
        return "\n".join(lines)

    @staticmethod
    def _fmt_run_dynamics(a: dict) -> str:
        """Format running dynamics: каденс 162 | шаг 1.05м | ВО 91мм | GCT 275мс | ВР 8.5%"""
        parts = []
        if a.get("avg_steps_per_min"):
            parts.append(f"каденс {a['avg_steps_per_min']} шаг/мин")
        if a.get("avg_step_length"):
            parts.append(f"шаг {a['avg_step_length'] / 1000:.2f}м")
        if a.get("avg_vertical_oscillation"):
            parts.append(f"верт.кол. {a['avg_vertical_oscillation']:.0f}мм")
        if a.get("avg_ground_contact_time"):
            # stored as HH:MM:SS.ffffff — convert to ms
            raw = str(a["avg_ground_contact_time"])
            try:
                secs_str = raw.split(".")[0]
                ps = secs_str.split(":")
                total_secs = int(ps[0]) * 3600 + int(ps[1]) * 60 + int(ps[2]) if len(ps) == 3 else 0
                frac = float("0." + raw.split(".")[1]) if "." in raw else 0
                ms = round((total_secs + frac) * 1000)
                parts.append(f"GCT {ms}мс")
            except Exception:
                pass
        if a.get("avg_vertical_ratio"):
            parts.append(f"верт.р. {a['avg_vertical_ratio']:.1f}%")
        return " | ".join(parts)

    def _format_activities(self, activities: list[dict], fitness_profile: dict | None = None) -> str:
        from datetime import date as _date, timedelta as _td
        cutoff_recent = (_date.today() - _td(days=7)).isoformat()
        recent = [a for a in activities if a.get("start_time", "") >= cutoff_recent]
        older = [a for a in activities if a.get("start_time", "") < cutoff_recent]

        lines = [f"ТРЕНИРОВКИ (всего {len(activities)} шт. за 28 дней):"]

        if recent:
            lines.append(f"  --- Последние 7 дней ({len(recent)} шт.) ---")
            for a in recent[:15]:
                sport = a.get("sport", "?")
                name = a.get("name", "")
                dist = a.get("distance")
                avg_hr = a.get("avg_hr")
                max_hr = a.get("max_hr")
                tl = a.get("training_load")
                te = a.get("training_effect")
                start = a.get("start_time", "?")
                feel = a.get("self_eval_feel")
                effort = a.get("self_eval_effort")
                vo2 = a.get("run_vo2max")

                header = f"  {start} — {sport}"
                if name and name != sport:
                    header += f" ({name})"
                detail = []
                if dist:
                    detail.append(f"{dist:.1f}км")
                if avg_hr:
                    detail.append(f"пульс {avg_hr}/{max_hr or '?'}")
                if tl:
                    detail.append(f"TL {tl}")
                if te:
                    detail.append(f"TE {te}")
                if vo2:
                    detail.append(f"VO2max тренировки {vo2}")
                if feel or effort:
                    detail.append(f"{feel or ''}/{effort or ''}".strip("/"))
                lines.append(header + (f" | {', '.join(detail)}" if detail else ""))
                # HR zones from Garmin
                zones_str = self._format_garmin_zones(a)
                if zones_str:
                    lines.append(f"    Зоны: {zones_str}")
                # Running dynamics (only for running)
                if sport == "running":
                    dyn = self._fmt_run_dynamics(a)
                    if dyn:
                        lines.append(f"    Динамика: {dyn}")

        if older:
            lines.append(f"  --- Ранее (8–28 дней назад, {len(older)} шт.) ---")
            for a in older[:20]:
                sport = a.get("sport", "?")
                name = a.get("name", "")
                dist = a.get("distance")
                avg_hr = a.get("avg_hr")
                tl = a.get("training_load")
                start = a.get("start_time", "?")[:10]
                feel = a.get("self_eval_feel")
                effort = a.get("self_eval_effort")
                vo2 = a.get("run_vo2max")
                brief = []
                if dist:
                    brief.append(f"{dist:.1f}км")
                if avg_hr:
                    brief.append(f"пульс {avg_hr}")
                if tl:
                    brief.append(f"TL {tl}")
                if vo2:
                    brief.append(f"VO2max тренировки {vo2}")
                if feel or effort:
                    brief.append(f"{feel or ''}/{effort or ''}".strip("/"))
                label = f"  {start} {sport}"
                if name and name != sport:
                    label += f"({name})"
                lines.append(label + (f": {', '.join(brief)}" if brief else ""))

        return "\n".join(lines)

    def _format_trends(self, metrics: dict[str, Any]) -> str:
        lines = ["ТРЕНДЫ (7 дней, от старого к новому):"]

        sleep_trend = metrics.get("sleep_trend_7d", [])
        if sleep_trend:
            scores = [str(s.get("score", "?")) for s in sleep_trend]
            lines.append(f"  Сон (score): {', '.join(scores)}")

            # Sleep duration trend — flag if 3+ nights under 6.5h
            total_sleep_vals = [s.get("total_sleep") for s in sleep_trend]
            total_secs_list = [self._time_str_to_secs(v) for v in total_sleep_vals if v]
            if total_secs_list:
                recent_short = sum(1 for s in total_secs_list[-3:] if s < 6.5 * 3600)
                if recent_short >= 3:
                    lines.append(f"  [НЕДОСЫП_ТРЕНД] сон <6.5ч три ночи подряд — снижение формы гарантировано")

            rr_vals = [s.get("avg_rr") for s in sleep_trend]
            rr_numeric = [v for v in rr_vals if v is not None]
            if rr_numeric:
                lines.append(f"  Частота дыхания ночью (вд/мин): {', '.join(str(v or '?') for v in rr_vals)}")
                # Validate RR trend: compare latest to 7d average
                if len(rr_numeric) >= 3:
                    rr_7d_avg = sum(rr_numeric[:-1]) / len(rr_numeric[:-1])
                    rr_latest = rr_numeric[-1]
                    rr_delta = rr_latest - rr_7d_avg
                    if rr_delta >= 2:
                        lines.append(
                            f"  [RR_РОСТ] ЧД сегодня {rr_latest:.1f} vs 7д avg {rr_7d_avg:.1f} "
                            f"(+{rr_delta:.1f}) — ранний маркер болезни/перегрузки"
                        )

            spo2_vals = [s.get("avg_spo2") for s in sleep_trend]
            if any(v is not None for v in spo2_vals):
                lines.append(f"  SpO2 ночью (%): {', '.join(str(v or '?') for v in spo2_vals)}")

        rhr_trend = metrics.get("rhr_trend_7d", [])
        if rhr_trend:
            rhr_vals = [r.get("resting_heart_rate") for r in rhr_trend]
            lines.append(f"  Пульс покоя: {', '.join(str(v or '?') for v in rhr_vals)}")
            # RHR trend validation: compare latest to 7d average
            rhr_numeric = [v for v in rhr_vals if v is not None]
            if len(rhr_numeric) >= 4:
                rhr_avg = sum(rhr_numeric[:-1]) / len(rhr_numeric[:-1])
                rhr_latest = rhr_numeric[-1]
                rhr_rise = rhr_latest - rhr_avg
                if rhr_rise >= 5:
                    lines.append(
                        f"  ⚠️ RHR +{rhr_rise:.0f} от 7д среднего ({rhr_avg:.0f}) — "
                        "недовосстановление или начало болезни"
                    )

        daily_trend = metrics.get("daily_trend_7d", [])
        if daily_trend:
            stress = [str(d.get("stress_avg", "?")) for d in daily_trend]
            lines.append(f"  Стресс средний: {', '.join(stress)}")
            bb = [str(d.get("bb_max", "?")) for d in daily_trend]
            lines.append(f"  Body Battery (уровень утром, bb_max): {', '.join(bb)}")

        # VO2max history trend
        vo2max_history = metrics.get("vo2max_history") or []
        if len(vo2max_history) >= 2:
            entries = sorted(vo2max_history, key=lambda e: e["date"])
            last = entries[-1]
            # 90-day trend
            cutoff_90 = (
                datetime.date.fromisoformat(last["date"]) - datetime.timedelta(days=90)
            ).isoformat()
            hist_90 = [e for e in entries if e["date"] >= cutoff_90]
            if len(hist_90) >= 2:
                delta_90 = round(last["vo2_max"] - hist_90[0]["vo2_max"], 1)
                arrow = "↑" if delta_90 > 0 else ("↓" if delta_90 < 0 else "→")
                trend_str = f"{arrow} {'+' if delta_90 >= 0 else ''}{delta_90} за 3 мес [{hist_90[0]['vo2_max']} → {last['vo2_max']}]"
            else:
                trend_str = f"текущий {last['vo2_max']}"
            recent = [e for e in entries if e["date"] >= (
                datetime.date.fromisoformat(last["date"]) - datetime.timedelta(days=30)
            ).isoformat()]
            pts = ", ".join(f"{e['date']}: {e['vo2_max']}" for e in recent[-6:])
            lines.append(f"  VO2max: {trend_str} | последние точки: {pts}")
        elif len(vo2max_history) == 1:
            lines.append(f"  VO2max: {vo2max_history[0]['vo2_max']} мл/кг/мин (1 точка, тренд появится после следующей синхронизации)")

        # CTL / ATL / TSB / ACWR
        fitness = metrics.get("fitness") or {}
        if fitness.get("ctl") is not None:
            ctl = fitness["ctl"]
            atl = fitness.get("atl")
            tsb = fitness["tsb"]
            tsb_str = f"+{tsb}" if tsb >= 0 else str(tsb)
            acwr_str = ""
            if atl is not None and ctl > 0:
                try:
                    acwr = float(atl) / float(ctl)
                    acwr_flag = " ⚠️ ПЕРЕГРУЗКА" if acwr > 1.5 else (" мало стимула" if acwr < 0.8 else "")
                    acwr_str = f", ACWR: {acwr:.2f}{acwr_flag}"
                except (ValueError, TypeError, ZeroDivisionError):
                    pass
            lines.append(
                f"  CTL (хроническая нагрузка): {ctl}, "
                f"ATL (острая): {atl}, TSB (форма): {tsb_str}{acwr_str}"
            )

        # Sport trends: current 7d vs previous 7d
        lines.append(self._format_sport_trends(metrics))

        return "\n".join(lines)

    def _format_sport_trends(self, metrics: dict[str, Any]) -> str:
        from datetime import datetime as _dt, timedelta as _td
        activities = metrics.get("activities_28d") or metrics.get("activities_14d", [])
        target_date = metrics.get("date", "")
        if not activities or not target_date:
            return ""

        try:
            today = _dt.fromisoformat(target_date).date()
        except Exception:
            from datetime import date as _date
            today = _date.today()

        def _week_stats(acts: list[dict], from_date, to_date) -> dict:
            sel = [
                a for a in acts
                if a.get("start_time", "") >= from_date.isoformat()
                and a.get("start_time", "") <= to_date.isoformat() + "T99"
            ]
            run = [a for a in sel if a.get("sport") == "running"]
            run_km = sum(a.get("distance") or 0 for a in run)
            run_secs = sum(self._time_str_to_secs(a.get("moving_time")) for a in run)
            hrs_all = [a.get("avg_hr") for a in run if a.get("avg_hr")]
            all_time_secs = sum(self._time_str_to_secs(a.get("moving_time")) for a in sel)
            return {
                "total_count": len(sel),
                "run_count": len(run),
                "run_km": run_km,
                "run_secs": run_secs,
                "avg_hr": round(sum(hrs_all) / len(hrs_all)) if hrs_all else None,
                "all_time_secs": all_time_secs,
            }

        def _fmt_pace(secs: float, km: float) -> str:
            if not secs or not km:
                return "?"
            p = secs / km / 60
            return f"{int(p)}:{int((p % 1) * 60):02d}"

        def _fmt_time(secs: float) -> str:
            if not secs:
                return "0м"
            h, m = divmod(int(secs) // 60, 60)
            return f"{h}ч {m}м" if h else f"{m}м"

        # Today's activities — explicit to prevent hallucination
        today_acts = [a for a in activities if a.get("start_time", "").startswith(today.isoformat())]
        today_runs = [a for a in today_acts if a.get("sport") == "running"]

        # Rolling 7-day windows (end at YESTERDAY to avoid implying activity today)
        d7_end = today - _td(days=1)
        d7_start = today - _td(days=7)
        prev7_end = today - _td(days=8)
        prev7_start = today - _td(days=14)
        cur7 = _week_stats(activities, d7_start, d7_end)
        prev7 = _week_stats(activities, prev7_start, prev7_end)

        # Calendar week: Monday of current week → today
        cal_start = today - _td(days=today.weekday())  # Monday
        cal_end = today
        # On Mon-Tue (weekday 0-1), current week has <2 days — show last full week instead
        if today.weekday() <= 1:
            # "Current" = last full Mon-Sun, "Previous" = the week before that
            cal_end_full = cal_start - _td(days=1)  # last Sunday
            cal_start_full = cal_end_full - _td(days=6)  # last Monday
            prev_cal_start = cal_start_full - _td(days=7)
            prev_cal_end = prev_cal_start + _td(days=6)
            curweek = _week_stats(activities, cal_start_full, cal_end_full)
            prevweek = _week_stats(activities, prev_cal_start, prev_cal_end)
            cal_start = cal_start_full
            cal_end = cal_end_full
            prev_cal_end_display = prev_cal_end
        else:
            # Same weekday range last week: last Monday → last Monday + same offset
            prev_cal_start = cal_start - _td(days=7)
            prev_cal_end = prev_cal_start + _td(days=today.weekday())  # same day count
            curweek = _week_stats(activities, cal_start, cal_end)
            prevweek = _week_stats(activities, prev_cal_start, prev_cal_end)
            prev_cal_end_display = prev_cal_end

        # Build 4-week summary (newest first: W1=current, W2, W3, W4)
        # Use yesterday as the base end to avoid including today in any window
        weeks = []
        base_end = today - _td(days=1)
        for i in range(4):
            w_end = base_end - _td(days=i * 7)
            w_start = w_end - _td(days=6)
            weeks.append((w_start, w_end, _week_stats(activities, w_start, w_end)))

        w1 = weeks[0][2]
        w2 = weeks[1][2]

        # Total 28-day stats
        all_run = [a for a in activities if a.get("sport") == "running"]
        total_km_28 = sum(a.get("distance") or 0 for a in all_run)
        total_secs_28 = sum(self._time_str_to_secs(a.get("moving_time")) for a in all_run)
        pace_28 = _fmt_pace(total_secs_28, total_km_28)

        # Explicit today block — prevents model from inferring activity from date ranges
        lines = ["\nСПОРТ — 4 недели (от новых к старым, бег):"]
        if today_runs:
            today_km = sum(a.get("distance") or 0 for a in today_runs)
            lines.append(
                f"  [СЕГОДНЯ {today.strftime('%d.%m')}] пробежек: {len(today_runs)}, {today_km:.1f} км"
            )
        else:
            lines.append(
                f"  [СЕГОДНЯ {today.strftime('%d.%m')}] пробежек НЕТ"
                + (f", других активностей: {len(today_acts)}" if today_acts else "")
            )
        lines.append(
            f"  [ИТОГО БЕГ 28Д] тотал 28 дней: {total_km_28:.1f} км / {len(all_run)} пробежек / средний темп {pace_28}/км"
        )

        # Explicit rolling 7-day summary — model MUST use these for "Бег 7 дней"
        pace_cur = _fmt_pace(cur7["run_secs"], cur7["run_km"])
        pace_prev = _fmt_pace(prev7["run_secs"], prev7["run_km"])
        lines.append(
            f"  [ИТОГО БЕГ 7Д] последние 7 дней ({d7_start.strftime('%d.%m')}–{d7_end.strftime('%d.%m')}): "
            f"{cur7['run_km']:.1f} км / {cur7['run_count']} пробежек / темп {pace_cur}/км"
        )
        lines.append(
            f"  [ИТОГО БЕГ 7Д ПРЕД] предыдущие 7 дней ({prev7_start.strftime('%d.%m')}–{prev7_end.strftime('%d.%m')}): "
            f"{prev7['run_km']:.1f} км / {prev7['run_count']} пробежек / темп {pace_prev}/км"
        )

        # Calendar week summary — model MUST use this for "Неделя с Пн"
        pace_cw = _fmt_pace(curweek["run_secs"], curweek["run_km"])
        pace_pw = _fmt_pace(prevweek["run_secs"], prevweek["run_km"])
        week_label = "неделя Пн-Вс (прошлая, полная)" if today.weekday() <= 1 else "текущая неделя с Пн"
        lines.append(
            f"  [ИТОГО НЕДЕЛЯ] {week_label} ({cal_start.strftime('%d.%m')}–{cal_end.strftime('%d.%m')}): "
            f"{curweek['run_km']:.1f} км / {curweek['run_count']} пробежек / темп {pace_cw}/км"
        )
        lines.append(
            f"  [ИТОГО НЕДЕЛЯ ПРЕД] предыдущая неделя ({prev_cal_start.strftime('%d.%m')}–{prev_cal_end_display.strftime('%d.%m')}): "
            f"{prevweek['run_km']:.1f} км / {prevweek['run_count']} пробежек / темп {pace_pw}/км"
        )
        # Per-week summary row
        week_rows = []
        for i, (ws, we, wst) in enumerate(weeks):
            label = f"Нед.{i + 1} ({ws.strftime('%d.%m')}–{we.strftime('%d.%m')})"
            if wst["run_km"] > 0:
                pace = _fmt_pace(wst["run_secs"], wst["run_km"])
                hr_str = f" пульс {wst['avg_hr']}" if wst["avg_hr"] else ""
                week_rows.append(
                    f"  {label}: {wst['run_km']:.1f} км / {wst['run_count']} пробежки, темп {pace}/км{hr_str}"
                )
            else:
                week_rows.append(f"  {label}: 0 км ({wst['total_count']} активностей)")
        lines.extend(week_rows)

        # Current week detail
        if w1["run_count"] or w2["run_count"]:
            km_delta_str = ""
            if w2["run_km"]:
                d = w1["run_km"] - w2["run_km"]
                km_delta_str = f" ({'+' if d >= 0 else ''}{d:.1f} к пред. неделе)"
            fp_km = (metrics.get("fitness_profile") or {}).get("weekly_km_target") or self._weekly_km_target
            dyn_target = metrics.get("weekly_km_target") or fp_km
            dyn_label = metrics.get("weekly_km_target_label", "")
            if dyn_target > 0 and w1["run_km"] > 0:
                pct = round(w1["run_km"] / dyn_target * 100)
                label_suffix = f" ({dyn_label})" if dyn_label else ""
                km_delta_str += f" | цель {dyn_target:.0f} км{label_suffix} = {pct}%"
            lines.append(f"  Текущая неделя итого: {w1['run_km']:.1f} км{km_delta_str}")
        lines.append(
            f"  Общее время (тек. нед.): {_fmt_time(w1['all_time_secs'])} → пред.: {_fmt_time(w2['all_time_secs'])}"
        )

        # Session-based 80/20 (Seiler) — Garmin zone times
        run_7d = [a for a in activities if a.get("sport") == "running"
                  and a.get("start_time", "") >= d7_start.isoformat()]
        if len(run_7d) >= 2:
            easy_s = 0
            for a in run_7d:
                gsecs = self._garmin_zone_secs(a)
                if gsecs:
                    total_s = sum(gsecs)
                    z123_s = gsecs[0] + gsecs[1] + gsecs[2]  # Z1-Z3 = aerobic in Garmin
                    is_easy = total_s > 0 and z123_s / total_s >= 0.80
                else:
                    z123 = sum(self._time_str_to_secs(a.get(f"hrz_{i}_time")) for i in range(1, 4))
                    total = self._time_str_to_secs(a.get("moving_time"))
                    is_easy = total > 0 and z123 / total >= 0.80
                if is_easy:
                    easy_s += 1
            hard_s = len(run_7d) - easy_s
            lines.append(
                f"  80/20 по сессиям (7д): {easy_s} лёгких / {hard_s} интенсивных из {len(run_7d)}"
            )

        return "\n".join(lines)

    @staticmethod
    def _time_str_to_secs(time_str: Any) -> float:
        """Convert 'HH:MM:SS.ffffff' to seconds."""
        if not time_str:
            return 0.0
        try:
            s = str(time_str).split(".")[0]  # strip microseconds
            parts = s.split(":")
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _fmt_time(val: Any) -> str:
        if val is None:
            return "?"
        s = str(val)
        # Strip microseconds from "HH:MM:SS.000000" or datetime strings
        if "." in s and len(s.split(".")[-1]) >= 4:
            s = s.rsplit(".", 1)[0]
        return s
