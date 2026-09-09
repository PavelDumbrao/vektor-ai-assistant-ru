from __future__ import annotations

import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MODULE = Path(__file__).resolve().parents[1] / "manager.py"
spec = importlib.util.spec_from_file_location("hermes_bot_manager", MODULE)
manager = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(manager)


def test_create_button_uses_managed_bot_deeplink():
    markup = manager.create_keyboard({"id": 503899482, "first_name": "Salavat"})
    button = markup["inline_keyboard"][0][0]
    assert "request_managed_bot" not in button
    assert button["text"] == "⚡ Создать моего Hermes"
    parsed = urlparse(button["url"])
    assert parsed.netloc == "t.me"
    assert parsed.path == "/newbot/ProAIHermesBot/Hermes503899482Bot"
    assert parse_qs(parsed.query)["name"] == ["Hermes | Salavat"]


def test_username_typed_in_chat_gets_specific_recovery(monkeypatch):
    sent = []
    monkeypatch.setattr(manager, "is_authorized_user", lambda user_id, state: True)
    monkeypatch.setattr(
        manager,
        "send",
        lambda chat_id, text, markup=None: sent.append((chat_id, text, markup)),
    )
    msg = {
        "chat": {"id": 503899482, "type": "private"},
        "from": {"id": 503899482, "first_name": "Salavat"},
        "text": "SalavatAi_bot",
    }
    manager.handle_message(msg, {"managed": {}})
    assert len(sent) == 1
    assert "обычное сообщение" in sent[0][1]
    assert sent[0][2]["inline_keyboard"][0][0]["url"].startswith(
        "https://t.me/newbot/ProAIHermesBot/"
    )
