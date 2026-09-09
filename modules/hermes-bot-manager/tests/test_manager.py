from __future__ import annotations

import importlib.util
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
    assert username == "SalavatAI"
    assert "заканчиваться" in problem and "bot" in problem

    _, problem = manager.username_problem("Салават_bot")
    assert "латинские" in problem

    username, problem = manager.username_problem("@SalavatAI_bot")
    assert username == "SalavatAI_bot"
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
    assert draft["username"] == "SalavatAI_bot"
    text, markup = sent[-1][1], sent[-1][2]
    assert "Бот создаётся <b>в твоём Telegram-аккаунте</b>" in text
    button = markup["inline_keyboard"][0][0]
    assert button["text"] == "✅ Подтвердить найм"
    parsed = urlparse(button["url"])
    assert parsed.path == "/newbot/ProAIHermesBot/SalavatAI_bot"
    assert parse_qs(parsed.query)["name"] == ["Салават AI"]
