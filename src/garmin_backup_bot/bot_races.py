"""Домен «цель и гонки»: тренировочная цель, календарь стартов, самочувствие.
"""


import logging
from datetime import datetime, timedelta

from telegram import (
    Update,
)
from telegram.ext import ContextTypes

from .bot_common import MAIN_KEYBOARD

logger = logging.getLogger(__name__)


class RacesMixin:

    async def handle_goal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "coach"):
            return
        """Set or view training goal: /goal [description]"""
        user_id = update.effective_user.id
        args = context.args or []
        if not args:
            current = self._storage.get_goal(user_id)
            if current:
                await update.message.reply_text(
                    f"Твоя текущая цель:\n{current}\n\n💬 Чтобы поменять — просто скажи: «новая цель — …»",
                    reply_markup=MAIN_KEYBOARD,
                )
            else:
                await update.message.reply_text(
                    "Цель не задана.\n\n💬 Скажи мне словами:\n  «полумарафон Берлин 28.09, цель 1:45»\n  «бегать 3 раза в неделю»\n  «похудеть на 5кг к лету»",
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
        if not await self._gate(update, "coach"):
            return
        """Button handler: show current goal and how to change it conversationally."""
        user_id = update.effective_user.id
        current = self._storage.get_goal(user_id)
        if current:
            await update.message.reply_text(
                f"Твоя цель:\n{current}\n\n💬 Чтобы поменять — просто скажи: «новая цель — …»",
                reply_markup=MAIN_KEYBOARD,
            )
        else:
            await update.message.reply_text(
                "Цель не задана.\n\n💬 Скажи мне словами:\n"
                "  «полумарафон Берлин 28.09, цель 1:45»\n"
                "  «бегать 3 раза в неделю»\n"
                "  «похудеть на 5 кг к лету»",
                reply_markup=MAIN_KEYBOARD,
            )

    async def handle_feeling(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "coach"):
            return
        """Save subjective well-being score: /feeling 4 [optional note]"""
        user_id = update.effective_user.id
        args = (context.args or [])
        if not args:
            await update.message.reply_text(
                "💬 Просто скажи мне как самочувствие:\n"
                "  «чувствую на 4»\n"
                "  «отлично, полно сил»\n"
                "  «устал, болят ноги» (я пойму как ниже)\n\n"
                "Шкала: 1 = очень плохо, 5 = отлично.",
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
                if r.get("actual_time"):
                    actual = f" — {r['actual_time']}"
                else:
                    actual = " (результат не указан — скажи мне словами)"
                lines.append(f"  ✓ #{r['id']} {r['date']} — {r['name']}{dist}{actual}")

        lines.append(
            "\n💬 Просто скажи мне что нужно:"
            "\n  «добавь Московский марафон 27.09, цель 3:30»"
            "\n  «эта суббота была забег 49:52, сплиты 4:47 / 5:10»"
            "\n  «пометь марафон как главную гонку»"
            "\n  «удали забег #3»"
        )
        return "\n".join(lines)

    async def handle_race_btn(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "coach"):
            return
        self._track_event(update, "race_btn")
        user_id = update.effective_user.id
        text = self._format_race_calendar(user_id)
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)

    async def handle_race_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "coach"):
            return
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

        # Result — записать фактическое время гонки: /race result #N 49:52 [заметка]
        if args[0].lower() == "result" and len(args) >= 3:
            try:
                race_id = int(args[1].lstrip("#"))
            except ValueError:
                await update.message.reply_text(
                    "Укажи номер: /race result #3 49:52",
                    reply_markup=MAIN_KEYBOARD,
                )
                return
            actual_time = args[2].strip()
            actual_notes = " ".join(args[3:]).strip() or None
            if not self._storage.set_race_result(user_id, race_id, actual_time, actual_notes):
                await update.message.reply_text(
                    f"Старт #{race_id} не найден.", reply_markup=MAIN_KEYBOARD,
                )
                return
            tail = f"\nЗаметки: {actual_notes}" if actual_notes else ""
            await update.message.reply_text(
                f"✅ Результат старта #{race_id} сохранён: {actual_time}{tail}\n\n"
                + self._format_race_calendar(user_id),
                reply_markup=MAIN_KEYBOARD,
            )
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
