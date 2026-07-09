"""SQL-инструменты Claude (read-only) и схемы tools для _ask_with_tools.

Безопасность: все БД открываются mode=ro; разрешены только SELECT и
PRAGMA TABLE_*; app.db отдаётся как in-memory копия строк текущего user_id
без таблиц-секретов (garmin_credentials, web_tokens). Изоляция пользователей —
на уровне движка, а не текста промпта.
"""

from __future__ import annotations

import logging
import sqlite3 as _sqlite3
from typing import Callable

logger = logging.getLogger(__name__)


def make_sql_runner(db_paths: dict[str, str], user_id: int | None) -> Callable[[str, str], str]:
    """Возвращает _run_sql(db_key, sql) — замыкание на пути БД и user_id."""

    # Таблицы app.db, которые разрешено видеть Claude (все с колонкой user_id).
    _APP_USER_TABLES = (
        "weekly_plans", "training_goal",
        "races", "user_profile_overrides", "food_entries",
    )

    def _open_ro(path: str) -> "_sqlite3.Connection":
        return _sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def _build_app_view(app_path: str, uid: int | None) -> "_sqlite3.Connection":
        """In-memory копия app.db только со строками этого user_id."""
        mem = _sqlite3.connect(":memory:")
        src = _open_ro(app_path)
        try:
            for tbl in _APP_USER_TABLES:
                ddl = src.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (tbl,),
                ).fetchone()
                if not ddl or not ddl[0]:
                    continue
                mem.execute(ddl[0])
                cols = [r[1] for r in src.execute(f"PRAGMA table_info({tbl})").fetchall()]
                if "user_id" in cols:
                    # Без известного user_id фильтровать нечем — лучше пусто, чем утечка.
                    rows = src.execute(
                        f"SELECT * FROM {tbl} WHERE user_id = ?", (uid,)
                    ).fetchall() if uid is not None else []
                else:
                    rows = src.execute(f"SELECT * FROM {tbl}").fetchall()
                if rows:
                    ph = ",".join(["?"] * len(cols))
                    mem.executemany(f"INSERT INTO {tbl} VALUES ({ph})", rows)
            mem.commit()
        finally:
            src.close()
        return mem

    def _run_sql(db_key: str, sql: str) -> str:
        db_path = db_paths.get(db_key)
        if not db_path:
            return f"[ошибка: база {db_key} не найдена]"
        sql_stripped = sql.strip().upper()
        allowed = sql_stripped.startswith("SELECT") or sql_stripped.startswith((
            "PRAGMA TABLE_INFO", "PRAGMA TABLE_LIST", "PRAGMA TABLE_XINFO",
        ))
        if not allowed:
            return "[ошибка: разрешены только SELECT и PRAGMA TABLE_INFO]"
        conn = None
        try:
            conn = _build_app_view(db_path, user_id) if db_key == "app" else _open_ro(db_path)
            conn.row_factory = _sqlite3.Row
            rows = conn.execute(sql, []).fetchmany(200)
            result = [dict(r) for r in rows]
            return str(result) if result else "[]"
        except Exception as e:
            logger.warning("Tool SQL failed (db=%s): %s", db_key, e)
            return "[ошибка: запрос не выполнен — проверь синтаксис и имена колонок]"
        finally:
            if conn is not None:
                conn.close()

    return _run_sql


