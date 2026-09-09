from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MODULE = Path(__file__).resolve().parents[1] / "manager.py"
spec = importlib.util.spec_from_file_location("hermes_bot_manager", MODULE)
manager = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(manager)


def capture(monkeypatch):
    sent = []
    monkeypatch.setattr(manager, "save_state", lambda state: None)
    monkeypatch.setattr(
        manager, "send",
        lambda chat_id, text, markup=None: sent.append((chat_id, text, markup)),
    )
    return sent


def test_menu_sells_hiring_not_bot_creation():
    button = manager.main_menu()["inline_keyboard"][0][0]
    assert button["text"] == "⚡ Нанять AI-ассистента"
    text = manager.welcome_text()
    assert "Найми своего персонального AI-ассистента" in text
    assert "Самообучающийся" in text and "подстраивается под тебя" in text

def test_username_rules_explain_exact_problem():
    username, problem = manager.username_problem("SalavatAI")
    assert username == "salavatai"
    assert "заканчиваться" in problem and "bot" in problem

    _, problem = manager.username_problem("Салават_bot")
    assert "латинские" in problem

    username, problem = manager.username_problem("@SalavatAI_bot")
    assert username == "salavatai_bot"
    assert problem is None


def test_name_step_accepts_normal_name_and_asks_username(monkeypatch):
    sent = capture(monkeypatch)
    state = {"drafts": {"503899482": {"step": "name"}}}
    assert manager.handle_hire_text(503899482, 503899482, "Салават AI", state)
    draft = state["drafts"]["503899482"]
    assert draft["name"] == "Салават AI"
    assert draft["step"] == "username"
    assert "обязательно должен заканчиваться на bot" in sent[-1][1]

def test_bad_username_does_not_advance(monkeypatch):
    sent = capture(monkeypatch)
    state = {"drafts": {"503899482": {"step": "username", "name": "Салават AI"}}}
    assert manager.handle_hire_text(503899482, 503899482, "SalavatAI", state)
    assert state["drafts"]["503899482"]["step"] == "username"
    assert "Нужно немного поправить username" in sent[-1][1]
    assert "заканчиваться" in sent[-1][1]


def test_valid_username_builds_prefilled_final_confirmation(monkeypatch):
    sent = capture(monkeypatch)
    state = {"drafts": {"503899482": {"step": "username", "name": "Салават AI"}}}
    assert manager.handle_hire_text(503899482, 503899482, "@SalavatAI_bot", state)
    draft = state["drafts"]["503899482"]
    assert draft["step"] == "confirm"
    assert draft["username"] == "salavatai_bot"
    text, markup = sent[-1][1], sent[-1][2]
    assert "Бот создаётся <b>в твоём Telegram-аккаунте</b>" in text
    button = markup["inline_keyboard"][0][0]
    assert button["text"] == "✅ Подтвердить найм"
    parsed = urlparse(button["url"])
    assert parsed.path == "/newbot/ProAIHermesBot/salavatai_bot"
    assert parse_qs(parsed.query)["name"] == ["Салават AI"]


def test_username_is_canonicalized_to_lowercase():
    username, problem = manager.username_problem("@MarkusHelperBot")
    assert username == "markushelperbot"
    assert problem is None


def test_new_managed_bot_enters_provisioning(monkeypatch, tmp_path):
    sent = capture(monkeypatch)
    state = {"managed": {}, "drafts": {"503899482": {"step": "confirm"}}}
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)
    monkeypatch.setattr(manager, "find_profile_for_owner", lambda *_: None)
    monkeypatch.setattr(manager, "stable_release_id", lambda: "hermes-0.21.0-test")
    monkeypatch.setattr(manager, "store_managed_token", lambda *_: tmp_path / "9000000001.env")
    monkeypatch.setattr(manager, "start_provisioning", lambda *_: "proai-hermes-provisioner@hermes-503899482.service")
    monkeypatch.setattr(
        manager, "api",
        lambda method, payload=None, timeout=65: (
            "test-token-not-secret"
            if method == "getManagedBotToken" else True
        ),
    )
    update = {
        "managed_bot": {
            "user": {"id": 503899482},
            "bot": {"id": 9000000001, "username": "SalavatAI_bot", "first_name": "Salavat AI"},
        }
    }
    manager.managed_event(update, state)
    item = state["managed"]["9000000001"]
    assert item["profile_status"] == "provisioning"
    assert item["profile"] == "h503899482"
    assert item["instance_id"] == "hermes-503899482"
    assert item["release_id"] == "hermes-0.21.0-test"
    assert item["provision_job"] == "proai-hermes-provisioner@hermes-503899482.service"
    assert any("автоматически разворачивает" in text for _chat, text, _markup in sent)


def test_reconcile_promotes_active_receipt(monkeypatch, tmp_path):
    sent = capture(monkeypatch)
    monkeypatch.setattr(manager, "PROVISIONING_DIR", tmp_path)
    receipt = tmp_path / "hermes-503899482.json"
    receipt.write_text(json.dumps({
        "state": "active",
        "health": {"service": True, "telegram": True},
    }))
    state = {"managed": {"9000000001": {
        "owner_user_id": 503899482,
        "profile": "h503899482",
        "profile_status": "provisioning",
        "instance_id": "hermes-503899482",
        "username": "salavatai_bot",
    }}}
    manager.reconcile_provisioning(state)
    item = state["managed"]["9000000001"]
    assert item["profile_status"] == "active"
    assert item["health"]["telegram"] is True
    assert item["notified_state"] == "active"
    assert any("готов к работе" in text for _chat, text, _markup in sent)


def test_start_provisioning_uses_bounded_systemd_unit(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "INSTANCES_DIR", tmp_path / "instances")
    monkeypatch.setattr(manager, "PROVISIONING_DIR", tmp_path / "provisioning")
    monkeypatch.setattr(manager, "JOBS_DIR", tmp_path / "jobs")
    calls = []

    class Result:
        returncode = 0

    monkeypatch.setattr(manager.subprocess, "run", lambda args, **kwargs: calls.append(args) or Result())
    instance = manager.HermesInstance(
        instance_id="hermes-503899482",
        owner_linux="h503899482",
        owner_telegram_id=503899482,
        bot_id=9000000001,
        bot_username="salavatai_bot",
        bot_name="Salavat AI",
        release_id="hermes-0.21.0-test",
    ).validate()
    unit = manager.start_provisioning(instance)
    assert unit == "proai-hermes-provisioner@hermes-503899482.service"
    assert calls == [[
        "/usr/bin/systemctl", "start", "--no-block",
        "proai-hermes-provisioner@hermes-503899482.service",
    ]]
