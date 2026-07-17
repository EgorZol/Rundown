"""Домен «подписка и оплата»: тарифы, триал, пейволл, инвойсы Telegram Payments.

Логика доступа — coach.access_level (pure). Здесь: гейт _gate() для хендлеров,
пейволл с кнопками покупки, инвойс ЮKassa, pre-checkout, зачисление платежа,
/paysupport. Без PAYMENT_PROVIDER_TOKEN в .env кнопки оплаты честно отвечают
«скоро» — слой можно катить до подключения ЮKassa.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.ext import ContextTypes

from . import coach as _coach
from .bot_common import MAIN_KEYBOARD

logger = logging.getLogger(__name__)

# Цены в копейках. Маржа ~50% при текущей себестоимости (см. token_usage);
# пересмотреть через месяц реальных платежей.
PLAN_PRICES = {
    _coach.PLAN_COACH: 1490_00,
    _coach.PLAN_CALORIES: 299_00,
}
PLAN_TITLES = {
    _coach.PLAN_COACH: "🏃 Тренер",
    _coach.PLAN_CALORIES: "🍽 Калории",
}
PLAN_DESCRIPTIONS = {
    _coach.PLAN_COACH: (
        "Полный AI-тренер на 30 дней: утренние брифинги, разбор тренировок, "
        "недельные планы, свободные вопросы и дневник питания."
    ),
    _coach.PLAN_CALORIES: (
        "Дневник питания на 30 дней: фото/голос/текст, калории и БЖУ, "
        "дневные отчёты и нормы под тренировки."
    ),
}
SUB_PERIOD_DAYS = 30


class PaymentsMixin:
    """Хостится в GarminBot (нужны _storage, _tz, _payment_provider_token)."""

    # ---------- гейт ----------

    async def _gate(self, update: Update, need: str = "coach") -> bool:
        """True — пускаем. False — показали пейволл, хендлер должен выйти.

        Новому юзеру (нет строки в subscriptions) молча заводим триал
        «Тренера» на TRIAL_DAYS и пускаем — приветствие триала отдельным
        сообщением, один раз.
        """
        user_id = update.effective_user.id
        today = datetime.now(self._tz).date()
        sub = self._storage.get_subscription(user_id)
        if sub is None:
            until = (today + timedelta(days=_coach.TRIAL_DAYS - 1)).isoformat()
            self._storage.upsert_subscription(user_id, _coach.PLAN_TRIAL, until)
            with_logging_suppress = update.effective_message
            if with_logging_suppress:
                await with_logging_suppress.reply_text(
                    f"🎁 Тебе включён пробный полный доступ на {_coach.TRIAL_DAYS} дней "
                    f"(до {until}). Пользуйся всем — потом выберешь тариф.",
                    reply_markup=MAIN_KEYBOARD,
                )
            return True
        if _coach.has_access(sub, today, need=need):
            return True
        await self._send_paywall(update, need=need, sub=sub)
        return False

    async def _send_paywall(self, update: Update, need: str, sub: dict) -> None:
        level = _coach.access_level(sub, datetime.now(self._tz).date())
        if sub.get("plan") == _coach.PLAN_TRIAL:
            head = "⏳ Пробный период закончился."
        elif level == "calories" and need == "coach":
            head = "Это возможность тарифа «Тренер»."
        else:
            head = "⏳ Подписка закончилась."
        buttons = [[InlineKeyboardButton(
            f"{PLAN_TITLES[_coach.PLAN_COACH]} — {PLAN_PRICES[_coach.PLAN_COACH] // 100}₽/мес",
            callback_data="buy:coach",
        )]]
        # Тариф «Калории» предлагаем только если его хватает для действия
        if need != "coach" or level != "calories":
            buttons.append([InlineKeyboardButton(
                f"{PLAN_TITLES[_coach.PLAN_CALORIES]} — {PLAN_PRICES[_coach.PLAN_CALORIES] // 100}₽/мес",
                callback_data="buy:calories",
            )])
        await update.effective_message.reply_text(
            f"{head}\n\n"
            "🏃 «Тренер» — брифинги, разборы, планы, вопросы и питание.\n"
            "🍽 «Калории» — только дневник питания.\n\n"
            "Вопросы по оплате — /paysupport",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    # ---------- покупка ----------

    async def handle_buy_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        plan = (query.data or "").split(":", 1)[-1]
        if plan not in PLAN_PRICES:
            await query.answer("Неизвестный тариф")
            return
        if not self._payment_provider_token:
            await query.answer(
                "Оплата подключается — напишу, как только заработает!", show_alert=True
            )
            return
        await query.answer()
        await context.bot.send_invoice(
            chat_id=update.effective_chat.id,
            title=f"{PLAN_TITLES[plan]} — 30 дней",
            description=PLAN_DESCRIPTIONS[plan],
            payload=f"sub:{plan}",
            provider_token=self._payment_provider_token,
            currency="RUB",
            prices=[LabeledPrice(f"{PLAN_TITLES[plan]}, 30 дней", PLAN_PRICES[plan])],
        )

    async def handle_pre_checkout(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.pre_checkout_query
        plan = (q.invoice_payload or "").split(":", 1)[-1]
        if plan in PLAN_PRICES and q.total_amount == PLAN_PRICES[plan]:
            await q.answer(ok=True)
        else:
            await q.answer(ok=False, error_message="Тариф устарел — открой оплату заново.")

    async def handle_successful_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        sp = update.effective_message.successful_payment
        # Идемпотентность (ревью): Telegram может доставить апдейт повторно —
        # без этого повторная доставка продлевала подписку дважды.
        charge_id = sp.telegram_payment_charge_id
        if charge_id and self._storage.payment_exists(charge_id):
            logger.warning("payment duplicate delivery: user=%s charge=%s", user_id, charge_id)
            await update.effective_message.reply_text(
                "Этот платёж уже зачислен ✅", reply_markup=MAIN_KEYBOARD)
            return
        plan = (sp.invoice_payload or "").split(":", 1)[-1]
        if plan not in PLAN_PRICES:
            logger.error("successful_payment с неизвестным payload=%r от %s", sp.invoice_payload, user_id)
            plan = _coach.PLAN_COACH
        today = datetime.now(self._tz).date()
        # Продление: paid_until — ПОСЛЕДНИЙ оплаченный день (включительно),
        # поэтому база продления = paid_until + 1 день, и «30 дней» = ровно 30
        # (ревью: было base+30 → 31 день).
        sub = self._storage.get_subscription(user_id)
        base = today
        credit_days = 0
        if sub and sub.get("paid_until"):
            try:
                from datetime import date as _date
                cur = _date.fromisoformat(str(sub["paid_until"])[:10])
                if sub.get("plan") == plan and cur >= today:
                    base = cur + timedelta(days=1)
                elif (sub.get("plan") in PLAN_PRICES and sub.get("plan") != plan
                      and cur >= today):
                    # Смена тарифа: неиспользованный остаток конвертируется по
                    # деньгам (ревью: раньше остаток молча сгорал)
                    remaining = (cur - today).days + 1
                    credit_days = remaining * PLAN_PRICES[sub["plan"]] // PLAN_PRICES[plan]
            except ValueError:
                pass
        until = (base + timedelta(days=SUB_PERIOD_DAYS - 1 + credit_days)).isoformat()
        self._storage.upsert_subscription(user_id, plan, until)
        self._storage.record_payment(
            user_id, plan, sp.total_amount, sp.currency,
            sp.telegram_payment_charge_id, sp.provider_payment_charge_id,
        )
        logger.info("payment ok: user=%s plan=%s amount=%s until=%s",
                    user_id, plan, sp.total_amount, until)
        credit_note = (f"\nОстаток прошлого тарифа зачтён: +{credit_days} дн." if credit_days else "")
        await update.effective_message.reply_text(
            f"✅ Оплата прошла! {PLAN_TITLES[plan]} активен до {until}.{credit_note}\n"
            "Чек придёт от ЮKassa. Спасибо, побежали! 🏃",
            reply_markup=MAIN_KEYBOARD,
        )

    async def paysupport(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обязательная команда для ботов с платежами (правило Telegram)."""
        await update.message.reply_text(
            "💬 Поддержка по оплате: напиши сюда вопрос, я передам владельцу, "
            "или свяжись напрямую — @EgorZol.\n"
            "Возврат за неиспользованный период — по запросу в течение 14 дней.",
            reply_markup=MAIN_KEYBOARD,
        )
