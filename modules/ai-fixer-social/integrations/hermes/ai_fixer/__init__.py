"""Native Hermes tool without enabling terminal or exposing the editor token."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path

from .preview import deliver_preview, deliver_rich_preview, deliver_telegram_preview
from .telegram_content import MAX_MEDIA_BYTES, prepare_spec
from .editor_gateway import callback_supported, draft_preview, refresh_controls, register_callbacks


PROFILE = Path("/home/pavel/.hermes")
REFERENCE = PROFILE / "assets/pavel/reference.png"
MANIFEST = PROFILE / "assets/pavel/manifest.json"
MEDIA_ROOT = Path("/home/pavel/workspace/ai-fixer-media")
CONTROL = "/usr/local/bin/ai-fixer-control"
MAX_IMAGE_BYTES = 9 * 1024 * 1024
WRITES = {"publish", "long_publish", "tg_publish", "tg_manage", "reply", "image_submit", "queue_set", "queue_cancel"}
ALLOWED = {
    "op",
    "text",
    "request_id",
    "photo_path",
    "confirm_hash",
    "message_id",
    "limit",
    "model",
    "prompt",
    "aspect_ratio",
    "image_size",
    "use_pavel_reference",
    "confirm_credits",
    "article_path",
    "telegram_spec_path",
    "draft_id",
    "scheduled_at",
    "rubric",
}


def telegram_payload(args):
    allowed = {"op", "request_id", "telegram_spec_path"}
    if args["op"] in {"tg_publish", "tg_manage"}:
        allowed.add("confirm_hash")
    if set(args) - allowed:
        raise ValueError("unexpected_telegram_arguments")
    path = Path(args["telegram_spec_path"]).resolve(strict=True)
    if (
        not path.is_relative_to(MEDIA_ROOT.resolve())
        or path.suffix != ".json"
        or not path.is_file()
    ):
        raise ValueError("telegram_spec_outside_media_directory")
    with path.open("rb") as handle:
        raw = handle.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise ValueError("telegram_spec_file_too_large")
    spec = json.loads(raw)
    if not isinstance(spec, dict):
        raise ValueError("telegram_spec_must_be_object")
    if "article" in spec:
        raise ValueError("use_local_article_path_not_inline_article")
    if "article_path" in spec:
        article_path = spec.pop("article_path")
        spec["article"] = request_payload(
            {
                "op": "long_validate",
                "request_id": args["request_id"],
                "article_path": article_path,
            }
        )["article"]
    items = [spec]
    if "items" in spec:
        if not isinstance(spec["items"], list) or len(spec["items"]) > 10:
            raise ValueError("invalid_album_items")
        items += spec["items"]
    total = 0
    for item in items:
        if not isinstance(item, dict) or "file_b64" in item or "filename" in item:
            raise ValueError("use_local_file_path_not_inline_file_data")
        if "file_path" not in item:
            continue
        file = Path(item.pop("file_path")).resolve(strict=True)
        if not file.is_relative_to(MEDIA_ROOT.resolve()) or not file.is_file():
            raise ValueError("media_outside_media_directory")
        with file.open("rb") as handle:
            raw = handle.read(MAX_MEDIA_BYTES + 1)
        total += len(raw)
        if total > MAX_MEDIA_BYTES:
            raise ValueError("total_media_exceeds_50_mb")
        item.update(filename=file.name, file_b64=base64.b64encode(raw).decode())
    payload = {key: value for key, value in args.items() if key != "telegram_spec_path"}
    payload["spec"] = spec
    return payload


def fingerprint(args):
    return hashlib.sha256(
        json.dumps(args, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def reference_info():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    with REFERENCE.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != manifest["sha256"]:
        return {"ok": False, "error": "reference_checksum_mismatch"}
    return {
        "ok": True,
        "reference_path": str(REFERENCE),
        "sha256": digest,
        "status": manifest["status"],
        "owner": manifest["owner"],
        "kind": "user_approved_ai_identity_anchor_not_raw_photograph",
        "instruction": "Передавай файл как image input/reference, не заменяй текстовым описанием лица.",
        "usage_rules": manifest["usage_rules"],
    }


def request_payload(args):
    if not isinstance(args, dict) or set(args) - ALLOWED:
        raise ValueError("invalid_arguments")
    payload = dict(args)
    if args.get("op") == "draft_preview":
        if set(args) - {"op", "request_id", "text", "photo_path", "article_path", "telegram_spec_path", "rubric"}:
            raise ValueError("invalid_draft_fields")
        formats = int("article_path" in args) + int("telegram_spec_path" in args) + int("text" in args or "photo_path" in args)
        if formats != 1:
            raise ValueError("one_draft_format_required")
        if "article_path" in args:
            content = {"article": request_payload({"op": "long_validate", "request_id": args["request_id"], "article_path": args["article_path"]})["article"]}
        elif "telegram_spec_path" in args:
            content = {"spec": telegram_payload({"op": "tg_validate", "request_id": args["request_id"], "telegram_spec_path": args["telegram_spec_path"]})["spec"]}
        else:
            native = {"kind": "photo", "caption": args["text"]} if args.get("photo_path") else {"kind": "text", "text": args["text"]}
            if args.get("photo_path"):
                encoded = request_payload({"op": "validate", "photo_path": args["photo_path"]})["photo_b64"]
                native.update(file_b64=encoded, filename=Path(args["photo_path"]).name)
            content = {"spec": native}
        return {"op": "draft_prepare", "request_id": args["request_id"], "content": content, "rubric": args.get("rubric", "")}
    if args.get("op") in {"tg_validate", "tg_preview", "tg_publish", "tg_manage"}:
        return telegram_payload(args)
    if str(args.get("op", "")).startswith("long_"):
        allowed = {"op", "request_id", "article_path"}
        if args["op"] == "long_publish":
            allowed.add("confirm_hash")
        if set(args) - allowed:
            raise ValueError("unexpected_long_post_fields")
        path = Path(payload.pop("article_path")).resolve(strict=True)
        if (
            not path.is_relative_to(MEDIA_ROOT.resolve())
            or path.suffix.lower() != ".json"
            or not path.is_file()
        ):
            raise ValueError("article_outside_media_directory")
        with path.open("rb") as handle:
            raw = handle.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError("article_file_too_large")
        article = json.loads(raw)
        if not isinstance(article, dict) or set(article) != {"html", "photos"}:
            raise ValueError("article_requires_html_and_photos")
        if not isinstance(article["photos"], list) or len(article["photos"]) > 10:
            raise ValueError("article_max_10_photos")
        photos = []
        total = 0
        for item in article["photos"]:
            if not isinstance(item, dict) or set(item) != {"id", "path"}:
                raise ValueError("article_photo_requires_id_and_path")
            encoded = request_payload({"op": "validate", "photo_path": item["path"]})[
                "photo_b64"
            ]
            total += len(base64.b64decode(encoded))
            if total > MAX_IMAGE_BYTES:
                raise ValueError("article_photos_total_exceeds_9_mib")
            photos.append({"id": item["id"], "data": encoded})
        payload["article"] = {"html": article["html"], "photos": photos}
        return payload
    path = payload.pop("photo_path", None)
    if path:
        resolved = Path(path).resolve(strict=True)
        if not resolved.is_relative_to(MEDIA_ROOT.resolve()) or not resolved.is_file():
            raise ValueError("photo_outside_media_directory")
        if resolved.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("unsupported_photo_type")
        with resolved.open("rb") as handle:
            raw = handle.read(MAX_IMAGE_BYTES + 1)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("photo_too_large")
        payload["photo_b64"] = base64.b64encode(raw).decode()
    return payload


class Controller:
    def __init__(self, owner_id):
        self.owner_id = str(owner_id)
        self.sessions = {}
        self.permits = {}
        self.lock = threading.Lock()

    def observe(
        self,
        *,
        session_id="",
        turn_id="",
        sender_id="",
        platform="",
        chat_type="",
        raw_user_message=None,
        is_internal_event=None,
        **_,
    ):
        platform = str(getattr(platform, "value", platform)).lower()
        chat_type = str(getattr(chat_type, "value", chat_type)).lower()
        authorized = (
            bool(session_id)
            and bool(turn_id)
            and str(sender_id) == self.owner_id
            and platform == "telegram"
            and chat_type == "dm"
            and is_internal_event is False
            and isinstance(raw_user_message, str)
            and bool(raw_user_message.strip())
        )
        with self.lock:
            old = self.sessions.get(str(session_id))
            if old is None or old[0] != str(turn_id) or not authorized:
                self.permits.pop(str(session_id), None)
            if authorized:
                self.sessions[str(session_id)] = (str(turn_id), time.monotonic() + 600)
            else:
                self.sessions.pop(str(session_id), None)

    def authorized(self, session_id):
        record = self.sessions.get(str(session_id))
        return bool(record and record[1] > time.monotonic())

    def guard(
        self,
        *,
        tool_name="",
        args=None,
        session_id="",
        turn_id="",
        tool_call_id="",
        **_,
    ):
        if tool_name != "ai_fixer":
            return None
        if not self.authorized(session_id):
            return {
                "action": "block",
                "message": "AI Fixer доступен только из личного текущего диалога Павла. Cron и чужие чаты запрещены.",
            }
        if not isinstance(args, dict):
            return {"action": "block", "message": "Некорректные аргументы AI Fixer."}
        if args.get("op") in WRITES:
            record = self.sessions[str(session_id)]
            if record[0] != str(turn_id):
                return {
                    "action": "block",
                    "message": "Нужно новое поручение владельца.",
                }
            digest = fingerprint(args)
            with self.lock:
                self.permits.pop(str(session_id), None)
            if args["op"] in {"queue_set", "queue_cancel"}:
                message = (
                    ("Одобрить постановку/перенос точной версии поста в очередь @ProAiCommunity?" if args["op"] == "queue_set"
                     else "Снять этот черновик с публикации?")
                    + "\nЧерновик: " + str(args.get("draft_id", ""))
                    + "\nВремя: " + str(args.get("scheduled_at", "отмена"))
                    + "\nHash: " + str(args.get("confirm_hash", ""))
                )
            elif args["op"] in {"tg_publish", "tg_manage"}:
                try:
                    action = prepare_spec(telegram_payload(args)["spec"])
                except Exception:
                    return {
                        "action": "block",
                        "message": "Сначала проверьте корректный JSON через tg_validate.",
                    }
                description = action.summary()
                message = (
                    "Разрешить действие редактора в @ProAiCommunity?\n"
                    f"Действие: {action.kind}; сообщение: {description.get('message_id')}\n"
                    + str(description.get("description", ""))[:1500]
                    + "\nHash подтверждения: "
                    + str(args.get("confirm_hash", ""))
                )
            elif args["op"] == "long_publish":
                message = (
                    "Опубликовать показанную статью с каруселью в @ProAiCommunity?\n"
                    + str(args.get("article_path", ""))
                    + "\nПроверенный hash: "
                    + str(args.get("confirm_hash", ""))
                )
            elif args["op"] == "publish":
                detail = str(args.get("text", ""))
                if args.get("photo_path"):
                    detail += "\nИзображение: " + str(args["photo_path"])
                message = "Опубликовать в @ProAiCommunity через AI Fixer?\n" + detail
            elif args["op"] == "reply":
                message = (
                    "Разрешить редактору проверить комментарий "
                    + str(args.get("message_id"))
                    + " и ответить только при прохождении ограничений? Уже отвеченное не дублируется."
                )
            else:
                model = args.get("model", "gpt-image-2")
                credits = {"gpt-image-2": 600, "nano-banana-2": 1200}.get(
                    model, "неизвестно"
                )
                message = (
                    f"Создать одно изображение через GRSAI, модель {model}, {credits} кредитов?\n"
                    f"Референс Павла: {args.get('use_pavel_reference', True)}\n"
                    + str(args.get("prompt", ""))
                )
            with self.lock:
                self.permits[str(session_id)] = (digest, time.monotonic() + 300)
            return {
                "action": "approve",
                "message": message,
                "rule_key": f"ai-fixer:{session_id}:{turn_id}:{tool_call_id}:{digest}",
            }
        return None

    def handle(self, args, *, session_id="", **_):
        if not self.authorized(session_id):
            return json.dumps({"ok": False, "error": "owner_dm_required"})
        try:
            if (not isinstance(args, dict) or set(args) - ALLOWED
                    or args.get("op") not in SCHEMA["parameters"]["properties"]["op"]["enum"]):
                raise ValueError("invalid_arguments")
            if args.get("op") in WRITES:
                with self.lock:
                    permit = self.permits.pop(str(session_id), None)
                if (
                    not permit
                    or permit[0] != fingerprint(args)
                    or permit[1] < time.monotonic()
                ):
                    return json.dumps(
                        {"ok": False, "error": "single_use_approval_required"}
                    )
            if args.get("op") == "reference":
                return json.dumps(reference_info(), ensure_ascii=False)
            if args.get("op") == "draft_preview":
                payload = request_payload(args)
                return json.dumps(draft_preview(owner_id=self.owner_id, request_id=args["request_id"],
                    content=payload["content"], rubric=payload["rubric"], ledger_path=PROFILE / "ai-fixer-previews.sqlite3"), ensure_ascii=False)
            if args.get("op") == "draft_refresh":
                if set(args) != {"op", "draft_id"}:
                    raise ValueError("invalid_refresh_arguments")
                return json.dumps(refresh_controls(self.owner_id, args["draft_id"]), ensure_ascii=False)
            long_preview = args.get("op") == "long_preview"
            telegram_preview = args.get("op") == "tg_preview"
            preview = args.get("op") == "preview" or long_preview or telegram_preview
            if (
                preview
                and not long_preview
                and not telegram_preview
                and (
                    set(args) - {"op", "request_id", "text", "photo_path"}
                    or not args.get("photo_path")
                )
            ):
                raise ValueError("preview_requires_one_photo_and_no_target")
            observed_turn = self.sessions.get(str(session_id))
            payload = request_payload(args)
            if preview:
                payload["op"] = (
                    "tg_validate"
                    if telegram_preview
                    else "long_validate"
                    if long_preview
                    else "validate"
                )
                if (
                    telegram_preview
                    and not prepare_spec(payload["spec"]).creates_message
                ):
                    raise ValueError("private_preview_supports_new_messages_only")
            result = subprocess.run(
                ["/usr/bin/sudo", "-n", CONTROL],
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=90,
                check=False,
            )
            if result.returncode != 0:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "broker_unavailable",
                        "exit_code": result.returncode,
                    }
                )
            parsed = json.loads(result.stdout)
            if args.get("op") == "tg_capabilities" and parsed.get("ok"):
                parsed["owner_buttons"] = callback_supported()
                parsed["owner_buttons_note"] = "Проверка callback bridge в текущем Hermes runtime; после обновления нужен private smoke."
            if args.get("op") in {"queue_set", "queue_cancel"} and parsed.get("ok"):
                ui = refresh_controls(self.owner_id, args["draft_id"])
                parsed["controls_refreshed"] = ui.get("controls_refreshed", False)
            if preview:
                if not parsed.get("ok"):
                    return json.dumps(parsed, ensure_ascii=False)
                if (
                    not self.authorized(session_id)
                    or self.sessions.get(str(session_id)) != observed_turn
                ):
                    raise ValueError("preview_owner_turn_expired")
                if long_preview:
                    return json.dumps(
                        deliver_rich_preview(
                            owner_id=self.owner_id,
                            request_id=args["request_id"],
                            article=payload["article"],
                            ledger_path=PROFILE / "ai-fixer-previews.sqlite3",
                        ),
                        ensure_ascii=False,
                    )
                if telegram_preview:
                    return json.dumps(
                        deliver_telegram_preview(
                            owner_id=self.owner_id,
                            request_id=args["request_id"],
                            spec=payload["spec"],
                            ledger_path=PROFILE / "ai-fixer-previews.sqlite3",
                        ),
                        ensure_ascii=False,
                    )
                return json.dumps(
                    deliver_preview(
                        owner_id=self.owner_id,
                        request_id=args["request_id"],
                        text=args["text"],
                        raw=base64.b64decode(payload["photo_b64"], validate=True),
                        ledger_path=PROFILE / "ai-fixer-previews.sqlite3",
                    ),
                    ensure_ascii=False,
                )
            encoded = parsed.pop("image_b64", None)
            if encoded:
                raw = base64.b64decode(encoded, validate=True)
                metadata = parsed["image"]
                if (
                    len(raw) > 32 * 1024 * 1024
                    or hashlib.sha256(raw).hexdigest() != metadata["sha256"]
                ):
                    raise ValueError("image_integrity_failure")
                suffix = metadata["suffix"]
                if suffix not in (".png", ".jpg"):
                    raise ValueError("image_suffix_not_allowed")
                MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
                output = MEDIA_ROOT / (
                    "grsai-"
                    + hashlib.sha256(parsed["request_id"].encode()).hexdigest()[:24]
                    + suffix
                )
                if (
                    output.exists()
                    and hashlib.sha256(output.read_bytes()).hexdigest()
                    != metadata["sha256"]
                ):
                    raise ValueError("image_output_conflict")
                output.write_bytes(raw)
                output.chmod(0o600)
                parsed["local_path"] = str(output)
                parsed["delivery_instruction"] = (
                    "Для готового поста используй ai_fixer preview с text и photo_path=local_path: фото и подпись одним сообщением владельцу. Для отдельной картинки без поста допустимо вложение в ответе. Не публикуй в канал без отдельного подтверждения."
                )
            return json.dumps(parsed, ensure_ascii=False)
        except subprocess.TimeoutExpired:
            return json.dumps(
                {
                    "ok": False,
                    "error": "broker_timeout",
                    "retry_allowed": False,
                    "readback_required": args.get("op") in WRITES,
                }
            )
        except Exception as exc:
            return json.dumps(
                {
                    "ok": False,
                    "error": "request_rejected",
                    "error_type": type(exc).__name__,
                }
            )


SCHEMA = {
    "name": "ai_fixer",
    "description": "AI Fixer, @ProAiCommunity: draft_preview (пост+кнопки владельцу), drafts/draft_refresh, queue_plan/queue_set/queue_cancel (одобренная очередь), analytics. Telegram toolkit: tg_capabilities, tg_objects, tg_validate, tg_preview, tg_publish, tg_manage: опросы/quiz, URL-кнопки, медиа, свои правки. Public writes только с owner approval. Навык ai-fixer-editor/references/editor-workflow.md. Короткие: validate/preview/publish. Статьи: long_validate/long_preview/long_publish. Комментарии: comments/reply. Картинки: reference, image_status/image_submit/image_poll/image_fetch. Чужие чаты/рассылки/баны/удаления запрещены.",
    "parameters": {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": [
                    "draft_preview", "drafts", "draft_refresh", "queue_plan", "queue_set", "queue_cancel", "analytics",
                    "reference",
                    "status",
                    "comments",
                    "validate",
                    "preview",
                    "publish",
                    "long_validate",
                    "long_preview",
                    "long_publish",
                    "tg_capabilities",
                    "tg_objects",
                    "tg_validate",
                    "tg_preview",
                    "tg_publish",
                    "tg_manage",
                    "reply",
                    "image_status",
                    "image_submit",
                    "image_poll",
                    "image_fetch",
                ],
            },
            "text": {
                "type": "string",
                "description": "Полный HTML поста. Без длинного тире.",
            },
            "draft_id": {"type": "string", "description": "Точный draft_id из draft_preview/drafts, не ID сообщения."},
            "scheduled_at": {"type": "string", "description": "ISO 8601 с часовым поясом, например 2026-09-06T11:30:00+03:00. queue_plan сначала; затем queue_set с его confirm_hash и отдельным owner approval. Также для переноса."},
            "rubric": {"type": "string", "maxLength": 80, "description": "Рубрика для аналитики нового draft_preview; не выдумывай статистику старых рубрик."},
            "request_id": {
                "type": "string",
                "description": "Уникальный ID; сохранять при повторной проверке того же запроса.",
            },
            "photo_path": {
                "type": "string",
                "description": "Готовое PNG/JPEG/WebP внутри /home/pavel/workspace/ai-fixer-media, до 9 MiB.",
            },
            "article_path": {
                "type": "string",
                "description": "Для long_validate/long_preview/long_publish: JSON внутри ai-fixer-media с html и photos:[{id,path}]. Длинная нативная статья с каруселью; инструкция ai-fixer-editor/references/long-posts.md. long_preview только владельцу; long_publish требует отдельного подтверждения и confirm_hash.",
            },
            "telegram_spec_path": {
                "type": "string",
                "description": "JSON внутри ai-fixer-media для tg_validate/tg_preview/tg_publish/tg_manage. Опрос, quiz, текст/медиа с кнопками, альбом, правка/закреп/реакция/закрытие своего опроса. Сначала skill ai-fixer-editor/references/telegram-toolkit.md. Без chat_id и методов API.",
            },
            "confirm_hash": {
                "type": "string",
                "description": "Точный payload_hash, возвращённый validate.",
            },
            "message_id": {
                "type": "integer",
                "description": "ID комментария из comments.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "model": {
                "type": "string",
                "enum": ["gpt-image-2", "nano-banana-2"],
                "default": "gpt-image-2",
            },
            "prompt": {
                "type": "string",
                "description": "Точный промпт картинки, до 6000 символов.",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": [
                    "1:1",
                    "16:9",
                    "9:16",
                    "4:3",
                    "3:4",
                    "3:2",
                    "2:3",
                    "4:5",
                    "5:4",
                    "21:9",
                ],
            },
            "image_size": {
                "type": "string",
                "enum": ["1K", "2K", "4K"],
                "description": "GPT Image 2 только 1K; Nano Banana 2 поддерживает 1K/2K/4K. По умолчанию 1K.",
            },
            "use_pavel_reference": {
                "type": "boolean",
                "default": True,
                "description": "Передать сохранённый портрет как реальный image input. False только когда Павла на картинке нет.",
            },
            "confirm_credits": {
                "type": "integer",
                "description": "Точная стоимость: gpt-image-2 = 600, nano-banana-2 = 1200. Не разрешает расход без подтверждения владельца.",
            },
        },
        "required": ["op"],
        "additionalProperties": False,
    },
}


def register(ctx):
    from hermes_cli.config import load_config

    config = load_config()
    owner_id = config["platforms"]["telegram"]["home_channel"]["chat_id"]
    controller = Controller(owner_id)
    register_callbacks(ctx, owner_id)
    ctx.register_hook("pre_llm_call", controller.observe)
    ctx.register_hook("pre_tool_call", controller.guard)
    ctx.register_tool(
        name="ai_fixer",
        toolset="ai_fixer",
        schema=SCHEMA,
        handler=controller.handle,
        check_fn=lambda: Path(CONTROL).is_file(),
        emoji="📝",
    )
