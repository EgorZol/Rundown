# Garmin Running Coach Bot

**Telegram-бот «персональный AI-тренер по бегу»**. Несмотря на имя репозитория «garmin-analysis-and-backup», главная функция — AI-анализ данных с Garmin + генерация недельных планов тренировок + разбор питания (фото/голос/текст). Бэкап-часть осталась как побочная — синхронизация локальных SQLite через `GarminDB` и ZIP-экспорт в `exports/`.

## TL;DR

- Бот: long-polling, systemd-user сервис из venv
- WebApp: FastAPI на :8085, контейнер `evg-garmin-webapp-1`, доступен через nginx на `https://aiaptechka.ru/garmin` (форма ввода Garmin creds)
- AI: Claude (sonnet-4-6 + fallback sonnet-4-5) для анализа/Q&A, Claude Vision для еды, OpenAI Whisper для голоса
- Данные: `data/app.db` (общая, 13 таблиц) + `data/users/{tg_id}/DBs/garmin*.db` (per-user, через GarminDB)
- 2 активных пользователя, последние правки кода — сегодня (21 мая 2026)

## Структура

```
/home/evg/garmin-analysis-and-backup/
├── Dockerfile.webapp           ← только webapp в контейнере (Python 3.12, uvicorn :8085)
├── README.md / CLAUDE.md       ← этот файл + детальные правила для AI-агентов
├── pyproject.toml / requirements.txt
├── .env                        ← Telegram/Anthropic/OpenAI/Fernet ключи
├── garmindb.log                ← лог GarminDB CLI
├── src/garmin_backup_bot/
│   ├── bot.py            (~139 KB) ← Telegram handlers, клавиатура, awaiting-стейты
│   ├── analyst.py        (~148 KB) ← Claude analysis, tool use для SQL по 3 БД
│   ├── plan_builder.py   (~65 KB)  ← генерация недельных планов (recovery/base/build/peak/taper)
│   ├── garmin_service.py (~49 KB)  ← синк с Garmin Connect через GarminDB CLI
│   ├── storage.py        (~33 KB)  ← SQLite CRUD, 13 таблиц app.db
│   ├── nutrition.py      (~26 KB)  ← Claude Vision еда, ISSN-нормы по типу дня
│   ├── webapp.py         (~5 KB)   ← FastAPI форма /connect для Garmin creds
│   ├── transcription.py            ← OpenAI Whisper voice→text
│   ├── config.py / crypto.py / main.py / __init__.py
├── data/
│   ├── app.db (~424 KB)        ← общая БД (creds, планы, еда, профили)
│   ├── app.db-wal / -shm       ← WAL (≈4 MB, важно бэкапить вместе с .db)
│   └── users/<tg_id>/DBs/      ← per-user Garmin SQLite (~149 MB у активного юзера)
│       ├── garmin.db           ← sleep, HR, stress, weight, devices
│       ├── garmin_activities.db ← activities, laps, records
│       ├── garmin_summary.db   ← days/weeks/months/years summary
│       └── garmin_monitoring.db ← минутные HR/RR/PulseOx/intensity
├── exports/                    ← ZIP-выгрузки Garmin (~120 MB, не чистится)
├── deploy/systemd/             ← unit-файлы (исходники)
└── .venv/                      ← Python venv для systemd-сервиса бота
```

## Стек

- **Python:** 3.12 (≥3.10 в pyproject)
- **Бот:** `python-telegram-bot==21.10` (polling)
- **WebApp:** FastAPI 0.115.6 + uvicorn 0.34.0
- **AI:** Anthropic Claude (`claude-sonnet-4-6` основная + `claude-sonnet-4-5` fallback)
- **Vision/Voice:** Claude Vision (еда), OpenAI Whisper (опционально)
- **Garmin:** `GarminDB==3.6.7` + `garth` (хранит OAuth-токены, не пароли)
- **БД:** SQLite (FTS не используется, WAL включён)
- **Шифрование:** Fernet (`cryptography==44.0.0`) для Garmin-паролей
- **Деплой:** Бот — `systemd --user`; WebApp — Docker Compose + nginx

## ENV (`.env`)

