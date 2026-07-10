"""Домен «диалог»: handle_question — Q&A с Claude, все write-tools
(факты/заметки/гонки/профиль/invoke_action) как замыкания.
"""


import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    Update,
)
from telegram.ext import ContextTypes

from .nutrition import NutritionAnalyzer, NutritionTruncatedError
from .bot_common import FOOD_CONFIRM_KB, MAIN_KEYBOARD, _api_error_msg
from .bot_memory import _classify_bad_memory, _parse_expiry
from .bot_profile import PROFILE_QUESTIONS, _is_skip_token

logger = logging.getLogger(__name__)


class QAMixin:

    async def handle_question(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._track_event(update, "question")
        user_id = update.effective_user.id
        # Support injected voice text (from handle_voice → handle_question delegation)
        question = (context.user_data.pop("_voice_text", None) or update.message.text or "").strip()
        if not question:
            return

        # Handle awaiting input for timezone (вес/LTHR/стаж — словами через set_* tools)
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

        # Edit an existing saved food entry (from 📊 Питание management list)
        if awaiting == "food_db_edit" and self._nutrition:
            from datetime import date as _date

            entry_id = context.user_data.pop("food_db_edit_id", None)
            old_date = context.user_data.pop("food_db_edit_date", None)
            if entry_id is None:
                await update.message.reply_text(
                    "Не нашёл, какую запись править. Открой 📊 Питание заново.",
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
                "Открой 📊 Питание, чтобы увидеть обновлённый список.",
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
        metrics = await asyncio.to_thread(self._get_metrics, user_id, today)
        today_sleep = self._service.collect_sleep_for_date(user_id, today)
        if today_sleep and metrics:
            metrics["sleep_last_night"] = today_sleep

        # Always fetch recent activities with km_splits for Q&A context
        # (activities_28d from collect_daily_metrics has no km_splits)
        if metrics:
            qa_activities = await asyncio.to_thread(self._service.collect_recent_activities, user_id, days=14)
            if qa_activities:
                metrics["recent_activities_for_qa"] = qa_activities[:5]

        history = self._storage.get_history(
            user_id, limit=20,
            sources=("qa", "plan_tweak", "morning", "workout"),
            recent_full=10,  # старшие 10 сообщений — только заголовки (экономия ~1k ток/вызов)
        )
        user_memory = self._storage.get_user_memory(user_id)
        training_goal = self._storage.get_goal(user_id)
        # Включаем прошедшие старты за 21 день — чтобы Claude видел недавние
        # фактические результаты (`actual_time`) и не переспрашивал «как прошёл забег».
        past_horizon = (today - timedelta(days=21)).isoformat()
        upcoming_races = self._storage.get_races(user_id, from_date=past_horizon)
        week_start = (today - timedelta(days=today.weekday())).isoformat()
        plan_meta = self._storage.get_plan_meta(user_id, week_start)
        current_plan = plan_meta["plan_text"] if plan_meta else ""
        current_week_type = plan_meta["week_type"] if plan_meta else ""
        # Pass DB paths so Claude can query them directly via tool use
        db_paths = self._service.get_db_paths(user_id)
        db_paths["app"] = str(self._storage._db_path)

        # Recent verified facts — подмешиваются в системный промпт как
        # источник истины (Claude не должен оспаривать).
        verified_since = (today - timedelta(days=21)).isoformat()
        verified_facts = self._storage.list_verified_facts(user_id, since_date=verified_since)

        # Write-tool: даём Claude возможность сохранить план, согласованный в чате
        def _save_plan_fn(plan_text: str, week_type: str) -> str:
            valid_types = {"recovery", "base", "build", "peak", "taper"}
            wt = week_type if week_type in valid_types else "build"
            if not plan_text or len(plan_text.strip()) < 30:
                return "[ошибка: plan_text слишком короткий — нужен полный текст плана]"
            # Инцидент 05.07.2026: Claude сдвинул все даты на +1 день, а план
            # ушёл под week_start текущей недели, затерев действующий план.
            # Даты валидируются кодом; week_start выводится из самих дат —
            # так план можно сохранить и на следующую неделю.
            from . import coach as _coach
            check = _coach.check_plan_dates(plan_text, today)
            if not check.ok:
                return (
                    "[ошибка: даты в плане не совпадают с днями недели: "
                    + "; ".join(check.errors[:7])
                    + f". Правильный календарь недели плана: {check.hint}. "
                    "Исправь даты в plan_text и вызови save_weekly_plan повторно.]"
                )
            ws = check.week_start.isoformat() if check.week_start else week_start
            self._storage.save_plan(user_id, ws, plan_text, wt)
            return f"OK: план сохранён (week_start={ws}, week_type={wt}, длина={len(plan_text)} симв.)"

        # Список действий бота для UI-подтверждения после Claude-ответа.
        # Каждый элемент: dict с полями для шаблонной фразы юзеру.
        tool_actions: list[dict] = []

        def _confirm_fact_fn(fact_date: str, fact_text: str) -> str:
            try:
                # Защита: дата должна быть ISO; если Claude передал «вчера» —
                # пусть лучше упадёт, чем сохраним мусор.
                datetime.fromisoformat(fact_date)
            except Exception:
                return "[ошибка: fact_date должен быть YYYY-MM-DD, получил '%s']" % fact_date
            new_id = self._storage.add_verified_fact(user_id, fact_date, fact_text)
            tool_actions.append({"kind": "fact", "id": new_id, "date": fact_date, "text": fact_text})
            return f"OK: факт #{new_id} сохранён за {fact_date}"

        def _remember_note_fn(text: str, expires_at: str | None = None) -> str:
            text = (text or "").strip()
            if not text:
                return "[ошибка: пустой текст]"
            reason = _classify_bad_memory(text)
            if reason:
                return f"[отказано: {reason}. Если это факт за конкретную дату — вызови confirm_fact. Цель — set_training_goal, вес/LTHR/стаж/часы — set_weight/set_lthr/set_experience/set_timezone.]"
            exp_iso = _parse_expiry(expires_at) if expires_at else None
            new_id = self._storage.add_memory_item(user_id, text, expires_at=exp_iso)
            if new_id is None:
                return "OK: уже есть похожая заметка, не дублирую"
            tool_actions.append({"kind": "remember", "id": new_id, "text": text, "expires_at": exp_iso})
            return f"OK: заметка #{new_id} сохранена" + (f" (до {exp_iso})" if exp_iso else "")

        def _forget_note_fn(item_id: int) -> str:
            ok = self._storage.delete_memory_item(user_id, int(item_id))
            if not ok:
                return f"[заметка #{item_id} не найдена или уже удалена]"
            tool_actions.append({"kind": "forget", "id": int(item_id)})
            return f"OK: заметка #{item_id} удалена"

        def _set_race_result_fn(race_id: int, actual_time: str, notes: str | None = None) -> str:
            ok = self._storage.set_race_result(user_id, int(race_id), actual_time, notes)
            if not ok:
                return f"[гонка #{race_id} не найдена]"
            tool_actions.append({"kind": "race_result", "id": int(race_id), "time": actual_time})
            return f"OK: результат #{race_id} = {actual_time}"

        def _retract_fact_fn(fact_id: int) -> str:
            ok = self._storage.deactivate_verified_fact(user_id, int(fact_id))
            if not ok:
                return f"[факт #{fact_id} не найден или уже отозван]"
            tool_actions.append({"kind": "fact_retract", "id": int(fact_id)})
            return f"OK: факт #{fact_id} отозван"

        def _add_race_fn(race_date: str, name: str, distance_km: float | None = None,
                         goal_time: str | None = None, notes: str | None = None) -> str:
            try:
                datetime.fromisoformat(race_date)
            except Exception:
                return "[ошибка: race_date должен быть YYYY-MM-DD, получил '%s']" % race_date
            name = (name or "").strip()
            if not name:
                return "[ошибка: пустое название гонки]"
            for r in self._storage.get_races(user_id):
                if r["date"] == race_date and r["name"].strip().lower() == name.lower():
                    return f"[гонка уже в календаре: #{r['id']} {race_date} — {name}]"
            rid = self._storage.save_race(user_id, race_date, name, distance_km, goal_time, notes)
            tool_actions.append({"kind": "race_add", "id": rid, "date": race_date, "name": name})
            return f"OK: гонка #{rid} добавлена ({race_date} — {name})"

        def _delete_race_fn(race_id: int) -> str:
            ok = self._storage.delete_race(user_id, int(race_id))
            if not ok:
                return f"[гонка #{race_id} не найдена]"
            tool_actions.append({"kind": "race_del", "id": int(race_id)})
            return f"OK: гонка #{race_id} удалена"

        def _set_race_priority_fn(race_id: int, is_priority: bool = True) -> str:
            ok = self._storage.set_race_priority(user_id, int(race_id), bool(is_priority))
            if not ok:
                return f"[гонка #{race_id} не найдена]"
            tool_actions.append({"kind": "race_priority", "id": int(race_id), "on": bool(is_priority)})
            return f"OK: гонка #{race_id} — " + ("A-гонка" if is_priority else "приоритет снят")

        def _record_feeling_fn(score: int, note: str | None = None) -> str:
            try:
                score = int(score)
            except Exception:
                return "[ошибка: score должен быть числом 1-5]"
            if score < 1 or score > 5:
                return "[ошибка: score должен быть 1..5]"
            self._storage.save_feeling(user_id, today.isoformat(), score, note)
            tool_actions.append({"kind": "feeling", "score": score})
            return f"OK: самочувствие за {today.isoformat()} = {score}/5"

        async def _set_training_goal_async(goal_text: str) -> str:
            goal_text = (goal_text or "").strip()
            if not goal_text:
                return "[ошибка: пустой goal_text]"
            self._storage.save_goal(user_id, goal_text)
            # Сбросить план на текущую неделю чтобы он пересчитался под новую цель.
            today_d = datetime.now(self._get_user_tz(user_id)).date()
            week_start_iso = (today_d - timedelta(days=today_d.weekday())).isoformat()
            self._storage.clear_plan(user_id, week_start_iso)
            # Авто-извлечение дат → A-гонки (как делает /goal). Не блокирует —
            # если парсер цели сломан, цель всё равно сохранена.
            added_lines: list[str] = []
            try:
                added_lines = await self._sync_races_from_goal(user_id, goal_text, today_d)
            except Exception as exc:
                logger.warning("set_training_goal: race-extraction failed: %s", exc)
            tail = ""
            if added_lines:
                tail = " A-гонки извлечены: " + "; ".join(added_lines)
            tool_actions.append({"kind": "goal", "text": goal_text})
            return f"OK: цель сохранена, план на эту неделю сброшен." + tail

        # ── Сеттеры профиля словами (те же поля, что и текст-команды/анкета) ──
        def _set_weight_fn(weight_kg: float) -> str:
            try:
                weight_kg = float(weight_kg)
            except Exception:
                return "[ошибка: weight_kg должен быть числом]"
            if not 30 <= weight_kg <= 200:
                return "[ошибка: вес вне разумного диапазона 30–200 кг]"
            self._storage.save_profile_override(user_id, weight_kg=weight_kg)
            tool_actions.append({"kind": "profile_set", "field": "вес", "value": f"{weight_kg:g} кг"})
            return f"OK: вес {weight_kg:g} кг сохранён в профиль (ISSN-нормы пересчитаются)"

        def _set_lthr_fn(lthr: float) -> str:
            try:
                lthr = float(lthr)
            except Exception:
                return "[ошибка: lthr должен быть числом]"
            if not 100 <= lthr <= 220:
                return "[ошибка: LTHR вне диапазона 100–220]"
            self._storage.save_profile_override(user_id, lthr=lthr)
            tool_actions.append({"kind": "profile_set", "field": "LTHR", "value": f"{lthr:.0f} уд/мин"})
            return f"OK: LTHR {lthr:.0f} сохранён в профиль"

        def _set_timezone_fn(timezone: str) -> str:
            tz_name = (timezone or "").strip()
            try:
                ZoneInfo(tz_name)
            except Exception:
                return f"[ошибка: неизвестный часовой пояс '{tz_name}' — нужен IANA-формат, например Europe/Moscow]"
            self._storage.save_profile_override(user_id, timezone=tz_name)
            tool_actions.append({"kind": "profile_set", "field": "часовой пояс", "value": tz_name})
            return f"OK: часовой пояс {tz_name} сохранён"

        def _set_experience_fn(years: float) -> str:
            try:
                years = float(years)
            except Exception:
                return "[ошибка: years должен быть числом]"
            if not 0 <= years <= 60:
                return "[ошибка: стаж вне диапазона 0–60 лет]"
            self._storage.save_profile_override(user_id, running_experience_years=years)
            tool_actions.append({"kind": "profile_set", "field": "стаж", "value": f"{years:g} лет"})
            return f"OK: беговой стаж {years:g} лет сохранён"

        # ── «Слова = кнопка»: запуск того же хендлера, что и кнопка ──
        _ACTION_HANDLERS = {
            "morning": self.handle_morning,
            "workout": self.handle_workout,
            "sport_status": self.handle_sport_status,
            "plan": self.handle_plan,
            "progress": self.handle_progress,
            "weekly_summary": self.handle_weekly_summary,
            "calories": self.handle_calories,
            "records": self.handle_records,
            "status": self.status,
            "help": self.help_cmd,
        }

        async def _invoke_action_fn(action: str) -> str:
            handler = _ACTION_HANDLERS.get((action or "").strip())
            if handler is None:
                return f"[ошибка: неизвестное действие '{action}'. Доступны: {', '.join(_ACTION_HANDLERS)}]"
            # Хендлер сам синкает, считает и шлёт сообщения юзеру — тем же
            # конвейером, что и кнопка (план: фазы, hard-safety, километраж, погода).
            await handler(update, context)
            return (
                f"OK: «{action}» выполнен настоящим конвейером, результат УЖЕ отправлен юзеру "
                "отдельными сообщениями. НЕ пересказывай и НЕ сочиняй его содержимое — "
                "просто коротко подтверди одной фразой."
            )

        write_tools = {
            "confirm_fact": _confirm_fact_fn,
            "remember_note": _remember_note_fn,
            "forget_note": _forget_note_fn,
            "set_race_result": _set_race_result_fn,
            "record_feeling": _record_feeling_fn,
            "set_training_goal": _set_training_goal_async,
            "add_race": _add_race_fn,
            "delete_race": _delete_race_fn,
            "set_race_priority": _set_race_priority_fn,
            "retract_fact": _retract_fact_fn,
            "set_weight": _set_weight_fn,
            "set_lthr": _set_lthr_fn,
            "set_timezone": _set_timezone_fn,
            "set_experience": _set_experience_fn,
            "invoke_action": _invoke_action_fn,
        }

        # Stage 5: считаем факты дня и недели в коде, подаём Claude как готовые блоки
        from . import coach as _coach
        qa_profile = self._storage.get_profile_override(user_id)
        # активности за 8 дней (хватает на текущую неделю + вчерашний день)
        qa_week_acts = await asyncio.to_thread(self._service.collect_recent_activities, user_id, days=8)
        qa_week_start = today - timedelta(days=today.weekday())
        qa_week_facts = _coach.compute_week_facts(
            activities=qa_week_acts,
            week_start=qa_week_start,
            week_end=today,
            plan_meta=plan_meta,
            profile=qa_profile,
        )
        qa_morning_facts = _coach.compute_morning_facts(metrics or {}, today=today) if metrics else None

        try:
            answer = await self._analyst.ask(
                question, metrics, history=history, user_memory=user_memory,
                upcoming_races=upcoming_races, training_goal=training_goal,
                current_plan=current_plan, current_week_type=current_week_type, db_paths=db_paths,
                user_id=user_id, today_iso=today.isoformat(),
                save_plan_fn=_save_plan_fn,
                write_tools=write_tools,
                verified_facts=verified_facts,
                morning_facts=qa_morning_facts,
                week_facts=qa_week_facts,
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

        # Save extracted memories — отфильтровать «целе-планово-гонко» подобные
        # (для них есть структурные команды; не давать им жить в user_memory).
        saved_memories: list[str] = []
        rejected: list[tuple[str, str]] = []
        for mem_text, mem_expiry in memories:
            reason = _classify_bad_memory(mem_text)
            if reason:
                rejected.append((mem_text, reason))
                continue
            new_id = self._storage.add_memory_item(user_id, mem_text, expires_at=mem_expiry)
            if new_id is not None:
                suffix = f" (до {mem_expiry})" if mem_expiry else ""
                saved_memories.append(f"{mem_text}{suffix}")
        if saved_memories:
            await update.message.reply_text(
                f"💾 Запомнил: {'; '.join(saved_memories)}",
                reply_markup=MAIN_KEYBOARD,
            )
        if rejected:
            logger.info(
                "Auto-memory rejected for user_id=%s: %s",
                user_id,
                "; ".join(f"{m!r}→{r}" for m, r in rejected),
            )

        # Видимые подтверждения действий, выполненных через write-tools (Claude
        # сам вызвал confirm_fact / remember_note / forget_note / set_race_result /
        # record_feeling). Группируем по типу — одно сообщение на тип.
        if tool_actions:
            lines: list[str] = []
            for a in tool_actions:
                k = a["kind"]
                if k == "fact":
                    lines.append(f"✅ Принял как факт ({a['date']}): {a['text']}")
                elif k == "fact_retract":
                    lines.append(f"↩️ Факт #{a['id']} отозван")
                elif k == "remember":
                    exp = f" (до {a['expires_at']})" if a.get("expires_at") else ""
                    lines.append(f"💾 Запомнил #{a['id']}: {a['text']}{exp}")
                elif k == "forget":
                    lines.append(f"🗑 Удалил заметку #{a['id']}")
                elif k == "race_result":
                    lines.append(f"🏁 Результат гонки #{a['id']}: {a['time']}")
                elif k == "race_add":
                    lines.append(f"🏁 Гонка #{a['id']} в календаре: {a['date']} — {a['name']}")
                elif k == "race_del":
                    lines.append(f"🗑 Гонка #{a['id']} удалена из календаря")
                elif k == "race_priority":
                    lines.append(f"⭐ Гонка #{a['id']}: " + ("A-гонка (главный старт)" if a["on"] else "приоритет снят"))
                elif k == "profile_set":
                    lines.append(f"📋 Профиль обновлён — {a['field']}: {a['value']}")
                elif k == "feeling":
                    lines.append(f"📝 Самочувствие за сегодня: {a['score']}/5")
                elif k == "goal":
                    lines.append(f"🎯 Новая цель сохранена. План на эту неделю пересчитается при следующем «📅 План».")
            if lines:
                await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)

        # Save to conversation history
        self._storage.add_message(user_id, "user", question, source="qa")
        self._storage.add_message(user_id, "assistant", answer_clean, source="qa")
