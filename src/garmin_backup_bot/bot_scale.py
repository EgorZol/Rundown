"""ScaleMixin: подключение умных весов Xiaomi/Amazfit (облако Zepp).

Флоу задуман так, чтобы НЕ спрашивать пароль от Xiaomi: юзер логинится сам
в браузере, отдаёт нам только OAuth-код. Подробности и единицы измерения —
в zepp_scale.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from . import zepp_scale
from .bot_common import MAIN_KEYBOARD

logger = logging.getLogger(__name__)

CONNECT_HELP = (
    "⚖️ <b>Подключение умных весов</b>\n\n"
    "Работает с весами Xiaomi/Amazfit, которые пишут в приложение "
    "Zepp Life или Zepp.\n\n"
    "<b>1.</b> Открой ссылку ниже в браузере (не в Telegram — нажми «Открыть "
    "в браузере», если предложит).\n"
    "<b>2.</b> Войди в свой аккаунт Xiaomi и нажми «Согласиться».\n"
    "<b>3.</b> Тебя перекинет на страницу, которая, скорее всего, не откроется — "
    "это нормально. Нужен только её адрес.\n"
    "<b>4.</b> Скопируй адрес из адресной строки целиком и пришли мне сюда.\n\n"
    "Пароль от Xiaomi я не спрашиваю и не храню — только код доступа к весам, "
    "в зашифрованном виде. Отключить можно в любой момент."
)


class ScaleMixin:
    async def handle_scale_connect(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Старт подключения весов: показываем ссылку и ждём адрес с кодом."""
        if not await self._gate(update, "coach"):
            return
        self._track_event(update, "scale_connect")
        context.user_data["awaiting"] = "scale_code"
        msg = update.effective_message
        await msg.reply_text(
            CONNECT_HELP, parse_mode="HTML", disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "🔗 Открыть страницу Xiaomi", url=zepp_scale.authorize_url())]]),
        )
        await msg.reply_text(
            "Жду адрес со страницы (или просто «отмена», чтобы выйти).",
            reply_markup=MAIN_KEYBOARD,
        )

    async def handle_scale_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Приём адреса с OAuth-кодом: обмен на токен + первый синк."""
        user_id = update.effective_user.id
        raw = (update.message.text or "").strip()
        context.user_data.pop("awaiting", None)

        code = zepp_scale.extract_code(raw)
        if not code:
            await update.message.reply_text(
                "Не нашёл код в присланном тексте. Нужен адрес целиком — он "
                "содержит «code=». Начни заново кнопкой в 📋 Профиле.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        status = await update.message.reply_text("Подключаю весы…")
        try:
            zepp_user_id, token = await asyncio.to_thread(zepp_scale.exchange_code, code)
        except Exception as exc:
            logger.warning("Scale connect failed for %s: %s", user_id, exc)
            with contextlib.suppress(Exception):
                await status.delete()
            await update.message.reply_text(
                "Не получилось обменять код — скорее всего он уже использован "
                "или устарел (живёт пару минут). Попробуй ещё раз: 📋 Профиль → "
                "«Подключить весы».",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        self._storage.save_scale_credentials(
            user_id, zepp_user_id, self._box.encrypt(token))
        with contextlib.suppress(Exception):
            await status.edit_text("Весы подключены, забираю историю измерений…")

        try:
            days, latest = await asyncio.to_thread(self._sync_scale_for_user, user_id)
        except Exception as exc:
            logger.exception("First scale sync failed for %s", user_id)
            with contextlib.suppress(Exception):
                await status.delete()
            await update.message.reply_text(
                f"Весы подключены, но первый синк не удался: {exc}. "
                "Попробую снова автоматически.", reply_markup=MAIN_KEYBOARD)
            return

        with contextlib.suppress(Exception):
            await status.delete()
        if not days:
            await update.message.reply_text(
                "✅ Весы подключены, но измерений в облаке пока нет.\n"
                "Встань на весы с телефоном рядом и открытым приложением — "
                "данные подтянутся сами.", reply_markup=MAIN_KEYBOARD)
            return

        tail = ""
        if latest:
            tail = f"\nПоследнее: {latest['weight']} кг"
            if latest.get("fat_pct"):
                tail += f", жир {latest['fat_pct']:.1f}%"
            if latest.get("muscle_kg"):
                tail += f", мышцы {latest['muscle_kg']:.1f} кг"
            tail += f" ({latest['day']})"
        await update.message.reply_text(
            f"✅ Весы подключены. Загрузил измерений: {days}.{tail}\n\n"
            "Дальше буду обновлять данные каждое утро сам.",
            reply_markup=MAIN_KEYBOARD,
        )

    async def handle_scale_disconnect(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        removed = self._storage.delete_scale_credentials(user_id)
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                "Весы отключены, токен удалён." if removed else "Весы и так не подключены."
            )

    # ── синк ──────────────────────────────────────────────────────────────

    def _sync_scale_for_user(self, user_id: int, limit_days: int | None = None) -> tuple[int, dict | None]:
        """Тянет измерения из Zepp в БД юзера. Возвращает (число дат, последнее).

        Синхронный — вызывать через asyncio.to_thread.
        """
        creds = self._storage.get_scale_credentials(user_id)
        if not creds:
            return 0, None
        token = self._box.decrypt(creds["token_encrypted"])
        try:
            records = zepp_scale.fetch_records(
                creds["zepp_user_id"], token, limit_days=limit_days)
        except zepp_scale.ZeppAuthError as exc:
            self._storage.mark_scale_sync(user_id, error=str(exc))
            raise
        # Страховка от смены единиц измерения на стороне Zepp
        bad = [r for r in records if not zepp_scale.composition_is_consistent(r)]
        if bad:
            logger.warning(
                "Scale %s: %d записей не сходятся по составу тела (мышцы+жир+кости "
                "≠ вес) — возможна смена единиц в Zepp", user_id, len(bad))
        days = self._service.store_scale_records(user_id, records)
        self._storage.mark_scale_sync(user_id, error=None)
        latest = max(records, key=lambda r: r.get("day", ""), default=None)
        return days, latest

    async def _scale_sync_for_user(self, user_id: int, context) -> None:
        """Ежедневный синк весов. Молча выходит, если весы не подключены."""
        if not self._storage.get_scale_credentials(user_id):
            return
        try:
            await asyncio.to_thread(self._sync_scale_for_user, user_id, 90)
        except zepp_scale.ZeppAuthError:
            # Zepp держит одну сессию: вход в мобильное приложение убивает наш
            # токен. Пароля юзера у нас нет, поэтому просим переподключить.
            await self._notify_scale_reauth(user_id, context)
        except Exception as exc:
            logger.warning("Scale sync failed for %s: %s", user_id, exc)

    async def _notify_scale_reauth(self, user_id: int, context) -> None:
        """Одно сообщение о том, что весы отвалились (не чаще раза в неделю)."""
        from . import coach as _coach
        history = self._storage.get_nudge_history(user_id)
        shown = {k: (n, last) for k, n, last in history}
        if "scale_reauth" in shown:
            from datetime import date, datetime as _dt
            try:
                last = _dt.fromisoformat(shown["scale_reauth"][1]).date()
                if (date.today() - last).days < _coach.NUDGE_REPEAT_DAYS:
                    return
            except Exception:
                pass
        self._storage.log_nudge(user_id, "scale_reauth")
        with contextlib.suppress(Exception):
            await context.bot.send_message(
                user_id,
                "⚖️ Весы отключились — Zepp разлогинил меня (так бывает, когда "
                "заходишь в приложение с телефона). Переподключи: 📋 Профиль → "
                "«Подключить весы». Данные не потеряны.",
                reply_markup=MAIN_KEYBOARD,
            )
