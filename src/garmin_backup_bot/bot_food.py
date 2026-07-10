"""Домен «еда»: запись приёмов (фото/голос/текст), подтверждение, отчёт питания,
калории Garmin. Голос при не-еде делегируется в handle_question (QAMixin).
"""


import asyncio
import contextlib
import logging
from datetime import date, datetime, timedelta

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from .nutrition import NutritionAnalyzer, NutritionTruncatedError
from .bot_common import FOOD_CONFIRM_KB, MAIN_KEYBOARD, _api_error_msg

logger = logging.getLogger(__name__)


class FoodMixin:

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
            metrics = await asyncio.to_thread(self._get_metrics, user_id, today)
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
                "Чтобы записать еду, сначала нажми 🍽 Еда",
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
                "Для следующего приёма пищи снова нажми 🍽 Еда."
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
                "Нажми 🍽 Еда чтобы начать записывать.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        garmin_daily = await asyncio.to_thread(self._get_garmin_daily_calories, user_id, today)
        weight_kg = await asyncio.to_thread(self._get_user_weight, user_id)
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
