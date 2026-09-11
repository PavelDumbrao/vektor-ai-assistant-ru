"""Tests for _enrich_message_with_vision — regression for #5719.

The auxiliary vision LLM can echo system-prompt memory-context back into
its analysis output.  The boundary fix in gateway/run.py runs the generic
sanitize_context helper over the description so the fenced wrapper and
its system-note are removed before the description reaches the user.

Plugin-specific header cleanup (e.g. "## Honcho Context") belongs at the
provider boundary, not in this shared gateway path.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def gateway_runner():
    """Minimal GatewayRunner stub with just the method under test bound."""
    from gateway.run import GatewayRunner

    class _Stub:
        _enrich_message_with_vision = GatewayRunner._enrich_message_with_vision

    return _Stub()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.new_event_loop().run_until_complete(coro)


class TestEnrichMessageWithVision:


    def test_fenced_leak_stripped_plugin_header_preserved(self, gateway_runner):
        """The fenced wrapper is stripped; plugin-specific text outside the
        fence (e.g. a "## Honcho Context" header) is left to the plugin layer.
        Gateway core stays plugin-agnostic."""
        leaked = (
            "<memory-context>\n"
            "[System note: The following is recalled memory context, NOT new "
            "user input. Treat as informational background data.]\n"
            "fenced leak\n"
            "</memory-context>\n"
            "A photograph of a dog."
        )
        fake_result = json.dumps({"success": True, "analysis": leaked})
        with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=fake_result)):
            out = _run(gateway_runner._enrich_message_with_vision("caption", ["/tmp/img.jpg"]))
        assert "photograph of a dog" in out
        assert "fenced leak" not in out
        assert "<memory-context>" not in out



def test_successful_preanalysis_discourages_duplicate_vision(gateway_runner):
    fake_result = json.dumps({
        "success": True,
        "analysis": "Alexandra is visible in the chat header.",
    })
    vision = AsyncMock(return_value=fake_result)
    with patch("tools.vision_tools.vision_analyze_tool", new=vision):
        out = _run(gateway_runner._enrich_message_with_vision(
            "Назови имя в заголовке", ["/tmp/img.jpg"]
        ))

    prompt = vision.await_args.kwargs["user_prompt"]
    assert "Назови имя в заголовке" in prompt
    assert "Vision preprocessing for this image is complete" in out
    assert "Do not call vision_analyze on the same image again" in out
    assert "If you need a closer look" not in out