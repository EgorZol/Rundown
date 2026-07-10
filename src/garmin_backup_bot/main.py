from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

from .analyst import HealthAnalyst
from .bot import build_application
from .config import load_settings
from .crypto import SecretBox
from .garmin_service import GarminService
from .nutrition import NutritionAnalyzer
from .plan_builder import WeeklyPlanBuilder
from .storage import Storage
from .transcription import Transcriber


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # httpx логирует URL запросов к Telegram на INFO — в URL виден bot-токен.
    # Глушим до WARNING, чтобы токен не светился в journalctl.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv()
    settings = load_settings()

    storage = Storage(settings.db_path)
    box = SecretBox(settings.encryption_key)
    service = GarminService(
        workdir_root=settings.garmin_workdir_root,
        exports_dir=settings.exports_dir,
    )
    analyst = HealthAnalyst(
        api_key=settings.anthropic_api_key,
        model=settings.anthropic_model,
        fallback_models=settings.anthropic_model_fallbacks,
        user_age=settings.user_age,
        weekly_km_target=settings.weekly_km_target,
        usage_sink=storage.log_token_usage,
    )
    plan_builder = WeeklyPlanBuilder(analyst=analyst, service=service)
    nutrition = NutritionAnalyzer(
        api_key=settings.anthropic_api_key,
        model=settings.anthropic_model,
        fallback_models=settings.anthropic_model_fallbacks,
        usage_sink=storage.log_token_usage,
    )
    transcriber = Transcriber(api_key=settings.openai_api_key) if settings.openai_api_key else None

    app = build_application(
        token=settings.telegram_bot_token,
        storage=storage,
        box=box,
        service=service,
        analyst=analyst,
        plan_builder=plan_builder,
        webapp_base_url=settings.webapp_base_url,
        webapp_token_ttl_seconds=settings.webapp_token_ttl_seconds,
        admin_user_ids=settings.admin_user_ids,
        user_timezone=settings.user_timezone,
        garmin_db_timezone=settings.garmin_db_timezone,
        nutrition=nutrition,
        transcriber=transcriber,
        payment_provider_token=settings.payment_provider_token,
    )
    app.run_polling()


if __name__ == "__main__":
    main()
