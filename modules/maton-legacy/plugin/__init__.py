"""Telegram DM onboarding for a per-client Maton credential.

The incoming credential is consumed by ``pre_gateway_dispatch`` before Hermes
creates a session, writes a transcript, or invokes an LLM.  Telegram deletion
and validation happen in a tracked async task; the hook itself stays
synchronous because that is the contract exposed by Hermes plugins.
"""

from __future__ import annotations

import logging
from pathlib import Path

from hermes_constants import get_hermes_home

from .controller import MatonChatController, load_settings


logger = logging.getLogger(__name__)
_controller: MatonChatController | None = None


def register(ctx) -> None:
    global _controller

    plugin_dir = Path(__file__).resolve().parent
    _controller = MatonChatController(
        hermes_home=get_hermes_home(),
        settings=load_settings(plugin_dir / "settings.json"),
    )

    def intercept(event, gateway, **_kwargs):
        assert _controller is not None
        return _controller.intercept(event, gateway)

    def command_fallback(_args: str) -> str:
        return (
            "Откройте личный чат с ботом и отправьте /maton ещё раз. "
            "API-ключи принимаются только в личных сообщениях."
        )

    ctx.register_hook("pre_gateway_dispatch", intercept)
    ctx.register_command(
        "maton",
        command_fallback,
        description="Подключить Maton",
    )
    ctx.register_command(
        "maton_cancel",
        command_fallback,
        description="Отменить подключение Maton",
    )
    logger.info("Maton Telegram onboarding hook registered")
