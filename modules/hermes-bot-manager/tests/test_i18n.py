from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import i18n

SPEC = importlib.util.spec_from_file_location("hermes_bot_manager_i18n_test", ROOT / "manager.py")
manager = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(manager)


def test_locale_canonicalization_and_fallback():
    assert i18n.canonical_locale("es-MX") == "es"
    assert i18n.canonical_locale("pt_BR") == "pt"
    assert i18n.canonical_locale("zh-Hans") == "zh"
    assert i18n.canonical_locale("it-IT") == "en"


def test_language_keyboard_exposes_major_languages():
    markup = i18n.language_keyboard()
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    assert len(buttons) == 10
    assert {b["callback_data"] for b in buttons} == {f"lang:{x}" for x in i18n.SUPPORTED}
def test_remember_locale_persists_and_preference_wins():
    state = {}
    user = {"id": 42, "language_code": "de-DE"}
    assert i18n.remember_locale(user, state) == "de"
    assert state["locales"]["42"] == "de"
    state["locales"]["42"] = "fr"
    assert i18n.remember_locale({"id": 42, "language_code": "es"}, state) == "fr"


def test_localized_main_menu_and_welcome():
    es = manager.main_menu("es")
    assert es["inline_keyboard"][0][0]["text"] == "⚡ Contratar asistente de IA"
    assert "🌐 Idioma" == es["inline_keyboard"][-1][0]["text"]
    assert "Autoaprendizaje" in manager.welcome_text("es")
    assert "自我学习" in manager.welcome_text("zh")


def test_old_state_without_locales_migrates(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text('{"offset": 1, "managed": {}, "imported": {}, "drafts": {}}')
    monkeypatch.setattr(manager, "STATE_FILE", state_file)
    state = manager.load_state()
    assert state["locales"] == {}
    assert state["offset"] == 1
def test_start_autodetects_telegram_language(monkeypatch):
    sent = []
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)
    monkeypatch.setattr(manager, "save_state", lambda _state: None)
    monkeypatch.setattr(manager, "send", lambda chat_id, text, markup=None: sent.append((chat_id, text, markup)))
    state = {"managed": {}, "imported": {}, "drafts": {}, "locales": {}}
    manager.handle_message({
        "chat": {"id": 77, "type": "private"},
        "from": {"id": 77, "language_code": "es-MX"},
        "text": "/start",
    }, state)
    assert state["locales"]["77"] == "es"
    assert "Contrata tu asistente" in sent[-1][1]
    assert sent[-1][2]["inline_keyboard"][0][0]["text"] == "⚡ Contratar asistente de IA"


def test_language_callback_overrides_detected_locale(monkeypatch):
    sent = []
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)
    monkeypatch.setattr(manager, "save_state", lambda _state: None)
    monkeypatch.setattr(manager, "answer_callback", lambda *_: None)
    monkeypatch.setattr(manager, "send", lambda chat_id, text, markup=None: sent.append((chat_id, text, markup)))
    state = {"managed": {}, "imported": {}, "drafts": {}, "locales": {"77": "es"}}
    manager.handle_callback({
        "id": "cb", "data": "lang:fr",
        "from": {"id": 77, "language_code": "es"},
        "message": {"chat": {"id": 77, "type": "private"}},
    }, state)
    assert state["locales"]["77"] == "fr"
    assert "Langue changée" in sent[-1][1]
    assert sent[-1][2]["inline_keyboard"][0][0]["text"] == "⚡ Recruter un assistant IA"

def test_every_supported_locale_renders_critical_hiring_flow():
    for locale in i18n.SUPPORTED:
        assert i18n.t(locale, "menu_hire") != "menu_hire"
        assert i18n.t(locale, "welcome") != "welcome"
        name_step = i18n.t(locale, "hire_name")
        username_step = i18n.t(locale, "hire_username")
        confirm = i18n.t(locale, "hire_confirm", name="Test AI", username="testai_bot")
        assert name_step and username_step and confirm
        assert "Test AI" in confirm
        assert "testai_bot" in confirm

def test_installer_bundles_i18n_module():
    install_source = (ROOT / "install.py").read_text()
    assert "'i18n.py'" in install_source
