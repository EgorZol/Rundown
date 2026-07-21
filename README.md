# Garmin Running Coach Bot

Telegram-бот **«персональный AI-тренер по бегу»**. Несмотря на имя репозитория `garmin-analysis-and-backup`, главная функция — анализ данных Garmin, недельные планы, Q&A и учёт питания. Локальный бэкап SQLite — побочная инфраструктура.

**Версия:** 0.8.0 (см. `CHANGELOG.md`)  
**Ветка:** `main`  
**Правила для AI-агентов и разработки:** [`CLAUDE.md`](./CLAUDE.md) — актуальный playbook; этот README — обзор для людей.

---

## TL;DR

| | |
|---|---|
| **Бот** | long-polling, systemd user service из `.venv` |
| **WebApp** | FastAPI `:8085` — форма Garmin credentials (`https://aiaptechka.ru/garmin`) |
| **AI** | Claude (анализ / план / Q&A / Vision для еды), OpenAI Whisper (голос) |
| **Garmin** | `garth` → JSON API (I/O-bound, 5–10 сек), **не** GarminDB CLI |
| **Данные** | `data/app.db` + `data/users/{tg_id}/DBs/garmin*.db` |
| **Dev** | `@Rundown_dev_bot`, `garmin-dev-bot.service`, данные в `data-dev/` |

---

## Возможности

- **🌅 Утро** — синк health + activities, recovery-вердикт (код), утренний бриф (Claude)
- **🏃 Разбор** — анализ последней тренировки (зоны, TE, сплиты, cardiac drift)
- **📅 План** — недельный план с фазами recovery / base / build / peak / taper
- **🏅 Форма / 📈 Прогресс / 📋 Итоги / 🏆 Рекорды** — сводки по метрикам
- **🍽 Еда / 📊 Питание / 🔥 Калории** — фото, голос, текст; ISSN-нормы по типу дня
- **🎯 Цель / 🏁 Старты** — цель + календарь гонок (A-гонка, приоритет, результаты)
- **Q&A** — свободный текст с SQL-tools по трём БД + write-tools (факты, заметки, гонки…)
- **Подписки** — тарифы «Тренер» / «Калории», триал 7 дней, ЮKassa (если задан токен)

---

## Структура

```
/home/evg/garmin-analysis-and-backup/
├── README.md / CLAUDE.md / CHANGELOG.md
├── pyproject.toml / requirements.txt / .env.example
├── Dockerfile.webapp              # только webapp (uvicorn :8085)
├── deploy/systemd/                # unit-файлы бота, webapp, бэкапа, dev-бота
├── docs/garth-migration-spike.md  # история ухода с GarminDB
├── scripts/
│   ├── backup_app_db.sh           # ежедневный WAL-safe бэкап
│   ├── broadcast.py / clean_user_memory.py / run_evals.py …
├── src/garmin_backup_bot/
│   ├── main.py              # сборка компонентов, polling
│   ├── bot.py               # фасад GarminBot, регистрация хендлеров
│   ├── bot_common.py        # кнопки BTN_*, MAIN_KEYBOARD
│   ├── bot_food.py          # еда
│   ├── bot_reports.py       # утро / разбор / план / форма / прогресс / итоги
│   ├── bot_qa.py            # Q&A + write-tools
│   ├── bot_races.py         # цель, старты, самочувствие
│   ├── bot_profile.py       # анкета, TZ, сброс
│   ├── bot_memory.py        # заметки
│   ├── bot_jobs.py          # напоминания, алерты, /admin_stats
│   ├── bot_payments.py      # тарифы, триал, пейволл, ЮKassa
│   ├── coach.py             # детерминированные пороги и вердикты (без LLM)
│   ├── analyst.py           # Claude: tool-цикл, analyze*/ask
│   ├── formatting.py        # блоки контекста для промптов
│   ├── prompts.py           # системные промпты
│   ├── tools.py             # SQL-раннер (ro) + схемы tools
│   ├── plan_builder.py      # генерация планов, макроцикл
│   ├── garmin_service.py    # фасад
│   ├── garmin_sync.py       # garth → SQLite
│   ├── garmin_metrics.py    # чтение метрик
│   ├── storage.py           # app.db CRUD, схема
│   ├── nutrition.py         # Vision + ISSN
│   ├── webapp.py / transcription.py / config.py / crypto.py
├── tests/                   # unittest (coach, storage, tools, plan, …)
├── data/
│   ├── app.db               # общая БД бота
│   ├── users/{tg_id}/       # per-user Garmin + JSON (splits/laps/HRV/…)
│   │   └── DBs/             # реальные garmin.db / garmin_activities.db
│   ├── backups/             # ежедневные бэкапы (см. backups/README-restore.md)
│   └── archive/             # старые monitoring.db после миграции
└── data-dev/                # изоляция dev-бота
```