def build_tool_schemas(
    save_plan_fn: Callable[[str, str], str] | None = None,
    write_tools: dict[str, Callable[..., str]] | None = None,
) -> list[dict]:
    """Схемы read/write tools. Write-tools добавляются только если переданы коллбеки."""
    tools = [
        {
            "name": "query_health_db",
            "description": (
                "Выполни SELECT к базе здоровья Garmin (garmin.db). Таблицы и колонки:\n"
                "• sleep — day, start, end, total_sleep, deep_sleep, light_sleep, rem_sleep, awake, "
                "avg_spo2, avg_rr, avg_stress, score, qualifier\n"
                "• resting_hr — day, resting_heart_rate\n"
                "• daily_summary — day, rhr, hr_min, hr_max, stress_avg, steps, step_goal, distance, "
                "calories_total, calories_active, calories_bmr, calories_consumed, "
                "moderate_activity_time, vigorous_activity_time, intensity_time_goal, "
                "floors_up, floors_down, hydration_intake, sweat_loss, "
                "spo2_avg, spo2_min, rr_waking_avg, rr_max, rr_min, bb_charged, bb_max, bb_min, description\n"
                "• weight — day, weight\n"
                "• stress — timestamp, stress (внутридневной ряд)\n"
                "• sleep_events — timestamp, event, duration\n"
                "ВАЖНО: HRV здесь НЕТ — он передан в снапшоте контекста, не в SQL. "
                "Калории — это colонки calories_* в daily_summary, НЕ просто `calories`."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            },
        },
        {
            "name": "query_activities_db",
            "description": (
                "Выполни SELECT к базе тренировок Garmin (garmin_activities.db). Таблицы и колонки:\n"
                "• activities — activity_id, name, sport, sub_sport, start_time, stop_time, "
                "elapsed_time, moving_time, distance, calories, "
                "avg_hr, max_hr, avg_rr, max_rr, avg_cadence, max_cadence, avg_speed, max_speed, "
                "ascent, descent, training_load, training_effect, anaerobic_training_effect, "
                "self_eval_feel, self_eval_effort, hr_zones_method, "
                "hrz_1_hr..hrz_5_hr (нижние границы зон), hrz_1_time..hrz_5_time (время в зонах), "
                "avg_temperature, max_temperature, min_temperature, start_lat, start_long\n"
                "• activity_laps — activity_id, lap, start_time, distance, elapsed_time, moving_time, "
                "avg_hr, max_hr, avg_speed, max_speed, avg_cadence, ascent, descent, calories, "
                "hrz_1_time..hrz_5_time (автосплиты, обычно по 1 км)\n"
                "• activity_splits — activity_id, split, completed, distance, moving_time, "
                "avg_hr, max_hr, avg_speed, avg_cadence, calories (ручные/тренерские сплиты)\n"
                "• activity_records — activity_id, record, timestamp, position_lat, position_long, "
                "distance, cadence, altitude, hr, rr, speed, temperature "
                "(посекундные точки; КОЛОНКА ПУЛЬСА НАЗЫВАЕТСЯ hr, НЕ heart_rate)\n"
                "• steps_activities — activity_id, steps, avg_pace, avg_moving_pace, max_pace, "
                "avg_steps_per_min, max_steps_per_min, avg_step_length, vo2_max, "
                "avg_ground_contact_time, avg_vertical_ratio, avg_vertical_oscillation, "
                "avg_gct_balance, avg_stance_time_percent (метрики бега/ходьбы)\n"
                "ВАЖНО: для сплитов используй activity_laps или activity_splits, НЕ activity_records. "
                "Для пейса в running — steps_activities.avg_pace (формат TIME, мин/км)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            },
        },
        {
            "name": "query_app_db",
            "description": (
                "Выполни SELECT к базе приложения. "
                "Таблицы: weekly_plans (user_id, week_start, plan_text), "
                "training_goal (user_id, goal_text) — ИМЕННО training_goal, единственное число, "
                "races (user_id, name, date, distance_km, goal_time), "
                "user_profile_overrides (user_id, lthr, weight_kg, timezone, age, weekly_km_target), "
                "food_entries (user_id, entry_date, entry_time, description, calories, protein_g, fat_g, carbs_g) — еда по дням."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            },
        },
    ]

    # ===== Write-tools для естественного диалога =====
    # Цель: пользователь общается обычным текстом, бот САМ распознаёт намерение
    # и вызывает нужный tool. Никаких команд знать не нужно.
    if write_tools and "confirm_fact" in write_tools:
        tools.append({
            "name": "confirm_fact",
            "description": (
                "Сохрани УТВЕРЖДЁННЫЙ пользователем факт за конкретную дату — становится "
                "источником истины, который ты будешь видеть в будущих контекстах.\n"
                "🚫 НЕ записывай через confirm_fact: вес, LTHR, часовой пояс — для них "
                "кнопки ⚖ Вес / 💓 LTHR / 🕐 Часы (иначе профиль и факты разойдутся, "
                "ISSN-нормы питания не увидят вес из факта).\n"
                "Вызывай, когда пользователь явно поправляет/утверждает данные:\n"
                "• «это правильно: 56 км за неделю» / «верно» (поправка после твоей ошибки)\n"
                "• «вчера было темповая Z4, а не Z3»\n"
                "• «итог неделя 15-21.06 — 56.14 км бега»\n"
                "• «в субботу 20.06 — отдых, не тренировка»\n"
                "• «пятница 19.06 — темповая 10.5 км Z4 5:46/км, чёткое выполнение»\n"
                "НЕ вызывай для бытовых разговоров и предположений. Только когда юзер "
                "ПОПРАВЛЯЕТ или ПОДТВЕРЖДАЕТ конкретные цифры/факты за дату.\n"
                "fact_date — YYYY-MM-DD. Если юзер сказал «вчера» — резолви относительно «Сегодня» в контексте."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "fact_date": {"type": "string", "description": "ISO дата факта (YYYY-MM-DD)"},
                    "fact_text": {"type": "string", "description": "Краткая формулировка факта"},
                },
                "required": ["fact_date", "fact_text"],
            },
        })
    if write_tools and "remember_note" in write_tools:
        tools.append({
            "name": "remember_note",
            "description": (
                "Сохрани долговременную заметку об атлете в персональную память (она будет "
                "видна в системном промпте всегда, пока не истечёт срок).\n"
                "Вызывай вместо тега [ЗАПОМНИТЬ]. Поводы:\n"
                "• Травмы, болезни, хронические состояния («болит ахилл»)\n"
                "• Предпочтения и расписание («не бегаю по средам»)\n"
                "• Стиль общения («пиши короче»)\n"
                "• Курсы лекарств с СРОКОМ — обязательно ставь expires_at\n"
                "expires_at — необязательное YYYY-MM-DD. Для курсов/отпусков считай дату окончания.\n"
                "🚫 Не вызывай для целей, гонок, веса, LTHR, результатов забегов — есть структурные таблицы."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Краткая формулировка"},
                    "expires_at": {"type": "string", "description": "YYYY-MM-DD или пусто"},
                },
                "required": ["text"],
            },
        })
    if write_tools and "forget_note" in write_tools:
        tools.append({
            "name": "forget_note",
            "description": (
                "Деактивируй заметку из памяти по её id (видишь id в блоке «Важная информация» — "
                "они приходят в формате «#N. текст»).\n"
                "Вызывай когда юзер говорит «забудь это», «уже не актуально», «убери про X», "
                "«антибиотики допил» (если запись о курсе была). Если в памяти нет подходящей "
                "заметки — не вызывай."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer", "description": "ID заметки из памяти"},
                },
                "required": ["item_id"],
            },
        })
    if write_tools and "set_race_result" in write_tools:
        tools.append({
            "name": "set_race_result",
            "description": (
                "Сохрани фактический результат гонки (race_id из «ПРЕДСТОЯЩИЕ СТАРТЫ» или "
                "«НЕДАВНО ПРОБЕЖАЛ» — это структурный источник истины).\n"
                "Вызывай когда юзер сообщает результат прошедшего старта:\n"
                "• «ночной забег 49:52, сплиты 0-5 23:59, 5-10 25:53» (race_id=4 из контекста)\n"
                "• «забег субботу пробежал 47:30»\n"
                "Если race_id неоднозначен — спроси юзера, какая именно гонка. "
                "Если соответствующей гонки в races нет — лучше confirm_fact, а не выдумывай id."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "race_id": {"type": "integer"},
                    "actual_time": {"type": "string", "description": "Формат «MM:SS» или «H:MM:SS»"},
                    "notes": {"type": "string", "description": "Сплиты, темп, ощущения"},
                },
                "required": ["race_id", "actual_time"],
            },
        })
    if write_tools and "set_training_goal" in write_tools:
        tools.append({
            "name": "set_training_goal",
            "description": (
                "Перезаписать главную тренировочную цель атлета (training_goal.goal_text). "
                "Эта цель — «север» планирования, видна в каждом отчёте/плане/прогрессе.\n"
                "Вызывай когда юзер ЯВНО говорит про новую/обновлённую цель:\n"
                "  • «новая цель — марафон Москва 27.09, 3:55»\n"
                "  • «убери Грут из целей, основная теперь марафон»\n"
                "  • «бегать 4 раза в неделю» (без даты — тоже валидно)\n"
                "После записи существующий план на текущую неделю автоматически сбрасывается, "
                "чтобы план пересчитался под новую цель. Из текста цели парсятся даты — "
                "если они есть, бот заведёт A-гонки сам.\n"
                "НЕ вызывай для частичных уточнений типа «темп 5:33» — это не «новая цель»."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "goal_text": {"type": "string", "description": "Полный текст новой цели (как юзер её сформулировал)"},
                },
                "required": ["goal_text"],
            },
        })
    if write_tools and "record_feeling" in write_tools:
        tools.append({
            "name": "record_feeling",
            "description": (
                "Запиши субъективное самочувствие за день. Вызывай когда юзер говорит:\n"
                "• «сегодня чувствую на 3», «плохое самочувствие», «отлично себя чувствую»\n"
                "• «устала», «полно сил» — резолви в score 1-5\n"
                "score: 1=очень плохо, 2=плохо, 3=нормально, 4=хорошо, 5=отлично.\n"
                "note — короткий текст пояснения от юзера (если есть)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "score": {"type": "integer", "minimum": 1, "maximum": 5},
                    "note": {"type": "string"},
                },
                "required": ["score"],
            },
        })

    if write_tools and "retract_fact" in write_tools:
        tools.append({
            "name": "retract_fact",
            "description": (
                "Отзови (деактивируй) ранее сохранённый факт по его #id из блока "
                "«ПОДТВЕРЖДЁННЫЕ АТЛЕТОМ ФАКТЫ». ОБЯЗАТЕЛЕН при исправлении: если юзер "
                "поправляет уже зафиксированный факт («на самом деле было не так») — "
                "СНАЧАЛА retract_fact(старый #id), ПОТОМ confirm_fact(новая версия). "
                "Иначе в базе останутся два противоречащих факта за одну дату "
                "(инцидент 09.07: три взаимоисключающих факта про один день)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"fact_id": {"type": "integer"}},
                "required": ["fact_id"],
            },
        })
    if write_tools and "add_race" in write_tools:
        tools.append({
            "name": "add_race",
            "description": (
                "Добавь ПРЕДСТОЯЩУЮ гонку в календарь стартов (таблица races). "
                "Вызывай когда юзер говорит «добавь забег/гонку/марафон <название> <дата>». "
                "race_date СТРОГО YYYY-MM-DD — год бери из блока КАЛЕНДАРЬ, не выдумывай. "
                "Дубль по дате+названию отсекается автоматически. "
                "Для ПРОШЕДШЕГО старта с временем — set_race_result, не add_race."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "race_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "name": {"type": "string"},
                    "distance_km": {"type": "number", "description": "дистанция в км, если известна"},
                    "goal_time": {"type": "string", "description": "целевое время H:MM:SS, если названо"},
                    "notes": {"type": "string", "description": "заметка: трейл/шоссе, «бежать легко» и т.п."},
                },
                "required": ["race_date", "name"],
            },
        })
    if write_tools and "delete_race" in write_tools:
        tools.append({
            "name": "delete_race",
            "description": (
                "Удали гонку из календаря. Вызывай на «удали забег #N» или однозначное "
                "«убери марафон из стартов». race_id — из блока «ПРЕДСТОЯЩИЕ СТАРТЫ» (#N). "
                "Если id не очевиден — сначала уточни у юзера, не угадывай."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"race_id": {"type": "integer"}},
                "required": ["race_id"],
            },
        })
    if write_tools and "set_race_priority" in write_tools:
        tools.append({
            "name": "set_race_priority",
            "description": (
                "Пометь гонку как A-гонку (главный старт, ведёт периодизацию плана) "
                "или сними пометку. «Это моя главная гонка» → is_priority=true."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "race_id": {"type": "integer"},
                    "is_priority": {"type": "boolean", "default": True},
                },
                "required": ["race_id"],
            },
        })
    if save_plan_fn is not None:
        tools.append({
            "name": "save_weekly_plan",
            "description": (
                "Сохрани план недели в weekly_plans (UPSERT по user_id+week_start). "
                "Неделя определяется АВТОМАТИЧЕСКИ из дат в plan_text — можно сохранить "
                "план и на следующую неделю. Код проверяет каждую пару «день DD.MM» "
                "по реальному календарю: при несовпадении получишь ошибку с правильным "
                "маппингом — исправь даты и вызови повторно. Даты бери ТОЛЬКО из блока "
                "КАЛЕНДАРЬ в контексте, не вычисляй сам. "
                "Используй ТОЛЬКО когда пользователь явно просит «сохрани/запиши/зафиксируй» план, "
                "который вы согласовали в этом диалоге. "
                "Не вызывай без подтверждения пользователя. "
                "Не вызывай если только что обсуждаемый план ещё не финализирован. "
                "plan_text — полный текст плана для отображения юзеру (по дням Пн-Вс, "
                "формат «День DD.MM — Тип, дистанция, зона/темп»). "
                "week_type — одно из: recovery, base, build, peak, taper "
                "(выбери по содержанию плана: пиковая нагрузка → peak, тейпер перед стартом → taper, "
                "восстановительная → recovery, базовый объём → base, развивающая → build)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "plan_text": {"type": "string", "description": "Полный текст плана недели"},
                    "week_type": {
                        "type": "string",
                        "enum": ["recovery", "base", "build", "peak", "taper"],
                    },
                },
                "required": ["plan_text", "week_type"],
            },
        })
    return tools
