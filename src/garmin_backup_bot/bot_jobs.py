"""Фон и наблюдаемость: планировщик напоминаний, алерты о протухшем синке,
обработчик ошибок с дедупом, /admin_stats.
"""


import asyncio
import contextlib
import logging
import time
from datetime import date, datetime, timedelta

from telegram import (
    Update,
)
from telegram.ext import CallbackContext, ContextTypes

from .bot_common import BTN_WORKOUT, MAIN_KEYBOARD

logger = logging.getLogger(__name__)


class JobsMixin:

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
                # 09:00–09:14 window → проверка «тихой деградации» синка
                if hh == 9 and mm < 15:
                    await self._sync_health_check_for_user(user_id, context)
                # 05:30–05:44 — весы: до утреннего отчёта, данные уже свежие
                if hh == 5 and 30 <= mm < 45:
                    await self._scale_sync_for_user(user_id, context)
            except Exception as exc:
                logger.debug("Periodic tick failed for user %d: %s", user_id, exc)

    async def _sync_health_check_for_user(self, user_id: int, context: CallbackContext) -> None:
        """Алерт владельцу, если Garmin-данные юзера протухли, а юзер активен.

        Ловит «тихие» деградации, которые не исключения: протухший пароль Garmin,
        молча падающий синк (широкие try/except глотают ошибки в data-дырку).
        """
        today_key = f"synccheck_{user_id}_{date.today().isoformat()}"
        if context.bot_data.get(today_key):
            return
        context.bot_data[today_key] = True
        last_day = await asyncio.to_thread(self._service.last_health_day, user_id)
        if last_day is None:
            return  # ещё ни разу не синкался — это не деградация
        tz = self._get_user_tz(user_id)
        stale_days = (datetime.now(tz).date() - date.fromisoformat(last_day)).days
        if stale_days < 3:
            return
        # Заброшенные юзеры не в счёт: алертим только если юзер жив в боте
        last_event = await asyncio.to_thread(self._storage.last_event_at, user_id)
        if not last_event:
            return
        try:
            idle_days = (datetime.now(tz) - datetime.fromisoformat(last_event).astimezone(tz)).days
        except ValueError:
            return
        if idle_days > 7:
            return
        for admin_id in self._admin_user_ids:
            with contextlib.suppress(Exception):
                await context.bot.send_message(
                    admin_id,
                    f"⚠️ Синк юзера {user_id}: в garmin.db нет данных с {last_day} "
                    f"({stale_days} дн.), при этом юзер активен в боте "
                    f"(последнее действие {idle_days} дн. назад). Возможно протух "
                    f"пароль Garmin или синк тихо падает — проверь journalctl.",
                )

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
        # Строку дня извлекает та же функция, что и в утреннем отчёте
        from . import coach as _coach_plan
        today_line = _coach_plan.plan_line_for_date(plan, today)
        if not today_line:
            return
        rest_keywords = ["отдых", "Отдых", "растяжк", "выходной"]
        if any(kw in today_line for kw in rest_keywords):
            return
        activities = await asyncio.to_thread(self._service.collect_recent_activities, user_id, days=1)
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

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Глобальный error handler: traceback владельцу в Telegram («Sentry» на минималках).

        Без него исключения мимо try/except в хендлерах видны только в journalctl —
        сбои у других юзеров оставались незамеченными. Дедуп: одна сигнатура —
        не чаще раза в час, повторы считаются и упоминаются при следующей отправке.
        """
        err = getattr(context, "error", None)
        if err is None:
            return
        # Сетевые чихи поллинга (getUpdates оборвался, PTB сам ретраит) — не алертим:
        # это самозаживающее, юзеров не задевает, а алерт в Telegram о недоступности
        # Telegram при настоящем отказе всё равно не дойдёт. update is None = ошибка
        # не из юзерского хендлера, а из фонового цикла.
        from telegram.error import NetworkError, TimedOut
        if update is None and isinstance(err, (NetworkError, TimedOut)):
            logger.warning("Транзиентная сетевая ошибка поллинга (не алертим): %s", err)
            return
        logger.error("Unhandled error in handler", exc_info=err)
        if not self._admin_user_ids:
            return
        try:
            import traceback as _tb
            sig = f"{type(err).__name__}: {str(err)[:120]}"
            now = time.monotonic()
            entry = self._error_dedup.get(sig)
            if entry and now - entry["sent_at"] < 3600:
                entry["suppressed"] += 1
                return
            suppressed = entry["suppressed"] if entry else 0
            self._error_dedup[sig] = {"sent_at": now, "suppressed": 0}
            tb_text = "".join(_tb.format_exception(type(err), err, err.__traceback__))[-1500:]
            user = getattr(update, "effective_user", None)
            uid = f"{user.id} (@{user.username})" if user else "—"
            msg_obj = getattr(update, "effective_message", None)
            src_text = (getattr(msg_obj, "text", None) or "—")[:80]
            text = (
                f"🚨 Ошибка бота\nЮзер: {uid}\nСообщение: {src_text}\n"
                + (f"Подавлено повторов за час: {suppressed}\n" if suppressed else "")
                + f"\n…{tb_text}"
            )
            for admin_id in self._admin_user_ids:
                with contextlib.suppress(Exception):
                    await context.bot.send_message(admin_id, text[:4000])
        except Exception:
            logger.exception("_on_error сам упал — подавляю")

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
        token_rows = await asyncio.to_thread(self._storage.get_token_usage_stats, 30)
        if token_rows:
            lines = ["", "Токены за 30 дней (in/out/cache_read):"]
            for r in token_rows:
                uid = r["user_id"] if r["user_id"] is not None else "—"
                lines.append(
                    f"  {uid}: {r['calls']} вызовов, "
                    f"{(r['input_tokens'] or 0) / 1000:.0f}k / "
                    f"{(r['output_tokens'] or 0) / 1000:.0f}k / "
                    f"{(r['cache_read_tokens'] or 0) / 1000:.0f}k"
                )
            text += "\n".join(lines)
        await update.effective_message.reply_text(text)

    @staticmethod
    def _fmt_retention(value: float | None, cohort_size: int) -> str:
        if value is None:
            return f"N/A (cohort={cohort_size})"
        return f"{value}% (cohort={cohort_size})"
