"""Домен «отчёты тренера»: утро, разбор, план (+твики), форма, прогресс, итоги, рекорды.
"""


import asyncio
import contextlib
import json
import logging
import re
from datetime import date, datetime, timedelta

from telegram import (
    Update,
)
from telegram.ext import ContextTypes

from .bot_common import BTN_MORNING, BTN_WORKOUT, MAIN_KEYBOARD, _api_error_msg, _is_garmin_auth_error

logger = logging.getLogger(__name__)


_PR_LABEL_TO_KM = {
    "1 км": 1.0, "5 км": 5.0, "10 км": 10.0, "15 км": 15.0,
    "Полумарафон": 21.1, "Марафон": 42.2,
}


def _label_to_km(label: str) -> float:
    """Map PR bucket label → canonical distance in km (для сравнения с факт. дистанцией)."""
    return _PR_LABEL_TO_KM.get(label.strip(), 0.0)


class ReportsMixin:

    _PLAN_TWEAK_PATTERNS = re.compile(
        r'(?:план|тренировк[иу]|неделю|расписание)\b.*'
        r'(?:полегче|потяжел|легче|сложнее|проще|тяжелее|измени|поменяй|перестрой|скорректируй'
        r'|добав[ьи]|убер[иь]|замен[иь]|больше|меньше|без\s|не\s+хочу|хочу|сдвин[ьу]|перенес[иь])'
        r'|(?:полегче|потяжел|легче|сложнее|проще|тяжелее)\b.*(?:план|тренировк|недел)'
        r'|(?:добав[ьи]|убер[иь]|замен[иь]|больше|меньше|хочу|не\s+хочу)\b.*'
        r'(?:интервал|темпов|длинн|бег[аоу]|отдых|восстановлен|силов|горк[иу]|фартлек|разминк)',
        re.IGNORECASE,
    )

    _PLAN_REQUEST_PATTERNS = re.compile(
        r'(?:дай|давай|составь|сделай|нужен|нужна|новый|обнови|пересоставь|перестрой|пересчитай)\b'
        r'[\s\S]{0,40}?(?:план|расписание|тренировк[иу]|недел)',
        re.IGNORECASE,
    )

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

    async def handle_morning(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "coach"):
            return
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
                sync_sem = self._sync_sem_for(user_id)
                sync_exc: Exception | None = None
                try:
                    async with sync_sem:
                        await asyncio.to_thread(
                            self._service.run_health_sync,
                            user_id=user_id,
                            username=creds.username,
                            password=password,
                        )
                except Exception as exc:
                    sync_exc = exc
                    logger.warning("Health sync error (will use cached data): %s", exc)

                # Подтягиваем и активности тоже: после garth-миграции это
                # быстрая I/O-операция (5-10 сек). Без этого утренний бриф
                # говорит «пробежек не было», если вчерашняя пробёжка ещё
                # не подтянулась до morning (см. жалобу Алины #7).
                try:
                    async with sync_sem:
                        await asyncio.to_thread(
                            self._service.run_activity_sync,
                            user_id=user_id,
                            username=creds.username,
                            password=password,
                        )
                except Exception as exc:
                    logger.warning("Activity sync error in morning (using cached): %s", exc)

                today = datetime.now(self._get_user_tz(user_id)).date()
                yesterday = today - timedelta(days=1)
                metrics = await asyncio.to_thread(self._get_metrics, user_id, today)

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

            # План недели: строки на сегодня/завтра извлекает КОД — утро больше
            # не реконструирует план из истории чата (инцидент 10.07: «пробежек
            # нет по плану» при плановом беге 8 км).
            from . import coach as _coach_plan
            week_start_iso = (today - timedelta(days=today.weekday())).isoformat()
            plan_meta = await asyncio.to_thread(self._storage.get_plan_meta, user_id, week_start_iso)
            if plan_meta:
                metrics["plan_week_type"] = plan_meta.get("week_type") or ""
                metrics["plan_today_line"] = _coach_plan.plan_line_for_date(plan_meta["plan_text"], today)
                metrics["plan_tomorrow_line"] = _coach_plan.plan_line_for_date(
                    plan_meta["plan_text"], today + timedelta(days=1))
            else:
                metrics["plan_missing"] = True

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
            # Утренняя история — все «крупные» источники, чтобы бот видел ответы
            # пользователя из QA (например, результат вчерашней гонки) и не переспрашивал.
            history = self._storage.get_history(
                user_id, limit=12, sources=("morning", "workout", "qa", "plan_tweak")
            )
            user_memory = self._storage.get_user_memory(user_id)
            verified_facts = self._storage.list_verified_facts(
                user_id, since_date=(today - timedelta(days=21)).isoformat()
            )
            from . import coach as _coach
            morning_facts = _coach.compute_morning_facts(metrics, today=today)
            analysis = await self._analyst.analyze(
                metrics, history=history, user_memory=user_memory,
                verified_facts=verified_facts,
                morning_facts=morning_facts,
            )
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await spinner

        with contextlib.suppress(Exception):
            await status_msg.delete()

        analysis = self._strip_memory_tags(analysis)
        full_text = header + "\n" + analysis
        chunks = self._split(full_text)
        for chunk in chunks:
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

        # Yesterday's nutrition summary (sent after the main brief, uses freshly-synced Garmin data)
        nutrition_report = await asyncio.to_thread(self._build_yesterday_nutrition_report, user_id, yesterday)
        if nutrition_report:
            for chunk in self._split(nutrition_report):
                await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

        # Одна подсказка о недостающих данных (цель/гонка/профиль…) — в самом конце
        nudge_line = self._data_gap_footer(user_id, metrics, today)
        if nudge_line:
            await update.message.reply_text(nudge_line, reply_markup=MAIN_KEYBOARD)

        # Сохраняем полностью — обрывок в 800 симв. отравлял будущий контекст
        # (бот не видел собственного вывода, переспрашивал результат гонки и т.п.).
        # Окно conversation_messages всё равно ограничено keep_last=60 на юзера.
        self._storage.add_message(user_id, "user", BTN_MORNING, source="morning")
        morning_full = full_text + (f"\n\n{nutrition_report}" if nutrition_report else "") \
            + (f"\n\n{nudge_line}" if nudge_line else "")
        self._storage.add_message(user_id, "assistant", morning_full, source="morning")

    async def handle_workout(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "coach"):
            return
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
                    async with self._sync_sem_for(user_id):
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

                activities = await asyncio.to_thread(self._service.collect_recent_activities, user_id, days=14)
                # «Основная» активность дня — бег, а не последняя по времени
                # (иначе при дне «бег → силовая → заминка» разбор достаётся заминке)
                from . import coach as _coach_reorder
                activities = _coach_reorder.reorder_primary_activity(activities)
                # Health state for workout context: same date logic as morning report.
                # daily_summary from yesterday (complete day), sleep from today (wake date = today = last night).
                today = datetime.now(self._get_user_tz(user_id)).date()
                yesterday = today - timedelta(days=1)
                daily_metrics = await asyncio.to_thread(self._get_metrics, user_id, today, yesterday)
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
            cached_plan_meta = self._storage.get_plan_meta(user_id, week_start)
            cached_plan = cached_plan_meta["plan_text"] if cached_plan_meta else None
            cached_week_type = cached_plan_meta["week_type"] if cached_plan_meta else ""
            # Узкая история: только прошлые workout/qa, лимит 6 — этого хватает
            # на «вчера я говорил, что устал», без раздутия до 17k токенов.
            workout_history = self._storage.get_history(
                user_id, limit=6, sources=("workout", "qa")
            )
            verified_facts = self._storage.list_verified_facts(
                user_id, since_date=(today - timedelta(days=21)).isoformat()
            )
            # Stage 2: считаем факты в коде, не в промпте.
            from . import coach as _coach
            week_start_d = today - timedelta(days=today.weekday())
            profile = self._storage.get_profile_override(user_id)
            week_facts = _coach.compute_week_facts(
                activities=activities,
                week_start=week_start_d,
                week_end=today,
                plan_meta=cached_plan_meta,
                profile=profile,
            )
            workout_facts = (
                _coach.compute_workout_facts(activities[0]) if activities else None
            )
            analysis = await self._analyst.analyze_workout(
                activities, daily_metrics, history=workout_history,
                user_memory=user_memory, plan_text=cached_plan,
                week_type=cached_week_type,
                verified_facts=verified_facts,
                workout_facts=workout_facts,
                week_facts=week_facts,
                today_iso=today.isoformat(),
            )
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await spinner

        with contextlib.suppress(Exception):
            await status_msg.delete()

        analysis = self._strip_memory_tags(analysis)
        chunks = self._split(analysis)
        for chunk in chunks:
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

        # Полный текст, без [:800] — иначе бот теряет связность между сессиями.
        self._storage.add_message(user_id, "user", BTN_WORKOUT, source="workout")
        self._storage.add_message(user_id, "assistant", analysis, source="workout")

    async def handle_plan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "coach"):
            return
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

        metrics = None
        try:
            async with lock:
                password = self._box.decrypt(creds.password_encrypted)
                # Sync activities so the plan sees the latest workouts
                try:
                    async with self._sync_sem_for(user_id):
                        await asyncio.to_thread(
                            self._service.run_activity_sync,
                            user_id=user_id,
                            username=creds.username,
                            password=password,
                        )
                except Exception as exc:
                    logger.warning("Activity sync before plan failed (using cached data): %s", exc)
                yesterday = today - timedelta(days=1)
                metrics = await asyncio.to_thread(self._get_metrics, user_id, today)

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
                # Неделя известна коду заранее — даты в тексте просто переписываем
                from . import coach as _coach
                plan_text, n_fixes = _coach.fix_plan_dates(plan_text, date.fromisoformat(week_start))
                if n_fixes:
                    logger.warning("handle_plan: исправлено %d дат в сгенерированном плане", n_fixes)
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

        plan_text = self._strip_memory_tags(plan_text)
        chunks = self._split(plan_text)
        for chunk in chunks:
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

        nudge_line = self._data_gap_footer(user_id, metrics, today)
        if nudge_line:
            await update.message.reply_text(nudge_line, reply_markup=MAIN_KEYBOARD)
            self._storage.add_message(user_id, "assistant", nudge_line, source="plan")

    def _data_gap_footer(self, user_id: int, metrics: dict | None, today) -> str | None:
        """Одна подсказка о пробеле в данных (или None). Никогда не валит отчёт.

        lthr/weight в metrics["fitness_profile"] уже объединены с ручными
        override'ами (_get_metrics); если metrics нет — эти два пробела
        пропускаем, чтобы не просить то, что может лежать в Garmin.
        """
        try:
            from . import coach as _coach
            profile = self._storage.get_profile_override(user_id)
            goal = self._storage.get_goal(user_id)
            races = self._storage.get_races(user_id, from_date=today.isoformat())
            fp = (metrics or {}).get("fitness_profile") or {}
            gaps = _coach.data_gaps(
                goal=goal,
                has_future_races=bool(races),
                profile=profile,
                lthr=fp.get("lthr") if metrics else 0,
                weight_kg=fp.get("weight_kg") if metrics else 0,
            )
            # Новичок (ни цели, ни анкеты) — напоминаем чаще, пока не настроится
            newbie = not (goal or "").strip() and not profile.get("profile_completed")
            repeat = _coach.NUDGE_REPEAT_DAYS_NEWBIE if newbie else _coach.NUDGE_REPEAT_DAYS
            nudge = _coach.pick_nudge(gaps, self._storage.get_nudge_history(user_id), today,
                                      repeat_days=repeat)
            if not nudge:
                return None
            self._storage.log_nudge(user_id, nudge.key)
            return f"💡 Кстати: {nudge.hint}"
        except Exception:
            logger.exception("data-gap footer failed for user %s", user_id)
            return None

    async def handle_sport_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "coach"):
            return
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
            metrics = await asyncio.to_thread(self._get_metrics, user_id, today)
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

    async def handle_progress(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "coach"):
            return
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
            metrics = await asyncio.to_thread(self._get_metrics, user_id, today)
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
        if not await self._gate(update, "coach"):
            return
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
            metrics = await asyncio.to_thread(self._get_metrics, user_id, today)
            if not metrics:
                metrics = {"date": today.isoformat()}

            # Окно недели:
            # • Пн → ушедшая неделя Пн-Вс (юзер ждёт «итог завершившейся недели»)
            # • Иначе → текущая Пн → сегодня
            if today.weekday() == 0:
                week_start_date = today - timedelta(days=7)
                week_end_date = today - timedelta(days=1)
            else:
                week_start_date = today - timedelta(days=today.weekday())
                week_end_date = today
            week_start = week_start_date.isoformat()
            week_end_iso = week_end_date.isoformat()
            # План для сравнения берём по понедельнику ИМЕННО этой недели (а не «текущей»)
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
                    from . import coach as _coach
                    plan_text, n_fixes = _coach.fix_plan_dates(plan_text, date.fromisoformat(week_start))
                    if n_fixes:
                        logger.warning("weekly_summary: исправлено %d дат в сгенерированном плане", n_fixes)
                    self._storage.save_plan(user_id, week_start, plan_text, week_type)
                except Exception as exc:
                    logger.warning("Could not auto-generate plan for weekly summary: %s", exc)
                    plan_text = ""

            feelings_stats = self._storage.get_feelings_stats(user_id, days=7)
            user_memory = self._storage.get_user_memory(user_id)

            # Collect food entries for the week
            week_food: list[dict] = []
            for i in range(7):
                day = week_start_date + timedelta(days=i)
                if day > week_end_date:
                    break
                day_entries = self._storage.get_food_entries(user_id, day.isoformat())
                week_food.extend(day_entries)

            # Garmin daily calories for the week
            garmin_week_cal: dict[str, dict] = {}
            for i in range(7):
                day = week_start_date + timedelta(days=i)
                if day > week_end_date:
                    break
                dc = await asyncio.to_thread(self._get_garmin_daily_calories, user_id, day)
                if dc:
                    garmin_week_cal[day.isoformat()] = dc

            # Активности окна (с понедельника недели). collect_recent_activities
            # тянет от date.today()−days, поэтому глубину берём с запасом.
            days_back = (today - week_start_date).days + 2
            all_recent = await asyncio.to_thread(self._service.collect_recent_activities, user_id, days=days_back)
            week_activities = [
                a for a in all_recent
                if week_start <= (a.get("start_time") or "")[:10] <= week_end_iso
            ]

            weight_kg = await asyncio.to_thread(self._get_user_weight, user_id)

            verified_facts = self._storage.list_verified_facts(
                user_id, since_date=week_start,
            )
            from . import coach as _coach
            wprofile = self._storage.get_profile_override(user_id)
            wplan_meta = self._storage.get_plan_meta(user_id, week_start)
            wweek_facts = _coach.compute_week_facts(
                activities=week_activities,
                week_start=week_start_date,
                week_end=week_end_date,
                plan_meta=wplan_meta,
                profile=wprofile,
            )
            report = await self._analyst.analyze_weekly_summary(
                metrics=metrics,
                plan_text=plan_text,
                feelings_stats=feelings_stats,
                user_memory=user_memory,
                food_entries=week_food,
                garmin_daily_calories=garmin_week_cal,
                weight_kg=weight_kg,
                week_activities=week_activities,
                verified_facts=verified_facts,
                week_facts=wweek_facts,
            )
        except Exception as exc:
            logger.exception("Error generating weekly summary")
            report = _api_error_msg(exc, "итог недели")

        with contextlib.suppress(Exception):
            await status_msg.delete()

        for chunk in self._split(report):
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)

    async def handle_records(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, "coach"):
            return
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

        import html as _html
        lines = ["🏆 <b>ЛИЧНЫЕ РЕКОРДЫ</b>", ""]
        for pr in records:
            hr_str = f"  ·  пульс {pr['avg_hr']}" if pr.get("avg_hr") else ""
            label = _html.escape(str(pr["distance"]))
            # Показываем фактическую дистанцию в строке заголовка — иначе
            # «5 км · 22:50 · 4:30» вызывает диссонанс (на самом деле 5.07 км).
            dist_km = pr.get("dist_km")
            dist_actual = f" ({dist_km:.2f} км)" if dist_km and abs(dist_km - _label_to_km(label)) > 0.05 else ""
            time_s = _html.escape(str(pr["time"]))
            pace = _html.escape(str(pr["pace"]))
            name = _html.escape(str(pr.get("name") or ""))
            date_s = _html.escape(str(pr.get("date") or ""))
            lines.append(f"<b>{label}</b>{dist_actual}  ·  {time_s}  ·  {pace}/км{hr_str}")
            lines.append(f"  └ {date_s} — {name}")
            lines.append("")  # отбивка между рекордами

        # Race predictions — таблица с фиксированной шириной → завернём в <pre>
        # чтобы Telegram отрисовал моноширинно (тогда колонки выровняются).
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
                lines.append(
                    f"🎯 <b>Прогноз финиша</b> (Daniels, VO2max {vo2})"
                )
                lines.append(
                    f"<i>Garmin VO2max часто завышает на 2-5 пунктов — реалистичнее {vo2 - 3}</i>"
                )
                lines.append("")
                table = []
                table.append(f"{'Дистанция':<10}  {'Теор.':>8}  {'Реал.':>8}  {'Факт':>8}")
                table.append(f"{'─' * 10}  {'─' * 8}  {'─' * 8}  {'─' * 8}")
                for dist in predictions:
                    actual = next((pr for pr in records if pr["distance"] == dist), None)
                    fact_str = actual["time"] if actual else "—"
                    adj_str = adjusted.get(dist, "?") if adjusted else "?"
                    table.append(
                        f"{dist:<10}  {predictions[dist]:>8}  {adj_str:>8}  {fact_str:>8}"
                    )
                lines.append("<pre>" + _html.escape("\n".join(table)) + "</pre>")

        await update.message.reply_text(
            "\n".join(lines), reply_markup=MAIN_KEYBOARD, parse_mode="HTML",
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
            metrics = await asyncio.to_thread(self._get_metrics, user_id, today) or {"date": today.isoformat()}
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
            from . import coach as _coach
            plan_text, n_fixes = _coach.fix_plan_dates(plan_text, date.fromisoformat(week_start))
            if n_fixes:
                logger.warning("plan_tweak: исправлено %d дат в сгенерированном плане", n_fixes)
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
