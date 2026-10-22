"""Voice picker plugin for Hermes Telegram bot.

Allows profile owners to listen to ElevenLabs assistant voice samples
and select a voice interactively via Telegram reply-keyboard buttons
without restarting the bot, auto-enabling voice mode via rewrite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger(__name__)

VOICES: list[tuple[str, str, str]] = [
    ("♂ Артём — подкаст", "rQOBu7YxCDxGiFdTm28w", "male"),
    ("♂ Денис — рассказчик", "0BcDz9UPwL3MpsnTeUlO", "male"),
    ("♂ Дмитрий — деловой", "hU3rD0Yk7DoiYULTX1pD", "male"),
    ("♂ Николай — YouTube", "3EuKHIEZbSzrHGNmdYsx", "male"),
    ("♂ Станислав — спокойный", "ogi2DyUAKJb7CEdqqvlU", "male"),
    ("♀ Марина — тёплый рассказчик", "ymDCYd8puC7gYjxIamPt", "female"),
    ("♀ Надя — энергичная", "gedzfqL7OGdPbwm0ynTP", "female"),
    ("♀ Лариса — молодая, соцсети", "AB9XsbSA4eLG12t2myjN", "female"),
    ("♀ Sarah — уверенная (EN)", "EXAVITQu4vr4xnSDxMaL", "female"),
    ("♀ Lily — бархатная (EN)", "pFZP5JQG7iQjIQuC4Bku", "female"),
]

COMMANDS = {"golos", "voice_pick", "voice-pick"}
PENDING_TTL_SECONDS = 10 * 60  # 10 minutes
MODEL_ID = "eleven_v4_turbo"
SAMPLES_DIR = Path("/opt/vektor/voice-samples")
SAMPLE_PAUSE_SECONDS = 0.3

_pending_memory: dict[str, float] = {}
_tasks: set[asyncio.Task] = set()
_state_lock = threading.Lock()


def _get_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home()
    except ImportError:
        env_home = os.environ.get("HERMES_HOME")
        if env_home:
            return Path(env_home).expanduser().resolve()
        return Path.home() / ".hermes"


def _state_file_path() -> Path:
    return _get_home() / "voice-picker" / "state.json"


def _load_state_from_disk(now: float | None = None) -> dict[str, float]:
    current = time.time() if now is None else now
    path = _state_file_path()
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                str(k): float(v)
                for k, v in data.items()
                if isinstance(v, (int, float)) and v > current
            }
    except Exception:
        pass
    return {}


def _save_state_to_disk(state: dict[str, float]) -> None:
    path = _state_file_path()
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass

    fd, temp_name = tempfile.mkstemp(prefix=".state.json.", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _set_pending(user_id: str, now: float | None = None) -> None:
    current = time.time() if now is None else now
    expires_at = current + PENDING_TTL_SECONDS
    with _state_lock:
        _pending_memory[user_id] = expires_at
        disk_state = _load_state_from_disk(now=current)
        disk_state[user_id] = expires_at
        _save_state_to_disk(disk_state)


def _is_pending(user_id: str, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    with _state_lock:
        expires_at = _pending_memory.get(user_id)
        if expires_at is not None:
            if expires_at > current:
                return True
            _pending_memory.pop(user_id, None)

        disk_state = _load_state_from_disk(now=current)
        expires_at = disk_state.get(user_id)
        if expires_at is not None and expires_at > current:
            _pending_memory[user_id] = expires_at
            return True
        elif user_id in disk_state:
            disk_state.pop(user_id, None)
            _save_state_to_disk(disk_state)

        return False


def _clear_pending(user_id: str) -> None:
    with _state_lock:
        _pending_memory.pop(user_id, None)
        disk_state = _load_state_from_disk()
        if user_id in disk_state:
            disk_state.pop(user_id, None)
            _save_state_to_disk(disk_state)


def _coerce_ids(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set, frozenset)):
        result: set[str] = set()
        for item in raw:
            result |= _coerce_ids(item)
        return result
    if isinstance(raw, str):
        return {s.strip() for s in raw.split(",") if s.strip()}
    if isinstance(raw, (int, float)):
        return {str(int(raw))}
    s = str(raw).strip()
    return {s} if s else set()


def _get_authorized_user_ids() -> set[str]:
    home = _get_home()
    config_path = home / "config.yaml"
    if not config_path.is_file() or config_path.is_symlink():
        return set()
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return set()
    if not isinstance(data, dict):
        return set()

    authorized: set[str] = set()
    platforms = data.get("platforms")
    if isinstance(platforms, dict):
        tg = platforms.get("telegram")
        if isinstance(tg, dict):
            extra = tg.get("extra")
            if isinstance(extra, dict):
                authorized |= _coerce_ids(extra.get("business_owner_ids"))
                authorized |= _coerce_ids(extra.get("allow_admin_from"))
            authorized |= _coerce_ids(tg.get("allow_admin_from"))

    authorized |= _coerce_ids(data.get("allow_admin_from"))
    return authorized


def _command_parts(text: str) -> tuple[str, str]:
    stripped = (text or "").lstrip()
    head, separator, tail = stripped.partition(" ")
    if not head.startswith("/"):
        return "", ""
    command = head[1:].split("@", 1)[0].lower()
    return command, tail.strip() if separator else ""


def _extract_voice_name(label: str) -> str:
    head = label.split(" — ", 1)[0].strip()
    name = head.lstrip("♂♀ \t")
    return name.strip()


def _parse_voice_choice(text: str) -> tuple[str, str, str] | None:
    raw = (text or "").strip()
    if not raw:
        return None

    candidate_num = raw.rstrip(".")
    if candidate_num.isdigit():
        idx = int(candidate_num) - 1
        if 0 <= idx < len(VOICES):
            return VOICES[idx]

    for v in VOICES:
        if raw == v[0]:
            return v

    raw_lower = raw.lower()
    for v in VOICES:
        if raw_lower == v[0].lower():
            return v

    return None


def _build_keyboard() -> Any:
    try:
        from telegram import KeyboardButton, ReplyKeyboardMarkup

        keyboard = [
            [KeyboardButton(VOICES[i][0]), KeyboardButton(VOICES[i + 1][0])]
            for i in range(0, len(VOICES), 2)
        ]
        return ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True,
            one_time_keyboard=True,
        )
    except Exception:
        return None


def _build_remove_keyboard() -> Any:
    try:
        from telegram import ReplyKeyboardRemove

        return ReplyKeyboardRemove()
    except Exception:
        return None


FALLBACK_PROMPT = (
    "Выбери голос ассистента:\n"
    + "\n".join(f"{i + 1}. {v[0]}" for i, v in enumerate(VOICES))
    + "\n\nОтветь цифрой 1–10 или подписью."
)


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


def _get_ptb_bot(adapter: Any) -> Any | None:
    if adapter is None:
        return None
    for attr in ("_bot", "bot"):
        bot = getattr(adapter, attr, None)
        if bot is not None and (hasattr(bot, "send_message") or hasattr(bot, "send_voice")):
            return bot
    for attr in ("_app", "app"):
        app = getattr(adapter, attr, None)
        if app is not None:
            bot = getattr(app, "bot", None)
            if bot is not None and (hasattr(bot, "send_message") or hasattr(bot, "send_voice")):
                return bot
    return None


async def _send_voice_picker(gateway: Any, source: Any) -> None:
    adapter = _adapter_for(gateway, source)
    chat_id = getattr(source, "chat_id", None) or getattr(source, "user_id", None)
    if not chat_id:
        return

    bot = _get_ptb_bot(adapter)
    markup = _build_keyboard()

    if bot is not None and hasattr(bot, "send_message"):
        # Send voice samples sequentially if bot supports send_voice
        if hasattr(bot, "send_voice"):
            for idx, (label, voice_id, _) in enumerate(VOICES, start=1):
                sample_file = SAMPLES_DIR / f"{voice_id}.ogg"
                if not sample_file.is_file():
                    continue
                try:
                    with open(sample_file, "rb") as f:
                        await bot.send_voice(
                            chat_id=chat_id,
                            voice=f,
                            caption=f"{idx}. {label}",
                        )
                    if SAMPLE_PAUSE_SECONDS > 0:
                        await asyncio.sleep(SAMPLE_PAUSE_SECONDS)
                except Exception:
                    logger.warning("sample send failed")

        prompt_text = "Послушай примеры выше и выбери голос кнопкой 👇"
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=prompt_text,
                reply_markup=markup,
            )
            return
        except Exception:
            logger.warning("sample send failed")

    if adapter is not None and hasattr(adapter, "send"):
        try:
            await adapter.send(chat_id, FALLBACK_PROMPT)
        except Exception:
            pass


async def _send_selection_confirmation(
    gateway: Any, source: Any, voice_tuple: tuple[str, str, str]
) -> None:
    adapter = _adapter_for(gateway, source)
    chat_id = getattr(source, "chat_id", None) or getattr(source, "user_id", None)
    if not chat_id:
        return

    name = _extract_voice_name(voice_tuple[0])
    caption = (
        f"Готово! Теперь я говорю голосом «{name}». "
        "Голосовые ответы включены: на голосовое отвечу голосом. "
        "Все ответы голосом — /voice tts, выключить — /voice off, сменить голос — /golos"
    )

    bot = _get_ptb_bot(adapter)
    remove_markup = _build_remove_keyboard()

    if bot is not None:
        sample_file = SAMPLES_DIR / f"{voice_tuple[1]}.ogg"
        if sample_file.is_file() and hasattr(bot, "send_voice"):
            try:
                with open(sample_file, "rb") as f:
                    await bot.send_voice(
                        chat_id=chat_id,
                        voice=f,
                        caption=caption,
                        reply_markup=remove_markup,
                    )
                return
            except Exception:
                logger.warning("sample send failed")

        if hasattr(bot, "send_message"):
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    reply_markup=remove_markup,
                )
                return
            except Exception:
                pass

    if adapter is not None and hasattr(adapter, "send"):
        try:
            await adapter.send(chat_id, caption)
        except Exception:
            pass


def _update_config_voice(voice_id: str) -> None:
    home = _get_home().resolve()
    config_path = home / "config.yaml"
    if config_path.is_symlink():
        raise RuntimeError(f"unsafe symlink: {config_path}")

    if not config_path.exists():
        home.mkdir(parents=True, mode=0o700, exist_ok=True)
        config: dict[str, Any] = {}
        owner_uid = None
        owner_gid = None
    else:
        info = config_path.stat()
        owner_uid = info.st_uid
        owner_gid = info.st_gid
        raw = config_path.read_text(encoding="utf-8")
        loaded = yaml.safe_load(raw) or {}
        if not isinstance(loaded, dict):
            raise RuntimeError("config.yaml must be a mapping")
        config = loaded

    tts = config.setdefault("tts", {})
    if not isinstance(tts, dict):
        tts = {}
        config["tts"] = tts
    elevenlabs = tts.setdefault("elevenlabs", {})
    if not isinstance(elevenlabs, dict):
        elevenlabs = {}
        tts["elevenlabs"] = elevenlabs

    elevenlabs["voice_id"] = voice_id
    elevenlabs["model_id"] = MODEL_ID

    dumped = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)

    fd, temp_name = tempfile.mkstemp(prefix=".config.yaml.", dir=config_path.parent)
    try:
        os.fchmod(fd, 0o600)
        if owner_uid is not None and owner_gid is not None:
            try:
                os.fchown(fd, owner_uid, owner_gid)
            except (PermissionError, OSError):
                pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(dumped)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, config_path)
        os.chmod(config_path, 0o600)
        if owner_uid is not None and owner_gid is not None:
            try:
                os.chown(config_path, owner_uid, owner_gid)
            except (PermissionError, OSError):
                pass
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _track_task(task: asyncio.Task) -> None:
    _tasks.add(task)

    def _done(completed: asyncio.Task) -> None:
        _tasks.discard(completed)
        try:
            completed.result()
        except Exception:
            pass

    task.add_done_callback(_done)


def _schedule(coro: Any) -> bool:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        return False
    _track_task(task)
    return True


def _is_direct_message(source: Any) -> bool:
    chat_type = str(getattr(source, "chat_type", "") or "").lower()
    if chat_type in ("dm", "private"):
        return True
    chat_id = str(getattr(source, "chat_id", "") or "")
    user_id = str(getattr(source, "user_id", "") or "")
    if chat_id and user_id and chat_id == user_id:
        return True
    return False


def _is_authorized(source: Any) -> bool:
    platform = getattr(getattr(source, "platform", None), "value", None) or getattr(
        source, "platform", None
    )
    if str(platform or "").lower() != "telegram":
        return False
    if not _is_direct_message(source):
        return False

    user_id = str(getattr(source, "user_id", "") or "").strip()
    if not user_id:
        return False

    authorized_ids = _get_authorized_user_ids()
    return user_id in authorized_ids


def _on_pre_gateway_dispatch(event: Any, gateway: Any, **_: Any) -> dict[str, str] | None:
    text = str(getattr(event, "text", "") or "")
    command, _ = _command_parts(text)
    source = getattr(event, "source", None)
    if source is None:
        return None

    user_id = str(getattr(source, "user_id", "") or "").strip()
    if not user_id:
        return None

    is_picker_command = command in COMMANDS
    has_pending = _is_pending(user_id)

    if not is_picker_command and not has_pending:
        return None

    if not _is_authorized(source):
        return None

    if is_picker_command:
        _set_pending(user_id)
        _schedule(_send_voice_picker(gateway, source))
        return {"action": "skip", "reason": "voice-picker-prompt"}

    # Pending choice evaluation
    matched_voice = _parse_voice_choice(text)
    if matched_voice is None:
        # Any other text is not intercepted
        return None

    _clear_pending(user_id)
    _update_config_voice(matched_voice[1])
    logger.info("voice changed")
    _schedule(_send_selection_confirmation(gateway, source, matched_voice))
    return {"action": "rewrite", "text": "/voice on"}


def _voice_picker_cli_command(_raw_args: str) -> str:
    lines = ["Голоса ElevenLabs для ассистента:"]
    for i, v in enumerate(VOICES):
        lines.append(f"{i + 1}. {v[0]} ({v[1]})")
    lines.append("")
    lines.append("Для интерактивного выбора отправьте команду /golos в личном чате Telegram.")
    return "\n".join(lines)


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    ctx.register_command(
        "golos",
        _voice_picker_cli_command,
        description="Выбрать голос ассистента ElevenLabs",
    )
    ctx.register_command(
        "voice_pick",
        _voice_picker_cli_command,
        description="Выбрать голос ассистента ElevenLabs (алиас /golos)",
    )
