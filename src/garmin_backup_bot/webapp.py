from __future__ import annotations

import html
import time
from collections import deque
from threading import Lock

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .config import load_settings
from .crypto import SecretBox
from .storage import Storage

app = FastAPI(title="Garmin Backup WebApp")
load_dotenv()
_settings = load_settings()
_storage = Storage(_settings.db_path)
_box = SecretBox(_settings.encryption_key)

# --- Защита публичных эндпоинтов ---------------------------------------------
_MAX_BODY = 64 * 1024          # 64 KB — форма крошечная, больше не нужно
_RATE_LIMIT = 20               # запросов с одного IP
_RATE_WINDOW = 60.0            # за окно, сек
_rate_hits: dict[str, deque] = {}
_rate_lock = Lock()


def _client_ip(request: Request) -> str:
    # За nginx реальный IP — в X-Forwarded-For (первый элемент).
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate(request: Request) -> None:
    ip = _client_ip(request)
    now = time.monotonic()
    with _rate_lock:
        dq = _rate_hits.setdefault(ip, deque())
        while dq and dq[0] < now - _RATE_WINDOW:
            dq.popleft()
        if len(dq) >= _RATE_LIMIT:
            raise HTTPException(status_code=429, detail="Too many requests")
        dq.append(now)


@app.middleware("http")
async def _limit_body(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > _MAX_BODY:
                return PlainTextResponse("Payload too large", status_code=413)
        except ValueError:
            pass
    return await call_next(request)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/connect", response_class=HTMLResponse)
def connect(request: Request, token: str = Query(..., min_length=8, max_length=128)) -> str:
    _check_rate(request)
    safe_token = html.escape(token, quote=True)
    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Connect Garmin</title>
  <style>
    :root {{
      --bg: #f7f4ee;
      --card: #fffdfa;
      --text: #1f1a14;
      --muted: #6b6257;
      --accent: #0f766e;
      --accent-2: #115e59;
      --border: #e8dfd1;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: 'Manrope', 'Segoe UI', sans-serif;
      color: var(--text);
      background: radial-gradient(circle at 15% 20%, #efe7d8, transparent 45%),
                  radial-gradient(circle at 85% 10%, #d5ece8, transparent 40%),
                  var(--bg);
      display: grid;
      place-items: center;
      padding: 16px;
    }}
    .card {{
      width: 100%;
      max-width: 420px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 20px;
      box-shadow: 0 8px 30px rgba(31,26,20,.08);
      animation: rise .35s ease-out;
    }}
    @keyframes rise {{
      from {{ opacity: 0; transform: translateY(8px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    h1 {{ margin: 0 0 10px; font-size: 1.3rem; }}
    p {{ margin: 0 0 16px; color: var(--muted); font-size: .95rem; }}
    label {{ display: block; margin: 12px 0 6px; font-weight: 600; font-size: .9rem; }}
    input {{
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 12px;
      font-size: 1rem;
      outline: none;
      background: #fff;
    }}
    input:focus {{ border-color: var(--accent); }}
    button {{
      margin-top: 16px;
      width: 100%;
      border: none;
      border-radius: 10px;
      padding: 11px 14px;
      color: #fff;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      font-weight: 700;
      cursor: pointer;
    }}
    .status {{ margin-top: 10px; font-size: .9rem; color: var(--muted); }}
  </style>
</head>
<body>
  <main class=\"card\">
    <h1>Garmin Connect</h1>
    <p>Данные уйдут в бота и сохранятся на сервере в зашифрованном виде.</p>

    <form id=\"garmin-form\">
      <input id=\"token\" name=\"token\" type=\"hidden\" value=\"{safe_token}\" />
      <label for=\"username\">Username / Email</label>
      <input id=\"username\" name=\"username\" autocomplete=\"username\" required />

      <label for=\"password\">Password</label>
      <input id=\"password\" name=\"password\" type=\"password\" autocomplete=\"current-password\" required />

      <button type=\"submit\">Save Securely</button>
      <div id=\"status\" class=\"status\"></div>
    </form>
  </main>

<script>
  const form = document.getElementById('garmin-form');
  const statusEl = document.getElementById('status');

  form.addEventListener('submit', async function(event) {{
    event.preventDefault();

    const token = document.getElementById('token').value;
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;

    if (!username || !password) {{
      statusEl.textContent = 'Fill all fields.';
      return;
    }}

    statusEl.textContent = 'Saving...';
    try {{
      const resp = await fetch('submit', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ token, username, password }}),
      }});
      const data = await resp.json();
      if (!resp.ok || !data.ok) {{
        statusEl.textContent = data.message || 'Failed to save.';
        return;
      }}
      statusEl.textContent = 'Saved. Return to Telegram and run /backup.';
    }} catch (err) {{
      statusEl.textContent = 'Network error. Try again.';
    }}
  }});
</script>
</body>
</html>
"""


class SubmitPayload(BaseModel):
    token: str = Field(min_length=8, max_length=128)
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=256)


@app.post("/submit")
def submit(payload: SubmitPayload, request: Request) -> dict[str, object]:
    _check_rate(request)
    token = payload.token.strip()
    username = payload.username.strip()
    password = payload.password
    if not token or not username or not password:
        return {"ok": False, "message": "Missing fields"}
    user_id = _storage.consume_web_token(token)
    if not user_id:
        return {"ok": False, "message": "Token expired or invalid. Re-run /link_garmin."}
    encrypted = _box.encrypt(password)
    _storage.upsert_credentials(user_id=user_id, username=username, password_encrypted=encrypted)
    return {"ok": True, "message": "Saved"}
