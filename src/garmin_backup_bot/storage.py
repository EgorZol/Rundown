from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class GarminCredentials:
    user_id: int
    username: str
    password_encrypted: str


@dataclass
class UsageStats:
    total_users: int
    dau: int
    wau: int
    mau: int
    retention_d1: float | None
    retention_d7: float | None
    retention_d30: float | None
    cohort_d1_size: int
    cohort_d7_size: int
    cohort_d30_size: int


class Storage:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS garmin_credentials (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL,
                    password_encrypted TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS web_tokens (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    event_name TEXT NOT NULL,
                    event_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_events_event_at ON usage_events(event_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_events_user_event_at ON usage_events(user_id, event_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'qa',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conv_user_created ON conversation_messages(user_id, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_memory (
                    user_id INTEGER PRIMARY KEY,
                    notes TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Per-item user memory: позволяет точечно удалять (/forget N),
            # ставить срок жизни (антибиотики и др. временные), дедуп по содержанию.
            # Старый user_memory.notes остаётся как fallback на время миграции.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_memory_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_items_user "
                "ON user_memory_items(user_id, is_active)"
            )
            # Одноразовая миграция из user_memory.notes: для каждого юзера,
            # у которого ещё нет ни одного item — split notes по \n и залить
            # как items без expiry.
            for user_id, notes in conn.execute(
                "SELECT user_id, notes FROM user_memory WHERE notes != ''"
            ).fetchall():
                has_items = conn.execute(
                    "SELECT 1 FROM user_memory_items WHERE user_id = ? LIMIT 1",
                    (user_id,),
                ).fetchone()
                if has_items:
                    continue
                seen: set[str] = set()
                for raw_line in notes.split("\n"):
                    line = raw_line.strip()
                    if not line:
                        continue
                    key = line.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    conn.execute(
                        "INSERT INTO user_memory_items (user_id, content) VALUES (?, ?)",
                        (user_id, line),
                    )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_summaries_sent (
                    user_id INTEGER NOT NULL,
                    summary_date TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, summary_date)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS weekly_plans (
                    user_id INTEGER NOT NULL,
                    week_start TEXT NOT NULL,
                    plan_text TEXT NOT NULL,
                    week_type TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, week_start)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_feelings (
                    user_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    note TEXT,
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, day)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS training_goal (
                    user_id INTEGER PRIMARY KEY,
                    goal_text TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS races (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    race_date TEXT NOT NULL,
                    name TEXT NOT NULL,
                    distance_km REAL,
                    goal_time TEXT,
                    notes TEXT,
                    is_priority INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            races_cols = {r[1] for r in conn.execute("PRAGMA table_info(races)").fetchall()}
            if "is_priority" not in races_cols:
                conn.execute("ALTER TABLE races ADD COLUMN is_priority INTEGER NOT NULL DEFAULT 0")
            if "actual_time" not in races_cols:
                # Фактический результат гонки (e.g. "49:52" или "3:31:55"). Заполняется
                # после старта через /race result. Структурное место для результата,
                # чтобы он не дублировался в user_memory и не приводил к переспросу.
                conn.execute("ALTER TABLE races ADD COLUMN actual_time TEXT")
            if "actual_notes" not in races_cols:
                conn.execute("ALTER TABLE races ADD COLUMN actual_notes TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_profile_overrides (
                    user_id INTEGER PRIMARY KEY,
                    lthr REAL,
                    weight_kg REAL,
                    timezone TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Migrations: add columns if they don't exist yet
            existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(user_profile_overrides)").fetchall()}
            if "timezone" not in existing_cols:
                conn.execute("ALTER TABLE user_profile_overrides ADD COLUMN timezone TEXT")
            if "running_experience_years" not in existing_cols:
                conn.execute("ALTER TABLE user_profile_overrides ADD COLUMN running_experience_years REAL")
            if "gender" not in existing_cols:
                conn.execute("ALTER TABLE user_profile_overrides ADD COLUMN gender TEXT")
            if "available_days" not in existing_cols:
                conn.execute("ALTER TABLE user_profile_overrides ADD COLUMN available_days TEXT")
            if "max_session_min_weekday" not in existing_cols:
                conn.execute("ALTER TABLE user_profile_overrides ADD COLUMN max_session_min_weekday INTEGER")
            if "max_session_min_weekend" not in existing_cols:
                conn.execute("ALTER TABLE user_profile_overrides ADD COLUMN max_session_min_weekend INTEGER")
            if "injuries" not in existing_cols:
                conn.execute("ALTER TABLE user_profile_overrides ADD COLUMN injuries TEXT")
            if "profile_completed" not in existing_cols:
                conn.execute("ALTER TABLE user_profile_overrides ADD COLUMN profile_completed INTEGER DEFAULT 0")
            if "location_name" not in existing_cols:
                conn.execute("ALTER TABLE user_profile_overrides ADD COLUMN location_name TEXT")
            if "location_lat" not in existing_cols:
                conn.execute("ALTER TABLE user_profile_overrides ADD COLUMN location_lat REAL")
            if "location_lon" not in existing_cols:
                conn.execute("ALTER TABLE user_profile_overrides ADD COLUMN location_lon REAL")
            if "age" not in existing_cols:
                conn.execute("ALTER TABLE user_profile_overrides ADD COLUMN age INTEGER")
            if "weekly_km_target" not in existing_cols:
                conn.execute("ALTER TABLE user_profile_overrides ADD COLUMN weekly_km_target REAL")

            # User-verified facts: всё, что атлет явно «утвердил» в чате
            # («это правильно X», «вчера было 56 км, не 66»). Бот в системном
            # промпте отдаёт этот блок как «источник истины — не оспаривай».
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS verified_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    fact_date TEXT NOT NULL,
                    fact_text TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    is_active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_facts_user_date "
                "ON verified_facts(user_id, fact_date, is_active)"
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS food_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    entry_date TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    description TEXT NOT NULL,
                    calories REAL NOT NULL,
                    protein_g REAL NOT NULL,
                    fat_g REAL NOT NULL,
                    carbs_g REAL NOT NULL,
                    confidence TEXT NOT NULL DEFAULT 'medium',
                    source TEXT NOT NULL DEFAULT 'text',
                    raw_response TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_food_user_date "
                "ON food_entries(user_id, entry_date)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    method TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_token_usage_user_at "
                "ON token_usage(user_id, created_at)"
            )

    def log_token_usage(
        self, user_id: int | None, method: str, model: str,
        input_tokens: int, output_tokens: int,
        cache_read_tokens: int = 0, cache_write_tokens: int = 0,
    ) -> None:
        """Записать расход токенов одного вызова Claude (атрибуция стоимости по юзерам)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO token_usage (user_id, method, model, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, method, model, input_tokens, output_tokens,
                 cache_read_tokens, cache_write_tokens, now_iso),
            )

    def get_token_usage_stats(self, days: int = 30) -> list[dict]:
        """Суммарный расход токенов по юзерам за N дней (для /admin_stats)."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, COUNT(*) AS calls,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(cache_read_tokens) AS cache_read_tokens,
                       SUM(cache_write_tokens) AS cache_write_tokens
                FROM token_usage
                WHERE created_at >= ?
                GROUP BY user_id
                ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
                """,
                (since,),
            ).fetchall()
        cols = ("user_id", "calls", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens")
        return [dict(zip(cols, r)) for r in rows]

    def save_profile_override(
        self, user_id: int, lthr: float | None = None, weight_kg: float | None = None,
        timezone: str | None = None, running_experience_years: float | None = None,
        gender: str | None = None, available_days: str | None = None,
        max_session_min_weekday: int | None = None, max_session_min_weekend: int | None = None,
        injuries: str | None = None, profile_completed: int | None = None,
        location_name: str | None = None, location_lat: float | None = None,
        location_lon: float | None = None,
        age: int | None = None, weekly_km_target: float | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_profile_overrides (
                    user_id, lthr, weight_kg, timezone, running_experience_years,
                    gender, available_days, max_session_min_weekday, max_session_min_weekend, injuries,
                    profile_completed, location_name, location_lat, location_lon,
                    age, weekly_km_target, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    lthr = COALESCE(excluded.lthr, lthr),
                    weight_kg = COALESCE(excluded.weight_kg, weight_kg),
                    timezone = COALESCE(excluded.timezone, timezone),
                    running_experience_years = COALESCE(excluded.running_experience_years, running_experience_years),
                    gender = COALESCE(excluded.gender, gender),
                    available_days = COALESCE(excluded.available_days, available_days),
                    max_session_min_weekday = COALESCE(excluded.max_session_min_weekday, max_session_min_weekday),
                    max_session_min_weekend = COALESCE(excluded.max_session_min_weekend, max_session_min_weekend),
                    injuries = COALESCE(excluded.injuries, injuries),
                    profile_completed = COALESCE(excluded.profile_completed, profile_completed),
                    location_name = COALESCE(excluded.location_name, location_name),
                    location_lat = COALESCE(excluded.location_lat, location_lat),
                    location_lon = COALESCE(excluded.location_lon, location_lon),
                    age = COALESCE(excluded.age, age),
                    weekly_km_target = COALESCE(excluded.weekly_km_target, weekly_km_target),
                    updated_at = excluded.updated_at
                """,
                (user_id, lthr, weight_kg, timezone, running_experience_years,
                 gender, available_days, max_session_min_weekday, max_session_min_weekend, injuries,
                 profile_completed, location_name, location_lat, location_lon,
                 age, weekly_km_target),
            )

    def get_profile_override(self, user_id: int) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT lthr, weight_kg, timezone, running_experience_years,"
                " gender, available_days, max_session_min_weekday, max_session_min_weekend,"
                " injuries, profile_completed, location_name, location_lat, location_lon,"
                " age, weekly_km_target"
                " FROM user_profile_overrides WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return {}
        return {
            "lthr": row[0], "weight_kg": row[1], "timezone": row[2],
            "running_experience_years": row[3],
            "gender": row[4], "available_days": row[5],
            "max_session_min_weekday": row[6], "max_session_min_weekend": row[7],
            "injuries": row[8], "profile_completed": row[9],
            "location_name": row[10], "location_lat": row[11], "location_lon": row[12],
            "age": row[13], "weekly_km_target": row[14],
        }

    def upsert_credentials(self, user_id: int, username: str, password_encrypted: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO garmin_credentials (user_id, username, password_encrypted)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    password_encrypted=excluded.password_encrypted,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (user_id, username, password_encrypted),
            )

    def get_credentials(self, user_id: int) -> GarminCredentials | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, username, password_encrypted FROM garmin_credentials WHERE user_id = ?",
                (user_id,),
            ).fetchone()

        if not row:
            return None

        return GarminCredentials(user_id=row[0], username=row[1], password_encrypted=row[2])

    def issue_web_token(self, user_id: int, ttl_seconds: int = 900) -> str:
        token = secrets.token_urlsafe(24)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        expires_iso = expires_at.isoformat()

        with self._connect() as conn:
            conn.execute(
                "DELETE FROM web_tokens WHERE user_id = ?",
                (user_id,),
            )
            conn.execute(
                "INSERT INTO web_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
                (token, user_id, expires_iso),
            )
        return token

    def consume_web_token(self, token: str) -> int | None:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            # Атомарно: один DELETE удаляет валидный токен и возвращает user_id.
            # Исключает double-spend (race между SELECT и DELETE).
            row = conn.execute(
                "DELETE FROM web_tokens WHERE token = ? AND expires_at >= ? "
                "RETURNING user_id",
                (token, now.isoformat()),
            ).fetchone()
            # Заодно подчищаем протухшие токены.
            conn.execute(
                "DELETE FROM web_tokens WHERE expires_at < ?",
                (now.isoformat(),),
            )
        return int(row[0]) if row else None

    def track_event(self, user_id: int, event_name: str) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO usage_events (user_id, event_name, event_at) VALUES (?, ?, ?)",
                (user_id, event_name, now_iso),
            )

    def add_message(self, user_id: int, role: str, content: str, source: str, keep_last: int = 60) -> None:
        """Save a conversation message and trim old ones beyond keep_last.

        `source` обязателен — допустимые значения: 'morning', 'workout', 'qa',
        'plan_tweak'. Без явного source легко тихо засорить QA-историю.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversation_messages (user_id, role, content, source, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, role, content, source, now_iso),
            )
            # Trim: keep only the most recent keep_last messages
            conn.execute(
                """
                DELETE FROM conversation_messages
                WHERE user_id = ? AND id NOT IN (
                    SELECT id FROM conversation_messages
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                """,
                (user_id, user_id, keep_last),
            )

    def get_history(
        self,
        user_id: int,
        limit: int = 20,
        sources: list[str] | tuple[str, ...] | str | None = None,
        max_chars_per_msg: int = 1200,
    ) -> list[dict]:
        """Return the last `limit` messages as [{'role': ..., 'content': ...}].

        `sources` — фильтр по типам источников:
          • None — все сообщения
          • str  — один источник (обратная совместимость)
          • list/tuple — несколько источников (e.g. ('morning','qa'))

        `max_chars_per_msg` — соft-cap на длину каждого сообщения при чтении.
        Длинные ассистент-сообщения (планы, разборы тренировок) обрезаются
        с пометкой «[…сокращено]», чтобы они не раздували каждый последующий
        LLM-вызов. Полный текст в БД сохраняется как есть.
        """
        if isinstance(sources, str):
            sources = (sources,)
        with self._connect() as conn:
            if not sources:
                rows = conn.execute(
                    """
                    SELECT role, content FROM (
                        SELECT id, role, content FROM conversation_messages
                        WHERE user_id = ?
                        ORDER BY id DESC
                        LIMIT ?
                    ) ORDER BY id ASC
                    """,
                    (user_id, limit),
                ).fetchall()
            else:
                placeholders = ",".join("?" * len(sources))
                rows = conn.execute(
                    f"""
                    SELECT role, content FROM (
                        SELECT id, role, content FROM conversation_messages
                        WHERE user_id = ? AND source IN ({placeholders})
                        ORDER BY id DESC
                        LIMIT ?
                    ) ORDER BY id ASC
                    """,
                    (user_id, *sources, limit),
                ).fetchall()
        out: list[dict] = []
        for role, content in rows:
            if max_chars_per_msg and len(content) > max_chars_per_msg:
                # Берём начало (это обычно суть/вердикт), хвост обрезаем.
                content = content[:max_chars_per_msg].rstrip() + " […сокращено]"
            out.append({"role": role, "content": content})
        return out

    # ---------- user memory: per-item модель ----------

    def list_memory_items(self, user_id: int) -> list[dict]:
        """Активные (не удалённые, не протухшие) заметки в порядке создания."""
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, content, created_at, expires_at
                FROM user_memory_items
                WHERE user_id = ? AND is_active = 1
                  AND (expires_at IS NULL OR expires_at >= ?)
                ORDER BY id ASC
                """,
                (user_id, today),
            ).fetchall()
        return [
            {"id": r[0], "content": r[1], "created_at": r[2], "expires_at": r[3]}
            for r in rows
        ]

    def add_memory_item(
        self, user_id: int, content: str, expires_at: str | None = None
    ) -> int | None:
        """Добавить заметку с дедупом (case-insensitive substring).

        Возвращает id новой записи или None если поглощена дубликатом.
        """
        content = content.strip()
        if not content:
            return None
        norm = content.lower()
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT id, content FROM user_memory_items
                WHERE user_id = ? AND is_active = 1
                """,
                (user_id,),
            ).fetchall()
            for ex_id, ex_content in existing:
                ex_norm = ex_content.lower()
                # точное совпадение / новый — подстрока существующего → пропускаем
                if norm == ex_norm or norm in ex_norm:
                    return None
                # новый — supersedes (содержит существующее) → деактивируем старое
                if ex_norm in norm:
                    conn.execute(
                        "UPDATE user_memory_items SET is_active = 0 WHERE id = ?",
                        (ex_id,),
                    )
            cur = conn.execute(
                """
                INSERT INTO user_memory_items (user_id, content, expires_at)
                VALUES (?, ?, ?)
                """,
                (user_id, content, expires_at),
            )
            return int(cur.lastrowid)

    def delete_memory_item(self, user_id: int, item_id: int) -> bool:
        """Деактивирует одну заметку. Возвращает True если найдена и удалена."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE user_memory_items SET is_active = 0
                WHERE id = ? AND user_id = ? AND is_active = 1
                """,
                (item_id, user_id),
            )
            return cur.rowcount > 0

    def clear_user_memory(self, user_id: int) -> int:
        """Деактивирует все активные заметки. Возвращает количество удалённых."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE user_memory_items SET is_active = 0
                WHERE user_id = ? AND is_active = 1
                """,
                (user_id,),
            )
            # Чистим и старое поле notes, чтобы не воскресло при ре-миграции
            conn.execute(
                "UPDATE user_memory SET notes = '', updated_at = CURRENT_TIMESTAMP "
                "WHERE user_id = ?",
                (user_id,),
            )
            return cur.rowcount

    def get_user_memory(self, user_id: int) -> str:
        """Склеивает активные items в \\n-разделённую строку для системного промпта.

        Формат строк: «#N. текст» — id нужен, чтобы Claude мог вызывать
        forget_note(item_id=N), когда юзер говорит «забудь это».
        """
        items = self.list_memory_items(user_id)
        return "\n".join(f"#{it['id']}. {it['content']}" for it in items)

    def get_plan(self, user_id: int, week_start: str) -> tuple[str, str] | None:
        """Return (plan_text, generated_at_iso) or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT plan_text, generated_at FROM weekly_plans WHERE user_id = ? AND week_start = ?",
                (user_id, week_start),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def get_plan_meta(self, user_id: int, week_start: str) -> dict | None:
        """Возвращает {plan_text, week_type, generated_at} или None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT plan_text, week_type, generated_at "
                "FROM weekly_plans WHERE user_id = ? AND week_start = ?",
                (user_id, week_start),
            ).fetchone()
        if not row:
            return None
        return {"plan_text": row[0], "week_type": row[1], "generated_at": row[2]}

    def save_plan(self, user_id: int, week_start: str, plan_text: str, week_type: str) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO weekly_plans (user_id, week_start, plan_text, week_type, generated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, week_start) DO UPDATE SET
                    plan_text=excluded.plan_text,
                    week_type=excluded.week_type,
                    generated_at=excluded.generated_at
                """,
                (user_id, week_start, plan_text, week_type, now_iso),
            )

    def clear_plan(self, user_id: int, week_start: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM weekly_plans WHERE user_id = ? AND week_start = ?",
                (user_id, week_start),
            )

    def save_goal(self, user_id: int, goal_text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO training_goal (user_id, goal_text)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET goal_text=excluded.goal_text,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (user_id, goal_text),
            )

    def get_goal(self, user_id: int) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT goal_text FROM training_goal WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row[0] if row else ""

    def save_feeling(self, user_id: int, day: str, score: int, note: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_feelings (user_id, day, score, note)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, day) DO UPDATE SET score=excluded.score, note=excluded.note,
                    recorded_at=CURRENT_TIMESTAMP
                """,
                (user_id, day, score, note or None),
            )

    def get_feelings(self, user_id: int, since_day: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT day, score, note FROM daily_feelings WHERE user_id = ? AND day >= ? ORDER BY day",
                (user_id, since_day),
            ).fetchall()
        return [{"day": r[0], "score": r[1], "note": r[2]} for r in rows]

    def get_feelings_stats(self, user_id: int, days: int = 14) -> dict:
        """Return feelings statistics for the given period."""
        since = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
        feelings = self.get_feelings(user_id, since)
        if not feelings:
            return {"count": 0, "avg": 0, "scores": [], "trend": ""}
        scores = [f["score"] for f in feelings]
        avg = sum(scores) / len(scores)
        # Trend: compare first half vs second half
        mid = len(scores) // 2
        if mid >= 2:
            first_half = sum(scores[:mid]) / mid
            second_half = sum(scores[mid:]) / len(scores[mid:])
            diff = second_half - first_half
            if diff > 0.3:
                trend = "improving"
            elif diff < -0.3:
                trend = "declining"
            else:
                trend = "stable"
        else:
            trend = "insufficient"
        return {
            "count": len(feelings),
            "avg": round(avg, 1),
            "scores": feelings,
            "trend": trend,
        }

    def save_race(self, user_id: int, race_date: str, name: str,
                  distance_km: float | None = None, goal_time: str | None = None,
                  notes: str | None = None) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO races (user_id, race_date, name, distance_km, goal_time, notes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, race_date, name, distance_km, goal_time, notes),
            )
            return cur.lastrowid

    def get_races(self, user_id: int, from_date: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if from_date:
                rows = conn.execute(
                    "SELECT id, race_date, name, distance_km, goal_time, notes, is_priority, "
                    "actual_time, actual_notes "
                    "FROM races WHERE user_id = ? AND race_date >= ? ORDER BY race_date",
                    (user_id, from_date),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, race_date, name, distance_km, goal_time, notes, is_priority, "
                    "actual_time, actual_notes "
                    "FROM races WHERE user_id = ? ORDER BY race_date",
                    (user_id,),
                ).fetchall()
        return [{"id": r[0], "date": r[1], "name": r[2], "distance_km": r[3],
                 "goal_time": r[4], "notes": r[5], "is_priority": bool(r[6]),
                 "actual_time": r[7], "actual_notes": r[8]}
                for r in rows]

    # ---------- verified facts (источник истины от атлета) ----------

    def add_verified_fact(self, user_id: int, fact_date: str, fact_text: str) -> int:
        """Сохраняет утверждённый юзером факт за дату. Возвращает id.

        Идентичный активный факт за ту же дату не дублируется (Claude иногда
        вызывает confirm_fact дважды за один ответ) — возвращается существующий id.
        """
        fact_text = (fact_text or "").strip()
        if not fact_text:
            return 0
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM verified_facts "
                "WHERE user_id = ? AND fact_date = ? AND fact_text = ? AND is_active = 1",
                (user_id, fact_date, fact_text),
            ).fetchone()
            if row:
                return int(row[0])
            cur = conn.execute(
                "INSERT INTO verified_facts (user_id, fact_date, fact_text) VALUES (?, ?, ?)",
                (user_id, fact_date, fact_text),
            )
            return int(cur.lastrowid)

    def list_verified_facts(
        self, user_id: int, since_date: str | None = None
    ) -> list[dict]:
        with self._connect() as conn:
            if since_date:
                rows = conn.execute(
                    "SELECT id, fact_date, fact_text, created_at "
                    "FROM verified_facts WHERE user_id = ? AND is_active = 1 "
                    "AND fact_date >= ? ORDER BY fact_date DESC, id DESC",
                    (user_id, since_date),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, fact_date, fact_text, created_at "
                    "FROM verified_facts WHERE user_id = ? AND is_active = 1 "
                    "ORDER BY fact_date DESC, id DESC",
                    (user_id,),
                ).fetchall()
        return [
            {"id": r[0], "fact_date": r[1], "fact_text": r[2], "created_at": r[3]}
            for r in rows
        ]

    def set_race_result(
        self, user_id: int, race_id: int, actual_time: str | None,
        actual_notes: str | None = None,
    ) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE races SET actual_time = ?, actual_notes = COALESCE(?, actual_notes) "
                "WHERE id = ? AND user_id = ?",
                (actual_time, actual_notes, race_id, user_id),
            )
            return cur.rowcount > 0

    def delete_race(self, user_id: int, race_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM races WHERE id = ? AND user_id = ?", (race_id, user_id)
            )
            return cur.rowcount > 0

    def set_race_priority(self, user_id: int, race_id: int, is_priority: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE races SET is_priority = ? WHERE id = ? AND user_id = ?",
                (1 if is_priority else 0, race_id, user_id),
            )
            return cur.rowcount > 0

    # ── Food entries ────────────────────────────────────────────────────────

    def save_food_entry(
        self,
        user_id: int,
        entry_date: str,
        entry_time: str,
        description: str,
        calories: float,
        protein_g: float,
        fat_g: float,
        carbs_g: float,
        confidence: str = "medium",
        source: str = "text",
        raw_response: str | None = None,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO food_entries "
                "(user_id, entry_date, entry_time, description, calories, protein_g, fat_g, carbs_g, "
                "confidence, source, raw_response) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, entry_date, entry_time, description, calories,
                 protein_g, fat_g, carbs_g, confidence, source, raw_response),
            )
            return cur.lastrowid

    def get_food_entries(self, user_id: int, entry_date: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, entry_date, entry_time, description, calories, "
                "protein_g, fat_g, carbs_g, confidence, source "
                "FROM food_entries WHERE user_id = ? AND entry_date = ? "
                "ORDER BY entry_time",
                (user_id, entry_date),
            ).fetchall()
        return [
            {"id": r[0], "date": r[1], "time": r[2], "description": r[3],
             "calories": r[4], "protein_g": r[5], "fat_g": r[6], "carbs_g": r[7],
             "confidence": r[8], "source": r[9]}
            for r in rows
        ]

    def delete_food_entry(self, user_id: int, entry_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM food_entries WHERE id = ? AND user_id = ?",
                (entry_id, user_id),
            )
            return cur.rowcount > 0

    def get_food_entry(self, user_id: int, entry_id: int) -> dict | None:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT id, entry_date, entry_time, description, calories, "
                "protein_g, fat_g, carbs_g, confidence, source "
                "FROM food_entries WHERE id = ? AND user_id = ?",
                (entry_id, user_id),
            ).fetchone()
        if not r:
            return None
        return {
            "id": r[0], "date": r[1], "time": r[2], "description": r[3],
            "calories": r[4], "protein_g": r[5], "fat_g": r[6], "carbs_g": r[7],
            "confidence": r[8], "source": r[9],
        }

    def update_food_entry(
        self,
        user_id: int,
        entry_id: int,
        *,
        entry_date: str | None = None,
        description: str | None = None,
        calories: float | None = None,
        protein_g: float | None = None,
        fat_g: float | None = None,
        carbs_g: float | None = None,
        confidence: str | None = None,
    ) -> bool:
        fields: list[str] = []
        values: list = []
        for col, val in (
            ("entry_date", entry_date),
            ("description", description),
            ("calories", calories),
            ("protein_g", protein_g),
            ("fat_g", fat_g),
            ("carbs_g", carbs_g),
            ("confidence", confidence),
        ):
            if val is not None:
                fields.append(f"{col} = ?")
                values.append(val)
        if not fields:
            return False
        values.extend([entry_id, user_id])
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE food_entries SET {', '.join(fields)} "
                "WHERE id = ? AND user_id = ?",
                values,
            )
            return cur.rowcount > 0

    def get_all_credential_user_ids(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT user_id FROM garmin_credentials").fetchall()
        return [row[0] for row in rows]

    def get_usage_stats(self) -> UsageStats:
        today = datetime.now(timezone.utc).date()
        today_s = today.isoformat()
        week_s = (today - timedelta(days=6)).isoformat()
        month_s = (today - timedelta(days=29)).isoformat()

        with self._connect() as conn:
            total_users = self._count_distinct_users(conn, "1900-01-01", today_s)
            dau = self._count_distinct_users(conn, today_s, today_s)
            wau = self._count_distinct_users(conn, week_s, today_s)
            mau = self._count_distinct_users(conn, month_s, today_s)

            retention_d1, cohort_d1_size = self._retention_exact_day(conn, day_n=1, today=today)
            retention_d7, cohort_d7_size = self._retention_exact_day(conn, day_n=7, today=today)
            retention_d30, cohort_d30_size = self._retention_exact_day(conn, day_n=30, today=today)

        return UsageStats(
            total_users=total_users,
            dau=dau,
            wau=wau,
            mau=mau,
            retention_d1=retention_d1,
            retention_d7=retention_d7,
            retention_d30=retention_d30,
            cohort_d1_size=cohort_d1_size,
            cohort_d7_size=cohort_d7_size,
            cohort_d30_size=cohort_d30_size,
        )

    def _count_distinct_users(self, conn: sqlite3.Connection, from_date: str, to_date: str) -> int:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id)
            FROM usage_events
            WHERE DATE(event_at) BETWEEN ? AND ?
            """,
            (from_date, to_date),
        ).fetchone()
        return int(row[0] or 0)

    def _retention_exact_day(self, conn: sqlite3.Connection, day_n: int, today: datetime.date) -> tuple[float | None, int]:
        cohort_date = (today - timedelta(days=day_n)).isoformat()
        today_s = today.isoformat()

        cohort_row = conn.execute(
            """
            WITH first_seen AS (
                SELECT user_id, MIN(DATE(event_at)) AS first_date
                FROM usage_events
                GROUP BY user_id
            )
            SELECT COUNT(*)
            FROM first_seen
            WHERE first_date = ?
            """,
            (cohort_date,),
        ).fetchone()
        cohort_size = int(cohort_row[0] or 0)
        if cohort_size == 0:
            return (None, 0)

        returned_row = conn.execute(
            """
            WITH first_seen AS (
                SELECT user_id, MIN(DATE(event_at)) AS first_date
                FROM usage_events
                GROUP BY user_id
            )
            SELECT COUNT(DISTINCT e.user_id)
            FROM usage_events e
            JOIN first_seen f ON f.user_id = e.user_id
            WHERE f.first_date = ?
              AND DATE(e.event_at) = ?
            """,
            (cohort_date, today_s),
        ).fetchone()
        returned = int(returned_row[0] or 0)
        return (round((returned / cohort_size) * 100.0, 2), cohort_size)
