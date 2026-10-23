from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from unittest.mock import MagicMock

MODULE = Path(__file__).resolve().parents[1] / "manager.py"
spec = importlib.util.spec_from_file_location("hermes_bot_manager", MODULE)
manager = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(manager)


def capture(monkeypatch):
    sent = []
    answered_callbacks = []
    api_calls = []

    monkeypatch.setattr(manager, "save_state", lambda state: None)
    monkeypatch.setattr(
        manager, "send",
        lambda chat_id, text, markup=None: sent.append((chat_id, text, markup)),
    )
    monkeypatch.setattr(
        manager, "answer_callback",
        lambda cb_id, text="": answered_callbacks.append((cb_id, text)),
    )
    monkeypatch.setattr(
        manager, "api",
        lambda method, payload=None, timeout=65: api_calls.append((method, payload or {})),
    )
    return sent, answered_callbacks, api_calls


# ============================================================================
# 1. Support integration tests
# ============================================================================

def test_support_button_calls_support_start(monkeypatch):
    sent, answered, _ = capture(monkeypatch)
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)

    start_calls = []
    monkeypatch.setattr(
        manager.support, "start",
        lambda chat_id, user_id, state, send, locale="ru", user_label="": start_calls.append(
            (chat_id, user_id, locale, user_label)
        ),
    )

    state = {
        "managed": {},
        "imported": {},
        "users": {"10001": {"username": "alice", "first_name": "Alice"}},
        "locales": {"10001": "ru"},
    }

    cb_support = {
        "id": "cb_1",
        "from": {"id": 10001, "language_code": "ru"},
        "message": {"chat": {"id": 10001, "type": "private"}, "message_id": 101},
        "data": "support",
    }
    manager.handle_callback(cb_support, state)

    assert len(start_calls) == 1
    assert start_calls[0][0] == 10001
    assert start_calls[0][1] == 10001
    assert start_calls[0][2] == "ru"


def test_support_message_intercepted_and_not_in_normal_handling(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)

    handled_calls = []

    def mock_handle_message(chat_id, user_id, text, state, send, settings=None):
        handled_calls.append((chat_id, user_id, text))
        return True

    monkeypatch.setattr(manager.support, "handle_message", mock_handle_message)

    state = {
        "managed": {},
        "imported": {},
        "users": {"10001": {"username": "alice"}},
        "locales": {"10001": "ru"},
        "support": {"10001": {"active": True, "turns": []}},
    }

    # Normal text message in support mode
    msg = {
        "chat": {"id": 10001, "type": "private"},
        "from": {"id": 10001, "language_code": "ru"},
        "text": "Как настроить ассистента?",
    }
    manager.handle_message(msg, state)

    # Handled by support module
    assert len(handled_calls) == 1
    assert handled_calls[0] == (10001, 10001, "Как настроить ассистента?")
    # Intercepted: regular hire prompt was NOT sent
    assert not any("Нажми «Нанять AI-ассистента»" in t for _, t, _ in sent)


def test_support_close_returns_to_normal_mode(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)

    state = {
        "managed": {},
        "imported": {},
        "users": {"10001": {"username": "alice"}},
        "locales": {"10001": "ru"},
        "support": {"10001": {"active": True, "turns": [], "locale": "ru"}},
    }

    # User clicks support_close
    cb_close = {
        "id": "cb_close",
        "from": {"id": 10001, "language_code": "ru"},
        "message": {"chat": {"id": 10001, "type": "private"}, "message_id": 102},
        "data": "support_close",
    }
    manager.handle_callback(cb_close, state)

    assert state["support"]["10001"]["active"] is False
    assert any("Сессия поддержки завершена" in t for _, t, _ in sent)

    # Next message is in normal mode, not intercepted
    sent.clear()
    msg = {
        "chat": {"id": 10001, "type": "private"},
        "from": {"id": 10001, "language_code": "ru"},
        "text": "/help",
    }
    manager.handle_message(msg, state)
    assert any("Помощь" in t for _, t, _ in sent)


