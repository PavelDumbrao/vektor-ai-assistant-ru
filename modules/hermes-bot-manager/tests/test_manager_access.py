from __future__ import annotations

import html
import importlib.util
import json
from pathlib import Path
import time

MODULE = Path(__file__).resolve().parents[1] / "manager.py"
spec = importlib.util.spec_from_file_location("hermes_bot_manager", MODULE)
manager = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(manager)


def capture(monkeypatch):
    sent = []
    api_calls = []
    answered_callbacks = []

    monkeypatch.setattr(manager, "save_state", lambda state: None)
    monkeypatch.setattr(
        manager, "send",
        lambda chat_id, text, markup=None: sent.append((chat_id, text, markup)),
    )
    monkeypatch.setattr(
        manager, "answer_callback",
        lambda cb_id, text="": answered_callbacks.append((cb_id, text)),
    )

    def fake_api(method, payload=None, timeout=65):
        api_calls.append((method, payload or {}))
        if method == "getMe":
            return {"username": manager.BOT_USERNAME, "can_manage_bots": True}
        return {"ok": True, "result": True}

    monkeypatch.setattr(manager, "api", fake_api)
    return sent, api_calls, answered_callbacks


def test_unauthorized_start_offers_request_access_button(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    state = {
        "managed": {},
        "imported": {},
        "allowed_users": {},
        "denied_users": {},
        "access_requests": {},
        "users": {},
        "locales": {},
    }
    user_msg = {
        "chat": {"id": 11111, "type": "private"},
        "from": {"id": 11111, "language_code": "ru"},
        "text": "/start",
    }
    manager.handle_message(user_msg, state)

    assert len(sent) == 1
    chat_id, text, markup = sent[0]
    assert chat_id == 11111
    assert "Доступ закрыт" in text
    assert markup is not None
    button = markup["inline_keyboard"][0][0]
    assert button["callback_data"] == "request_access"
    assert "Запросить доступ" in button["text"]


def test_request_access_callback_and_admin_approval(monkeypatch):
    sent, api_calls, answered = capture(monkeypatch)
    state = {
        "managed": {},
        "imported": {},
        "allowed_users": {},
        "denied_users": {},
        "access_requests": {},
        "users": {},
        "locales": {},
    }

    # 1. User with username clicks request_access
    cb_user = {
        "id": "cb_1",
        "from": {"id": 22222, "username": "alice", "first_name": "Alice", "last_name": "Wonderland", "language_code": "ru"},
        "message": {"chat": {"id": 22222, "type": "private"}, "message_id": 101},
        "data": "request_access",
    }
    manager.handle_callback(cb_user, state)

    # State recorded pending request and cached user identity
    assert "22222" in state["access_requests"]
    req = state["access_requests"]["22222"]
    assert req["status"] == "pending"
    assert req["username"] == "alice"
    assert req["first_name"] == "Alice"
    assert req["last_name"] == "Wonderland"
    assert state["users"]["22222"]["username"] == "alice"

    # User received confirmation
    assert any(c == 22222 and "Заявка на доступ отправлена" in t for c, t, _ in sent)

    # Admin (PAVEL_ID) received notification with @alice, full name, and decision buttons
    admin_msgs = [(c, t, m) for c, t, m in sent if c == manager.PAVEL_ID]
    assert len(admin_msgs) == 1
    _, admin_text, admin_markup = admin_msgs[0]
    assert "@alice" in admin_text
    assert "Alice Wonderland" in admin_text
    assert "22222" in admin_text
    assert admin_markup is not None
    buttons = admin_markup["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == "access_allow:22222"
    assert buttons[1]["callback_data"] == "access_deny:22222"

    # 2. Duplicate callback within 24h does NOT send a second message to admin
    sent.clear()
    manager.handle_callback(cb_user, state)
    assert not any(c == manager.PAVEL_ID for c, _, _ in sent)
    assert any("Заявка уже отправлена" in t for _, t in answered)

    # 3. Non-admin cannot approve
    sent.clear()
    cb_fake_admin = {
        "id": "cb_fake",
        "from": {"id": 99999, "username": "intruder"},
        "message": {"chat": {"id": 99999, "type": "private"}, "message_id": 102},
        "data": "access_allow:22222",
    }
    manager.handle_callback(cb_fake_admin, state)
    assert "22222" not in state["allowed_users"]
    assert state["access_requests"]["22222"]["status"] == "pending"
    assert any("Access denied" in t for _, t in answered)

    # 4. Real admin approves
    sent.clear()
    cb_real_admin = {
        "id": "cb_admin",
        "from": {"id": manager.PAVEL_ID, "username": "pavel"},
        "message": {"chat": {"id": manager.PAVEL_ID, "type": "private"}, "message_id": 103},
        "data": "access_allow:22222",
    }
    manager.handle_callback(cb_real_admin, state)
    assert "22222" in state["allowed_users"]
    assert state["allowed_users"]["22222"]["username"] == "alice"
    assert state["allowed_users"]["22222"]["added_by"] == manager.PAVEL_ID
    assert state["access_requests"]["22222"]["status"] == "approved"

    # User received access_granted notification with main menu
    assert any(c == 22222 and "Доступ предоставлен" in t for c, t, _ in sent)

    # Admin message text was edited
    edit_calls = [call for call in api_calls if call[0] == "editMessageText"]
    assert len(edit_calls) >= 1
    assert "Доступ разрешён" in edit_calls[-1][1]["text"]


def test_access_deny_flow(monkeypatch):
    sent, api_calls, _ = capture(monkeypatch)
    state = {
        "managed": {},
        "imported": {},
        "allowed_users": {},
        "denied_users": {},
        "access_requests": {},
        "users": {},
        "locales": {},
    }

    # User submits request
    cb_user = {
        "id": "cb_deny_req",
        "from": {"id": 33333, "username": "mallory", "first_name": "Mallory", "language_code": "ru"},
        "message": {"chat": {"id": 33333, "type": "private"}, "message_id": 201},
        "data": "request_access",
    }
    manager.handle_callback(cb_user, state)
    assert state["access_requests"]["33333"]["status"] == "pending"

    # Admin denies
    sent.clear()
    cb_admin = {
        "id": "cb_deny_action",
        "from": {"id": manager.PAVEL_ID},
        "message": {"chat": {"id": manager.PAVEL_ID, "type": "private"}, "message_id": 202},
        "data": "access_deny:33333",
    }
    manager.handle_callback(cb_admin, state)
    assert state["access_requests"]["33333"]["status"] == "denied"
    assert "33333" not in state["allowed_users"]

    # User received polite rejection
    assert any(c == 33333 and "Заявка на доступ отклонена" in t for c, t, _ in sent)

    # Admin message text was edited
    edit_calls = [call for call in api_calls if call[0] == "editMessageText"]
    assert len(edit_calls) >= 1
    assert "Доступ отклонён" in edit_calls[-1][1]["text"]


def test_user_without_username_flow(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    state = {
        "managed": {},
        "imported": {},
        "allowed_users": {},
        "denied_users": {},
        "access_requests": {},
        "users": {},
        "locales": {},
    }

    cb_user = {
        "id": "cb_no_uname",
        "from": {"id": 44444, "first_name": "Bob", "last_name": "Builder", "language_code": "ru"},
        "message": {"chat": {"id": 44444, "type": "private"}, "message_id": 301},
        "data": "request_access",
    }
    manager.handle_callback(cb_user, state)

    admin_msgs = [(c, t, m) for c, t, m in sent if c == manager.PAVEL_ID]
    assert len(admin_msgs) == 1
    _, text, markup = admin_msgs[0]
    assert "без username" in text
    assert "Bob Builder" in text
    assert "tg://user?id=44444" in text
    assert markup["inline_keyboard"][0][0]["callback_data"] == "access_allow:44444"


def test_user_label_and_user_link():
    state = {
        "users": {
            "100": {"username": "john", "first_name": "John", "last_name": "Doe"},
            "200": {"username": "", "first_name": "Jane", "last_name": "Roe"},
        },
        "managed": {},
        "imported": {},
    }

    # user_link helper
    assert manager.user_link(100) == "tg://user?id=100"

    # user_label with username
    assert manager.user_label(100, state) == "@john (John Doe)"

    # user_label without username
    assert manager.user_label(200, state) == "Jane Roe (без username)"

    # user_label with explicit user dict
    assert manager.user_label(300, state, {"username": "mike", "first_name": "Michael"}) == "@mike (Michael)"
    assert manager.user_label(400, state, {"first_name": "Anna"}) == "Anna (без username)"


def test_tenants_and_new_managed_contain_username(monkeypatch, tmp_path):
    sent, _, _ = capture(monkeypatch)
    state = {
        "managed": {},
        "imported": {},
        "allowed_users": {"55555": {"added_by": manager.PAVEL_ID, "username": "david"}},
        "users": {"55555": {"username": "david", "first_name": "David"}},
        "drafts": {},
        "locales": {},
    }

    monkeypatch.setattr(manager, "stable_release_id", lambda: "hermes-0.21.0-test")
    monkeypatch.setattr(manager, "store_managed_token", lambda *_: tmp_path / "bot.env")
    monkeypatch.setattr(manager, "start_provisioning", lambda *_: "proai-hermes-provisioner@hermes-55555.service")
    monkeypatch.setattr(manager, "api", lambda method, payload=None, timeout=65: "token-abc" if method == "getManagedBotToken" else True)

    # 1. New Managed bot creation notification to admin contains @username
    update = {
        "managed_bot": {
            "user": {"id": 55555, "username": "david", "first_name": "David", "language_code": "ru"},
            "bot": {"id": 95555, "username": "david_bot", "first_name": "David Bot"},
        }
    }
    manager.managed_event(update, state)

    # In managed record, owner_username is stored
    assert state["managed"]["95555"]["owner_username"] == "david"

    admin_msgs = [t for c, t, _ in sent if c == manager.PAVEL_ID]
    assert any("🛠 Новый Managed Hermes" in t and "@david" in t for t in admin_msgs)

    # 2. /tenants command output contains @username
    sent.clear()
    manager.handle_message({
        "chat": {"id": manager.PAVEL_ID, "type": "private"},
        "from": {"id": manager.PAVEL_ID},
        "text": "/tenants",
    }, state)

    tenants_msg = [t for c, t, _ in sent if c == manager.PAVEL_ID]
    assert any("Таблица тенантов" in t and "@david" in t for t in tenants_msg)


def test_offboard_confirm_notice_and_offboard_cancel(monkeypatch, tmp_path):
    sent, _, _ = capture(monkeypatch)
    monkeypatch.setattr(manager, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(manager, "MANAGED_DIR", tmp_path / "state" / "managed")
    monkeypatch.setattr(manager, "INSTANCES_DIR", tmp_path / "state" / "instances")
    monkeypatch.setattr(manager, "PROVISIONING_DIR", tmp_path / "state" / "provisioning")
    monkeypatch.setattr(manager, "OFFBOARDED_DIR", tmp_path / "state" / "offboarded")
    monkeypatch.setattr(manager, "is_admin", lambda uid: uid == manager.PAVEL_ID)
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)

    subp_calls = []
    class SubpResult:
        returncode = 0
    monkeypatch.setattr(manager.subprocess, "run", lambda args, **kw: subp_calls.append(args) or SubpResult())

    now = int(time.time())
    state = {
        "managed": {
            "96666": {
                "bot_id": 96666,
                "owner_user_id": 66666,
                "profile": "h66666",
                "username": "frank_bot",
                "profile_status": "active",
                "owner_username": "frank",
            }
        },
        "imported": {},
        "users": {"66666": {"username": "frank", "first_name": "Frank"}},
        "pending_confirmations": {"offboard:66666": {"expires_at": now + 300}},
        "locales": {"66666": "ru"},
    }

    # 1. Admin confirms offboard
    manager.handle_message({
        "chat": {"id": manager.PAVEL_ID, "type": "private"},
        "from": {"id": manager.PAVEL_ID},
        "text": "/offboard 66666 confirm",
    }, state)

    item = state["managed"]["96666"]
    assert item["profile_status"] == "offboarded"
    assert "offboarded_at" in item
    assert item["purge_after"] == item["offboarded_at"] + 30 * 86400
    assert "purged_at" not in item

    # Client receives offboard notice with +30 days date formatted as DD.MM.YYYY
    client_msgs = [t for c, t, _ in sent if c == 66666]
    expected_date = time.strftime("%d.%m.%Y", time.localtime(item["purge_after"]))
    assert any("хранятся 30 дней" in t and expected_date in t for t in client_msgs)

    # Admin receives message mentioning purge and cancel command
    admin_msgs = [t for c, t, _ in sent if c == manager.PAVEL_ID]
    assert any("Данные будут удалены автоматически" in t and expected_date in t and "/offboard-cancel 66666" in t for t in admin_msgs)

    # 2. Admin calls /offboard-cancel 66666
    sent.clear()
    subp_calls.clear()
    manager.handle_message({
        "chat": {"id": manager.PAVEL_ID, "type": "private"},
        "from": {"id": manager.PAVEL_ID},
        "text": "/offboard-cancel 66666",
    }, state)

    assert item["profile_status"] == "active"
    assert "purge_after" not in item
    assert "offboarded_at" not in item

    # Verify systemctl and lanectl calls
    units_enabled = [a[-1] for a in subp_calls if a[0] == "/usr/bin/systemctl" and a[1] == "enable"]
    assert "h66666-hermes.service" in units_enabled
    assert "h66666-hermes-passive-secretary-retention.timer" in units_enabled
    assert any(a[:3] == ["/usr/local/sbin/lanectl", "add", "--client"] and a[3] == "h66666" for a in subp_calls)

    # Client receives offboard_cancelled
    client_msgs = [t for c, t, _ in sent if c == 66666]
    assert any("снова активен" in t for t in client_msgs)


def test_purge_due_stub_finds_expired_records():
    now = int(time.time())
    state = {
        "managed": {
            "due_1": {
                "bot_id": 1001,
                "owner_user_id": 501,
                "profile_status": "offboarded",
                "purge_after": now - 100,  # Expired
            },
            "not_due_yet": {
                "bot_id": 1002,
                "owner_user_id": 502,
                "profile_status": "offboarded",
                "purge_after": now + 1000,  # In the future
            },
            "already_purged": {
                "bot_id": 1003,
                "owner_user_id": 503,
                "profile_status": "offboarded",
                "purge_after": now - 100,
                "purged_at": now - 50,  # Already purged
            },
        },
        "imported": {},
    }

    due = manager.purge_due(state)
    assert len(due) == 1
    assert due[0]["bot_id"] == 1001


def test_support_and_pay_stubs(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)
    state = {
        "managed": {},
        "imported": {},
        "users": {},
        "locales": {"77777": "ru"},
    }

    # Test callback support
    cb_support = {
        "id": "cb_sup",
        "from": {"id": 77777, "language_code": "ru"},
        "message": {"chat": {"id": 77777, "type": "private"}, "message_id": 401},
        "data": "support",
    }
    manager.handle_callback(cb_support, state)
    assert any("Служба поддержки" in t for c, t, _ in sent if c == 77777)

    # Test callback pay
    sent.clear()
    cb_pay = {
        "id": "cb_pay",
        "from": {"id": 77777, "language_code": "ru"},
        "message": {"chat": {"id": 77777, "type": "private"}, "message_id": 402},
        "data": "pay",
    }
    manager.handle_callback(cb_pay, state)
    assert any("Оплата и подписка" in t for c, t, _ in sent if c == 77777)


def test_support_message_does_not_intercept_normal_messages(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)
    state = {
        "managed": {},
        "imported": {},
        "support": {"77777": {"active": True}},
        "locales": {"77777": "ru"},
    }

    msg = {
        "chat": {"id": 77777, "type": "private"},
        "from": {"id": 77777, "language_code": "ru"},
        "text": "/help",
    }
    manager.handle_message(msg, state)

    # /help was not swallowed by support_message, standard help was sent
    assert any("Помощь" in t for c, t, _ in sent if c == 77777)


def test_admin_requests_command(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    state = {
        "access_requests": {
            "88888": {
                "status": "pending",
                "username": "eva",
                "first_name": "Eva",
                "last_name": "Green",
                "requested_at": 1700000000,
            }
        },
        "users": {"88888": {"username": "eva", "first_name": "Eva", "last_name": "Green"}},
        "managed": {},
        "imported": {},
        "locales": {},
    }

    # 1. Non-admin is denied
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)
    monkeypatch.setattr(manager, "is_admin", lambda uid: False)
    manager.handle_message({
        "chat": {"id": 88888, "type": "private"},
        "from": {"id": 88888, "language_code": "ru"},
        "text": "/requests",
    }, state)
    assert any("Доступ закрыт" in t or "Access denied" in t for c, t, _ in sent if c == 88888)

    # 2. Admin receives list of pending requests
    sent.clear()
    monkeypatch.setattr(manager, "is_admin", lambda uid: uid == manager.PAVEL_ID)
    manager.handle_message({
        "chat": {"id": manager.PAVEL_ID, "type": "private"},
        "from": {"id": manager.PAVEL_ID},
        "text": "/requests",
    }, state)

    admin_msgs = [t for c, t, _ in sent if c == manager.PAVEL_ID]
    assert any("Заявки на доступ" in t and "@eva" in t and "88888" in t for t in admin_msgs)


def test_user_label_escapes_html_in_names():
    state = {"users": {"5": {"username": "", "first_name": "<b>Боб</b>", "last_name": "&Co"}}}
    label = manager.user_label(5, state)
    assert "<b>" not in label and "&lt;b&gt;" in label and "&amp;Co" in label


def test_offboard_cancel_restores_lane_with_same_port(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(manager, "send", lambda chat_id, text, markup=None: sent.append((chat_id, text, markup)))
    monkeypatch.setattr(manager, "save_state", lambda state: None)
    monkeypatch.setattr(manager, "is_admin", lambda uid: uid == manager.PAVEL_ID)
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)
    calls = []

    class R:
        returncode = 0

    monkeypatch.setattr(manager.subprocess, "run", lambda args, **kw: calls.append(args) or R())
    state = {"managed": {"1": {"owner_user_id": 50009, "profile": "h50009", "username": "b", "profile_status": "offboarded",
                               "offboarded_at": 1, "purge_after": 2, "lane": {"port": 21042, "limits": {"elevenlabs_chars_month": 15000, "rpm": 60}}}},
             "imported": {}, "pending_confirmations": {}}
    manager.handle_message({"chat": {"id": manager.PAVEL_ID, "type": "private"}, "from": {"id": manager.PAVEL_ID},
                            "text": "/offboard-cancel 50009"}, state)
    lane_add = next(c for c in calls if c[:2] == ["/usr/local/sbin/lanectl", "add"])
    assert "--port" in lane_add and "21042" in lane_add and "--rpm" in lane_add and "60" in lane_add
    assert any(c[:2] == ["/usr/local/sbin/lanectl", "apply"] for c in calls)
    assert state["managed"]["1"]["profile_status"] == "active" and "lane" not in state["managed"]["1"]
