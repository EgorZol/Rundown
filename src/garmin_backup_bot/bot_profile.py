"""Домен «профиль»: анкета (пол/возраст/…/город), часовой пояс, сброс.
"""


import asyncio
import json
import logging
import re

from telegram import (
    Update,
)
from telegram.ext import ContextTypes

from .bot_common import MAIN_KEYBOARD

logger = logging.getLogger(__name__)


PROFILE_QUESTIONS = [
    ("gender", "👤 Пол?\n\nВведи: М или Ж (или «далее» чтобы пропустить)", "profile_gender"),
    ("age", "🎂 Возраст?\n\nНапример: 36 (нужно для расчёта пульсовых зон)", "profile_age"),
    ("running_experience_years", "🏃 Сколько лет бегаешь регулярно?\n\nНапример: 2 (или «далее»)", "profile_experience"),
    ("weight_kg", "⚖️ Вес (кг)?\n\nНаприме��: 72.5 (или «далее» — возьмём из Garmin)", "profile_weight"),
    ("lthr", "💓 Пульс лактатного порога (LTHR)?\n\nНапример: 172 (или «далее» — возьмём из Garmin)", "profile_lthr"),
    ("available_days", "📅 В какие дни недели можешь бегать?\n\nНапиши дни через пробел, например: пн ср пт сб", "profile_days"),
    ("max_session_min_weekday", "⏱ Макс. время на тренировку в будний день (минуты)?\n\nНапример: 60 (или «далее»)", "profile_weekday_min"),
    ("max_session_min_weekend", "⏱ Макс. время на тренировку в выходной день (минуты)?\n\nНапример: 120 (или «далее»)", "profile_weekend_min"),
    ("injuries", "🩹 Есть травмы или ограничения?\n\nОпиши кратко или напиши «нет»", "profile_injuries"),
    ("location_name", "📍 Город, где бегаешь?\n\nНапример: Москва, Минск, Dubai (нужно для прогноза погоды в плане)", "profile_location"),
]


SKIP_TOKENS = {"далее", "дальше", "пропустить", "скип", "skip", "next", "-", "--"}


def _is_skip_token(text: str) -> bool:
    """True, если пользователь хочет пропустить вопрос.

    Терпимо к регистру и хвостовой пунктуации/пробелам, которые часто
    добавляет мобильная клавиатура («Далее.», «далее !», «далее\\n»).
    """
    norm = (text or "").strip().lower().strip(" .!?…,:;")
    return norm in SKIP_TOKENS or text.strip() in ("-", "--")