def test_support_admin_reply_delivers_text(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)
    monkeypatch.setattr(manager, "is_admin", lambda uid: uid == manager.PAVEL_ID)

    state = {
        "managed": {},
        "imported": {},
        "users": {"10001": {"username": "alice"}},
        "locales": {str(manager.PAVEL_ID): "ru", "10001": "ru"},
        "support": {"10001": {"active": True, "chat_id": 10001, "locale": "ru", "turns": []}},
    }

    # Admin issues /reply
    msg = {
        "chat": {"id": manager.PAVEL_ID, "type": "private"},
        "from": {"id": manager.PAVEL_ID, "language_code": "ru"},
        "text": "/reply 10001 Мы всё проверили и исправили!",
    }
    manager.handle_message(msg, state)

    # User received message
    user_msgs = [t for c, t, _ in sent if c == 10001]
    assert len(user_msgs) == 1
    assert "Ответ поддержки" in user_msgs[0]
    assert "Мы всё проверили и исправили!" in user_msgs[0]

    # Admin received confirmation
    admin_msgs = [t for c, t, _ in sent if c == manager.PAVEL_ID]
    assert len(admin_msgs) == 1
    assert "Отправлено @alice" in admin_msgs[0]


# ============================================================================
# 2. Billing integration tests
# ============================================================================

