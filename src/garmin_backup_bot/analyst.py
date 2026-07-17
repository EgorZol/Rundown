from __future__ import annotations

import asyncio
import contextvars
import datetime
import logging
from typing import Any, Callable

from anthropic import Anthropic

from .formatting import FormattingMixin
from .tools import build_tool_schemas, make_sql_runner
from .prompts import (
    PLAN_SYSTEM,
    PROGRESS_SYSTEM,
    SPORT_STATUS_SYSTEM,
    SYSTEM_PROMPT,
    WEEKLY_SYSTEM,
    WORKOUT_SYSTEM,
    build_ask_stable_prompt,
)

# Текущий Telegram user_id для атрибуции расхода токенов. Выставляется в bot.py
# на каждый update (TypeHandler, group=-1); contextvars протекают через await
# и asyncio.to_thread, так что все Claude-вызовы внутри хендлера видят юзера.
CURRENT_USER_ID: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "garmin_bot_current_user_id", default=None
)


async def call_write_tool(fn: Callable[..., Any], tool_input: dict) -> str:
    """Вызов write-tool с поддержкой async-коллбеков.

    Диспатч работает в event loop бота — sync-функции зовём напрямую,
    корутины await'им. До 10.07.2026 async-коллбеки оборачивались в
    asyncio.run() и молча падали в fallback (loop уже запущен) —
    авто-парсинг гонок из цели не работал никогда.
    """
    import inspect
    result = fn(**tool_input)
    if inspect.iscoroutine(result):
        result = await result
    return result

logger = logging.getLogger(__name__)



