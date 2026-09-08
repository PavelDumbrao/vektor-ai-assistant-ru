"""Owner-only Telegram controls. Callback capabilities never enter model output."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import subprocess
from pathlib import Path

from .preview import deliver_once
from .rich_post import prepare_article
from .telegram_content import execute_spec, prepare_spec


CONTROL = "/usr/local/bin/ai-fixer-control"
ADAPTER = Path("/home/pavel/.hermes/hermes-agent/plugins/platforms/telegram/adapter.py")


def callback_supported():
    try:
        adapter = ADAPTER.read_text(encoding="utf-8")
        if "telegram_callback_handlers" in adapter:
            return True  # Compatibility with the earlier, explicitly patched runtime.
        core = ADAPTER.parents[3] / "hermes_cli/plugins.py"
        return "_wire_plugin_handlers(app)" in adapter and "def register_telegram_handler(" in core.read_text(encoding="utf-8")
    except OSError:
        return False


def register_callbacks(ctx, owner_id):
    handler = CallbackHandler(owner_id)

    def wire(application, adapter):
        from telegram.ext import CallbackQueryHandler
        application.add_handler(CallbackQueryHandler(handler, pattern=r"^af:", block=True))

    native = getattr(ctx, "register_telegram_handler", None)
    if callable(native):
        native(wire)
    elif callback_supported():
        ctx.register_middleware("telegram_callback_handlers", lambda: {"pattern": r"^af:", "handler": handler})


def broker(payload):
    result = subprocess.run(["/usr/bin/sudo", "-n", CONTROL], input=json.dumps(payload, ensure_ascii=False),
                            text=True, capture_output=True, timeout=70, check=False)
    if result.returncode != 0:
        return {"ok": False, "error": "editor_broker_unavailable"}
    return json.loads(result.stdout)


def owner_config(owner_id):
    from gateway.config import Platform, load_gateway_config

    config = load_gateway_config().platforms[Platform.TELEGRAM]
    if not config.enabled or not config.token or not config.home_channel or str(config.home_channel.chat_id) != str(owner_id):
        raise ValueError("telegram_owner_mismatch")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return config


def send_editor_preview(owner_id, content, markup):
    from telegram import Bot
    import httpx

    config = owner_config(owner_id)

    async def send():
        async with Bot(token=config.token) as bot:
            if bot.username.lower() != "vektor_assist_bot":
                raise ValueError("preview_bot_mismatch")
        async with httpx.AsyncClient(timeout=httpx.Timeout(70, connect=10)) as client:
            base = f"https://api.telegram.org/bot{config.token}"
            if "spec" in content:
                prepared = prepare_spec(content["spec"])
                public_rows = prepared.params.get("reply_markup", {}).get("inline_keyboard", [])
                prepared.params["reply_markup"] = {"inline_keyboard": public_rows + markup["inline_keyboard"]}
                result = await execute_spec(client, base, int(owner_id), prepared)
                message_id = result["message_ids"][0]
            else:
                article = prepare_article(content["article"])
                response = await client.post(base + "/sendRichMessage",
                    data={"chat_id": str(owner_id), "rich_message": json.dumps(article.wire(), ensure_ascii=False),
                          "reply_markup": json.dumps(markup, ensure_ascii=False)}, files=article.files())
                data = response.json()
                message = data.get("result", {})
                if (not data.get("ok") or not message.get("rich_message") or
                        message.get("chat", {}).get("id") != int(owner_id) or not message.get("message_id")):
                    raise ValueError("editor_preview_response_uncertain")
                message_id = message["message_id"]
        return {"message_id": message_id, "has_content": True}

    return asyncio.run(send())


def draft_preview(*, owner_id, request_id, content, rubric, ledger_path):
    if not callback_supported():
        return {"ok": False, "error": "telegram_callback_adapter_missing", "sent": False}
    created = broker({"op": "draft_prepare", "request_id": request_id, "content": content, "rubric": rubric})
    if not created.get("ok"):
        return created
    binding = created.pop("bind_token")
    markup = created.pop("reply_markup")
    if created.get("preview_message_id"):
        return {**created, "message_id": created["preview_message_id"], "deduplicated": True,
                "published": created["status"] == "sent", "instruction": "Черновик уже показан; не отправляй снова."}
    if created["status"] != "preparing":
        return {"ok": False, "error": "draft_preview_requires_readback"}
    digest = hashlib.sha256(f"editor:{owner_id}:{created['draft_id']}:{created['payload_hash']}".encode()).hexdigest()
    delivered = deliver_once(owner_id=str(owner_id), request_id="editor-" + created["draft_id"], digest=digest,
                             ledger_path=ledger_path, required_field="has_content", delivery="owner_dm_editor_draft",
                             sender=lambda: send_editor_preview(owner_id, content, markup))
    if not delivered.get("ok"):
        return {**delivered, "draft_id": created["draft_id"]}
    bound = broker({"op": "draft_bind", "draft_id": created["draft_id"], "bind_token": binding, "message_id": delivered["message_id"]})
    if not bound.get("ok"):
        return {"ok": False, "error": "preview_sent_binding_failed", "message_id": delivered["message_id"],
                "draft_id": created["draft_id"], "retry_allowed": False, "readback_required": True}
    return {**bound, "message_id": delivered["message_id"], "delivery": "owner_dm_editor_draft", "published": False,
            "instruction": "Пост с кнопками уже у Павла. Только короткое подтверждение, без повторного текста/MEDIA. Сам кнопки публикации не нажимай."}


def refresh_controls(owner_id, draft_id):
    if not callback_supported():
        return {"ok": False, "error": "telegram_callback_adapter_missing", "controls_refreshed": False}
    from telegram import Bot

    result = broker({"op": "draft_controls", "draft_id": draft_id})
    if not result.get("ok"):
        return result
    markup = result.pop("reply_markup")
    config = owner_config(owner_id)

    async def update():
        async with Bot(token=config.token) as bot:
            if bot.username.lower() != "vektor_assist_bot":
                raise ValueError("preview_bot_mismatch")
            await bot.edit_message_reply_markup(chat_id=int(owner_id), message_id=result["preview_message_id"], reply_markup=markup)

    try:
        asyncio.run(update())
        result["controls_refreshed"] = True
    except Exception as exc:
        result["controls_refreshed"] = False
        result["error_type"] = type(exc).__name__
    return result


class CallbackHandler:
    def __init__(self, owner_id):
        self.owner_id = int(owner_id)

    async def __call__(self, update, context):
        query = update.callback_query
        message = getattr(query, "message", None)
        chat = getattr(message, "chat", None)
        if (not query or not message or getattr(query.from_user, "id", None) != self.owner_id
                or getattr(chat, "id", None) != self.owner_id or getattr(chat, "type", None) != "private"
                or getattr(getattr(message, "from_user", None), "id", None) != context.bot.id):
            if query:
                await query.answer("Эта кнопка доступна только Павлу в личке ВЕКТОРА.", show_alert=True)
            return
        try:
            result = await asyncio.to_thread(broker, {"op": "draft_click", "data": query.data,
                "user_id": self.owner_id, "chat_id": self.owner_id, "chat_type": "private",
                "message_id": message.message_id, "callback_id": str(query.id)})
        except Exception:
            await query.answer("Ответ не получен. Не повторяй подтверждение, сначала проверь очередь.", show_alert=True)
            return
        if not result.get("ok"):
            await query.answer("Кнопка устарела или действие недоступно. Попроси ВЕКТОРА проверить черновик.", show_alert=True)
            return
        await query.answer(result["notice"][:195], show_alert=True)
        try:
            # Content is never edited here, only the private workflow controls.
            await query.edit_message_reply_markup(reply_markup=result["reply_markup"])
        except Exception:
            # State already committed; do not repeat the state transition on UI failure.
            logging.getLogger(__name__).warning("editor_controls_refresh_failed draft_id=%s", result["draft_id"])
