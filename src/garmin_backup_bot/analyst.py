from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any, Callable

from anthropic import Anthropic

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Ты — профессиональный тренер по бегу и здоровью, ведёшь любителя.
Задача: ежедневный брифинг — оцени состояние и дай конкретное задание на сегодня.

МЕТОДОЛОГИЯ (используй эти пороги при анализе):
• HRV: выше базы = восстановлен, допустима нагрузка; ниже базы / UNBALANCED = снизить нагрузку
• BB (bb_max = уровень 0-100, bb_charged = дельта за ночь — не путать):
  75-100 = любая нагрузка; 50-74 = умеренная; 25-49 = только лёгкая; <25 = отдых
• Сон: <7ч или score <70 = неполное восстановление (recovery=caution уже выставлен кодом если есть данные)
• SpO2 ночью: <95% = нарушение дыхания во сне или высокогорье
• Training Load (TL) Garmin = аэробный TE × длительность. НЕ учитывает анаэробную нагрузку (Z5, спринты)
• CTL (хроническая нагрузка, 42д) = тренированность
• Все ЧИСЛОВЫЕ ПОРОГИ (RHR, deep sleep, REM, RR, TSB, ACWR, BB, HRV) применены в КОДЕ. Готовый вердикт — в блоке MORNING FACTS (recovery: good/caution/poor/alarm + drivers). НЕ ПЕРЕСЧИТЫВАЙ пороги сам, бери из MORNING FACTS
• ЗОНЫ GARMIN (5-зонная модель — границы в профиле спортсмена):
  Z1 = разминка/восстановление (очень лёгкий), Z2 = лёгкая (восстановительный бег),
  Z3 = АЭРОБНАЯ (основная зона для базовых/лёгких пробежек!), Z4 = пороговая (темповый бег), Z5 = анаэробная (интервалы/макс)
  ВАЖНО: лёгкий/базовый бег у Garmin = Z3 (не Z2 как в классической модели). Рекомендуй лёгкий бег в Z3!
• 80/20 (Seiler): ~80% СЕССИЙ (не времени) в Z1-Z3 (аэробная), ~20% интенсивных. Зоны пульса — с часов Garmin (как в Garmin Connect)
• Адаптация = отдых, не тренировка. Смело рекомендуй паузу когда данные говорят об этом
• Субъективное самочувствие (1-5): если ≤2 два дня подряд ИЛИ в данных есть [СИГНАЛ_ПЕРЕГРУЗКИ] — рекомендуй обязательный отдых, тренировку не назначай
• Температура тренировки >27°C: ЧСС на 5-10 уд/мин выше нормы — это норма. ОБЯЗАТЕЛЬНО скорректируй: если >27°C, сдвигай верхние границы зон на +7 уд/мин при оценке. Z2 при жаре может показать Z3 — это нормально, не критикуй
• Cardiac drift >5% (ЧСС растёт при стабильном темпе) = недовосстановление или жара (если >27°C — скорее жара, не тревога)
• Если в данных есть ПИТАНИЕ — оценивай влияние на восстановление: дефицит >800 ккал к нагрузке = неполное восстановление, мало углей (<3 г/кг) перед качественной = энергии не хватит, очень низкий белок (<1 г/кг) = мышцы не восстановятся. Упоминай питание ТОЛЬКО если есть конкретный сигнал — не дублируй цифры
• Частота дыхания (avg_rr): если [RR_РОСТ] в данных — это ранний маркер болезни/перегрузки, рекомендуй снизить нагрузку

ФОРМАТ — строго в таком порядке, без отступлений:

🌅 [Хорошая / Осторожно / Отдых] — [одна фраза: почему именно так]

⚡️ BB [bb_max]/100, HRV [last_night] мс / 7д [weekly] мс ([статус], база [low]–[high]), RHR [уд/мин]
   [⚠️ только если avg_rr вырос >2 вд/мин ИЛИ SpO2 <95% — одна строка]

💡 Сегодня: [тип тренировки] [объём] в [зона] ([NNN–NNN уд/мин]), темп ~[X:XX]/км
   [одна строка: питание или восстановление — только если есть конкретный повод]

😴 Сон: [ч:мм], score [N], глуб [X]%/REM [X]%. [причина если score <70 или <7ч]

📈 7 дней: RHR [↑↓—], BB [↑↓—], сон [↑↓—], ЧД [↑↓— вд/мин]
   Бег 7д: [ИТОГО БЕГ 7Д] км [темп]/км vs пред. [ИТОГО БЕГ 7Д ПРЕД] км
   Нед.: [ИТОГО НЕДЕЛЯ] км vs пред. [ИТОГО НЕДЕЛЯ ПРЕД] км | CTL [N] TSB [+/-N]

