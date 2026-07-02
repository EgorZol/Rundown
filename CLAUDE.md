# CLAUDE.md — Garmin Running Coach Bot

## Роль и зона ответственности

Ассистент разрабатывает и поддерживает Telegram-бота — персонального AI-тренера по бегу с анализом данных Garmin, планированием тренировок и учётом питания.

- Пишет Python-код, модифицирует существующие модули
- НЕ создаёт новые файлы без явной необходимости
- Все пользовательские тексты бота — на русском
- НЕ трогает `.env` (секреты)
- Использует Git (ветка `main`) для сохранения изменений кода, делает коммиты с понятными описаниями на русском языке

## Пользователь

- Единственный разработчик, владеет всем стеком (Python, SQLite, Telegram Bot API, Claude API)
- Русскоязычный, предпочитает краткие ответы и быструю реализацию
- Контекст: бегун-любитель, готовится к гонкам (15К–ультра), использует Garmin

## Архитектура и ключевые файлы

```
src/garmin_backup_bot/
├── bot.py              # ~2400 строк. Telegram-хендлеры, клавиатура, джобы
├── analyst.py          # ~2500 строк. Claude-анализ, системные промпты, tool use (SQL)
├── plan_builder.py     # ~1200 строк. Генерация планов, тип недели (recovery/base/build/peak/taper)
├── garmin_service.py   # ~1060 строк. Синхронизация Garmin Connect, сбор метрик
├── storage.py          # ~700 строк. SQLite CRUD, 13 таблиц
├── nutrition.py        # ~420 строк. Распознавание еды (Claude Vision), ISSN-нормы
├── webapp.py           # FastAPI — форма для Garmin credentials (порт 8085)
├── transcription.py    # OpenAI Whisper voice-to-text
├── config.py           # Загрузка .env → Settings dataclass
├── crypto.py           # Fernet encrypt/decrypt
└── main.py             # Точка входа: собирает компоненты и запускает бот
```

### Связи между модулями

- `main.py` создаёт `GarminService`, `HealthAnalyst`, `PlanBuilder`, `NutritionAnalyzer`, `Transcriber` и передаёт в `bot.py`
- `bot.py` вызывает `analyst.py` для AI-анализа, `plan_builder.py` для планов, `nutrition.py` для еды
- `garmin_service.py` читает/пишет per-user SQLite БД в `data/users/{id}/DBs/`
- `storage.py` управляет `data/app.db` (credentials, планы, еда, история, профили)

## Источники истины

| Что | Где | Приоритет |
|-----|-----|-----------|
| Схема app.db | `storage.py:_init_schema()` | Единственное место определения таблиц |
| Схема Garmin SQLite (real cols) | `analyst.py` tool descriptions `query_*_db` | Описания должны совпадать с реальными колонками `garmin.db` / `garmin_activities.db` |
| Поля Garmin DB | `garmin_service.py:collect_daily_metrics()` | Какие поля доступны из Garmin |
| HR-зоны | Garmin 5-zone модель | Z3 = аэробная (основная лёгкая зона), НЕ Z2 |
| ISSN-нормы | `nutrition.py:calculate_issn_targets()` | Периодизированы по 6 типам тренировочного дня |
| Окна фаз цикла | `plan_builder.py:_phase_windows(dist_km)` | Масштабируются по дистанции A-гонки (10К ≠ марафон) |
| Выбор A-гонки | `plan_builder.py:_select_target_race()` | is_priority → ближайшая non-tune-up → каскад |
| Формат плана | `plan_builder.py` системный промпт | Дни Пн–Вс + дата + зона + пас |
| Конфигурация | `.env.example` | Все допустимые переменные окружения |

## Правила работы с кодом

### bot.py
- Хендлеры регистрируются в `_register_handlers()` — порядок важен: специфичные ПЕРЕД общим `handle_question`
- `filters.PHOTO` и `filters.VOICE` не конфликтуют с `filters.TEXT` — разные типы сообщений
- Awaiting-состояния через `context.user_data["awaiting"]` — food, food_edit, weight, lthr, timezone, profile и др.
- `MAIN_KEYBOARD` — ReplyKeyboardMarkup, отправляется с каждым ответом
- **Длинные сообщения** (>4000 chars): Telegram отсекает на 4096 → используй `self._split(text)` + цикл `reply_text(chunk, reply_markup=MAIN_KEYBOARD)`. Splitter режет по `\n\n` (абзацы) → `\n` (строки) → hard slice. Подключён ко всем «длинным» хендлерам (план/спорт/утро/Q&A/тренировка). Новые хендлеры с Claude-ответом — обязательно через `self._split()`

