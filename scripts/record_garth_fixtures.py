#!/usr/bin/env python3
"""Записывает реальные ответы Garmin API в tests/fixtures/garth/.

Одноразовый инструмент: оборачивает garth-клиент рекордером, гоняет обычный
инкрементальный синк владельца и сохраняет по одному образцу ответа на каждый
тип эндпоинта. Персональные данные вычищаются (_scrub) — фикстуры коммитятся
в публичный репозиторий.

    .venv/bin/python scripts/record_garth_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from src.garmin_backup_bot.config import load_settings  # noqa: E402
from src.garmin_backup_bot.storage import Storage  # noqa: E402
from src.garmin_backup_bot.crypto import SecretBox  # noqa: E402
from src.garmin_backup_bot.garmin_service import GarminService  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "garth"
UID = 172354679

# путь-подстрока → имя фикстуры (записывается ПЕРВЫЙ встреченный ответ)
ROUTES = [
    ("dailySleepData", "sleep.json"),
    ("socialProfile", "social_profile.json"),
    ("dailyStress", "daily_stress.json"),
    ("hrv-service", "hrv.json"),
    ("usersummary-service", "daily_summary.json"),
    ("weight-service", "weight.json"),
    ("personal-information", "personal_info.json"),
    ("activitylist-service", "activity_list.json"),
    ("hrTimeInZones", "activity_zones.json"),
    ("/laps", "activity_laps.json"),
    ("/splits", "activity_splits.json"),
    ("activity-service/activity/", "activity_detail.json"),  # после zones/laps/splits!
]

SCRUB_DROP = {"geoPolylineDTO", "sleepMovement", "sleepLevels", "sleepHeartRate",
              "sleepStress", "sleepBodyBattery", "wellnessEpochSPO2DataDTOList",
              "sleepRestlessMoments", "breathingDisruptionData"}
SCRUB_TEXT = {"displayName", "ownerDisplayName", "fullName", "firstName", "lastName",
              "userName", "username", "emailAddress", "activityName", "ownerFullName",
              "profileImageUrlSmall", "profileImageUrlMedium", "profileImageUrlLarge",
              "locationName"}
SCRUB_EMPTY = {"hrvReadings"}  # обязательны для pydantic garth, содержимое личное
SCRUB_NUM = {"userProfileId", "userProfilePk", "profileId", "ownerId",
             "startLatitude", "startLongitude", "endLatitude", "endLongitude"}


def _scrub(node):
    if isinstance(node, dict):
        return {k: ("user" if k in SCRUB_TEXT else 0 if k in SCRUB_NUM
                    else [] if k in SCRUB_EMPTY else _scrub(v))
                for k, v in node.items() if k not in SCRUB_DROP}
    if isinstance(node, list):
        return [_scrub(x) for x in node]
    return node


class RecordingClient:
    def __init__(self, real):
        self._real = real
        self.saved: set[str] = set()

    def connectapi(self, path, **kwargs):
        resp = self._real.connectapi(path, **kwargs)
        # список активностей пишем только для первой страницы
        if "activitylist-service" in path and (kwargs.get("params") or {}).get("start", 0) > 0:
            return resp
        for needle, fname in ROUTES:
            if needle in path and fname not in self.saved:
                self.saved.add(fname)
                data = _scrub(resp)
                if fname == "activity_list.json" and isinstance(data, list):
                    data = data[:3]  # трёх активностей достаточно
                (OUT / fname).write_text(json.dumps(data, ensure_ascii=False, indent=1))
                print(f"  записан {fname} ({path[:60]}…)")
                break
        return resp

    def __getattr__(self, name):
        return getattr(self._real, name)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    s = load_settings()
    storage = Storage(s.db_path)
    box = SecretBox(s.encryption_key)
    creds = storage.get_credentials(UID)
    svc = GarminService(workdir_root=s.garmin_workdir_root, exports_dir=s.exports_dir)

    real_login = svc._garth_login
    svc._garth_login = lambda *a, **kw: RecordingClient(real_login(*a, **kw))

    print("health sync…")
    svc.run_health_sync(UID, creds.username, box.decrypt(creds.password_encrypted))
    print("activity sync…")
    svc.run_activity_sync(UID, creds.username, box.decrypt(creds.password_encrypted))
    print(f"\nфикстуры в {OUT}:")
    for f in sorted(OUT.glob("*.json")):
        print(f"  {f.name}: {f.stat().st_size // 1024} КБ")


if __name__ == "__main__":
    main()
