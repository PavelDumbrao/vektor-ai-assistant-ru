"""Typed Telegram editor operations. No arbitrary methods, URLs or recipients."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import PurePath
from urllib.parse import urlsplit

from .post_validation import validate_post, visible_text
from .rich_post import prepare_article


MAX_MEDIA_BYTES = 50_000_000
MAX_PHOTO_BYTES = 9 * 1024 * 1024
SEND_KINDS = {
    "text",
    "photo",
    "video",
    "animation",
    "audio",
    "voice",
    "document",
    "album",
    "poll",
    "quiz",
}
MANAGE_KINDS = {
    "edit_text",
    "edit_caption",
    "edit_media",
    "edit_buttons",
    "edit_article",
    "pin",
    "unpin",
    "stop_poll",
    "react",
}
ASSET_KINDS = {"photo", "video", "animation", "audio", "voice", "document"}
ASSET_EXTENSIONS = {
    "photo": {".png", ".jpg", ".jpeg", ".webp"},
    "video": {".mp4"},
    "animation": {".gif", ".mp4"},
    "audio": {".mp3", ".m4a"},
    "voice": {".ogg", ".opus"},
    "document": {
        ".pdf",
        ".docx",
        ".xlsx",
        ".pptx",
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".zip",
    },
}


def fields(data, allowed):
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise ValueError("unknown_telegram_fields")


def bounded_text(value, minimum, maximum, label):
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or "\x00" in value
    ):
        raise ValueError(f"invalid_{label}")
    return value


def boolean(value, label):
    if type(value) is not bool:
        raise ValueError(f"invalid_{label}")
    return value


def html_text(value, *, caption=False, required=True):
    text = bounded_text(value, 1 if required else 0, 30000, "text")
    if not text and not required:
        return text
    errors = validate_post(text, has_photo=caption)
    if errors or len(visible_text(text).encode("utf-16-le")) // 2 > (
        1024 if caption else 4096
    ):
        raise ValueError("invalid_telegram_html_or_length")
    return text


def keyboard(value):
    if not isinstance(value, list) or len(value) > 4:
        raise ValueError("buttons_require_at_most_4_rows")
    rows = []
    total = 0
    for row in value:
        if not isinstance(row, list) or not 1 <= len(row) <= 4:
            raise ValueError("buttons_require_1_to_4_per_row")
        result = []
        for button in row:
            fields(button, {"text", "url"})
            text = bounded_text(button.get("text"), 1, 64, "button_text")
            link = bounded_text(button.get("url"), 1, 2048, "button_url")
            url = urlsplit(link)
            if url.scheme != "https" or not url.netloc or url.username or url.password:
                raise ValueError("button_requires_public_https_url")
            host = url.hostname or ""
            if (
                host == "localhost"
                or host.endswith((".localhost", ".local"))
                or "." not in host
            ):
                raise ValueError("button_requires_public_https_url")
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                if not address.is_global:
                    raise ValueError("button_requires_public_https_url")
            if any(ord(char) < 32 for char in link):
                raise ValueError("invalid_button_url")
            result.append({"text": text, "url": link})
            total += 1
        rows.append(result)
    if total > 12:
        raise ValueError("too_many_buttons")
    return {"inline_keyboard": rows}


def decode_asset(data, kind):
    filename = bounded_text(data.get("filename"), 1, 180, "filename")
    if PurePath(filename).name != filename or not re.fullmatch(
        r"[\w .()\-]+", filename
    ):
        raise ValueError("invalid_filename")
    suffix = PurePath(filename).suffix.lower()
    if suffix not in ASSET_EXTENSIONS[kind]:
        raise ValueError("unsupported_media_extension")
    encoded = data.get("file_b64")
    if not isinstance(encoded, str) or len(encoded) > MAX_MEDIA_BYTES * 4 // 3 + 8:
        raise ValueError("media_too_large")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:
        raise ValueError("invalid_media_base64") from None
    if not raw or len(raw) > (MAX_PHOTO_BYTES if kind == "photo" else MAX_MEDIA_BYTES):
        raise ValueError("media_size_limit")
    if kind == "photo" and not (
        raw.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))
        or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")
    ):
        raise ValueError("invalid_photo_bytes")
    if kind in {"video", "animation"} and suffix == ".mp4" and raw[4:8] != b"ftyp":
        raise ValueError("invalid_mp4_bytes")
    if (
        kind == "animation"
        and suffix == ".gif"
        and not raw.startswith((b"GIF87a", b"GIF89a"))
    ):
        raise ValueError("invalid_gif_bytes")
    if kind == "voice" and not raw.startswith(b"OggS"):
        raise ValueError("voice_requires_ogg_opus")
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".gif": "image/gif",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".pdf": "application/pdf",
    }.get(suffix, "application/octet-stream")
    return filename, raw, mime


@dataclass
class TelegramSpec:
    kind: str
    method: str
    params: dict
    files: dict = field(default_factory=dict)
    provided: set = field(default_factory=set)

    @property
    def creates_message(self):
        return self.kind in SEND_KINDS

    @property
    def payload_hash(self):
        data = {
            "format": "telegram_toolkit_v1",
            "kind": self.kind,
            "method": self.method,
            "params": self.params,
            "files": {
                key: {"name": asset[0], "sha256": hashlib.sha256(asset[1]).hexdigest()}
                for key, asset in self.files.items()
            },
        }
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def summary(self):
        return {
            "kind": self.kind,
            "creates_message": self.creates_message,
            "message_id": self.params.get("message_id"),
            "payload_hash": self.payload_hash,
            "file_count": len(self.files),
            "upload_bytes": sum(len(x[1]) for x in self.files.values()),
            "description": self.params.get("question")
            or self.params.get("text")
            or self.params.get("caption")
            or self.kind,
        }


def prepare_spec(data) -> TelegramSpec:
    if not isinstance(data, dict):
        raise ValueError("telegram_spec_must_be_object")
    kind = data.get("kind")
    if kind not in SEND_KINDS | MANAGE_KINDS:
        raise ValueError("unsupported_telegram_operation")
    common = (
        {"kind", "buttons", "silent", "protect_content"}
        if kind in SEND_KINDS
        else {"kind", "message_id"}
    )
    allowed = {
        "text": {"text", "link_preview"},
        "photo": {"filename", "file_b64", "caption", "has_spoiler"},
        "video": {"filename", "file_b64", "caption", "has_spoiler"},
        "animation": {"filename", "file_b64", "caption", "has_spoiler"},
        "audio": {"filename", "file_b64", "caption", "title", "performer"},
        "voice": {"filename", "file_b64", "caption"},
        "document": {"filename", "file_b64", "caption"},
        "album": {"items"},
        "poll": {
            "question",
            "options",
            "allows_multiple_answers",
            "is_anonymous",
            "open_period",
            "close_date",
            "shuffle_options",
            "hide_results_until_closes",
        },
        "quiz": {
            "question",
            "options",
            "correct_option_ids",
            "explanation",
            "allows_multiple_answers",
            "is_anonymous",
            "open_period",
            "close_date",
            "shuffle_options",
            "hide_results_until_closes",
        },
        "edit_text": {"text", "buttons", "link_preview"},
        "edit_caption": {"caption", "buttons"},
        "edit_media": {"media_type", "filename", "file_b64", "caption", "buttons"},
        "edit_buttons": {"buttons"},
        "edit_article": {"article", "buttons"},
        "pin": set(),
        "unpin": set(),
        "stop_poll": set(),
        "react": {"emoji"},
    }[kind]
    fields(data, common | allowed)
    params = {}
    uploads = {}
    if kind in SEND_KINDS:
        params["disable_notification"] = boolean(data.get("silent", False), "silent")
        params["protect_content"] = boolean(
            data.get("protect_content", False), "protect_content"
        )
    else:
        if type(data.get("message_id")) is not int or data["message_id"] <= 0:
            raise ValueError("positive_message_id_required")
        params["message_id"] = data["message_id"]
    if "buttons" in data:
        if kind == "album":
            raise ValueError("albums_do_not_support_inline_buttons")
        params["reply_markup"] = keyboard(data["buttons"])
    if kind in {"text", "edit_text"}:
        method = "sendMessage" if kind == "text" else "editMessageText"
        params.update(
            text=html_text(data.get("text")),
            parse_mode="HTML",
            link_preview_options={
                "is_disabled": not boolean(
                    data.get("link_preview", False), "link_preview"
                )
            },
        )
    elif kind in ASSET_KINDS:
        method = {
            "photo": "sendPhoto",
            "video": "sendVideo",
            "animation": "sendAnimation",
            "audio": "sendAudio",
            "voice": "sendVoice",
            "document": "sendDocument",
        }[kind]
        uploads["file0"] = decode_asset(data, kind)
        params[kind] = "attach://file0"
        params.update(
            caption=html_text(data.get("caption", ""), caption=True, required=False),
            parse_mode="HTML",
        )
        if kind == "video":
            params["supports_streaming"] = True
        for flag in ("has_spoiler",):
            if flag in data:
                params[flag] = boolean(data[flag], flag)
        for label in ("title", "performer"):
            if label in data:
                params[label] = bounded_text(data[label], 1, 128, label)
    elif kind == "album":
        method = "sendMediaGroup"
        items = data.get("items")
        if not isinstance(items, list) or not 2 <= len(items) <= 10:
            raise ValueError("album_requires_2_to_10_items")
        media = []
        for index, item in enumerate(items):
            fields(item, {"kind", "filename", "file_b64", "caption"})
            media_kind = item.get("kind")
            if media_kind not in {"photo", "video"}:
                raise ValueError("album_supports_photos_and_mp4_only")
            key = f"file{index}"
            uploads[key] = decode_asset(item, media_kind)
            entry = {
                "type": media_kind,
                "media": f"attach://{key}",
                "caption": html_text(
                    item.get("caption", ""), caption=True, required=False
                ),
                "parse_mode": "HTML",
            }
            if media_kind == "video":
                entry["supports_streaming"] = True
            media.append(entry)
        params["media"] = media
    elif kind in {"poll", "quiz"}:
        method = "sendPoll"
        options = data.get("options")
        if not isinstance(options, list) or not 1 <= len(options) <= 12:
            raise ValueError("poll_requires_1_to_12_options")
        options = [bounded_text(option, 1, 100, "poll_option") for option in options]
        if len(set(options)) != len(options):
            raise ValueError("duplicate_poll_options")
        if data.get("is_anonymous", True) is not True:
            raise ValueError("only_anonymous_polls_enabled")
        params.update(
            question=bounded_text(data.get("question"), 1, 300, "poll_question"),
            options=[{"text": option} for option in options],
            is_anonymous=True,
            type="quiz" if kind == "quiz" else "regular",
        )
        params["allows_multiple_answers"] = boolean(
            data.get("allows_multiple_answers", False), "allows_multiple_answers"
        )
        if kind == "quiz":
            correct = data.get("correct_option_ids")
            if (
                not isinstance(correct, list)
                or not correct
                or any(type(x) is not int or not 0 <= x < len(options) for x in correct)
                or sorted(set(correct)) != correct
            ):
                raise ValueError("invalid_quiz_correct_option_ids")
            if len(correct) > 1 and not params["allows_multiple_answers"]:
                raise ValueError("multiple_correct_answers_require_multiple_selection")
            params["correct_option_ids"] = correct
            explanation = bounded_text(
                data.get("explanation", ""), 0, 200, "quiz_explanation"
            )
            if explanation.count("\n") > 2:
                raise ValueError("quiz_explanation_max_2_line_breaks")
            params["explanation"] = explanation
        if "open_period" in data and "close_date" in data:
            raise ValueError("choose_poll_duration_or_deadline")
        for timer in ("open_period", "close_date"):
            if timer in data:
                value = data[timer]
                seconds = (
                    value - int(time.time())
                    if timer == "close_date" and type(value) is int
                    else value
                )
                if type(value) is not int or not 5 <= seconds <= 2628000:
                    raise ValueError("invalid_poll_timer")
                params[timer] = value
        for flag in ("shuffle_options", "hide_results_until_closes"):
            if flag in data:
                params[flag] = boolean(data[flag], flag)
    elif kind == "edit_caption":
        method = "editMessageCaption"
        params.update(
            caption=html_text(data.get("caption"), caption=True, required=False),
            parse_mode="HTML",
        )
    elif kind == "edit_media":
        method = "editMessageMedia"
        media_kind = data.get("media_type")
        if media_kind not in {"photo", "video", "animation", "audio", "document"}:
            raise ValueError("unsupported_edit_media_type")
        uploads["file0"] = decode_asset(data, media_kind)
        params["media"] = {"type": media_kind, "media": "attach://file0"}
        if "caption" in data:
            params["media"].update(
                caption=html_text(data["caption"], caption=True, required=False),
                parse_mode="HTML",
            )
    elif kind == "edit_article":
        method = "editMessageText"
        article = prepare_article(data.get("article"))
        params["rich_message"] = article.wire()
        uploads = article.files()
    elif kind == "edit_buttons":
        method = "editMessageReplyMarkup"
        if "buttons" not in data:
            raise ValueError("buttons_required_use_empty_list_to_remove")
    else:
        method = {
            "pin": "pinChatMessage",
            "unpin": "unpinChatMessage",
            "stop_poll": "stopPoll",
            "react": "setMessageReaction",
        }[kind]
        if kind == "pin":
            params["disable_notification"] = True
        if kind == "react":
            emoji = bounded_text(data.get("emoji", ""), 0, 16, "reaction")
            params["reaction"] = [{"type": "emoji", "emoji": emoji}] if emoji else []
    if sum(len(asset[1]) for asset in uploads.values()) > MAX_MEDIA_BYTES:
        raise ValueError("total_media_exceeds_50_mb")
    return TelegramSpec(kind, method, params, uploads, set(data))


class TelegramRejected(RuntimeError):
    """An explicit Bot API rejection; no token or raw response in exception text."""

    def __init__(self, code):
        self.code = code
        super().__init__(f"telegram_rejected_{code}")


async def execute_spec(client, base_url, chat_id, spec):
    params = {**spec.params, "chat_id": chat_id}
    if spec.files:
        data = {
            key: json.dumps(value, ensure_ascii=False)
            if isinstance(value, (dict, list, bool))
            else str(value)
            for key, value in params.items()
        }
        response = await client.post(
            f"{base_url}/{spec.method}", data=data, files=spec.files
        )
    else:
        response = await client.post(f"{base_url}/{spec.method}", json=params)
    payload = response.json()
    if payload.get("ok") is not True:
        result = payload.get("result")
        if isinstance(result, (dict, list)) and result:
            raise RuntimeError("telegram_error_with_result_requires_readback")
        if (
            spec.kind.startswith("edit_")
            and "message is not modified" in str(payload.get("description", "")).lower()
        ):
            return {
                "messages": [],
                "message_ids": [spec.params["message_id"]],
                "no_change": True,
            }
        code = payload.get("error_code", response.status_code)
        if type(code) is int and 400 <= code < 500:
            raise TelegramRejected(code)
        raise RuntimeError("telegram_upstream_result_uncertain")
    response.raise_for_status()
    result = payload.get("result")
    if spec.creates_message:
        messages = result if isinstance(result, list) else [result]
        if not messages or any(
            not isinstance(m, dict)
            or not m.get("message_id")
            or m.get("chat", {}).get("id") != chat_id
            for m in messages
        ):
            raise RuntimeError("telegram_delivery_response_mismatch")
        expected = len(spec.params["media"]) if spec.kind == "album" else 1
        if len(messages) != expected:
            raise RuntimeError("telegram_album_response_count_mismatch")
        return {
            "messages": messages,
            "message_ids": [m["message_id"] for m in messages],
        }
    messages = [result] if isinstance(result, dict) and "message_id" in result else []
    if any(
        m.get("chat", {}).get("id") != chat_id
        or m.get("message_id") != spec.params["message_id"]
        for m in messages
    ):
        raise RuntimeError("telegram_mutation_response_mismatch")
    output = {"messages": messages, "message_ids": [spec.params["message_id"]]}
    if spec.kind == "stop_poll":
        if not isinstance(result, dict) or result.get("is_closed") is not True:
            raise RuntimeError("poll_stop_response_mismatch")
        output["poll"] = result
    return output
