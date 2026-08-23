"""Standalone Hermes Passive Secretary plugin.

The collector receives only capability-free JSON DTOs.  A separate disabled-
by-default outbound tool can ask core for a one-time owner approval and a
single live-adapter Business reply; the plugin never receives a PTB ``Bot`` or
bot token.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .controller import (
    EXACT_DATE_TOOL_SCHEMA,
    REPLY_TOOL_SCHEMA,
    SOURCES_TOOL_SCHEMA,
    PassiveSecretaryController,
)
from .settings import load_settings


logger = logging.getLogger(__name__)
_controller: PassiveSecretaryController | None = None


def register(ctx) -> None:
    global _controller
    plugin_dir = Path(__file__).resolve().parent
    _controller = PassiveSecretaryController(load_settings(plugin_dir / "settings.json"))

    def passive_update(event, **kwargs):
        assert _controller is not None
        return _controller.on_passive_update(event, **kwargs)

    def pre_llm_call(**kwargs):
        assert _controller is not None
        return _controller.on_pre_llm_call(**kwargs)

    def exact_date(args, **kwargs):
        assert _controller is not None
        return _controller.handle_exact_date(args, **kwargs)

    def sources(args, **kwargs):
        assert _controller is not None
        return _controller.handle_sources(args, **kwargs)

    def pre_tool_call(**kwargs):
        assert _controller is not None
        return _controller.on_pre_tool_call(**kwargs)

    async def reply(args, **kwargs):
        assert _controller is not None
        return await _controller.handle_reply(args, **kwargs)

    ctx.register_hook("telegram_passive_update", passive_update)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_tool(
        name="passive_secretary_search",
        toolset="passive_secretary",
        schema=EXACT_DATE_TOOL_SCHEMA,
        handler=exact_date,
        check_fn=_controller.tool_available,
        description="Exact-date search in the owner's passive Telegram archive.",
        emoji="🗄️",
    )
    ctx.register_tool(
        name="passive_secretary_sources",
        toolset="passive_secretary",
        schema=SOURCES_TOOL_SCHEMA,
        handler=sources,
        check_fn=_controller.tool_available,
        description="Resolve safe opaque selectors for Telegram archive sources.",
        emoji="🔎",
    )
    ctx.register_tool(
        name="passive_secretary_reply",
        toolset="passive_secretary_outbound",
        schema=REPLY_TOOL_SCHEMA,
        handler=reply,
        check_fn=_controller.reply_tool_available,
        description=(
            "Explicit owner-requested, owner-confirmed one-time Telegram Business "
            "reply through the live adapter."
        ),
        emoji="✉️",
    )
    logger.info(
        "Passive secretary plugin registered (capture_enabled=%s)",
        _controller.settings.capture_enabled,
    )
