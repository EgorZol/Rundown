from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import Application, CallbackContext, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .analyst import HealthAnalyst
from .crypto import SecretBox
from .garmin_service import GarminService
from .nutrition import NutritionAnalyzer, NutritionTruncatedError
from .plan_builder import WeeklyPlanBuilder
from .storage import Storage
from .transcription import Transcriber

logger = logging.getLogger(__name__)

BTN_MORNING = "🌅 Утро"
BTN_WORKOUT = "🏃 Разбор пробежки"
BTN_PLAN = "📅 План"
BTN_SPORT = "🏅 Форма за 4 недели"
BTN_GOAL = "🎯 Моя цель"
BTN_MEMORY = "🧠 Заметки"
BTN_STATUS = "📊 Статус"
BTN_CALORIES = "🔥 Калории"
BTN_RACE = "🏁 Старты"
BTN_PROGRESS = "📈 Прогресс"
BTN_WEEKLY = "📋 Итог недели"
BTN_RECORDS = "🏆 Рекорды"
BTN_WEIGHT = "⚖️ Вес"
BTN_LTHR = "💓 LTHR (порог)"
BTN_TIMEZONE = "🕐 Часовой пояс"
BTN_EXPERIENCE = "🏃 Стаж"
BTN_PROFILE = "📋 Профиль"
BTN_FOOD = "🍽 Записать еду"
BTN_FOOD_REPORT = "📊 Питание за день"

# Profile questionnaire: ordered list of (field_name, question_text, awaiting_key)
# All questions support "далее" / "-" to skip.
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


def _is_garmin_auth_error(exc: Exception) -> bool:
    """True if the sync exception is a Garmin login/credentials failure."""
    s = str(exc).lower()
    return any(
        marker in s
        for marker in ("401", "unauthorized", "garthhttperror", "sso.garmin.com/sso/signin")
    )

