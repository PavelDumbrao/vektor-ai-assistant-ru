"""Secure Maton onboarding through the owner's Telegram DM.

The secret path is deliberately handled by ``pre_gateway_dispatch`` before
gateway auth, logging, session persistence, or LLM dispatch.  The hook checks
the normal Hermes allowlist itself, deletes the Telegram message first, then
validates and stores the key.  No secret-bearing value is returned or logged.
"""

from __future__ import annotations

import asyncio
import dataclasses
import http.client
import logging
import os
import re
import tempfile
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

_KEY_ENV = "MCP_MATON_API_KEY"
_MATON_CHECK_HOST = "api.maton.ai"
_MATON_CHECK_PATH = "/connections?limit=1"
_PENDING_TTL_SECONDS = 10 * 60
_MAX_ATTEMPTS_PER_MINUTE = 5
_COMMANDS = {"maton", "maton-key", "maton_key"}
_SECRET_COMMANDS = {"maton-key", "maton_key"}
_SAFE_KEY_RE = re.compile(r"^[\x21-\x7e]{20,512}$")

_pending: dict[tuple[str, str, str], float] = {}
_attempts: dict[tuple[str, str, str], deque[float]] = defaultdict(deque)
_tasks: set[asyncio.Task] = set()
_state_lock = threading.Lock()


def _get_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _source_key(source: Any) -> tuple[str, str, str]:
    platform = getattr(getattr(source, "platform", None), "value", None)
    return (
        str(platform or ""),
        str(getattr(source, "chat_id", "") or ""),
        str(getattr(source, "user_id", "") or ""),
    )


def _command_parts(text: str) -> tuple[str, str]:
    stripped = (text or "").lstrip()
    head, separator, tail = stripped.partition(" ")
    if not head.startswith("/"):
        return "", ""
    command = head[1:].split("@", 1)[0].lower()
    return command, tail.strip() if separator else ""


def _valid_key_shape(value: str) -> bool:
    return bool(_SAFE_KEY_RE.fullmatch(value or ""))


def _key_present() -> bool:
    env_path = _get_home() / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{_KEY_ENV}=") and line.partition("=")[2].strip():
                return True
    except OSError:
        pass
    return bool(os.environ.get(_KEY_ENV, "").strip())


def _is_authorized_dm(gateway: Any, source: Any) -> bool:
    platform = getattr(getattr(source, "platform", None), "value", None)
    if platform != "telegram" or getattr(source, "chat_type", None) != "dm":
        return False
    try:
        return bool(gateway._is_user_authorized(source))
    except Exception:
        return False


