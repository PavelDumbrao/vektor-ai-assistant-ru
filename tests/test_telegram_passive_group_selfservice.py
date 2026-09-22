from __future__ import annotations

import ast
import html as _html
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "modules" / "passive-secretary" / "hermes-core-patch"
TELEGRAM = PATCH / "plugins" / "platforms" / "telegram"
if str(PATCH) not in sys.path:
    sys.path.insert(0, str(PATCH))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


updates_mod = load_module(
    "frozen_passive_group_updates_selfservice_test",
    TELEGRAM / "passive_updates.py",
)
PassiveGroupRegistry = updates_mod.PassiveGroupRegistry


class FakeInlineKeyboardButton:
    def __init__(self, text: str, callback_data: str):
        self.text = text
        self.callback_data = callback_data


class FakeInlineKeyboardMarkup:
    def __init__(self, rows):
        self.inline_keyboard = rows


class FakeLogger:
    def warning(self, *args, **kwargs):
        return None


def load_adapter_methods():
    path = TELEGRAM / "adapter.py"
    text = path.read_text()
    lines = text.splitlines(True)
    tree = ast.parse(text)
    adapter = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TelegramAdapter"
    )
    wanted = {
        "_chat_member_is_present",
        "_group_consent_label",
        "_passive_group_health",
        "_prompt_passive_group_consent",
        "_ingest_passive_group_update_once",
        "_handle_passive_group_consent_callback",
    }
    blocks = []
    for node in adapter.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in wanted:
            continue
        starts = [node.lineno] + [item.lineno for item in node.decorator_list]
        start = min(starts)
        blocks.append("".join(lines[start - 1 : node.end_lineno]).rstrip() + "\n\n")
    found = {
        node.name
        for node in adapter.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in wanted
    }
    if found != wanted:
        raise AssertionError(f"missing adapter methods: {sorted(wanted - found)}")

    source = "from __future__ import annotations\n\nclass Adapter:\n" + "".join(blocks)
    namespace = {
        "_html": _html,
        "InlineKeyboardButton": FakeInlineKeyboardButton,
        "InlineKeyboardMarkup": FakeInlineKeyboardMarkup,
        "ParseMode": SimpleNamespace(HTML="HTML"),
        "logger": FakeLogger(),
        "build_group_passive_update_dto": lambda *args, **kwargs: {
            "kind": "group_message"
        },
    }
    exec(compile(source, str(path), "exec"), namespace)
    cls = namespace["Adapter"]

    def ordinary_group_message(update):
        message = getattr(update, "message", None)
        if message is None:
            message = getattr(update, "edited_message", None)
        return message

    cls._ordinary_group_message = staticmethod(ordinary_group_message)
    return cls


Adapter = load_adapter_methods()

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
        return {"chat_id": CHAT, "title": "Preorders"}

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
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=OWNER, type="private")
        )
        self.answers: list[dict] = []
        self.edits: list[dict] = []

    async def answer(self, **kwargs):
        self.answers.append(kwargs)

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)


def make_adapter():
    adapter = Adapter()
    adapter.name = "Telegram"
    adapter._bot = FakeBot()
    adapter._passive_group_registry = FakeRegistry()
    adapter._group_passive_owner_id = lambda: OWNER
    adapter._get_passive_group_registry = lambda: adapter._passive_group_registry
    adapter._group_passive_chat_ids = (
        lambda: adapter._passive_group_registry.approved_ids(owner_id=OWNER)
    )
    return adapter


def update(sender_id: int):
    chat = SimpleNamespace(
        id=CHAT,
        type="supergroup",
        title="Preorders",
        username=None,
    )
    message = SimpleNamespace(
        chat=chat,
        from_user=SimpleNamespace(id=sender_id, is_bot=False),
    )
    return SimpleNamespace(message=message, edited_message=None)


class TelegramPassiveGroupSelfServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_registry_approve_and_blocked_shadowing(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = PassiveGroupRegistry(
                Path("passive-secretary/groups/test.json"),
                hermes_home=Path(tmp),
            )
            nonce = registry.begin(
                chat_id=CHAT,
                owner_id=OWNER,
                title="Preorders",
                ttl_seconds=300,
            )
            self.assertIn(CHAT, registry.blocked_ids(owner_id=OWNER))
            self.assertNotIn(CHAT, registry.approved_ids(owner_id=OWNER))
            result = registry.resolve(
                nonce=nonce,
                owner_id=OWNER,
                approve=True,
            )
            self.assertIsNotNone(result)
            self.assertEqual(result["chat_id"], CHAT)
            self.assertIn(CHAT, registry.approved_ids(owner_id=OWNER))
            self.assertNotIn(CHAT, registry.blocked_ids(owner_id=OWNER))

    async def test_unknown_group_owner_message_opens_single_pending_consent(self):
        adapter = make_adapter()
        self.assertFalse(
            await adapter._ingest_passive_group_update_once(update(OWNER))
        )
        self.assertEqual(adapter._passive_group_registry.begin_calls, 1)
        self.assertEqual(len(adapter._bot.sent), 1)

        self.assertFalse(
            await adapter._ingest_passive_group_update_once(update(OWNER))
        )
        self.assertEqual(adapter._passive_group_registry.begin_calls, 1)
        self.assertEqual(len(adapter._bot.sent), 1)

    async def test_unknown_group_non_owner_is_silently_dropped(self):
        adapter = make_adapter()
        self.assertFalse(
            await adapter._ingest_passive_group_update_once(update(123456789))
        )
        self.assertEqual(adapter._passive_group_registry.begin_calls, 0)
        self.assertEqual(adapter._bot.sent, [])

    async def test_approval_runs_rights_health_check_and_renders_status(self):
        adapter = make_adapter()
        await adapter._ingest_passive_group_update_once(update(OWNER))
        query = FakeQuery()
        await adapter._handle_passive_group_consent_callback(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(),
        )
        self.assertEqual(
            adapter._passive_group_registry.state[CHAT],
            "approved",
        )
        rendered = query.edits[-1]["text"]
        self.assertIn("Passive Secretary", rendered)
        self.assertIn("Исходящие сообщения", rendered)


if __name__ == "__main__":
    unittest.main()