| Переменная | Назначение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен бота от @BotFather |
| `ENCRYPTION_KEY` | Fernet key (base64 32B). 🔴 **Потеря = все Garmin-пароли нерасшифруемы** |
| `DB_PATH=./data/app.db` | общая БД |
| `EXPORTS_DIR=./exports` | ZIP-архивы |
| `GARMIN_WORKDIR_ROOT=./data/users` | per-user данные |
| `WEBAPP_BASE_URL=https://aiaptechka.ru/garmin` | URL WebApp (для one-shot токенов) |
| `WEBAPP_TOKEN_TTL_SECONDS=900` | TTL ссылки на форму |
| `GARMIN_DB_SYNC_CMD` | `garmindb_cli.py --config {config_dir} -d -i -l` |
| `GARMIN_START_DATE=2025-06-01` | горизонт истории |
| `ADMIN_USER_IDS=123456789` | админы (Telegram user_id через запятую) |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ANTHROPIC_MODEL_FALLBACKS` | Claude |
| `OPENAI_API_KEY` | для Whisper |
| `USER_AGE`, `WEEKLY_KM_TARGET` | дефолты профиля |

## Схема БД (`data/app.db`)

13 таблиц, источник истины — `storage.py:_init_schema()`. Текущие счётчики:

| Таблица | Строк | Назначение |
|---|---:|---|
| `conversation_messages` | 86 | История чатов с ботом |
| `daily_feelings` | 1 | Ежедневное самочувствие |
| `food_entries` | 88 | Записи о еде (фото/текст/голос) |
| `garmin_credentials` | 2 | Зашифрованные Fernet-ом Garmin login/password |
| `races` | 8 | Целевые гонки (поле `is_priority` — A-race флаг) |
| `training_goal` | 1 | Глобальная цель |
| `usage_events` | 1234 | События использования (для статистики) |
| `user_memory` | 2 | "Помни/Забудь" |
| `user_profile_overrides` | 2 | LTHR, timezone, опыт и т.п. |
| `web_tokens` | 0 | One-shot токены для WebApp |
| `weekly_plans` | 6 | Сгенерированные планы |
| `daily_summaries_sent` | 0 | Анти-дубль для утренних сводок |

**Per-user Garmin БД** (для одного активного юзера ~149 MB):
- `garmin.db` 28.6 MB — sleep (352), resting_hr (246), stress (349 350), daily_summary (352), weight (6)
- `garmin_activities.db` 48.3 MB — activities (143), activity_laps (1234), activity_records (411 532)
- `garmin_monitoring.db` 71.1 MB — monitoring (97 643), monitoring_hr (285 169), monitoring_rr (287 178), monitoring_pulse_ox (123 387)

⚠️ Файлы `garmin.db`/`garmin_activities.db` могут быть и в корне юзера (0 байт stub), и в подпапке `DBs/`. **Реальные данные — в `DBs/`**.

## Хендлеры и команды бота

### Команды
`/start`, `/link_garmin`, `/status`, `/remember`, `/memory`, `/forget`, `/admin_stats`, `/plan`, `/feeling`, `/goal`, `/race`, `/race priority #N` / `/race unpriority #N` (пометить A-гонку), `/profile_reset`

### Reply-кнопки (`MAIN_KEYBOARD`)
Утро / Тренировка / План / Спорт / Цель / Самочувствие / Память / Статус / Питание / Старты / Прогресс / Итог недели / Рекорды / Вес / LTHR / Часовой пояс / Опыт / Профиль / Еда / Отчёт по еде

### Медиа
- `filters.PHOTO` — Claude Vision еда
- `filters.VOICE` — Whisper → нормализованный текст
- `filters.StatusUpdate.WEB_APP_DATA` — данные из WebApp формы
- `filters.TEXT & ~filters.COMMAND` → `handle_question` — Claude Q&A с tool-use SQL по 3 БД (`query_health_db`, `query_activities_db`, `query_app_db`)

### Awaiting-стейты
`context.user_data["awaiting"]` ∈ {food, food_edit, weight, lthr, timezone, profile, ...} — переключает интерпретацию следующего сообщения.

### WebApp FastAPI (`webapp.py`, порт 8085)
- `GET /healthz` → "ok"
- `GET /connect?token=…` → HTML-форма (one-shot токен TTL 900 сек)
- `POST /submit` → Fernet-шифрование пароля → `garmin_credentials` upsert

## Запуск / эксплуатация

### Бот (systemd-user)
```bash
systemctl --user start garmin-backup-bot.service
systemctl --user stop garmin-backup-bot.service
systemctl --user restart garmin-backup-bot.service
systemctl --user status garmin-backup-bot.service
journalctl --user -u garmin-backup-bot -f
```