DAY_ALIASES = {
    "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
    "пнд": 0, "втр": 1, "срд": 2, "чтв": 3, "птн": 4, "суб": 5, "вск": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


DAY_NAMES_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def parse_days(text: str) -> list[int]:
    """«пн, ср сб» → [0, 2, 5] (0=Пн). Пустой список — ничего не распознано."""
    days = []
    for tok in re.split(r"[,\s/]+", (text or "").lower()):
        tok = tok.strip().rstrip(".")
        if tok in DAY_ALIASES:
            days.append(DAY_ALIASES[tok])
    return sorted(set(days))



class ProfileMixin:

    async def handle_timezone_btn(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "any"):
            return
        """Show current timezone and prompt for change."""
        user_id = update.effective_user.id
        overrides = self._storage.get_profile_override(user_id)
        current_tz = overrides.get("timezone") or str(self._tz)
        await update.message.reply_text(
            f"🕐 Текущий часовой пояс: {current_tz}\n\n"
            f"Введи название зоны (примеры):\n"
            f"  Europe/Moscow\n"
            f"  Europe/Berlin\n"
            f"  Asia/Yekaterinburg\n"
            f"  Asia/Novosibirsk\n"
            f"  Asia/Vladivostok\n\n"
            f"Полный список: en.wikipedia.org/wiki/List_of_tz_database_time_zones",
            reply_markup=MAIN_KEYBOARD,
        )
        context.user_data["awaiting"] = "timezone"

    async def handle_profile_btn(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "coach"):
            return
        """Show current profile and start questionnaire for missing fields."""
        self._track_event(update, "profile")
        user_id = update.effective_user.id
        overrides = self._storage.get_profile_override(user_id)

        day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

        # Build current profile summary
        lines = ["📋 Твой профиль:\n"]
        gender_map = {"male": "М", "female": "Ж"}
        g = overrides.get("gender")
        lines.append(f"  Пол: {gender_map.get(g, '—')}")
        user_age = overrides.get("age")
        lines.append(f"  Возраст: {f'{user_age} лет' if user_age is not None else '—'}")
        exp = overrides.get("running_experience_years")
        lines.append(f"  Беговой стаж: {f'{exp:.0f} лет' if exp is not None else '—'}")
        w = overrides.get("weight_kg")
        lines.append(f"  Вес: {f'{w} кг' if w is not None else '— (из Garmin)'}")
        lt = overrides.get("lthr")
        lines.append(f"  LTHR: {f'{lt:.0f} уд/мин' if lt is not None else '— (из Garmin)'}")
        ad = overrides.get("available_days")
        if ad:
            try:
                days_list = json.loads(ad)
                lines.append(f"  Дни бега: {', '.join(day_names[d] for d in sorted(days_list))}")
            except Exception:
                lines.append("  Дни бега: —")
        else:
            lines.append("  Дни бега: —")
        wd = overrides.get("max_session_min_weekday")
        lines.append(f"  Макс. тренировка (будни): {f'{wd} мин' if wd else '—'}")
        we = overrides.get("max_session_min_weekend")
        lines.append(f"  Макс. тренировка (выходные): {f'{we} мин' if we else '—'}")
        inj = overrides.get("injuries")
        lines.append(f"  Травмы/ограничения: {inj if inj else '—'}")
        loc = overrides.get("location_name")
        lines.append(f"  Город: {loc if loc else '—'}")

        # Find first missing field and start questionnaire
        missing = self._next_profile_question(overrides)
        if missing:
            field, question, awaiting_key = missing
            lines.append(f"\n{'—' * 20}\n{question}")
            await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)
            context.user_data["awaiting"] = awaiting_key
            context.user_data["profile_step"] = field
        else:
            lines.append("\n✅ Профиль заполнен! Чтобы изменить — /profile_reset")
            await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)
        await self._send_scale_status(update, user_id)

    async def _send_scale_status(self, update, user_id: int) -> None:
        """Блок про умные весы с кнопкой подключения/отключения."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        creds = self._storage.get_scale_credentials(user_id)
        if creds:
            last = (creds.get("last_sync_at") or "")[:10] or "—"
            text = f"⚖️ Умные весы: подключены (последний синк {last})"
            if creds.get("last_error"):
                text += "\n⚠️ Последний синк не прошёл — возможно, нужно переподключить."
            kb = [[InlineKeyboardButton("Отключить весы", callback_data="scale_off")],
                  [InlineKeyboardButton("Переподключить", callback_data="scale_connect")]]
        else:
            text = ("⚖️ Умные весы не подключены.\n"
                    "Если у тебя весы Xiaomi/Amazfit — могу забирать вес и состав "
                    "тела автоматически.")
            kb = [[InlineKeyboardButton("Подключить весы", callback_data="scale_connect")]]
        await update.effective_message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(kb))

    async def handle_profile_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "coach"):
            return
        """Reset profile questionnaire fields so the user can refill them."""
        user_id = update.effective_user.id
        with self._storage._connect() as conn:
            conn.execute(
                "UPDATE user_profile_overrides SET gender=NULL, age=NULL, available_days=NULL,"
                " max_session_min_weekday=NULL, max_session_min_weekend=NULL,"
                " injuries=NULL, running_experience_years=NULL, weight_kg=NULL, lthr=NULL,"
                " location_name=NULL, location_lat=NULL, location_lon=NULL,"
                " weekly_km_target=NULL,"
                " profile_completed=0, updated_at=CURRENT_TIMESTAMP"
                " WHERE user_id=?",
                (user_id,),
            )
        await update.message.reply_text(
            "🔄 Профиль сброшен. Нажми 📋 Профиль чтобы заполнить заново.",
            reply_markup=MAIN_KEYBOARD,
        )

    @staticmethod
    def _next_profile_question(overrides: dict) -> tuple[str, str, str] | None:
        """Return the next unanswered profile question, or None if all filled/completed."""
        if overrides.get("profile_completed"):
            return None
        for field, question, awaiting_key in PROFILE_QUESTIONS:
            if overrides.get(field) is None:
                return (field, question, awaiting_key)
        return None

    def _advance_profile(self, context: ContextTypes.DEFAULT_TYPE, overrides: dict) -> str | None:
        """Find the next question after the current profile step. Return message or None if done."""
        missing = self._next_profile_question(overrides)
        if missing:
            field, question, awaiting_key = missing
            context.user_data["awaiting"] = awaiting_key
            context.user_data["profile_step"] = field
            return question
        return None

    @staticmethod
    async def _parse_profile_answer(awaiting_key: str, text: str) -> tuple[dict, str | None]:
        """Parse user answer for a profile question.

        Returns (kwargs_for_save, error_message). If error_message is set, kwargs is empty.
        """
        text = text.strip()
        if awaiting_key == "profile_gender":
            t = text.lower()
            if t in ("м", "m", "муж", "мужской", "male"):
                return {"gender": "male"}, None
            if t in ("ж", "f", "жен", "женский", "female"):
                return {"gender": "female"}, None
            return {}, "Введи М или Ж"

        if awaiting_key == "profile_age":
            try:
                v = int(float(text.replace(",", ".")))
                if not (10 <= v <= 100):
                    raise ValueError
                return {"age": v}, None
            except (ValueError, TypeError):
                return {}, "Введи возраст от 10 до 100 (например: 36)"

        if awaiting_key == "profile_experience":
            try:
                v = float(text.replace(",", "."))
                if not (0 <= v <= 50):
                    raise ValueError
                return {"running_experience_years": v}, None
            except (ValueError, TypeError):
                return {}, "Введи число от 0 до 50 (например: 2)"

        if awaiting_key == "profile_weight":
            try:
                v = float(text.replace(",", "."))
                if not (30 < v < 250):
                    raise ValueError
                return {"weight_kg": v}, None
            except (ValueError, TypeError):
                return {}, "Введи вес в кг от 30 до 250 (например: 72.5)"

        if awaiting_key == "profile_lthr":
            try:
                v = float(text.replace(",", "."))
                if not (100 < v < 220):
                    raise ValueError
                return {"lthr": v}, None
            except (ValueError, TypeError):
                return {}, "Введи LTHR от 100 до 220 (например: 172)"

        if awaiting_key == "profile_days":
            days = parse_days(text)
            if not days:
                return {}, "Не распознал дни. Напиши, например: пн ср пт сб"
            return {"available_days": json.dumps(days)}, None

        if awaiting_key == "profile_weekday_min":
            try:
                v = int(float(text.replace(",", ".")))
                if not (15 <= v <= 300):
                    raise ValueError
                return {"max_session_min_weekday": v}, None
            except (ValueError, TypeError):
                return {}, "Введи число минут от 15 до 300 (например: 60)"

        if awaiting_key == "profile_weekend_min":
            try:
                v = int(float(text.replace(",", ".")))
                if not (15 <= v <= 480):
                    raise ValueError
                return {"max_session_min_weekend": v}, None
            except (ValueError, TypeError):
                return {}, "Введи число минут от 15 до 480 (например: 120)"

        if awaiting_key == "profile_injuries":
            t = text.lower().strip()
            if t in ("нет", "не", "нету", "no", "none", "-", "0"):
                return {"injuries": "нет"}, None
            if len(text) > 500:
                return {}, "Слишком длинный текст (макс 500 символов). Опиши кратко."
            return {"injuries": text}, None

        if awaiting_key == "profile_location":
            import urllib.request
            import urllib.parse
            query = urllib.parse.quote(text.strip())
            url = f"https://geocoding-api.open-meteo.com/v1/search?name={query}&count=1&language=ru"
            try:
                def _geocode():
                    with urllib.request.urlopen(url, timeout=5) as resp:
                        return json.loads(resp.read())
                data = await asyncio.to_thread(_geocode)
                results = data.get("results") or []
                if not results:
                    return {}, f"Не нашёл город «{text}». Попробуй на английском, например: Moscow"
                r = results[0]
                name = r.get("name", text)
                country = r.get("country", "")
                display = f"{name}, {country}" if country else name
                return {
                    "location_name": display,
                    "location_lat": round(r["latitude"], 4),
                    "location_lon": round(r["longitude"], 4),
                }, None
            except Exception:
                return {}, "Ошибка геокодинга. Попробуй ещё раз или напиши «далее»."

        return {}, "Неизвестный шаг профиля"