class HealthAnalyst(FormattingMixin):
    def __init__(
        self,
        api_key: str,
        model: str,
        fallback_models: list[str] | None = None,
        user_age: int = 35,
        weekly_km_target: float = 0.0,
        usage_sink: "Callable[..., None] | None" = None,
    ) -> None:
        self._client = Anthropic(api_key=api_key)
        candidates = [model, *(fallback_models or [])]
        self._models = list(dict.fromkeys(m for m in candidates if m))
        self._user_age = user_age
        self._weekly_km_target = weekly_km_target
        # Куда писать расход токенов (storage.log_token_usage); None = только лог
        self._usage_sink = usage_sink
        # Tanaka formula: more accurate for middle-aged than 220-age
        self._hr_max = round(208 - 0.7 * user_age)
        self._hr_zones = {
            "Z1": (0, round(self._hr_max * 0.60)),
            "Z2": (round(self._hr_max * 0.60), round(self._hr_max * 0.70)),
            "Z3": (round(self._hr_max * 0.70), round(self._hr_max * 0.80)),
            "Z4": (round(self._hr_max * 0.80), round(self._hr_max * 0.90)),
            "Z5": (round(self._hr_max * 0.90), self._hr_max),
        }


    def _report_usage(self, method: str, model: str, response: Any) -> None:
        """Записать usage одного API-вызова в sink (token_usage). Никогда не роняет анализ."""
        if self._usage_sink is None:
            return
        try:
            u = response.usage
            self._usage_sink(
                CURRENT_USER_ID.get(),
                method,
                model,
                u.input_tokens or 0,
                u.output_tokens or 0,
                getattr(u, "cache_read_input_tokens", 0) or 0,
                getattr(u, "cache_creation_input_tokens", 0) or 0,
            )
        except Exception as exc:
            logger.debug("usage sink failed: %s", exc)

    async def _generate_text(
        self,
        system_prompt: str | list[dict],
        user_prompt: str,
        max_tokens: int,
        history: list[dict] | None = None,
        user_memory: str = "",
        method: str = "generate",
    ) -> str:
        # Build messages: history (prior turns) + current user message
        messages: list[dict] = list(history or [])
        messages.append({"role": "user", "content": user_prompt})

        # system_prompt может быть либо строкой (legacy), либо уже готовым списком
        # блоков [stable+cache_control, dynamic]. Сплит делает вызывающая сторона —
        # это даёт prompt-cache hit на стабильную часть.
        if isinstance(system_prompt, list):
            system_block = system_prompt
        else:
            full_system = system_prompt
            if user_memory:
                full_system = (
                    "Важная информация о пользователе (запомни навсегда):\n"
                    f"{user_memory}\n\n{system_prompt}"
                )
            system_block = [{"type": "text", "text": full_system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}]

        last_exc: Exception | None = None
        for idx, model in enumerate(self._models):
            try:
                response = await asyncio.to_thread(
                    self._client.messages.create,
                    model=model,
                    max_tokens=max_tokens,
                    system=system_block,
                    messages=messages,
                )
                text_parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
                if idx > 0:
                    logger.warning("Anthropic fallback model used: %s", model)
                result = "\n".join(part for part in text_parts if part).strip()
                self._report_usage(method, model, response)
                u = response.usage
                cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
                cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
                if response.stop_reason == "max_tokens":
                    logger.warning(
                        "Response truncated by max_tokens=%d (input=%d out=%d cache_read=%d cache_write=%d)",
                        max_tokens, u.input_tokens, u.output_tokens, cache_read, cache_write,
                    )
                else:
                    logger.info(
                        "Response OK: stop=%s input=%d out=%d cache_read=%d cache_write=%d",
                        response.stop_reason, u.input_tokens, u.output_tokens, cache_read, cache_write,
                    )
                return result
            except Exception as exc:
                last_exc = exc
                logger.warning("Anthropic model failed: %s (%s)", model, exc)

        if last_exc:
            raise last_exc
        raise RuntimeError("No Anthropic models configured")

    async def analyze_workout(self, activities: list[dict[str, Any]], daily_metrics: dict[str, Any] | None, history: list[dict] | None = None, user_memory: str = "", plan_text: str = "", week_type: str = "", verified_facts: list[dict] | None = None, workout_facts: "Any" = None, week_facts: "Any" = None, today_iso: str | None = None) -> str:
        """Analyze recent workouts and give training feedback."""
        if not activities:
            return "Нет данных о тренировках за последние 7 дней."

        parts: list[str] = [
            "=== ТРЕНИРОВКИ (анализируем только #1, остальные — фон для трендов/объёма) ==="
        ]
        # Итог дня при нескольких активностях — посчитан кодом (coach),
        # модель его только показывает (правило изоляции запрещает ей суммировать)
        from . import coach as _coach_day
        _day_marker = _coach_day.day_activities_marker(activities)
        if _day_marker:
            parts.append(_day_marker)
        for i, a in enumerate(activities[:10], 1):
            sport = a.get("sport", "?")
            name = a.get("name") or sport
            start = a.get("start_time", "?")
            dist = a.get("distance")
            cal = a.get("calories")
            avg_hr = a.get("avg_hr")
            max_hr = a.get("max_hr")
            moving = a.get("moving_time")
            elapsed = a.get("elapsed_time")
            tl = a.get("training_load")
            te = a.get("training_effect")
            ate = a.get("anaerobic_training_effect")
            avg_speed = a.get("avg_speed")
            avg_cadence = a.get("avg_cadence")
            ascent = a.get("ascent")
            avg_temp = a.get("avg_temperature")

            marker = " ⟵ ОСНОВНАЯ (анализируй её)" if i == 1 else ""
            lines = [f"{i}. {start} — {name}{marker}"]
            if dist:
                lines.append(f"   Дистанция: {dist:.2f} км")
            if moving:
                moving_secs = self._time_str_to_secs(moving)
                elapsed_secs = self._time_str_to_secs(elapsed) if elapsed else 0
                lines.append(f"   Время: {moving}")
                if elapsed_secs > moving_secs + 60 and elapsed_secs > 0:
                    walk_pct = (elapsed_secs - moving_secs) / elapsed_secs * 100
                    walk_flag = " ⚠️ много пауз — усталость?" if walk_pct > 8 else ""
                    lines.append(f"   Паузы/ходьба: {walk_pct:.0f}% от времени{walk_flag}")
            if avg_speed:
                lines.append(f"   Средняя скорость: {avg_speed:.1f} км/ч")
            if avg_hr or max_hr:
                lines.append(f"   Пульс: ср. {avg_hr or '?'}, макс. {max_hr or '?'}")
            # HR zones — use Garmin zone times directly (matches Garmin Connect)
            zones_str = self._format_garmin_zones(a)
            if zones_str:
                lines.append(f"   Зоны: {zones_str}")
            if avg_cadence and sport == "running":
                steps = avg_cadence * 2
                cadence_note = ""
                if avg_speed and avg_speed > 0:
                    pace_min = 60.0 / avg_speed
                    if pace_min > 6.0:
                        norm_lo, norm_hi = 155, 165
                    elif pace_min > 5.0:
                        norm_lo, norm_hi = 162, 172
                    else:
                        norm_lo, norm_hi = 170, 180
                    if steps < norm_lo - 2:
                        cadence_note = f" ⚠️ низкий для темпа (норма {norm_lo}-{norm_hi})"
                    elif steps > norm_hi + 5:
                        cadence_note = f" (выше нормы {norm_lo}-{norm_hi}, ok)"
                    else:
                        cadence_note = f" ✓ норма ({norm_lo}-{norm_hi})"
                lines.append(f"   Каденс: {steps:.0f} шаг/мин{cadence_note}")
            elif avg_cadence:
                lines.append(f"   Каденс: {avg_cadence * 2:.0f} шаг/мин")
            # Running dynamics: VO, GCT, vertical ratio (only for running)
            if sport == "running":
                dyn = self._fmt_run_dynamics(a)
                if dyn:
                    lines.append(f"   Динамика бега: {dyn}")
            if ascent:
                lines.append(f"   Набор высоты: {ascent:.0f} м")
            # Weather: prefer API data (accurate), fall back to avg_temperature from device (often invalid)
            weather = a.get("weather") or {}
            w_start = weather.get("weatherStartCondition") or {}
            w_temp = w_start.get("temp_c")
            # avg_temperature from garmindb is often 127.0 (invalid sentinel) — ignore it
            if w_temp is not None:
                heat_note = " ⚠️ жара — ЧСС выше нормы на 5-10 уд/мин" if w_temp > 27 else ""
                w_extra = ""
                if w_start.get("wind_kph"):
                    w_extra += f", ветер {w_start['wind_kph']:.0f} км/ч"
                if w_start.get("humidity_pct"):
                    w_extra += f", влажность {w_start['humidity_pct']}%"
                if w_start.get("description"):
                    w_extra += f", {w_start['description']}"
                lines.append(f"   Погода: {w_temp:.0f}°C{w_extra}{heat_note}")
            elif avg_temp is not None and avg_temp != 127.0:
                heat_note = " ⚠️ жара — ЧСС выше нормы на 5-10 уд/мин" if avg_temp > 27 else ""
                lines.append(f"   Температура (датчик часов): {avg_temp:.0f}°C{heat_note}")
            if cal:
                lines.append(f"   Калории: {cal}")
            if tl:
                lines.append(f"   Нагрузка (Training Load): {tl:.0f}")
            if te:
                te_flag = " ⚠️ перегрузка — нужен отдых" if te >= 4.8 else (" — развивающая" if te >= 3.0 else "")
                lines.append(f"   Аэробный TE: {te:.1f}{te_flag}")
            if ate:
                ate_flag = " ⚠️ высокая анаэробная нагрузка" if ate >= 4.5 else ""
                lines.append(f"   Анаэробный TE: {ate:.1f}{ate_flag}")
            # Lap data (from Garmin Connect API — actual workout structure)
            laps = a.get("laps", [])
            if laps:
                # Filter out near-zero laps (auto-pause artifacts)
                meaningful_laps = [l for l in laps if (l.get("distance_m") or 0) > 50 or (l.get("duration_s") or 0) > 10]
                if meaningful_laps:
                    # Detect interval structure: work laps = shortest duration cluster
                    durations = [l.get("duration_s") or 0 for l in meaningful_laps]
                    if durations:
                        min_dur = min(durations)
                        max_dur = max(durations)
                        # If there's significant duration variation, classify laps
                        if max_dur > min_dur * 1.5 and min_dur < 180:
                            threshold = min_dur * 1.5
                            work_laps = [l for l in meaningful_laps if (l.get("duration_s") or 0) <= threshold]
                            rest_laps = [l for l in meaningful_laps if (l.get("duration_s") or 0) > threshold]
                            interval_summary = f"[ИТОГО ИНТЕРВАЛОВ: {len(work_laps)}, восстановлений: {len(rest_laps)}]"
                        else:
                            interval_summary = ""
                    else:
                        interval_summary = ""

                    lines.append(f"   Лапы ({len(meaningful_laps)}){' ' + interval_summary if interval_summary else ''}:")
                    for l in meaningful_laps:
                        dist_str = f"{l['distance_m']}м" if l.get("distance_m") else ""
                        dur = l.get("duration_s") or 0
                        dur_str = f"{dur//60}:{dur%60:02d}" if dur else ""
                        hr_str = f"  пульс {l['avg_hr']}" if l.get("avg_hr") else ""
                        pace_str = f"  темп {l['pace']}/км" if l.get("pace") else ""
                        lines.append(f"     лап {l['lap']}: {dist_str} {dur_str}{pace_str}{hr_str}")

            # Per-km splits (limit to 30 to cover even long runs without context overflow)
            km_splits = a.get("km_splits", [])
            if km_splits:
                shown = km_splits[:30]
                omitted = len(km_splits) - len(shown)
                lines.append(f"   Сплиты по километрам{' (первые 30 из ' + str(len(km_splits)) + ')' if omitted else ''}:")
                for s in shown:
                    hr_str = f"  пульс {s['avg_hr']}" if s.get("avg_hr") else ""
                    cad_str = f"  каденс {s['avg_cadence']}" if s.get("avg_cadence") else ""
                    lines.append(f"     км {s['km']}: темп {s['pace']}{hr_str}{cad_str}")
                # Cardiac drift здесь НЕ считаем (ревью: два разных значения в одном
                # контексте) — единственный источник: cardiac_drift_pct в WORKOUT FACTS
            parts.append("\n".join(lines))

        # Calendar week summary — prevents Claude from using rolling 7d instead of Mon–today
        from datetime import date as _date, timedelta as _td
        # P0.3: используем TZ юзера (today_iso) если передан, иначе fallback на
        # системную дату. Без этого OLD inline блок и NEW WEEK FACTS могли
        # показывать разные суммы при смене суток у юзера в дальнем TZ.
        _today = _date.fromisoformat(today_iso) if today_iso else _date.today()
        _cal_start = _today - _td(days=_today.weekday())  # Monday
        _cal_runs = [
            a for a in activities
            if a.get("sport") == "running"
            and a.get("start_time", "")[:10] >= _cal_start.isoformat()
            and a.get("start_time", "")[:10] <= _today.isoformat()
        ]
        _cal_km = sum(a.get("distance") or 0 for a in _cal_runs)
        _cal_tl = sum(a.get("training_load") or 0 for a in _cal_runs)
        # Z1-Z3 (aerobic) balance from Garmin zone times
        _zone_secs = [0.0] * 5
        for a in _cal_runs:
            gsecs = self._garmin_zone_secs(a)
            if gsecs:
                for i in range(5):
                    _zone_secs[i] += gsecs[i]
        _zone_total = sum(_zone_secs)
        _z123 = _zone_secs[0] + _zone_secs[1] + _zone_secs[2]  # Z1-Z3 = aerobic in Garmin
        _z123_pct = round(_z123 / _zone_total * 100) if _zone_total > 0 else None
        _week_line = (
            f"[ИТОГО НЕДЕЛЯ Пн {_cal_start.strftime('%d.%m')}–{_today.strftime('%d.%m')}] "
            f"бег {_cal_km:.1f} км / {len(_cal_runs)} пробежек / TL {_cal_tl:.0f}"
        )
        if _z123_pct is not None:
            _week_line += f" / Z1-Z3 {_z123_pct}%"
        if week_type:
            _week_line += f" / фаза: {week_type}"
        parts.append(_week_line)

        # Add calendar so Claude never miscalculates day-of-week names
        latest_date = activities[0].get("start_time", "")[:10] if activities else ""
        parts.append(self._calendar_block(latest_date))

        context_block = "\n\n".join(parts)

        if daily_metrics:
            dm = daily_metrics.get("daily_summary") or {}
            rhr = daily_metrics.get("resting_hr") or {}
            hrv = daily_metrics.get("hrv") or {}
            sleep = daily_metrics.get("sleep") or {}
            context_block += "\n\n=== СОСТОЯНИЕ НА ДЕНЬ ТРЕНИРОВКИ ==="
            if dm.get("bb_max") is not None:
                context_block += f"\nBody Battery уровень утром (bb_max): {dm['bb_max']}"
            if dm.get("bb_charged") is not None:
                context_block += f"\nBody Battery заряд за ночь (bb_charged, дельта): {dm['bb_charged']}"
            if dm.get("stress_avg") is not None:
                context_block += f"\nСредний стресс за день: {dm['stress_avg']}"
            if rhr.get("resting_heart_rate"):
                context_block += f"\nПульс покоя: {rhr['resting_heart_rate']}"
            # HRV — key recovery indicator for interpreting workout quality
            if hrv.get("last_night_avg"):
                bl = hrv.get("baseline_balanced_low", "?")
                bu = hrv.get("baseline_balanced_upper", "?")
                context_block += (
                    f"\nHRV ночь: {hrv['last_night_avg']} мс"
                    f" (база {bl}–{bu}, статус {hrv.get('status', '?')})"
                )
            # Sleep before workout — explains cardiac drift, HR elevation, performance
            if sleep:
                total = sleep.get("total_sleep")
                score = sleep.get("score")
                total_secs = self._time_str_to_secs(total)
                context_block += f"\nСон перед тренировкой: {self._fmt_time(total) if total else '?'}"
                if score is not None:
                    context_block += f", score {score}"
                if total_secs > 0:
                    deep_secs = self._time_str_to_secs(sleep.get("deep_sleep"))
                    rem_secs = self._time_str_to_secs(sleep.get("rem_sleep"))
                    if deep_secs > 0:
                        deep_h = deep_secs / 3600
                        context_block += f", глубокий {deep_h:.1f}ч ({round(deep_secs / total_secs * 100)}%)"
                    if rem_secs > 0:
                        rem_h = rem_secs / 3600
                        context_block += f", REM {rem_h:.1f}ч ({round(rem_secs / total_secs * 100)}%)"
                if sleep.get("avg_rr") is not None:
                    context_block += f"\nЧД ночью: {sleep['avg_rr']} вд/мин"

        # Add plan for comparison
        if plan_text:
            context_block += (
                "\n\n=== ПЛАН НА ЭТУ НЕДЕЛЮ (для сравнения с фактом) ===\n"
                + plan_text[:600]
            )

        workout_system = WORKOUT_SYSTEM
        fp = (daily_metrics or {}).get("fitness_profile")
        gz = (daily_metrics or {}).get("garmin_zones")
        facts_block = self._format_verified_facts_block(verified_facts)
        # Stage 2: пред-вычисленные факты тренировки и недели — Claude больше
        # не считает зоны/каденс/GPS, он только описывает их человеческим языком.
        coach_block = ""
        if workout_facts is not None:
            coach_block += "\n\n📐 WORKOUT FACTS (источник истины, считал не ты):\n"
            coach_block += workout_facts.to_prompt_block()
        if week_facts is not None:
            coach_block += "\n\n📐 WEEK FACTS:\n" + week_facts.to_prompt_block()
        try:
            return await self._generate_text(
                method="analyze_workout",
                system_prompt=(
                    workout_system
                    + coach_block
                    + (("\n" + facts_block) if facts_block else "")
                    + self._user_context_block(fp, garmin_zone_boundaries=gz)
                ),
                user_prompt=context_block,
                max_tokens=2000,
                history=history,
                user_memory=user_memory,
            )
        except Exception as exc:
            logger.exception("Error during workout analysis")
            return f"Не удалось получить анализ тренировки: {exc}"

    async def analyze_plan(self, context_text: str, history: list[dict] | None = None, user_memory: str = "", fitness_profile: dict | None = None, garmin_zones: dict | None = None) -> str:
        """Generate a weekly training plan based on current state and recent activities."""
        plan_system = PLAN_SYSTEM
        try:
            return await self._generate_text(
                method="analyze_plan",
                system_prompt=plan_system + self._user_context_block(fitness_profile, garmin_zone_boundaries=garmin_zones),
                user_prompt=context_text,
                max_tokens=3000,
                history=history,
                user_memory=user_memory,
            )
        except Exception as exc:
            logger.exception("Error during plan generation")
            return f"Не удалось сгенерировать план: {exc}"

    async def parse_races_from_text(self, text: str, today_iso: str) -> list[dict]:
        """Extract race events from free-form text. Returns list of dicts with keys:
        date (YYYY-MM-DD), name, distance_km (float|None), goal_time (str|None), notes (str|None).
        Returns [] if nothing found."""
        import json as _json
        system = (
            f"Сегодня {today_iso}. Извлеки из текста ниже список предстоящих соревнований/стартов.\n"
            "Верни ТОЛЬКО валидный JSON-массив объектов, без пояснений.\n"
            "Каждый объект: {\"date\": \"YYYY-MM-DD\", \"name\": \"...\", "
            "\"distance_km\": число или null, \"goal_time\": \"HH:MM:SS или M:SS или null\", "
            "\"notes\": \"краткая заметка или null\"}\n"
            "Если дата неточная (например 'в мае') — бери середину месяца.\n"
            "Если год не указан — ближайший подходящий будущий.\n"
            "Если стартов нет — верни []."
        )
        try:
            raw = await self._generate_text(
                method="parse_races",
                system_prompt=system,
                user_prompt=text,
                max_tokens=800,
            )
            # Extract JSON array from response (model may wrap in backticks)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            start = raw.find("[")
            end = raw.rfind("]") + 1
            if start == -1 or end == 0:
                return []
            return _json.loads(raw[start:end])
        except Exception as exc:
            logger.warning("Failed to parse races from text: %s", exc)
            return []

    async def analyze_calories(self, metrics: dict[str, Any], today=None) -> str:
        """Return a formatted calorie report for the current calendar week (no LLM)."""
        from datetime import date as _date, timedelta as _td

        if today is None:
            today = _date.today()

        # Current calendar week: Monday → today
        week_start = today - _td(days=today.weekday())

        daily_trend = metrics.get("daily_trend_7d") or []
        activities = metrics.get("activities_28d") or metrics.get("activities_14d") or []

        # Filter daily summaries to current week
        week_days = {
            r["day"]: r for r in daily_trend
            if r.get("day", "") >= week_start.isoformat()
        }

        # Filter activities to current week, group by day
        from collections import defaultdict
        acts_by_day: dict[str, list] = defaultdict(list)
        for a in activities:
            day = (a.get("start_time") or "")[:10]
            if day >= week_start.isoformat():
                acts_by_day[day].append(a)

        day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        lines = [f"🔥 КАЛОРИИ — неделя {week_start.strftime('%d.%m')}–{today.strftime('%d.%m')}"]
        lines.append("")

        week_total = 0
        week_active = 0
        week_workout_kcal = 0

        # Iterate Mon → today
        d = week_start
        while d <= today:
            d_iso = d.isoformat()
            day_label = f"{day_names[d.weekday()]} {d.strftime('%d.%m')}"
            row = week_days.get(d_iso, {})
            day_acts = acts_by_day.get(d_iso, [])

            total = row.get("calories_total")
            bmr = row.get("calories_bmr")
            active = row.get("calories_active")

            if total or day_acts:
                if d == today:
                    day_label += " ← сегодня"
                line = f"{day_label}:"
                if total:
                    line += f"  {total} ккал"
                    week_total += total
                if active:
                    line += f" (акт. {active})"
                    week_active += active
                if bmr:
                    line += f" [BMR {bmr}]"
                lines.append(line)

                # Activities for this day
                for a in day_acts:
                    sport = a.get("sport", "?")
                    kcal = a.get("calories")
                    dist = a.get("distance")
                    sport_icons = {"running": "🏃", "cycling": "🚴", "swimming": "🏊", "fitness_equipment": "💪"}
                    icon = sport_icons.get(sport, "⚡️")
                    act_parts = []
                    if dist:
                        act_parts.append(f"{dist:.1f}км")
                    if kcal:
                        act_parts.append(f"{kcal} ккал")
                        week_workout_kcal += kcal
                    lines.append(f"  {icon} {sport}" + (f" | {', '.join(act_parts)}" if act_parts else ""))
            else:
                lines.append(f"{day_label}:  нет данных")

            d += _td(days=1)

        # Week totals
        lines.append("")
        lines.append(f"Итого за неделю:")
        if week_total:
            lines.append(f"  Всего сожжено:   {week_total} ккал")
        if week_active:
            lines.append(f"  Активные:        {week_active} ккал")
        if week_workout_kcal:
            lines.append(f"  Тренировки:      {week_workout_kcal} ккал")

        # Today's partial day note
        today_row = week_days.get(today.isoformat(), {})
        if today_row.get("calories_total") and today_row.get("calories_bmr"):
            bmr_today = today_row["calories_bmr"]
            total_today = today_row["calories_total"]
            lines.append(f"\nСегодня {today.strftime('%d.%m')}: {total_today} ккал "
                         f"(из них BMR {bmr_today} — пассивный расход)")

        return "\n".join(lines)

    async def analyze_sport_status(self, metrics: dict[str, Any], training_goal: str = "") -> str:
        """Generate a sport-focused status report with trends, volumes, and dynamics."""
        sport_system = SPORT_STATUS_SYSTEM
        sport_prompt = self._format_sport_trends(metrics)
        if not sport_prompt:
            sport_prompt = "Нет данных о тренировках."

        # Add VO2max — current from fitness profile (authoritative), history for trend
        fp_vo2 = (metrics.get("fitness_profile") or {}).get("vo2_max")
        if fp_vo2:
            sport_prompt += f"\n\nVO2MAX ТЕКУЩИЙ (из профиля Garmin): {fp_vo2} мл/кг/мин — используй это значение в отчёте"
        vo2_hist = metrics.get("vo2max_history") or []
        if len(vo2_hist) >= 2:
            vo2_hist_sorted = sorted(vo2_hist, key=lambda e: e["date"])
            last = vo2_hist_sorted[-1]
            recent = vo2_hist_sorted[-6:]
            pts = ", ".join(f"{e['date']}: {e['vo2_max']}" for e in recent)
            # 90-day trend (3 months) — used for ↑↓ label
            cutoff_90 = (datetime.date.fromisoformat(last["date"]) - datetime.timedelta(days=90)).isoformat()
            hist_90 = [e for e in vo2_hist_sorted if e["date"] >= cutoff_90]
            if len(hist_90) >= 2:
                delta_90 = round(last["vo2_max"] - hist_90[0]["vo2_max"], 1)
                trend_label = f"{'↑' if delta_90 > 0 else ('↓' if delta_90 < 0 else '→')} за 3 мес: {'+' if delta_90 >= 0 else ''}{delta_90}"
            else:
                delta_90 = 0
                trend_label = "— (мало данных)"
            sport_prompt += f"\nVO2MAX ИСТОРИЯ (тренд): {trend_label} | последние: {pts}"
            # Flag significant drop from RECENT peak (last 90 days) to avoid false alarm from old history
            peak_90 = max(e["vo2_max"] for e in hist_90) if hist_90 else last["vo2_max"]
            drop_from_peak = round(peak_90 - last["vo2_max"], 1)
            if drop_from_peak >= 2:
                sport_prompt += f"\n[VO2MAX_DROP] пик {peak_90} → текущий {last['vo2_max']} (падение {drop_from_peak}) — проверь перетренировку или болезнь"

        # Add form (CTL/ATL/TSB + ACWR)
        perf = (metrics.get("fitness") or metrics.get("performance") or {})
        if perf.get("ctl") is not None:
            try:
                ctl = float(perf["ctl"])
                atl = float(perf["atl"]) if perf.get("atl") is not None else None
                tsb = float(perf["tsb"]) if perf.get("tsb") is not None else None
                atl_s = f"{atl:.0f}" if atl is not None else "?"
                tsb_s = f"{tsb:+.0f}" if tsb is not None else "?"
                form_line = f"\n\nФОРМА: CTL {ctl:.0f}, ATL {atl_s}, TSB {tsb_s}"
                # ACWR (Acute:Chronic Workload Ratio)
                if atl is not None and ctl > 0:
                    acwr = atl / ctl
                    acwr_flag = ""
                    if acwr > 1.5:
                        acwr_flag = " ⚠️ ПЕРЕГРУЗКА — нужна разгрузочная неделя"
                    elif acwr > 1.2:
                        acwr_flag = " ⚠️ повышенная нагрузка — не наращивать объём"
                    elif acwr < 0.8:
                        acwr_flag = " (мало стимула)"
                    form_line += f", ACWR {acwr:.2f}{acwr_flag}"
                sport_prompt += form_line
            except (ValueError, TypeError):
                pass

        # Add last running activity dynamics — only if data exists and is within 14 days
        activities = metrics.get("activities_28d") or metrics.get("activities_14d") or []
        run_acts = [a for a in activities if a.get("sport") == "running"]
        cutoff_14d = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
        # Find most recent run WITH dynamics data, within last 14 days
        last_run_with_dyn = None
        for a in reversed(run_acts):
            act_date = a.get("start_time", "")[:10]
            if act_date < cutoff_14d:
                break
            if any(a.get(k) for k in ["avg_steps_per_min", "avg_vertical_oscillation", "avg_ground_contact_time", "avg_vertical_ratio"]):
                last_run_with_dyn = a
                break
        if last_run_with_dyn:
            cadence = last_run_with_dyn.get("avg_steps_per_min")
            vo_osc = last_run_with_dyn.get("avg_vertical_oscillation")
            gct = last_run_with_dyn.get("avg_ground_contact_time")
            vr = last_run_with_dyn.get("avg_vertical_ratio")
            dyn_parts = []
            try:
                if cadence:
                    dyn_parts.append(f"каденс {float(cadence):.0f} шаг/мин")
                if vo_osc:
                    dyn_parts.append(f"верт.кол. {float(vo_osc):.0f} мм")
                if gct:
                    dyn_parts.append(f"GCT {float(gct):.0f} мс")
                if vr:
                    dyn_parts.append(f"верт.р. {float(vr):.1f}%")
            except (ValueError, TypeError):
                pass
            if dyn_parts:
                sport_prompt += f"\n\nДИНАМИКА (бег {last_run_with_dyn.get('start_time', '')[:10]}): {', '.join(dyn_parts)}"

        # Add HR zone distribution for current week — Garmin zones
        from datetime import date as _date, timedelta as _td
        today_d = _date.today()
        week_start = (today_d - _td(days=today_d.weekday())).isoformat()
        cur_week_acts = [a for a in run_acts if a.get("start_time", "") >= week_start]
        if cur_week_acts:
            week_secs = [0.0] * 5
            for a in cur_week_acts:
                gsecs = self._garmin_zone_secs(a)
                if gsecs:
                    for i in range(5):
                        week_secs[i] += gsecs[i]
            total_secs = sum(week_secs)
            if total_secs > 0:
                z123_pct = round((week_secs[0] + week_secs[1] + week_secs[2]) / total_secs * 100)
                z45_pct = 100 - z123_pct
                zone_breakdown = " / ".join(
                    f"Z{i+1} {round(week_secs[i]/60)}м"
                    for i in range(5) if week_secs[i] >= 60
                )
                sport_prompt += (
                    f"\n\nЗОНЫ ТЕКУЩЕЙ НЕДЕЛИ: Z1-Z3 {z123_pct}% / Z4-Z5 {z45_pct}%"
                    + (f" | {zone_breakdown}" if zone_breakdown else "")
                )

        if training_goal:
            sport_prompt += f"\n\nЦЕЛЬ АТЛЕТА: {training_goal}"

        # Dynamic weekly km target from race schedule
        dyn_target = metrics.get("weekly_km_target")
        dyn_label = metrics.get("weekly_km_target_label", "")
        if dyn_target:
            sport_prompt += f"\n\nЦЕЛЕВОЙ ОБЪЁМ ТЕКУЩЕЙ НЕДЕЛИ: {dyn_target} км ({dyn_label})"

        fitness_profile = metrics.get("fitness_profile")
        try:
            return await self._generate_text(
                method="sport_status",
                system_prompt=sport_system + self._user_context_block(fitness_profile, garmin_zone_boundaries=metrics.get("garmin_zones")),
                user_prompt=sport_prompt,
                max_tokens=1800,
            )
        except Exception as exc:
            logger.exception("Error during sport status analysis")
            return f"Не удалось получить спортивный статус: {exc}"

    async def analyze_progress(
        self,
        metrics: dict[str, Any],
        race_predictions: dict[str, str] | None = None,
        weight_history: list[dict] | None = None,
        personal_records: list[dict] | None = None,
        feelings_stats: dict | None = None,
        training_goal: str = "",
        upcoming_races: list[dict] | None = None,
        user_memory: str = "",
    ) -> str:
        """Generate a progress report toward the training goal."""
        parts = ["=== ОТЧЁТ О ПРОГРЕССЕ ===\n"]

        if training_goal:
            parts.append(f"ЦЕЛЬ: {training_goal}")

        # Upcoming races with countdown
        if upcoming_races:
            from datetime import date as _date
            today_d = _date.fromisoformat(metrics.get("date", _date.today().isoformat()))
            race_lines = ["ПРЕДСТОЯЩИЕ СТАРТЫ (день и счёт уже вычислены — не пересчитывай):"]
            for r in upcoming_races:
                dist = f" {r['distance_km']:.1f}км" if r.get("distance_km") else ""
                goal_t = f", цель {r['goal_time']}" if r.get("goal_time") else ""
                race_lines.append(
                    f"  {r['date']} — {r['name']}{dist}{goal_t} "
                    f"[до старта: {self._race_countdown(r['date'], today_d)}]"
                )
            parts.append("\n".join(race_lines))

        # Race predictions from VO2max
        if race_predictions:
            fp = metrics.get("fitness_profile") or {}
            vo2_val = fp.get("vo2_max") or 0
            pred_lines = [
                f"ПРОГНОЗ ФИНИША (модель Daniels/Gilbert, Garmin VO2max {vo2_val}):",
                "  (Garmin обычно завышает VO2max на 2-5 пунктов — реалистичный прогноз ниже)",
            ]
            for dist, time_str in race_predictions.items():
                pred_lines.append(f"  {dist}: {time_str}")
            # Adjusted prediction (VO2max - 3)
            if race_predictions and vo2_val:
                from .plan_builder import WeeklyPlanBuilder
                adjusted = WeeklyPlanBuilder.predict_race_times(vo2_val - 3)
                if adjusted:
                    pred_lines.append(f"  Скорректированный прогноз (VO2max {vo2_val - 3}, более реалистичный):")
                    for dist, time_str in adjusted.items():
                        pred_lines.append(f"    {dist}: {time_str}")
            parts.append("\n".join(pred_lines))

        # VO2max trend
        vo2_hist = metrics.get("vo2max_history") or []
        if len(vo2_hist) >= 2:
            vo2_hist_s = sorted(vo2_hist, key=lambda e: e["date"])
            first, last = vo2_hist_s[0], vo2_hist_s[-1]
            delta = round(last["vo2_max"] - first["vo2_max"], 1)
            peak = max(e["vo2_max"] for e in vo2_hist_s)
            recent = vo2_hist_s[-6:]
            pts = ", ".join(f"{e['date']}: {e['vo2_max']}" for e in recent)
            parts.append(
                f"VO2max: {first['vo2_max']} → {last['vo2_max']} ({'+' if delta >= 0 else ''}{delta})"
                f" | пик {peak} | последние: {pts}"
            )

        # CTL trend
        perf = metrics.get("fitness") or {}
        if perf.get("ctl") is not None:
            parts.append(f"ФОРМА: CTL {perf['ctl']}, ATL {perf.get('atl', '?')}, TSB {perf.get('tsb', '?')}")

        # Volume: pre-computed per week (do NOT let Claude sum activities)
        from datetime import date as _d, timedelta as _td
        activities = metrics.get("activities_28d") or []
        run_acts = [a for a in activities if a.get("sport") == "running"]
        if run_acts:
            today_d = _d.fromisoformat(metrics.get("date", _d.today().isoformat()))
            total_km_28 = sum(a.get("distance") or 0 for a in run_acts)
            vol_lines = ["ОБЪЁМ БЕГА (предвычислено, НЕ суммируй вручную):"]
            vol_lines.append(f"  Тотал 28 дней: {total_km_28:.1f} км / {len(run_acts)} пробежек")
            # Current calendar week: Mon → today
            cal_mon = today_d - _td(days=today_d.weekday())
            weeks = [
                ("Тек. нед. (Пн–сегодня)", cal_mon, today_d),
            ]
            # Previous 3 full calendar weeks (Mon–Sun)
            for i in range(1, 4):
                w_sun = cal_mon - _td(days=i * 7 - 6)  # Sunday of that week
                w_mon = w_sun - _td(days=6)             # Monday of that week
                weeks.append((f"Нед.{i}", w_mon, w_sun))
            for label, w_start, w_end in weeks:
                w_runs = [
                    a for a in run_acts
                    if w_start.isoformat() <= a.get("start_time", "")[:10] <= w_end.isoformat()
                ]
                w_km = sum(a.get("distance") or 0 for a in w_runs)
                w_secs = sum(self._time_str_to_secs(a.get("moving_time")) for a in w_runs)
                pace = f"{int(w_secs/w_km/60)}:{int((w_secs/w_km/60 % 1)*60):02d}" if w_km > 0 and w_secs > 0 else "?"
                vol_lines.append(
                    f"  {label} ({w_start.strftime('%d.%m')}–{w_end.strftime('%d.%m')}): "
                    f"{w_km:.1f} км / {len(w_runs)} пробежек / темп {pace}/км"
                )
            parts.append("\n".join(vol_lines))

        # Weight trend
        if weight_history and len(weight_history) >= 2:
            first_w = weight_history[0]
            last_w = weight_history[-1]
            delta_w = round(last_w["weight"] - first_w["weight"], 1)
            parts.append(
                f"ВЕС: {first_w['weight']:.1f} кг ({first_w['day']}) → "
                f"{last_w['weight']:.1f} кг ({last_w['day']}) = "
                f"{'+' if delta_w >= 0 else ''}{delta_w:.1f} кг"
            )

        # Personal records
        if personal_records:
            pr_lines = ["ЛИЧНЫЕ РЕКОРДЫ:"]
            for pr in personal_records:
                hr_str = f", пульс {pr['avg_hr']}" if pr.get("avg_hr") else ""
                pr_lines.append(
                    f"  {pr['distance']}: {pr['time']} ({pr['pace']}/км) — {pr['date']}{hr_str}"
                )
            parts.append("\n".join(pr_lines))

        # Feelings trend
        if feelings_stats and feelings_stats.get("count", 0) >= 3:
            trend_labels = {
                "improving": "улучшение",
                "declining": "ухудшение",
                "stable": "стабильно",
            }
            parts.append(
                f"САМОЧУВСТВИЕ: среднее {feelings_stats['avg']}/5 за {feelings_stats['count']} дней, "
                f"тренд: {trend_labels.get(feelings_stats['trend'], '?')}"
            )

        # Weeks summary for long-term trend
        weeks = metrics.get("weeks_summary") or []
        if weeks:
            parts.append(f"ТРЕНД 6 МЕСЯЦЕВ ({len(weeks)} недель):")
            parts.append("  Неделя       | ЧСС покоя | Сон     | Стресс | BB макс")
            for w in weeks[-12:]:
                rhr = f"{w['rhr_avg']:.0f}" if w.get("rhr_avg") else "?"
                sleep = "?" if not w.get("sleep_avg") else str(w["sleep_avg"]).split(".")[0]
                stress = f"{w['stress_avg']:.0f}" if w.get("stress_avg") else "?"
                bb = f"{w['bb_max']:.0f}" if w.get("bb_max") else "?"
                parts.append(f"  {w['first_day']}  | {rhr:>9} | {sleep:>7} | {stress:>6} | {bb:>7}")

        context = "\n\n".join(parts)

        progress_system = PROGRESS_SYSTEM
        fp = metrics.get("fitness_profile")
        try:
            return await self._generate_text(
                method="progress",
                system_prompt=progress_system + self._user_context_block(fp, garmin_zone_boundaries=metrics.get("garmin_zones")),
                user_prompt=context,
                max_tokens=2000,
                user_memory=user_memory,
            )
        except Exception as exc:
            logger.exception("Error during progress analysis")
            return f"Не удалось сгенерировать отчёт о прогрессе: {exc}"

    async def analyze_weekly_summary(
        self,
        metrics: dict[str, Any],
        plan_text: str = "",
        feelings_stats: dict | None = None,
        user_memory: str = "",
        food_entries: list[dict] | None = None,
        garmin_daily_calories: dict[str, dict] | None = None,
        weight_kg: float | None = None,
        week_activities: list[dict] | None = None,
        verified_facts: list[dict] | None = None,
        week_facts: "Any" = None,
    ) -> str:
        """Generate a weekly summary report."""
        from datetime import date as _date, timedelta as _td
        today = _date.fromisoformat(metrics.get("date", _date.today().isoformat()))
        # Резолв окна:
        # • Если запрос в понедельник — юзер ждёт «итог завершившейся недели»
        #   (прошлая Пн-Вс). Без этого фикса окно было «Пн-Пн», 0 тренировок.
        # • Иначе — текущая неделя Пн → сегодня включительно.
        if today.weekday() == 0:
            week_start = today - _td(days=7)
            week_end = today - _td(days=1)
            window_label = "ИТОГ ПРОШЛОЙ НЕДЕЛИ"
        else:
            week_start = today - _td(days=today.weekday())
            week_end = today
            window_label = "ИТОГ НЕДЕЛИ"

        parts = [
            f"=== {window_label} {week_start.strftime('%d.%m')}–{week_end.strftime('%d.%m')} ===\n"
        ]

        # Источник активностей: явный week_activities (если передан bot.py),
        # иначе фильтрация activities_28d из снапшота — оставлено как fallback.
        if week_activities is not None:
            activities = week_activities
        else:
            activities = metrics.get("activities_28d") or []
        week_acts = [
            a for a in activities
            if week_start.isoformat() <= a.get("start_time", "")[:10] <= week_end.isoformat()
        ]
        run_acts = [a for a in week_acts if a.get("sport") == "running"]

        # Summary stats
        total_km = sum(a.get("distance") or 0 for a in run_acts)
        total_tl = sum(a.get("training_load") or 0 for a in week_acts)
        total_runs = len(run_acts)
        total_sessions = len(week_acts)

        # Run time + avg HR
        run_secs = sum(self._time_str_to_secs(a.get("moving_time")) for a in run_acts)
        if run_secs > 0 and total_km > 0:
            avg_pace = run_secs / total_km / 60
            pace_str = f"{int(avg_pace)}:{int((avg_pace % 1) * 60):02d}"
        else:
            pace_str = "?"
        hr_vals = [a.get("avg_hr") for a in run_acts if a.get("avg_hr")]
        avg_hr_str = f", средний пульс {round(sum(hr_vals)/len(hr_vals))}" if hr_vals else ""

        parts.append(
            f"СТАТИСТИКА: {total_sessions} тренировок ({total_runs} пробежек), "
            f"бег {total_km:.1f} км, средний темп {pace_str}/км{avg_hr_str}, TL {total_tl:.0f}"
        )

        # Activities list
        if week_acts:
            act_lines = ["ТРЕНИРОВКИ:"]
            for a in week_acts:
                sport = a.get("sport", "?")
                dist = a.get("distance")
                tl = a.get("training_load")
                day = a.get("start_time", "?")[:10]
                name = a.get("name", sport)
                detail = []
                if dist:
                    detail.append(f"{dist:.1f}км")
                if a.get("avg_hr"):
                    detail.append(f"пульс {a['avg_hr']}")
                if tl:
                    detail.append(f"TL {tl:.0f}")
                act_lines.append(f"  {day} — {name}: {', '.join(detail)}")
            parts.append("\n".join(act_lines))

        # 80/20 balance — Garmin zone times directly
        if len(run_acts) >= 2:
            all_zone_secs = [0.0] * 5
            easy_sessions = 0
            for a in run_acts:
                gsecs = self._garmin_zone_secs(a)
                if gsecs:
                    total_s = sum(gsecs)
                    z123_s = gsecs[0] + gsecs[1] + gsecs[2]  # Z1-Z3 = aerobic in Garmin
                    for i in range(5):
                        all_zone_secs[i] += gsecs[i]
                    if total_s > 0 and z123_s / total_s >= 0.80:
                        easy_sessions += 1
            hard_sessions = len(run_acts) - easy_sessions
            zone_total = sum(all_zone_secs)
            if zone_total > 0:
                z123_pct = round((all_zone_secs[0] + all_zone_secs[1] + all_zone_secs[2]) / zone_total * 100)
                zone_detail = " / ".join(
                    f"Z{i+1} {round(all_zone_secs[i]/60)}м" for i in range(5) if all_zone_secs[i] >= 60
                )
                parts.append(
                    f"80/20 по сессиям: {easy_sessions} лёгких / {hard_sessions} интенсивных"
                    f" | Z1-Z3 {z123_pct}% времени"
                    + (f" ({zone_detail})" if zone_detail else "")
                )

        # Sleep average
        sleep_trend = metrics.get("sleep_trend_7d") or []
        if sleep_trend:
            scores = [s.get("score") for s in sleep_trend if s.get("score")]
            total_sleeps = [self._time_str_to_secs(s.get("total_sleep")) for s in sleep_trend if s.get("total_sleep")]
            if scores:
                parts.append(f"СОН: средний score {sum(scores)/len(scores):.0f}")
            if total_sleeps:
                avg_h = sum(total_sleeps) / len(total_sleeps) / 3600
                parts.append(f"  Среднее время сна: {avg_h:.1f}ч")

        # BB average
        daily_trend = metrics.get("daily_trend_7d") or []
        if daily_trend:
            bbs = [d.get("bb_max") for d in daily_trend if d.get("bb_max") is not None]
            if bbs:
                parts.append(f"BB средний за неделю: {sum(bbs)/len(bbs):.0f}")

        # CTL/TSB
        perf = metrics.get("fitness") or {}
        if perf.get("ctl") is not None:
            parts.append(f"ФОРМА: CTL {perf['ctl']}, TSB {perf.get('tsb', '?')}")

        # Feelings
        if feelings_stats and feelings_stats.get("count", 0) >= 2:
            parts.append(f"САМОЧУВСТВИЕ: среднее {feelings_stats['avg']}/5 за {feelings_stats['count']} дней")

        # Cached plan for comparison
        if plan_text:
            # Limit plan text to avoid bloating context
            parts.append(f"ПЛАН НА ЭТУ НЕДЕЛЮ (для сравнения):\n{plan_text[:800]}")

        # Nutrition summary for the week
        if food_entries:
            from collections import defaultdict
            by_day: dict[str, list] = defaultdict(list)
            for e in food_entries:
                by_day[e.get("date", "?")].append(e)

            total_cal = sum(e.get("calories", 0) for e in food_entries)
            total_p = sum(e.get("protein_g", 0) for e in food_entries)
            total_f = sum(e.get("fat_g", 0) for e in food_entries)
            total_c = sum(e.get("carbs_g", 0) for e in food_entries)
            days_logged = len(by_day)
            avg_cal = total_cal / days_logged if days_logged else 0

            nut_lines = [f"ПИТАНИЕ ЗА НЕДЕЛЮ ({days_logged} дн. из записей):"]
            nut_lines.append(
                f"  Среднее потребление: {avg_cal:.0f} ккал/день "
                f"(Б {total_p/days_logged:.0f}г  Ж {total_f/days_logged:.0f}г  У {total_c/days_logged:.0f}г)"
            )

            # Daily balance (intake vs Garmin expenditure)
            if garmin_daily_calories:
                balance_days = []
                for day_str, entries in sorted(by_day.items()):
                    day_cal_in = sum(e.get("calories", 0) for e in entries)
                    garmin = (garmin_daily_calories or {}).get(day_str)
                    if garmin and garmin.get("calories_total"):
                        bal = day_cal_in - garmin["calories_total"]
                        balance_days.append(bal)
                        nut_lines.append(
                            f"  {day_str}: {day_cal_in:.0f} съедено / "
                            f"{garmin['calories_total']:.0f} сожжено = "
                            f"{bal:+.0f} ккал"
                        )
                    else:
                        nut_lines.append(f"  {day_str}: {day_cal_in:.0f} съедено (расход Garmin н/д)")
                if balance_days:
                    avg_bal = sum(balance_days) / len(balance_days)
                    nut_lines.append(
                        f"  Средний баланс: {avg_bal:+.0f} ккал/день "
                        f"({'дефицит' if avg_bal < 0 else 'профицит'})"
                    )

            # ISSN targets
            if weight_kg and weight_kg > 0:
                p_min, p_max = round(weight_kg * 1.4), round(weight_kg * 1.7)
                c_min, c_max = round(weight_kg * 5), round(weight_kg * 7)
                f_min, f_max = round(weight_kg * 0.8), round(weight_kg * 1.2)
                avg_p = total_p / days_logged if days_logged else 0
                avg_c = total_c / days_logged if days_logged else 0
                avg_f = total_f / days_logged if days_logged else 0
                nut_lines.append(
                    f"  ISSN нормы ({weight_kg:.0f}кг): "
                    f"Б {avg_p:.0f}/{p_min}-{p_max}г, "
                    f"У {avg_c:.0f}/{c_min}-{c_max}г, "
                    f"Ж {avg_f:.0f}/{f_min}-{f_max}г"
                )

            parts.append("\n".join(nut_lines))

        context = "\n\n".join(parts)

        weekly_system = WEEKLY_SYSTEM
        fp = metrics.get("fitness_profile")
        facts_block = self._format_verified_facts_block(verified_facts)
        coach_block = (
            "\n\n📐 WEEK FACTS (источник истины, считал не ты):\n" + week_facts.to_prompt_block()
            if week_facts is not None else ""
        )
        try:
            return await self._generate_text(
                method="weekly_summary",
                system_prompt=(
                    weekly_system
                    + coach_block
                    + (("\n" + facts_block) if facts_block else "")
                    + self._user_context_block(fp, garmin_zone_boundaries=metrics.get("garmin_zones"))
                ),
                user_prompt=context,
                max_tokens=1500,
                user_memory=user_memory,
            )
        except Exception as exc:
            logger.exception("Error during weekly summary")
            return f"Не удалось сгенерировать итог недели: {exc}"

    async def ask(
        self,
        question: str,
        metrics: dict[str, Any] | None,
        history: list[dict] | None = None,
        user_memory: str = "",
        upcoming_races: list[dict] | None = None,
        training_goal: str = "",
        current_plan: str = "",
        current_week_type: str = "",
        db_paths: dict[str, str] | None = None,
        user_id: int | None = None,
        today_iso: str | None = None,
        save_plan_fn: Callable[[str, str], str] | None = None,
        write_tools: dict[str, Callable[..., str]] | None = None,
        verified_facts: list[dict] | None = None,
        morning_facts: "Any" = None,
        week_facts: "Any" = None,
    ) -> str:
        fitness_profile = None
        context_block = ""
        if metrics:
            fitness_profile = metrics.get("fitness_profile")
            context_block = self._format_metrics_light(metrics)
            # Recent activities with km_splits — immediately available without a tool call
            qa_acts = metrics.get("recent_activities_for_qa") or []
            if qa_acts:
                act_lines = ["\nПОСЛЕДНИЕ ТРЕНИРОВКИ (сплиты и детали):"]
                for a in qa_acts[:5]:
                    name = a.get("name") or a.get("sport", "?")
                    start = a.get("start_time", "?")
                    dist = a.get("distance")
                    avg_hr = a.get("avg_hr")
                    detail = []
                    if dist:
                        detail.append(f"{dist:.1f}км")
                    if avg_hr:
                        detail.append(f"пульс {avg_hr}")
                    if a.get("training_load"):
                        detail.append(f"TL {a['training_load']:.0f}")
                    act_lines.append(f"  {start} — {name}: {', '.join(detail)}")
                    km_splits = a.get("km_splits") or []
                    if km_splits:
                        split_parts = [
                            f"км{s.get('km')}: {s.get('pace')}" + (f" пульс {s['avg_hr']}" if s.get("avg_hr") else "")
                            for s in km_splits[:15]
                        ]
                        act_lines.append(f"    Сплиты: {' | '.join(split_parts)}")
                context_block += "\n".join(act_lines)

        # Structured user context
        extra_context = ""
        if training_goal:
            extra_context += f"\nЦЕЛЬ АТЛЕТА: {training_goal}"
        if upcoming_races:
            from datetime import date as _date
            today_d = _date.today()
            future_lines: list[str] = []
            past_lines: list[str] = []
            for r in upcoming_races:
                days_left = (_date.fromisoformat(r["date"]) - today_d).days
                dist = f" {r['distance_km']:.1f}км" if r.get("distance_km") else ""
                goal_t = f", цель {r['goal_time']}" if r.get("goal_time") else ""
                star = " ⭐" if r.get("is_priority") else ""
                if days_left >= 0:
                    future_lines.append(
                        f"  #{r.get('id','?')} {r['date']} — {r['name']}{dist}{goal_t}{star} "
                        f"({self._race_countdown(r['date'], today_d)})"
                    )
                else:
                    actual = r.get("actual_time") or "результат не указан"
                    note = f", {r['actual_notes']}" if r.get("actual_notes") else ""
                    past_lines.append(f"  #{r.get('id','?')} {r['date']} — {r['name']}{dist}: факт {actual}{note}")
            if future_lines:
                extra_context += (
                    "\nПРЕДСТОЯЩИЕ СТАРТЫ (день и счёт уже вычислены — "
                    "«завтра/послезавтра» бери из скобок, сам не пересчитывай):\n"
                    + "\n".join(future_lines)
                )
            if past_lines:
                extra_context += "\n\nНЕДАВНО ПРОБЕЖАЛ (структурный источник истины — не переспрашивай результат):\n" + "\n".join(past_lines)
        if current_plan:
            wt_label = f" (фаза: {current_week_type})" if current_week_type else ""
            extra_context += f"\nТЕКУЩИЙ ПЛАН НЕДЕЛИ{wt_label}:\n{current_plan}"
        facts_block = self._format_verified_facts_block(verified_facts)
        if facts_block:
            extra_context += "\n" + facts_block

        from datetime import date as _date, timedelta as _td
        _today = today_iso or _date.today().isoformat()
        _yd = (_date.fromisoformat(_today) - _td(days=1)).isoformat()
        _user_id_str = str(user_id) if user_id else "?"
        # STABLE — почти не меняется между вызовами одного юзера в течение дня:
        # роль + правила + инструменты + self-recognition + user_memory + user_id.
        # Этот блок пойдёт под cache_control: ephemeral, чтобы получать cache_read.
        stable_part = build_ask_stable_prompt(_user_id_str)
        # user_memory клеим в стабильную часть — он редко меняется в течение
        # сессии и должен попадать под кэш вместе с правилами.
        if user_memory:
            stable_part = (
                "Важная информация о пользователе (запомни навсегда):\n"
                f"{user_memory}\n\n" + stable_part
            )
        # Coach facts — детерминированные блоки. Если переданы — подавляют
        # право Claude пересчитывать те же величины.
        coach_block = ""
        if morning_facts is not None:
            coach_block += "\n\n" + morning_facts.to_prompt_block()
        if week_facts is not None:
            coach_block += "\n\n📐 WEEK FACTS:\n" + week_facts.to_prompt_block()

        # DYNAMIC — то, что меняется каждый день/вызов: даты, снапшот метрик,
        # цель/гонки/план, профиль атлета. Этот блок НЕ кэшируется.
        dynamic_part = (
            f"⚙️ КОНТЕКСТ ЗАПРОСА (динамика):\n"
            f"• Сегодня: {_today}\n"
            f"• Вчера: {_yd}\n"
            + coach_block
            + "\n\n"
            + context_block
            + extra_context
            + self._user_context_block(fitness_profile, garmin_zone_boundaries=(metrics or {}).get("garmin_zones"))
        )
        system: list[dict] = [
            {"type": "text", "text": stable_part, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
            {"type": "text", "text": dynamic_part},
        ]
        try:
            # Tools нужны не только для SQL: write-tools и invoke_action должны
            # работать и у юзера БЕЗ Garmin-БД. До 10.07.2026 условие было
            # «if db_paths» — свежий юзер получал QA вообще без tools, и модель
            # имитировала <tool_call> текстом, «сохраняя» вес/цели в никуда
            # (поймано первым прогоном scripts/run_evals.py).
            if db_paths or write_tools or save_plan_fn:
                return await self._ask_with_tools(
                    system=system,
                    question=question,
                    history=history,
                    user_memory=user_memory,
                    db_paths=db_paths or {},
                    user_id=user_id,
                    save_plan_fn=save_plan_fn,
                    write_tools=write_tools,
                )
            return await self._generate_text(
                method="ask",
                system_prompt=system,
                user_prompt=question,
                max_tokens=2000,
                history=history,
                user_memory=user_memory,
            )
        except Exception:
            logger.exception("Error during ask")
            return "Не удалось получить ответ — что-то пошло не так. Попробуй переформулировать вопрос."

    async def _ask_with_tools(
        self,
        system: str | list[dict],
        question: str,
        history: list[dict] | None,
        user_memory: str,
        db_paths: dict[str, str],
        user_id: int | None = None,
        save_plan_fn: Callable[[str, str], str] | None = None,
        write_tools: dict[str, Callable[..., str]] | None = None,
    ) -> str:
        """Ask with SQL tool use — Claude can query DB directly to answer questions.

        Безопасность SQL-инструмента:
        - все БД открываются read-only (mode=ro) — запись/PRAGMA writable_schema невозможны;
        - разрешены только SELECT и интроспективные PRAGMA TABLE_*;
        - общая app.db не отдаётся напрямую: для query_app_db строится in-memory
          копия ТОЛЬКО со строками текущего user_id и без таблиц-секретов
          (garmin_credentials, web_tokens). Изоляция пользователей — на уровне движка,
          а не текста промпта."""
        _run_sql = make_sql_runner(db_paths, user_id)
        tools = build_tool_schemas(save_plan_fn=save_plan_fn, write_tools=write_tools)

        # `system` может быть готовым списком блоков [stable+cache, dynamic]
        # — в этом случае user_memory уже вшит в stable и доп. префикс не нужен.
        if isinstance(system, list):
            system_block = system
        else:
            full_system = system
            if user_memory:
                full_system = (
                    "Важная информация о пользователе:\n"
                    f"{user_memory}\n\n{system}"
                )
            system_block = [{"type": "text", "text": full_system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}]

        base_messages: list[dict] = list(history or [])
        base_messages.append({"role": "user", "content": question})

        tool_call_count = 0
        last_exc: Exception | None = None
        # Ревью: фолбэк на другую модель начинал диалог ЗАНОВО — уже выполненные
        # write-tools (confirm_fact, add_race...) выполнялись повторно (двойные
        # записи). Если хоть один write-tool отработал — фолбэк запрещён.
        wrote_something = False

        for model in self._models:
            if wrote_something:
                break
            messages = list(base_messages)  # fresh copy per model
            try:
                while True:
                    response = await asyncio.to_thread(
                        self._client.messages.create,
                        model=model,
                        max_tokens=2000,
                        system=system_block,
                        tools=tools,
                        messages=messages,
                    )
                    self._report_usage("ask_tools", model, response)
                    if response.stop_reason == "tool_use":
                        tool_call_count += 1
                        if tool_call_count > 8:
                            # Лимит — но НЕ теряем диалог. Извлекаем текст из последнего
                            # ответа Claude (если есть) или возвращаем стандартное
                            # сообщение, чтобы юзер не получил RuntimeError при том,
                            # что часть tool-вызовов могла уже записать в БД.
                            logger.warning(
                                "Tool-use loop reached safety limit (>%d). Returning partial.",
                                8,
                            )
                            partial = [
                                b.text for b in response.content
                                if getattr(b, "type", "") == "text"
                            ]
                            partial_text = "\n".join(p for p in partial if p).strip()
                            if partial_text:
                                # Ревью: преамбула могла обещать шаги, которые не выполнены
                                return (partial_text
                                        + "\n\n⚠️ Я упёрся в лимит шагов — что-то из "
                                          "обещанного выше мог не успеть. Спроси ещё раз, "
                                          "если чего-то не хватает.")
                            return (
                                "Я сделал много запросов к данным, но не успел собрать ответ "
                                "за 8 итераций. Попробуй переформулировать вопрос точнее "
                                "или сузить период."
                            )
                        # Append assistant message with tool_use blocks
                        messages.append({"role": "assistant", "content": response.content})
                        # Execute each tool call
                        tool_results = []
                        for block in response.content:
                            if getattr(block, "type", "") == "tool_use":
                                if block.name in (write_tools or {}) or block.name == "save_weekly_plan":
                                    wrote_something = True
                                if block.name == "save_weekly_plan" and save_plan_fn is not None:
                                    plan_text = (block.input or {}).get("plan_text", "")
                                    week_type = (block.input or {}).get("week_type", "build")
                                    try:
                                        save_result = save_plan_fn(plan_text, week_type)
                                    except Exception as e:
                                        logger.warning("save_weekly_plan failed: %s", e)
                                        save_result = f"[ошибка сохранения: {e}]"
                                    logger.info("Tool save_weekly_plan week_type=%s plen=%d result=%r",
                                                week_type, len(plan_text), save_result)
                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": save_result,
                                    })
                                    continue
                                # Универсальный диспатч write-tools (confirm_fact,
                                # remember_note, forget_note, set_race_result, record_feeling).
                                if write_tools and block.name in write_tools:
                                    fn = write_tools[block.name]
                                    try:
                                        save_result = await call_write_tool(fn, block.input or {})
                                    except TypeError as e:
                                        save_result = f"[ошибка аргументов: {e}]"
                                    except Exception as e:
                                        logger.warning("Tool %s failed: %s", block.name, e)
                                        save_result = f"[ошибка сохранения: {e}]"
                                    logger.info("Tool %s input=%r result=%r",
                                                block.name, block.input, save_result)
                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": save_result,
                                    })
                                    continue
                                db_key = {
                                    "query_health_db": "garmin",
                                    "query_activities_db": "activities",
                                    "query_app_db": "app",
                                }.get(block.name, "")
                                sql_q = (block.input or {}).get("sql", "")
                                # SQLite (включая пересборку in-memory app-view) — в поток,
                                # чтобы не блокировать event loop на каждый tool-раунд
                                result = await asyncio.to_thread(_run_sql, db_key, sql_q)
                                logger.info("Tool %s sql=%r result_len=%d preview=%r",
                                            block.name, sql_q, len(result), result[:200])
                                MAX_TOOL_RESULT = 24000
                                if len(result) > MAX_TOOL_RESULT:
                                    truncated_content = (
                                        result[:MAX_TOOL_RESULT]
                                        + f"\n\n[ВНИМАНИЕ: результат обрезан — показано {MAX_TOOL_RESULT} из {len(result)} символов. "
                                        f"НЕ суммируй и НЕ перечисляй частичные данные — переформулируй SQL: "
                                        f"выбери только нужные колонки (без description/raw_response при подсчётах), "
                                        f"добавь агрегаты (SUM/COUNT) или сузь период.]"
                                    )
                                else:
                                    truncated_content = result
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": truncated_content,
                                })
                        # Если по какой-то причине ни один tool_use блок не дал
                        # результата — НЕ шлём пустой turn (запутает Claude и
                        # тратит вызов). Выйдем из цикла и попробуем выдать текст.
                        if tool_results:
                            messages.append({"role": "user", "content": tool_results})
                        else:
                            logger.warning("tool_use stop_reason but tool_results empty — exiting loop")
                            partial = [
                                b.text for b in response.content
                                if getattr(b, "type", "") == "text"
                            ]
                            return "\n".join(p for p in partial if p).strip() or (
                                "Не удалось обработать запрос. Попробуй переформулировать."
                            )
                    else:
                        # end_turn or max_tokens — extract text
                        text_parts = [
                            b.text for b in response.content
                            if getattr(b, "type", "") == "text"
                        ]
                        return "\n".join(p for p in text_parts if p).strip()
                break
            except Exception as exc:
                last_exc = exc
                logger.warning("Tool-use ask failed on model %s: %s", model, exc)

        if last_exc:
            raise last_exc
        raise RuntimeError("No Anthropic models available")

    async def analyze(self, metrics: dict[str, Any], history: list[dict] | None = None, user_memory: str = "", verified_facts: list[dict] | None = None, morning_facts: "Any" = None) -> str:
        user_prompt = self._format_metrics(metrics)
        facts_block = self._format_verified_facts_block(verified_facts)
        coach_block = (
            "\n\n" + morning_facts.to_prompt_block() + "\n"
            if morning_facts is not None else ""
        )
        system = (
            SYSTEM_PROMPT
            + coach_block
            + (("\n" + facts_block) if facts_block else "")
            + self._user_context_block(metrics.get("fitness_profile"), garmin_zone_boundaries=metrics.get("garmin_zones"))
        )
        try:
            return await self._generate_text(
                method="morning",
                system_prompt=system,
                user_prompt=user_prompt,
                max_tokens=2000,
                history=history,
                user_memory=user_memory,
            )
        except Exception as exc:
            logger.exception("Unexpected error during AI analysis")
            return f"Не удалось получить анализ: {exc}"


















