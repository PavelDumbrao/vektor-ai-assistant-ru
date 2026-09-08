"""Реестр «рук» — агентов на машинах Павла, которые умеют смотреть и нажимать.

Агент подключается сам, исходящим соединением. Сервис держит одну активную связь
и умеет отправить команду, дождавшись ответа. Разрешения проверяются дважды:
здесь по текущему режиму разговора и ещё раз на самой машине.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional

log = logging.getLogger("vektor.hands")

# Что разрешено в каком режиме. Должно совпадать с таблицей в агенте.
ALLOWED = {
    "watch": {"screenshot", "zoom", "find"},
    "hint": {"screenshot", "zoom", "find", "point"},
    "act": {"screenshot", "zoom", "find", "point", "click", "type", "key", "open_url"},
}

MODE_NAMES = {"watch": "смотрит", "hint": "подсказывает", "act": "действует"}


class HandsHub:
    def __init__(self) -> None:
        self.ws: Any = None
        self.info: dict[str, Any] = {}
        self._waiting: dict[str, asyncio.Future] = {}
        self.on_change = None  # корутина, которую дёргаем при подключении и отключении

    @property
    def connected(self) -> bool:
        return self.ws is not None

    async def attach(self, ws: Any, info: dict[str, Any]) -> None:
        if self.ws is not None:
            with_old = self.ws
            self.ws = None
            try:
                await with_old.close(code=1000)
            except Exception:  # noqa: BLE001
                pass
        self.ws = ws
        self.info = info
        log.info("руки подключены: экран %sx%s", info.get("screen_width"), info.get("screen_height"))
        await self._notify()

    async def detach(self, ws: Any) -> None:
        if self.ws is ws:
            self.ws = None
            self.info = {}
            log.info("руки отключены")
            await self._notify()
        for fut in list(self._waiting.values()):
            if not fut.done():
                fut.set_result({"ok": False, "error": "связь с машиной Павла оборвалась"})
        self._waiting.clear()

    async def _notify(self) -> None:
        if self.on_change:
            with_cb = self.on_change
            try:
                await with_cb()
            except Exception as exc:  # noqa: BLE001
                log.warning("уведомление о руках не прошло: %s", exc)

    def resolve(self, msg: dict[str, Any]) -> None:
        fut = self._waiting.pop(msg.get("id", ""), None)
        if fut and not fut.done():
            fut.set_result(msg)

    async def call(self, action: str, mode: str, timeout: float = 25.0, **params: Any) -> dict[str, Any]:
        if action not in ALLOWED.get(mode, set()):
            return {"ok": False,
                    "error": f"сейчас режим «{MODE_NAMES.get(mode, mode)}» — это действие в нём запрещено. "
                             f"Скажи Павлу, что нужен другой режим, и не пытайся обойти запрет."}
        if not self.connected:
            return {"ok": False,
                    "error": "руки не подключены: на маке Павла не запущен агент ВЕКТОРА"}
        req_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiting[req_id] = fut
        payload = {"id": req_id, "action": action, "mode": mode, **params}
        try:
            await self.ws.send_json(payload)
        except Exception as exc:  # noqa: BLE001
            self._waiting.pop(req_id, None)
            return {"ok": False, "error": f"команда не ушла: {type(exc).__name__}"}
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._waiting.pop(req_id, None)
            return {"ok": False, "error": "машина Павла не ответила вовремя"}


hub = HandsHub()
