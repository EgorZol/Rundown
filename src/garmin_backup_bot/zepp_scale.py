"""Умные весы Xiaomi/Amazfit (облако Zepp) — подключение и чтение измерений.

Пользователь авторизуется САМ в браузере (OAuth Xiaomi) и присылает боту
адрес, на который его перекинуло. Пароль от Xiaomi мы не спрашиваем и не
храним никогда: к этому аккаунту привязан умный дом, и держать чужие пароли
от него — недопустимая ответственность. У нас оседает только токен Zepp,
зашифрованный тем же Fernet, что и Garmin-креды.

⚠️ ЕДИНИЦЫ ИЗМЕРЕНИЯ ZEPP (грабли 21.07.2026):
  килограммы — muscleRate (ДА, несмотря на «Rate» в названии), boneMass
  проценты   — fatRate, bodyWaterRate, proteinRatio
Инварианта для самопроверки: мышцы + жир + кости = полный вес.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
import uuid
from typing import Any

import requests

logger = logging.getLogger(__name__)

# OAuth-клиент приложения Zepp Life на стороне Xiaomi
CLIENT_ID = "428135909242707968"
REDIRECT_URI = "https://api-mifit-cn.huami.com/huami.health.loginview.do"
API_HOST = "https://api-mifit.zepp.com"
LOGIN_URL = "https://account.zepp.com/v2/client/login"
AGENT = ("Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
TIMEOUT = 30


class ZeppAuthError(RuntimeError):
    """Токен протух или код невалиден — нужна повторная авторизация юзером."""


def authorize_url() -> str:
    """Ссылка, которую юзер открывает в браузере: логин Xiaomi + согласие."""
    return (
        "https://account.xiaomi.com/oauth2/authorize"
        f"?client_id={CLIENT_ID}&pt=1&response_type=code"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
    )


def extract_code(raw: str) -> str | None:
    """Достаёт OAuth-код из присланного текста: полного URL или самого кода."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "code=" in raw:
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        if parsed.get("code"):
            return parsed["code"][0]
        _, _, tail = raw.partition("code=")
        code = tail.split("&")[0].strip()
        return code or None
    # ссылка есть, а кода в ней нет — не выдаём URL за код
    if "://" in raw:
        return None
    # юзер мог скопировать только код
    if " " not in raw and len(raw) >= 16:
        return raw
    return None


def exchange_code(code: str) -> tuple[str, str]:
    """OAuth-код Xiaomi → (zepp_user_id, app_token). Пароль не нужен."""
    form = {
        "app_name": "com.xiaomi.hm.health",
        "app_version": "6.14.0",
        "code": code,
        "country_code": "RU",
        "device_id": str(uuid.uuid4()),
        "device_model": "phone",
        "dn": "api-mifit.zepp.com",
        "grant_type": "request_token",
        "third_name": "xiaomi-hm-mifit",
    }
    resp = requests.post(
        LOGIN_URL, data=urllib.parse.urlencode(form),
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": AGENT},
        timeout=TIMEOUT,
    )
    try:
        js = resp.json()
    except ValueError:
        raise ZeppAuthError("Zepp вернул неразборчивый ответ")
    info = js.get("token_info") or {}
    user_id, app_token = info.get("user_id"), info.get("app_token")
    if not user_id or not app_token:
        raise ZeppAuthError(f"Zepp не выдал токен (result={js.get('result')})")
    return str(user_id), app_token


def fetch_records(zepp_user_id: str, app_token: str, limit_days: int | None = None) -> list[dict[str, Any]]:
    """Измерения основного профиля весов, от новых к старым.

    Возвращает готовые к записи словари. weightType != 0 — служебные записи
    со сломанными значениями, отбрасываем.
    """
    out: list[dict[str, Any]] = []
    cutoff = time.time() - limit_days * 86400 if limit_days else None
    ts = int(time.time())
    seen_pages = 0
    while ts and ts > 0 and seen_pages < 20:
        resp = requests.get(
            f"{API_HOST}/users/{zepp_user_id}/members/-1/weightRecords",
            params={"limit": 200, "toTime": ts},
            headers={"apptoken": app_token}, timeout=TIMEOUT,
        )
        if resp.status_code in (401, 403):
            raise ZeppAuthError("Токен Zepp недействителен")
        resp.raise_for_status()
        js = resp.json()
        items = js.get("items") or []
        if not items:
            break
        for rec in items:
            if rec.get("weightType") != 0:
                continue
            generated = rec.get("generatedTime") or 0
            if cutoff and generated < cutoff:
                return out
            parsed = _parse_record(rec, generated)
            if parsed:
                out.append(parsed)
        nxt = js.get("next")
        if not nxt or nxt >= ts:
            break
        ts = nxt
        seen_pages += 1
    return out


def _parse_record(rec: dict, generated: int) -> dict[str, Any] | None:
    s = rec.get("summary") or {}
    weight = s.get("weight")
    if not weight:
        return None
    muscle_kg = s.get("muscleRate")  # именно килограммы, см. шапку модуля
    fat_pct = s.get("fatRate") or None
    # fatRate == 0 — весы не сняли импеданс, состава тела в записи нет
    return {
        "day": time.strftime("%Y-%m-%d", time.localtime(generated)),
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(generated)),
        "weight": weight,
        "bmi": s.get("bmi"),
        "fat_pct": fat_pct,
        "water_pct": s.get("bodyWaterRate"),
        "muscle_kg": muscle_kg,
        "muscle_pct": round(muscle_kg / weight * 100, 2) if muscle_kg else None,
        "bone_kg": s.get("boneMass"),
        "protein_pct": s.get("proteinRatio"),
        "visceral_fat": s.get("visceralFat"),
        "bmr_kcal": s.get("metabolism"),
        "body_score": s.get("bodyScore"),
        "body_age": s.get("muscleAge"),
        "impedance": s.get("impedance"),
    }


def composition_is_consistent(rec: dict[str, Any]) -> bool:
    """Мышцы + жир + кости ≈ вес. Страховка от смены единиц на стороне Zepp."""
    weight, fat, muscle, bone = (rec.get("weight"), rec.get("fat_pct"),
                                 rec.get("muscle_kg"), rec.get("bone_kg"))
    if not all((weight, fat, muscle, bone)):
        return True  # нечего проверять — не считаем ошибкой
    return abs(muscle + weight * fat / 100 + bone - weight) <= 2.0
