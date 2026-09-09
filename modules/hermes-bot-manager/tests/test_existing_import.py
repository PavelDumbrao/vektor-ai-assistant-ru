from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "manager.py"
spec = importlib.util.spec_from_file_location("forge_manager_import", MODULE)
manager = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(manager)


def capture(monkeypatch):
    sent = []
    monkeypatch.setattr(manager, "send", lambda chat_id, text, markup=None: sent.append((chat_id, text, markup)))
    return sent


def test_import_existing_profile_never_copies_token(monkeypatch):
    state = {"managed": {}, "imported": {}, "drafts": {}}
    monkeypatch.setattr(manager, "_profile_owner_telegram_id", lambda owner: 450206471)
    monkeypatch.setattr(manager, "_profile_bot_identity", lambda owner: {
        "bot_id": 8651847038, "username": "vektor_assist_bot", "name": "Вектор",
    })
    monkeypatch.setattr(manager, "_imported_service_active", lambda item: True)
    monkeypatch.setattr(manager, "save_state", lambda state: None)
    item = manager.import_existing_profile("pavel", state)
    assert item["profile"] == "pavel"
    assert item["profile_status"] == "active"
    assert item["username"] == "vektor_assist_bot"
    assert "token" not in item


def test_show_my_includes_imported_assistant(monkeypatch):
    sent = capture(monkeypatch)
    monkeypatch.setattr(manager, "_imported_service_active", lambda item: True)
    state = {"managed": {}, "imported": {"8651847038": {
        "owner_user_id": 450206471, "profile": "pavel", "bot_id": 8651847038,
        "username": "vektor_assist_bot", "name": "Вектор",
    }}}
    manager.show_my(450206471, 450206471, state)
    text = sent[-1][1]
    assert "@vektor_assist_bot" in text
    assert "работает" in text
    assert "подключён ранее" in text


def test_show_my_deduplicates_managed_and_imported(monkeypatch):
    sent = capture(monkeypatch)
    monkeypatch.setattr(manager, "_imported_service_active", lambda item: True)
    item = {"owner_user_id": 450206471, "profile": "pavel", "bot_id": 8651847038,
            "username": "vektor_assist_bot", "name": "Вектор"}
    state = {"managed": {"8651847038": {**item, "profile_status": "active"}},
             "imported": {"8651847038": item}}
    manager.show_my(450206471, 450206471, state)
    assert sent[-1][1].count("@vektor_assist_bot") == 1