def _rate_limited(key: tuple[str, str, str], now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    with _state_lock:
        bucket = _attempts[key]
        while bucket and current - bucket[0] >= 60:
            bucket.popleft()
        if len(bucket) >= _MAX_ATTEMPTS_PER_MINUTE:
            return True
        bucket.append(current)
    return False


def _set_pending(key: tuple[str, str, str], now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    with _state_lock:
        _pending[key] = current + _PENDING_TTL_SECONDS


def _pop_pending(key: tuple[str, str, str], now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    with _state_lock:
        expires = _pending.get(key, 0)
        if expires <= current:
            _pending.pop(key, None)
            return False
        _pending.pop(key, None)
        return True


def _clear_pending(key: tuple[str, str, str]) -> None:
    with _state_lock:
        _pending.pop(key, None)


def _validate_remote(value: str) -> str:
    connection = http.client.HTTPSConnection(_MATON_CHECK_HOST, timeout=15)
    try:
        connection.request(
            "GET",
            _MATON_CHECK_PATH,
            headers={
                "Authorization": f"Bearer {value}",
                "Accept": "application/json",
                "User-Agent": "Vektor-Maton-Onboarding/1.0",
            },
        )
        response = connection.getresponse()
        status = int(response.status)
        if 200 <= status < 300:
            return "valid"
        return "invalid" if status == 401 else "temporary"
    except (OSError, TimeoutError, http.client.HTTPException):
        return "temporary"
    finally:
        connection.close()


def _atomic_store_secret(value: str) -> None:
    home = _get_home().resolve()
    env_path = home / ".env"
    if env_path.is_symlink():
        raise RuntimeError("secret_file_unsafe")
    if env_path.exists():
        info = env_path.stat()
        if not env_path.is_file() or info.st_uid != os.geteuid():
            raise RuntimeError("secret_file_unsafe")
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        home.mkdir(parents=True, exist_ok=True)
        lines = []

    output: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(f"{_KEY_ENV}="):
            if not replaced:
                output.append(f"{_KEY_ENV}={value}")
                replaced = True
            continue
        output.append(line)
    if not replaced:
        if output and output[-1] != "":
            output.append("")
        output.append(f"{_KEY_ENV}={value}")
    output.append("")

    fd, temp_name = tempfile.mkstemp(prefix=".env.maton.", dir=home)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, env_path)
        os.chmod(env_path, 0o600)
        directory_fd = os.open(home, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    os.environ[_KEY_ENV] = value


def _enable_maton_config() -> None:
    from utils import atomic_roundtrip_yaml_update

    config_path = _get_home() / "config.yaml"
    if config_path.is_symlink() or not config_path.is_file():
        raise RuntimeError("maton_config_missing")
    atomic_roundtrip_yaml_update(config_path, "mcp_servers.maton.enabled", True)
    os.chmod(config_path, 0o600)


def _provision_key(value: str) -> str:
    if not _valid_key_shape(value):
        return "invalid"
    validation = _validate_remote(value)
    if validation != "valid":
        return validation
    _atomic_store_secret(value)
    _enable_maton_config()
    return "valid"


def _adapter_for(gateway: Any, source: Any) -> Any | None:
    try:
        adapter = gateway._adapter_for_source(source)
        if adapter is not None:
            return adapter
    except Exception:
        pass
    try:
        return gateway.adapters.get(source.platform)
    except Exception:
        return None


async def _send(adapter: Any, chat_id: str, text: str) -> None:
    if adapter is None:
        return
    try:
        await adapter.send(chat_id, text)
    except Exception:
        logger.warning("Maton onboarding response failed")


async def _reload_maton(gateway: Any, event: Any) -> bool:
    sanitized_event = dataclasses.replace(event, text="/maton")
    try:
        reload_fn = getattr(gateway, "_execute_mcp_reload", None)
        if callable(reload_fn):
            await reload_fn(sanitized_event)
        else:
            from tools.mcp_tool import discover_mcp_tools, shutdown_mcp_servers

            await asyncio.to_thread(shutdown_mcp_servers)
            await asyncio.to_thread(discover_mcp_tools)
        from tools.mcp_tool import get_registered_mcp_server_names

        return "maton" in get_registered_mcp_server_names()
    except Exception:
        logger.warning("Maton MCP reload failed")
        return False


async def _handle_key_message(gateway: Any, event: Any, value: str) -> None:
    source = event.source
    adapter = _adapter_for(gateway, source)
    deleted = False
    try:
        if adapter is not None and event.message_id:
            deleted = bool(
                await adapter.delete_message(source.chat_id, str(event.message_id))
            )
    except Exception:
        deleted = False
    if not deleted:
        await _send(
            adapter,
            source.chat_id,
            "⚠️ Не удалось удалить сообщение с ключом. Я не сохранил его. "
            "Удалите сообщение вручную и повторите /maton.",
        )
        return

    await _send(adapter, source.chat_id, "🔐 Ключ удалён из чата. Проверяю Maton…")
    result = await asyncio.to_thread(_provision_key, value)
    if result == "invalid":
        await _send(
            adapter,
            source.chat_id,
            "❌ Maton отклонил ключ. Откройте Maton → Settings, скопируйте "
            "актуальный API key и снова отправьте /maton.",
        )
        return
    if result != "valid":
        await _send(
            adapter,
            source.chat_id,
            "⚠️ Maton временно недоступен. Ключ не сохранён. Повторите /maton позже.",
        )
        return

    connected = await _reload_maton(gateway, event)
    if connected:
        await _send(
            adapter,
            source.chat_id,
            "✅ Maton подключён. Теперь напишите, например: «Подключи Google Drive». "
            "Я создам безопасную ссылку авторизации и дождусь статуса ACTIVE.",
        )
    else:
        await _send(
            adapter,
            source.chat_id,
            "✅ Ключ Maton проверен и сохранён. Для применения отправьте /reload-mcp "
            "или перезапустите Вектор, затем напишите: «Подключи Google Drive».",
        )


async def _prompt_for_key(gateway: Any, event: Any) -> None:
    source = event.source
    adapter = _adapter_for(gateway, source)
    if _key_present():
        await _send(
            adapter,
            source.chat_id,
            "✅ Ключ Maton уже сохранён. Напишите, какой сервис подключить: "
            "Google Drive, Gmail, Notion, Slack или другой.",
        )
        return
    _set_pending(_source_key(source))
    await _send(
        adapter,
        source.chat_id,
        "Пришлите API key Maton следующим отдельным сообщением. Я перехвачу его "
        "до LLM и логов, удалю сообщение, проверю ключ и включу Maton. "
        "Ожидание действует 10 минут. Для отмены: /maton_cancel.",
    )


async def _reject_secret(gateway: Any, event: Any) -> None:
    source = event.source
    adapter = _adapter_for(gateway, source)
    try:
        if adapter is not None and event.message_id:
            await adapter.delete_message(source.chat_id, str(event.message_id))
    except Exception:
        pass
    await _send(adapter, source.chat_id, "⛔ Ключ Maton принимается только от владельца в личном чате.")


def _track_task(task: asyncio.Task) -> None:
    _tasks.add(task)

    def _done(completed: asyncio.Task) -> None:
        _tasks.discard(completed)
        try:
            completed.result()
        except Exception:
            logger.warning("Maton onboarding task failed")

    task.add_done_callback(_done)


def _schedule(coro: Any) -> bool:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        return False
    _track_task(task)
    return True


def _on_pre_gateway_dispatch(event: Any, gateway: Any, **_: Any) -> dict[str, str] | None:
    text = str(getattr(event, "text", "") or "")
    command, command_arg = _command_parts(text)
    source = getattr(event, "source", None)
    source_key = _source_key(source)
    platform = source_key[0]

    is_maton_command = command in _COMMANDS or command == "maton_cancel"
    pending_secret = False
    if platform == "telegram" and not command:
        pending_secret = _pop_pending(source_key)
    if not is_maton_command and not pending_secret:
        return None

    authorized = _is_authorized_dm(gateway, source)
    if not authorized:
        if command in _SECRET_COMMANDS or pending_secret:
            _schedule(_reject_secret(gateway, event))
            return {"action": "skip", "reason": "maton-secret-rejected"}
        return None

    if command == "maton_cancel":
        _clear_pending(source_key)
        adapter = _adapter_for(gateway, source)
        _schedule(_send(adapter, source.chat_id, "Ввод ключа Maton отменён."))
        return {"action": "skip", "reason": "maton-onboarding-cancelled"}

    if command == "maton":
        _schedule(_prompt_for_key(gateway, event))
        return {"action": "skip", "reason": "maton-onboarding-prompt"}

    value = command_arg if command in _SECRET_COMMANDS else text.strip()
    if _rate_limited(source_key):
        adapter = _adapter_for(gateway, source)
        _schedule(
            _send(adapter, source.chat_id, "Слишком много попыток. Подождите минуту и повторите /maton.")
        )
        return {"action": "skip", "reason": "maton-onboarding-rate-limit"}
    _schedule(_handle_key_message(gateway, event, value))
    return {"action": "skip", "reason": "maton-secret-consumed"}


def _maton_cli_command(_raw_args: str) -> str:
    if _key_present():
        return "Maton key is configured. In Telegram, ask the assistant which service to connect."
    return "Maton key is not configured. In the owner's Telegram DM, send /maton."


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    ctx.register_command(
        "maton",
        _maton_cli_command,
        description="Безопасно подключить Maton и внешние сервисы",
    )
