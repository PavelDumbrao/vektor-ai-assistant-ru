from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "modules" / "passive-secretary" / "hermes-core-patch"
sys.path.insert(0, str(CORE))
LIVE_HERMES = Path("/home/pavel/.hermes/hermes-agent")
if LIVE_HERMES.is_dir():
    sys.path.append(str(LIVE_HERMES))

_CORE_IMPORT_ERROR = None
try:
    import importlib.util

    adapter_path = CORE / "plugins" / "platforms" / "telegram" / "adapter.py"
    spec = importlib.util.spec_from_file_location(
        "passive_identity_test_adapter",
        adapter_path,
    )
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError("cannot load patched Telegram adapter")
    adapter_module = importlib.util.module_from_spec(spec)
    sys.modules["passive_identity_test_adapter"] = adapter_module
    spec.loader.exec_module(adapter_module)

    ChatType = adapter_module.ChatType
    TelegramAdapter = adapter_module.TelegramAdapter
    bind_passive_identity_capability = adapter_module.bind_passive_identity_capability
    resolve_telegram_identities_for_current_session = (
        adapter_module.resolve_telegram_identities_for_current_session
    )
    unbind_passive_identity_capability = adapter_module.unbind_passive_identity_capability
    from tools.approval import reset_current_session_key, set_current_session_key
except (ModuleNotFoundError, ImportError) as exc:
    _CORE_IMPORT_ERROR = exc
    ChatType = None
    TelegramAdapter = None



class FakeBot:
    def __init__(self, chats):
        self.chats = chats
        self.calls = []

    async def get_chat(self, chat_id):
        self.calls.append(chat_id)
        value = self.chats.get(chat_id)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RuntimeError("not found")
        return value


def adapter_with_bot(bot: FakeBot) -> TelegramAdapter:
    adapter = object.__new__(TelegramAdapter)
    adapter.config = SimpleNamespace(
        extra={
            "business_updates_mode": "passive",
            "business_owner_ids": [1],
        }
    )
    adapter._bot = bot
    return adapter


class LoopThread:
    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever,
            daemon=True,
        )
        self.thread.start()
        return self.loop

    def __exit__(self, *_args):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.loop.close()


@unittest.skipIf(_CORE_IMPORT_ERROR is not None, f"full Hermes core unavailable: {_CORE_IMPORT_ERROR}")
class PassiveIdentityCoreTests(unittest.TestCase):
    def test_adapter_resolves_exact_private_ids_and_null_username(self):
        bot = FakeBot({
            10: SimpleNamespace(
                id=10,
                type=ChatType.PRIVATE,
                username="roman",
                first_name="Roman",
                last_name="M",
            ),
            11: SimpleNamespace(
                id=11,
                type=ChatType.PRIVATE,
                username=None,
                first_name="Evgeniy",
                last_name="K",
            ),
        })
        adapter = adapter_with_bot(bot)
        results = asyncio.run(
            adapter.resolve_private_chat_identities(owner_id=1, chat_ids=[10, 11])
        )
        self.assertEqual([r.status for r in results], ["resolved", "resolved"])
        self.assertEqual(results[0].telegram_id, 10)
        self.assertEqual(results[0].username, "roman")
        self.assertIsNone(results[1].username)
        self.assertEqual(bot.calls, [10, 11])

    def test_adapter_rejects_identity_mismatch(self):
        bot = FakeBot({
            10: SimpleNamespace(
                id=99,
                type=ChatType.PRIVATE,
                username="wrong",
                first_name="Wrong",
                last_name="User",
            )
        })
        adapter = adapter_with_bot(bot)
        result = asyncio.run(
            adapter.resolve_private_chat_identities(owner_id=1, chat_ids=[10])
        )[0]
        self.assertEqual(result.status, "failed_known")
        self.assertEqual(result.error_code, "identity_mismatch")

    def test_bridge_is_bound_to_current_owner_session(self):
        bot = FakeBot({
            10: SimpleNamespace(
                id=10,
                type=ChatType.PRIVATE,
                username="roman",
                first_name="Roman",
                last_name="M",
            )
        })
        adapter = adapter_with_bot(bot)
        with LoopThread() as loop:
            token = bind_passive_identity_capability(
                "owner-session",
                owner_id=1,
                owner_chat_id=1,
                adapter=adapter,
                loop=loop,
            )
            self.assertIsNotNone(token)
            context_token = set_current_session_key("owner-session")
            try:
                result = resolve_telegram_identities_for_current_session(
                    owner_id="1",
                    chat_ids=[10],
                )
                self.assertEqual(result[0]["telegram_id"], 10)
                self.assertEqual(result[0]["username"], "roman")
                self.assertEqual(
                    resolve_telegram_identities_for_current_session(
                        owner_id="2",
                        chat_ids=[10],
                    ),
                    [],
                )
            finally:
                reset_current_session_key(context_token)
                unbind_passive_identity_capability("owner-session", token)

    def test_explicit_agent_session_alias_resolves(self):
        bot = FakeBot({
            10: SimpleNamespace(
                id=10,
                type=ChatType.PRIVATE,
                username="roman",
                first_name="Roman",
                last_name="M",
            )
        })
        adapter = adapter_with_bot(bot)
        with LoopThread() as loop:
            token = bind_passive_identity_capability(
                "agent-session-id",
                owner_id=1,
                owner_chat_id=1,
                adapter=adapter,
                loop=loop,
            )
            self.assertIsNotNone(token)
            context_token = set_current_session_key("gateway-session-key")
            try:
                result = resolve_telegram_identities_for_current_session(
                    owner_id="1",
                    chat_ids=[10],
                    session_id="agent-session-id",
                )
                self.assertEqual(result[0]["telegram_id"], 10)
                self.assertEqual(result[0]["username"], "roman")
            finally:
                reset_current_session_key(context_token)
                unbind_passive_identity_capability("agent-session-id", token)

    def test_unbound_session_cannot_resolve(self):
        context_token = set_current_session_key("not-bound")
        try:
            self.assertEqual(
                resolve_telegram_identities_for_current_session(
                    owner_id="1",
                    chat_ids=[10],
                ),
                [],
            )
        finally:
            reset_current_session_key(context_token)


if __name__ == "__main__":
    unittest.main()
