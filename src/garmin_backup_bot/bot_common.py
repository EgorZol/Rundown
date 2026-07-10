"""Общие константы UI Telegram-бота: кнопки, клавиатуры, сообщения об ошибках.

Импортируется всеми bot_*-миксинами; сам не зависит от них (без циклов).
"""


import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


logger = logging.getLogger(__name__)


BTN_CALORIES = "🔥 Калории"


BTN_FOOD = "🍽 Еда"


BTN_FOOD_REPORT = "📊 Питание"


BTN_GOAL = "🎯 Моя цель"


BTN_MORNING = "🌅 Утро"


BTN_PLAN = "📅 План"


BTN_PROFILE = "📋 Профиль"


BTN_PROGRESS = "📈 Прогресс"


BTN_RACE = "🏁 Старты"


BTN_RECORDS = "🏆 Рекорды"


BTN_SPORT = "🏅 Форма"


BTN_STATUS = "📊 Статус"


BTN_TIMEZONE = "🕐 Часы"


BTN_WEEKLY = "📋 Итоги"


BTN_WORKOUT = "🏃 Разбор"


MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_MORNING), KeyboardButton(BTN_WORKOUT), KeyboardButton(BTN_SPORT)],
        [KeyboardButton(BTN_PLAN), KeyboardButton(BTN_PROGRESS), KeyboardButton(BTN_WEEKLY)],
        [KeyboardButton(BTN_GOAL), KeyboardButton(BTN_RACE), KeyboardButton(BTN_CALORIES)],
        [KeyboardButton(BTN_FOOD), KeyboardButton(BTN_FOOD_REPORT), KeyboardButton(BTN_RECORDS)],
        [KeyboardButton(BTN_STATUS), KeyboardButton(BTN_PROFILE), KeyboardButton(BTN_TIMEZONE)],
    ],
    resize_keyboard=True,
)


FOOD_CONFIRM_KB = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("✅ Сохранить", callback_data="food:save"),
        InlineKeyboardButton("✏️ Изменить", callback_data="food:edit"),
        InlineKeyboardButton("❌ Отмена", callback_data="food:cancel"),
    ]
])


def _api_error_msg(exc: Exception, action: str = "операция") -> str:
    """Return a user-friendly error message, distinguishing transient vs permanent failures."""
    s = str(exc).lower()
    if "rate_limit" in s or "rate limit" in s or "429" in s:
        return f"⏳ Сервис AI временно перегружен (rate limit). Попробуй через минуту."
    if "overloaded" in s or "529" in s or "503" in s:
        return f"⏳ Сервис AI временно недоступен. Попробуй через минуту."
    if "timeout" in s or "timed out" in s:
        return f"⏳ Запрос занял слишком долго. Попробуй ещё раз."
    if "connection" in s or "network" in s:
        return f"⚡ Ошибка сети. Проверь соединение и попробуй снова."
    return f"Не удалось выполнить {action}: {exc}"


def _is_garmin_auth_error(exc: Exception) -> bool:
    """True if the sync exception is a Garmin login/credentials failure."""
    s = str(exc).lower()
    return any(
        marker in s
        for marker in ("401", "unauthorized", "garthhttperror", "sso.garmin.com/sso/signin")
    )
