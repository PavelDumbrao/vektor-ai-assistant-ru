from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

import httpx

from .telegram_content import execute_spec


class TelegramApiError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str, *, timeout: float = 40.0):
        self._token = token
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))

    async def close(self) -> None:
        await self._client.aclose()

    def _redact(self, value: str) -> str:
        return value.replace(self._token, "<redacted-token>")

    async def _post(self, method: str, *, data: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.post(f"{self._base}/{method}", json=data or {})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise TelegramApiError(self._redact(str(exc))) from exc
        if not payload.get("ok"):
            description = self._redact(str(payload.get("description", "Telegram API error")))
            raise TelegramApiError(description)
        return payload

    async def get_me(self) -> dict[str, Any]:
        return (await self._post("getMe"))["result"]

    async def get_chat_member(self, chat_id: int, user_id: int) -> dict[str, Any]:
        return (await self._post("getChatMember", data={"chat_id": chat_id, "user_id": user_id}))["result"]

    async def get_updates(self, *, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
        data: dict[str, Any] = {
            "timeout": timeout,
            "limit": 100,
            "allowed_updates": ["message", "edited_message", "channel_post", "edited_channel_post", "my_chat_member", "poll"],
        }
        if offset is not None:
            data["offset"] = offset
        payload = await self._post("getUpdates", data=data)
        return list(payload.get("result") or [])

    async def send_reply(self, *, chat_id: int, message_id: int, text: str) -> dict[str, Any]:
        payload = await self._post(
            "sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "link_preview_options": {"is_disabled": True},
                "reply_parameters": {
                    "message_id": message_id,
                    "allow_sending_without_reply": False,
                },
            },
        )
        return payload["result"]

    async def send_text(self, *, chat_id: int, text: str) -> dict[str, Any]:
        payload = await self._post(
            "sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": {"prefer_large_media": True, "show_above_text": False},
            },
        )
        return payload["result"]

    async def send_photo(self, *, chat_id: int, photo: Path, caption: str) -> dict[str, Any]:
        if not photo.is_file():
            raise TelegramApiError(f"Photo file does not exist: {photo}")
        content_type = mimetypes.guess_type(photo.name)[0] or "application/octet-stream"
        try:
            with photo.open("rb") as handle:
                response = await self._client.post(
                    f"{self._base}/sendPhoto",
                    data={"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"},
                    files={"photo": (photo.name, handle, content_type)},
                )
            response.raise_for_status()
            payload = response.json()
        except (OSError, httpx.HTTPError, json.JSONDecodeError) as exc:
            raise TelegramApiError(self._redact(str(exc))) from exc
        if not payload.get("ok"):
            raise TelegramApiError(self._redact(str(payload.get("description", "Telegram API error"))))
        return payload["result"]

    async def send_rich_post(self, *, chat_id: int, article) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"{self._base}/sendRichMessage",
                data={"chat_id": str(chat_id), "rich_message": json.dumps(article.wire(), ensure_ascii=False)},
                files=article.files(),
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise TelegramApiError(self._redact(str(exc))) from exc
        if not payload.get("ok"):
            raise TelegramApiError(self._redact(str(payload.get("description", "Telegram API error"))))
        result = payload["result"]
        if not result.get("rich_message") or result.get("chat", {}).get("id") != chat_id:
            raise TelegramApiError("rich_message_response_mismatch")
        return result

    async def send_spec(self, *, chat_id: int, spec) -> dict[str, Any]:
        return await execute_spec(self._client, self._base, chat_id, spec)
