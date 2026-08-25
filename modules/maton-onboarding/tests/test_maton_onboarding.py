from __future__ import annotations

import asyncio
import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


PLUGIN = Path(__file__).resolve().parents[1] / "plugin" / "__init__.py"
spec = importlib.util.spec_from_file_location(
    "maton_onboarding_under_test",
    PLUGIN,
    submodule_search_locations=[str(PLUGIN.parent)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
assert spec.loader is not None
spec.loader.exec_module(module)
_ORIGINAL_GET_HOME = module._get_home


class _Platform:
    value = "telegram"


def _event(text: str):
    return SimpleNamespace(
        text=text,
        message_id="42",
        source=SimpleNamespace(
            platform=_Platform(),
            chat_id="100",
            user_id="100",
            chat_type="dm",
        ),
    )


class MatonOnboardingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        module._get_home = _ORIGINAL_GET_HOME
        module._pending.clear()
        module._attempts.clear()
        module._tasks.clear()

    def test_command_parser_never_returns_secret_for_non_command(self):
        self.assertEqual(module._command_parts("plain-secret"), ("", ""))
        self.assertEqual(
            module._command_parts("/maton_key key-value"),
            ("maton_key", "key-value"),
        )

    def test_key_shape_is_bounded_ascii_without_whitespace(self):
        self.assertTrue(module._valid_key_shape("a" * 20))
        self.assertFalse(module._valid_key_shape("short"))
        self.assertFalse(module._valid_key_shape("a" * 19 + " "))
        self.assertFalse(module._valid_key_shape("а" * 32))
        self.assertFalse(module._valid_key_shape("a" * 513))

    def test_atomic_secret_write_is_owner_only_and_replaces(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            (home / ".env").write_text("A=1\nMCP_MATON_API_KEY=old\n", encoding="utf-8")
            module._get_home = lambda: home
            prior = os.environ.get("MCP_MATON_API_KEY")
            try:
                module._atomic_store_secret("x" * 32)
                text = (home / ".env").read_text(encoding="utf-8")
                self.assertEqual(text.count("MCP_MATON_API_KEY="), 1)
                self.assertIn("A=1", text)
                self.assertEqual(stat.S_IMODE((home / ".env").stat().st_mode), 0o600)
            finally:
                if prior is None:
                    os.environ.pop("MCP_MATON_API_KEY", None)
                else:
                    os.environ["MCP_MATON_API_KEY"] = prior

    async def test_secret_command_is_skipped_before_gateway_dispatch(self):
        seen = []

        async def fake_handle(gateway, event, value):
            seen.append(value)

        original = module._handle_key_message
        module._handle_key_message = fake_handle
        try:
            gateway = SimpleNamespace(_is_user_authorized=lambda source: True)
            result = module._on_pre_gateway_dispatch(
                _event("/maton_key " + "k" * 24), gateway
            )
            self.assertEqual(result["action"], "skip")
            await asyncio.gather(*list(module._tasks))
            self.assertEqual(seen, ["k" * 24])
            self.assertNotIn("k" * 24, str(result))
        finally:
            module._handle_key_message = original

    async def test_delete_failure_never_provisions_secret(self):
        adapter = SimpleNamespace(delete_message=AsyncMock(return_value=False), send=AsyncMock())
        gateway = SimpleNamespace(_adapter_for_source=lambda source: adapter)
        called = False

        def fake_provision(value):
            nonlocal called
            called = True
            return "valid"

        original = module._provision_key
        module._provision_key = fake_provision
        try:
            await module._handle_key_message(gateway, _event("secret"), "s" * 24)
            self.assertFalse(called)
            adapter.send.assert_awaited_once()
            self.assertNotIn("s" * 24, adapter.send.await_args.args[1])
        finally:
            module._provision_key = original

    async def test_success_response_never_echoes_secret(self):
        adapter = SimpleNamespace(delete_message=AsyncMock(return_value=True), send=AsyncMock())
        gateway = SimpleNamespace(_adapter_for_source=lambda source: adapter)
        secret = "z" * 32
        original_provision = module._provision_key
        original_reload = module._reload_maton
        module._provision_key = lambda value: "valid"

        async def fake_reload(gateway, event):
            return True

        module._reload_maton = fake_reload
        try:
            await module._handle_key_message(gateway, _event(secret), secret)
            payload = "\n".join(call.args[1] for call in adapter.send.await_args_list)
            self.assertNotIn(secret, payload)
            self.assertIn("Maton подключён", payload)
        finally:
            module._provision_key = original_provision
            module._reload_maton = original_reload


if __name__ == "__main__":
    unittest.main()
