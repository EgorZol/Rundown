# Changelog

Все заметные изменения проекта документируются в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
проект следует [SemVer](https://semver.org/lang/ru/) (light): `MAJOR.MINOR.PATCH`.

- **MAJOR** — большие архитектурные сдвиги, изменяющие модель работы (новые таблицы, новые подходы)
- **MINOR** — заметные новые фичи без поломок
- **PATCH** — багфиксы, мелкие правки промптов, UX-полировка

## [Unreleased]

## [0.6.0] — 2026-07-07

Надёжность и масштабируемость: мониторинг, бэкапы, CI, автотесты,
распил analyst.py, фиксы синка и дат.

### Added
- **Учёт токенов Claude**: таблица `token_usage` (user_id, method, model,
  in/out/cache) — пишется после каждого API-вызова, включая tool-цикл.
  Атрибуция юзера через ContextVar. Блок «Токены за 30 дней» в /admin_stats.
- **Мониторинг («Sentry на минималках»)**: глобальный error handler —
  traceback владельцу в Telegram с дедупом; алерт «тихой деградации» синка
  (данные протухли ≥3 дн при активном юзере).
- **Карта возможностей UI** (`prompts.CAPABILITIES`) — единый источник
  истины: рендерится в системный промпт QA, `/help` и /start. Тест полноты:
  каждая BTN_* описана, фантомов нет, hidden-флаги сверены с клавиатурой.
- **Write-tools гонок**: `add_race` (ISO-дата, дедуп), `delete_race`,
  `set_race_priority` — кнопка «🏁 Старты» больше не обещает того, чего
  бот не умел; confirm_fact-костыль убран.
- **Автотесты 32 → 78**: storage (CRUD/миграции/retention/токены),
  tools (инварианты безопасности SQL: mode=ro, изоляция user_id,
  невидимость секретов), formatting (даты/календарь), garmin_service
  (первичность синка по доменам), prompts (полнота карты UI), coach
  (валидация дат плана).
- **CI**: pre-commit hook (тесты перед каждым коммитом) + GitHub Actions
  (тесты на push/PR, Python 3.12).
- **Бэкапы**: per-user Garmin БД + JSON (splits/laps/HRV) в ежедневном
  бэкапе (ротация 7 дн); offsite-зеркало всего в Google Drive через
  rclone (scope=drive.file).

### Fixed
- **P0: гонка garth-сессий** — модульный синглтон garth при Semaphore(5)
  позволял параллельным синкам разных юзеров затирать друг другу
  авторизацию. Теперь изолированный `garth.Client` на вызов (+ бонусом
  встроенный retry 429/5xx).
- **Первичность синка по своему домену** — активити-синк проверял таблицу
  sleep и после health-синка считал себя инкрементальным → у нового юзера
  не качалась годовая история активностей (инцидент Саши: 25 активностей
  вместо 511; история восстановлена бэкфиллом).
- **Даты недельного плана считает код**: `coach.check_plan_dates` — пары
  «Пн DD.MM» валидируются по календарю, week_start выводится из дат
  (план на следующую неделю больше не затирает текущую);
  `coach.fix_plan_dates` — авто-коррекция дат в генерируемых планах.
- **Off-by-one дней до старта**: `_race_countdown` отдаёт готовое слово
  («суббота, послезавтра») вместо «[2 дней]» — LLM не считает дни сам.
- **Календарь дней недели добавлен в QA-контекст** — бот называл
  понедельник воскресеньем, видя только ISO-дату.
- **confirm_fact**: идентичный факт за ту же дату не дублируется; запрет
  записи веса/LTHR/tz (вес уходил в факты мимо профиля и ISSN-норм).
- **Еда**: правка/удаление — «📊 Питание» (бот отправлял в «🍽 Еда»);
  цифры в отчётах — только из свежего SQL, не из истории чата.
- **Род бота зафиксирован** (мужской: «принял», не «приняла»).
- `requirements.txt`: удалён мёртвый GarminDB, добавлены garth и
  apscheduler — свежая установка снова рабочая.

### Changed
- **analyst.py распилен 3190 → 1482 строк**: `formatting.py`
  (FormattingMixin, блоки контекста), `prompts.py` (системные промпты),
  `tools.py` (SQL-раннер + схемы tools). Поведение не менялось.
- **Event loop разблокирован**: тяжёлые чтения per-user БД и SQL
  tool-цикла — в `asyncio.to_thread` (14 call sites + tool-цикл).
- **Первичные синки — строго по одному** (`Semaphore(1)`), инкрементальные
  до 5 параллельно; выбор через `is_initial_sync_pending`.


## [0.5.0] — 2026-06-22

Hygiene release: чистка dead code, освобождение диска, ежедневный бэкап БД.

### Added
- **Ежедневный бэкап `data/app.db`** через systemd-user timer (03:00 + рандом ±5 мин).
  WAL-safe (sqlite3 `.backup`), ротация 14 дней, складывает в `data/backups/`.
  Unit-файлы в `deploy/systemd/garmin-backup-db.{service,timer}`,
  скрипт `scripts/backup_app_db.sh`.
- `data/archive/` — куда переехали archived `garmin_monitoring.db` (189 МБ суммарно).

### Changed
- **Удалён весь legacy GarminDB CLI код** (после garth-миграции v0.1.x).
  В `garmin_service.py` ушли: `run_backup`, `has_sleep_data_for_date`,
  `_scrub_password`, `_nice_prefix`, `_build_command`, `_write_garmindb_config`,
  `_default_garmindb_start_date`, поле `_sync_cmd_template`. Импорты `shlex`,
  `shutil`, `subprocess`, `sys` тоже удалены — стали сиротами.
- `GarminService.__init__` больше не требует `sync_cmd_template`.
- `Settings.garmin_db_sync_cmd` удалён. `GARMIN_DB_SYNC_CMD` в .env теперь
  не требуется (можно безопасно удалить из вашего .env).
- В `analyst.SYSTEM_PROMPT` (morning) удалена строка про оценку питания —
  в утренний отчёт питание не приходит, правило вводило Claude в заблуждение.
- CLAUDE.md: обновлена документация про длинные сообщения (убрана ссылка на
  удалённый `_send_long`).

### Removed (dead code)
- `Storage.set_user_memory` — заменён `add_memory_item`/`clear_user_memory`.
- `Storage.is_summary_sent`, `Storage.mark_summary_sent` — сироты.
- `Storage.delete_verified_fact` — не был подключён к хендлеру.
- `GarminBot._send_long` — все длинные хендлеры используют `_split()`.
- `HealthAnalyst._fmt_zone_times` — продублирован в `_format_activities`.

### Storage
- **`data/users/*/DBs/garmin_monitoring.db`** ×3 = 189 МБ перемещены в
  `data/archive/` (бот их не использует после garth-миграции). Если когда-то
  понадобятся поминутные HR/RR/PulseOx — данные сохранены, не удалены.

### Notes
- NutritionStatus dataclass в coach.py НЕ добавлен в этом релизе. Питание
  уже агрегируется кодом перед отправкой Claude в weekly_summary, дополнительный
  dataclass был бы over-engineering. Отложено на v0.6.0 если появится явная боль.

## [0.4.1] — 2026-06-22

Patch — 5 P0 багов, найденных параллельным агентским аудитом (6 agents).

### Fixed
- **`analyst.py:_ask_with_tools`**: `block.input.get("sql", "")` мог упасть
  AttributeError если Claude вернул `input=null`. Заменено на null-safe
  `(block.input or {}).get(...)`.
- **`analyst.py:_ask_with_tools`**: при превышении лимита 8 tool calls
  цикл делал `break` → доходил до `raise RuntimeError("No models")`. Юзер
  получал ошибку, хотя часть tool-вызовов уже отработала и записала в БД.
  Теперь возвращаем partial text из последнего ответа Claude или
  понятное сообщение «попробуй переформулировать», без crash.
- **`analyst.py:analyze_workout`**: OLD inline блок `[ИТОГО НЕДЕЛЯ]`
  считал от системной даты (`date.today()`), а WEEK FACTS — от TZ юзера.
  У юзеров в дальнем TZ на стыке суток два блока в одном промпте
  показывали разные суммы км. Теперь `today_iso` пробрасывается из
  bot.handle_workout, оба блока выровнены.
- **`analyst.py:_ask_with_tools`**: если в response stop_reason=tool_use,
  но при обработке не получилось ни одного tool_result — раньше
  пустой `[]` всё равно append'ился как user-turn, путая Claude.
  Теперь guard: выходим из цикла и возвращаем что есть.
- **`coach.compute_recovery_status({})`**: пустой metrics возвращал
  `label="good", safe_to_train_hard=True` — ложно-успокаивающий вердикт
  свежим юзерам без синхронизации. Теперь при отсутствии источников
  данных возвращает `label="no_data", safe_to_train_hard=False` с
  drivers=["нет утренних метрик — синхронизация не подтянула …"].

### Tests
- 32/32 проходят. `test_morning_no_data` обновлён под новое поведение.

## [0.4.0] — 2026-06-22

Большой архитектурный рефакторинг: «расчёты → код, решения → LLM, факты → БД».
Числовые пороги и арифметика вынесены из промптов в детерминированный модуль
`coach.py` с unit-тестами. Промпты сжаты, цифры больше не «плавают».

### Added
- **Модуль `coach.py`** — pure-функции, dataclasses (`MorningFacts`, `WeekFacts`,
  `WorkoutFacts`, `RecoveryStatus`, `GpsAnomaly`), константы порогов в одном месте
  (`PHASE_BANDS`, `RHR_SPIKE_BPM`, `TSB_*`, `ACWR_*` и др.).
- **`tests/test_coach.py`** — 32 unittest'а, в т.ч. регресс-кейс «66 vs 56»
  (ходьба не должна суммироваться в км бега).
- `verified_facts` пробрасываются во все анализы (morning/workout/weekly/QA),
  не только в QA как было ранее.
- Версия бота показывается в `/status`.
- `CHANGELOG.md`.

### Changed
- `analyze`, `analyze_workout`, `analyze_weekly_summary`, `ask` принимают
  пред-вычисленные `*Facts` через готовый блок системного промпта.
- Системные промпты сжаты: убраны конкретные пороги (RHR>5, TSB полосы,
  ACWR полосы, GPS-эвристика, phase-aware 80/20 mapping) — теперь живут
  только в `coach.py` как константы.
- `pyproject.toml` версия → `0.4.0`.

### Fixed (через детерминизацию)
- Невозможно теперь: «66 км вместо 56» (бот суммировал ходьбу) — фильтр
  `sport='running'` enforced в коде.
- Невозможно теперь: «норма 63 км из воздуха» — `norm_km` берётся ТОЛЬКО
  из `weekly_km_target` профиля, иначе `None`.
- Невозможно теперь: «80/20 жёстко для всех фаз» — `PHASE_BANDS` —
  единственный источник политики, верифицируется тестами.
- Невозможно теперь: GPS-аномалии пропускаются — детектируются кодом,
  Claude получает готовый список.

## [0.3.0] — 2026-06-22

Естественный диалог: бот сам распознаёт намерения пользователя и пишет в БД
без команд.

### Added
- Таблица `verified_facts(id, user_id, fact_date, fact_text, is_active)` —
  «утверждённые атлетом» факты как источник истины поверх Garmin-данных.
- Write-tools для Claude: `confirm_fact`, `remember_note`, `forget_note`,
  `set_race_result`, `record_feeling`. Claude вызывает их сам, по смысловым
  триггерам в свободном тексте.
- UI-подтверждения после tool-вызовов: «✅ Принял как факт», «💾 Запомнил»,
  «🗑 Удалил», «🏁 Результат», «📝 Самочувствие».
- В системный промпт `ask()` добавлен блок «🟢 ПРИОРИТЕТ ИСТОЧНИКОВ»:
  факт атлета > Garmin DB > история чата.

### Changed
- `get_user_memory` возвращает строки в формате `#N. текст`, чтобы Claude
  мог вызывать `forget_note(item_id=N)`.

## [0.2.0] — 2026-06-22

Чинит память и контекст. Главный смысл — пользователь общается, бот понимает,
ничего не забывает и не переспрашивает то, что уже сказал.

### Added
- Таблица `user_memory_items(id, user_id, content, created_at, expires_at,
  is_active)` — per-item модель с дедупом и сроком жизни. Миграция из старого
  `user_memory.notes` при старте.
- Команды `/forget N`, `/forget all`, `/memory` с пронумерованным списком.
- `/remember --until <дата> <текст>` — заметка со сроком.
- Парсер `_parse_expiry`: ISO, DD.MM[.YYYY], «завтра/послезавтра», «через N
  дней/недель/месяцев». 18 тест-кейсов покрыто (вне unittest).
- Тег `[ЗАПОМНИТЬ до DATE: …]` — Claude сам ставит срок жизни.
- Claude сам уточняет срок («на сколько запомнить?»), если состояние явно
  временное (травма, болезнь, курс лекарств).
- `races.actual_time` + `actual_notes`; команда `/race result #N <время>`.
- Окно недели в «📊 Итог недели» — если today=Пн, показывается прошлая Пн-Вс.
- `activity_sync` в `handle_morning` (раньше синкался только health).
- GPS-аномалия (промптовая эвристика — позже переехала в код в 0.4.0).
- Phase-aware 80/20 правило (позже переехало в код).
- Блок «🚫 НЕ ВЫДУМЫВАЙ НОРМУ» (позже стал избыточен в 0.4.0).
- Подтверждённые факты + verified_facts overlay (расширено в 0.3.0).

### Changed
- Убрана `[:800]` усечка в `handle_morning` и `handle_workout` — обрывок
  отравлял будущий контекст.
- `add_message(source=...)` стало обязательным аргументом.
- `get_history(sources=[...])` принимает список источников.
- Морнинг-история теперь читает `("morning","workout","qa","plan_tweak")`,
  workout — `("workout","qa")`. Бот видит ответы юзера из QA и не
  переспрашивает результат гонки.
- `handle_question` тянет `races` с горизонтом −21 день, включая прошедшие
  с `actual_time`.
- Системный промпт `ask()` разнесён на stable (правила+user_memory под
  `cache_control: ephemeral`) и dynamic (даты+метрики+цель+план без кэша).

### Fixed
- Куча мелкого: blacklist в `/remember` и auto-extract для целей/планов/гонок
  с датой/LTHR/веса — для них есть структурные команды.
- Защитный strip `[ЗАПОМНИТЬ:]` в плановых/анализных путях.
- Чистка `user_memory_items` от целевых/планово-гоночных строк (одноразовый
  скрипт `scripts/clean_user_memory.py`).

## [0.1.0] — 2026-02-06 / 2026-06-18

Initial commit + UX-чистка после аудита: webapp на русском, BotCommands,
lock в handle_plan, переименование кнопок.

### Added
- Telegram-бот «персональный AI-тренер по бегу»: morning brief, анализ
  тренировки, недельный план, разбор питания, Q&A.
- Стек: Python 3.12, python-telegram-bot 21.10 (polling), FastAPI webapp
  на :8085, Anthropic Claude (sonnet-4-6 + fallback 4-5), OpenAI Whisper,
  garth для синка с Garmin Connect, SQLite с Fernet-шифрованием паролей.
- Структура per-user: `data/users/{tg_id}/DBs/garmin*.db`.
- Webapp `/connect` с one-shot токенами, rate-limit, лимит тела.
- Аудит безопасности: 12 из 14 проблем починены.
- garth-миграция: убран CPU-тяжёлый GarminDB FIT-парсинг, синк теперь
  I/O-bound сетевой за 5-10 сек.
- `plan_builder.determine_week_type` с phase-aware периодизацией по
  дистанции A-гонки.
