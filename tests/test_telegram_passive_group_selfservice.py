from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "modules" / "passive-secretary" / "hermes-core-patch"
TELEGRAM = PATCH / "plugins" / "platforms" / "telegram"
for path in (PATCH, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from plugins.platforms.telegram.passive_updates import PassiveGroupRegistry  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402

OWNER = 100001
CHAT = -100999111222


class FakeRegistry:
    def __init__(self) -> None:
        self.state: dict[int, str] = {}
        self.begin_calls = 0

    def approved_ids(self, *, owner_id: int) -> set[int]:
        assert owner_id == OWNER
        return {cid for cid, state in self.state.items() if state == "approved"}

    def blocked_ids(self, *, owner_id: int) -> set[int]:
        assert owner_id == OWNER
        return {
            cid
            for cid, state in self.state.items()
            if state in {"pending", "denied"}
        }

    def begin(self, *, chat_id: int, owner_id: int, title: str, username=None, **_) -> str:
        assert owner_id == OWNER
        self.begin_calls += 1
        self.state[chat_id] = "pending"
        return "nonce-test-abcdefghijkl"

    def resolve(self, *, nonce: str, owner_id: int, approve: bool, **_):
        assert owner_id == OWNER
        if nonce != "nonce-test-abcdefghijkl":
            return None
        self.state[CHAT] = "approved" if approve else "denied"
        return {"chat_id": CHAT, "title": "Предзаказы"}

    def revoke(self, *, chat_id: int, owner_id: int) -> bool:
        assert owner_id == OWNER
        self.state[chat_id] = "denied"
        return True


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def get_me(self):
        return SimpleNamespace(id=200002, can_read_all_group_messages=True)

    async def get_chat_member(self, *, chat_id: int, user_id: int):
        assert chat_id == CHAT
        return SimpleNamespace(status="member", is_member=True)

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=1)

    async def leave_chat(self, **_):
        raise AssertionError("leave_chat must not run in approval path")


class FakeQuery:
    def __init__(self) -> None:
        self.data = "pg:y:nonce-test-abcdefghijkl"
        self.from_user = SimpleNamespace(id=OWNER)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=OWNER, type="private"))
        self.answers: list[dict] = []
        self.edits: list[dict] = []

    async def answer(self, **kwargs):
        self.answers.append(kwargs)

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)


def make_adapter() -> TelegramAdapter:
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = SimpleNamespace(value="telegram")
    adapter._bot = FakeBot()
    adapter._passive_group_registry = FakeRegistry()
    adapter._group_passive_owner_id = lambda: OWNER
    adapter._get_passive_group_registry = lambda: adapter._passive_group_registry
    adapter._group_passive_chat_ids = lambda: adapter._passive_group_registry.approved_ids(
        owner_id=OWNER
    )
    return adapter


def update(sender_id: int):
    chat = SimpleNamespace(
        id=CHAT, type="supergroup", title="Предзаказы", username=None
    )
    message = SimpleNamespace(
        chat=chat, from_user=SimpleNamespace(id=sender_id, is_bot=False)
    )
    return SimpleNamespace(message=message, edited_message=None)



class TelegramPassiveGroupSelfServiceTests(unittest.TestCase):
    def test_registry_approve_and_blocked_shadowing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            registry = PassiveGroupRegistry(
                Path("passive-secretary/groups/test.json"), hermes_home=home
            )
            nonce = registry.begin(
                chat_id=CHAT, owner_id=OWNER, title="Предзаказы", ttl_seconds=300
            )
            self.assertIn(CHAT, registry.blocked_ids(owner_id=OWNER))
            self.assertNotIn(CHAT, registry.approved_ids(owner_id=OWNER))

            result = registry.resolve(
                nonce=nonce, owner_id=OWNER, approve=True
            )
            self.assertIsNotNone(result)
            self.assertEqual(result["chat_id"], CHAT)
            self.assertIn(CHAT, registry.approved_ids(owner_id=OWNER))
            self.assertNotIn(CHAT, registry.blocked_ids(owner_id=OWNER))

    def test_unknown_group_owner_message_opens_single_pending_consent(self):
        async def scenario():
            adapter = make_adapter()
            self.assertFalse(
                await adapter._ingest_passive_group_update_once(update(OWNER))
            )
            self.assertEqual(adapter._passive_group_registry.begin_calls, 1)
            self.assertEqual(len(adapter._bot.sent), 1)
            self.assertIn(
                "Подключить пассивного секретаря",
                adapter._bot.sent[0]["text"],
            )

            # Pending group must not create duplicate consent prompts.
            self.assertFalse(
                await adapter._ingest_passive_group_update_once(update(OWNER))
            )
            self.assertEqual(adapter._passive_group_registry.begin_calls, 1)
            self.assertEqual(len(adapter._bot.sent), 1)

        asyncio.run(scenario())

    def test_unknown_group_non_owner_is_silently_dropped(self):
        async def scenario():
            adapter = make_adapter()
            self.assertFalse(
                await adapter._ingest_passive_group_update_once(
                    update(123456789)
                )
            )
            self.assertEqual(adapter._passive_group_registry.begin_calls, 0)
            self.assertEqual(adapter._bot.sent, [])

        asyncio.run(scenario())

    def test_approval_runs_rights_health_check_and_renders_status(self):
        async def scenario():
            adapter = make_adapter()
            await adapter._ingest_passive_group_update_once(update(OWNER))
            query = FakeQuery()
            await adapter._handle_passive_group_consent_callback(
                SimpleNamespace(callback_query=query), SimpleNamespace()
            )
            self.assertEqual(
                adapter._passive_group_registry.state[CHAT], "approved"
            )
            rendered = query.edits[-1]["text"]
            self.assertIn("Чтение обычных сообщений: доступно", rendered)
            self.assertIn("Passive Secretary: включён", rendered)
            self.assertIn("Исходящие сообщения: выключены", rendered)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
