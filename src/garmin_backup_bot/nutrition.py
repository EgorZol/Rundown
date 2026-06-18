from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, timedelta
from typing import Any

from anthropic import Anthropic

logger = logging.getLogger(__name__)


class NutritionTruncatedError(Exception):
    """Claude обрезал ответ по max_tokens — JSON невалидный, нужен дружелюбный ответ юзеру."""


FOOD_RECOGNITION_SYSTEM = """\
Ты — диетолог-нутрициолог. Проанализируй еду и оцени нутриентный состав.

ПРАВИЛА:
• Определи все блюда и продукты
• Оцени размер порции (для фото — ориентируйся на тарелку/посуду для масштаба)
• Для каждого продукта оцени калории и БЖУ
• Суммируй всё в один итог
• Если на фото не еда — верни confidence: "none"
• Если еда видна нечётко или трудно определить — confidence: "low"
• Порции по умолчанию — стандартные (не маленькие, не огромные)

ОТВЕТ — строго JSON, без markdown, без ```json, просто JSON:
{
  "description": "краткое описание блюд на русском",
  "items": [
    {"name": "название продукта", "portion_g": 150, "calories": 200, "protein_g": 10.0, "fat_g": 5.0, "carbs_g": 25.0}
  ],
  "total": {
    "calories": 450,
    "protein_g": 25.0,
    "fat_g": 12.0,
    "carbs_g": 55.0
  },
  "confidence": "high"
}

confidence:
• "high" — стандартное блюдо, уверен в оценке (±15%)
• "medium" — порция нестандартная или блюдо необычное (±30%)
• "low" — сложно определить состав (±50%)
• "none" — не еда
"""

# Месяцы в родительном/именительном падеже → номер месяца
_MONTH_PATTERNS: list[tuple[str, int]] = [
    (r"январ[ьяе]", 1),
    (r"феврал[ьяе]", 2),
    (r"март[ае]?", 3),
    (r"апрел[ьяе]", 4),
    (r"ма[йяе]", 5),
    (r"июн[ьяе]", 6),
    (r"июл[ьяе]", 7),
    (r"август[ае]?", 8),
    (r"сентябр[ьяе]", 9),
    (r"октябр[ьяе]", 10),
    (r"ноябр[ьяе]", 11),
    (r"декабр[ьяе]", 12),
]


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _clamp_past(d: date, today: date) -> date:
    """Если дата в будущем (опечатка/прошлый год) — откатить на год назад."""
    if d > today:
        prev = _safe_date(d.year - 1, d.month, d.day)
        return prev or d
    return d


