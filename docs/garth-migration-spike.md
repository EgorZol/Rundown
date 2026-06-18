# Спайк: миграция синка с GarminDB на garth/JSON API

**Статус:** не начат. Это спецификация спайка, не реализация.
**Создан:** 2026-05-22.
**Зачем:** см. раздел «Контекст».

## Контекст

Сейчас синхронизация Garmin-данных идёт через `garmindb_cli.py` (subprocess в `garmin_service.py`): качает FIT-файлы и **парсит их локально** — это CPU-тяжело. На 2-ядерном VPS первый синк нового юзера фактически вешает сервер; параллельные синки нескольких юзеров складывают его совсем. Под мультиюзерность не годится.

Костыль уже стоит (2026-05-22): `nice -n19 ionice -c3` на garmindb-процесс + глобальный семафор (один синк за раз). Сервер больше не виснет, но способ остаётся «тяжёлый GarminDB».

**Настоящее решение:** заменить GarminDB на прямые запросы к Garmin Connect API через `garth` (библиотека уже в проекте, версия 0.5.19, сейчас используется только для логина). JSON приходит готовым — локального парсинга FIT нет, работа становится I/O-bound вместо CPU-bound и нормально масштабируется.

**Бот НЕ использует** поминутный `garmin_monitoring.db` (71 МБ балласта) — при переходе на JSON он просто не качается.

## Цель спайка

Не реализовать миграцию, а **подтвердить её осуществимость** и убрать все «?»:
1. Для каждой таблицы/колонки, которую читает бот, найти конкретный источник в garth (типизированный класс или `connectapi`-эндпоинт) — «поле в поле».
2. Оценить число запросов на первый синк (год истории) и риск упереться в rate limit Garmin.
3. Принять решение по посекундным `activity_records` (оставить полностью / свести к laps).
4. По итогу — точная оценка трудозатрат на Фазу 1 (саму реализацию).

## Что читает бот (источник истины — `analyst.py` tool-descriptions + чтения в `garmin_service.py`)

### garmin.db

| Таблица.колонка | Вероятный источник в garth | Проверить |
|---|---|---|
| `sleep` (day, start, end, total_sleep, deep_sleep, rem_sleep, score) | `garth.SleepData.get(date)` + `garth.DailySleep` (sleep score) | имена полей стадий сна, формат времени start/end |
| `resting_hr` (day, resting_heart_rate) | `connectapi("/usersummary-service/usersummary/daily/{displayName}", params={"calendarDate": date})` → поле `restingHeartRate` | точный путь, есть ли RHR в usersummary, или нужен `/wellness-service/wellness/dailyHeartRate/...` |
| `daily_summary` (day, rhr, bb_max, bb_min, stress_avg, steps) | сборка: steps ← `garth.DailySteps`; stress ← `garth.DailyStress`; body battery ← `garth.BodyBatteryData`/`DailyBodyBatteryStress`; rhr ← usersummary | как собрать в одну строку на день; имена bb_max/bb_min |
| `weight` (day, weight) | `garth.WeightData.get(date)` | единицы (г/кг), что при отсутствии замера |

### garmin_activities.db

| Таблица.колонка | Вероятный источник | Проверить |
|---|---|---|
| `activities` (activity_id, sport, name, start_time, distance, avg_hr, max_hr, moving_time, avg_speed) | `connectapi("/activitylist-service/activities/search/activities", params={"start":0,"limit":N})` | имена полей в JSON списка активностей |
| `activities` (training_load, hrz_1_time..hrz_5_time) | `connectapi("/activity-service/activity/{activityId}")` — детали | точные имена: `activityTrainingLoad`? `hrTimeInZone_N`? |
| `activity_records` (activity_id, timestamp, distance, heart_rate, speed, altitude) | `connectapi("/activity-service/activity/{activityId}/details")` — посекундные сэмплы | ⚠️ РЕШЕНИЕ: оставить посекундно (тяжёлый запрос на каждую активность) или свести к laps (`/activity/{id}/splits`) |

### garmin_summary.db

| Что | Проверить |
|---|---|
| days/weeks/months/years summary, intensity_hr | Действительно ли бот это запрашивает (читается в `garmin_service.py:282`)? Если да — `garth.WeeklySteps/WeeklyStress/WeeklyIntensityMinutes` либо вычислять локально из дневных данных |

## Открытые вопросы (закрыть в спайке)

1. **RHR** — точный эндпоинт. `usersummary` daily содержит `restingHeartRate`?
2. **HR-зоны и training load** — точные имена полей в `/activity-service/activity/{id}`.
3. **Rate limits Garmin Connect** — сколько запросов в минуту безопасно. Первый синк года ≈ 365 дней × ~5 эндпоинтов + N активностей ≈ 1500–2500 запросов. Нужна стратегия throttle + backoff (garth кидает `garth.exc.GarthHTTPError` на 429).
4. **Посекундные `activity_records`** — оставить или laps-only. Бот использует для «покажи сплиты». Решить по реальной потребности.
5. **`garmin_summary.db`** — что именно читается, вычислимо ли локально.
6. **Таймзоны** — garth отдаёт локальное или UTC; как маппить на `day`.
7. **displayName / profile id** — часть эндпоинтов требует. Брать из `garth.UserProfile`.
8. **Первый vs инкрементальный синк** — после первого тянуть только новые дни (`since last_sync`).

## Как делать спайк (практически)

1. Залогиниться garth-ом под аккаунтом владельца (токены уже лежат в `data/users/<admin_id>/config/`, можно `garth.resume()`).
2. Для **одного дня** дёрнуть каждый источник, сдампить сырой JSON, разложить «поле в поле» против таблицы выше.
3. Для **2–3 активностей** — список + детали + splits, сверить с `activities`/`activity_records`.
4. Замерить тайминги; прикинуть запросы-на-первый-синк и риск 429.
5. Обновить таблицы выше — убрать все «?», проставить точные пути и имена полей.

## Критерии успеха спайка

- Таблица «поле в поле» без единого «проверить» — каждая нужная колонка имеет подтверждённый источник.
- Решён вопрос посекундных `activity_records`.
- Есть оценка: запросов на первый синк, ожидаемое время, риск rate limit + стратегия троттлинга.
- Есть оценка трудозатрат Фазы 1.

## Что НЕ делать в спайке

- Не переписывать `garmin_service.py` — это Фаза 1, после спайка.
- Не трогать схему БД, `analyst.py`, `storage.py` — они остаются как есть, меняется только наполнение таблиц (из JSON вместо FIT).
- Не настраивать очередь/воркер — это Фаза 2.

## Что дальше (после спайка)

- **Фаза 1:** реализовать JSON-синк в `garmin_service.py` (заменить `garmindb_cli`-subprocess на garth-функции + INSERT в SQLite). Инкрементальный синк по дате.
- **Фаза 2:** вынести синк в отдельный воркер + очередь задач (таблица pending-jobs, бот только enqueue).
- **Фаза 3:** при реальном росте — отдельный хост/контейнер под гармин.

Подробнее о фазах и масштабировании — обсуждалось в сессии 2026-05-22.