Unit: `~/.config/systemd/user/garmin-backup-bot.service` (исходник в `deploy/systemd/`). ExecStart: `/home/evg/garmin-analysis-and-backup/.venv/bin/python -m garmin_backup_bot.main`.

### WebApp (docker)
```bash
# часть моно-compose /home/evg/docker-compose.yml (сервис garmin-webapp)
docker logs evg-garmin-webapp-1 --tail 50
docker compose -f /home/evg/docker-compose.yml restart garmin-webapp
```

### Синтаксис после правок
```bash
python3 -c "import ast; ast.parse(open('src/garmin_backup_bot/bot.py').read())"
```

## Макроцикл и выбор A-гонки (plan_builder.py)

Окна фаз — **масштабируются по дистанции**, а не прибиты как для марафона:

| Дистанция | taper | peak | build |
|---|---:|---:|---:|
| <10К | 5 дн | 14 дн | 28 дн |
| 10К | 7 | 21 | 42 |
| 21К полумарафон | 10 | 24 | 49 |
| 42К марафон / 50К | 14 | 28 | 56 |
| 80К+ ультра | 21 | 42 | 84 |

**Выбор целевой гонки** (`_select_target_race`):
1. `is_priority=1` (помечена `/race priority #N`) → без ограничения горизонтом, юзер сам выбрал A-гонку
2. иначе — ближайшая non-tune-up в 56 дн. Tune-up детектится по notes: «бежать легко», «с женой», «тест формы», «tune-up»
3. **каскад**: если в 14 дн после ближайшей есть гонка ≥ её дистанции — целимся на ту (типичный паттерн «лёгкая 10К как подводка к 50К через неделю»)

В календаре `🏁 Старты` приоритетная отмечается ⭐ и показывает свой `#ID`.

Hard-safety сигналы (RHR-spike, BB <50 ≥2дн, HRV UNBALANCED, ACWR >1.5, TSB <-25) **override** макроцикл — всегда recovery.

## ISSN-нормы и тип тренировочного дня

`nutrition.py:classify_training_day(plan_line, active_calories)` определяет 6 уровней:
- rest / easy / steady / threshold / long / race

Под каждый уровень — свои нормы калорий/углеводов/белков/жиров от веса (`calculate_issn_targets`). Не статические — периодизированы по типу дня.

Тип определяется из строки плана на сегодня; fallback — `calories_active` из Garmin.

Если `calories_total < 1400` — данные расхода неполные, показывается предупреждение.

## Footguns

0. **Tool-use схемы должны соответствовать реальной БД.** Описания `query_health_db`/`query_activities_db` в `analyst.py` — это контракт для Claude. Если там написано `heart_rate`, а в БД `hr` — Claude не получит ошибку в виде «no such column», только обобщённое «запрос не выполнен» (по аудиту) → начнёт галлюцинировать. Сверять с PRAGMA TABLE_INFO при правках.



1. **Garmin sync — тяжёлая операция (2–3 мин).** Вызывать ТОЛЬКО в хендлерах «Утро» и «Спорт». CLAUDE.md прямо запрещает в других местах. Иначе бот зависает на минуты.
2. **Пароли в открытом виде на момент синка.** `GarminConnectConfig.json` пишется с расшифрованным паролем и удаляется после. Если процесс упал — может остаться на диске.
3. **WAL не флашится при копировании.** При копировании `app.db` без `.db-wal`/`-shm` теряются последние записи. Бэкап делать через `sqlite3 app.db ".backup app.db.bak"` или `VACUUM INTO`.
4. **Per-user БД растут быстро.** У одного юзера `garmin_monitoring.db` уже 71 MB за <1 год. Ротации нет.
5. **Stub-файлы в `data/users/{id}/`.** Реальные данные — в `DBs/`, не в корне юзера.
6. **HR-зоны Garmin 5-zone.** Z3 = аэробная (лёгкий бег), НЕ Z2. CLAUDE.md явно предупреждает.
7. **`.get(key, default)` vs `.get(key) or default`.** Если ключ есть со значением `None`, первое вернёт `None`. Использовать `or default`.
8. **`.env` содержит реальные секреты.** Telegram, Anthropic, OpenAI keys, Fernet encryption key. Резервного бэкапа Fernet-ключа нет.
9. **`exports/` не очищается** — 120 MB ZIP-архивов с февраля 2026.
10. **WebApp container не имеет прямого порт-mapping.** Рассчитывает на nginx из `shared_proxy` external network. Прямой curl с хоста не достучится.
11. **Anthropic API fallback chain.** Если ретайрят `sonnet-4-6` — fallback на `sonnet-4-5`. Если retire оба — править `.env`. Дефолт в `config.py` — `claude-sonnet-4-20250514` (устаревший).
12. **Имя проекта вводит в заблуждение.** `garmin-analysis-and-backup` — но реально это AI-тренер по бегу.