class NutritionAnalyzer:
    """Food recognition via Claude Vision and text analysis."""

    def __init__(
        self,
        api_key: str,
        model: str,
        fallback_models: list[str] | None = None,
    ) -> None:
        self._client = Anthropic(api_key=api_key)
        candidates = [model, *(fallback_models or [])]
        self._models = list(dict.fromkeys(m for m in candidates if m))

    async def _call_api(
        self,
        messages: list[dict],
        max_tokens: int = 3000,
    ) -> str:
        """Call Anthropic API with retry on fallback models.

        Replicates the retry pattern from analyst.py _generate_text.
        """
        system_block = [
            {"type": "text", "text": FOOD_RECOGNITION_SYSTEM,
             "cache_control": {"type": "ephemeral"}}
        ]

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
                text_parts = [
                    block.text for block in response.content
                    if getattr(block, "type", "") == "text"
                ]
                if idx > 0:
                    logger.warning("Nutrition: fallback model used: %s", model)
                result = "\n".join(part for part in text_parts if part).strip()
                u = response.usage
                cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
                cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
                logger.info(
                    "Nutrition API OK: stop=%s in=%d out=%d cache_read=%d cache_write=%d",
                    response.stop_reason, u.input_tokens, u.output_tokens,
                    cache_read, cache_write,
                )
                if response.stop_reason == "max_tokens":
                    logger.warning(
                        "Nutrition response truncated by max_tokens=%d — JSON будет невалидным",
                        max_tokens,
                    )
                    raise NutritionTruncatedError(
                        "Слишком длинный список продуктов. "
                        "Попробуй разбить на 2 сообщения (например: завтрак отдельно, обед+перекус отдельно)."
                    )
                return result
            except Exception as exc:
                last_exc = exc
                logger.warning("Nutrition model failed: %s (%s)", model, exc)

        if last_exc:
            raise last_exc
        raise RuntimeError("No Anthropic models configured")

    def _parse_food_json(self, raw: str) -> dict:
        """Parse Claude's JSON response into a food dict."""
        text = raw.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) >= 3 else parts[-1]
            if text.startswith("json"):
                text = text[4:]
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError(f"No JSON object in response: {raw[:200]}")
        data = json.loads(text[start:end])
        # Validate required fields
        total = data.get("total") or {}
        return {
            "description": data.get("description", "Неизвестная еда"),
            "items": data.get("items") or [],
            "calories": float(total.get("calories", 0)),
            "protein_g": float(total.get("protein_g", 0)),
            "fat_g": float(total.get("fat_g", 0)),
            "carbs_g": float(total.get("carbs_g", 0)),
            "confidence": data.get("confidence", "medium"),
            "raw": raw,
        }

    async def analyze_photo(
        self,
        b64_data: str,
        media_type: str = "image/jpeg",
        caption: str | None = None,
    ) -> dict:
        """Recognize food from a photo using Claude Vision.

        Args:
            b64_data: Base64-encoded image data.
            media_type: Image MIME type (image/jpeg, image/png, etc.).
            caption: Optional user caption — порции, состав, уточнения. Имеет
                приоритет над визуальной оценкой при конфликте (пользователь
                видит блюдо лучше камеры).

        Returns:
            Parsed food dict with description, calories, macros, confidence, raw.
        """
        caption_clean = (caption or "").strip()
        if caption_clean:
            prompt_text = (
                "Пользователь прислал фото еды с подписью.\n"
                f"ПОДПИСЬ ПОЛЬЗОВАТЕЛЯ: {caption_clean}\n\n"
                "Подпись — приоритетный источник: если в ней указаны блюда, "
                "порции (граммы/штуки/мл), состав или способ приготовления — "
                "ОБЯЗАТЕЛЬНО используй эти данные. Фото — для уточнения того, "
                "что не названо в подписи. При конфликте доверяй подписи.\n"
                "Оцени калории и БЖУ. Ответ — JSON."
            )
        else:
            prompt_text = "Что за еда на фото? Оцени калории и БЖУ. Ответ — JSON."

        messages = [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64_data,
                    },
                },
                {
                    "type": "text",
                    "text": prompt_text,
                },
            ],
        }]
        raw = await self._call_api(messages)
        return self._parse_food_json(raw)

    async def analyze_text(self, text: str) -> dict:
        """Recognize food from a text description.

        Args:
            text: User's description of food eaten.

        Returns:
            Parsed food dict with description, calories, macros, confidence, raw.
        """
        messages = [{
            "role": "user",
            "content": (
                f"Пользователь описал еду: {text}\n\n"
                "Оцени калории и БЖУ. Ответ — JSON."
            ),
        }]
        raw = await self._call_api(messages)
        return self._parse_food_json(raw)

    async def analyze_correction(self, current: dict, correction: str) -> dict:
        """Apply user correction to an existing food recognition.

        Args:
            current: Previously recognized food dict (description, items, calories, macros).
            correction: User's correction text — what was wrong, what to add/replace/remove.

        Returns:
            Updated food dict — current блюдо с применёнными правками, остальное без изменений.
        """
        current_json = {
            "description": current.get("description", ""),
            "items": current.get("items") or [],
            "total": {
                "calories": current.get("calories", 0),
                "protein_g": current.get("protein_g", 0),
                "fat_g": current.get("fat_g", 0),
                "carbs_g": current.get("carbs_g", 0),
            },
            "confidence": current.get("confidence", "medium"),
        }
        prompt = (
            "ТЕКУЩЕЕ РАСПОЗНАВАНИЕ ЕДЫ (то, что было определено ранее):\n"
            f"{json.dumps(current_json, ensure_ascii=False, indent=2)}\n\n"
            f"ПРАВКА ПОЛЬЗОВАТЕЛЯ: {correction}\n\n"
            "Примени правку к текущему распознаванию:\n"
            "• Если правка уточняет блюдо/порцию/состав — замени соответствующие "
            "поля, остальное оставь как есть.\n"
            "• Если правка добавляет блюдо — добавь в items и пересчитай total.\n"
            "• Если правка удаляет блюдо — убери из items и пересчитай total.\n"
            "• Если правка полностью переопределяет еду — замени всё.\n"
            "• Пересчитай total как сумму items.\n"
            "Ответ — JSON в том же формате."
        )
        messages = [{"role": "user", "content": prompt}]
        raw = await self._call_api(messages)
        return self._parse_food_json(raw)

    @staticmethod
    def classify_training_day(
        plan_line: str | None = None,
        active_calories: float | None = None,
    ) -> str:
        """Classify training day type from plan text or Garmin active calories.

        Returns: rest / regen / light / quality / long_ultra / race
        """
        import re

        if plan_line:
            low = plan_line.lower()
            # Race day / carb loading
            if any(k in low for k in ("гонка", "соревнован", "race", "загрузка")):
                return "race"
            # Rest
            if any(k in low for k in ("отдых", "выходной", "rest")) and \
               not any(k in low for k in ("бег", "пробеж", "кросс", "плаван")):
                return "rest"
            # Regen: pilates, stretching, yoga, swimming (but not if it's a run)
            is_run = any(k in low for k in ("бег", "пробеж", "кросс"))
            if not is_run and any(k in low for k in ("пилатес", "растяжк", "йога",
                                                      "yoga", "реген", "восстановит",
                                                      "плаван")):
                return "regen"
            # Check distance
            dist_match = re.search(r"(\d+)[–-]?(\d*)\s*км", low)
            km = 0
            if dist_match:
                km = int(dist_match.group(1))  # min of range for conservative estimate
            # Quality: intervals, tempo, fartlek, or long 15+km
            if any(k in low for k in ("интервал", "темпов", "фартлек", "качеств")):
                return "quality"
            if km >= 25:
                return "long_ultra"
            if km >= 15:
                return "quality"
            if km > 0 or any(k in low for k in ("бег", "пробеж", "кросс")):
                return "light"
            return "rest"

        # Fallback: Garmin active calories
        if active_calories is not None:
            if active_calories < 100:
                return "rest"
            if active_calories < 250:
                return "regen"
            if active_calories < 500:
                return "light"
            if active_calories < 900:
                return "quality"
            return "long_ultra"

        return "light"  # safe default

    @staticmethod
    def calculate_issn_targets(
        weight_kg: float,
        day_type: str = "light",
    ) -> dict[str, dict[str, float]]:
        """ISSN-based targets periodized by training day type.

        Day types: rest / regen / light / quality / long_ultra / race
        """
        carb_ranges = {
            "rest":       (3.0, 4.0),
            "regen":      (4.0, 5.0),
            "light":      (5.0, 6.0),
            "quality":    (6.0, 7.0),
            "long_ultra": (7.0, 8.0),
            "race":       (8.0, 10.0),
        }
        protein_ranges = {
            "rest":       (1.2, 1.4),
            "regen":      (1.2, 1.4),
            "light":      (1.4, 1.7),
            "quality":    (1.6, 2.0),
            "long_ultra": (1.6, 2.0),
            "race":       (1.4, 1.7),
        }
        c_lo, c_hi = carb_ranges.get(day_type, (5.0, 7.0))
        p_lo, p_hi = protein_ranges.get(day_type, (1.4, 1.7))
        return {
            "protein_g": {"min": round(weight_kg * p_lo, 1), "max": round(weight_kg * p_hi, 1)},
            "carbs_g": {"min": round(weight_kg * c_lo, 1), "max": round(weight_kg * c_hi, 1)},
            "fat_g": {"min": round(weight_kg * 0.8, 1), "max": round(weight_kg * 1.2, 1)},
        }

    @staticmethod
    def parse_entry_date(text: str, today: date) -> tuple[date | None, str]:
        """Найти дату в тексте и вернуть (дата | None, текст без даты).

        Понимает: «сегодня/вчера/позавчера», «16.05», «16.05.2026»,
        «16/05», «16 мая», «за 16 мая», «16-го мая». Будущие даты
        откатываются на год назад. Если даты нет — возвращает (None, text).
        """
        if not text:
            return None, text

        def _cut(src: str, start: int, end: int) -> str:
            return (src[:start] + src[end:]).strip(" ,.;:-—\t\n")

        low = text.lower()

        # 1. Относительные слова
        for word, delta in (("позавчера", 2), ("вчера", 1), ("сегодня", 0)):
            m = re.search(r"(?:за\s+)?\b" + word + r"\b", low)
            if m:
                return today - timedelta(days=delta), _cut(text, m.start(), m.end())

        # 2. Числовой формат DD.MM[.YYYY] (. / -)
        # Без явного префикса «за» и без года такие паттерны часто ложно
        # срабатывают на чисто пищевых дробях («5/6 порции», «5.6 г», «10.5 ккал»).
        # Поэтому требуем ОДНО из двух: префикс «за» или явный год.
        m = re.search(
            r"за\s+\b(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\b", low
        )
        if not m:
            m = re.search(
                r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})\b", low
            )
        if m:
            dd, mm = int(m.group(1)), int(m.group(2))
            yy = m.group(3) if m.lastindex and m.lastindex >= 3 else None
            if yy is None:
                year = today.year
            else:
                year = int(yy) if len(yy) == 4 else 2000 + int(yy)
            d = _safe_date(year, mm, dd)
            if d:
                return _clamp_past(d, today), _cut(text, m.start(), m.end())

        # 3. «DD <месяц словом>»
        for rgx, mon in _MONTH_PATTERNS:
            m = re.search(
                r"(?:за\s+)?\b(\d{1,2})(?:-?го)?\s+" + rgx + r"\b", low
            )
            if m:
                d = _safe_date(today.year, mon, int(m.group(1)))
                if d:
                    return _clamp_past(d, today), _cut(text, m.start(), m.end())

        return None, text

    @staticmethod
    def format_food_confirmation(result: dict) -> str:
        """Format food analysis result for user confirmation."""
        desc = result["description"]
        cal = result["calories"]
        p = result["protein_g"]
        f = result["fat_g"]
        c = result["carbs_g"]
        conf = result["confidence"]

        if conf == "none":
            return "На фото не еда. Попробуй другое фото или опиши текстом."

        conf_labels = {"high": "высокая", "medium": "средняя", "low": "низкая"}
        conf_str = conf_labels.get(conf, conf)

        lines = [f"🍽 {desc}"]
        items = result.get("items") or []
        if len(items) > 1:
            for item in items:
                lines.append(
                    f"  - {item['name']}: {item.get('calories', '?')} ккал "
                    f"({item.get('portion_g', '?')}г)"
                )
        lines.append("")
        lines.append(f"🔥 {cal:.0f} ккал | Б {p:.0f}г  Ж {f:.0f}г  У {c:.0f}г")
        lines.append(f"Точность: {conf_str}")
        ed = result.get("entry_date")
        if ed:
            try:
                lines.append(
                    f"\n📅 Запишу за {date.fromisoformat(ed).strftime('%d.%m.%Y')}"
                )
            except ValueError:
                pass
        lines.append("\nСохранить?")
        return "\n".join(lines)

    @staticmethod
    def _short_desc(desc: str, max_len: int = 35) -> str:
        """Сократить описание еды для компактного отчёта: режем по первому разделителю, потом по длине."""
        if not desc:
            return "?"
        s = desc.strip()
        cut = len(s)
        for sep in ("/", ",", " (", " —", " - ", "."):
            i = s.find(sep)
            if 0 < i < cut:
                cut = i
        s = s[:cut].strip()
        if len(s) > max_len:
            s = s[: max_len - 1].rstrip() + "…"
        return s

    @staticmethod
    def format_daily_report(
        entries: list[dict],
        garmin_daily: dict[str, Any] | None,
        weight_kg: float | None,
        report_date: date,
        plan_line: str | None = None,
        compact: bool = False,
    ) -> str:
        """Format a daily nutrition report.

        Args:
            entries: Food entries for the day (from storage.get_food_entries).
            garmin_daily: Garmin daily summary with calories_total/bmr/active.
                Может содержать ключ "estimated": True если total — оценка по 7-дн медиане.
            weight_kg: User weight for ISSN targets (optional).
            report_date: Date of the report.
            plan_line: Today's line from the weekly plan (for ISSN periodization).
            compact: True для краткого ночного отчёта (короткие строки, дельты КБЖУ).
        """
        lines = [f"🍽 ПИТАНИЕ — {report_date.strftime('%d.%m.%Y')}"]

        if not entries:
            lines.append("\nНет записей о еде за этот день.")
            return "\n".join(lines)

        if compact:
            lines[0] += f" · {len(entries)} приёмов"

        # List meals
        lines.append("")
        total_cal = 0.0
        total_p = 0.0
        total_f = 0.0
        total_c = 0.0
        for i, e in enumerate(entries, 1):
            cal = e.get("calories", 0)
            p = e.get("protein_g", 0)
            f = e.get("fat_g", 0)
            c = e.get("carbs_g", 0)
            total_cal += cal
            total_p += p
            total_f += f
            total_c += c
            if compact:
                desc = NutritionAnalyzer._short_desc(e.get("description", "?"))
                lines.append(
                    f"{e.get('time', '?')} {desc} · {cal:.0f}к · "
                    f"Б{p:.0f} Ж{f:.0f} У{c:.0f}"
                )
            else:
                lines.append(
                    f"{i}. {e.get('time', '?')} — {e.get('description', '?')}\n"
                    f"   {cal:.0f} ккал | Б {p:.0f}г  Ж {f:.0f}г  У {c:.0f}г"
                )

        lines.append("")

        # Garmin expenditure
        garmin_total = garmin_daily.get("calories_total") if garmin_daily else None

        balance: float | None = None
        if garmin_total:
            stale = garmin_total < 1400
            balance = total_cal - garmin_total
            balance_label = "профицит" if balance > 0 else "дефицит"
            if compact:
                lines.append(f"📊 Съедено: {total_cal:.0f}к · Б{total_p:.0f} Ж{total_f:.0f} У{total_c:.0f}")
                if stale:
                    lines.append(f"🔥 Сожжено: {garmin_total:.0f}к ⚠️ (синк неполный)")
                    balance = None  # don't show misleading balance
                else:
                    lines.append(f"🔥 Сожжено: {garmin_total:.0f}к (Garmin)")
                if balance is not None:
                    sign = "+" if balance > 0 else ""
                    lines.append(f"Δ калории: {sign}{balance:.0f}к ({balance_label})")
            else:
                lines.append(
                    f"📊 Итого: {total_cal:.0f} ккал (съедено) / "
                    f"{garmin_total:.0f} ккал (сожжено)"
                )
                if stale:
                    lines.append("⚠️ Расход неполный — данные с последнего синка Garmin")
                else:
                    lines.append(f"Баланс: {balance:+.0f} ккал ({balance_label})")
        else:
            if compact:
                lines.append(f"📊 Съедено: {total_cal:.0f}к · Б{total_p:.0f} Ж{total_f:.0f} У{total_c:.0f}")
                lines.append("🔥 Сожжено: н/д (нажми ☀️ Утро)")
            else:
                lines.append(f"📊 Итого съедено: {total_cal:.0f} ккал")
                lines.append("Расход Garmin: нажми ☀️ Утро для синхронизации")

        # Macro percentages — в полном отчёте отдельной строкой
        if not compact:
            total_macro_cal = total_p * 4 + total_f * 9 + total_c * 4
            if total_macro_cal > 0:
                pct_p = total_p * 4 / total_macro_cal * 100
                pct_f = total_f * 9 / total_macro_cal * 100
                pct_c = total_c * 4 / total_macro_cal * 100
                lines.append(
                    f"Б: {total_p:.0f}г ({pct_p:.0f}%) | "
                    f"Ж: {total_f:.0f}г ({pct_f:.0f}%) | "
                    f"У: {total_c:.0f}г ({pct_c:.0f}%)"
                )

        # ISSN targets (periodized by training day)
        if weight_kg and weight_kg > 0:
            active_cal = garmin_daily.get("calories_active") if garmin_daily else None
            day_type = NutritionAnalyzer.classify_training_day(plan_line, active_cal)
            targets = NutritionAnalyzer.calculate_issn_targets(weight_kg, day_type)
            day_labels = {
                "rest": "отдых", "regen": "реген/пилатес",
                "light": "лёгкий бег", "quality": "качество/длинная",
                "long_ultra": "ультра-блок", "race": "гонка/загрузка",
            }
            day_label = day_labels.get(day_type, day_type)

            def _delta(actual: float, t: dict) -> tuple[float, str]:
                """Returns (signed_delta_to_range, arrow). Positive=excess, negative=deficit, 0=in-range."""
                if actual < t["min"]:
                    return actual - t["min"], "↓"
                if actual > t["max"]:
                    return actual - t["max"], "↑"
                return 0.0, "✓"

            tp = targets["protein_g"]
            tc = targets["carbs_g"]
            tf = targets["fat_g"]
            dp, ap = _delta(total_p, tp)
            dc, ac = _delta(total_c, tc)
            df, af = _delta(total_f, tf)

            if compact:
                lines.append(f"\n🎯 vs ISSN ({weight_kg:.0f}кг, {day_label}):")
                parts = []
                if balance is not None:
                    bal_arrow = "↑" if balance > 100 else ("↓" if balance < -100 else "✓")
                    bal_sign = "+" if balance >= 0 else ""
                    parts.append(f"К {bal_sign}{balance:.0f} {bal_arrow}")
                parts.append(f"Б {dp:+.0f} {ap}" if dp else f"Б ✓")
                parts.append(f"Ж {df:+.0f} {af}" if df else f"Ж ✓")
                parts.append(f"У {dc:+.0f} {ac}" if dc else f"У ✓")
                lines.append(" | ".join(parts))
            else:
                lines.append(f"\n🎯 Нормы ISSN ({weight_kg:.0f} кг, {day_label} нагрузка):")

                def _status_word(arrow: str) -> str:
                    return {"↓": "мало", "↑": "много", "✓": "норма"}[arrow]

                lines.append(
                    f"Белок:  {total_p:.0f} / {tp['min']:.0f}–{tp['max']:.0f}г "
                    f"{ap} {_status_word(ap)}"
                )
                lines.append(
                    f"Углев.: {total_c:.0f} / {tc['min']:.0f}–{tc['max']:.0f}г "
                    f"{ac} {_status_word(ac)}"
                )
                lines.append(
                    f"Жиры:  {total_f:.0f} / {tf['min']:.0f}–{tf['max']:.0f}г "
                    f"{af} {_status_word(af)}"
                )

        return "\n".join(lines)