### Claude API (analyst.py, nutrition.py, plan_builder.py)
- Всегда retry с fallback-моделями — паттерн из `analyst.py:_generate_text()`
- Prompt caching: `cache_control: {"type": "ephemeral"}` на системных промптах
- Tool use для SQL-запросов: `query_health_db`, `query_activities_db`, `query_app_db`
- **При правке описаний tool-ов** — сверять с реальной схемой `data/users/<id>/DBs/*.db` (PRAGMA table_info). Уход в выдуманные имена (`heart_rate` вместо `hr`, `calories` вместо `calories_total`) приводит к молчаливым tool-fail'ам — Claude получает обобщённую ошибку и галлюцинирует
- **Анализ одной тренировки**: контекст `analyze_workout` начинается с метки «⟵ ОСНОВНАЯ» на активности #1. Промпт ЗАПРЕЩАЕТ агрегировать поля (Z1-Z5, TL, ЧСС) между активностями. Если редактируешь — сохрани оба сигнала
- **Q&A self-recognition**: в системном промпте `ask` есть блок «🤖 ЧТО Я УМЕЮ» (формат «✅ Сохранено... (#N)», кнопки/команды). Цель — чтобы Claude узнавал свои же сообщения и не отвечал «это от другого приложения»

### storage.py
- Новые таблицы — только в `_init_schema()`, с `IF NOT EXISTS`
- Новые колонки в существующих таблицах — `CREATE TABLE` с полем + блок `ALTER TABLE ... ADD COLUMN` если колонки нет (паттерн `user_profile_overrides`, `races.is_priority`). На проде идёт миграция при старте
- Методы по паттерну `save_X() -> int`, `get_X() -> list[dict]`, `delete_X() -> bool`

### Garmin sync
- Синхронизация переведена с внешней CLI-утилиты `GarminDB` (FIT-парсер) на прямой I/O-bound сетевой синк через Garth JSON API (`garth`). Это устранило 100% утилизацию CPU и зависания бота.
- `run_health_sync()` — быстрая сетевая операция (5–10 сек), загружает сон/BB/RHR/вес напрямую в `garmin.db`.
- `run_activity_sync()` — быстрая сетевая операция (5–10 сек), загружает тренировки, laps и Splits напрямую в `garmin_activities.db`.
- Таблица `steps_activities` (беговая динамика) заполняется только для беговых/пеших активностей или при наличии шагов во избежание `IntegrityError` на силовых/йоге.
- **Ограничения `NOT NULL`:** В БД пользователей SQLite колонки времени зон (`hrz_1_time`...`hrz_5_time`), elapsed/moving_time и темпа имеют ограничения `NOT NULL`. Для них добавлены жесткие строковые дефолты (например, `"00:00:00.000000"`), если Garmin возвращает `None`.
- Вызывать ТОЛЬКО в "Утро" и "Спорт" хендлерах. Семафор `self._global_sync_sem` (лимит = 5, `bot.py`) ограничивает одновременные синки для защиты от банов (HTTP 429) со стороны Garmin; плюс per-user `asyncio.Lock` против двойного синка одного юзера.
- **`_garth_login` возвращает изолированный `garth.Client` на вызов** — НЕ модульный синглтон. Модульные `garth.configure/resume/login` мутируют глобальную сессию процесса, и параллельные синки разных юзеров затирали бы друг другу авторизацию. Data-классы garth (`SleepData`, `HRVData` и т.п.) вызывать только с явным `client=garth_client`.

### Цель vs гонки
- `training_goal.goal_text` хранит цель как **одну строку** (передаётся Claude в текст плана) — это «север» юзера, может быть и без даты
- `races` — структурированный список с датами. **Только это** двигает `determine_week_type` через race-override
- Когда `/goal` сохраняется, `_sync_races_from_goal` парсит текст через `parse_races_from_text` и **автоматически создаёт A-гонки** (`is_priority=1`) для всех найденных дат, если их ещё нет в `races`. Существующие совпавшие — помечаются приоритетными. Юзер видит «⭐ Из цели извлечены A-гонки: …»
- Если цель — без даты («хочу выйти на 50 км/нед», «похудеть»), макроцикл работает только по TSB/BB/HRV, race-override не включается. Это нормально

### Питание
- ISSN-нормы привязаны к типу тренировочного дня (6 уровней: rest → race)
- Тип дня определяется из строки плана на сегодня, fallback — `calories_active` из Garmin
- Если `calories_total < 1400` — данные расхода неполные, показать предупреждение
- **Парсер даты записи** (`nutrition.py:parse_entry_date`): для DD.MM без года ТРЕБУЕТСЯ префикс «за» (либо явный год DD.MM.YYYY). Иначе ловит ложные срабатывания на дробях («5/6 порции», «5.6 ккал», «10.5 г»). Слова `вчера/сегодня/позавчера/<месяц словом>` работают без префикса
- **`max_tokens` API питания** = 3000 (`nutrition.py:_call_api`). Длинные списки еды (10+ продуктов) пробивают 1000. При `stop_reason="max_tokens"` подсистема бросает `NutritionTruncatedError` с дружелюбным текстом «разбей на 2 сообщения» — bot.py ловит и показывает юзеру вместо сырого `JSONDecodeError`

### Планирование (plan_builder.py)
- Окна фаз макроцикла масштабируются по дистанции через `_phase_windows(dist_km)`:
  - <10К: taper 5 / peak 14 / build 28 дн
  - 10К: 7 / 21 / 42
  - 21К: 10 / 24 / 49
  - 42К (марафон) / 50К: 14 / 28 / 56
  - 80К+ ультра: 21 / 42 / 84
- Выбор целевой гонки (`_select_target_race`):
  1. `is_priority=True` (юзер пометил `/race priority #N`) — без ограничения горизонтом
  2. ближайшая non-tune-up в горизонте 56 дн (фильтр по `_TUNE_UP_NOTE_HINTS` — «бежать легко», «с женой», «тест формы» и т.п.)
  3. **Каскад**: если в 14 дн после ближайшей есть гонка ≥ её дистанции — пересаживаемся на неё
- Hard-safety сигналы (RHR-spike, низкий BB, HRV UNBALANCED, ACWR >1.5, TSB <-25) ВСЕГДА override race-логику

## Обязательные паттерны

1. **Перед редактированием** — прочитать файл целиком или нужный диапазон строк
2. **После изменения .py** — проверить синтаксис:
   ```bash
   python3 -c "import ast; ast.parse(open('src/garmin_backup_bot/FILE.py').read())"
   ```
3. **После изменения bot.py** — перезапустить сервис:
   ```bash
   systemctl --user restart garmin-backup-bot.service
   ```
4. **После перезапуска** — проверить статус:
   ```bash
   systemctl --user status garmin-backup-bot.service | head -10
   ```
5. **None-safe доступ к спискам**: `.get("items") or []` вместо `.get("items", [])` — второе вернёт `None` если ключ существует со значением `None`
6. **Все сообщения пользователю** — на русском, с emoji-префиксами (как в существующем коде)
7. **Работа с Git**: проект использует Git (ветка `main`). После успешной проверки синтаксиса и перезапуска сервиса зафиксировать изменения: `git status` для проверки, `git add .` и `git commit -m "описание"` (на русском языке).

## Анти-паттерны

### НЕ добавлять Garmin sync в пользовательские хендлеры
```python
# ПЛОХО — блокирует на 2-3 минуты:
async def handle_food_report(self, ...):
    await asyncio.to_thread(self._service.run_health_sync, ...)  # 2+ мин ожидания

# ХОРОШО — использовать кэшированные данные + предупреждение:
async def handle_food_report(self, ...):
    garmin_daily = self._get_garmin_daily_calories(user_id, today)  # мгновенно из БД
    # Если данные неполные — показать предупреждение
```

### НЕ использовать статические ISSN-нормы
```python
# ПЛОХО — одинаковые нормы каждый день:
targets = {"carbs_g": {"min": weight * 5.0, "max": weight * 7.0}}

# ХОРОШО — периодизация по типу дня:
day_type = NutritionAnalyzer.classify_training_day(plan_line, active_calories)
targets = NutritionAnalyzer.calculate_issn_targets(weight, day_type)
```

### НЕ путать HR-зоны Garmin
```
# ПЛОХО: "Z2 — основная зона лёгкого бега"
# ХОРОШО: "Z3 — аэробная, основная зона лёгкого бега"
# В Garmin 5-zone: Z1=warmup, Z2=light, Z3=AEROBIC (easy runs), Z4=threshold, Z5=anaerobic
# hrz_X_hr — это НИЖНЯЯ граница (floor) зоны X
```

### НЕ использовать .get() с дефолтом для nullable полей
```python
# ПЛОХО — если items=None, вернёт None:
data.get("items", [])

# ХОРОШО — гарантированно вернёт список:
data.get("items") or []
```

## Верификация изменений

| Шаг | Команда | Когда |
|-----|---------|-------|
| Синтаксис | `python3 -c "import ast; ast.parse(open(f).read())"` | После каждого изменения .py |
| Импорты | `.venv/bin/python3 -c "from src.garmin_backup_bot.main import main"` | После изменения сигнатур/импортов |
| Перезапуск | `systemctl --user restart garmin-backup-bot.service` | После изменения кода бота |
| Статус | `systemctl --user status garmin-backup-bot.service` | После перезапуска |
| Логи | `journalctl --user -u garmin-backup-bot -n 30` | При ошибках |
| Коммит | `git add . && git commit -m "описание"` | После успешной проверки и перезапуска |

## Деплой

- **Бот**: systemd user service из `.venv`, working dir `/home/evg/garmin-analysis-and-backup`
- **Webapp**: Docker Compose (`garmin-webapp`) + nginx на `aiaptechka.ru/garmin`
- **БД**: `./data/app.db` (общая) + `./data/users/{id}/DBs/` (per-user Garmin)
- **Логи**: `journalctl --user -u garmin-backup-bot`

## Edge cases

- **Нет данных Garmin за сегодня** — показать "нажми Утро для синхронизации", не падать
- **Нет плана на неделю** — `classify_training_day` использует fallback по `calories_active`
- **Нет веса в профиле** — ISSN-нормы не показываются, только калории и БЖУ
- **Фото не еды** — `confidence: "none"` → "На фото не еда. Попробуй другое фото или опиши текстом."
- **Синк уже идёт** — `self._get_sync_lock(user_id).locked()` → показать "Уже идёт синхронизация, подожди"
- **Claude API недоступен** — fallback-модели из `ANTHROPIC_MODEL_FALLBACKS`, если все упали — показать ошибку пользователю
