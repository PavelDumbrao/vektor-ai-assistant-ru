from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.auxiliary_client import async_call_llm, call_llm, _is_server_error


class Server502(Exception):
    status_code = 502


def response(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def test_server_error_detector_accepts_5xx_and_rejects_4xx():
    assert _is_server_error(Server502("bad gateway")) is True
    bad = Exception("bad request")
    bad.status_code = 400
    assert _is_server_error(bad) is False


def test_sync_502_exhaustion_enters_configured_fallback_chain():
    primary = MagicMock()
    primary.base_url = "https://primary.example/v1"
    primary.chat.completions.create.side_effect = Server502("bad gateway")
    fallback = MagicMock()
    fallback.base_url = "https://fallback.example/v1"
    fallback.chat.completions.create.return_value = response("fallback-ok")

    with patch(
        "agent.auxiliary_client._resolve_task_provider_model",
        return_value=("primary-vision", "gpt-5.6-sol", None, None, None),
    ), patch(
        "agent.auxiliary_client.resolve_vision_provider_client",
        return_value=("primary-vision", primary, "gpt-5.6-sol"),
    ), patch(
        "agent.auxiliary_client._transient_retry_count", return_value=0,
    ), patch(
        "agent.auxiliary_client._try_configured_fallback_chain",
        return_value=(fallback, "gpt-5.6-terra", "fallback-provider"),
    ) as chain:
        result = call_llm(
            task="vision",
            messages=[{"role": "user", "content": "read image"}],
        )

    assert result.choices[0].message.content == "fallback-ok"
    assert primary.chat.completions.create.call_count == 1
    assert fallback.chat.completions.create.call_count == 1
    assert chain.call_args.kwargs["failed_model"] == "gpt-5.6-sol"
    assert chain.call_args.kwargs["reason"] == "server error"


@pytest.mark.asyncio
async def test_async_502_after_retry_enters_configured_fallback_chain():
    primary = MagicMock()
    primary.base_url = "https://primary.example/v1"
    primary.chat.completions.create = AsyncMock(
        side_effect=Server502("bad gateway")
    )
    fallback_sync = MagicMock()
    fallback_sync.base_url = "https://fallback.example/v1"
    fallback_async = MagicMock()
    fallback_async.base_url = "https://fallback.example/v1"
    fallback_async.chat.completions.create = AsyncMock(
        return_value=response("async-fallback-ok")
    )

    with patch(
        "agent.auxiliary_client._resolve_task_provider_model",
        return_value=("primary-vision", "gpt-5.6-sol", None, None, None),
    ), patch(
        "agent.auxiliary_client.resolve_vision_provider_client",
        return_value=("primary-vision", primary, "gpt-5.6-sol"),
    ), patch(
        "agent.auxiliary_client._try_configured_fallback_chain",
        return_value=(fallback_sync, "gpt-5.6-terra", "fallback-provider"),
    ) as chain, patch(
        "agent.auxiliary_client._to_async_client",
        return_value=(fallback_async, "gpt-5.6-terra"),
    ):
        result = await async_call_llm(
            task="vision",
            messages=[{"role": "user", "content": "read image"}],
        )
    assert result.choices[0].message.content == "async-fallback-ok"
    assert primary.chat.completions.create.await_count == 2
    assert fallback_async.chat.completions.create.await_count == 1
    assert chain.call_args.kwargs["failed_model"] == "gpt-5.6-sol"
    assert chain.call_args.kwargs["reason"] == "server error"

@pytest.mark.asyncio
async def test_async_runtime_failure_walks_to_next_configured_fallback():
    primary = MagicMock()
    primary.base_url = "https://primary.example/v1"
    primary.chat.completions.create = AsyncMock(side_effect=Server502("primary 502"))

    fb1_sync = MagicMock()
    fb1_sync.base_url = "https://fallback-one.example/v1"
    fb1_async = MagicMock()
    fb1_async.base_url = "https://fallback-one.example/v1"
    fb1_async.chat.completions.create = AsyncMock(side_effect=Server502("fb1 502"))

    fb2_sync = MagicMock()
    fb2_sync.base_url = "https://fallback-two.example/v1"
    fb2_async = MagicMock()
    fb2_async.base_url = "https://fallback-two.example/v1"
    fb2_async.chat.completions.create = AsyncMock(return_value=response("fb2-ok"))

    chain_results = [
        (fb1_sync, "gpt-5.6-terra", "fallback_chain[0](lingsuan-vision)"),
        (fb2_sync, "gpt-5.6-sol", "fallback_chain[1](gengruihuan-vision)"),
        (None, None, ""),
    ]

    def to_async(client, model, **_kwargs):
        if client is fb1_sync:
            return fb1_async, model
        if client is fb2_sync:
            return fb2_async, model
        raise AssertionError("unexpected fallback client")

    with patch(
        "agent.auxiliary_client._resolve_task_provider_model",
        return_value=("primary-vision", "gpt-5.6-sol", None, None, None),
    ), patch(
        "agent.auxiliary_client.resolve_vision_provider_client",
        return_value=("primary-vision", primary, "gpt-5.6-sol"),
    ), patch(
        "agent.auxiliary_client._try_configured_fallback_chain",
        side_effect=chain_results,
    ) as chain, patch(
        "agent.auxiliary_client._to_async_client", side_effect=to_async,
    ):
        result = await async_call_llm(
            task="vision",
            messages=[{"role": "user", "content": "read image"}],
        )

    assert result.choices[0].message.content == "fb2-ok"
    assert fb1_async.chat.completions.create.await_count == 1
    assert fb2_async.chat.completions.create.await_count == 1
    assert chain.call_count >= 2


@pytest.mark.asyncio
async def test_async_vision_timeout_skips_same_provider_retry():
    class VisionTimeout(Exception):
        pass
    VisionTimeout.__name__ = "APITimeoutError"

    primary = MagicMock()
    primary.base_url = "https://primary.example/v1"
    primary.chat.completions.create = AsyncMock(
        side_effect=VisionTimeout("Request timed out.")
    )
    fallback_sync = MagicMock()
    fallback_sync.base_url = "https://fallback.example/v1"
    fallback_async = MagicMock()
    fallback_async.base_url = "https://fallback.example/v1"
    fallback_async.chat.completions.create = AsyncMock(
        return_value=response("timeout-fallback-ok")
    )
    with patch(
        "agent.auxiliary_client._resolve_task_provider_model",
        return_value=("primary-vision", "gpt-5.6-sol", None, None, None),
    ), patch(
        "agent.auxiliary_client.resolve_vision_provider_client",
        return_value=("primary-vision", primary, "gpt-5.6-sol"),
    ), patch(
        "agent.auxiliary_client._try_configured_fallback_chain",
        return_value=(fallback_sync, "gpt-5.6-terra", "fallback-provider"),
    ), patch(
        "agent.auxiliary_client._to_async_client",
        return_value=(fallback_async, "gpt-5.6-terra"),
    ):
        result = await async_call_llm(
            task="vision",
            messages=[{"role": "user", "content": "read image"}],
        )

    assert result.choices[0].message.content == "timeout-fallback-ok"
    assert primary.chat.completions.create.await_count == 1
    assert fallback_async.chat.completions.create.await_count == 1