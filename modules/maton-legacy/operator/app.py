"""One-time HTTPS onboarding portal for a single Hermes client."""

from __future__ import annotations

import html
import json
import os
import secrets
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from install_maton_client import ProvisionError, activate_with_key, restore_from_backup
from portal_store import (
    SessionError,
    begin_attempt,
    finish_attempt,
    issue_csrf,
    read_session,
)


SETTINGS_PATH = Path(os.environ.get("MATON_PORTAL_SETTINGS", Path(__file__).with_name("settings.json")))
SETTINGS = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
DB_PATH = SETTINGS["db_path"]
MAX_ATTEMPTS = int(SETTINGS["max_attempts"])
ALLOWED_HOST = urllib.parse.urlparse(SETTINGS["public_base_url"]).hostname or ""

@asynccontextmanager
async def lifespan(_app: FastAPI):
    if SETTINGS.get("preflight_runtime", True):
        _inspect_hermes_pid()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[ALLOWED_HOST, "localhost", "127.0.0.1"])


SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=31536000",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}


def _page(*, title: str, body: str, status_code: int = 200) -> HTMLResponse:
    document = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light; --bg:#eef3ee; --card:#fff; --text:#17211b;
      --muted:#5d6a62; --line:#d6e0d8; --accent:#188755; --accent2:#11663f;
      --danger:#b42318; --ok:#067647; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; min-height:100vh; background:linear-gradient(160deg,#e2f3e8 0%,var(--bg) 48%,#f7f4ea 100%);
      color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ width:min(100% - 32px,520px); margin:0 auto; padding:40px 0; min-height:100vh;
      display:grid; align-items:center; }}
    .card {{ background:var(--card); border:1px solid var(--line); border-radius:24px; padding:28px;
      box-shadow:0 18px 48px rgba(25,55,38,.10); }}
    .brand {{ display:flex; align-items:center; gap:12px; margin-bottom:28px; color:var(--muted); font-weight:650; }}
    .mark {{ display:grid; place-items:center; width:44px; height:44px; border-radius:15px;
      background:#183d2b; color:#fff; font-size:20px; font-weight:800; }}
    h1 {{ margin:0 0 12px; font-size:clamp(1.7rem,7vw,2.25rem); line-height:1.08; letter-spacing:-.035em; }}
    p {{ margin:0 0 20px; line-height:1.55; color:var(--muted); }}
    label {{ display:block; margin:24px 0 8px; font-weight:700; }}
    input {{ width:100%; min-height:52px; border:1px solid #aab8ae; border-radius:13px; padding:12px 14px;
      color:var(--text); background:#fff; font:inherit; }}
    input:focus-visible,button:focus-visible,a:focus-visible {{ outline:3px solid rgba(24,135,85,.28); outline-offset:2px; }}
    button {{ width:100%; min-height:52px; margin-top:18px; border:0; border-radius:13px;
      background:var(--accent); color:#fff; font:700 1rem/1 inherit; cursor:pointer; }}
    button:hover {{ background:var(--accent2); }} button:active {{ transform:translateY(1px); }}
    .hint {{ font-size:.9rem; margin-top:10px; }}
    .notice {{ border-radius:13px; padding:13px 14px; margin:18px 0; line-height:1.45; }}
    .error {{ color:var(--danger); background:#fff1f0; border:1px solid #f5c2bd; }}
    .success {{ color:var(--ok); background:#ecfdf3; border:1px solid #abefc6; }}
    .secure {{ margin-top:22px; padding-top:18px; border-top:1px solid var(--line); font-size:.86rem; color:var(--muted); }}
    a {{ color:var(--accent2); font-weight:650; }}
    @media (max-width:420px) {{ main {{ width:min(100% - 20px,520px); padding:10px 0; }} .card {{ padding:22px; border-radius:20px; }} }}
    @media (prefers-reduced-motion:reduce) {{ * {{ scroll-behavior:auto!important; transition:none!important; }} }}
  </style>
</head>
<body><main><section class="card" aria-labelledby="page-title">
  <div class="brand"><span class="mark" aria-hidden="true">AI</span><span>Защищённое подключение</span></div>
  {body}
</section></main></body></html>"""
    return HTMLResponse(document, status_code=status_code, headers=SECURITY_HEADERS)


def _form(token: str, csrf: str, error: str | None = None) -> HTMLResponse:
    error_html = (
        f'<div class="notice error" role="alert">{html.escape(error)}</div>' if error else ""
    )
    body = f"""
<h1 id="page-title">Подключить Maton</h1>
<p>Вставьте личный API-ключ Maton. Мы сразу проверим его и подключим сервис к вашему AI-ассистенту.</p>
{error_html}
<form method="post" action="/connect/{html.escape(token)}" autocomplete="off">
  <input type="hidden" name="csrf" value="{html.escape(csrf)}">
  <label for="api-key">API-ключ Maton</label>
  <input id="api-key" name="api_key" type="password" required minlength="20" maxlength="8192"
    autocomplete="off" autocapitalize="none" spellcheck="false" aria-describedby="key-help">
  <p class="hint" id="key-help">Ключ не сохраняется в браузере и не отправляется в Telegram.</p>
  <button type="submit">Проверить и подключить</button>
</form>
<p class="secure">Нет ключа? Откройте <a href="https://maton.ai/settings" rel="noreferrer noopener" target="_blank">настройки Maton</a>, создайте ключ и вернитесь сюда. Ссылка одноразовая.</p>"""
    response = _page(title="Подключение Maton", body=body)
    response.set_cookie(
        "maton_csrf",
        csrf,
        max_age=int(SETTINGS["ttl_seconds"]),
        httponly=True,
        secure=True,
        samesite="strict",
        path=f"/connect/{token}",
    )
    return response


def _public_error(exc: ProvisionError) -> str:
    value = str(exc).lower()
    if "rejected" in value or "format" in value:
        return "Maton не принял этот API-ключ. Скопируйте актуальный ключ целиком и повторите."
    if "unavailable" in value or "http" in value:
        return "Maton временно не отвечает. Ключ не сохранён — попробуйте ещё раз немного позже."
    if "mcp" in value:
        return "Ключ верный, но контрольная проверка Maton не прошла. Изменения отменены."
    return "Не удалось завершить подключение. Изменения отменены; попробуйте ещё раз."


def _read_env_value(path: Path, name: str) -> str:
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip() and not raw.lstrip().startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    return ""


def _smoke_test_mcp() -> None:
    env = os.environ.copy()
    env.update({"HOME": SETTINGS["linux_home"], "HERMES_HOME": SETTINGS["hermes_home"]})
    try:
        result = subprocess.run(
            [SETTINGS["hermes_python"], "-m", "hermes_cli.main", "mcp", "test", "maton"],
            cwd=SETTINGS["hermes_agent_dir"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=75,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvisionError("Maton MCP connection test could not run") from exc
    if result.returncode != 0:
        raise ProvisionError("Maton MCP did not pass the connection test")


def _inspect_hermes_pid() -> int:
    service = SETTINGS["hermes_service"]
    try:
        result = subprocess.run(
            ["systemctl", "show", service, "--property=MainPID", "--value"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvisionError("Hermes service state is unavailable") from exc
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        raise ProvisionError("Hermes service PID is unavailable")
    old_pid = int(result.stdout.strip())
    if old_pid <= 1:
        raise ProvisionError("Hermes service is not running")
    try:
        status = Path(f"/proc/{old_pid}/status").read_text(encoding="utf-8")
        cmdline = Path(f"/proc/{old_pid}/cmdline").read_bytes().replace(b"\0", b" ")
        uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
        real_uid = int(uid_line.split()[1]) if uid_line else -1
    except (OSError, ValueError, IndexError) as exc:
        raise ProvisionError("Hermes process identity is unavailable") from exc
    if real_uid != os.getuid() or b"hermes_cli.main gateway run" not in cmdline:
        raise ProvisionError("Hermes process identity check failed")
    return old_pid


def _restart_hermes() -> None:
    service = SETTINGS["hermes_service"]
    old_pid = _inspect_hermes_pid()
    try:
        os.kill(old_pid, signal.SIGTERM)
    except OSError as exc:
        raise ProvisionError("Hermes restart could not be requested") from exc
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            check = subprocess.run(
                ["systemctl", "show", service, "--property=MainPID", "--property=ActiveState"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProvisionError("Hermes restart status is unavailable") from exc
        fields = dict(
            line.split("=", 1) for line in check.stdout.splitlines() if "=" in line
        )
        if fields.get("ActiveState") == "active" and fields.get("MainPID", "0").isdigit() \
                and int(fields["MainPID"]) not in {0, old_pid}:
            return
    raise ProvisionError("Hermes restart did not complete in time")


def _notify_telegram(chat_id: str) -> None:
    token = _read_env_value(Path(SETTINGS["hermes_home"]) / ".env", "TELEGRAM_BOT_TOKEN")
    if not token:
        raise ProvisionError("Telegram bot token is unavailable")
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": (
                "✅ Maton подключён и проверен.\n\n"
                "Какие сервисы хотите подключить? Например: Google Drive, Gmail, "
                "Calendar, Notion или CRM."
            ),
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read(65_536))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ProvisionError("Telegram confirmation could not be delivered") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ProvisionError("Telegram rejected the confirmation message")


@app.middleware("http")
async def secure_transport(request: Request, call_next):
    if request.url.path != "/healthz" and request.headers.get("x-forwarded-proto") != "https":
        return JSONResponse({"detail": "HTTPS required"}, status_code=400, headers=SECURITY_HEADERS)
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@app.get("/connect/{token}")
async def connect_form(token: str):
    session = read_session(DB_PATH, token)
    now = int(time.time())
    if session is None or session.expires_at < now or session.status in {"expired", "locked"}:
        return _page(
            title="Ссылка недоступна",
            status_code=410,
            body='<h1 id="page-title">Ссылка недоступна</h1><p>Запросите у ассистента новую ссылку подключения Maton.</p>',
        )
    if session.status == "used":
        return _page(
            title="Maton подключён",
            body='<h1 id="page-title">Maton уже подключён</h1><div class="notice success">Вернитесь в Telegram и выберите нужный сервис.</div>',
        )
    if session.status == "processing":
        return _page(
            title="Проверяем подключение",
            body='<h1 id="page-title">Проверяем подключение</h1><p>Подождите немного и вернитесь в Telegram.</p>',
        )
    try:
        csrf = issue_csrf(DB_PATH, token)
    except SessionError:
        return _page(title="Ссылка недоступна", body='<h1 id="page-title">Ссылка недоступна</h1>', status_code=410)
    return _form(token, csrf)


@app.post("/connect/{token}")
async def connect_submit(token: str, request: Request):
    content_length = request.headers.get("content-length", "0")
    if not content_length.isdigit() or int(content_length) > 9_000:
        return _page(title="Ошибка", body='<h1 id="page-title">Слишком большой запрос</h1>', status_code=413)
    raw = await request.body()
    if len(raw) > 9_000:
        return _page(title="Ошибка", body='<h1 id="page-title">Слишком большой запрос</h1>', status_code=413)
    try:
        fields = urllib.parse.parse_qs(raw.decode("utf-8", "strict"), keep_blank_values=True)
    except UnicodeDecodeError:
        return _page(title="Ошибка", body='<h1 id="page-title">Некорректный запрос</h1>', status_code=400)
    key = (fields.get("api_key") or [""])[0].strip()
    csrf = (fields.get("csrf") or [""])[0]
    cookie_csrf = request.cookies.get("maton_csrf", "")
    if not csrf or not cookie_csrf or not secrets.compare_digest(csrf, cookie_csrf):
        return _page(
            title="Ошибка проверки",
            body='<h1 id="page-title">Проверка формы не пройдена</h1><p>Откройте ссылку из Telegram заново.</p>',
            status_code=400,
        )
    try:
        session = begin_attempt(
            DB_PATH, token=token, csrf=csrf, max_attempts=MAX_ATTEMPTS
        )
    except SessionError as exc:
        return _page(
            title="Ссылка недоступна",
            body=f'<h1 id="page-title">Ссылка недоступна</h1><p>{html.escape(str(exc))}</p>',
            status_code=410,
        )
    try:
        backup_dir = activate_with_key(
            hermes_home=SETTINGS["hermes_home"],
            owner=SETTINGS["owner"],
            key=key,
            timeout=15,
        )
        key = ""
        _restart_hermes()
        _smoke_test_mcp()
        finish_attempt(DB_PATH, token, success=True, max_attempts=MAX_ATTEMPTS)
        try:
            _notify_telegram(session.chat_id)
        except ProvisionError:
            pass
        response = _page(
            title="Maton подключён",
            body=(
                '<h1 id="page-title">Готово — Maton подключён</h1>'
                '<div class="notice success" role="status">Ключ проверен, а соединение с Maton работает.</div>'
                '<p>Вернитесь в Telegram: ассистент уже готов подключать Google Drive, Gmail, Calendar, Notion, CRM и другие сервисы.</p>'
            ),
        )
        response.delete_cookie("maton_csrf", path=f"/connect/{token}")
        return response
    except Exception as exc:
        public_exc = exc if isinstance(exc, ProvisionError) else ProvisionError(
            "Internal activation failure"
        )
        key = ""
        if "backup_dir" in locals():
            try:
                restore_from_backup(
                    hermes_home=SETTINGS["hermes_home"],
                    backup_dir=backup_dir,
                    owner=SETTINGS["owner"],
                )
                _restart_hermes()
            except ProvisionError:
                pass
        finish_attempt(DB_PATH, token, success=False, max_attempts=MAX_ATTEMPTS)
        current = read_session(DB_PATH, token)
        if current and current.status == "pending":
            new_csrf = issue_csrf(DB_PATH, token)
            return _form(token, new_csrf, _public_error(public_exc))
        return _page(
            title="Подключение не выполнено",
            body='<h1 id="page-title">Подключение не выполнено</h1><p>Запросите у ассистента новую ссылку и проверьте ключ.</p>',
            status_code=400,
        )
