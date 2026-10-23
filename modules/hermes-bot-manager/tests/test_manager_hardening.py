from __future__ import annotations

import importlib.util
import json
import os
import shutil
import time
from pathlib import Path

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


def test_authorization_rules(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "INSTANCES_DIR", tmp_path / "instances")
    manager.INSTANCE_OWNERS = None
    state = {
        "managed": {},
        "imported": {},
        "allowed_users": {},
        "denied_users": {},
    }

    # 1. User with matching home_channel in config.yaml, but not in state -> NOT authorized
    monkeypatch.setattr(manager, "find_profile_for_owner", lambda uid: "some_profile")
    assert manager.is_authorized_user(99901, state) is False

    # 2. Owner of entry in state['managed'] -> authorized
    state["managed"]["7000000001"] = {"owner_user_id": 99902, "bot_id": 7000000001}
    assert manager.is_authorized_user(99902, state) is True

    # 3. Owner of instance file in state/instances -> authorized
    (tmp_path / "instances").mkdir(parents=True, exist_ok=True)
    inst_file = tmp_path / "instances" / "hermes-99903.json"
    inst_file.write_text(json.dumps({"owner_telegram_id": 99903}))
    manager.INSTANCE_OWNERS = None
    assert manager.is_authorized_user(99903, state) is True

    # 4. /deny removes access even for owner of managed bot
    sent = capture(monkeypatch)
    admin_msg = {
        "chat": {"id": manager.PAVEL_ID, "type": "private"},
        "from": {"id": manager.PAVEL_ID},
        "text": "/deny 99902",
    }
    manager.handle_message(admin_msg, state)
    assert "99902" in state["denied_users"]
    assert manager.is_authorized_user(99902, state) is False