---

## Стек

| Слой | Технология |
|------|------------|
| Python | 3.12 (≥3.10) |
| Бот | `python-telegram-bot==21.10` (polling) |
| WebApp | FastAPI + uvicorn |
| AI | Anthropic Claude (основная + fallback-модели из `.env`) |
| Голос | OpenAI Whisper (опционально) |
| Garmin | **`garth`** (OAuth-сессия, JSON API) |
| БД | SQLite, WAL |
| Секреты | Fernet (`cryptography`) для Garmin-паролей |
| Планировщик | APScheduler (периодические тики в боте) |
| Деплой | systemd `--user` (бот, бэкап); Docker/nginx (webapp) |

Зависимости: `requirements.txt`. Устаревший **GarminDB CLI для синка больше не используется** (остатки схем/полей в SQLite совместимы с историческими данными).

---

## Архитектура (коротко)

```
main.py
  ├─ Storage(app.db)
  ├─ GarminService = GarminSyncMixin + GarminMetricsMixin
  ├─ HealthAnalyst  → Claude + SQL tools
  ├─ WeeklyPlanBuilder
  ├─ NutritionAnalyzer
  ├─ Transcriber?
  └─ GarminBot = Food + Reports + QA + Races + Profile + Memory + Jobs + Payments
```

**Принцип:** расчёты и пороги — в коде (`coach.py`), язык и тактика — у Claude.  
**Синк:** только в «Утро» и «Спорт» (и онбординг). Глобальный семафор ≤5; первичный синк — строго 1. На каждый вызов — изолированный `garth.Client` (без гонки сессий).

Подробности, анти-паттерны и чеклист после правок — в [`CLAUDE.md`](./CLAUDE.md).

---

## ENV

Шаблон: `.env.example`. Секреты — только в `.env` / `.env.dev` (не в git).

