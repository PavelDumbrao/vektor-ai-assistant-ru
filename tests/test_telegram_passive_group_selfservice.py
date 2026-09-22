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

    def info(self, *args, **kwargs):
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
        "_auto_approve_passive_group",
        "_prompt_passive_group_consent",
        "_ingest_passive_group_update_once",
        "_handle_passive_group_membership",
        "_handle_passive_group_consent_callback",
    }
    blocks = []
    found = set()
    for node in adapter.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in wanted:
            continue
        starts = [node.lineno] + [item.lineno for item in node.decorator_list]
        start = min(starts)
        blocks.append("".join(lines[start - 1 : node.end_lineno]).rstrip() + "\n\n")
        found.add(node.name)
    if found != wanted:
        raise AssertionError(f"missing adapter methods: {sorted(wanted - found)}")

    source = "from __future__ import annotations\n\nclass Adapter:\n" + "".join(blocks)
    namespace = {
        "_html": _html,
        "InlineKeyboardButton": FakeInlineKeyboardButton,
        "InlineKeyboardMarkup": FakeInlineKeyboardMarkup,
        "ParseMode": SimpleNamespace(HTML="HTML"),
        "logger": FakeLogger(),
        "Update": object,
        "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object),
        "Any": object,
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
TECH = 100002
OTHER = 100003
CHAT = -100999111222


class FakeBot:
    def __init__(self, health_ok: bool = True) -> None:
        self.sent: list[dict] = []
        self.left: list[dict] = []
        self.health_ok = health_ok

    async def get_me(self):
        return SimpleNamespace(
            id=200002,
            can_read_all_group_messages=self.health_ok,
        )

    async def get_chat_member(self, *, chat_id: int, user_id: int):
        return SimpleNamespace(
            status="member" if self.health_ok else "left",
            is_member=self.health_ok,
        )

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=1)

    async def leave_chat(self, **kwargs):
        self.left.append(kwargs)
        raise AssertionError("passive-group onboarding must never call leave_chat")


class FakeQuery:
    def __init__(self, nonce: str, decision: str = "n") -> None:
        self.data = f"pg:{decision}:{nonce}"
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


def make_adapter(tmp: str, *, bot: FakeBot | None = None):
    adapter = Adapter()
    adapter.name = "Telegram"
    adapter._bot = bot or FakeBot()
    registry = PassiveGroupRegistry(
        Path("groups.json"),
        hermes_home=Path(tmp),
    )
    adapter._passive_group_registry = registry
    adapter._get_passive_group_registry = lambda: registry
    adapter._group_passive_enabled = lambda: True
    adapter._group_passive_owner_id = lambda: OWNER
    adapter._group_passive_trusted_inviter_ids = lambda: {TECH}
    adapter._group_passive_chat_ids = (
        lambda: registry.approved_ids(owner_id=OWNER)
        - registry.blocked_ids(owner_id=OWNER)
    )
    return adapter, registry


def membership_update(actor_id: int, old: str = "left", new: str = "member"):
    chat = SimpleNamespace(
        id=CHAT,
        type="supergroup",
        title="Test Group",
        username=None,
    )
    membership = SimpleNamespace(
        chat=chat,
        from_user=SimpleNamespace(id=actor_id),
        old_chat_member=SimpleNamespace(
            status=old,
            is_member=(old == "member"),
        ),
        new_chat_member=SimpleNamespace(
            status=new,
            is_member=(new == "member"),
        ),
    )
    return SimpleNamespace(
        my_chat_member=membership,
        effective_chat=chat,
    )


def group_message(sender_id: int):
    chat = SimpleNamespace(
        id=CHAT,
        type="supergroup",
        title="Test Group",
        username=None,
    )
    message = SimpleNamespace(
        chat=chat,
        from_user=SimpleNamespace(
            id=sender_id,
            is_bot=False,
        ),
    )
    return SimpleNamespace(
        message=message,
        edited_message=None,
    )


class TelegramPassiveGroupSelfServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_registry_pending_and_denied_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = PassiveGroupRegistry(
                Path("groups.json"),
                hermes_home=Path(tmp),
            )
            nonce = registry.begin(
                chat_id=CHAT,
                owner_id=OWNER,
                title="Test Group",
            )
            self.assertIn(CHAT, registry.pending_ids(owner_id=OWNER))
            self.assertIn(CHAT, registry.blocked_ids(owner_id=OWNER))

            result = registry.resolve(
                nonce=nonce,
                owner_id=OWNER,
                approve=False,
            )
            self.assertIsNotNone(result)
            self.assertNotIn(CHAT, registry.pending_ids(owner_id=OWNER))
            self.assertIn(CHAT, registry.blocked_ids(owner_id=OWNER))

            reopened = registry.begin(
                chat_id=CHAT,
                owner_id=OWNER,
                title="Test Group",
            )
            self.assertTrue(reopened)
            self.assertIn(CHAT, registry.pending_ids(owner_id=OWNER))

    async def test_owner_add_is_silent_auto_approve(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, registry = make_adapter(tmp)
            await adapter._handle_passive_group_membership(
                membership_update(OWNER),
                SimpleNamespace(),
            )
            self.assertIn(CHAT, registry.approved_ids(owner_id=OWNER))
            self.assertNotIn(CHAT, registry.blocked_ids(owner_id=OWNER))
            self.assertEqual(adapter._bot.sent, [])
            self.assertEqual(adapter._bot.left, [])

    async def test_unknown_inviter_stays_without_capture_or_leave(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, registry = make_adapter(tmp)
            await adapter._handle_passive_group_membership(
                membership_update(OTHER),
                SimpleNamespace(),
            )
            self.assertNotIn(CHAT, registry.approved_ids(owner_id=OWNER))
            self.assertIn(CHAT, registry.blocked_ids(owner_id=OWNER))
            self.assertEqual(adapter._bot.sent, [])
            self.assertEqual(adapter._bot.left, [])

    async def test_denied_group_owner_message_reopens_consent(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, registry = make_adapter(tmp)
            registry.revoke(chat_id=CHAT, owner_id=OWNER)

            result = await adapter._ingest_passive_group_update_once(
                group_message(OWNER)
            )
            self.assertFalse(result)
            self.assertIn(CHAT, registry.pending_ids(owner_id=OWNER))
            self.assertEqual(len(adapter._bot.sent), 1)
            self.assertEqual(adapter._bot.left, [])

    async def test_owner_denial_disables_capture_but_keeps_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, registry = make_adapter(tmp)
            nonce = registry.begin(
                chat_id=CHAT,
                owner_id=OWNER,
                title="Test Group",
            )
            query = FakeQuery(nonce, decision="n")
            await adapter._handle_passive_group_consent_callback(
                SimpleNamespace(callback_query=query),
                SimpleNamespace(),
            )
            self.assertNotIn(CHAT, registry.approved_ids(owner_id=OWNER))
            self.assertIn(CHAT, registry.blocked_ids(owner_id=OWNER))
            self.assertEqual(adapter._bot.left, [])
            self.assertIn("останется в группе", query.edits[-1]["text"])

    async def test_owner_add_bad_rights_fails_closed_without_leave(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = FakeBot(health_ok=False)
            adapter, registry = make_adapter(tmp, bot=bot)
            await adapter._handle_passive_group_membership(
                membership_update(OWNER),
                SimpleNamespace(),
            )
            self.assertNotIn(CHAT, registry.approved_ids(owner_id=OWNER))
            self.assertIn(CHAT, registry.blocked_ids(owner_id=OWNER))
            self.assertEqual(bot.left, [])


if __name__ == "__main__":
    unittest.main()
