from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    encryption_key: str
    db_path: Path
    exports_dir: Path
    garmin_workdir_root: Path
    webapp_base_url: str | None
    webapp_token_ttl_seconds: int
    admin_user_ids: set[int]
    anthropic_api_key: str
    anthropic_model: str
    anthropic_model_fallbacks: list[str]
    user_timezone: str
    garmin_db_timezone: str  # timezone garmindb uses when storing naive datetimes
    user_age: int
    weekly_km_target: float
    openai_api_key: str | None
    payment_provider_token: str | None  # BotFather → Payments → ЮKassa; пусто = оплата «скоро»


def load_settings() -> Settings:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    key = os.getenv("ENCRYPTION_KEY", "").strip()

    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    if not key:
        raise RuntimeError("ENCRYPTION_KEY is required")

    db_path = Path(os.getenv("DB_PATH", "./data/app.db")).expanduser()
    exports_dir = Path(os.getenv("EXPORTS_DIR", "./exports")).expanduser()
    workdir_root = Path(os.getenv("GARMIN_WORKDIR_ROOT", "./data/users")).expanduser()
    # GARMIN_DB_SYNC_CMD больше не используется (после garth-миграции). Принимаем
    # из .env для backward-compat, но не валидируем — оставлен для будущих юзеров,
    # у которых ENV всё ещё содержит этот ключ. Можно безопасно удалить из .env.
    webapp_base_url = os.getenv("WEBAPP_BASE_URL", "").strip() or None
    webapp_token_ttl_seconds = int(os.getenv("WEBAPP_TOKEN_TTL_SECONDS", "900"))
    admin_user_ids_raw = os.getenv("ADMIN_USER_IDS", "").strip()
    admin_user_ids: set[int] = set()
    if admin_user_ids_raw:
        for item in admin_user_ids_raw.split(","):
            value = item.strip()
            if not value:
                continue
            try:
                admin_user_ids.add(int(value))
            except ValueError:
                # Некорректный id в ADMIN_USER_IDS не должен ронять старт бота.
                print(f"config: пропускаю некорректный ADMIN_USER_IDS={value!r}", file=__import__("sys").stderr)

    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is required")
    anthropic_model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5").strip()
    anthropic_model_fallbacks_raw = os.getenv(
        "ANTHROPIC_MODEL_FALLBACKS",
        "claude-sonnet-4-20250514",
    ).strip()
    anthropic_model_fallbacks = [
        model.strip()
        for model in anthropic_model_fallbacks_raw.split(",")
        if model.strip()
    ]
    user_timezone = os.getenv("USER_TIMEZONE", "Europe/Moscow").strip()
    garmin_db_timezone = os.getenv("GARMIN_DB_TIMEZONE", user_timezone).strip()
    user_age = int(os.getenv("USER_AGE", "35").strip())
    weekly_km_target = float(os.getenv("WEEKLY_KM_TARGET", "0").strip())
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip() or None
    payment_provider_token = os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip() or None

    return Settings(
        telegram_bot_token=token,
        encryption_key=key,
        db_path=db_path,
        exports_dir=exports_dir,
        garmin_workdir_root=workdir_root,
        webapp_base_url=webapp_base_url,
        webapp_token_ttl_seconds=webapp_token_ttl_seconds,
        admin_user_ids=admin_user_ids,
        anthropic_api_key=anthropic_api_key,
        anthropic_model=anthropic_model,
        anthropic_model_fallbacks=anthropic_model_fallbacks,
        user_timezone=user_timezone,
        garmin_db_timezone=garmin_db_timezone,
        user_age=user_age,
        weekly_km_target=weekly_km_target,
        openai_api_key=openai_api_key,
        payment_provider_token=payment_provider_token,
    )