DAY_ALIASES = {
    "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
    "пнд": 0, "втр": 1, "срд": 2, "чтв": 3, "птн": 4, "суб": 5, "вск": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

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


class GarminBot:
    _MAX_MSG_LEN = 4000

    async def _send_long(self, message, text: str, **kwargs) -> None:
        """reply_text с авто-разбиением >_MAX_MSG_LEN. reply_markup идёт только на последний чанк."""
        chunks = self._split(text, self._MAX_MSG_LEN)
        last = len(chunks) - 1
        kw_no_markup = {k: v for k, v in kwargs.items() if k != "reply_markup"}
        for i, chunk in enumerate(chunks):
            await message.reply_text(chunk, **(kwargs if i == last else kw_no_markup))

    def __init__(
        self,
        app: Application,
        storage: Storage,
        box: SecretBox,
        service: GarminService,
        analyst: HealthAnalyst,
        plan_builder: WeeklyPlanBuilder,
        webapp_base_url: str | None,
        webapp_token_ttl_seconds: int,
        admin_user_ids: set[int],
        user_timezone: str,
        garmin_db_timezone: str | None = None,
        nutrition: NutritionAnalyzer | None = None,
        transcriber: Transcriber | None = None,
    ) -> None:
        self._app = app
        self._storage = storage
        self._box = box
        self._service = service
        self._analyst = analyst
        self._plan_builder = plan_builder
        self._nutrition = nutrition
        self._transcriber = transcriber
        self._webapp_base_url = (webapp_base_url or "").rstrip("/")
        self._webapp_token_ttl_seconds = webapp_token_ttl_seconds
        self._admin_user_ids = admin_user_ids
        self._tz = ZoneInfo(user_timezone)
        # Timezone used by garmindb when storing naive datetimes (defaults to user_timezone)
        self._garmin_db_tz = ZoneInfo(garmin_db_timezone or user_timezone)
        self._sync_locks: dict[int, asyncio.Lock] = {}
        # Лимит параллельных Garmin-синков по всему боту.
        # После миграции на Garth (I/O-bound, 5-10 сек) серверная нагрузка низкая,
        # ограничение нужно только чтобы не упереться в Garmin rate-limit с одного IP.
        # Per-user защита от двойного синка одного юзера — в _sync_locks выше.
        self._global_sync_sem = asyncio.Semaphore(5)
        self._register_handlers()
        self._schedule_reminders()

    def _get_sync_lock(self, user_id: int) -> asyncio.Lock:
        """Get or create a per-user sync lock."""
        if user_id not in self._sync_locks:
            self._sync_locks[user_id] = asyncio.Lock()
        return self._sync_locks[user_id]

    def _get_metrics(self, user_id: int, today, yesterday=None) -> dict | None:
        """Collect daily metrics and apply manual profile overrides (LTHR, weight)."""
        from datetime import date as _date, datetime as _dt
        yd = yesterday or (today - timedelta(days=1))
        metrics = self._service.collect_daily_metrics(user_id, today) \
            or self._service.collect_daily_metrics(user_id, yd)
        if not metrics:
            logger.warning("_get_metrics: no data for user=%s today=%s yd=%s", user_id, today, yd)
            return None
        # Copy to avoid mutating shared/cached data across concurrent users
        metrics = dict(metrics)
        # Always show today's date even if health data (sleep) isn't synced yet
        if metrics.get("date") != today.isoformat():
            metrics["date"] = today.isoformat()
        if metrics:
            overrides = self._storage.get_profile_override(user_id)
            if overrides and metrics.get("fitness_profile"):
                if overrides.get("lthr") is not None:
                    metrics["fitness_profile"]["lthr"] = overrides["lthr"]
                if overrides.get("weight_kg") is not None:
                    metrics["fitness_profile"]["weight_kg"] = overrides["weight_kg"]
                if overrides.get("running_experience_years") is not None:
                    metrics["fitness_profile"]["running_experience_years"] = overrides["running_experience_years"]
                if overrides.get("age") is not None:
                    metrics["fitness_profile"]["age"] = overrides["age"]
                if overrides.get("weekly_km_target") is not None:
                    metrics["fitness_profile"]["weekly_km_target"] = overrides["weekly_km_target"]
                for pf_key in ("gender", "available_days", "max_session_min_weekday", "max_session_min_weekend", "injuries", "location_name", "location_lat", "location_lon"):
                    if overrides.get(pf_key) is not None:
                        metrics["fitness_profile"][pf_key] = overrides[pf_key]
            elif overrides and not metrics.get("fitness_profile"):
                metrics["fitness_profile"] = overrides
            # Extract Garmin HR zone boundaries from most recent running activity
            # hrz_X_hr = FLOOR (lower bound) of zone X
            run_acts = [a for a in (metrics.get("activities_28d") or []) if a.get("sport") == "running" and a.get("hrz_1_hr")]
            if run_acts:
                latest_run = run_acts[0]  # activities_28d is sorted DESC by start_time
                metrics["garmin_zones"] = {
                    f"hrz_{i}_hr": latest_run.get(f"hrz_{i}_hr")
                    for i in range(1, 6)
                    if latest_run.get(f"hrz_{i}_hr")
                }
            # Propagate observed max HR to fitness_profile for fallback zone calculation
            if metrics.get("observed_hr_max") and metrics.get("fitness_profile"):
                metrics["fitness_profile"]["observed_hr_max"] = metrics["observed_hr_max"]
            # Convert sleep start/end from server-local time to user's timezone
            # garmindb stores naive datetimes in server's local timezone (e.g. CET)
            user_tz = self._get_user_tz(user_id)
            sleep_obj = metrics.get("sleep_last_night") or metrics.get("sleep")
            if sleep_obj:
                for key in ("start", "end"):
                    raw = sleep_obj.get(key, "")
                    if raw:
                        try:
                            dt_val = _dt.fromisoformat(str(raw).split(".")[0]).replace(tzinfo=self._garmin_db_tz)
                            local = dt_val.astimezone(user_tz)
                            sleep_obj[key] = local.strftime("%Y-%m-%d %H:%M")
                        except Exception:
                            pass
            # Add yesterday's food so AI can correlate nutrition with recovery/performance
            try:
                yd_date = (today - timedelta(days=1)).isoformat() if hasattr(today, "isoformat") else None
                if yd_date:
                    food_yd = self._storage.get_food_entries(user_id, yd_date)
                    if food_yd:
                        metrics["food_yesterday"] = food_yd
            except Exception as exc:
                logger.debug("_get_metrics: food lookup failed for user=%s: %s", user_id, exc)
        return metrics

    def _get_user_tz(self, user_id: int):
        """Return per-user ZoneInfo, falling back to bot default."""
        overrides = self._storage.get_profile_override(user_id)
        tz_name = overrides.get("timezone")
        if tz_name:
            try:
                return ZoneInfo(tz_name)
            except Exception:
                logger.warning("_get_user_tz: invalid timezone '%s' for user=%s, falling back to default", tz_name, user_id)
        return self._tz

    def _schedule_reminders(self) -> None:
        """Schedule periodic check that dispatches per-user reminders respecting each user's timezone."""
        job_queue = self._app.job_queue
        if job_queue is None:
            logger.warning("JobQueue not available — reminders disabled")
            return
        # Run every 15 minutes; each tick checks every user's local time
        job_queue.run_repeating(self._periodic_jobs_tick, interval=900, first=10, name="periodic_tick")
        logger.info("Periodic job tick scheduled every 15 min")

    async def _periodic_jobs_tick(self, context: CallbackContext) -> None:
        """Every 15 min, check each user's local time and dispatch daily jobs."""
        user_ids = self._storage.get_all_credential_user_ids()
        for user_id in user_ids:
            try:
                tz = self._get_user_tz(user_id)
                now_local = datetime.now(tz)
                hh, mm = now_local.hour, now_local.minute
                # 08:00–08:14 window → daily training reminder
                if hh == 8 and mm < 15:
                    await self._daily_reminder_for_user(user_id, context)
            except Exception as exc:
                logger.debug("Periodic tick failed for user %d: %s", user_id, exc)

    async def _daily_reminder_for_user(self, user_id: int, context: CallbackContext) -> None:
        """Send daily training reminder for a single user."""
        tz = self._get_user_tz(user_id)
        today = datetime.now(tz).date()
        # Deduplicate
        sent_key = f"reminder_sent_{user_id}_{today.isoformat()}"
        if context.bot_data.get(sent_key):
            return
        day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        week_start = (today - timedelta(days=today.weekday())).isoformat()
        today_label = day_names[today.weekday()]
        today_dd_mm = today.strftime("%d.%m")
        plan_row = self._storage.get_plan(user_id, week_start)
        plan = plan_row[0] if plan_row else None
        if not plan:
            return
        today_line = None
        for line in plan.split("\n"):
            stripped = line.strip()
            if stripped.startswith(today_label) and today_dd_mm in stripped:
                today_line = stripped
                break
        if not today_line:
            return
        rest_keywords = ["отдых", "Отдых", "растяжк", "выходной"]
        if any(kw in today_line for kw in rest_keywords):
            return
        activities = self._service.collect_recent_activities(user_id, days=1)
        today_acts = [
            a for a in activities
            if a.get("start_time", "")[:10] == today.isoformat()
        ]
        if today_acts:
            return
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🏃 Сегодня по плану:\n\n{today_line}\n\nУдачной тренировки! После — нажми «{BTN_WORKOUT}»",
            reply_markup=MAIN_KEYBOARD,
        )
        context.bot_data[sent_key] = True
        logger.info("Daily reminder sent to user %d", user_id)

    def _get_garmin_daily_calories(self, user_id: int, day: date) -> dict | None:
        """Get Garmin calorie data for a specific day."""
        metrics = self._service.collect_daily_metrics(user_id, day)
        if not metrics:
            return None
        ds = metrics.get("daily_summary") or {}
        if ds.get("calories_total"):
            return {
                "calories_total": ds.get("calories_total"),
                "calories_bmr": ds.get("calories_bmr"),
                "calories_active": ds.get("calories_active"),
            }
        return None

    def _build_yesterday_nutrition_report(self, user_id: int, yesterday: date) -> str | None:
        """Compact nutrition summary for yesterday — used in утренний отчёт после синка."""
        entries = self._storage.get_food_entries(user_id, yesterday.isoformat())
        if not entries:
            return None
        garmin_daily = self._get_garmin_daily_calories(user_id, yesterday)
        weight_kg = self._get_user_weight(user_id)
        plan_line = self._get_plan_line(user_id, yesterday)
        return NutritionAnalyzer.format_daily_report(
            entries, garmin_daily, weight_kg, yesterday,
            plan_line=plan_line, compact=True,
        )

    def _get_user_weight(self, user_id: int) -> float | None:
        """Get user weight from profile overrides or Garmin."""
        overrides = self._storage.get_profile_override(user_id)
        if overrides and overrides.get("weight_kg"):
            return overrides["weight_kg"]
        # Fallback: try Garmin fitness profile
        today = datetime.now(self._tz).date()
        metrics = self._service.collect_daily_metrics(user_id, today)
        if metrics:
            fp = metrics.get("fitness_profile") or {}
            if fp.get("weight_kg"):
                return fp["weight_kg"]
        return None

    def _get_plan_line(self, user_id: int, day: date) -> str | None:
        """Extract today's line from the weekly plan."""
        day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        week_start = (day - timedelta(days=day.weekday())).isoformat()
        plan_row = self._storage.get_plan(user_id, week_start)
        if not plan_row:
            return None
        plan_text = plan_row[0]
        day_label = day_names[day.weekday()]
        day_dd_mm = day.strftime("%d.%m")
        for line in plan_text.split("\n"):
            stripped = line.strip()
            if stripped.startswith(day_label) and day_dd_mm in stripped:
                return stripped
        return None

    def _register_handlers(self) -> None:
        self._app.add_handler(CommandHandler("start", self.start))
        self._app.add_handler(CommandHandler("link_garmin", self.link_garmin))
        self._app.add_handler(CommandHandler("status", self.status))
        self._app.add_handler(CommandHandler("remember", self.remember))
        self._app.add_handler(CommandHandler("memory", self.show_memory))
        self._app.add_handler(CommandHandler("forget", self.forget_memory))
        self._app.add_handler(CommandHandler("admin_stats", self.admin_stats))
        self._app.add_handler(CommandHandler("plan", self.handle_plan))
        self._app.add_handler(CommandHandler("feeling", self.handle_feeling))
        self._app.add_handler(CommandHandler("goal", self.handle_goal))
        self._app.add_handler(CommandHandler("race", self.handle_race_cmd))
        self._app.add_handler(CommandHandler("profile_reset", self.handle_profile_reset))
        self._app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, self.handle_webapp_data))
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{BTN_MORNING}$"), self.handle_morning)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{BTN_WORKOUT}$"), self.handle_workout)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{BTN_PLAN}$"), self.handle_plan)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{BTN_SPORT}$"), self.handle_sport_status)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{BTN_GOAL}$"), self.handle_goal_btn)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{BTN_MEMORY}$"), self.show_memory)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{BTN_STATUS}$"), self.status)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{BTN_CALORIES}$"), self.handle_calories)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{BTN_RACE}$"), self.handle_race_btn)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{BTN_PROGRESS}$"), self.handle_progress)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{BTN_WEEKLY}$"), self.handle_weekly_summary)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{BTN_RECORDS}$"), self.handle_records)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{re.escape(BTN_WEIGHT)}$"), self.handle_weight_btn)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{re.escape(BTN_LTHR)}$"), self.handle_lthr_btn)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{re.escape(BTN_TIMEZONE)}$"), self.handle_timezone_btn)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{re.escape(BTN_EXPERIENCE)}$"), self.handle_experience_btn)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{re.escape(BTN_PROFILE)}$"), self.handle_profile_btn)
        )
        # Food / nutrition handlers
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{re.escape(BTN_FOOD)}$"), self.handle_food_btn)
        )
        self._app.add_handler(
            MessageHandler(filters.Regex(f"^{re.escape(BTN_FOOD_REPORT)}$"), self.handle_food_report)
        )
        self._app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        self._app.add_handler(MessageHandler(filters.VOICE, self.handle_voice))
        self._app.add_handler(CallbackQueryHandler(self.handle_food_callback, pattern="^food:"))
        self._app.add_handler(CallbackQueryHandler(self.handle_fooddb_callback, pattern="^fdb:"))
        # General text — must be last
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_question))

    # ── Commands ──────────────────────────────────────────────────────────────

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._track_event(update, "start")
        text = (
            "Привет! Я твой персональный тренер по бегу.\n"
            "Анализирую данные с Garmin и составляю индивидуальный план.\n\n"
            "Чтобы начать, нужно 3 шага:\n\n"
            "1. Подключи Garmin \u2014 /link_garmin\n"
            "   Мне нужен доступ к твоим тренировкам, пульсу и сну\n\n"
            f"2. Заполни профиль \u2014 {BTN_PROFILE}\n"
            "   Пол, стаж, доступные дни, ограничения по времени, травмы\n\n"
            f"3. Поставь цель \u2014 {BTN_GOAL}\n"
            "   Например: \xabполумарафон из 1:45\xbb или \xabбегать 3 раза в неделю\xbb\n\n"
            f"После этого жми {BTN_PLAN} \u2014 и получишь первый недельный план.\n"
            "План учитывает твою форму, нагрузку, сон, пульс и цель.\n"
            "Обновляй его хоть каждый день \u2014 он подстраивается под факт.\n\n"
            "Каждое утро:\n"
            f"  {BTN_MORNING} \u2014 брифинг: как восстановился + задание на сегодня\n\n"
            "После пробежки:\n"
            f"  {BTN_WORKOUT} \u2014 разбор: зоны, сплиты, cardiac drift, рекомендации\n\n"
            "Можешь просто написать любой вопрос \u2014 я отвечу на основе твоих данных.\n"
            "Например: \xabпочему у меня пульс вырос?\xbb, \xabготов ли я к полумарафону?\xbb,\n"
            "\xabсравни мои последние две длинные\xbb \u2014 я знаю всю твою историю."
        )
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
        if self._webapp_base_url:
            await self._send_webapp_button(update, context)

    async def link_garmin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._track_event(update, "link_garmin")
        if not self._webapp_base_url:
            await update.message.reply_text("WEBAPP_BASE_URL не задан.", reply_markup=MAIN_KEYBOARD)
            return
        await update.message.reply_text("Открываю форму подключения...", reply_markup=MAIN_KEYBOARD)
        await self._send_webapp_button(update, context)

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._track_event(update, "status")
        user_id = update.effective_user.id
        creds = self._storage.get_credentials(user_id)
        if not creds:
            await update.message.reply_text(
                "Garmin не подключён. Используй /link_garmin", reply_markup=MAIN_KEYBOARD
            )
            return
        sync_info = self._service.get_sync_summary(user_id)
        last_sync = sync_info.last_sync_at if sync_info else "нет данных"
        text = f"Garmin: {creds.username}\nПоследняя синхронизация: {last_sync}"
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)

    async def remember(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Save persistent notes that Claude always sees. Usage: /remember <text>"""
        user_id = update.effective_user.id
        note = " ".join(context.args or []).strip()
        if not note:
            await update.message.reply_text(
                "Использование: /remember <заметка>\n\nПример:\n/remember Моя цель — пробежать полумарафон за 1:45 в мае 2026\n/remember Я предпочитаю тренировки утром, не переношу жару",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        current = self._storage.get_user_memory(user_id)
        updated = (current + "\n" + note).strip() if current else note
        self._storage.set_user_memory(user_id, updated)
        await update.message.reply_text(
            f"Запомнил. Теперь Claude всегда будет учитывать это.\n\nТекущие заметки:\n{updated}",
            reply_markup=MAIN_KEYBOARD,
        )

    async def show_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show current persistent memory."""
        user_id = update.effective_user.id
        notes = self._storage.get_user_memory(user_id)
        if notes:
            await update.message.reply_text(f"Твои заметки для Claude:\n\n{notes}", reply_markup=MAIN_KEYBOARD)
        else:
            await update.message.reply_text(
                "Заметок пока нет. Добавь через /remember <текст>", reply_markup=MAIN_KEYBOARD
            )

    async def forget_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Clear all persistent memory."""
        user_id = update.effective_user.id
        self._storage.set_user_memory(user_id, "")
        await update.message.reply_text("Заметки очищены.", reply_markup=MAIN_KEYBOARD)

    async def handle_goal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Set or view training goal: /goal [description]"""
        user_id = update.effective_user.id
        args = context.args or []
        if not args:
            current = self._storage.get_goal(user_id)
            if current:
                await update.message.reply_text(f"Твоя текущая цель:\n{current}\n\nЧтобы изменить: /goal [новая цель]", reply_markup=MAIN_KEYBOARD)
            else:
                await update.message.reply_text(
                    "Цель не задана.\n\nПример: /goal Марафон в Берлине 28.09.2026, целевое время 3:30, сейчас 40 км/нед",
                    reply_markup=MAIN_KEYBOARD,
                )
            return
        goal_text = " ".join(args)
        self._storage.save_goal(user_id, goal_text)
        today_d = datetime.now(self._get_user_tz(user_id)).date()
        week_start = (today_d - timedelta(days=today_d.weekday())).isoformat()
        self._storage.clear_plan(user_id, week_start)

        # Авто-экстракт гонок из текста цели — если найдены даты, добавляем их
        # в races как is_priority=1 (цель → A-гонка). Уже существующие пропускаем.
        added_lines = await self._sync_races_from_goal(user_id, goal_text, today_d)

        msg = f"Цель сохранена:\n{goal_text}\n\nПлан на неделю пересчитан под новую цель."
        if added_lines:
            msg += "\n\n⭐ Из цели извлечены A-гонки:\n" + "\n".join(added_lines)
        await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)

    async def _sync_races_from_goal(
        self, user_id: int, goal_text: str, today_d,
    ) -> list[str]:
        """Парсит из цели даты+дистанции и заводит их в races c is_priority=1.
        Возвращает список строк для отчёта юзеру."""
        try:
            parsed = await self._analyst.parse_races_from_text(goal_text, today_d.isoformat())
        except Exception as exc:
            logger.warning("goal->races parse failed: %s", exc)
            return []
        if not parsed:
            return []
        existing = self._storage.get_races(user_id)
        existing_keys = {(r["date"], (r.get("name") or "").strip().lower()) for r in existing}
        from datetime import date as _date
        added: list[str] = []
        for r in parsed:
            try:
                rd = _date.fromisoformat(r["date"])
            except (KeyError, TypeError, ValueError):
                continue
            if rd < today_d:
                continue
            key = (rd.isoformat(), (r.get("name") or "").strip().lower())
            if key in existing_keys:
                # Уже есть — просто пометим приоритетной, если ещё не отмечена
                for er in existing:
                    if (er["date"], (er.get("name") or "").strip().lower()) == key and not er.get("is_priority"):
                        self._storage.set_race_priority(user_id, er["id"], True)
                        added.append(
                            f"  ⭐ #{er['id']} {rd.strftime('%d.%m.%Y')} — {er['name']} (помечена приоритетной)"
                        )
                continue
            race_id = self._storage.save_race(
                user_id, rd.isoformat(), r.get("name") or "Цель",
                r.get("distance_km"), r.get("goal_time"), r.get("notes"),
            )
            self._storage.set_race_priority(user_id, race_id, True)
            dist_str = f" {r['distance_km']:.0f}км" if r.get("distance_km") else ""
            added.append(
                f"  ⭐ #{race_id} {rd.strftime('%d.%m.%Y')} — {r.get('name') or 'Цель'}{dist_str}"
            )
        return added

    async def handle_goal_btn(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Button handler: show current goal and instructions to change it."""
        user_id = update.effective_user.id
        current = self._storage.get_goal(user_id)
        if current:
            await update.message.reply_text(
                f"Твоя цель:\n{current}\n\nЧтобы изменить — напиши:\n/goal [новая цель]",
                reply_markup=MAIN_KEYBOARD,
            )
        else:
            await update.message.reply_text(
                "Цель не задана. Задай командой:\n/goal [описание]\n\n"
                "Например:\n/goal Марафон в Берлине 28.09.2026, целевое время 3:30",
                reply_markup=MAIN_KEYBOARD,
            )

    async def handle_feeling(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Save subjective well-being score: /feeling 4 [optional note]"""
        user_id = update.effective_user.id
        args = (context.args or [])
        if not args:
            await update.message.reply_text(
                "Используй: /feeling [1-5] [заметка]\n"
                "1 = очень плохо, 2 = плохо, 3 = нормально, 4 = хорошо, 5 = отлично\n"
                "Пример: /feeling 4 Немного устал после вчерашней тренировки",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        try:
            score = int(args[0])
            if not 1 <= score <= 5:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Оценка должна быть от 1 до 5.", reply_markup=MAIN_KEYBOARD)
            return
        note = " ".join(args[1:]) if len(args) > 1 else ""
        today_d = datetime.now(self._get_user_tz(user_id)).date()
        today = today_d.isoformat()
        self._storage.save_feeling(user_id, today, score, note)
        labels = {1: "очень плохо 😞", 2: "плохо 😕", 3: "нормально 😐", 4: "хорошо 😊", 5: "отлично 💪"}
        msg = f"Самочувствие за {today}: {score}/5 — {labels[score]}"
        if note:
            msg += f"\nЗаметка: {note}"

        # Invalidate cached plan if feelings drop ≤2 (safety: force re-evaluation)
        if score <= 2:
            yesterday = (today_d - timedelta(days=1)).isoformat()
            yesterday_feelings = self._storage.get_feelings(user_id, yesterday)
            yesterday_score = next(
                (f["score"] for f in yesterday_feelings if f["day"] == yesterday), None
            )
            if yesterday_score is not None and yesterday_score <= 2:
                week_start = (today_d - timedelta(days=today_d.weekday())).isoformat()
                self._storage.clear_plan(user_id, week_start)
                msg += "\n\n⚠️ Два дня подряд самочувствие низкое — план на неделю пересчитан."

        await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)

    def _format_race_calendar(self, user_id: int) -> str:
        from datetime import date as _date
        today = _date.today()
        upcoming = self._storage.get_races(user_id, from_date=today.isoformat())
        past = [r for r in self._storage.get_races(user_id) if r["date"] < today.isoformat()]

        lines = ["🏁 КАЛЕНДАРЬ СТАРТОВ\n"]

        if upcoming:
            lines.append("Предстоящие:\n")
            for r in upcoming:
                race_date = _date.fromisoformat(r["date"])
                days_left = (race_date - today).days
                dist = f" · {r['distance_km']:.1f} км" if r["distance_km"] else ""
                goal = f" · цель {r['goal_time']}" if r["goal_time"] else ""
                if days_left == 0:
                    countdown = "СЕГОДНЯ 🔥"
                elif days_left == 1:
                    countdown = "завтра"
                elif days_left < 7:
                    countdown = f"через {days_left} дн."
                elif days_left < 30:
                    weeks = days_left // 7
                    countdown = f"через {weeks} нед. ({days_left} дн.)"
                else:
                    months = days_left // 30
                    countdown = f"через ~{months} мес. ({days_left} дн.)"
                star = " ⭐" if r.get("is_priority") else ""
                entry = (
                    f"🏅 #{r['id']} {r['name']}{dist}{star}\n"
                    f"   📅 {race_date.strftime('%d.%m.%Y')}  ⏳ {countdown}{goal}"
                )
                if r.get("notes"):
                    entry += f"\n   📝 {r['notes']}"
                lines.append(entry + "\n")
        else:
            lines.append("Предстоящих стартов нет.")

        if past:
            lines.append("Прошедшие:")
            for r in past[-3:]:
                dist = f" · {r['distance_km']:.1f} км" if r["distance_km"] else ""
                lines.append(f"  ✓ {r['date']} — {r['name']}{dist}")

        lines.append(
            "\nДобавить: /race ГГГГ-ММ-ДД Название [дистанция] [время]"
            "\nили текстом: /race бегу полумарафон в мае, хочу 1:47"
            "\nУдалить: /race delete #ID"
            "\nПриоритет (A-гонка): /race priority #ID  |  снять: /race unpriority #ID"
        )
        return "\n".join(lines)

    async def handle_race_btn(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._track_event(update, "race_btn")
        user_id = update.effective_user.id
        text = self._format_race_calendar(user_id)
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)

    async def handle_race_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Add or delete a race: /race 2026-05-15 Полумарафон [21.1] [1:45:00]
        Delete: /race delete #3"""
        self._track_event(update, "race_cmd")
        user_id = update.effective_user.id
        args = context.args or []

        if not args:
            await update.message.reply_text(
                "Добавить старт:\n"
                "  Явно:  /race 2026-05-15 Полумарафон 21.1 1:45:00\n"
                "  Текстом: /race Бегу полумарафон в мае, хочу 1:47\n"
                "  (можно вставить любой текст с планами — AI распарсит)\n\n"
                "Удалить: /race delete #3\n\n"
                + self._format_race_calendar(user_id),
                reply_markup=MAIN_KEYBOARD,
            )
            return

        # Delete command
        if args[0].lower() == "delete" and len(args) >= 2:
            try:
                race_id = int(args[1].lstrip("#"))
                if self._storage.delete_race(user_id, race_id):
                    await update.message.reply_text(
                        f"Старт #{race_id} удалён.", reply_markup=MAIN_KEYBOARD
                    )
                else:
                    await update.message.reply_text(
                        f"Старт #{race_id} не найден.", reply_markup=MAIN_KEYBOARD
                    )
            except ValueError:
                await update.message.reply_text("Укажи номер: /race delete #3", reply_markup=MAIN_KEYBOARD)
            return

        # Priority on/off — пометить гонку как A-race (под которую периодизация)
        if args[0].lower() in ("priority", "unpriority") and len(args) >= 2:
            mark = args[0].lower() == "priority"
            try:
                race_id = int(args[1].lstrip("#"))
            except ValueError:
                await update.message.reply_text(
                    "Укажи номер: /race priority #3", reply_markup=MAIN_KEYBOARD,
                )
                return
            if not self._storage.set_race_priority(user_id, race_id, mark):
                await update.message.reply_text(
                    f"Старт #{race_id} не найден.", reply_markup=MAIN_KEYBOARD,
                )
                return
            # план пересчитать — фаза могла измениться
            today_d = datetime.now(self._get_user_tz(user_id)).date()
            ws = (today_d - timedelta(days=today_d.weekday())).isoformat()
            self._storage.clear_plan(user_id, ws)
            verb = "помечен приоритетным ⭐" if mark else "снят с приоритета"
            await update.message.reply_text(
                f"Старт #{race_id} {verb}. План на неделю будет пересчитан при следующем /plan.\n\n"
                + self._format_race_calendar(user_id),
                reply_markup=MAIN_KEYBOARD,
            )
            return

        from datetime import date as _date

        # Check if first arg looks like a date — if not, treat whole text as natural language
        is_date = False
        try:
            _date.fromisoformat(args[0])
            is_date = True
        except ValueError:
            pass

        if not is_date:
            # AI parsing mode: extract races from free-form text
            free_text = " ".join(args)
            status_msg = await update.message.reply_text("Разбираю текст, ищу старты...")
            today = datetime.now(self._get_user_tz(user_id)).date()
            try:
                races = await self._analyst.parse_races_from_text(free_text, today.isoformat())
            except Exception as exc:
                logger.exception("parse_races_from_text failed: %s", exc)
                races = None
            await status_msg.delete()

            if not races:
                await update.message.reply_text(
                    "Не нашёл стартов в тексте. Попробуй явно: /race 2026-05-15 Название 21.1 1:45:00",
                    reply_markup=MAIN_KEYBOARD,
                )
                return

            added = []
            for r in races:
                try:
                    race_date = _date.fromisoformat(r["date"])
                    race_id = self._storage.save_race(
                        user_id, race_date.isoformat(), r["name"],
                        r.get("distance_km"), r.get("goal_time"), r.get("notes"),
                    )
                    days_left = (race_date - today).days
                    dist_str = f" {r['distance_km']:.1f}км" if r.get("distance_km") else ""
                    goal_str = f" → {r['goal_time']}" if r.get("goal_time") else ""
                    added.append(f"  #{race_id} {race_date.strftime('%d.%m.%Y')} — {r['name']}{dist_str}{goal_str} [{days_left} дн.]")
                except Exception as e:
                    logger.warning("Could not save parsed race %s: %s", r, e)

            if not added:
                await update.message.reply_text("Не удалось сохранить старты.", reply_markup=MAIN_KEYBOARD)
                return

            # Invalidate cached plan — race calendar changed
            today_d = datetime.now(self._get_user_tz(user_id)).date()
            ws = (today_d - timedelta(days=today_d.weekday())).isoformat()
            self._storage.clear_plan(user_id, ws)

            reply = "✅ Добавлены старты:\n" + "\n".join(added) + "\n\n" + self._format_race_calendar(user_id)
            for chunk in self._split(reply):
                await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)
            return

        # Structured add: /race YYYY-MM-DD Name [distance] [goal]
        race_date = _date.fromisoformat(args[0])
        if len(args) < 2:
            await update.message.reply_text("Укажи название старта.", reply_markup=MAIN_KEYBOARD)
            return

        name_parts = []
        distance_km = None
        goal_time = None
        for part in args[1:]:
            try:
                distance_km = float(part)
            except ValueError:
                if ":" in part and len(part) <= 8:
                    goal_time = part
                else:
                    name_parts.append(part)

        name = " ".join(name_parts) if name_parts else args[1]
        race_id = self._storage.save_race(
            user_id, race_date.isoformat(), name, distance_km, goal_time
        )

        today = datetime.now(self._get_user_tz(user_id)).date()
        # Invalidate cached plan — race calendar changed
        ws = (today - timedelta(days=today.weekday())).isoformat()
        self._storage.clear_plan(user_id, ws)

        days_left = (race_date - today).days
        dist_str = f" {distance_km:.1f}км" if distance_km else ""
        goal_str = f", цель {goal_time}" if goal_time else ""
        countdown = f"{days_left} дней" if days_left > 0 else "СЕГОДНЯ"
        await update.message.reply_text(
            f"✅ Старт добавлен #{race_id}:\n"
            f"{race_date.strftime('%d.%m.%Y')} — {name}{dist_str}{goal_str}\n"
            f"До старта: {countdown}\n\n"
            + self._format_race_calendar(user_id),
            reply_markup=MAIN_KEYBOARD,
        )

    async def handle_sport_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._track_event(update, "sport_status")
        user_id = update.effective_user.id

        creds = self._storage.get_credentials(user_id)
        if not creds:
            await update.message.reply_text(
                "Сначала подключи Garmin: /link_garmin", reply_markup=MAIN_KEYBOARD
            )
            return

        status_msg = await update.message.reply_text("Анализирую спортивные данные...")
        stop = asyncio.Event()
        spinner = asyncio.create_task(self._animate(status_msg, stop, [
            "Считаю объёмы за 4 недели",
            "Анализирую динамику бега",
            "Оцениваю форму и тренды",
        ]))

        try:
            today = datetime.now(self._get_user_tz(user_id)).date()
            yesterday = today - timedelta(days=1)
            metrics = self._get_metrics(user_id, today)
            training_goal = self._storage.get_goal(user_id)
            upcoming_races = self._storage.get_races(user_id, from_date=today.isoformat())
            # Dynamic weekly km target based on race schedule
            if metrics:
                run_acts = [a for a in (metrics.get("activities_28d") or []) if a.get("sport") == "running"]
                recent_km = sum(a.get("distance", 0) for a in run_acts[-4:]) / max(len(run_acts[-4:]) / (7/7), 1) if run_acts else 30.0
                weekly_km_vals = []
                for i in range(4):
                    ws = (today - timedelta(days=today.weekday() + 7 * i)).isoformat()
                    we = (today - timedelta(days=today.weekday() + 7 * i - 6)).isoformat() if i > 0 else today.isoformat()
                    wk = sum(a.get("distance", 0) for a in run_acts if ws <= a.get("start_time", "")[:10] <= we)
                    if wk > 0:
                        weekly_km_vals.append(wk)
                avg_weekly_km = sum(weekly_km_vals) / len(weekly_km_vals) if weekly_km_vals else 30.0
                target_km, target_label = self._plan_builder.compute_weekly_km_target(upcoming_races, avg_weekly_km)
                metrics["weekly_km_target"] = target_km
                metrics["weekly_km_target_label"] = target_label
            report = await self._analyst.analyze_sport_status(
                metrics or {"date": today.isoformat()},
                training_goal=training_goal,
            )
        except Exception as exc:
            logger.exception("Error generating sport status")
            report = _api_error_msg(exc, "спортивный статус")
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await spinner

        with contextlib.suppress(Exception):
            await status_msg.delete()

        chunks = self._split(report)
        for chunk in chunks:
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

    async def handle_calories(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._track_event(update, "calories")
        user_id = update.effective_user.id

        creds = self._storage.get_credentials(user_id)
        if not creds:
            await update.message.reply_text(
                "Сначала подключи Garmin: /link_garmin", reply_markup=MAIN_KEYBOARD
            )
            return

        status_msg = await update.message.reply_text("Считаю калории за неделю...")

        try:
            today = datetime.now(self._get_user_tz(user_id)).date()
            yesterday = today - timedelta(days=1)
            metrics = self._get_metrics(user_id, today)
            report = await self._analyst.analyze_calories(
                metrics or {"date": today.isoformat()},
                today=today,
            )
        except Exception as exc:
            logger.exception("Error generating calorie report")
            report = _api_error_msg(exc, "анализ калорий")

        with contextlib.suppress(Exception):
            await status_msg.delete()

        chunks = self._split(report)
        for chunk in chunks:
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

    # ── Food / Nutrition handlers ────────────────────────────────────────────

    async def handle_food_btn(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Activate food logging mode."""
        self._track_event(update, "food_mode")
        if not self._nutrition:
            await update.message.reply_text(
                "Модуль питания не настроен.", reply_markup=MAIN_KEYBOARD,
            )
            return
        context.user_data["awaiting"] = "food"
        await update.message.reply_text(
            "🍽 Режим записи еды\n\n"
            "Отправь мне:\n"
            "📷 Фото еды\n"
            "🎤 Голосовое описание\n"
            "✏️ Или напиши текстом\n\n"
            "Я оценю калории и БЖУ.\n\n"
            "📅 Задним числом: укажи дату в сообщении или подписи к фото "
            "(«вчера», «16 мая», «16.05») — например «вчера борщ».\n"
            "Для выхода — нажми любую другую кнопку.",
            reply_markup=MAIN_KEYBOARD,
        )

    def _resolve_food_date(
        self, text: str, context: ContextTypes.DEFAULT_TYPE, user_id: int
    ) -> tuple[str | None, str]:
        """Вернуть (entry_date ISO | None, текст без даты).

        Парсит дату из сообщения. Если даты нет — берёт «липкую» дату,
        заданную ранее (``food_date``). Явное «сегодня» сбрасывает её.
        """
        today = datetime.now(self._get_user_tz(user_id)).date()
        parsed, cleaned = NutritionAnalyzer.parse_entry_date(text or "", today)
        if parsed is not None:
            if parsed == today:
                context.user_data.pop("food_date", None)
                return None, cleaned
            return parsed.isoformat(), cleaned
        sticky = context.user_data.get("food_date")
        return (sticky or None), (text or "")

    async def _prompt_food_date_only(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        date_iso: str | None,
    ) -> bool:
        """Если в сообщении только дата без еды — запомнить и попросить еду.

        Возвращает True, если сообщение обработано (нужен ``return`` у вызова).
        """
        from datetime import date as _date

        if date_iso:
            context.user_data["food_date"] = date_iso
            d = _date.fromisoformat(date_iso).strftime("%d.%m.%Y")
            await update.message.reply_text(
                f"📅 Записываю за {d}.\n"
                "Теперь пришли еду — фото, голосом или текстом.",
                reply_markup=MAIN_KEYBOARD,
            )
        else:
            await update.message.reply_text(
                "Опиши еду или пришли фото.", reply_markup=MAIN_KEYBOARD,
            )
        return True

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle photo messages — food recognition when in food mode."""
        import base64
        import io

        awaiting = context.user_data.get("awaiting")
        if awaiting != "food":
            await update.message.reply_text(
                "Чтобы записать еду, сначала нажми 🍽 Записать еду",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        if not self._nutrition:
            await update.message.reply_text("Модуль питания не настроен.", reply_markup=MAIN_KEYBOARD)
            return

        status_msg = await update.message.reply_text("📷 Анализирую фото...")
        try:
            photo = update.message.photo[-1]  # largest size
            if photo.file_size and photo.file_size > 10 * 1024 * 1024:
                await status_msg.edit_text("Фото слишком большое (макс 10 МБ).")
                return
            file = await context.bot.get_file(photo.file_id)
            buf = io.BytesIO()
            await file.download_to_memory(buf)
            b64_data = base64.b64encode(buf.getvalue()).decode("utf-8")

            raw_caption = (update.message.caption or "").strip()
            date_iso, caption = self._resolve_food_date(
                raw_caption, context, update.effective_user.id
            )
            caption = caption.strip() or None
            if caption:
                logger.info("Food photo with caption (len=%d): %r", len(caption), caption[:200])
            result = await self._nutrition.analyze_photo(
                b64_data, "image/jpeg", caption=caption,
            )
            if date_iso:
                result["entry_date"] = date_iso
        except Exception as exc:
            logger.exception("Food photo analysis failed")
            with contextlib.suppress(Exception):
                await status_msg.delete()
            await update.message.reply_text(
                f"Не удалось распознать еду: {exc}", reply_markup=MAIN_KEYBOARD,
            )
            return

        with contextlib.suppress(Exception):
            await status_msg.delete()

        if result.get("confidence") == "none":
            await update.message.reply_text(
                "На фото не еда. Попробуй другое фото или опиши текстом.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        context.user_data["pending_food"] = result
        context.user_data["pending_food_source"] = "photo"
        text = NutritionAnalyzer.format_food_confirmation(result)
        await update.message.reply_text(text, reply_markup=FOOD_CONFIRM_KB)

    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle voice messages — transcribe, then route to food or question."""
        import io

        if not self._transcriber:
            await update.message.reply_text(
                "Голосовые сообщения не настроены (нужен OPENAI_API_KEY).",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        status_msg = await update.message.reply_text("🎤 Распознаю речь...")
        try:
            voice = update.message.voice
            file = await context.bot.get_file(voice.file_id)
            buf = io.BytesIO()
            await file.download_to_memory(buf)
            text = await self._transcriber.transcribe(buf, voice.mime_type or "audio/ogg")
        except Exception as exc:
            logger.exception("Voice transcription failed")
            with contextlib.suppress(Exception):
                await status_msg.delete()
            await update.message.reply_text(
                f"Не удалось распознать речь: {exc}", reply_markup=MAIN_KEYBOARD,
            )
            return

        with contextlib.suppress(Exception):
            await status_msg.delete()

        if not text:
            await update.message.reply_text("Не удалось распознать речь.", reply_markup=MAIN_KEYBOARD)
            return

        awaiting = context.user_data.get("awaiting")
        if awaiting in ("food", "food_edit") and self._nutrition:
            # Food mode (or edit mode): analyze transcribed text as food
            await update.message.reply_text(f"🎤 Распознано: {text}")
            date_iso, food_text = self._resolve_food_date(
                text, context, update.effective_user.id
            )
            if awaiting == "food" and not food_text.strip():
                await self._prompt_food_date_only(update, context, date_iso)
                return
            status_msg2 = await update.message.reply_text("🍽 Оцениваю калории...")
            try:
                result = await self._nutrition.analyze_text(food_text)
            except NutritionTruncatedError as exc:
                logger.warning("Food text truncated: %s", exc)
                with contextlib.suppress(Exception):
                    await status_msg2.delete()
                await update.message.reply_text(
                    f"⚠️ {exc}", reply_markup=MAIN_KEYBOARD,
                )
                return
            except Exception as exc:
                logger.exception("Food text analysis failed")
                with contextlib.suppress(Exception):
                    await status_msg2.delete()
                await update.message.reply_text(
                    f"Не удалось оценить еду: {exc}", reply_markup=MAIN_KEYBOARD,
                )
                return

            with contextlib.suppress(Exception):
                await status_msg2.delete()

            if result.get("confidence") == "none":
                await update.message.reply_text(
                    "Не похоже на еду. Попробуй описать подробнее.",
                    reply_markup=MAIN_KEYBOARD,
                )
                return

            if date_iso:
                result["entry_date"] = date_iso
            context.user_data["pending_food"] = result
            context.user_data["pending_food_source"] = "voice"
            confirmation = NutritionAnalyzer.format_food_confirmation(result)
            await update.message.reply_text(confirmation, reply_markup=FOOD_CONFIRM_KB)
        else:
            # General mode: show transcription, then route through handle_question
            # so awaiting states (weight, lthr, timezone, profile, etc.) are respected
            await update.message.reply_text(f"🎤 {text}")
            # Inject transcribed text into user_data and delegate to handle_question
            context.user_data["_voice_text"] = text
            await self.handle_question(update, context)

    async def handle_food_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline keyboard callbacks for food confirmation."""
        query = update.callback_query
        await query.answer()
        data = query.data
        user_id = update.effective_user.id

        if data == "food:save":
            pending = context.user_data.pop("pending_food", None)
            source = context.user_data.pop("pending_food_source", "text")
            if not pending:
                await query.edit_message_text("Данные устарели, попробуй заново.")
                return

            tz = self._get_user_tz(user_id)
            now = datetime.now(tz)
            entry_date = pending.get("entry_date") or now.date().isoformat()
            entry_id = self._storage.save_food_entry(
                user_id=user_id,
                entry_date=entry_date,
                entry_time=now.strftime("%H:%M"),
                description=pending["description"],
                calories=pending["calories"],
                protein_g=pending["protein_g"],
                fat_g=pending["fat_g"],
                carbs_g=pending["carbs_g"],
                confidence=pending.get("confidence", "medium"),
                source=source,
                raw_response=pending.get("raw"),
            )
            from datetime import date as _date

            today_iso = now.date().isoformat()
            date_note = ""
            if entry_date != today_iso:
                with contextlib.suppress(ValueError):
                    date_note = (
                        f" за {_date.fromisoformat(entry_date).strftime('%d.%m.%Y')}"
                    )
            await query.edit_message_text(
                f"✅ Сохранено{date_note}! (#{entry_id})\n"
                f"{pending['description']}: {pending['calories']:.0f} ккал\n\n"
                "Для следующего приёма пищи снова нажми 🍽 Записать еду."
            )
            context.user_data.pop("awaiting", None)
            context.user_data.pop("food_date", None)

        elif data == "food:edit":
            pending = context.user_data.get("pending_food")
            if not pending:
                await query.edit_message_text(
                    "Данные распознавания устарели — пришли еду заново."
                )
                context.user_data["awaiting"] = "food"
                return
            current_line = (
                f"🍽 {pending['description']}\n"
                f"🔥 {pending['calories']:.0f} ккал | "
                f"Б {pending['protein_g']:.0f}г  "
                f"Ж {pending['fat_g']:.0f}г  "
                f"У {pending['carbs_g']:.0f}г"
            )
            await query.edit_message_text(
                "✏️ Что поправить? Напиши только то, что не так — остальное "
                "останется как есть.\n\n"
                f"Сейчас распознано:\n{current_line}\n\n"
                "Примеры:\n"
                "• «не куриная грудка, а индейка»\n"
                "• «риса было 250г, а не 150г»\n"
                "• «добавь стакан кефира»\n"
                "• «убери хлеб»"
            )
            context.user_data["awaiting"] = "food_edit"

        elif data == "food:cancel":
            context.user_data.pop("pending_food", None)
            context.user_data.pop("pending_food_source", None)
            await query.edit_message_text("❌ Отменено.")
            context.user_data.pop("awaiting", None)
            context.user_data.pop("food_date", None)

    async def handle_food_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show daily nutrition report for today."""
        self._track_event(update, "food_report")
        user_id = update.effective_user.id
        today = datetime.now(self._get_user_tz(user_id)).date()

        entries = self._storage.get_food_entries(user_id, today.isoformat())
        if not entries:
            await update.message.reply_text(
                f"Сегодня ({today.strftime('%d.%m')}) нет записей о еде.\n"
                "Нажми 🍽 Записать еду чтобы начать записывать.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        garmin_daily = self._get_garmin_daily_calories(user_id, today)
        weight_kg = self._get_user_weight(user_id)
        plan_line = self._get_plan_line(user_id, today)
        report = NutritionAnalyzer.format_daily_report(
            entries, garmin_daily, weight_kg, today, plan_line=plan_line,
        )
        for chunk in self._split(report):
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

        context.user_data["food_report_date"] = today.isoformat()
        text, kb = self._food_manage_view(user_id, today.isoformat())
        await update.message.reply_text(text, reply_markup=kb)

    def _food_manage_view(
        self, user_id: int, date_iso: str
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Текст списка записей + inline-клавиатура управления (✏️/🗑) с
        навигацией по дням (◀ / ▶)."""
        from datetime import date as _date, timedelta as _td

        entries = self._storage.get_food_entries(user_id, date_iso)
        try:
            d = _date.fromisoformat(date_iso)
            d_label = d.strftime("%d.%m.%Y")
        except ValueError:
            d = datetime.now(self._get_user_tz(user_id)).date()
            d_label = date_iso

        today = datetime.now(self._get_user_tz(user_id)).date()
        prev_iso = (d - _td(days=1)).isoformat()
        next_iso = (d + _td(days=1)).isoformat()
        nav = [InlineKeyboardButton("◀ Пред. день", callback_data=f"fdb:g:{prev_iso}")]
        if d < today:
            nav.append(
                InlineKeyboardButton("След. день ▶", callback_data=f"fdb:g:{next_iso}")
            )

        rows: list[list[InlineKeyboardButton]] = []
        if not entries:
            text = (
                f"📋 За {d_label} записей нет.\n"
                "Листай дни кнопками ниже."
            )
            rows.append(nav)
            return text, InlineKeyboardMarkup(rows)

        lines = [f"📋 Записи за {d_label} — нажми, чтобы изменить или удалить:"]
        for i, e in enumerate(entries, 1):
            desc = e["description"]
            short = desc if len(desc) <= 28 else desc[:27] + "…"
            lines.append(
                f"{i}. {e['time']} — {desc} "
                f"({e['calories']:.0f} ккал)"
            )
            rows.append([
                InlineKeyboardButton(
                    f"✏️ {i}. {short}", callback_data=f"fdb:e:{e['id']}"
                ),
                InlineKeyboardButton("🗑", callback_data=f"fdb:d:{e['id']}"),
            ])
        rows.append(nav)
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    async def handle_fooddb_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Управление записями еды: правка/удаление из отчёта."""
        from datetime import date as _date

        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        parts = query.data.split(":")  # fdb:<action>:<id>
        action = parts[1] if len(parts) > 1 else ""
        # parts[2] (id записи) может прийти искажённым от клиента — парсим безопасно.
        try:
            arg_id: int | None = int(parts[2]) if len(parts) > 2 else None
        except ValueError:
            arg_id = None
        date_iso = context.user_data.get(
            "food_report_date",
            datetime.now(self._get_user_tz(user_id)).date().isoformat(),
        )

        async def _refresh(note: str | None = None) -> None:
            text, kb = self._food_manage_view(user_id, date_iso)
            if note:
                text = f"{note}\n\n{text}"
            await query.edit_message_text(text, reply_markup=kb)

        if action == "g":  # перейти к другому дню
            try:
                _date.fromisoformat(parts[2])  # валидация
                date_iso = parts[2]
            except (IndexError, ValueError):
                pass
            context.user_data["food_report_date"] = date_iso
            await _refresh()
            return

        if action == "d":  # запросить подтверждение удаления
            if arg_id is None:
                await _refresh("⚠️ Некорректная команда.")
                return
            entry_id = arg_id
            entry = self._storage.get_food_entry(user_id, entry_id)
            if not entry:
                await _refresh("⚠️ Запись уже удалена.")
                return
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🗑 Удалить", callback_data=f"fdb:dy:{entry_id}"
                ),
                InlineKeyboardButton("↩️ Отмена", callback_data="fdb:dn"),
            ]])
            await query.edit_message_text(
                f"Удалить запись?\n\n"
                f"🍽 {entry['description']}\n"
                f"🔥 {entry['calories']:.0f} ккал | "
                f"Б {entry['protein_g']:.0f}г Ж {entry['fat_g']:.0f}г "
                f"У {entry['carbs_g']:.0f}г",
                reply_markup=kb,
            )
            return

        if action == "dy":  # подтверждённое удаление
            if arg_id is None:
                await _refresh("⚠️ Некорректная команда.")
                return
            entry_id = arg_id
            ok = self._storage.delete_food_entry(user_id, entry_id)
            await _refresh("✅ Удалено." if ok else "⚠️ Запись не найдена.")
            return

        if action == "dn":  # отмена удаления
            await _refresh()
            return

        if action == "e":  # начать правку записи
            if arg_id is None:
                await _refresh("⚠️ Некорректная команда.")
                return
            entry_id = arg_id
            entry = self._storage.get_food_entry(user_id, entry_id)
            if not entry:
                await _refresh("⚠️ Запись уже удалена.")
                return
            context.user_data["awaiting"] = "food_db_edit"
            context.user_data["food_db_edit_id"] = entry_id
            context.user_data["food_db_edit_date"] = entry["date"]
            try:
                d_label = _date.fromisoformat(entry["date"]).strftime("%d.%m.%Y")
            except ValueError:
                d_label = entry["date"]
            await query.edit_message_text(
                f"✏️ Правка записи за {d_label}:\n"
                f"🍽 {entry['description']}\n"
                f"🔥 {entry['calories']:.0f} ккал | "
                f"Б {entry['protein_g']:.0f}г Ж {entry['fat_g']:.0f}г "
                f"У {entry['carbs_g']:.0f}г\n\n"
                "Напиши, что поправить (или новое описание целиком). "
                "Можно сменить дату — например «перенеси на вчера», «16.05».\n"
                "Примеры:\n"
                "• «риса было 250г, а не 150г»\n"
                "• «добавь стакан кефира»\n"
                "• «это был не обед, а ужин: паста с курицей 400г»"
            )
            return

    # ── Button handlers ───────────────────────────────────────────────────────

    async def handle_morning(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._track_event(update, "morning")
        user_id = update.effective_user.id

        creds = self._storage.get_credentials(user_id)
        if not creds:
            await update.message.reply_text(
                "Сначала подключи Garmin: /link_garmin", reply_markup=MAIN_KEYBOARD
            )
            return

        lock = self._get_sync_lock(user_id)
        if lock.locked():
            await update.message.reply_text(
                "Уже идёт синхронизация, подожди немного...", reply_markup=MAIN_KEYBOARD
            )
            return

        status_msg = await update.message.reply_text("Синхронизирую данные с Garmin...")
        stop = asyncio.Event()
        spinner = asyncio.create_task(self._animate(status_msg, stop, [
            "Синхронизирую сон и показатели",
            "Загружаю Body Battery и стресс",
            "Собираю тренды за неделю",
            "Отправляю данные в AI",
            "Готовлю утренний отчёт",
        ]))

        try:
            async with lock:
                password = self._box.decrypt(creds.password_encrypted)
                sync_exc: Exception | None = None
                try:
                    async with self._global_sync_sem:
                        await asyncio.to_thread(
                            self._service.run_health_sync,
                            user_id=user_id,
                            username=creds.username,
                            password=password,
                        )
                except Exception as exc:
                    sync_exc = exc
                    logger.warning("Health sync error (will use cached data): %s", exc)

                today = datetime.now(self._get_user_tz(user_id)).date()
                yesterday = today - timedelta(days=1)
                metrics = self._get_metrics(user_id, today)

            if not metrics:
                stop.set()
                with contextlib.suppress(Exception):
                    await spinner
                if sync_exc and _is_garmin_auth_error(sync_exc):
                    await status_msg.edit_text(
                        "❌ Garmin не пускает с этим логином/паролем.\n\n"
                        "Возможные причины:\n"
                        "• Опечатка в email или пароле — введи заново через /link_garmin\n"
                        "• В аккаунте Garmin включена двухфакторная аутентификация "
                        "(2FA) — её нужно отключить в настройках Garmin Connect, "
                        "бот пока не поддерживает 2FA\n"
                        "• Аккаунт зарегистрирован на garmin.cn (Китай) — не "
                        "поддерживается\n\n"
                        "Перепроверь и попробуй ещё раз: /link_garmin"
                    )
                else:
                    await status_msg.edit_text(
                        "Нет данных за вчера. Убедись, что часы синхронизированы с Garmin Connect."
                    )
                return

            # Add subjective feelings (last 7 days) to metrics
            seven_days_ago = (today - timedelta(days=6)).isoformat()
            feelings = self._storage.get_feelings(user_id, seven_days_ago)
            if feelings:
                metrics["feelings"] = feelings

            # Add training goal to metrics
            training_goal = self._storage.get_goal(user_id)
            if training_goal:
                metrics["training_goal"] = training_goal

            # Add upcoming races to metrics
            upcoming_races = self._storage.get_races(user_id, from_date=today.isoformat())
            if upcoming_races:
                metrics["upcoming_races"] = upcoming_races

            # Dynamic weekly km target based on race schedule
            run_acts = [a for a in (metrics.get("activities_28d") or []) if a.get("sport") == "running"]
            weekly_km_vals = []
            for i in range(4):
                ws = (today - timedelta(days=today.weekday() + 7 * i)).isoformat()
                we = (today - timedelta(days=today.weekday() + 7 * i - 6)).isoformat() if i > 0 else today.isoformat()
                wk = sum(a.get("distance", 0) for a in run_acts if ws <= a.get("start_time", "")[:10] <= we)
                if wk > 0:
                    weekly_km_vals.append(wk)
            avg_weekly_km = sum(weekly_km_vals) / len(weekly_km_vals) if weekly_km_vals else 30.0
            target_km, target_label = self._plan_builder.compute_weekly_km_target(upcoming_races, avg_weekly_km)
            metrics["weekly_km_target"] = target_km
            metrics["weekly_km_target_label"] = target_label

            header = self._analyst.format_header(metrics, tz=self._get_user_tz(user_id))
            # Только morning-история, чтобы QA-обсуждения с извинениями не загрязняли бриф
            history = self._storage.get_history(user_id, limit=10, source="morning")
            user_memory = self._storage.get_user_memory(user_id)
            analysis = await self._analyst.analyze(metrics, history=history, user_memory=user_memory)
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await spinner

        with contextlib.suppress(Exception):
            await status_msg.delete()

        full_text = header + "\n" + analysis
        chunks = self._split(full_text)
        for chunk in chunks:
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

        # Yesterday's nutrition summary (sent after the main brief, uses freshly-synced Garmin data)
        nutrition_report = self._build_yesterday_nutrition_report(user_id, yesterday)
        if nutrition_report:
            for chunk in self._split(nutrition_report):
                await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

        # Save to conversation history (truncate long reports to save context)
        self._storage.add_message(user_id, "user", BTN_MORNING, source="morning")
        self._storage.add_message(user_id, "assistant", full_text[:800], source="morning")

    async def handle_workout(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._track_event(update, "workout")
        user_id = update.effective_user.id

        creds = self._storage.get_credentials(user_id)
        if not creds:
            await update.message.reply_text(
                "Сначала подключи Garmin: /link_garmin", reply_markup=MAIN_KEYBOARD
            )
            return

        lock = self._get_sync_lock(user_id)
        if lock.locked():
            await update.message.reply_text(
                "Уже идёт синхронизация, подожди немного...", reply_markup=MAIN_KEYBOARD
            )
            return

        status_msg = await update.message.reply_text("Синхронизирую тренировки...")
        stop = asyncio.Event()
        spinner = asyncio.create_task(self._animate(status_msg, stop, [
            "Синхронизирую активности с Garmin",
            "Загружаю данные тренировки",
            "Анализирую нагрузку и пульс",
            "Готовлю разбор тренировки",
        ]))

        try:
            async with lock:
                password = self._box.decrypt(creds.password_encrypted)
                sync_exc: Exception | None = None
                try:
                    async with self._global_sync_sem:
                        await asyncio.to_thread(
                            self._service.run_activity_sync,
                            user_id=user_id,
                            username=creds.username,
                            password=password,
                        )
                except Exception as exc:
                    # Partial failure is OK — garmindb sometimes crashes on individual FIT files.
                    # Continue with whatever data is already in the local DB.
                    sync_exc = exc
                    logger.warning("Activity sync error (will use cached data): %s", exc)

                activities = self._service.collect_recent_activities(user_id, days=14)
                # Health state for workout context: same date logic as morning report.
                # daily_summary from yesterday (complete day), sleep from today (wake date = today = last night).
                today = datetime.now(self._get_user_tz(user_id)).date()
                yesterday = today - timedelta(days=1)
                daily_metrics = self._get_metrics(user_id, today, yesterday)
                if daily_metrics:
                    sleep_today = self._service.collect_sleep_for_date(user_id, today)
                    if sleep_today:
                        daily_metrics["sleep"] = sleep_today
                    # HRV is stored with today's date (last night's measurement)
                    # Override yesterday's HRV with today's if available
                    hrv_today = self._service.collect_hrv_for_date(user_id, today)
                    if hrv_today:
                        daily_metrics["hrv"] = hrv_today

            if not activities:
                stop.set()
                with contextlib.suppress(Exception):
                    await spinner
                if sync_exc and _is_garmin_auth_error(sync_exc):
                    await status_msg.edit_text(
                        "❌ Garmin не пускает с этим логином/паролем.\n\n"
                        "Возможные причины:\n"
                        "• Опечатка в email или пароле — введи заново через /link_garmin\n"
                        "• В аккаунте Garmin включена двухфакторная аутентификация "
                        "(2FA) — её нужно отключить в настройках Garmin Connect, "
                        "бот пока не поддерживает 2FA\n"
                        "• Аккаунт зарегистрирован на garmin.cn (Китай) — не "
                        "поддерживается\n\n"
                        "Перепроверь и попробуй ещё раз: /link_garmin"
                    )
                else:
                    await status_msg.edit_text(
                        "Нет тренировок за последние 7 дней. Убедись, что часы синхронизированы с Garmin Connect."
                    )
                return

            user_memory = self._storage.get_user_memory(user_id)
            # Include current week's plan for comparison (if exists)
            week_start = (today - timedelta(days=today.weekday())).isoformat()
            cached_plan_row = self._storage.get_plan(user_id, week_start)
            cached_plan = cached_plan_row[0] if cached_plan_row else None
            # Workout analysis is self-contained — passing 60 messages of prior workout analyses
            # bloats input to ~17k tokens and causes response truncation.
            analysis = await self._analyst.analyze_workout(
                activities, daily_metrics, history=None,
                user_memory=user_memory, plan_text=cached_plan,
            )
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await spinner

        with contextlib.suppress(Exception):
            await status_msg.delete()

        chunks = self._split(analysis)
        for chunk in chunks:
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

        # Save to conversation history (truncate long reports to save context)
        self._storage.add_message(user_id, "user", BTN_WORKOUT, source="workout")
        self._storage.add_message(user_id, "assistant", analysis[:800], source="workout")

    async def handle_plan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._track_event(update, "plan")
        user_id = update.effective_user.id

        creds = self._storage.get_credentials(user_id)
        if not creds:
            await update.message.reply_text(
                "Сначала подключи Garmin: /link_garmin", reply_markup=MAIN_KEYBOARD
            )
            return

        today = datetime.now(self._get_user_tz(user_id)).date()
        week_start = (today - timedelta(days=today.weekday())).isoformat()

        # Return cached plan only if generated today (stale cache misses new workouts)
        cached_row = self._storage.get_plan(user_id, week_start)
        if cached_row:
            plan_text_cached, generated_at = cached_row
            try:
                from datetime import timezone as _tz
                gen_date = datetime.fromisoformat(generated_at).astimezone(self._get_user_tz(user_id)).date()
            except Exception:
                gen_date = None
            if gen_date == today:
                chunks = self._split(plan_text_cached)
                for chunk in chunks:
                    await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)
                return
            # Cache is from a previous day — regenerate to pick up new workouts

        status_msg = await update.message.reply_text("Анализирую данные и составляю план...")
        stop = asyncio.Event()
        spinner = asyncio.create_task(self._animate(status_msg, stop, [
            "Определяю тип недели",
            "Извлекаю реальные темпы из тренировок",
            "Строю план по методологии тренера",
            "Финализирую расписание",
        ]))

        lock = self._get_sync_lock(user_id)
        if lock.locked():
            stop.set()
            with contextlib.suppress(Exception):
                await spinner
            await status_msg.edit_text("Уже идёт синхронизация, подожди немного…")
            return

        try:
            async with lock:
                password = self._box.decrypt(creds.password_encrypted)
                # Sync activities so the plan sees the latest workouts
                try:
                    async with self._global_sync_sem:
                        await asyncio.to_thread(
                            self._service.run_activity_sync,
                            user_id=user_id,
                            username=creds.username,
                            password=password,
                        )
                except Exception as exc:
                    logger.warning("Activity sync before plan failed (using cached data): %s", exc)
                yesterday = today - timedelta(days=1)
                metrics = self._get_metrics(user_id, today)

                history = self._storage.get_history(user_id, limit=10)
                user_memory = self._storage.get_user_memory(user_id)
                training_goal = self._storage.get_goal(user_id)
                upcoming_races = self._storage.get_races(user_id, from_date=today.isoformat())
                # Also fetch recent past races for post-race recovery detection
                past_races_since = (today - timedelta(days=21)).isoformat()
                all_recent_races = self._storage.get_races(user_id, from_date=past_races_since)
                past_races = [r for r in all_recent_races if r["date"] < today.isoformat()]
                # Get recent feelings for safety checks in determine_week_type
                from datetime import date as _date
                feelings_since = (_date.today() - timedelta(days=6)).isoformat()
                recent_feelings = self._storage.get_feelings(user_id, feelings_since)
                # Pass previous plan for continuity (even if stale — LLM sees what was planned)
                prev_plan_row = self._storage.get_plan(user_id, week_start)
                prev_plan = prev_plan_row[0] if prev_plan_row else ""
                plan_text, week_type = await self._plan_builder.generate_plan(
                    user_id=user_id,
                    metrics=metrics or {"date": today.isoformat()},
                    history=history,
                    user_memory=user_memory,
                    training_goal=training_goal,
                    upcoming_races=upcoming_races,
                    feelings=recent_feelings,
                    previous_plan=prev_plan,
                    past_races=past_races,
                )
                self._storage.save_plan(user_id, week_start, plan_text, week_type)
        except Exception as exc:
            logger.exception("Error generating plan")
            plan_text = _api_error_msg(exc, "составление плана")
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await spinner

        with contextlib.suppress(Exception):
            await status_msg.delete()

        chunks = self._split(plan_text)
        for chunk in chunks:
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

    async def handle_progress(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show progress report with race predictions, weight trend, PRs."""
        self._track_event(update, "progress")
        user_id = update.effective_user.id

        creds = self._storage.get_credentials(user_id)
        if not creds:
            await update.message.reply_text(
                "Сначала подключи Garmin: /link_garmin", reply_markup=MAIN_KEYBOARD
            )
            return

        status_msg = await update.message.reply_text("Анализирую прогресс...")
        stop = asyncio.Event()
        spinner = asyncio.create_task(self._animate(status_msg, stop, [
            "Собираю данные за 3 месяца",
            "Считаю прогноз финиша",
            "Анализирую прогресс",
        ]))

        try:
            today = datetime.now(self._get_user_tz(user_id)).date()
            yesterday = today - timedelta(days=1)
            metrics = self._get_metrics(user_id, today)
            if not metrics:
                metrics = {"date": today.isoformat()}

            # Race predictions from VO2max
            fp = metrics.get("fitness_profile") or {}
            vo2 = fp.get("vo2_max")
            if not vo2:
                vo2_hist = metrics.get("vo2max_history") or []
                if vo2_hist:
                    vo2 = vo2_hist[-1].get("vo2_max")
            race_predictions = self._plan_builder.predict_race_times(vo2) if vo2 else None

            # Weight history
            weight_history = self._service.collect_weight_history(user_id, days=90)

            # Personal records
            personal_records = self._service.collect_personal_records(user_id)

            # Feelings stats
            feelings_stats = self._storage.get_feelings_stats(user_id, days=14)

            # Context
            training_goal = self._storage.get_goal(user_id)
            upcoming_races = self._storage.get_races(user_id, from_date=today.isoformat())
            user_memory = self._storage.get_user_memory(user_id)

            report = await self._analyst.analyze_progress(
                metrics=metrics,
                race_predictions=race_predictions,
                weight_history=weight_history,
                personal_records=personal_records,
                feelings_stats=feelings_stats,
                training_goal=training_goal,
                upcoming_races=upcoming_races,
                user_memory=user_memory,
            )
        except Exception as exc:
            logger.exception("Error generating progress report")
            report = _api_error_msg(exc, "отчёт о прогрессе")
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await spinner

        with contextlib.suppress(Exception):
            await status_msg.delete()

        for chunk in self._split(report):
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

    async def handle_weekly_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show weekly training summary with plan comparison."""
        self._track_event(update, "weekly_summary")
        user_id = update.effective_user.id

        creds = self._storage.get_credentials(user_id)
        if not creds:
            await update.message.reply_text(
                "Сначала подключи Garmin: /link_garmin", reply_markup=MAIN_KEYBOARD
            )
            return

        status_msg = await update.message.reply_text("Подвожу итоги недели...")

        try:
            today = datetime.now(self._get_user_tz(user_id)).date()
            metrics = self._get_metrics(user_id, today)
            if not metrics:
                metrics = {"date": today.isoformat()}

            # Cached plan for comparison — auto-generate if missing
            week_start_date = today - timedelta(days=today.weekday())
            week_start = week_start_date.isoformat()
            plan_row = self._storage.get_plan(user_id, week_start)
            plan_text = plan_row[0] if plan_row else ""

            if not plan_text:
                try:
                    history = self._storage.get_history(user_id, limit=10)
                    user_memory_for_plan = self._storage.get_user_memory(user_id)
                    training_goal = self._storage.get_goal(user_id)
                    upcoming_races = self._storage.get_races(user_id, from_date=today.isoformat())
                    from datetime import date as _date
                    feelings_since = (_date.today() - timedelta(days=6)).isoformat()
                    recent_feelings = self._storage.get_feelings(user_id, feelings_since)
                    plan_text, week_type = await self._plan_builder.generate_plan(
                        user_id=user_id,
                        metrics=metrics,
                        history=history,
                        user_memory=user_memory_for_plan,
                        training_goal=training_goal,
                        upcoming_races=upcoming_races,
                        feelings=recent_feelings,
                    )
                    self._storage.save_plan(user_id, week_start, plan_text, week_type)
                except Exception as exc:
                    logger.warning("Could not auto-generate plan for weekly summary: %s", exc)
                    plan_text = ""

            feelings_stats = self._storage.get_feelings_stats(user_id, days=7)
            user_memory = self._storage.get_user_memory(user_id)

            # Collect food entries for the week
            from datetime import date as _date
            week_food: list[dict] = []
            for i in range(7):
                day = week_start_date + timedelta(days=i)
                if day > today:
                    break
                day_entries = self._storage.get_food_entries(user_id, day.isoformat())
                week_food.extend(day_entries)

            # Garmin daily calories for the week
            garmin_week_cal: dict[str, dict] = {}
            for i in range(7):
                day = week_start_date + timedelta(days=i)
                if day > today:
                    break
                dc = self._get_garmin_daily_calories(user_id, day)
                if dc:
                    garmin_week_cal[day.isoformat()] = dc

            weight_kg = self._get_user_weight(user_id)

            report = await self._analyst.analyze_weekly_summary(
                metrics=metrics,
                plan_text=plan_text,
                feelings_stats=feelings_stats,
                user_memory=user_memory,
                food_entries=week_food,
                garmin_daily_calories=garmin_week_cal,
                weight_kg=weight_kg,
            )
        except Exception as exc:
            logger.exception("Error generating weekly summary")
            report = _api_error_msg(exc, "итог недели")

        with contextlib.suppress(Exception):
            await status_msg.delete()

        for chunk in self._split(report):
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

    async def handle_records(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show personal records at standard distances."""
        self._track_event(update, "records")
        user_id = update.effective_user.id

        creds = self._storage.get_credentials(user_id)
        if not creds:
            await update.message.reply_text(
                "Сначала подключи Garmin: /link_garmin", reply_markup=MAIN_KEYBOARD
            )
            return

        records = self._service.collect_personal_records(user_id)

        if not records:
            await update.message.reply_text(
                "Пока нет данных о личных рекордах. Нужны синхронизированные пробежки.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        lines = ["🏆 ЛИЧНЫЕ РЕКОРДЫ\n"]
        for pr in records:
            hr_str = f"  пульс {pr['avg_hr']}" if pr.get("avg_hr") else ""
            lines.append(
                f"  {pr['distance']:>14}: {pr['time']:>8}  ({pr['pace']}/км){hr_str}"
            )
            lines.append(f"                  {pr['date']} — {pr['name']}")

        # Race predictions
        fp_file = self._service._workdir_root / str(user_id) / "fitness_profile.json"
        vo2 = None
        if fp_file.exists():
            try:
                import json
                fp = json.loads(fp_file.read_text())
                vo2 = fp.get("vo2_max")
            except Exception:
                pass
        if vo2:
            predictions = self._plan_builder.predict_race_times(vo2)
            adjusted = self._plan_builder.predict_race_times(vo2 - 3)
            if predictions:
                lines.append(f"\n🎯 ПРОГНОЗ ФИНИША (Daniels, Garmin VO2max {vo2}):")
                lines.append(f"   Garmin завышает на 2-5 пунктов — реалистичнее VO2max {vo2-3}\n")
                header = f"  {'Дистанция':>14}  {'Теор.':>8}  {'Реалист.':>8}  {'Факт':>8}"
                lines.append(header)
                for dist in predictions:
                    actual = next((pr for pr in records if pr["distance"] == dist), None)
                    fact_str = actual["time"] if actual else "—"
                    adj_str = adjusted.get(dist, "?") if adjusted else "?"
                    lines.append(
                        f"  {dist:>14}  {predictions[dist]:>8}  {adj_str:>8}  {fact_str:>8}"
                    )

        await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)

    async def handle_weight_btn(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show current weight and prompt for manual entry."""
        user_id = update.effective_user.id
        overrides = self._storage.get_profile_override(user_id)
        current = overrides.get("weight_kg")
        current_str = f"Текущий: {current} кг\n" if current else ""
        await update.message.reply_text(
            f"⚖️ Введи вес в кг (например: 72.5)\n{current_str}",
            reply_markup=MAIN_KEYBOARD,
        )
        context.user_data["awaiting"] = "weight"

    async def handle_lthr_btn(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show current LTHR and prompt for manual entry."""
        user_id = update.effective_user.id
        overrides = self._storage.get_profile_override(user_id)
        current = overrides.get("lthr")
        current_str = f"Текущий (вручную): {current:.0f} уд/мин\n" if current else ""
        await update.message.reply_text(
            f"💓 Введи LTHR — лактатный порог пульса (например: 172)\n"
            f"{current_str}"
            f"Если введён вручную — данные Garmin игнорируются.\n"
            f"Как измерить: 30 мин бег в полную силу, средний пульс за последние 20 мин = LTHR.",
            reply_markup=MAIN_KEYBOARD,
        )
        context.user_data["awaiting"] = "lthr"

    async def handle_timezone_btn(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    async def handle_experience_btn(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Prompt for running experience in years."""
        user_id = update.effective_user.id
        overrides = self._storage.get_profile_override(user_id)
        current = overrides.get("running_experience_years")
        current_str = f"Текущий: {current:.0f} лет\n" if current else ""
        await update.message.reply_text(
            f"🏃 Сколько лет ты бегаешь регулярно? (например: 2)\n{current_str}"
            f"Это влияет на безопасный темп роста объёмов.",
            reply_markup=MAIN_KEYBOARD,
        )
        context.user_data["awaiting"] = "experience"

    async def handle_profile_btn(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    async def handle_profile_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            tokens = re.split(r"[,\s]+", text.lower())
            days = []
            for tok in tokens:
                tok = tok.strip().rstrip(".")
                if tok in DAY_ALIASES:
                    days.append(DAY_ALIASES[tok])
            if not days:
                return {}, "Не распознал дни. Напиши, например: пн ср пт сб"
            days = sorted(set(days))
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

    async def handle_question(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._track_event(update, "question")
        user_id = update.effective_user.id
        # Support injected voice text (from handle_voice → handle_question delegation)
        question = (context.user_data.pop("_voice_text", None) or update.message.text or "").strip()
        if not question:
            return

        # Handle awaiting input for weight/LTHR/timezone/experience
        awaiting = context.user_data.pop("awaiting", None)
        if awaiting == "timezone":
            tz_name = question.strip()
            try:
                ZoneInfo(tz_name)  # validate
                self._storage.save_profile_override(user_id, timezone=tz_name)
                await update.message.reply_text(
                    f"✅ Часовой пояс сохранён: {tz_name}", reply_markup=MAIN_KEYBOARD
                )
            except Exception:
                await update.message.reply_text(
                    f"Не знаю такой часовой пояс: {tz_name}\n"
                    f"Введи точное название, например: Europe/Moscow",
                    reply_markup=MAIN_KEYBOARD,
                )
            return
        if awaiting in ("weight", "lthr"):
            try:
                value = float(question.replace(",", "."))
                if awaiting == "weight":
                    if not (30 < value < 250):
                        raise ValueError
                    self._storage.save_profile_override(user_id, weight_kg=value)
                    await update.message.reply_text(
                        f"✅ Вес сохранён: {value} кг", reply_markup=MAIN_KEYBOARD
                    )
                else:
                    if not (100 < value < 220):
                        raise ValueError
                    self._storage.save_profile_override(user_id, lthr=value)
                    await update.message.reply_text(
                        f"✅ LTHR сохранён: {value:.0f} уд/мин\n"
                        f"Данные Garmin по LTHR теперь игнорируются.",
                        reply_markup=MAIN_KEYBOARD,
                    )
            except (ValueError, TypeError):
                await update.message.reply_text(
                    "Не понял число. Введи ещё раз (например: 72.5 или 172)",
                    reply_markup=MAIN_KEYBOARD,
                )
            return
        if awaiting == "experience":
            try:
                value = float(question.replace(",", "."))
                if not (0 <= value <= 50):
                    raise ValueError
                self._storage.save_profile_override(user_id, running_experience_years=value)
                await update.message.reply_text(
                    f"✅ Беговой стаж сохранён: {value:.0f} лет",
                    reply_markup=MAIN_KEYBOARD,
                )
            except (ValueError, TypeError):
                await update.message.reply_text(
                    "Введи число от 0 до 50 (например: 2)",
                    reply_markup=MAIN_KEYBOARD,
                )
            return

        # Food mode — text as food description
        if awaiting == "food" and self._nutrition:
            date_iso, food_text = self._resolve_food_date(
                question, context, user_id
            )
            if not food_text.strip():
                await self._prompt_food_date_only(update, context, date_iso)
                return
            status_msg = await update.message.reply_text("🍽 Оцениваю калории...")
            try:
                result = await self._nutrition.analyze_text(food_text)
            except NutritionTruncatedError as exc:
                logger.warning("Food text truncated: %s", exc)
                with contextlib.suppress(Exception):
                    await status_msg.delete()
                await update.message.reply_text(
                    f"⚠️ {exc}", reply_markup=MAIN_KEYBOARD,
                )
                return
            except Exception as exc:
                logger.exception("Food text analysis failed")
                with contextlib.suppress(Exception):
                    await status_msg.delete()
                await update.message.reply_text(
                    f"Не удалось оценить: {exc}", reply_markup=MAIN_KEYBOARD,
                )
                return
            with contextlib.suppress(Exception):
                await status_msg.delete()
            if result.get("confidence") == "none":
                await update.message.reply_text(
                    "Не похоже на еду. Попробуй описать подробнее.",
                    reply_markup=MAIN_KEYBOARD,
                )
                return
            if date_iso:
                result["entry_date"] = date_iso
            context.user_data["pending_food"] = result
            context.user_data["pending_food_source"] = "text"
            text = NutritionAnalyzer.format_food_confirmation(result)
            await update.message.reply_text(text, reply_markup=FOOD_CONFIRM_KB)
            return

        # Food edit mode — apply correction to existing recognition
        if awaiting == "food_edit" and self._nutrition:
            context.user_data["awaiting"] = "food"  # back to food mode after
            pending = context.user_data.get("pending_food")
            date_iso, corr_text = self._resolve_food_date(
                question, context, user_id
            )
            prev_date = (pending or {}).get("entry_date")
            new_date = date_iso or prev_date
            # Правка только даты — не гонять Claude заново
            if pending and not corr_text.strip():
                if date_iso:
                    pending["entry_date"] = date_iso
                else:
                    pending.pop("entry_date", None)
                context.user_data["pending_food"] = pending
                await update.message.reply_text(
                    NutritionAnalyzer.format_food_confirmation(pending),
                    reply_markup=FOOD_CONFIRM_KB,
                )
                return
            status_msg = await update.message.reply_text("🍽 Применяю правку...")
            try:
                if pending:
                    result = await self._nutrition.analyze_correction(pending, corr_text)
                else:
                    result = await self._nutrition.analyze_text(corr_text)
            except NutritionTruncatedError as exc:
                logger.warning("Food re-analysis truncated: %s", exc)
                with contextlib.suppress(Exception):
                    await status_msg.delete()
                await update.message.reply_text(
                    f"⚠️ {exc}", reply_markup=MAIN_KEYBOARD,
                )
                return
            except Exception as exc:
                logger.exception("Food re-analysis failed")
                with contextlib.suppress(Exception):
                    await status_msg.delete()
                await update.message.reply_text(
                    f"Не удалось оценить: {exc}", reply_markup=MAIN_KEYBOARD,
                )
                return
            with contextlib.suppress(Exception):
                await status_msg.delete()
            if result.get("confidence") == "none":
                await update.message.reply_text(
                    "Не похоже на еду. Попробуй описать подробнее.",
                    reply_markup=MAIN_KEYBOARD,
                )
                return
            if new_date:
                result["entry_date"] = new_date
            context.user_data["pending_food"] = result
            context.user_data["pending_food_source"] = "text"
            text = NutritionAnalyzer.format_food_confirmation(result)
            await update.message.reply_text(text, reply_markup=FOOD_CONFIRM_KB)
            return

        # Edit an existing saved food entry (from 📊 Питание за день management list)
        if awaiting == "food_db_edit" and self._nutrition:
            from datetime import date as _date

            entry_id = context.user_data.pop("food_db_edit_id", None)
            old_date = context.user_data.pop("food_db_edit_date", None)
            if entry_id is None:
                await update.message.reply_text(
                    "Не нашёл, какую запись править. Открой 📊 Питание за день заново.",
                    reply_markup=MAIN_KEYBOARD,
                )
                return
            entry = self._storage.get_food_entry(user_id, entry_id)
            if not entry:
                await update.message.reply_text(
                    "Запись уже удалена.", reply_markup=MAIN_KEYBOARD,
                )
                return
            date_iso, corr_text = self._resolve_food_date(
                question, context, user_id
            )
            new_date = date_iso or old_date
            status_msg = await update.message.reply_text("🍽 Применяю правку...")
            try:
                if corr_text.strip():
                    result = await self._nutrition.analyze_correction(
                        entry, corr_text
                    )
                else:
                    result = None  # только смена даты
            except Exception as exc:
                logger.exception("Food DB edit re-analysis failed")
                with contextlib.suppress(Exception):
                    await status_msg.delete()
                await update.message.reply_text(
                    f"Не удалось оценить: {exc}", reply_markup=MAIN_KEYBOARD,
                )
                return
            with contextlib.suppress(Exception):
                await status_msg.delete()
            if result is not None and result.get("confidence") == "none":
                await update.message.reply_text(
                    "Не похоже на еду. Попробуй описать подробнее.",
                    reply_markup=MAIN_KEYBOARD,
                )
                context.user_data["awaiting"] = "food_db_edit"
                context.user_data["food_db_edit_id"] = entry_id
                context.user_data["food_db_edit_date"] = old_date
                return

            upd: dict = {}
            if result is not None:
                upd.update(
                    description=result["description"],
                    calories=result["calories"],
                    protein_g=result["protein_g"],
                    fat_g=result["fat_g"],
                    carbs_g=result["carbs_g"],
                    confidence=result.get("confidence", "medium"),
                )
            if new_date and new_date != entry["date"]:
                upd["entry_date"] = new_date
            if not upd:
                await update.message.reply_text(
                    "Ничего не изменилось.", reply_markup=MAIN_KEYBOARD,
                )
                return
            self._storage.update_food_entry(user_id, entry_id, **upd)
            fresh = self._storage.get_food_entry(user_id, entry_id) or entry
            try:
                d_label = _date.fromisoformat(fresh["date"]).strftime("%d.%m.%Y")
            except ValueError:
                d_label = fresh["date"]
            await update.message.reply_text(
                f"✅ Запись обновлена (за {d_label}):\n"
                f"🍽 {fresh['description']}\n"
                f"🔥 {fresh['calories']:.0f} ккал | "
                f"Б {fresh['protein_g']:.0f}г Ж {fresh['fat_g']:.0f}г "
                f"У {fresh['carbs_g']:.0f}г\n\n"
                "Открой 📊 Питание за день, чтобы увидеть обновлённый список.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        # Profile questionnaire flow
        if awaiting and awaiting.startswith("profile_"):
            context.user_data.pop("profile_step", None)
            skipped = _is_skip_token(question)
            if skipped:
                ack = "⏭ Пропущено."
            else:
                ok, err = await self._parse_profile_answer(awaiting, question)
                if err:
                    context.user_data["awaiting"] = awaiting
                    await update.message.reply_text(err, reply_markup=MAIN_KEYBOARD)
                    return
                self._storage.save_profile_override(user_id, **ok)
                ack = "✅ Сохранено!"
            # Advance to next question (re-read overrides to see what's left)
            overrides = self._storage.get_profile_override(user_id)
            # If skipped, manually advance past this field by finding next unanswered after current
            if skipped:
                # Find the current field index and look for questions after it
                cur_idx = next((i for i, (_, _, k) in enumerate(PROFILE_QUESTIONS) if k == awaiting), -1)
                next_missing = None
                for field, q_text, a_key in PROFILE_QUESTIONS[cur_idx + 1:]:
                    if overrides.get(field) is None:
                        next_missing = (field, q_text, a_key)
                        break
                if next_missing:
                    field, q_text, a_key = next_missing
                    context.user_data["awaiting"] = a_key
                    context.user_data["profile_step"] = field
                    await update.message.reply_text(f"{ack}\n\n{q_text}", reply_markup=MAIN_KEYBOARD)
                else:
                    self._storage.save_profile_override(user_id, profile_completed=1)
                    await update.message.reply_text(
                        f"{ack}\n\n✅ Профиль заполнен 🎉\n"
                        "Теперь план тренировок будет учитывать твой профиль.\n"
                        "Изменить — /profile_reset",
                        reply_markup=MAIN_KEYBOARD,
                    )
            else:
                next_q = self._advance_profile(context, overrides)
                if next_q:
                    await update.message.reply_text(f"{ack}\n\n{next_q}", reply_markup=MAIN_KEYBOARD)
                else:
                    self._storage.save_profile_override(user_id, profile_completed=1)
                    await update.message.reply_text(
                        f"{ack}\n\n✅ Профиль полностью заполнен 🎉\n"
                        "Теперь план тренировок будет учитывать твой профиль.\n"
                        "Изменить — /profile_reset",
                        reply_markup=MAIN_KEYBOARD,
                    )
            return

        creds = self._storage.get_credentials(user_id)
        if not creds:
            await update.message.reply_text(
                "Сначала подключи Garmin: /link_garmin", reply_markup=MAIN_KEYBOARD
            )
            return

        # Detect plan adjustment / generation requests
        # tweak — корректировка существующего плана (нужен кэш)
        # request — генерация плана с нуля («дай план», «составь план») — кэш необязателен
        if self._is_plan_tweak(question) or self._is_plan_request(question):
            await self._regenerate_plan_with_tweak(update, context, user_id, question)
            return

        status_msg = await update.message.reply_text("Думаю...")

        today = datetime.now(self._get_user_tz(user_id)).date()
        yesterday = today - timedelta(days=1)
        # Try today first, then yesterday — user may ask about today's workout
        metrics = self._get_metrics(user_id, today)
        today_sleep = self._service.collect_sleep_for_date(user_id, today)
        if today_sleep and metrics:
            metrics["sleep_last_night"] = today_sleep

        # Always fetch recent activities with km_splits for Q&A context
        # (activities_28d from collect_daily_metrics has no km_splits)
        if metrics:
            qa_activities = self._service.collect_recent_activities(user_id, days=14)
            if qa_activities:
                metrics["recent_activities_for_qa"] = qa_activities[:5]

        history = self._storage.get_history(user_id, limit=20)
        user_memory = self._storage.get_user_memory(user_id)
        training_goal = self._storage.get_goal(user_id)
        upcoming_races = self._storage.get_races(user_id, from_date=today.isoformat())
        week_start = (today - timedelta(days=today.weekday())).isoformat()
        current_plan_row = self._storage.get_plan(user_id, week_start)
        current_plan = current_plan_row[0] if current_plan_row else ""
        # Pass DB paths so Claude can query them directly via tool use
        db_paths = self._service.get_db_paths(user_id)
        db_paths["app"] = str(self._storage._db_path)

        # Write-tool: даём Claude возможность сохранить план, согласованный в чате
        def _save_plan_fn(plan_text: str, week_type: str) -> str:
            valid_types = {"recovery", "base", "build", "peak", "taper"}
            wt = week_type if week_type in valid_types else "build"
            if not plan_text or len(plan_text.strip()) < 30:
                return "[ошибка: plan_text слишком короткий — нужен полный текст плана]"
            self._storage.save_plan(user_id, week_start, plan_text, wt)
            return f"OK: план сохранён (week_start={week_start}, week_type={wt}, длина={len(plan_text)} симв.)"

        try:
            answer = await self._analyst.ask(
                question, metrics, history=history, user_memory=user_memory,
                upcoming_races=upcoming_races, training_goal=training_goal,
                current_plan=current_plan, db_paths=db_paths,
                user_id=user_id, today_iso=today.isoformat(),
                save_plan_fn=_save_plan_fn,
            )
        except Exception as exc:
            logger.exception("Error in handle_question")
            with contextlib.suppress(Exception):
                await status_msg.delete()
            await update.message.reply_text(_api_error_msg(exc, "ответ на вопрос"), reply_markup=MAIN_KEYBOARD)
            return

        # Extract auto-memory tags before displaying
        answer_clean, memories = self._extract_memories(answer)

        chunks = self._split(answer_clean)
        with contextlib.suppress(Exception):
            await status_msg.edit_text(chunks[0])
        for chunk in chunks[1:]:
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

        # Save extracted memories
        if memories:
            current_notes = self._storage.get_user_memory(user_id)
            new_notes = current_notes.rstrip()
            for mem in memories:
                if mem.lower() not in current_notes.lower():
                    new_notes += f"\n{mem}"
            if new_notes != current_notes:
                self._storage.set_user_memory(user_id, new_notes.strip())
                await update.message.reply_text(
                    f"💾 Запомнил: {'; '.join(memories)}",
                    reply_markup=MAIN_KEYBOARD,
                )

        # Save to conversation history
        self._storage.add_message(user_id, "user", question, source="qa")
        self._storage.add_message(user_id, "assistant", answer_clean, source="qa")

    async def admin_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._track_event(update, "admin_stats")
        user = update.effective_user
        if not user or user.id not in self._admin_user_ids:
            await update.effective_message.reply_text("Доступ запрещён.")
            return
        stats = self._storage.get_usage_stats()
        text = (
            "Usage Stats\n"
            f"Total Users: {stats.total_users}\n"
            f"DAU: {stats.dau} / WAU: {stats.wau} / MAU: {stats.mau}\n"
            f"Retention D1: {self._fmt_retention(stats.retention_d1, stats.cohort_d1_size)}\n"
            f"Retention D7: {self._fmt_retention(stats.retention_d7, stats.cohort_d7_size)}\n"
            f"Retention D30: {self._fmt_retention(stats.retention_d30, stats.cohort_d30_size)}"
        )
        await update.effective_message.reply_text(text)

    async def handle_webapp_data(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._track_event(update, "webapp_data")
        message = update.effective_message
        if not message or not message.web_app_data:
            return
        try:
            payload = json.loads(message.web_app_data.data)
        except json.JSONDecodeError:
            await message.reply_text("Некорректные данные из WebApp.")
            return

        token = str(payload.get("token", ""))
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        if not token or not username or not password:
            await message.reply_text("Не хватает полей. Повторите /link_garmin.")
            return

        token_user_id = self._storage.consume_web_token(token)
        sender_id = update.effective_user.id if update.effective_user else None
        if not token_user_id or sender_id != token_user_id:
            await message.reply_text("Токен недействителен или просрочен. Повторите /link_garmin.")
            return

        encrypted = self._box.encrypt(password)
        self._storage.upsert_credentials(
            user_id=token_user_id, username=username, password_encrypted=encrypted
        )
        await message.reply_text(
            "✅ Garmin подключён!\n\n"
            "👉 Нажми «Утро» — пойдёт первая синхронизация данных "
            "(2–3 минуты, только в первый раз дольше). После неё бот "
            "разберёт твой сон, восстановление и нагрузку.",
            reply_markup=MAIN_KEYBOARD,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _animate(self, message, stop: asyncio.Event, stages: list[str]) -> None:
        dots = ""
        started = time.monotonic()
        while not stop.is_set():
            elapsed = int(time.monotonic() - started)
            idx = min(elapsed // 20, len(stages) - 1)
            dots = "." * ((elapsed % 3) + 1)
            text = f"{stages[idx]}{dots} ({elapsed}с)"
            with contextlib.suppress(Exception):
                await message.edit_text(text)
            try:
                await asyncio.wait_for(stop.wait(), timeout=3.0)
                break
            except asyncio.TimeoutError:
                pass

    async def _send_webapp_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user:
            return
        token = self._storage.issue_web_token(
            user_id=update.effective_user.id,
            ttl_seconds=self._webapp_token_ttl_seconds,
        )
        url = f"{self._webapp_base_url}/connect?token={token}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(text="Открыть форму", url=url)]])
        await update.effective_message.reply_text(
            "Введи Garmin credentials в защищённой форме и нажми Save.",
            reply_markup=keyboard,
        )

    def _track_event(self, update: Update, event_name: str) -> None:
        user = update.effective_user
        if user:
            self._storage.track_event(user.id, event_name)

    _PLAN_TWEAK_PATTERNS = re.compile(
        r'(?:план|тренировк[иу]|неделю|расписание)\b.*'
        r'(?:полегче|потяжел|легче|сложнее|проще|тяжелее|измени|поменяй|перестрой|скорректируй'
        r'|добав[ьи]|убер[иь]|замен[иь]|больше|меньше|без\s|не\s+хочу|хочу|сдвин[ьу]|перенес[иь])'
        r'|(?:полегче|потяжел|легче|сложнее|проще|тяжелее)\b.*(?:план|тренировк|недел)'
        r'|(?:добав[ьи]|убер[иь]|замен[иь]|больше|меньше|хочу|не\s+хочу)\b.*'
        r'(?:интервал|темпов|длинн|бег[аоу]|отдых|восстановлен|силов|горк[иу]|фартлек|разминк)',
        re.IGNORECASE,
    )

    # «Дай/составь/сделай/нужен новый план», «план на 16-21.06», «давай план» —
    # запрос на генерацию нового плана через plan_builder, даже если кэша ещё нет.
    _PLAN_REQUEST_PATTERNS = re.compile(
        r'(?:дай|давай|составь|сделай|нужен|нужна|новый|обнови|пересоставь|перестрой|пересчитай)\b'
        r'[\s\S]{0,40}?(?:план|расписание|тренировк[иу]|недел)',
        re.IGNORECASE,
    )

    def _is_plan_tweak(self, text: str) -> bool:
        """Check if the user's message is a request to adjust the current plan."""
        return bool(self._PLAN_TWEAK_PATTERNS.search(text))

    def _is_plan_request(self, text: str) -> bool:
        """Запрос на генерацию плана с нуля (без обязательного кэша)."""
        return bool(self._PLAN_REQUEST_PATTERNS.search(text))

    async def _regenerate_plan_with_tweak(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        user_id: int, tweak_request: str,
    ) -> None:
        """Regenerate the weekly plan incorporating the user's adjustment request."""
        self._track_event(update, "plan_tweak")
        status_msg = await update.message.reply_text("Корректирую план...")

        today = datetime.now(self._get_user_tz(user_id)).date()
        week_start = (today - timedelta(days=today.weekday())).isoformat()

        try:
            metrics = self._get_metrics(user_id, today) or {"date": today.isoformat()}
            history = self._storage.get_history(user_id, limit=10)
            user_memory = self._storage.get_user_memory(user_id)
            training_goal = self._storage.get_goal(user_id)
            upcoming_races = self._storage.get_races(user_id, from_date=today.isoformat())
            from datetime import date as _date
            feelings_since = (_date.today() - timedelta(days=6)).isoformat()
            recent_feelings = self._storage.get_feelings(user_id, feelings_since)
            prev_plan_row = self._storage.get_plan(user_id, week_start)
            prev_plan = prev_plan_row[0] if prev_plan_row else ""

            # Append the tweak to user_memory so the plan generator sees it
            tweak_note = f"\n[КОРРЕКТИРОВКА ПЛАНА от пользователя]: {tweak_request}"
            plan_memory = (user_memory + tweak_note).strip()

            plan_text, week_type = await self._plan_builder.generate_plan(
                user_id=user_id,
                metrics=metrics,
                history=history,
                user_memory=plan_memory,
                training_goal=training_goal,
                upcoming_races=upcoming_races,
                feelings=recent_feelings,
                previous_plan=prev_plan,
            )
            self._storage.save_plan(user_id, week_start, plan_text, week_type)
        except Exception as exc:
            logger.exception("Error regenerating plan with tweak")
            plan_text = _api_error_msg(exc, "корректировка плана")
        finally:
            with contextlib.suppress(Exception):
                await status_msg.delete()

        chunks = self._split(plan_text)
        for chunk in chunks:
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

        self._storage.add_message(user_id, "user", tweak_request, source="plan_tweak")
        self._storage.add_message(user_id, "assistant", plan_text, source="plan_tweak")

    @staticmethod
    def _extract_memories(text: str) -> tuple[str, list[str]]:
        """Extract [ЗАПОМНИТЬ: ...] tags from AI response. Return (clean_text, memories)."""
        memories = []
        clean = text
        for m in re.finditer(r'\[ЗАПОМНИТЬ:\s*(.+?)\]', text):
            memories.append(m.group(1).strip())
            clean = clean.replace(m.group(0), "")
        # Clean up trailing whitespace/newlines left after removal
        clean = re.sub(r'\n{3,}', '\n\n', clean).strip()
        return clean, memories

    @staticmethod
    def _split(text: str, max_len: int = 4000) -> list[str]:
        if len(text) <= max_len:
            return [text]
        chunks: list[str] = []
        buf = ""
        for para in text.split("\n\n"):
            cand = (buf + "\n\n" + para) if buf else para
            if len(cand) <= max_len:
                buf = cand
                continue
            if buf:
                chunks.append(buf)
                buf = ""
            if len(para) <= max_len:
                buf = para
                continue
            line_buf = ""
            for line in para.split("\n"):
                lc = (line_buf + "\n" + line) if line_buf else line
                if len(lc) <= max_len:
                    line_buf = lc
                    continue
                if line_buf:
                    chunks.append(line_buf)
                    line_buf = ""
                while len(line) > max_len:
                    chunks.append(line[:max_len])
                    line = line[max_len:]
                line_buf = line
            if line_buf:
                buf = line_buf
        if buf:
            chunks.append(buf)
        return chunks

    @staticmethod
    def _fmt_retention(value: float | None, cohort_size: int) -> str:
        if value is None:
            return f"N/A (cohort={cohort_size})"
        return f"{value}% (cohort={cohort_size})"


_BOT_COMMANDS = [
    BotCommand("start", "С чего начать"),
    BotCommand("link_garmin", "Подключить Garmin"),
    BotCommand("plan", "Недельный план"),
    BotCommand("goal", "Поставить цель"),
    BotCommand("race", "Гонки и старты"),
    BotCommand("feeling", "Записать самочувствие"),
    BotCommand("status", "Статус подключения"),
    BotCommand("remember", "Запомнить заметку"),
    BotCommand("memory", "Показать заметки"),
    BotCommand("forget", "Удалить заметку"),
    BotCommand("profile_reset", "Сбросить профиль"),
]


async def _set_bot_commands(app: Application) -> None:
    try:
        await app.bot.set_my_commands(_BOT_COMMANDS)
    except Exception as exc:
        logger.warning("set_my_commands failed: %s", exc)


def build_application(
    token: str,
    storage: Storage,
    box: SecretBox,
    service: GarminService,
    analyst: HealthAnalyst,
    plan_builder: WeeklyPlanBuilder,
    webapp_base_url: str | None,
    webapp_token_ttl_seconds: int,
    admin_user_ids: set[int],
    user_timezone: str,
    garmin_db_timezone: str | None = None,
    nutrition: NutritionAnalyzer | None = None,
    transcriber: Transcriber | None = None,
) -> Application:
    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(True)
        .post_init(_set_bot_commands)
        .build()
    )
    GarminBot(
        app=app,
        storage=storage,
        box=box,
        service=service,
        analyst=analyst,
        plan_builder=plan_builder,
        webapp_base_url=webapp_base_url,
        webapp_token_ttl_seconds=webapp_token_ttl_seconds,
        admin_user_ids=admin_user_ids,
        user_timezone=user_timezone,
        garmin_db_timezone=garmin_db_timezone,
        nutrition=nutrition,
        transcriber=transcriber,
    )
    return app