| Переменная | Назначение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен бота |
| `ENCRYPTION_KEY` | Fernet key. **Потеря = Garmin-пароли нерасшифруемы** |
| `DB_PATH` | `./data/app.db` |
| `EXPORTS_DIR` | `./exports` |
| `GARMIN_WORKDIR_ROOT` | `./data/users` |
| `WEBAPP_BASE_URL` | URL формы (для one-shot токенов) |
| `WEBAPP_TOKEN_TTL_SECONDS` | TTL ссылки (по умолчанию 900) |
| `ADMIN_USER_IDS` | Telegram user_id через запятую |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` / `ANTHROPIC_MODEL_FALLBACKS` | Claude |
| `OPENAI_API_KEY` | Whisper (опционально) |
| `USER_TIMEZONE` | дефолт TZ (например `Europe/Moscow`) |
| `GARMIN_DB_TIMEZONE` | TZ naive-дат в Garmin SQLite |
| `USER_AGE` / `WEEKLY_KM_TARGET` | дефолты профиля |
| `PAYMENT_PROVIDER_TOKEN` | ЮKassa через BotFather; пусто = «скоро» |

`GARMIN_DB_SYNC_CMD` в старых `.env` можно удалить — игнорируется.

Dev-бот: `.env` + `.env.dev` (перекрывает токен и пути данных).

---

## Данные

### `data/app.db` (источник схемы — `storage.py:_init_schema()`)

Основные таблицы:

| Таблица | Назначение |
|---|---|
| `garmin_credentials` | login + Fernet(password) |
| `web_tokens` | one-shot токены WebApp |
| `conversation_messages` | история диалогов |
| `weekly_plans` | сгенерированные планы |
| `training_goal` / `races` | цель и старты (`is_priority` = A-гонка) |
| `user_profile_overrides` | вес, LTHR, TZ, дни бега, … |
| `user_memory` / `user_memory_items` | долговременные заметки |
| `verified_facts` | факты, подтверждённые атлетом |
| `food_entries` | еда |
| `daily_feelings` | самочувствие |
| `subscriptions` / `payments` | тарифы и платежи |
| `plan_preferences` | постоянные пожелания к планам |
| `safety_overrides` | осознанное снятие hard-safety на неделю |
| `token_usage` | учёт токенов Claude |
| `usage_events` / `nudge_log` | аналитика и троттлинг подсказок |

### Per-user Garmin (`data/users/{tg_id}/`)

| Путь | Содержимое |
|---|---|
| `DBs/garmin.db` | sleep, resting_hr, daily_summary, weight, … |
| `DBs/garmin_activities.db` | activities, laps, splits, steps_activities, … |
| `config/garth_session` | OAuth-сессия garth |
| `splits/`, `laps/`, `HRV/`, `Sleep/`, … | JSON-кэш деталей |
| `CoachData/` | служебные метрики тренера |

⚠️ Stub-файлы `garmin.db` в корне юзера могут быть пустыми — **реальные данные в `DBs/`**.

### Бэкапы

- Timer `garmin-backup-db.timer` → `scripts/backup_app_db.sh` (~03:00)
- `data/backups/app-YYYY-MM-DD.db` + `users/…`, offsite rclone (см. `data/backups/README-restore.md`)

---

## Бот: команды и UI

### Команды

`/start`, `/help`, `/link_garmin`, `/status`, `/remember`, `/memory`, `/forget`, `/plan`, `/feeling`, `/goal`, `/race`, `/profile_reset`, `/admin_stats`, `/paysupport` (и связанные с оплатой).

### Reply-клавиатура (`MAIN_KEYBOARD`)

```
🌅 Утро     🏃 Разбор     🏅 Форма
📅 План     📈 Прогресс   📋 Итоги
🎯 Моя цель 🏁 Старты     🔥 Калории
🍽 Еда      📊 Питание    🏆 Рекорды
📊 Статус   📋 Профиль    🕐 Часы
```

Вес, LTHR, дни бега, заметки — через естественный язык / профиль / write-tools (отдельные кнопки убраны).

### Медиа

- **Фото** → распознавание еды (Claude Vision)
- **Голос** → Whisper → тот же пайплайн, что текст
- **WebApp data** → сохранение Garmin credentials
- **Текст** → Q&A (`handle_question`) с tool-use

### WebApp (`webapp.py`)

- `GET /healthz`
- `GET /connect?token=…` — HTML-форма
- `POST /submit` — encrypt + upsert credentials (rate-limit, лимит тела)

---

## Тренерская логика (важно)

### HR-зоны Garmin (5-zone)

**Z3 = аэробная, основная зона лёгкого бега** (не Z2).  
Z1 warmup → Z2 light → **Z3 aerobic** → Z4 threshold → Z5 anaerobic.

### Макроцикл (`plan_builder.py`)

Окна фаз масштабируются по дистанции A-гонки:

| Дистанция | taper | peak | build |
|---|---:|---:|---:|
| <10К | 5 | 14 | 28 |
| 10К | 7 | 21 | 42 |
| 21К | 10 | 24 | 49 |
| 42–50К | 14 | 28 | 56 |
| 80К+ | 21 | 42 | 84 |

Выбор A-гонки: `is_priority` → ближайшая non-tune-up → каскад на более длинную в 14 днях.  
Hard-safety (RHR-spike, низкий BB, HRV, ACWR, TSB) **override** race-логику; атлет может осознанно снять ограничение на неделю (`safety_overrides`).

### Питание

ISSN-нормы периодизированы по 6 типам дня: rest → easy → steady → threshold → long → race.  
Тип дня — из строки плана; fallback — `calories_active` из Garmin.

### 80/20 по сессиям

Считает код (`coach.run_is_intensive`): Z4+Z5 доминируют (>50%) **или** аэробный TE ≥ 4 **или** анаэробный TE ≥ 2. Не «на глаз» в промпте.

---

## Эксплуатация

### Прод-бот

```bash
systemctl --user start|stop|restart garmin-backup-bot.service
systemctl --user status garmin-backup-bot.service
journalctl --user -u garmin-backup-bot -f
```

Unit: `deploy/systemd/garmin-backup-bot.service`  
`WorkingDirectory=/home/evg/garmin-analysis-and-backup`  
`ExecStart=…/.venv/bin/python -m garmin_backup_bot.main`

### Dev-бот (ручной, обычно выключен)

```bash
systemctl --user start garmin-dev-bot.service   # @Rundown_dev_bot, data-dev/
# … проверка хендлеров …
systemctl --user stop garmin-dev-bot.service
```

### WebApp

Docker-образ `Dockerfile.webapp` (uvicorn `:8085`); снаружи — nginx `aiaptechka.ru/garmin`.  
Исходник unit: `deploy/systemd/garmin-backup-webapp.service` (может быть не активен, если compose/nginx ведёт контейнер отдельно).

### После правок кода

```bash
# синтаксис
python3 -c "import ast; ast.parse(open('src/garmin_backup_bot/FILE.py').read())"