def test_open_pay_without_settings_shows_pay_soon(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    state = {
        "managed": {},
        "imported": {},
        "locales": {"10001": "ru"},
    }

    # Case A: BILLING is None
    monkeypatch.setattr(manager, "BILLING", None)
    manager.open_pay(10001, 10001, state)
    assert any("Раздел оплаты станет доступен" in t for c, t, _ in sent if c == 10001)

    # Case B: load_settings returns None
    sent.clear()
    mock_billing = MagicMock()
    mock_billing.load_settings.return_value = None
    monkeypatch.setattr(manager, "BILLING", mock_billing)
    manager.open_pay(10001, 10001, state)
    assert any("Раздел оплаты станет доступен" in t for c, t, _ in sent if c == 10001)


def test_open_pay_with_settings_sends_button_link(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    state = {
        "managed": {},
        "imported": {},
        "users": {"10001": {"username": "alice"}},
        "locales": {"10001": "ru"},
    }

    mock_settings = MagicMock()
    mock_settings.plan_name = "Тариф Бизнес"
    mock_settings.plan_price_rub = 2500

    mock_billing = MagicMock()
    mock_billing.load_settings.return_value = mock_settings
    now = int(time.time())
    mock_billing.read_state.return_value = {"paid_until": now + 86400 * 10}
    mock_billing.forge_order_id.return_value = "forge_10001_99999"
    mock_billing.create_payment_link.return_value = "https://pay.example.com/form123"

    monkeypatch.setattr(manager, "BILLING", mock_billing)

    manager.open_pay(10001, 10001, state)

    user_msgs = [(t, m) for c, t, m in sent if c == 10001]
    assert len(user_msgs) == 1
    text, markup = user_msgs[0]

    assert "Тариф Бизнес" in text
    assert "2500" in text
    assert "оплачено до" in text

    assert markup and "inline_keyboard" in markup
    button = markup["inline_keyboard"][0][0]
    assert button["url"] == "https://pay.example.com/form123"
    assert "Оплатить" in button["text"]


def test_billing_admin_command(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)
    monkeypatch.setattr(manager, "is_admin", lambda uid: uid == manager.PAVEL_ID)

    mock_billing = MagicMock()
    now = int(time.time())
    mock_billing.read_state.return_value = {
        "paid_until": now + 86400 * 5,
        "payments": [{"date": "2026-10-20", "sum": "2500", "currency": "RUB", "order_id": "ord_1"}],
    }
    monkeypatch.setattr(manager, "BILLING", mock_billing)

    state = {
        "managed": {},
        "imported": {},
        "users": {"123": {"username": "bob"}},
        "locales": {str(manager.PAVEL_ID): "ru"},
        "billing_required": {},
    }

    # 1. /billing 123 require
    msg_req = {
        "chat": {"id": manager.PAVEL_ID, "type": "private"},
        "from": {"id": manager.PAVEL_ID, "language_code": "ru"},
        "text": "/billing 123 require",
    }
    manager.handle_message(msg_req, state)

    assert "123" in state["billing_required"]
    assert state["billing_required"]["123"]["by"] == manager.PAVEL_ID
    assert "Обязательная оплата включена" in sent[-1][1]

    # 2. /billing 123 status
    msg_stat = {
        "chat": {"id": manager.PAVEL_ID, "type": "private"},
        "from": {"id": manager.PAVEL_ID, "language_code": "ru"},
        "text": "/billing 123 status",
    }
    manager.handle_message(msg_stat, state)
    status_text = sent[-1][1]
    assert "Биллинг:" in status_text
    assert "Обязательно" in status_text
    assert "2500 RUB" in status_text

    # 3. /billing @bob free
    msg_free = {
        "chat": {"id": manager.PAVEL_ID, "type": "private"},
        "from": {"id": manager.PAVEL_ID, "language_code": "ru"},
        "text": "/billing @bob free",
    }
    manager.handle_message(msg_free, state)

    assert "123" not in state["billing_required"]
    assert "Обязательная оплата снята" in sent[-1][1]


def test_billing_enforce_overdue_stops_service(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    now = int(time.time())

    mock_billing = MagicMock()
    # Expired 5 days ago (past 3 days grace period)
    mock_billing.read_state.return_value = {"paid_until": now - 5 * 86400}
    monkeypatch.setattr(manager, "BILLING", mock_billing)

    systemctl_calls = []

    def mock_subprocess_run(cmd, *args, **kwargs):
        systemctl_calls.append(list(cmd))
        mock_proc = MagicMock()
        # is-active returns 0 (active)
        mock_proc.returncode = 0
        return mock_proc

    monkeypatch.setattr(manager.subprocess, "run", mock_subprocess_run)

    state = {
        "billing_required": {"12345": {"since": now - 10 * 86400, "by": manager.PAVEL_ID}},
        "managed": {
            "bot_1": {
                "bot_id": 1001,
                "owner_user_id": 12345,
                "profile": "h12345",
                "username": "clientbot",
                "profile_status": "active",
            }
        },
        "imported": {},
        "locales": {"12345": "ru", str(manager.PAVEL_ID): "ru"},
    }

    manager.billing_enforce(state, force=True)

    # Unit stop command was called
    stop_cmds = [c for c in systemctl_calls if "stop" in c]
    assert len(stop_cmds) == 1
    assert stop_cmds[0] == ["/usr/bin/systemctl", "stop", "--", "h12345-hermes.service"]

    # Item paused_for_billing was set
    item = state["managed"]["bot_1"]
    assert "paused_for_billing" in item
    assert item["paused_for_billing"] == now

    # Client was notified with pay button
    client_msgs = [(t, m) for c, t, m in sent if c == 12345]
    assert len(client_msgs) == 1
    assert "Подписка не оплачена" in client_msgs[0][0]
    assert client_msgs[0][1] and client_msgs[0][1]["inline_keyboard"][0][0]["callback_data"] == "pay"

    # Admin was notified with username
    admin_msgs = [t for c, t, _ in sent if c == manager.PAVEL_ID]
    assert len(admin_msgs) == 1
    assert "@clientbot" in admin_msgs[0]


def test_billing_enforce_paid_resumes_service(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    now = int(time.time())

    mock_billing = MagicMock()
    # Paid for 10 days ahead
    mock_billing.read_state.return_value = {"paid_until": now + 10 * 86400}
    monkeypatch.setattr(manager, "BILLING", mock_billing)

    systemctl_calls = []

    def mock_subprocess_run(cmd, *args, **kwargs):
        systemctl_calls.append(list(cmd))
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        return mock_proc

    monkeypatch.setattr(manager.subprocess, "run", mock_subprocess_run)

    state = {
        "billing_required": {"12345": {"since": now - 10 * 86400, "by": manager.PAVEL_ID}},
        "managed": {
            "bot_1": {
                "bot_id": 1001,
                "owner_user_id": 12345,
                "profile": "h12345",
                "username": "clientbot",
                "profile_status": "active",
                "paused_for_billing": now - 3600,
            }
        },
        "imported": {},
        "locales": {"12345": "ru", str(manager.PAVEL_ID): "ru"},
    }

    manager.billing_enforce(state, force=True)

    # Unit start command was called
    start_cmds = [c for c in systemctl_calls if "start" in c]
    assert len(start_cmds) == 1
    assert start_cmds[0] == ["/usr/bin/systemctl", "start", "--", "h12345-hermes.service"]

    # paused_for_billing removed
    item = state["managed"]["bot_1"]
    assert "paused_for_billing" not in item

    # Client was notified of resumption
    client_msgs = [t for c, t, _ in sent if c == 12345]
    assert len(client_msgs) == 1
    assert "Подписка возобновлена" in client_msgs[0]

    # Admin was notified
    admin_msgs = [t for c, t, _ in sent if c == manager.PAVEL_ID]
    assert len(admin_msgs) == 1
    assert "@clientbot" in admin_msgs[0]


def test_billing_enforce_ignores_users_without_require(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    now = int(time.time())

    mock_billing = MagicMock()
    # Expired payment
    mock_billing.read_state.return_value = {"paid_until": now - 10 * 86400}
    monkeypatch.setattr(manager, "BILLING", mock_billing)

    systemctl_calls = []
    monkeypatch.setattr(manager.subprocess, "run", lambda cmd, *a, **kw: systemctl_calls.append(list(cmd)))

    state = {
        "billing_required": {},  # No users required
        "managed": {
            "bot_1": {
                "bot_id": 1001,
                "owner_user_id": 12345,
                "profile": "h12345",
                "profile_status": "active",
            }
        },
        "imported": {
            "legacy_1": {
                "bot_id": 2001,
                "owner_user_id": 67890,
                "profile": "legacyclient",
                "profile_status": "active",
            }
        },
        "locales": {},
    }

    manager.billing_enforce(state, force=True)

    # Nothing touched
    assert len(systemctl_calls) == 0
    assert len(sent) == 0


def test_billing_enforce_rate_limited(monkeypatch):
    now = int(time.time())
    mock_billing = MagicMock()
    mock_billing.read_state.return_value = {"paid_until": now - 5 * 86400}
    monkeypatch.setattr(manager, "BILLING", mock_billing)

    systemctl_calls = []

    def mock_subprocess_run(cmd, *args, **kwargs):
        systemctl_calls.append(list(cmd))
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        return mock_proc

    monkeypatch.setattr(manager.subprocess, "run", mock_subprocess_run)

    state = {
        "billing_required": {"12345": {"since": now - 10 * 86400, "by": manager.PAVEL_ID}},
        "managed": {
            "bot_1": {
                "bot_id": 1001,
                "owner_user_id": 12345,
                "profile": "h12345",
                "username": "clientbot",
                "profile_status": "active",
            }
        },
        "imported": {},
        "locales": {},
    }

    # 1. First call executes
    manager.billing_enforce(state, force=False)
    assert len(systemctl_calls) > 0
    systemctl_calls.clear()

    # 2. Second call immediately after does not execute (rate-limited to 1 hour)
    manager.billing_enforce(state, force=False)
    assert len(systemctl_calls) == 0


# ============================================================================
# 3. Auto-purge integration tests
# ============================================================================

def test_purge_due_purges_expired_records(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    now = int(time.time())

    purge_calls = []

    def mock_purge(owner):
        purge_calls.append(owner)
        return {
            "owner": owner,
            "ok": True,
            "purged_at": now,
            "archive": f"/var/backups/{owner}.tar.zst",
            "steps": [],
        }

    monkeypatch.setattr(manager.offboard_purge, "purge", mock_purge)

    state = {
        "managed": {
            "bot_1": {
                "bot_id": 1001,
                "owner_user_id": 12345,
                "profile": "h12345",
                "username": "alicebot",
                "profile_status": "offboarded",
                "purge_after": now - 100,  # Expired
            },
            "bot_2": {
                "bot_id": 1002,
                "owner_user_id": 12346,
                "profile": "h12346",
                "profile_status": "active",  # Not offboarded
                "purge_after": now - 100,
            },
        },
        "imported": {
            "legacy_1": {
                "bot_id": 2001,
                "owner_user_id": 55555,
                "profile": "legacyclient",
                "username": "legbot",
                "profile_status": "offboarded",
                "purge_after": now - 50,  # Expired
            }
        },
        "locales": {str(manager.PAVEL_ID): "ru"},
    }

    due = manager.purge_due(state, force=True)

    assert len(due) == 2
    assert purge_calls == ["h12345", "legacyclient"]

    # Check updated records
    m1 = state["managed"]["bot_1"]
    assert m1["purged_at"] == now
    assert m1["purge_archive"] == "/var/backups/h12345.tar.zst"

    imp1 = state["imported"]["legacy_1"]
    assert imp1["purged_at"] == now
    assert imp1["purge_archive"] == "/var/backups/legacyclient.tar.zst"

    # Admin notified with archive path and usernames
    admin_msgs = [t for c, t, _ in sent if c == manager.PAVEL_ID]
    assert len(admin_msgs) == 2
    assert any("alicebot" in t and "/var/backups/h12345.tar.zst" in t for t in admin_msgs)
    assert any("legbot" in t and "/var/backups/legacyclient.tar.zst" in t for t in admin_msgs)


def test_purge_due_handles_error(monkeypatch):
    sent, _, _ = capture(monkeypatch)
    now = int(time.time())

    def mock_purge(owner):
        return {
            "owner": owner,
            "ok": False,
            "failed_step": "archive",
            "steps": [],
            "archive": "",
        }

    monkeypatch.setattr(manager.offboard_purge, "purge", mock_purge)

    state = {
        "managed": {
            "bot_1": {
                "bot_id": 1001,
                "owner_user_id": 12345,
                "profile": "h12345",
                "username": "alicebot",
                "profile_status": "offboarded",
                "purge_after": now - 100,
            }
        },
        "imported": {},
        "locales": {str(manager.PAVEL_ID): "ru"},
    }

    due = manager.purge_due(state, force=True)

    assert len(due) == 1
    item = state["managed"]["bot_1"]
    assert item["purge_error"] == "archive"
    assert item["purge_after"] == now + 86400
    assert "purged_at" not in item

    # Admin alerted
    admin_msgs = [t for c, t, _ in sent if c == manager.PAVEL_ID]
    assert len(admin_msgs) == 1
    assert "Не удалось удалить h12345: archive" in admin_msgs[0]


def test_purge_due_rate_limited(monkeypatch):
    now = int(time.time())
    purge_calls = []
    monkeypatch.setattr(
        manager.offboard_purge, "purge",
        lambda owner: purge_calls.append(owner) or {"ok": True, "purged_at": now, "archive": "a"},
    )

    state = {
        "managed": {
            "bot_1": {
                "bot_id": 1001,
                "owner_user_id": 12345,
                "profile": "h12345",
                "profile_status": "offboarded",
                "purge_after": now - 100,
            }
        },
        "imported": {},
    }

    # 1. First call runs
    due1 = manager.purge_due(state, force=False)
    assert len(due1) == 1
    assert len(purge_calls) == 1

    # 2. Second call immediately after does nothing (rate limited to 1 hour)
    due2 = manager.purge_due(state, force=False)
    assert due2 == []
    assert len(purge_calls) == 1


def test_reconcile_runs_expire_and_saves(monkeypatch):
    saved = []
    monkeypatch.setattr(manager, "save_state", lambda s: saved.append(True))
    monkeypatch.setattr(manager, "purge_due", lambda s: None)
    monkeypatch.setattr(manager, "billing_enforce", lambda s: None)

    # support.expire returns [10001]
    monkeypatch.setattr(manager.support, "expire", lambda s: [10001])

    state = {"managed": {}, "imported": {}, "support": {}}
    manager.reconcile_provisioning(state)

    assert len(saved) >= 1


def test_billing_require_gives_three_days_grace_before_stop(monkeypatch):
    sent, calls = [], []
    monkeypatch.setattr(manager, "send", lambda chat_id, text, markup=None: sent.append((chat_id, text, markup)))
    monkeypatch.setattr(manager, "save_state", lambda state: None)

    class R:
        returncode = 0

    monkeypatch.setattr(manager.subprocess, "run", lambda args, **kw: calls.append(args) or R())
    monkeypatch.setattr(manager, "BILLING", None)  # no billing record at all
    now = int(time.time())
    state = {"managed": {"1": {"owner_user_id": 50011, "profile": "h50011", "username": "fresh", "profile_status": "active"}},
             "imported": {}, "billing_required": {"50011": {"since": now - 3600, "by": manager.PAVEL_ID}}}
    manager.billing_enforce(state, force=True)
    assert not any(c[:2] == ["/usr/bin/systemctl", "stop"] for c in calls)
    state["billing_required"]["50011"]["since"] = now - 4 * 86400
    state.pop("billing_enforced_at", None)
    manager.billing_enforce(state, force=True)
    assert any(c[:2] == ["/usr/bin/systemctl", "stop"] for c in calls)
