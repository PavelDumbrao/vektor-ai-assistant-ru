"""Secret-safe Maton credential capture for the Hermes Telegram gateway."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


logger = logging.getLogger(__name__)

ENV_KEY = "MCP_MATON_API_KEY"
DEFAULT_TTL_SECONDS = 600
DEFAULT_GUARD_SECONDS = 86_400
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_SMOKE_TIMEOUT_SECONDS = 75
_COMMAND_RE = re.compile(r"^/maton(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$", re.IGNORECASE)
_ACTION_COMMAND_RE = re.compile(
    r"^/maton_(cancel|status|reconnect)(?:@[A-Za-z0-9_]+)?$",
    re.IGNORECASE,
)


class ChatActivationError(RuntimeError):
    """A public-safe activation failure category."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class ChatSettings:
    owner: str
    linux_home: str
    hermes_home: str
    hermes_python: str
    hermes_agent_dir: str
    activation_module: str
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    guard_seconds: int = DEFAULT_GUARD_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    smoke_timeout_seconds: int = DEFAULT_SMOKE_TIMEOUT_SECONDS
    portal_base_url: str = ""


def load_settings(path: str | Path) -> ChatSettings:
    settings_path = Path(path)
    if settings_path.is_symlink() or not settings_path.is_file():
        raise RuntimeError("Maton chat onboarding settings are missing or unsafe")
    raw = json.loads(settings_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("Maton chat onboarding settings must be an object")
    required = (
        "owner",
        "linux_home",
        "hermes_home",
        "hermes_python",
        "hermes_agent_dir",
        "activation_module",
    )
    if any(not isinstance(raw.get(key), str) or not raw[key] for key in required):
        raise RuntimeError("Maton chat onboarding settings are incomplete")
    values = dict(raw)
    values["ttl_seconds"] = max(60, min(int(raw.get("ttl_seconds", DEFAULT_TTL_SECONDS)), 3600))
    values["guard_seconds"] = max(3600, min(int(raw.get("guard_seconds", DEFAULT_GUARD_SECONDS)), 604_800))
    values["max_attempts"] = max(1, min(int(raw.get("max_attempts", DEFAULT_MAX_ATTEMPTS)), 10))
    values["smoke_timeout_seconds"] = max(
        15,
        min(int(raw.get("smoke_timeout_seconds", DEFAULT_SMOKE_TIMEOUT_SECONDS)), 180),
    )
    values["portal_base_url"] = str(raw.get("portal_base_url") or "")
    return ChatSettings(**{key: values[key] for key in ChatSettings.__dataclass_fields__})


class PendingStore:
    """Atomic credential-expectation state containing no credential values."""

    def __init__(self, path: str | Path, *, ttl_seconds: int, guard_seconds: int, max_attempts: int):
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds
        self.guard_seconds = guard_seconds
        self.max_attempts = max_attempts
        self._lock = threading.RLock()

    @staticmethod
    def identity_key(identity: tuple[str, ...]) -> str:
        payload = "\0".join(identity).encode("utf-8", "strict")
        return hashlib.sha256(payload).hexdigest()

    def _load(self) -> dict[str, Any]:
        if self.path.is_symlink():
            raise RuntimeError("Refusing symlinked Maton pending state")
        if not self.path.exists():
            return {"version": 1, "entries": {}}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("entries"), dict):
            raise RuntimeError("Invalid Maton pending state")
        return raw

    def _save(self, data: dict[str, Any]) -> None:
        if self.path.is_symlink():
            raise RuntimeError("Refusing symlinked Maton pending state")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temp = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            if temp.exists():
                temp.unlink()

    def start(self, identity: tuple[str, ...], *, now: int | None = None) -> None:
        current = int(time.time()) if now is None else now
        key = self.identity_key(identity)
        with self._lock:
            data = self._load()
            data["entries"][key] = {
                "status": "pending",
                "attempts": 0,
                "expires_at": current + self.ttl_seconds,
                "guard_until": current + self.guard_seconds,
            }
            self._save(data)

    def read(self, identity: tuple[str, ...], *, now: int | None = None) -> dict[str, Any] | None:
        current = int(time.time()) if now is None else now
        key = self.identity_key(identity)
        with self._lock:
            data = self._load()
            record = data["entries"].get(key)
            if not isinstance(record, dict):
                return None
            if int(record.get("guard_until", 0)) < current:
                data["entries"].pop(key, None)
                self._save(data)
                return None
            if (
                record.get("status") == "processing"
                and int(record.get("processing_until", 0)) < current
            ):
                record["status"] = "expired"
                data["entries"][key] = record
                self._save(data)
            if record.get("status") == "pending" and int(record.get("expires_at", 0)) < current:
                record["status"] = "expired"
                data["entries"][key] = record
                self._save(data)
            return dict(record)

    def claim(self, identity: tuple[str, ...], *, now: int | None = None) -> bool:
        current = int(time.time()) if now is None else now
        key = self.identity_key(identity)
        with self._lock:
            data = self._load()
            record = data["entries"].get(key)
            if not isinstance(record, dict):
                return False
            if record.get("status") != "pending" or int(record.get("expires_at", 0)) < current:
                return False
            record["status"] = "processing"
            record["attempts"] = int(record.get("attempts", 0)) + 1
            record["processing_until"] = current + 300
            data["entries"][key] = record
            self._save(data)
            return True

    def retry(self, identity: tuple[str, ...], *, now: int | None = None) -> bool:
        current = int(time.time()) if now is None else now
        key = self.identity_key(identity)
        with self._lock:
            data = self._load()
            record = data["entries"].get(key)
            if not isinstance(record, dict):
                return False
            can_retry = int(record.get("attempts", 0)) < self.max_attempts
            record["status"] = "pending" if can_retry else "locked"
            record["expires_at"] = current + self.ttl_seconds if can_retry else current
            record["guard_until"] = current + self.guard_seconds
            record.pop("processing_until", None)
            data["entries"][key] = record
            self._save(data)
            return can_retry

    def guard(self, identity: tuple[str, ...], status: str, *, now: int | None = None) -> None:
        current = int(time.time()) if now is None else now
        key = self.identity_key(identity)
        with self._lock:
            data = self._load()
            previous = data["entries"].get(key) or {}
            data["entries"][key] = {
                "status": status,
                "attempts": int(previous.get("attempts", 0)),
                "expires_at": current,
                "guard_until": current + self.guard_seconds,
            }
            self._save(data)

    def clear(self, identity: tuple[str, ...]) -> None:
        key = self.identity_key(identity)
        with self._lock:
            data = self._load()
            if key in data["entries"]:
                data["entries"].pop(key, None)
                self._save(data)