# тесты (обязательно перед коммитом)
.venv/bin/python -m unittest discover tests -q

# прод
systemctl --user restart garmin-backup-bot.service
```

Pre-commit hook: `.githooks/pre-commit` (`git config core.hooksPath .githooks`).  
CI: `.github/workflows/tests.yml`.

---

## Footguns

1. **Tool-описания SQL ↔ реальные колонки БД.** Рассинхрон → Claude «молча» ломает запросы и галлюцинирует. Сверять с `PRAGMA table_info`.
2. **Синк только в Утро/Спорт** (и онбординг). Не тащить в еду/статус/Q&A.
3. **WAL:** копировать `app.db` через `sqlite3 … ".backup"` / `scripts/backup_app_db.sh`, не `cp` без `-wal`/`-shm`.
4. **Реальные Garmin-данные** — в `data/users/{id}/DBs/`, не stub в корне юзера.
5. **HR-зоны:** Z3 = easy aerobic, не Z2.
6. **`.get(key) or default`**, не `.get(key, default)` для nullable.
7. **`.env` с секретами.** Бэкап `ENCRYPTION_KEY` — в `data/backups/encryption-key.enc` (см. restore README).
8. **Длинные ответы Telegram:** `self._split()` (лимит ~4000).
9. **Имя репозитория** ≠ продукт: это AI-тренер, не «утилита бэкапа».

---

## Безопасность (кратко)

- SQL-tools Claude: `mode=ro`, только SELECT; `query_app_db` — in-memory срез **своего** `user_id`; credentials/tokens не отдаются.
- Garmin-пароли: Fernet at rest; email в логах маскируется.
- WebApp: one-shot токены, rate-limit, лимит тела.
- Ошибки SQLite пользователю/модели — обобщённые; детали в journal.

Исторический аудит (2026-05) и последующие фиксы — в старых разделах CHANGELOG / git history.

---

## MCP (локально на сервере)

`/home/evg/garmin-mcp-sqlite.py` — read-only SQL к `app.db` и Garmin-БД выбранного юзера (для агентов/отладки). Не часть runtime бота.

---

## Полезные ссылки в репо

| Файл | Зачем |
|------|--------|
| [`CLAUDE.md`](./CLAUDE.md) | Правила разработки, паттерны, деплой-чеклист |
| [`CHANGELOG.md`](./CHANGELOG.md) | История релизов (SemVer light) |
| [`docs/garth-migration-spike.md`](./docs/garth-migration-spike.md) | Миграция GarminDB → garth |
| [`data/backups/README-restore.md`](./data/backups/README-restore.md) | Восстановление с бэкапа / offsite |
| `storage.py:_init_schema()` | Схема app.db |
| `coach.py` | Пороги recovery, 80/20, факты |
| `plan_builder.py` | Фазы, A-гонка, формат плана |
| `nutrition.py` | ISSN, классификация дня |