def test_install_profile_token_never_writes_home(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(manager, "TOKEN_UPDATES_DIR", tmp_path / "state" / "token-updates")
    monkeypatch.setattr(manager, "PROFILE_DIR", tmp_path / "profiles")
    monkeypatch.setattr(manager, "INSTANCES_DIR", tmp_path / "instances")

    (tmp_path / "profiles").mkdir(parents=True, exist_ok=True)
    (tmp_path / "profiles" / "testclient.json").write_text(json.dumps({"owner": "testclient"}))

    written_paths = []
    replaced_paths = []
    real_write = Path.write_text
    real_replace = os.replace

    def tracking_write(self, data, *args, **kwargs):
        written_paths.append(str(self))
        return real_write(self, data, *args, **kwargs)

    def tracking_replace(src, dst, *args, **kwargs):
        replaced_paths.append((str(src), str(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", tracking_write)
    monkeypatch.setattr(os, "replace", tracking_replace)
    monkeypatch.setattr(manager, "send", lambda *_: None)

    status = manager.install_profile_token("testclient", "123456:ABC-DEF_12345678901234567890")
    assert status == "pending_operator"

    # Verify no file under /home was written or replaced
    for path in written_paths:
        assert not path.startswith("/home"), f"Attempted write to {path}"
    for src, dst in replaced_paths:
        assert not src.startswith("/home") and not dst.startswith("/home"), f"Attempted replace to {dst}"

    # Verify task file was created under token-updates in state dir
    task_file = tmp_path / "state" / "token-updates" / "testclient.json"
    assert task_file.is_file()
    task_data = json.loads(task_file.read_text(encoding="utf-8"))
    assert task_data["status"] == "pending_operator"
    assert task_data["owner"] == "testclient"


def test_receipt_validation_and_failures(monkeypatch, tmp_path):
    sent = capture(monkeypatch)
    monkeypatch.setattr(manager, "PROVISIONING_DIR", tmp_path / "provisioning")
    (tmp_path / "provisioning").mkdir(parents=True, exist_ok=True)

    # 1. Receipt with wrong bot_id is ignored
    wrong_receipt = tmp_path / "provisioning" / "hermes-50001.json"
    wrong_receipt.write_text(json.dumps({
        "state": "active",
        "bot_id": 99999,
        "updated_at": int(time.time()),
    }))
    state = {"managed": {"8000000001": {
        "bot_id": 8000000001,
        "owner_user_id": 50001,
        "profile": "h50001",
        "profile_status": "provisioning",
        "instance_id": "hermes-50001",
        "username": "testbot",
        "provision_started_at": int(time.time()) - 10,
    }}}
    manager.reconcile_provisioning(state)
    assert state["managed"]["8000000001"]["profile_status"] == "provisioning"

    # 2. Failed receipt for active assistant does NOT demote to provision_failed
    state["managed"]["8000000001"]["profile_status"] = "active"
    failed_receipt = tmp_path / "provisioning" / "hermes-50001.json"
    failed_receipt.write_text(json.dumps({
        "state": "failed",
        "bot_id": 8000000001,
        "error_code": "asr_worker_down",
        "updated_at": int(time.time()),
    }))
    manager.reconcile_provisioning(state)
    assert state["managed"]["8000000001"]["profile_status"] == "active"
    assert state["managed"]["8000000001"]["last_error"] == "asr_worker_down"
    assert any("Health Alert" in text for _chat, text, _markup in sent)

    # 3. Timeout on provisioning (> PROVISION_DEADLINE_S)
    wrong_receipt.unlink()
    state["managed"]["8000000001"]["profile_status"] = "provisioning"
    state["managed"]["8000000001"]["provision_started_at"] = int(time.time()) - 1500
    manager.reconcile_provisioning(state)
    assert state["managed"]["8000000001"]["profile_status"] == "provision_failed"
    assert state["managed"]["8000000001"]["error_code"] == "provision_timeout"

    # 4. Provisioner service exited without receipt
    state["managed"]["8000000001"]["profile_status"] = "provisioning"
    state["managed"]["8000000001"]["provision_started_at"] = int(time.time()) - 100
    class UnitState:
        stdout = "failed\nexit-code\n"
        returncode = 0
    monkeypatch.setattr(manager.subprocess, "run", lambda *a, **k: UnitState())
    manager.reconcile_provisioning(state)
    assert state["managed"]["8000000001"]["profile_status"] == "provision_failed"
    assert state["managed"]["8000000001"]["error_code"] == "provisioner_exited"


def test_managed_bot_token_pending_retry_and_dedup(monkeypatch, tmp_path):
    sent = capture(monkeypatch)
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)
    monkeypatch.setattr(manager, "stable_release_id", lambda: "hermes-0.21.0-test")
    monkeypatch.setattr(manager, "store_managed_token", lambda *_: tmp_path / "managed.env")
    monkeypatch.setattr(manager, "start_provisioning", lambda *_: "proai-hermes-provisioner@hermes-50002.service")

    api_calls = []
    def fake_api(method, payload=None, timeout=65):
        api_calls.append(method)
        if method == "getManagedBotToken":
            raise RuntimeError("bot_api_timeout")
        return True

    monkeypatch.setattr(manager, "api", fake_api)

    state = {"managed": {}, "drafts": {}}
    update = {
        "managed_bot": {
            "user": {"id": 50002, "language_code": "ru"},
            "bot": {"id": 9000000002, "username": "pending_bot", "first_name": "Pending AI"},
        }
    }

    # Step (a) saves token_pending and step (b) fails gracefully
    manager.managed_event(update, state)
    item = state["managed"]["9000000002"]
    assert item["profile_status"] == "token_pending"
    assert item["retry_count"] == 1
    assert item["next_retry_at"] > 0
    assert any("чуть больше времени" in text for _chat, text, _markup in sent)

    # Duplicate update does not reset status
    manager.managed_event(update, state)
    assert state["managed"]["9000000002"]["profile_status"] == "token_pending"
    assert state["managed"]["9000000002"]["retry_count"] == 1

    # Retry succeeds when time reaches next_retry_at
    monkeypatch.setattr(
        manager, "api",
        lambda method, payload=None, timeout=65: "test-token" if method == "getManagedBotToken" else True,
    )
    monkeypatch.setattr(time, "time", lambda: item["next_retry_at"] + 10)
    manager.reconcile_provisioning(state)
    assert state["managed"]["9000000002"]["profile_status"] == "provisioning"


def test_second_bot_blocked_on_show_create(monkeypatch):
    sent = capture(monkeypatch)
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)
    monkeypatch.setattr(manager, "api", lambda method, payload=None, timeout=65: {"can_manage_bots": True})

    # Case 1: Existing bot in provisioning
    state = {
        "managed": {
            "9000000001": {"owner_user_id": 50003, "profile_status": "provisioning", "username": "first_bot"}
        },
        "imported": {},
        "drafts": {},
    }
    manager.show_create(50003, {"id": 50003, "language_code": "ru"}, state)
    assert any("уже идёт подключение" in text for _chat, text, _markup in sent)
    assert "50003" not in state["drafts"]

    # Case 2: Existing bot in active
    sent.clear()
    state["managed"]["9000000001"]["profile_status"] = "active"
    manager.show_create(50003, {"id": 50003, "language_code": "ru"}, state)
    assert any("уже есть активный" in text and "@first_bot" in text for _chat, text, _markup in sent)
    assert any("запросил второго бота" in text for chat_id, text, _markup in sent if chat_id == manager.PAVEL_ID)
    assert "50003" not in state["drafts"]


def test_load_and_save_state_integrity(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    bak_file = tmp_path / "state.json.bak"
    monkeypatch.setattr(manager, "STATE_FILE", state_file)
    monkeypatch.setattr(manager, "STATE_DIR", tmp_path)

    # 1. save_state creates .bak on subsequent writes
    manager.save_state({"offset": 1, "managed": {}})
    assert state_file.is_file()
    assert not bak_file.is_file()

    manager.save_state({"offset": 2, "managed": {}})
    assert bak_file.is_file()
    assert json.loads(bak_file.read_text())["offset"] == 1
    assert json.loads(state_file.read_text())["offset"] == 2

    # 2. Corrupt state.json is renamed to .corrupt-<ts> and .bak is loaded
    state_file.write_text("{broken json syntax")
    loaded = manager.load_state()
    assert loaded["offset"] == 1
    corrupt_files = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(corrupt_files) == 1

    # 3. Unauthorized users do not have locales saved
    monkeypatch.setattr(manager, "is_authorized_user", lambda uid, state: False)
    monkeypatch.setattr(manager, "send", lambda *_: None)
    state = {"managed": {}, "locales": {}}
    manager.handle_message({
        "chat": {"id": 99999, "type": "private"},
        "from": {"id": 99999, "language_code": "de"},
        "text": "/start",
    }, state)
    assert "99999" not in state["locales"]


def test_admin_commands_access_and_behavior(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(manager, "send", lambda chat_id, text, markup=None: sent.append((chat_id, text, markup)))
    monkeypatch.setattr(manager, "save_state", lambda state: None)
    monkeypatch.setattr(manager, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(manager, "MANAGED_DIR", tmp_path / "state" / "managed")
    monkeypatch.setattr(manager, "INSTANCES_DIR", tmp_path / "state" / "instances")
    monkeypatch.setattr(manager, "PROVISIONING_DIR", tmp_path / "state" / "provisioning")
    monkeypatch.setattr(manager, "OFFBOARDED_DIR", tmp_path / "state" / "offboarded")
    monkeypatch.setattr(manager, "stable_release_id", lambda: "hermes-0.21.0-test")

    state = {
        "managed": {
            "9000000001": {
                "bot_id": 9000000001,
                "owner_user_id": 50004,
                "profile": "h50004",
                "username": "failing_bot",
                "profile_status": "provision_failed",
                "error_code": "setup_failed",
                "attempt": 1,
            }
        },
        "pending_confirmations": {},
    }

    # 1. Non-admin is denied for admin commands
    monkeypatch.setattr(manager, "is_admin", lambda uid: False)
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)
    for cmd in ["/tenants", "/failed", "/retry 9000000001", "/offboard 50004"]:
        sent.clear()
        manager.handle_message({
            "chat": {"id": 50004, "type": "private"},
            "from": {"id": 50004},
            "text": cmd,
        }, state)
        assert any("Доступ закрыт" in text or "Access denied" in text for _chat, text, _markup in sent)

    # 2. Admin calls /tenants and /failed
    monkeypatch.setattr(manager, "is_admin", lambda uid: uid == manager.PAVEL_ID)
    sent.clear()
    manager.handle_message({
        "chat": {"id": manager.PAVEL_ID, "type": "private"},
        "from": {"id": manager.PAVEL_ID},
        "text": "/tenants",
    }, state)
    assert any("Таблица тенантов" in text and "@failing_bot" in text for _chat, text, _markup in sent)

    sent.clear()
    manager.handle_message({
        "chat": {"id": manager.PAVEL_ID, "type": "private"},
        "from": {"id": manager.PAVEL_ID},
        "text": "/failed",
    }, state)
    assert any("Сбойные тенанты" in text and "setup_failed" in text for _chat, text, _markup in sent)

    # 3. Admin /retry
    calls = []
    monkeypatch.setattr(manager, "start_provisioning", lambda inst, overwrite=False: calls.append(inst.instance_id) or "unit")
    sent.clear()
    manager.handle_message({
        "chat": {"id": manager.PAVEL_ID, "type": "private"},
        "from": {"id": manager.PAVEL_ID},
        "text": "/retry 9000000001",
    }, state)
    assert state["managed"]["9000000001"]["attempt"] == 2
    assert state["managed"]["9000000001"]["profile_status"] == "provisioning"
    assert calls == ["hermes-50004"]

    # 4. Admin /offboard two-step confirmation
    sent.clear()
    manager.handle_message({
        "chat": {"id": manager.PAVEL_ID, "type": "private"},
        "from": {"id": manager.PAVEL_ID},
        "text": "/offboard 50004",
    }, state)
    assert "offboard:50004" in state["pending_confirmations"]
    assert any("/offboard 50004 confirm" in text for _chat, text, _markup in sent)

    # Second step: confirm
    subp_calls = []
    class UnitState:
        returncode = 0
    monkeypatch.setattr(manager.subprocess, "run", lambda args, **kw: subp_calls.append(args) or UnitState())
    sent.clear()
    manager.handle_message({
        "chat": {"id": manager.PAVEL_ID, "type": "private"},
        "from": {"id": manager.PAVEL_ID},
        "text": "/offboard 50004 confirm",
    }, state)
    assert state["managed"]["9000000001"]["profile_status"] == "offboarded"
    assert "offboarded_at" in state["managed"]["9000000001"]
    assert any("отключён" in text or "выведен из эксплуатации" in text for _chat, text, _markup in sent)
    assert any("userdel" in text for _chat, text, _markup in sent)
    assert any("lanectl" in a[0] for a in subp_calls)


def test_offboard_legacy_imported_client_uses_named_profile(monkeypatch, tmp_path):
    """Legacy clients live under a named Linux profile (pavel, bebov, ...), not h<tg_id>: /offboard must stop THEIR units."""
    sent = []
    monkeypatch.setattr(manager, "send", lambda chat_id, text, markup=None: sent.append((chat_id, text, markup)))
    monkeypatch.setattr(manager, "save_state", lambda state: None)
    monkeypatch.setattr(manager, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(manager, "MANAGED_DIR", tmp_path / "state" / "managed")
    monkeypatch.setattr(manager, "INSTANCES_DIR", tmp_path / "state" / "instances")
    monkeypatch.setattr(manager, "PROVISIONING_DIR", tmp_path / "state" / "provisioning")
    monkeypatch.setattr(manager, "OFFBOARDED_DIR", tmp_path / "state" / "offboarded")
    monkeypatch.setattr(manager, "is_admin", lambda uid: uid == manager.PAVEL_ID)
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)
    subp_calls = []

    class UnitState:
        returncode = 0

    monkeypatch.setattr(manager.subprocess, "run", lambda args, **kw: subp_calls.append(args) or UnitState())
    state = {
        "managed": {},
        "imported": {"8651847038": {"owner_user_id": 50005, "profile": "legacyowner", "username": "legacy_bot", "profile_status": "active"}},
        "pending_confirmations": {"offboard:50005": {"expires_at": int(time.time()) + 100}},
    }
    manager.handle_message({"chat": {"id": manager.PAVEL_ID, "type": "private"}, "from": {"id": manager.PAVEL_ID},
                            "text": "/offboard 50005 confirm"}, state)
    units = [a[-1] for a in subp_calls if a[0] == "/usr/bin/systemctl"]
    assert "legacyowner-hermes.service" in units
    assert "legacyowner-hermes-passive-secretary-retention.timer" in units
    assert "proai-hermes-shared-metrics-export@legacyowner.timer" in units
    assert not any(u.startswith("h50005") for u in units)
    assert any(a[:3] == ["/usr/local/sbin/lanectl", "remove", "--client"] and a[3] == "legacyowner" for a in subp_calls)
    assert state["imported"]["8651847038"]["profile_status"] == "offboarded"
    assert any("hermes_legacyowner" in text and "passive-secretary-postgres" in text for _c, text, _m in sent)


def test_imported_owner_never_gets_second_hire_flow(monkeypatch):
    sent = capture(monkeypatch)
    monkeypatch.setattr(manager, "is_authorized_user", lambda *_: True)
    monkeypatch.setattr(manager, "api", lambda method, payload=None, timeout=65: {"can_manage_bots": True})
    monkeypatch.setattr(manager, "_imported_service_active", lambda item: False)  # service down: still no second hire
    state = {"managed": {}, "drafts": {},
             "imported": {"8651847038": {"owner_user_id": 50006, "profile": "legacyowner", "username": "legacy_bot", "profile_status": "inactive"}}}
    manager.show_create(50006, {"id": 50006, "language_code": "ru"}, state)
    assert any("уже есть активный" in text and "@legacy_bot" in text for _c, text, _m in sent)
    assert "50006" not in state["drafts"]


def test_replace_token_stub_removed():
    source = MODULE.read_text(encoding="utf-8")
    assert "/replace-token" not in source


def _policy(tmp_path, release_id: str, ready: bool = True):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "defaults": {"forge_new": {"channel": "stable", "track": "modern"}},
        "tracks": {"modern": {"stable": {"release_id": release_id}}},
    }))
    releases = tmp_path / "releases"
    (releases / release_id).mkdir(parents=True, exist_ok=True)
    (releases / release_id / "runtime.json").write_text(json.dumps(
        {"state": "ready" if ready else "building", "release_id": release_id}))
    return policy, releases


def test_new_hire_release_comes_from_fleet_policy(monkeypatch, tmp_path):
    policy, releases = _policy(tmp_path, "hermes-0.21.0-policy-stable")
    monkeypatch.setattr(manager, "FLEET_POLICY", policy)
    monkeypatch.setattr(manager, "RELEASE_ROOT", releases)
    monkeypatch.setattr(manager, "load_manager_settings", lambda: {"FORGE_STABLE_RELEASE_ID": "hermes-0.21.0-env-fallback"})
    assert manager.stable_release_id() == "hermes-0.21.0-policy-stable"


def test_new_hire_release_falls_back_to_env_when_policy_unusable(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "load_manager_settings", lambda: {"FORGE_STABLE_RELEASE_ID": "hermes-0.21.0-env-fallback"})
    monkeypatch.setattr(manager, "FLEET_POLICY", tmp_path / "missing.json")
    monkeypatch.setattr(manager, "RELEASE_ROOT", tmp_path / "releases")
    assert manager.stable_release_id() == "hermes-0.21.0-env-fallback"
    policy, releases = _policy(tmp_path, "hermes-0.21.0-not-ready", ready=False)
    monkeypatch.setattr(manager, "FLEET_POLICY", policy)
    monkeypatch.setattr(manager, "RELEASE_ROOT", releases)
    assert manager.stable_release_id() == "hermes-0.21.0-env-fallback"
    monkeypatch.setattr(manager, "load_manager_settings", lambda: {"FORGE_STABLE_RELEASE_ID": "../etc"})
    try:
        manager.stable_release_id()
    except RuntimeError as exc:
        assert str(exc) == "forge_stable_release_missing"
    else:
        raise AssertionError("invalid release id must be rejected")