## Аудит безопасности (2026-05-22)

Проведён аудит, найдено 14 проблем. Статус:

| # | Серьёзность | Проблема | Статус |
|---|---|---|---|
| 1 | 🔴 | SQL-инструмент Claude: БД на запись + `PRAGMA writable_schema` проходил гард | ✅ БД открываются `mode=ro`, гард — только `SELECT` + `PRAGMA TABLE_*` |
| 2 | 🔴 | Нет изоляции юзеров в общей `app.db` через SQL-инструмент | ✅ `query_app_db` бьёт по in-memory копии только со строками своего `user_id`; `garmin_credentials`/`web_tokens` не копируются |
| 3 | 🔴 | Плейнтекст-пароль в `GarminConnectConfig.json` не чистился при краше sync | ✅ `try/finally` вокруг `subprocess.run` в `run_backup` и `_run_sync` |
| 4 | 🟡 | WebApp `/submit`: нет rate-limit и лимита размера тела | ✅ rate-limit 20/60с по IP, лимит тела 64 KB, `max_length` на полях |
| 5 | 🟡 | Несогласованная проверка владельца токена `/submit` vs Telegram-путь | ⚠️ принято: токен 192-бит, риск низкий |
| 6 | 🟡 | Email Garmin (PII) в `journalctl` | ✅ `_mask_email()` — логируется `f***@domain` |
| 7 | 🟡 | `int(parts[2])` в callback-хендлерах падал на подделанной callback-data | ✅ безопасный парс `arg_id` в `handle_fooddb_callback` |
| 8 | 🟡 | Тексты ошибок SQLite уходили в контекст модели (разведка схемы) | ✅ обобщённое сообщение, детали — в лог |
| 9 | 🟢 | WebApp бинд `0.0.0.0:8085` | ⚠️ не проблема: порт контейнера не опубликован на хост |
| 10 | 🟢 | `PRAGMA journal_mode=WAL` на каждом коннекте | ⚠️ принято: no-op стоимостью микросекунды |
| 11 | 🟢 | `consume_web_token` неатомарен (double-spend) | ✅ один `DELETE … RETURNING` |
| 12 | 🟢 | Текст исключения уходил юзеру в `ask()` | ✅ обобщённое сообщение |
| 13 | 🟢 | Стрей-файл `=1.30.0` в корне | ✅ удалён |
| 14 | 🟢 | Хрупкий парс `ADMIN_USER_IDS` | ✅ `try/except`, невалидный id не роняет старт |

Не входило в аудит, но замечено: бот-токен светится в httpx-логах (`journalctl --user -u garmin-backup-bot`) на INFO — как было в Ksucha. Лечится `logging.getLogger("httpx").setLevel(logging.WARNING)`.

## Масштабирование / производительность

Первый Garmin-синк нового юзера CPU-тяжёлый (GarminDB парсит FIT-файлы) — на 2-ядерном VPS вешал сервер.

**Сделано (костыль, 2026-05-22):**
- garmindb-процесс запускается с `nice -n19 ionice -c3` — не голодает бота и соседние сервисы (`garmin_service.py:_nice_prefix`)
- Глобальный семафор `_global_sync_sem` в `bot.py` — только один синк во всём боте одновременно, остальные ждут в очереди

**Настоящее решение (спланировано):** уход от GarminDB на Garmin Connect JSON API через `garth` — см. `docs/garth-migration-spike.md`. Убирает CPU-парсинг, делает синк I/O-bound и масштабируемым.

## Полезное
- Детальные правила работы с кодом: `CLAUDE.md`
- Спайк миграции синка: `docs/garth-migration-spike.md`
- Источник истины схемы: `storage.py:_init_schema()`
- ISSN-нормы: `nutrition.py:calculate_issn_targets()`
- Системные промпты Claude: `analyst.py`, `plan_builder.py`, `nutrition.py`
