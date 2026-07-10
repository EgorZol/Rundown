"""Фасад GarminBot: регистрация хендлеров, core-команды, вебапп, утилиты.

Доменные хендлеры — в bot_*-миксинах (food/races/memory/profile/reports/qa/jobs),
константы кнопок и клавиатуры — в bot_common.py (реэкспортируются отсюда).
Правишь домен → соответствующий bot_<домен>.py; новый хендлер регистрируй
в _register_handlers здесь.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
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
from telegram.ext import Application, CallbackContext, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters

from .analyst import HealthAnalyst
from .crypto import SecretBox
from .garmin_service import GarminService
from .nutrition import NutritionAnalyzer, NutritionTruncatedError
from .plan_builder import WeeklyPlanBuilder
from .storage import Storage
from .transcription import Transcriber
from .bot_common import (  # noqa: F401  (реэкспорт для тестов и внешнего кода)
    BTN_CALORIES,
    BTN_FOOD,
    BTN_FOOD_REPORT,
    BTN_GOAL,
    BTN_MORNING,
    BTN_PLAN,
    BTN_PROFILE,
    BTN_PROGRESS,
    BTN_RACE,
    BTN_RECORDS,
    BTN_SPORT,
    BTN_STATUS,
    BTN_TIMEZONE,
    BTN_WEEKLY,
    BTN_WORKOUT,
    FOOD_CONFIRM_KB,
    MAIN_KEYBOARD,
    _api_error_msg,
    _is_garmin_auth_error,
)
from .bot_food import FoodMixin
from .bot_jobs import JobsMixin
from .bot_memory import MemoryMixin
from .bot_profile import ProfileMixin
from .bot_qa import QAMixin
from .bot_races import RacesMixin
from .bot_reports import ReportsMixin

logger = logging.getLogger(__name__)


class GarminBot(FoodMixin, RacesMixin, MemoryMixin, ProfileMixin, ReportsMixin, QAMixin, JobsMixin):
    _MAX_MSG_LEN = 4000

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
        # Первичный синк нового юзера (~365 дней, сотни запросов) — строго
        # по одному на весь бот: несколько параллельных первичных синков
        # с одного IP — почти гарантированный 429/бан от Garmin.
        self._initial_sync_sem = asyncio.Semaphore(1)
        # Дедуп для _on_error: сигнатура ошибки → {"sent_at", "suppressed"}
        self._error_dedup: dict[str, dict] = {}
        self._register_handlers()
        self._schedule_reminders()

    def _get_sync_lock(self, user_id: int) -> asyncio.Lock:
        """Get or create a per-user sync lock."""
        if user_id not in self._sync_locks:
            self._sync_locks[user_id] = asyncio.Lock()
        return self._sync_locks[user_id]

    def _sync_sem_for(self, user_id: int) -> asyncio.Semaphore:
        """Семафор для синка: первичный — сериализуем, инкрементальный — до 5."""
        try:
            if self._service.is_initial_sync_pending(user_id):
                return self._initial_sync_sem
        except Exception:
            pass
        return self._global_sync_sem

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

    async def _set_user_context(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """group=-1: выставляет CURRENT_USER_ID для атрибуции расхода токенов.

        concurrent_updates(True) обрабатывает каждый update в своей asyncio-задаче,
        contextvar изолирован между юзерами и протекает в to_thread-вызовы Claude.
        """
        user = getattr(update, "effective_user", None)
        if user is not None:
            from .analyst import CURRENT_USER_ID
            CURRENT_USER_ID.set(user.id)

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Справка о возможностях — рендерится из prompts.CAPABILITIES (единый источник)."""
        self._track_event(update, "help")
        from .prompts import help_text
        for chunk in self._split(help_text()):
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

    def _register_handlers(self) -> None:
        self._app.add_error_handler(self._on_error)
        self._app.add_handler(TypeHandler(Update, self._set_user_context), group=-1)
        self._app.add_handler(CommandHandler("start", self.start))
        self._app.add_handler(CommandHandler("help", self.help_cmd))
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
        self._app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.StatusUpdate.WEB_APP_DATA, self.handle_webapp_data))
        self._app.add_handler(
            MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(f"^{BTN_MORNING}$"), self.handle_morning)
        )
        self._app.add_handler(
            MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(f"^{BTN_WORKOUT}$"), self.handle_workout)
        )
        self._app.add_handler(
            MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(f"^{BTN_PLAN}$"), self.handle_plan)
        )
        self._app.add_handler(
            MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(f"^{BTN_SPORT}$"), self.handle_sport_status)
        )
        self._app.add_handler(
            MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(f"^{BTN_GOAL}$"), self.handle_goal_btn)
        )
        self._app.add_handler(
            MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(f"^{BTN_STATUS}$"), self.status)
        )
        self._app.add_handler(
            MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(f"^{BTN_CALORIES}$"), self.handle_calories)
        )
        self._app.add_handler(
            MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(f"^{BTN_RACE}$"), self.handle_race_btn)
        )
        self._app.add_handler(
            MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(f"^{BTN_PROGRESS}$"), self.handle_progress)
        )
        self._app.add_handler(
            MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(f"^{BTN_WEEKLY}$"), self.handle_weekly_summary)
        )
        self._app.add_handler(
            MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(f"^{BTN_RECORDS}$"), self.handle_records)
        )
        self._app.add_handler(
            MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(f"^{re.escape(BTN_TIMEZONE)}$"), self.handle_timezone_btn)
        )
        self._app.add_handler(
            MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(f"^{re.escape(BTN_PROFILE)}$"), self.handle_profile_btn)
        )
        # Food / nutrition handlers
        self._app.add_handler(
            MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(f"^{re.escape(BTN_FOOD)}$"), self.handle_food_btn)
        )
        self._app.add_handler(
            MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(f"^{re.escape(BTN_FOOD_REPORT)}$"), self.handle_food_report)
        )
        self._app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.PHOTO, self.handle_photo))
        self._app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.VOICE, self.handle_voice))
        self._app.add_handler(CallbackQueryHandler(self.handle_food_callback, pattern="^food:"))
        self._app.add_handler(CallbackQueryHandler(self.handle_fooddb_callback, pattern="^fdb:"))
        # General text — must be last
        self._app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, self.handle_question))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._track_event(update, "start")
        user_id = update.effective_user.id
        has_garmin = self._storage.get_credentials(user_id) is not None

        text = (
            "Привет! Я твой AI-тренер по бегу.\n"
            "Анализирую данные с Garmin и веду твою подготовку.\n\n"
            "💬 Просто пиши как живому человеку:\n"
            "  «вчера пробежал 10км за 49:52»\n"
            "  «болит колено неделю»\n"
            "  «составь план на неделю»\n"
            "  «сколько я пробежал за месяц?»\n"
            "  «чем сегодня заняться?»\n\n"
            "📝 Или используй кнопки ниже — готовые сценарии:\n"
            f"  {BTN_MORNING} — брифинг после сна\n"
            f"  {BTN_WORKOUT} — анализ пробежки\n"
            f"  {BTN_PLAN} — план на неделю\n"
            f"  {BTN_WEEKLY} — разбор недели\n"
            f"  {BTN_FOOD} — записать приём пищи\n\n"
            "Полный список возможностей — /help\n"
            "Я помню всю твою историю: тренировки, сон, форму, цели.\n"
            "Если я ошибусь — просто поправь словами, я запомню."
        )
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
        # Если Garmin ещё не привязан — отдельным сообщением показываем
        # кнопку подключения через WebApp. Это единственное место в боте,
        # где разговорный путь невозможен (нужна веб-форма для логина).
        if not has_garmin and self._webapp_base_url:
            await update.message.reply_text(
                "👉 Сначала подключи Garmin — нажми кнопку ниже:",
                reply_markup=MAIN_KEYBOARD,
            )
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
        from . import __version__ as _bot_version
        creds = self._storage.get_credentials(user_id)
        if not creds:
            await update.message.reply_text(
                f"Garmin ещё не подключён. Нажми /start — я подскажу как.\n\nверсия бота: v{_bot_version}",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        sync_info = self._service.get_sync_summary(user_id)
        last_sync = sync_info.last_sync_at if sync_info else "нет данных"
        text = (
            f"Garmin: {creds.username}\n"
            f"Последняя синхронизация: {last_sync}\n\n"
            f"версия бота: v{_bot_version}"
        )
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)

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
        await message.reply_text("✅ Garmin подключён!", reply_markup=MAIN_KEYBOARD)
        # Первичный синк запускаем сразу и сами — юзеру не нужно догадываться
        # нажать «Утро». Кнопка «Утро» остаётся страховкой: если этот таск
        # оборвётся (рестарт бота и т.п.), утренний синк дотянет историю.
        context.application.create_task(self._initial_sync_and_onboard(update, context))

    async def _initial_sync_and_onboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Фоновая загрузка истории после подключения Garmin + финал онбординга.

        Показывается один раз в жизни юзера (ключ «onboarding» в nudge_log);
        при переподключении креденшлов с уже загруженной историей — короткий ответ.
        """
        user_id = update.effective_user.id
        message = update.effective_message
        creds = self._storage.get_credentials(user_id)
        if not creds:
            return
        if not self._service.is_initial_sync_pending(user_id):
            await message.reply_text(
                "История уже загружена — жми 🌅 Утро за свежим брифингом.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        status_msg = await message.reply_text("⏳ Загружаю твою историю из Garmin — обычно 2–5 минут...")
        stop = asyncio.Event()
        spinner = asyncio.create_task(self._animate(status_msg, stop, [
            "Загружаю сон, пульс и восстановление",
            "Загружаю тренировки за год",
            "Считаю круги и сплиты",
            "Строю аналитику",
        ]))
        try:
            lock = self._get_sync_lock(user_id)
            async with lock:
                password = self._box.decrypt(creds.password_encrypted)
                async with self._sync_sem_for(user_id):
                    await asyncio.to_thread(
                        self._service.run_health_sync,
                        user_id=user_id, username=creds.username, password=password,
                    )
                async with self._sync_sem_for(user_id):
                    await asyncio.to_thread(
                        self._service.run_activity_sync,
                        user_id=user_id, username=creds.username, password=password,
                    )
        except Exception as exc:
            stop.set()
            with contextlib.suppress(Exception):
                await spinner
            logger.exception("initial sync failed for user %s", user_id)
            if _is_garmin_auth_error(exc):
                await status_msg.edit_text(
                    "❌ Garmin не принял логин/пароль. Проверь их и повтори /link_garmin."
                )
            else:
                await status_msg.edit_text(
                    "Не получилось загрузить историю (Garmin капризничает). "
                    "Нажми 🌅 Утро — попробуем ещё раз."
                )
            return
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await spinner

        with contextlib.suppress(Exception):
            await status_msg.delete()

        summary = self._service.get_sync_summary(user_id)
        n_workouts = summary.workouts_rows if summary else 0
        text = (
            f"📖 Готово! Загрузил историю: {n_workouts} тренировок, сон и пульс за год.\n\n"
            "Чтобы я работал как тренер, а не просто читал данные, не хватает двух вещей:\n"
            "🎯 Цель — просто напиши, например: «моя цель — полумарафон из 1:45»\n"
            "📋 Профиль — кнопка ниже, анкета на 2 минуты (дни бега, травмы, город для погоды)\n\n"
            "А завтра после сна жми 🌅 Утро — разберу восстановление и скажу, чем заняться."
        )
        if self._storage.get_nudge_history(user_id).get("onboarding"):
            text = f"📖 История обновлена: {n_workouts} тренировок. Жми 🌅 Утро!"
        await message.reply_text(text, reply_markup=MAIN_KEYBOARD)
        self._storage.log_nudge(user_id, "onboarding")
        self._storage.add_message(user_id, "assistant", text, source="onboarding")

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


# Меню сознательно короткое: курс «кнопки > команды». Остальные хендлеры
# (/plan, /goal, /race, /memory…) живы для обратной совместимости,
# но в меню не светятся — всё есть на клавиатуре и словами.
_BOT_COMMANDS = [
    BotCommand("start", "С чего начать"),
    BotCommand("help", "Что умеет бот"),
    BotCommand("link_garmin", "Подключить Garmin"),
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