ПРАВИЛА ТОНА:
• Проблему обозначай символом ⚠️ — один раз, без восклицательных знаков и заглавных букв
• Никаких "КРИТИЧНО", "ТРЕВОЖНЫЙ СИГНАЛ", "ВНИМАНИЕ!!!" — это утренний брифинг, не скорая помощь
• НЕ используй markdown-форматирование (**, ##, __ и т.п.)
• Дни недели берёшь ТОЛЬКО из раздела КАЛЕНДАРЬ в данных — не вычисляй самостоятельно
• Километраж бега берёшь ТОЛЬКО из меток [ИТОГО БЕГ 28Д], [ИТОГО БЕГ 7Д] и [ИТОГО НЕДЕЛЯ] в данных — не суммируй активности вручную
• Тренировки за СЕГОДНЯ берёшь ТОЛЬКО из метки [СЕГОДНЯ ДД.ММ] — если там написано "пробежек НЕТ", значит сегодня тренировки не было, не придумывай
• Максимум 800 символов — уложись в один экран телефона

ЭТАЛОННЫЙ ПРИМЕР (именно такой объём и тон):

🌅 Хорошая — HRV в норме, BB высокий, сон качественный.

⚡️ BB 82/100, HRV 61 мс / 7д 63 мс (BALANCED, база 55–70), RHR 52

💡 Сегодня: лёгкий бег 8–10 км в Z3 (115–135 уд/мин), темп ~5:50/км
   Ужин — белок + углеводы до 20:00, завтра силовая.

😴 Сон: 7:12, score 74, глуб 21%/REM 23%.

📈 7 дней: RHR ↓, BB —, сон ↑, ЧД —
   Бег: 38 км 5:45/км vs пред. 32 км | CTL 42 TSB +3
"""


class HealthAnalyst:
    def __init__(
        self,
        api_key: str,
        model: str,
        fallback_models: list[str] | None = None,
        user_age: int = 35,
        weekly_km_target: float = 0.0,
    ) -> None:
        self._client = Anthropic(api_key=api_key)
        candidates = [model, *(fallback_models or [])]
        self._models = list(dict.fromkeys(m for m in candidates if m))
        self._user_age = user_age
        self._weekly_km_target = weekly_km_target
        # Tanaka formula: more accurate for middle-aged than 220-age
        self._hr_max = round(208 - 0.7 * user_age)
        self._hr_zones = {
            "Z1": (0, round(self._hr_max * 0.60)),
            "Z2": (round(self._hr_max * 0.60), round(self._hr_max * 0.70)),
            "Z3": (round(self._hr_max * 0.70), round(self._hr_max * 0.80)),
            "Z4": (round(self._hr_max * 0.80), round(self._hr_max * 0.90)),
            "Z5": (round(self._hr_max * 0.90), self._hr_max),
        }

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

    async def _generate_text(
        self,
        system_prompt: str | list[dict],
        user_prompt: str,
        max_tokens: int,
        history: list[dict] | None = None,
        user_memory: str = "",
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
            system_block = [{"type": "text", "text": full_system, "cache_control": {"type": "ephemeral"}}]

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
                # Cardiac drift: compare avg HR of first third vs last third of run
                # Skip first km (HR still rising) and last km (may include cooldown)
                if len(km_splits) >= 5:
                    body = km_splits[1:-1]  # exclude warmup km and final km
                    n = len(body)
                    first_hrs = [s["avg_hr"] for s in body[:n // 3] if s.get("avg_hr")]
                    last_hrs = [s["avg_hr"] for s in body[n * 2 // 3:] if s.get("avg_hr")]
                    if first_hrs and last_hrs:
                        drift = (sum(last_hrs) / len(last_hrs)) - (sum(first_hrs) / len(first_hrs))
                        drift_pct = drift / (sum(first_hrs) / len(first_hrs)) * 100
                        # Use API weather temp if available, device sensor as fallback (ignore 127.0)
                        _eff_temp = w_temp if w_temp is not None else (avg_temp if avg_temp != 127.0 else None)
                        if drift_pct > 5:
                            if _eff_temp is not None and _eff_temp > 27:
                                drift_flag = " ⚠️ >5% — скорее жара (темп >27°C)"
                            elif _eff_temp is not None and _eff_temp < 20:
                                drift_flag = " ⚠️ >5% — недовосстановление (не жара, темп <20°C)"
                            else:
                                drift_flag = " ⚠️ >5% — недовосстановление или жара"
                        else:
                            drift_flag = ""
                        lines.append(f"   Cardiac drift: {drift_pct:+.1f}%{drift_flag}")
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

        workout_system = """\
Ты — профессиональный тренер по бегу, анализируешь тренировку любителя.

🚨 ЖЁСТКОЕ ПРАВИЛО ИЗОЛЯЦИИ ДАННЫХ:
• Разбираешь ТОЛЬКО активность №1 (помечена «⟵ ОСНОВНАЯ»). Все её цифры берёшь только из её блока.
• Активности №2+ — это контекст для трендов и недельного объёма, НЕ для разбора сегодня.
• ЗАПРЕЩЕНО суммировать/усреднять поля разных активностей (Z1-Z5, TL, пульс, темп, дистанция).
  Если поле есть в №1 — бери оттуда; если нет — пиши «нет данных», НЕ ВЫДУМЫВАЙ и НЕ БЕРИ из №2+.
• «Зоны: Z1 Xм / Z2 Xм …» — это итог по ОДНОЙ тренировке, не за неделю.
• Недельный объём — ТОЛЬКО из метки [ИТОГО НЕДЕЛЯ Пн DD.MM–DD.MM], не складывай из списка.

МЕТОДОЛОГИЯ:
• 🛰️ GPS-АНОМАЛИИ детектируются КОДОМ. Приходят в WORKOUT FACTS / WEEK FACTS как готовый
  список gps_anomalies — ИСПОЛЬЗУЙ их фразу как есть, не пересчитывай. Не наказывай юзера
  за «слишком быстрый темп», если в gps_anomalies стоит флаг.
• Cardiac drift пороги применены в коде (cardiac_drift_pct в WORKOUT FACTS). Если >5% и температура <20°C — недовосстановление. Если >27°C — жара. Никогда не упоминай жару при <20°C
• ЗОНЫ GARMIN (5-зонная модель — границы в профиле спортсмена):
  Z1 = разминка/восстановление, Z2 = лёгкая, Z3 = АЭРОБНАЯ (основная зона лёгкого/базового бега!), Z4 = пороговая, Z5 = анаэробная
  ВАЖНО: лёгкий бег у Garmin = Z3 (не Z2 как в классической модели). Если большая часть времени в Z3 при лёгком беге — это НОРМАЛЬНО и ХОРОШО
• Каденс зависит от темпа: темп >6:00/км → норма 155-165; темп 5:00-6:00/км → 162-172; темп <5:00/км → 170-180. НЕ критикуй каденс 157-165 при лёгком беге медленнее 6:00/км
• TL (Garmin) = аэробный TE × длительность — НЕ учитывает анаэробную нагрузку. TL >100 = тяжёлая нагрузка, нужен день отдыха. TE аэр: 3-4 = развитие, 5 = перегрузка
• self_eval_feel (Garmin): Strong=хорошо, Normal=нормально, Weak=плохо; self_eval_effort: Maximum/Hard/Moderate/Light/Minimum — субъективная оценка атлета после тренировки
• Беговая динамика — нормы: каденс зависит от темпа (>6:00/км → 155-165 норма, 5-6 мин/км → 162-172, <5:00/км → 170-180), верт.кол. <80мм (>90 = избыточно), GCT <250мс (>280 = слабое отталкивание), верт.р. <8% (>10% = неэффективно)
• Если есть данные динамики — обязательно оцени: высокий VO + высокий GCT = тяжёлые ноги/усталость; GCT/скорость: падение GCT при росте темпа = хорошая экономичность; нестабильный каденс по сплитам = утомление мышц
• Лапы (поле "Лапы" в данных) — реальная структура тренировки от Garmin. Если есть метка [ИТОГО ИНТЕРВАЛОВ: N] — используй именно это число как количество рабочих отрезков, не считай сам. Анализируй: стабильность темпа по интервалам (разброс <5сек = хорошо), динамику ЧСС (растёт к концу = усталость/недовосстановление)
• 80/20 ВЕРДИКТ — в WEEK FACTS: z1_z3_pct + z1_z3_verdict (in_band/below/above/unknown_phase). Если verdict=above или in_band для build/peak — НЕ критикуй интенсивность; below — поляризация хорошая, можно добавлять качество. Если unknown_phase — не выноси вердикт по соотношению
• Рост объёма >10%/нед = риск перегрузки (правило 10%)
• Объём недели берёшь ТОЛЬКО из метки [ИТОГО НЕДЕЛЯ Пн DD.MM–DD.MM] — это календарная неделя (Пн–сегодня), не суммируй из списка тренировок сам
• VO2max — показатель аэробного потолка; темп на уровне VO2max = 3-5 мин интенсивность
• Жара >27°C: ЧСС на 5-10 уд/мин выше нормы — сдвигай верхние границы зон на +7 уд/мин. При температуре <20°C жара вообще не при чём — не упоминай её

ФОРМАТ — строго в таком порядке:

🏅 [вердикт одной фразой: тип нагрузки + качество]

📊 ЧСС [ср]/[макс], темп [X:XX]/км, TL [N], TE [аэр]/[анаэр]
   Зоны: Z1 [мин] / Z2 [мин] / Z3 [мин] / Z4 [мин] / Z5 [мин]
   [cardiac drift — только если >5%, одна строка]

🔢 Лапы (если есть в данных — показывай структуру тренировки):
лап | дист | время | темп | ЧСС
[данные строками — каждый лап = один отрезок (интервал/восстановление/разминка)]
[одна строка: вывод по интенсивным лапам — средний темп, ЧСС, стабильность]

🔢 Сплиты по км (если нет лапов или тренировка базовая):
км | темп  | ЧСС | кад
[данные строками без комментариев к каждому км]
[одно-два наблюдения только если есть явная аномалия — резкий рост ЧСС, провал темпа]

📅 Неделя (Пн–сегодня): [ИТОГО НЕДЕЛЯ] км, TL [сумма]. [одна фраза про 80/20 баланс]

😴 Восстановление: [N часов/дней]. [одно конкретное действие]

📅 План vs факт: [только если есть план — одна фраза: выполнено / перевыполнено / не то]

➡️ Следующая: [тип + зона + объём + когда]

ПРАВИЛА ТОНА:
• Проблему обозначай ⚠️ — один раз, без восклицательных знаков и заглавных букв
• Каждый факт упоминается ровно один раз — не повторяй одни цифры в разных блоках
• НЕ используй markdown (**, ##, __ и т.п.)
• Лимит строго 3500 символов — лучше меньше, лишнее отрезает Telegram

ЭТАЛОННЫЙ ПРИМЕР:

🏅 Аэробный длинный бег, ровно выполнен — хорошая работа в Z3 (аэробная).

📊 ЧСС 148/167, темп 5:42/км, TL 89, TE 3.2/0.5
   Зоны: Z1 0 / Z2 4 / Z3 38 / Z4 12 / Z5 3 мин
   Cardiac drift +4% — в норме.

🔢 Сплиты:
км | темп  | ЧСС | кад
1  | 5:55  | 141 | 172
2  | 5:48  | 146 | 174
…
10 | 5:38  | 156 | 176
Последние 3 км темп вырос при стабильном ЧСС — хороший финишный задел.

📅 Неделя: 41 км, TL 210. 80% объёма в Z1-Z3 — норма.

😴 Восстановление: 24 часа. Белок + сон 8ч.

➡️ Следующая: интервалы 6×1 км в Z4 послезавтра.
"""
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
        plan_system = """\
Ты — профессиональный тренер по бегу. Составь план тренировок на текущую неделю.

МЕТОДОЛОГИЯ:
• Тип недели определяет объём и интенсивность:
  - Разгрузочная (Recovery): 60% от обычного объёма, только Z1-Z3, никаких интервалов
  - Базовая (Base): 100% объёма, 80% сессий в Z1-Z3, 1 качественная тренировка
  - Развивающая (Build): +5-8% объёма, 80/20 по сессиям, 1-2 качественных тренировки
  - Пиковая (Peak): 100% объёма, специфичные для дистанции работы (см. СПЕЦИФИКА ДИСТАНЦИИ)
  - Тейпер: снижение объёма по протоколу, сохранить 1 короткую интенсивную работу для остроты
• Темпы — РАСЧЁТНЫЕ (VDOT) как ориентир, РЕАЛЬНЫЕ как коррекция. Если реальный > VDOT на 15+сек/км → атлет не готов
• ЗАПРЕТ КЛАСТЕРОВ: НИКОГДА не ставь 2 тяжёлых дня подряд (TL >80 или Z4-Z5 работы). После каждой качественной/тяжёлой тренировки — обязательно лёгкий день (Z1-Z3) или отдых. Это ЖЁСТКОЕ правило, нарушение = травма
• Длинный бег — воскресенье или суббота, не после качественной тренировки
• Не планировать интенсивные тренировки в Recovery/Taper-неделю
• [СИГНАЛ_ПЕРЕГРУЗКИ] = ЖЁСТКОЕ ПРАВИЛО: если в контексте есть этот тег — ТОЛЬКО Z1-Z3, НИКАКИХ интервалов/темповых, объём строго по коэффициенту. Безопасность > прогресс
• Специфичность: если в данных есть СПЕЦИФИКА ДИСТАНЦИИ — используй рекомендованные типы тренировок
• 80/20 по Seiler: 80% СЕССИЙ лёгких (Z1-Z3), 20% интенсивных (Z4-Z5) — не путать с % времени
• ЗОНЫ GARMIN (5-зонная модель — границы в профиле спортсмена):
  Z1 = разминка/восстановление, Z2 = лёгкая, Z3 = АЭРОБНАЯ (основная зона лёгкого/базового бега!), Z4 = пороговая, Z5 = анаэробная
  ВАЖНО: лёгкий бег у Garmin = Z3 (не Z2). Когда назначаешь лёгкий бег — пиши "Z3 (аэробная)" с конкретным диапазоном пульса
• Правило 10%: рост объёма не более 10%/нед или +5 км (что меньше). Не превышай коэффициент объёма. Если указан беговой стаж <1 года — макс +5%/нед
• Cross-training: если в данных есть силовые или другие тренировки — не ставь тяжёлый бег на следующий день после тяжёлой силовой (TL >60). Учитывай общий TL, не только беговой
• Количество беговых сессий в неделю: если в профиле указаны доступные дни — ставь бег ТОЛЬКО на эти дни. Иначе — столько, сколько атлет реально бегает (см. "сессий/нед" в данных). Не добавляй +2 сессии за неделю
• Длинный бег: не назначай дистанцию длиннее чем "макс. длинный бег" из данных + 2 км. Если есть раздел ДЛИННЫЙ БЕГ — используй рекомендацию оттуда
• Ограничения по времени: если в профиле указана макс. длительность тренировки — не превышай её. Рассчитывай дистанцию по реальному лёгкому темпу × макс. время
• Травмы: если в профиле указаны травмы/ограничения — обязательно учитывай их (не назначай запрещённые нагрузки)
• Восстановление после гонки — ПРОГРЕССИВНЫЙ ПРОТОКОЛ:
  Фаза 1 (первая половина восстановления): ТОЛЬКО Z1-Z2, короткие лёгкие пробежки или ходьба, никаких интервалов/темповых
  Фаза 2 (вторая половина): постепенный выход в Z3 (аэробный), объём до 60% обычного, без Z4-Z5 до полного восстановления
  Фаза указана в данных (если тип "recovery" из-за гонки). Строго следуй фазе — не давай Z3 в фазе 1
• TE баланс: если в данных есть TE за 14 дней — следи чтобы стимулирующих (≥3.5) не было >30% от всех сессий. Если больше — убери одну качественную тренировку
• Cardiac drift: если в данных отмечен drift >5% — это маркер недовосстановления. Снизь объём на 10-15%, замени интенсивную тренировку на лёгкую
• Экономичность бега: если тренд снижается — добавь 1-2 страйда/ускорения в конце лёгких пробежек (6×20с). Если улучшается — текущий подход работает, не меняй
• Качественные сессии: если в данных есть раздел КАЧЕСТВЕННЫЕ СЕССИИ — используй для планирования разнообразия (не дублируй тот же тип работы подряд, чередуй темпо и VO2max)
• Корректировка: если в заметках есть [КОРРЕКТИРОВКА ПЛАНА от пользователя] — это прямой запрос атлета. ОБЯЗАТЕЛЬНО учти его при составлении плана, даже если это отклоняется от стандартной методологии (в разумных пределах безопасности)
• Погода: если в датах недели указана температура — учитывай её:
  - Жара >27°C: рекомендуй бег рано утром или вечером, снизь темп на 10-20 сек/км, добавь напоминание про гидратацию
  - Жара >32°C: только лёгкий бег в Z1-Z3 или перенос тренировки, предупреди об опасности
  - Мороз <-10°C: рекомендуй утепление, дыхание через бафф, осторожно на скользком
  - Дождь/ливень: нормально для бега, но скользко — осторожнее на поворотах
  - Сильный ветер >40 км/ч: рекомендуй маршрут с укрытием или перенос интервалов

ФОРМАТ — строго без отступлений:

📅 ПЛАН НА НЕДЕЛЮ — [Тип]: [название на русском]
Цель: [одна конкретная фраза]

Пн DD.MM: [тип + объём + зона/темп]
Вт DD.MM: ...
Ср DD.MM: ...
Чт DD.MM: ...
Пт DD.MM: ...
Сб DD.MM: ...
Вс DD.MM: ...

Итого: ~X км, TL ~N
80/20: X км Z1-Z3 / Y км Z4-Z5

ПРАВИЛА:
• НЕ используй markdown (**, ## и т.п.)
• Каждый день — одна строка: тип тренировки + конкретный объём + зона пульса или темп
• Отдых пишется просто: "Отдых или лёгкая растяжка"
• Для дней с отметкой ✅ в данных — пиши "уже выполнено" + факт из данных (км). Для дней без ✅ — НИКОГДА не пиши "уже выполнено", даже если дата в прошлом
• Максимум 1500 символов
"""
        try:
            return await self._generate_text(
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
        sport_system = """\
Ты — тренер по бегу. Дай краткий отчёт о спортивном статусе атлета.

ФОРМАТ — строго без отступлений:

🏅 СПОРТ СТАТУС — [дата]

📊 Объёмы:
  Тотал 28 дней: [км] / [тренировок]
  [таблица 4 недели: неделя / км / треней / темп / пульс]

📈 VO2max: [значение] — [тренд за 3 месяца стрелкой ↑↓—]

⚡️ Форма: CTL [N] / ATL [N] / TSB [+/-N] — [1 фраза: свеж/нагружен/пик/восстановление]

🦵 Динамика бега (если есть метка ДИНАМИКА в данных — не старше 14 дней):
[каденс с оценкой нормы по темпу, верт.кол., GCT, верт.р.; если метки ДИНАМИКА нет — пропусти этот раздел]

🎯 80/20 (текущая неделя): [% Z1-Z3 аэроб] / [% Z4-Z5 интенсив] — [в норме / нарушен]

[если есть цель — одна строка: "До старта X недель / прогресс по объёму: Y%"]

💬 Вывод: [1-2 фразы: что хорошо, на что обратить внимание]

ПРАВИЛА:
• НЕ используй markdown (**, ## и т.п.)
• Максимум 1200 символов
• Если данных нет — пропусти раздел

КАДЕНС — нормы по темпу (не критикуй если в диапазоне):
  темп >6:00/км → норма 155–165 шаг/мин
  темп 5:00–6:00/км → норма 162–172 шаг/мин
  темп <5:00/км → норма 170–180 шаг/мин

ЗОНЫ — данные с часов Garmin (5-зонная модель), без пересчёта.
Z1 = разминка, Z2 = лёгкая, Z3 = АЭРОБНАЯ (основная зона базового бега!), Z4 = порог, Z5 = анаэробная.
Z1-Z3 — лёгкая аэробная работа (цель ≥80% сессий), Z4-Z5 — интенсив (порог/VO2max).

СИГНАЛЫ — если в данных есть:
  [VO2MAX_DROP] — обязательно упомяни возможную перетренировку или болезнь
  TSB < -25 — состояние перегрузки, рекомендуй разгрузку
  TSB > +10 — хорошая форма, оптимально для гонки или тяжёлой тренировки
  ACWR 1.2-1.5 — повышенная нагрузка, не наращивать объём
  ACWR > 1.5 — острая перегрузка, нужна разгрузочная неделя
  ACWR < 0.8 — мало стимула, можно добавлять нагрузку
"""
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
            race_lines = ["ПРЕДСТОЯЩИЕ СТАРТЫ:"]
            for r in upcoming_races:
                race_date = _date.fromisoformat(r["date"])
                days_left = (race_date - today_d).days
                dist = f" {r['distance_km']:.1f}км" if r.get("distance_km") else ""
                goal_t = f", цель {r['goal_time']}" if r.get("goal_time") else ""
                race_lines.append(f"  {r['date']} — {r['name']}{dist}{goal_t} [{days_left} дней]")
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

        progress_system = """\
Ты — профессиональный тренер по бегу. Составь отчёт о прогрессе атлета.

ФОРМАТ:

📈 ПРОГРЕСС — [дата]

🎯 Цель: [цель атлета]
   [оценка: на сколько % готов / сколько осталось]

🏁 Прогноз финиша (по VO2max):
[таблица: дистанция / время / vs цель]

⚡️ Форма: CTL [N] — [тренд за месяц]
   VO2max: [значение] — [тренд]

📊 Объём: [28д итого] км
   [тренд: рост/стабильно/снижение]

⚖️ Вес: [текущий] кг — [тренд за 3 мес]

🏆 Личные рекорды:
[дистанция: время (дата)]

😊 Самочувствие: [среднее]/5 — [тренд]

💬 Вывод: [2-3 фразы: что идёт хорошо, что улучшить, прогноз]

ПРАВИЛА:
• НЕ используй markdown (**, ## и т.п.)
• Если есть цель гонки + прогноз — обязательно сравни прогноз с целью
• Объёмы бега бери ТОЛЬКО из предвычисленных данных — НЕ суммируй активности вручную
• Максимум 2000 символов
"""
        fp = metrics.get("fitness_profile")
        try:
            return await self._generate_text(
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

        weekly_system = """\
Ты — профессиональный тренер по бегу. Составь краткий итог недели.

ФОРМАТ:

📋 ИТОГ НЕДЕЛИ [ДД.ММ–ДД.ММ]

📊 Объём: [X] км / [N] тренировок, TL [N]
   Темп средний: [X:XX]/км
   80/20: [N] лёгких / [M] интенсивных — [в норме / нарушен]

😴 Восстановление: сон [X.X]ч (score [N]), BB [N]

🍽 Питание: [только если ПИТАНИЕ ЗА НЕДЕЛЮ есть в данных — средний калораж, баланс дефицит/профицит, оценка БЖУ vs ISSN нормы; если данных о питании нет — пропусти эту секцию]

📅 План vs факт: [только если ПЛАН НА ЭТУ НЕДЕЛЮ есть в данных — сравни конкретные тренировки: что запланировано vs что выполнено; если плана нет — пропусти эту строку]

⚡️ Ключевые моменты:
[2-3 пункта: лучшая тренировка, наблюдения, проблемы]

➡️ Рекомендация на следующую неделю: [1-2 фразы]

ПРАВИЛА:
• НЕ используй markdown (**, ## и т.п.)
• Объёмы бери ТОЛЬКО из строки СТАТИСТИКА — НЕ суммируй тренировки вручную
• Факты из данных, не выдумывай
• Максимум 1200 символов
"""
        fp = metrics.get("fitness_profile")
        facts_block = self._format_verified_facts_block(verified_facts)
        coach_block = (
            "\n\n📐 WEEK FACTS (источник истины, считал не ты):\n" + week_facts.to_prompt_block()
            if week_facts is not None else ""
        )
        try:
            return await self._generate_text(
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
                    future_lines.append(f"  #{r.get('id','?')} {r['date']} — {r['name']}{dist}{goal_t}{star} (через {days_left} дн.)")
                else:
                    actual = r.get("actual_time") or "результат не указан"
                    note = f", {r['actual_notes']}" if r.get("actual_notes") else ""
                    past_lines.append(f"  #{r.get('id','?')} {r['date']} — {r['name']}{dist}: факт {actual}{note}")
            if future_lines:
                extra_context += "\nПРЕДСТОЯЩИЕ СТАРТЫ:\n" + "\n".join(future_lines)
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
        stable_part = (
            "Ты — персональный тренер и health-ассистент. У тебя есть прямой доступ к базе данных Garmin "
            "пользователя через SQL-инструменты.\n\n"
            "⚙️ КОНТЕКСТ ЗАПРОСА (стабильная часть):\n"
            f"• user_id текущего пользователя: {_user_id_str} — ВСЕГДА используй этот user_id в WHERE для app_db\n"
            "• Формат дат в БД: 'YYYY-MM-DD' (например '2026-05-13'). НЕ используй другие форматы\n"
            "• Таблицы health_db и activities_db уже per-user (не нужен user_id в WHERE)\n"
            "• Таблицы app_db общие — ОБЯЗАТЕЛЬНО WHERE user_id = " + _user_id_str + "\n\n"
            "ИНСТРУМЕНТЫ (ЧТЕНИЕ):\n"
            "• query_health_db — сон, пульс покоя, BB, стресс, вес, дневные сводки (sleep, resting_hr, daily_summary, weight, stress, sleep_events). HRV в SQL НЕТ — он только в снапшоте выше.\n"
            "• query_activities_db — тренировки и их детали (activities, activity_laps, activity_splits, activity_records, steps_activities). Сплиты — activity_laps/splits, НЕ activity_records (это сырые точки).\n"
            "• query_app_db — планы, цели, старты, профиль, питание (таблицы: weekly_plans, races, training_goal, user_profile_overrides, food_entries). ВНИМАНИЕ: таблица называется training_goal (единственное число), НЕ training_goals\n"
            "  food_entries: user_id, entry_date, entry_time, description, calories, protein_g, fat_g, carbs_g, confidence, source, raw_response — записи о приёмах еды по дням\n\n"
            "ИНСТРУМЕНТЫ ЗАПИСИ — ВЫЗЫВАЙ САМ, БЕЗ КОМАНД ОТ ЮЗЕРА:\n"
            "Пользователь общается обычным текстом. Команд знать НЕ должен. "
            "Распознавай намерение и вызывай нужный tool. Доступны:\n\n"
            "• confirm_fact(fact_date, fact_text) — юзер УТВЕРЖДАЕТ/ПОПРАВЛЯЕТ конкретный факт за дату.\n"
            "  Триггеры: «это правильно», «верно», «нет, было X», «вчера было Y», «итого недели N км».\n"
            "  Пример: юзер «Бери мои данные. Пят 19.06 темповая Z4, темп 5:46, 10.5 км». →\n"
            "          вызови confirm_fact(fact_date=\"2026-06-19\", fact_text=\"темповая Z4, темп 5:46, 10.5 км — чёткое выполнение\")\n"
            "  В тексте ответа подтверди: «Принял: 19.06 — темповая Z4. Буду использовать как факт.»\n\n"
            "• remember_note(text, expires_at?) — долговременная заметка. Замена тегу [ЗАПОМНИТЬ].\n"
            "  Травмы, состояния, предпочтения, расписание. Курсы лекарств — обязательно expires_at.\n"
            "  Если ВРЕМЕННОЕ состояние без срока — сначала спроси «на сколько запомнить?», "
            "  потом на следующем шаге вызови с expires_at.\n\n"
            "• forget_note(item_id) — деактивировать заметку. id виден в блоке «Важная информация» в формате «#N. текст».\n"
            "  Триггеры: «забудь про X», «уже не актуально», «антибиотики допил» (если был курс).\n"
            "  Найди подходящий #N в текущей памяти и вызови. Если непонятно какую — уточни у юзера.\n\n"
            "• set_race_result(race_id, actual_time, notes?) — фактический результат прошедшего старта.\n"
            "  Триггеры: «вчера пробежал 5к за 24:30», «ночной забег 49:52». race_id из блока «НЕДАВНО ПРОБЕЖАЛ».\n"
            "  Если race_id не находится в контексте — лучше confirm_fact, а не выдумывай.\n\n"
            "• record_feeling(score, note?) — субъективное самочувствие 1-5.\n"
            "  Триггеры: «чувствую на 3», «отлично», «плохо», «устала», «полно сил».\n"
            "  Маппинг: 1=очень плохо, 2=плохо, 3=нормально, 4=хорошо, 5=отлично.\n\n"
            "• save_weekly_plan(plan_text, week_type) — план на ТЕКУЩУЮ неделю. ТОЛЬКО после явного «сохрани/да».\n\n"
            "ПОСЛЕ УСПЕШНОГО tool — кратко подтверди в тексте ответа человеческой фразой "
            "(«Принял», «Запомнил», «Сохранил результат»). НЕ выдумывай подтверждение если tool не вызывал.\n\n"
            "🚫 ПРАВИЛО ЧЕСТНОСТИ — НЕ ВРИ О СОХРАНЕНИИ:\n"
            "У тебя НЕТ инструментов для записи цели, гонок, веса, LTHR, профиля, еды, заметок памяти, "
            "тренировок в БД. Сохранять план — ТОЛЬКО через save_weekly_plan (если он есть в tools).\n"
            "Если юзер просит «сохрани цель/вес/гонку/еду/заметку» — НЕ ПИШИ «✅ Сохранил», «✅ Запомнил», «зафиксировал в памяти» и т.п. "
            "Это будет ложь — данные останутся только в текущем чате и пропадут.\n"
            "Вместо этого ЧЕСТНО скажи: «Чтобы это сохранилось в базу, используй: …» и подскажи кнопку/команду:\n"
            "  • цель → /goal <текст> (цель + парсит даты в гонки)\n"
            "  • гонка → /race add <дата> <название> <км> (или /race priority #N — пометить A-гонку)\n"
            "  • вес → кнопка ⚖ Вес\n"
            "  • LTHR → кнопка 💓 LTHR\n"
            "  • часовой пояс → /tz <Europe/Moscow>\n"
            "  • еда → кнопка 🍽 Еда (текст/фото/голос) — после сохранения система пришлёт «✅ Сохранено… (#N)»\n"
            "  • заметка в долговременную память — поставь в КОНЦЕ ответа тег [ЗАПОМНИТЬ: ...], бот его извлечёт и сохранит (это единственный способ записи памяти)\n"
            "  • тренировка — синкается автоматически по кнопке 💪 Спорт или внутри /plan и 🏃 Тренировка\n"
            "ИСКЛЮЧЕНИЕ: если в этом ответе ты реально вызвал save_weekly_plan и получил «OK: план сохранён…», "
            "тогда можешь честно подтвердить сохранение плана.\n\n"
            "🤖 ЧТО Я (этот бот) УМЕЮ — не путай со «сторонними приложениями»:\n"
            "• Считаю калории/БЖУ еды (фото, голос, текст) — Claude Vision/Whisper + ISSN-нормы по типу тренировочного дня.\n"
            "  После сохранения шлю подтверждение в формате:\n"
            "    «✅ Сохранено[ за DD.MM.YYYY]! (#N)\\n<описание>: <ккал> ккал»\n"
            "  Запись лежит в app_db.food_entries (id=N). Если юзер прислал такой текст или цитирует «#N» —\n"
            "  это МОЯ собственная запись, можно достать SQL'ом: SELECT * FROM food_entries WHERE id = N AND user_id = " + _user_id_str + ".\n"
            "  НИКОГДА не говори «это от другого бота/приложения» про сообщения в этом формате.\n"
            "• Синхронизирую Garmin (сон/HRV/BB/RHR/тренировки), строю недельный план (build/peak/taper по дистанции A-гонки), "
            "разбираю тренировку (зоны/сплиты/cardiac drift), храню гонки (`races`, A-гонка помечена `is_priority=1`).\n"
            "• Кнопки: 🌅 Утро · 🏃 Тренировка · 📅 План · 💪 Спорт · 🍽 Еда · 📋 Отчёт по еде · 🏁 Старты · ⚖ Вес · 💓 LTHR · ...\n"
            "• Команды: /plan /race /race priority #N /feeling /goal /remember /memory /forget /status /admin_stats /profile_reset.\n\n"
            "🚨 КРИТИЧЕСКИ ВАЖНОЕ ПРАВИЛО — TOOL FIRST:\n"
            "В контексте у тебя ТОЛЬКО снапшот сегодняшнего дня (сон, BB, HRV, RHR). История, конкретные тренировки, "
            "сплиты, прошлая еда, тренды — этого в контексте НЕТ. Только в БД через SQL.\n\n"
            "ОБЯЗАТЕЛЬНО вызывай SQL-инструмент, если в вопросе:\n"
            "• Конкретная дата или период («вчера», «5 мая», «прошлая неделя», «за месяц», «3 дня назад»)\n"
            "• Конкретная тренировка («моя длинная», «последний темп», «вчерашний бег»)\n"
            "• Сравнение («сравни мои последние две», «как изменился пульс», «лучше или хуже»)\n"
            "• История/тренд («тенденция», «динамика», «как часто», «за последние N»)\n"
            "• Подсчёт чего-либо («сколько», «средний», «всего», «максимальный»)\n"
            "• Сплиты, темпы, зоны конкретной активности\n"
            "• Конкретные приёмы еды или калории за прошлые дни\n\n"
            "ЗАПРЕЩЕНО:\n"
            "• Выдумывать цифры, даты, сплиты, темпы\n"
            "• Отвечать «у тебя было X км», если ты не сделал SQL-запрос\n"
            "• Ссылаться на «вчерашнюю тренировку», не запросив её из activities\n"
            "• Округлять или оценивать «примерно» — лучше запросить точно\n"
            "• СЧИТАТЬ В УМЕ суммы/средние/проценты больше чем по 3-4 числам — LLM в такой арифметике "
            "ненадёжна. Если в результате SQL нужна агрегация >4 значений (особенно скользящие/cumulative/% от цели) — "
            "сделай ВТОРОЙ SQL-запрос, который посчитает за тебя. Никогда не суммируй в голове 10+ чисел.\n\n"
            "🟢 ПРИОРИТЕТ ИСТОЧНИКОВ ДАННЫХ:\n"
            "1. Блок «ПОДТВЕРЖДЁННЫЕ АТЛЕТОМ ФАКТЫ» — это юзер сам утвердил. Используй как ground truth. "
            "Не противоречь ему даже если в Garmin-БД другое — атлет может знать что Garmin записал криво.\n"
            "2. SQL по garmin_*.db — объективные данные часов (если фактов нет за эту дату).\n"
            "3. История чата — для контекста разговора (не для цифр).\n"
            "Если факт и БД расходятся — упомяни обе цифры одной фразой («атлет: 56 км, Garmin: 56.5 — "
            "погрешность GPS») и используй факт юзера.\n\n"
            "🚫 НОРМА КМ/НЕД: только `norm_km_per_week` из WEEK FACTS. Если там `not_set` — пиши «норма не задана», без чисел из воздуха.\n\n"
            "🚫 НЕ ПЕРЕДЕЛЫВАЙ ПЛАН НА ХОДУ:\n"
            "Если передан блок «ТЕКУЩИЙ ПЛАН НЕДЕЛИ» — это утверждённый план. ОТВЕЧАЙ ПО НЕМУ "
            "(«сегодня по плану: …», «вторник по плану: …»). Не выдумывай альтернативные задачи дня, "
            "не пиши «сегодня лучше отдых» если в плане стоит тренировка — кроме случая hard-safety override "
            "(RHR-spike, BB <50 ≥2дн, HRV UNBALANCED, ACWR >1.5, TSB <-25). В override-случае ЯВНО назови "
            "причину одной фразой («отклоняюсь от плана из-за RHR +12 над базой») и предложи замену. "
            "Если юзер просит изменить план — скажи «изменить план: кнопка 📅 План перегенерирует, или напиши "
            "/plan tweak <что поправить>», не делай молчаливую замену в QA.\n\n"
            "🛰️ GPS-АНОМАЛИИ + ⚖️ 80/20 — вычислены в коде. Готовые сигналы — в блоках MORNING FACTS / WEEK FACTS:\n"
            "• gps_anomalies: бери формулировку как есть, не пересчитывай. Не критикуй темп если флаг есть.\n"
            "• z1_z3_pct + z1_z3_verdict (in_band/below/above/unknown_phase) — фаз-зависимый вердикт уже применён.\n"
            "  НЕ выноси свой вердикт по 80/20. Если unknown_phase — соотношение не оценивай.\n\n"
            "🧮 СКОЛЬЗЯЩИЕ ОКНА И АГРЕГАТЫ — ВСЕГДА SQL, НИКОГДА В УМЕ:\n"
            "SQLite поддерживает window functions. Для «км за 7/28 дней», «средний пульс за месяц», "
            "«трендов» и т.п. — используй их, а не считай руками.\n\n"
            "Шаблон скользящей суммы (7-дн и 28-дн км по каждому дню):\n"
            "  WITH per_day AS (\n"
            "    SELECT DATE(start_time) day, SUM(distance) km\n"
            "    FROM activities WHERE sport='running' AND start_time >= 'YYYY-MM-DD'\n"
            "    GROUP BY DATE(start_time)\n"
            "  )\n"
            "  SELECT day,\n"
            "    ROUND(SUM(km) OVER (ORDER BY day ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 1) AS km_7d,\n"
            "    ROUND(SUM(km) OVER (ORDER BY day ROWS BETWEEN 27 PRECEDING AND CURRENT ROW), 1) AS km_28d\n"
            "  FROM per_day ORDER BY day;\n\n"
            "ВАЖНО: для скользящего 28-дн окна с начала 'YYYY-MM-DD' данные нужны на 27 дней ГЛУБЖЕ — "
            "иначе первые точки будут заниженными (включат только часть окна).\n\n"
            "Если для скользящего окна по дням (а не по тренировкам) — сначала сформируй ряд всех дат "
            "(recursive CTE или JOIN с calendar), потом ROWS BETWEEN. Иначе ROWS BETWEEN N PRECEDING "
            "будет считать N тренировок, а не N календарных дней. Безопаснее — RANGE BETWEEN INTERVAL '27 days' "
            "PRECEDING (но SQLite не поддерживает RANGE с INTERVAL — используй self-join):\n"
            "  SELECT a.day, ROUND(SUM(b.km), 1) AS km_28d\n"
            "  FROM per_day a JOIN per_day b ON b.day BETWEEN DATE(a.day, '-27 days') AND a.day\n"
            "  GROUP BY a.day ORDER BY a.day;\n\n"
            "ПРИМЕРЫ:\n"
            "Q: «как я бегал на прошлой неделе?» → СНАЧАЛА query_activities_db с фильтром по start_time, ПОТОМ ответ\n"
            "Q: «сколько я ел углей за последние 3 дня?» → СНАЧАЛА query_app_db к food_entries (с SUM), ПОТОМ ответ\n"
            "Q: «скользящий 28-дн объём за 3 месяца» → query_activities_db с self-join или window function, "
            "НИКОГДА не считай вручную\n"
            "Q: «покажи сплиты моей последней длинной» → query_activities_db: activities (last running) JOIN activity_laps по activity_id\n"
            "Q: «как я восстановился?» → можно ответить из контекста (сегодняшние сон/BB/HRV уже есть)\n"
            "Q: «что мне сегодня тренировать?» → можно из контекста + план (если передан)\n\n"
            "ПРАВИЛА ОФОРМЛЕНИЯ:\n"
            "• Пиши на русском, кратко. НЕ используй markdown (**, ##) — только текст и эмодзи\n"
            "• Если SQL вернул [] — честно скажи «данных за этот период нет», не выдумывай\n\n"
            "ЗОНЫ GARMIN (5-зонная модель — границы в профиле спортсмена):\n"
            "Z1 = разминка/восстановление, Z2 = лёгкая, Z3 = АЭРОБНАЯ (основная зона лёгкого/базового бега!), "
            "Z4 = пороговая (темповый бег), Z5 = анаэробная (интервалы/макс).\n"
            "ВАЖНО: лёгкий бег у Garmin = Z3, НЕ Z2. Рекомендуй лёгкий бег в Z3.\n\n"
            "АВТО-ЗАПОМИНАНИЕ:\n"
            "Если в сообщении пользователя есть важная информация, которую нужно учитывать в будущем "
            "(травмы, предпочтения, расписание, ограничения, стиль общения) — добавь В КОНЦЕ ответа тег:\n"
            "[ЗАПОМНИТЬ: краткая формулировка]                — бессрочно\n"
            "[ЗАПОМНИТЬ до 2026-07-05: формулировка]          — со сроком жизни (ISO YYYY-MM-DD)\n"
            "[ЗАПОМНИТЬ до 05.07: формулировка]                — DD.MM (год авто)\n"
            "[ЗАПОМНИТЬ до через 14 дней: формулировка]        — относительный срок\n"
            "После истечения заметка перестаёт показываться в контексте.\n\n"
            "Когда ставить срок vs бессрочно:\n"
            "• Антибиотики, лекарства, курс лечения, восстановление травмы, отпуск — ставь СРОК.\n"
            "  Если пользователь сказал «принимаю X 14 дней» — посчитай дату окончания и поставь её.\n"
            "• Травма с диагнозом без чёткого срока — бессрочно (потом юзер сам забудет через /forget).\n"
            "• Предпочтения, стиль общения, расписание — бессрочно.\n\n"
            "🕒 УТОЧНЕНИЕ СРОКА (важное правило):\n"
            "Если состояние явно ВРЕМЕННОЕ (травма, болезнь, курс лекарств, отпуск, стресс-период, временный график), "
            "а пользователь срок НЕ назвал — НЕ ставь [ЗАПОМНИТЬ] сразу. Вместо этого в конце ответа коротко спроси:\n"
            "  «На сколько запомнить? До даты, через N дней — или бессрочно?»\n"
            "В СЛЕДУЮЩЕМ сообщении юзера будет ответ: «на 2 недели», «до 05.07», «до конца месяца», «насовсем».\n"
            "Тогда на ТОМ шаге поставь [ЗАПОМНИТЬ до <срок>: …] (или без «до» — если «насовсем»).\n\n"
            "Пример диалога:\n"
            "  юзер: «потянул икру, болит»\n"
            "  ты: «Понял. Без прыжковых и горок пока. На сколько запомнить — до даты или насовсем?» (БЕЗ тега)\n"
            "  юзер: «недели на две»\n"
            "  ты: «Ок, две недели держим лёгкий бег. [ЗАПОМНИТЬ до через 14 дней: восстанавливает икру — без прыжков/горок]»\n\n"
            "Троттл: если в QA-истории уже видишь, что ты ОДИН раз спрашивал срок и юзер сменил тему — НЕ переспрашивай. "
            "Просто поставь [ЗАПОМНИТЬ] без срока, бессрочно (юзер сам уберёт через /forget когда не нужно).\n"
            "Бытовые предпочтения («не пью кофе», «бегаю утром») НЕ требуют уточнения — это бессрочно сразу.\n\n"
            "Примеры:\n"
            "• «болит ахилл» → [ЗАПОМНИТЬ: болит ахилл — исключить прыжковые и горки]\n"
            "• «не бегаю по средам» → [ЗАПОМНИТЬ: не бегает по средам]\n"
            "• «начал курс антибиотиков 14 дней» → [ЗАПОМНИТЬ до через 14 дней: курс антибиотиков — без Z4/Z5]\n"
            "• «пью азитромицин до 05.07» → [ЗАПОМНИТЬ до 05.07: азитромицин — HRV занижен, без интенсивности]\n"
            "• «уезжаю в отпуск с 10 по 24 июля» → [ЗАПОМНИТЬ до 24.07: в отпуске — план облегчить]\n"
            "🚫 НЕЛЬЗЯ через [ЗАПОМНИТЬ]: цели с датами, гонки/старты, вес, LTHR, часовой пояс, планы тренировок, "
            "результаты прошлых забегов с временем — для них есть структурные команды (/goal /race /tz, кнопки ⚖/💓). "
            "Сохранение их в заметки → расхождение с БД и две разные «цели» одновременно.\n"
            "НЕ дублируй то, что уже есть в заметках пользователя. Тег ставь ТОЛЬКО когда есть новая информация.\n"
        )
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
            {"type": "text", "text": stable_part, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": dynamic_part},
        ]
        try:
            if db_paths:
                return await self._ask_with_tools(
                    system=system,
                    question=question,
                    history=history,
                    user_memory=user_memory,
                    db_paths=db_paths,
                    user_id=user_id,
                    save_plan_fn=save_plan_fn,
                    write_tools=write_tools,
                )
            return await self._generate_text(
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
        import sqlite3 as _sqlite3

        # Таблицы app.db, которые разрешено видеть Claude (все с колонкой user_id).
        _APP_USER_TABLES = (
            "weekly_plans", "training_goal",
            "races", "user_profile_overrides", "food_entries",
        )

        def _open_ro(path: str) -> "_sqlite3.Connection":
            return _sqlite3.connect(f"file:{path}?mode=ro", uri=True)

        def _build_app_view(app_path: str, uid: int | None) -> "_sqlite3.Connection":
            """In-memory копия app.db только со строками этого user_id."""
            mem = _sqlite3.connect(":memory:")
            src = _open_ro(app_path)
            try:
                for tbl in _APP_USER_TABLES:
                    ddl = src.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                        (tbl,),
                    ).fetchone()
                    if not ddl or not ddl[0]:
                        continue
                    mem.execute(ddl[0])
                    cols = [r[1] for r in src.execute(f"PRAGMA table_info({tbl})").fetchall()]
                    if "user_id" in cols:
                        # Без известного user_id фильтровать нечем — лучше пусто, чем утечка.
                        rows = src.execute(
                            f"SELECT * FROM {tbl} WHERE user_id = ?", (uid,)
                        ).fetchall() if uid is not None else []
                    else:
                        rows = src.execute(f"SELECT * FROM {tbl}").fetchall()
                    if rows:
                        ph = ",".join(["?"] * len(cols))
                        mem.executemany(f"INSERT INTO {tbl} VALUES ({ph})", rows)
                mem.commit()
            finally:
                src.close()
            return mem

        def _run_sql(db_key: str, sql: str) -> str:
            db_path = db_paths.get(db_key)
            if not db_path:
                return f"[ошибка: база {db_key} не найдена]"
            sql_stripped = sql.strip().upper()
            allowed = sql_stripped.startswith("SELECT") or sql_stripped.startswith((
                "PRAGMA TABLE_INFO", "PRAGMA TABLE_LIST", "PRAGMA TABLE_XINFO",
            ))
            if not allowed:
                return "[ошибка: разрешены только SELECT и PRAGMA TABLE_INFO]"
            conn = None
            try:
                conn = _build_app_view(db_path, user_id) if db_key == "app" else _open_ro(db_path)
                conn.row_factory = _sqlite3.Row
                rows = conn.execute(sql, []).fetchmany(200)
                result = [dict(r) for r in rows]
                return str(result) if result else "[]"
            except Exception as e:
                logger.warning("Tool SQL failed (db=%s): %s", db_key, e)
                return "[ошибка: запрос не выполнен — проверь синтаксис и имена колонок]"
            finally:
                if conn is not None:
                    conn.close()

        tools = [
            {
                "name": "query_health_db",
                "description": (
                    "Выполни SELECT к базе здоровья Garmin (garmin.db). Таблицы и колонки:\n"
                    "• sleep — day, start, end, total_sleep, deep_sleep, light_sleep, rem_sleep, awake, "
                    "avg_spo2, avg_rr, avg_stress, score, qualifier\n"
                    "• resting_hr — day, resting_heart_rate\n"
                    "• daily_summary — day, rhr, hr_min, hr_max, stress_avg, steps, step_goal, distance, "
                    "calories_total, calories_active, calories_bmr, calories_consumed, "
                    "moderate_activity_time, vigorous_activity_time, intensity_time_goal, "
                    "floors_up, floors_down, hydration_intake, sweat_loss, "
                    "spo2_avg, spo2_min, rr_waking_avg, rr_max, rr_min, bb_charged, bb_max, bb_min, description\n"
                    "• weight — day, weight\n"
                    "• stress — timestamp, stress (внутридневной ряд)\n"
                    "• sleep_events — timestamp, event, duration\n"
                    "ВАЖНО: HRV здесь НЕТ — он передан в снапшоте контекста, не в SQL. "
                    "Калории — это colонки calories_* в daily_summary, НЕ просто `calories`."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"sql": {"type": "string"}},
                    "required": ["sql"],
                },
            },
            {
                "name": "query_activities_db",
                "description": (
                    "Выполни SELECT к базе тренировок Garmin (garmin_activities.db). Таблицы и колонки:\n"
                    "• activities — activity_id, name, sport, sub_sport, start_time, stop_time, "
                    "elapsed_time, moving_time, distance, calories, "
                    "avg_hr, max_hr, avg_rr, max_rr, avg_cadence, max_cadence, avg_speed, max_speed, "
                    "ascent, descent, training_load, training_effect, anaerobic_training_effect, "
                    "self_eval_feel, self_eval_effort, hr_zones_method, "
                    "hrz_1_hr..hrz_5_hr (нижние границы зон), hrz_1_time..hrz_5_time (время в зонах), "
                    "avg_temperature, max_temperature, min_temperature, start_lat, start_long\n"
                    "• activity_laps — activity_id, lap, start_time, distance, elapsed_time, moving_time, "
                    "avg_hr, max_hr, avg_speed, max_speed, avg_cadence, ascent, descent, calories, "
                    "hrz_1_time..hrz_5_time (автосплиты, обычно по 1 км)\n"
                    "• activity_splits — activity_id, split, completed, distance, moving_time, "
                    "avg_hr, max_hr, avg_speed, avg_cadence, calories (ручные/тренерские сплиты)\n"
                    "• activity_records — activity_id, record, timestamp, position_lat, position_long, "
                    "distance, cadence, altitude, hr, rr, speed, temperature "
                    "(посекундные точки; КОЛОНКА ПУЛЬСА НАЗЫВАЕТСЯ hr, НЕ heart_rate)\n"
                    "• steps_activities — activity_id, steps, avg_pace, avg_moving_pace, max_pace, "
                    "avg_steps_per_min, max_steps_per_min, avg_step_length, vo2_max, "
                    "avg_ground_contact_time, avg_vertical_ratio, avg_vertical_oscillation, "
                    "avg_gct_balance, avg_stance_time_percent (метрики бега/ходьбы)\n"
                    "ВАЖНО: для сплитов используй activity_laps или activity_splits, НЕ activity_records. "
                    "Для пейса в running — steps_activities.avg_pace (формат TIME, мин/км)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"sql": {"type": "string"}},
                    "required": ["sql"],
                },
            },
            {
                "name": "query_app_db",
                "description": (
                    "Выполни SELECT к базе приложения. "
                    "Таблицы: weekly_plans (user_id, week_start, plan_text), "
                    "training_goal (user_id, goal_text) — ИМЕННО training_goal, единственное число, "
                    "races (user_id, name, date, distance_km, goal_time), "
                    "user_profile_overrides (user_id, lthr, weight_kg, timezone, age, weekly_km_target), "
                    "food_entries (user_id, entry_date, entry_time, description, calories, protein_g, fat_g, carbs_g) — еда по дням."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"sql": {"type": "string"}},
                    "required": ["sql"],
                },
            },
        ]

        # ===== Write-tools для естественного диалога =====
        # Цель: пользователь общается обычным текстом, бот САМ распознаёт намерение
        # и вызывает нужный tool. Никаких команд знать не нужно.
        if write_tools and "confirm_fact" in write_tools:
            tools.append({
                "name": "confirm_fact",
                "description": (
                    "Сохрани УТВЕРЖДЁННЫЙ пользователем факт за конкретную дату — становится "
                    "источником истины, который ты будешь видеть в будущих контекстах.\n"
                    "Вызывай, когда пользователь явно поправляет/утверждает данные:\n"
                    "• «это правильно: 56 км за неделю» / «верно» (поправка после твоей ошибки)\n"
                    "• «вчера было темповая Z4, а не Z3»\n"
                    "• «итог неделя 15-21.06 — 56.14 км бега»\n"
                    "• «в субботу 20.06 — отдых, не тренировка»\n"
                    "• «пятница 19.06 — темповая 10.5 км Z4 5:46/км, чёткое выполнение»\n"
                    "НЕ вызывай для бытовых разговоров и предположений. Только когда юзер "
                    "ПОПРАВЛЯЕТ или ПОДТВЕРЖДАЕТ конкретные цифры/факты за дату.\n"
                    "fact_date — YYYY-MM-DD. Если юзер сказал «вчера» — резолви относительно «Сегодня» в контексте."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "fact_date": {"type": "string", "description": "ISO дата факта (YYYY-MM-DD)"},
                        "fact_text": {"type": "string", "description": "Краткая формулировка факта"},
                    },
                    "required": ["fact_date", "fact_text"],
                },
            })
        if write_tools and "remember_note" in write_tools:
            tools.append({
                "name": "remember_note",
                "description": (
                    "Сохрани долговременную заметку об атлете в персональную память (она будет "
                    "видна в системном промпте всегда, пока не истечёт срок).\n"
                    "Вызывай вместо тега [ЗАПОМНИТЬ]. Поводы:\n"
                    "• Травмы, болезни, хронические состояния («болит ахилл»)\n"
                    "• Предпочтения и расписание («не бегаю по средам»)\n"
                    "• Стиль общения («пиши короче»)\n"
                    "• Курсы лекарств с СРОКОМ — обязательно ставь expires_at\n"
                    "expires_at — необязательное YYYY-MM-DD. Для курсов/отпусков считай дату окончания.\n"
                    "🚫 Не вызывай для целей, гонок, веса, LTHR, результатов забегов — есть структурные таблицы."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Краткая формулировка"},
                        "expires_at": {"type": "string", "description": "YYYY-MM-DD или пусто"},
                    },
                    "required": ["text"],
                },
            })
        if write_tools and "forget_note" in write_tools:
            tools.append({
                "name": "forget_note",
                "description": (
                    "Деактивируй заметку из памяти по её id (видишь id в блоке «Важная информация» — "
                    "они приходят в формате «#N. текст»).\n"
                    "Вызывай когда юзер говорит «забудь это», «уже не актуально», «убери про X», "
                    "«антибиотики допил» (если запись о курсе была). Если в памяти нет подходящей "
                    "заметки — не вызывай."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer", "description": "ID заметки из памяти"},
                    },
                    "required": ["item_id"],
                },
            })
        if write_tools and "set_race_result" in write_tools:
            tools.append({
                "name": "set_race_result",
                "description": (
                    "Сохрани фактический результат гонки (race_id из «ПРЕДСТОЯЩИЕ СТАРТЫ» или "
                    "«НЕДАВНО ПРОБЕЖАЛ» — это структурный источник истины).\n"
                    "Вызывай когда юзер сообщает результат прошедшего старта:\n"
                    "• «ночной забег 49:52, сплиты 0-5 23:59, 5-10 25:53» (race_id=4 из контекста)\n"
                    "• «забег субботу пробежал 47:30»\n"
                    "Если race_id неоднозначен — спроси юзера, какая именно гонка. "
                    "Если соответствующей гонки в races нет — лучше confirm_fact, а не выдумывай id."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "race_id": {"type": "integer"},
                        "actual_time": {"type": "string", "description": "Формат «MM:SS» или «H:MM:SS»"},
                        "notes": {"type": "string", "description": "Сплиты, темп, ощущения"},
                    },
                    "required": ["race_id", "actual_time"],
                },
            })
        if write_tools and "record_feeling" in write_tools:
            tools.append({
                "name": "record_feeling",
                "description": (
                    "Запиши субъективное самочувствие за день. Вызывай когда юзер говорит:\n"
                    "• «сегодня чувствую на 3», «плохое самочувствие», «отлично себя чувствую»\n"
                    "• «устала», «полно сил» — резолви в score 1-5\n"
                    "score: 1=очень плохо, 2=плохо, 3=нормально, 4=хорошо, 5=отлично.\n"
                    "note — короткий текст пояснения от юзера (если есть)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "score": {"type": "integer", "minimum": 1, "maximum": 5},
                        "note": {"type": "string"},
                    },
                    "required": ["score"],
                },
            })

        if save_plan_fn is not None:
            tools.append({
                "name": "save_weekly_plan",
                "description": (
                    "Сохрани план на ТЕКУЩУЮ неделю в weekly_plans (UPSERT по user_id+week_start). "
                    "Используй ТОЛЬКО когда пользователь явно просит «сохрани/запиши/зафиксируй» план, "
                    "который вы согласовали в этом диалоге. "
                    "Не вызывай без подтверждения пользователя. "
                    "Не вызывай если только что обсуждаемый план ещё не финализирован. "
                    "plan_text — полный текст плана для отображения юзеру (по дням Пн-Вс, "
                    "формат «День DD.MM — Тип, дистанция, зона/темп»). "
                    "week_type — одно из: recovery, base, build, peak, taper "
                    "(выбери по содержанию плана: пиковая нагрузка → peak, тейпер перед стартом → taper, "
                    "восстановительная → recovery, базовый объём → base, развивающая → build)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "plan_text": {"type": "string", "description": "Полный текст плана недели"},
                        "week_type": {
                            "type": "string",
                            "enum": ["recovery", "base", "build", "peak", "taper"],
                        },
                    },
                    "required": ["plan_text", "week_type"],
                },
            })

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
            system_block = [{"type": "text", "text": full_system, "cache_control": {"type": "ephemeral"}}]

        base_messages: list[dict] = list(history or [])
        base_messages.append({"role": "user", "content": question})

        tool_call_count = 0
        last_exc: Exception | None = None

        for model in self._models:
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
                                return partial_text
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
                                        save_result = fn(**(block.input or {}))
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
                                result = _run_sql(db_key, sql_q)
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
                system_prompt=system,
                user_prompt=user_prompt,
                max_tokens=2000,
                history=history,
                user_memory=user_memory,
            )
        except Exception as exc:
            logger.exception("Unexpected error during AI analysis")
            return f"Не удалось получить анализ: {exc}"

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
        """Return [z1_s, z2_s, z3_s, z4_s, z5_s] directly from Garmin data, or None."""
        secs = []
        for i in range(1, 6):
            t = activity.get(f"hrz_{i}_time")
            secs.append(self._time_str_to_secs(t) if t else 0.0)
        return secs if any(s > 0 for s in secs) else None

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
            race_lines = ["ПРЕДСТОЯЩИЕ СТАРТЫ:"]
            for r in races:
                race_date = _date.fromisoformat(r["date"])
                days_left = (race_date - today_d).days
                dist = f" {r['distance_km']:.1f}км" if r.get("distance_km") else ""
                goal_t = f", цель {r['goal_time']}" if r.get("goal_time") else ""
                weeks_left = days_left // 7
                race_lines.append(
                    f"  {r['date']} — {r['name']}{dist}{goal_t} "
                    f"[{days_left} дней / {weeks_left} недель до старта]"
                )
            parts.append("\n".join(race_lines))

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
    def _fmt_zone_times(a: dict) -> str:
        """Format HR zone time breakdown: Z1 1м | Z2 2м | Z3 65м | Z4 6м | Z5 1м"""
        def _secs(val: Any) -> int:
            if not val:
                return 0
            s = str(val).split(".")[0]
            parts = s.split(":")
            try:
                if len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                if len(parts) == 2:
                    return int(parts[0]) * 60 + int(parts[1])
            except Exception:
                pass
            return 0

        times = [_secs(a.get(f"hrz_{i}_time")) for i in range(1, 6)]
        if not any(times):
            return ""
        parts = []
        for i, t in enumerate(times, 1):
            if t > 0:
                parts.append(f"Z{i} {t // 60}м")
        return " | ".join(parts)

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