class ActivationBackend:
    """Validate, persist, smoke-test, and roll back a Maton configuration."""

    def __init__(self, settings: ChatSettings):
        self.settings = settings

    def _module(self):
        path = Path(self.settings.activation_module)
        if path.is_symlink() or not path.is_file():
            raise ChatActivationError("internal")
        name = f"maton_client_activation_{hashlib.sha256(str(path).encode()).hexdigest()[:12]}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ChatActivationError("internal")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _smoke_test(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "HOME": self.settings.linux_home,
                "HERMES_HOME": self.settings.hermes_home,
            }
        )
        try:
            result = subprocess.run(
                [
                    self.settings.hermes_python,
                    "-m",
                    "hermes_cli.main",
                    "mcp",
                    "test",
                    "maton",
                ],
                cwd=self.settings.hermes_agent_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.settings.smoke_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ChatActivationError("mcp") from exc
        if result.returncode != 0:
            raise ChatActivationError("mcp")

    def activate(self, secret: str) -> Path:
        stage = "load_module"
        module = None
        backup: Path | None = None
        try:
            module = self._module()
            stage = "validate_and_persist"
            backup = module.activate_with_key(
                hermes_home=self.settings.hermes_home,
                owner=self.settings.owner,
                key=secret,
                timeout=15,
            )
            stage = "mcp_smoke_test"
            self._smoke_test()
            return Path(backup)
        except ChatActivationError as exc:
            logger.warning(
                "Maton activation failed: stage=%s category=%s error_type=%s",
                stage,
                exc.category,
                type(exc).__name__,
            )
            if backup is not None and module is not None:
                module.restore_from_backup(
                    hermes_home=self.settings.hermes_home,
                    backup_dir=backup,
                    owner=self.settings.owner,
                )
            raise
        except Exception as exc:
            if backup is not None and module is not None:
                try:
                    module.restore_from_backup(
                        hermes_home=self.settings.hermes_home,
                        backup_dir=backup,
                        owner=self.settings.owner,
                    )
                except Exception:
                    logger.error("Maton rollback failed after activation error")
            text = str(exc).lower()
            if "rejected" in text or "format" in text:
                category = "invalid"
            elif "unavailable" in text or "http" in text or "timeout" in text:
                category = "unavailable"
            else:
                category = "internal"
            logger.error(
                "Maton activation failed: stage=%s category=%s error_type=%s",
                stage,
                category,
                type(exc).__name__,
            )
            raise ChatActivationError(category) from exc


def _platform_value(source: Any) -> str:
    value = getattr(source, "platform", "")
    return str(getattr(value, "value", value) or "").lower()


def _identity(source: Any) -> tuple[str, ...]:
    return (
        _platform_value(source),
        str(getattr(source, "profile", "") or "default"),
        str(getattr(source, "chat_id", "") or ""),
        str(getattr(source, "user_id", "") or ""),
        str(getattr(source, "thread_id", "") or ""),
    )


def _request_kind(text: str) -> str | None:
    cleaned = " ".join((text or "").strip().lower().replace("ё", "е").split())
    action_match = _ACTION_COMMAND_RE.fullmatch(cleaned)
    if action_match:
        return action_match.group(1).lower()
    match = _COMMAND_RE.fullmatch(cleaned)
    if match:
        arg = (match.group(1) or "").strip()
        if arg in {"cancel", "отмена", "отменить"}:
            return "cancel"
        if arg in {"status", "статус"}:
            return "status"
        if arg in {"reconnect", "replace", "переподключить", "заменить"}:
            return "reconnect"
        return "connect"
    if cleaned in {"maton", "матон"}:
        return "connect"
    service = r"(?:maton|матон)"
    if re.search(rf"\b(?:подключи|подключить|настрой|настроить)\b.*\b{service}\b", cleaned):
        return "connect"
    if re.search(rf"\b{service}\b.*\b(?:подключи|подключить|настрой|настроить)\b", cleaned):
        return "connect"
    if re.search(rf"\b(?:переподключи|переподключить|замени|заменить)\b.*\b{service}\b", cleaned):
        return "reconnect"
    return None


def _looks_like_credential(text: str) -> bool:
    value = (text or "").strip()
    return 20 <= len(value) <= 4096 and not any(char.isspace() for char in value)


class MatonChatController:
    def __init__(
        self,
        *,
        hermes_home: str | Path,
        settings: ChatSettings,
        backend: ActivationBackend | Any | None = None,
    ):
        self.hermes_home = Path(hermes_home)
        self.settings = settings
        self.store = PendingStore(
            self.hermes_home / "maton-chat-state" / "pending.json",
            ttl_seconds=settings.ttl_seconds,
            guard_seconds=settings.guard_seconds,
            max_attempts=settings.max_attempts,
        )
        self.backend = backend or ActivationBackend(settings)
        self._tasks: set[asyncio.Task] = set()

    def _is_authorized(self, gateway: Any, source: Any) -> bool:
        checker = getattr(gateway, "_is_user_authorized", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(source))
        except Exception:
            logger.warning("Maton onboarding could not verify gateway authorization")
            return False

    def _adapter(self, gateway: Any, source: Any):
        resolver = getattr(gateway, "_adapter_for_source", None)
        if callable(resolver):
            return resolver(source)
        return None

    def _schedule(self, coroutine) -> None:
        try:
            task = asyncio.create_task(coroutine)
        except RuntimeError:
            coroutine.close()
            logger.error("Maton onboarding could not schedule secure message handling")
            return
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except Exception:
            error = RuntimeError("unable to inspect task")
        if error is not None:
            logger.error("Maton onboarding background task failed: %s", type(error).__name__)

    async def wait_for_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def _send(self, gateway: Any, source: Any, text: str, *, reply_to: str | None = None) -> bool:
        adapter = self._adapter(gateway, source)
        if adapter is None:
            return False
        metadata = None
        metadata_builder = getattr(gateway, "_thread_metadata_for_source", None)
        if callable(metadata_builder):
            try:
                metadata = metadata_builder(source, reply_to)
            except Exception:
                metadata = None
        try:
            result = await adapter.send(
                str(getattr(source, "chat_id", "")),
                text,
                reply_to=reply_to,
                metadata=metadata,
            )
            return bool(getattr(result, "success", True))
        except Exception:
            logger.warning("Maton onboarding Telegram reply failed")
            return False

    async def _prompt(self, gateway: Any, source: Any, message_id: str | None) -> None:
        await self._send(
            gateway,
            source,
            (
                "🔐 Отправьте API-ключ Maton одним следующим сообщением.\n\n"
                "Я перехвачу его до ИИ и истории, сразу удалю сообщение, затем "
                "проверю ключ и подключение. Ключ действует только для вашего Maton.\n\n"
                f"Ожидание: {self.settings.ttl_seconds // 60} мин. Отмена: /maton_cancel"
            ),
            reply_to=message_id,
        )

    async def _delete(self, gateway: Any, source: Any, message_id: str | None) -> bool:
        adapter = self._adapter(gateway, source)
        if adapter is None or not message_id:
            return False
        try:
            return bool(
                await adapter.delete_message(
                    str(getattr(source, "chat_id", "")),
                    str(message_id),
                )
            )
        except Exception:
            logger.warning("Maton onboarding could not delete the credential message")
            return False

    async def _discard_guarded_message(
        self,
        gateway: Any,
        source: Any,
        message_id: str | None,
    ) -> None:
        deleted = await self._delete(gateway, source, message_id)
        await self._send(
            gateway,
            source,
            (
                "⏱ Запрос подключения Maton уже истёк. Сообщение с возможным ключом "
                + ("удалено." if deleted else "не удалось удалить — удалите его вручную.")
                + " Отправьте /maton и повторите."
            ),
        )

    async def _consume_secret(
        self,
        *,
        gateway: Any,
        source: Any,
        identity: tuple[str, ...],
        message_id: str | None,
        secret: str,
    ) -> None:
        deleted = await self._delete(gateway, source, message_id)
        if not deleted:
            try:
                self.store.guard(identity, "delete_failed")
            except Exception:
                logger.error("Maton onboarding could not persist delete-failure guard")
            await self._send(
                gateway,
                source,
                (
                    "⚠️ Не удалось удалить сообщение, поэтому ключ не использован и "
                    "не сохранён. Удалите сообщение вручную и отправьте /maton снова."
                ),
            )
            secret = ""
            return

        try:
            await asyncio.to_thread(self.backend.activate, secret)
        except ChatActivationError as exc:
            try:
                can_retry = self.store.retry(identity)
            except Exception:
                can_retry = False
                logger.error("Maton onboarding could not persist retry state")
            messages = {
                "invalid": "Maton не принял этот API-ключ. Скопируйте актуальный ключ целиком.",
                "unavailable": "Maton временно не отвечает. Ключ не сохранён.",
                "mcp": "Ключ принят, но контрольное MCP-подключение не прошло. Изменения отменены.",
                "internal": "Не удалось завершить подключение. Изменения отменены.",
            }
            suffix = (
                " Отправьте новый ключ следующим сообщением."
                if can_retry
                else " Лимит попыток исчерпан; отправьте /maton, чтобы начать заново."
            )
            await self._send(gateway, source, f"❌ {messages.get(exc.category, messages['internal'])}{suffix}")
            secret = ""
            return
        except Exception:
            try:
                self.store.retry(identity)
            except Exception:
                logger.error("Maton onboarding could not persist failure state")
            logger.error("Unexpected Maton chat activation failure", exc_info=True)
            await self._send(
                gateway,
                source,
                "❌ Не удалось завершить подключение. Ключ не сохранён; попробуйте ещё раз.",
            )
            secret = ""
            return

        secret = ""
        try:
            self.store.clear(identity)
        except Exception:
            logger.error("Maton onboarding could not clear completed pending state")
        await self._send(
            gateway,
            source,
            (
                "✅ Отлично, Maton подключён и проверен.\n\n"
                "Какие сервисы хотите подключить? Например: Google Drive, Gmail, "
                "Calendar, Notion или CRM."
            ),
        )
        restart = getattr(gateway, "request_restart", None)
        if callable(restart):
            try:
                restart()
            except Exception:
                logger.warning("Maton was activated but gateway restart request failed")

    def intercept(self, event: Any, gateway: Any) -> dict[str, str] | None:
        source = getattr(event, "source", None)
        if source is None or _platform_value(source) != "telegram":
            return None
        if not self._is_authorized(gateway, source):
            return None

        text = str(getattr(event, "text", "") or "").strip()
        kind = _request_kind(text)
        chat_type = str(getattr(source, "chat_type", "") or "").lower()
        message_id = str(getattr(event, "message_id", "") or "") or None

        if chat_type != "dm":
            if kind in {"connect", "reconnect", "status"}:
                self._schedule(
                    self._send(
                        gateway,
                        source,
                        "🔒 Подключение Maton доступно только в личном чате с ботом.",
                        reply_to=message_id,
                    )
                )
                return {"action": "skip", "reason": "maton-private-chat-only"}
            return None

        identity = _identity(source)

        if kind == "cancel":
            try:
                self.store.guard(identity, "cancelled")
            except Exception:
                logger.error("Maton onboarding could not persist cancellation state")
            self._schedule(self._send(gateway, source, "Подключение Maton отменено."))
            return {"action": "skip", "reason": "maton-onboarding-cancelled"}

        if kind == "status":
            active = bool(os.getenv(ENV_KEY))
            status = "подключён" if active else "ещё не подключён"
            self._schedule(self._send(gateway, source, f"Maton {status}."))
            return {"action": "skip", "reason": "maton-status"}

        if kind in {"connect", "reconnect"}:
            if kind == "connect" and os.getenv(ENV_KEY):
                self._schedule(
                    self._send(
                        gateway,
                        source,
                        "✅ Maton уже подключён. Для замены ключа: /maton_reconnect",
                    )
                )
            else:
                try:
                    self.store.start(identity)
                except Exception:
                    logger.error("Maton onboarding could not open pending state")
                    self._schedule(
                        self._send(
                            gateway,
                            source,
                            "⚠️ Не удалось безопасно открыть приём ключа. Ключ пока не отправляйте.",
                        )
                    )
                else:
                    self._schedule(self._prompt(gateway, source, message_id))
            return {"action": "skip", "reason": "maton-onboarding-started"}

        try:
            record = self.store.read(identity)
        except Exception:
            logger.error("Maton onboarding pending state is unavailable")
            if _looks_like_credential(text):
                self._schedule(self._discard_guarded_message(gateway, source, message_id))
                return {"action": "skip", "reason": "maton-state-failure-credential-guard"}
            return None
        if record is None:
            return None

        if text.startswith("/"):
            return None

        status = str(record.get("status") or "")
        if status == "processing":
            self._schedule(self._send(gateway, source, "⏳ Ключ уже проверяется. Подождите результат."))
            return {"action": "skip", "reason": "maton-onboarding-processing"}

        if status != "pending":
            if _looks_like_credential(text):
                self._schedule(self._discard_guarded_message(gateway, source, message_id))
                return {"action": "skip", "reason": "maton-expired-credential-guard"}
            try:
                self.store.clear(identity)
            except Exception:
                logger.error("Maton onboarding could not clear expired state")
            return None

        try:
            claimed = self.store.claim(identity)
        except Exception:
            logger.error("Maton onboarding could not claim the credential window")
            self._schedule(self._discard_guarded_message(gateway, source, message_id))
            return {"action": "skip", "reason": "maton-claim-failure-credential-guard"}
        if not claimed:
            return {"action": "skip", "reason": "maton-onboarding-claim-race"}

        self._schedule(
            self._consume_secret(
                gateway=gateway,
                source=source,
                identity=identity,
                message_id=message_id,
                secret=text,
            )
        )
        return {"action": "skip", "reason": "maton-credential-consumed"}
