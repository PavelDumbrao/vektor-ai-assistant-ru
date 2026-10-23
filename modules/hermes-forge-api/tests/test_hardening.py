from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import socket
import threading
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

CONTROL_PATH = Path(__file__).resolve().parents[1] / "control.py"
control_spec = importlib.util.spec_from_file_location("forge_control_h", CONTROL_PATH)
control = importlib.util.module_from_spec(control_spec)
assert control_spec.loader is not None
control_spec.loader.exec_module(control)

API_PATH = Path(__file__).resolve().parents[1] / "api.py"
api_spec = importlib.util.spec_from_file_location("forge_api_h", API_PATH)
api = importlib.util.module_from_spec(api_spec)
assert api_spec.loader is not None
api_spec.loader.exec_module(api)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fake_entry(home_path: Path, profile: str = "pavel") -> SimpleNamespace:
    return SimpleNamespace(
        pw_uid=os.getuid(),
        pw_gid=os.getgid(),
        pw_dir=str(home_path),
        pw_name=profile,
    )


def fake_profile(monkeypatch, tmp_path, profile: str = "pavel"):
    """Point control at a throwaway home for `profile` and return (entry, hermes_dir)."""
    home = tmp_path / "home" / profile
    hermes = home / ".hermes"
    hermes.mkdir(parents=True, exist_ok=True)
    entry = fake_entry(home, profile)
    monkeypatch.setattr(control, "_profile_paths", lambda _p: (entry, hermes / ".env", hermes / "config.yaml"))
    return entry, hermes


# ---------------------------------------------------------------------------
# 1. File Helper Tests
# ---------------------------------------------------------------------------

def test_file_helper_symlink_env_rejected(tmp_path, monkeypatch):
    profile = "pavel"
    home = tmp_path / profile
    hermes = home / ".hermes"
    hermes.mkdir(parents=True)
    secret_target = tmp_path / "sensitive.txt"
    secret_target.write_text("TOP_SECRET_DO_NOT_TOUCH", encoding="utf-8")

    (hermes / ".env").symlink_to(secret_target)

    entry = fake_entry(home, profile)
    monkeypatch.setattr(control, "HOME_ROOT", tmp_path)

    with pytest.raises(control.ControlError, match="profile_file_unsafe") as exc:
        with control.ProfileFs(profile, entry) as fs:
            fs.read_bytes(".env")
    assert exc.value.status == 409
    assert secret_target.read_text(encoding="utf-8") == "TOP_SECRET_DO_NOT_TOUCH"

    with pytest.raises(control.ControlError, match="profile_file_unsafe") as exc:
        with control.ProfileFs(profile, entry) as fs:
            fs.write_bytes(".env", b"OVERWRITTEN")
    assert exc.value.status == 409
    assert secret_target.read_text(encoding="utf-8") == "TOP_SECRET_DO_NOT_TOUCH"


def test_file_helper_symlink_config_rejected(tmp_path, monkeypatch):
    profile = "pavel"
    home = tmp_path / profile
    hermes = home / ".hermes"
    hermes.mkdir(parents=True)
    secret_target = tmp_path / "sensitive.txt"
    secret_target.write_text("TOP_SECRET_DO_NOT_TOUCH", encoding="utf-8")

    (hermes / "config.yaml").symlink_to(secret_target)

    entry = fake_entry(home, profile)
    monkeypatch.setattr(control, "HOME_ROOT", tmp_path)

    with pytest.raises(control.ControlError, match="profile_file_unsafe") as exc:
        with control.ProfileFs(profile, entry) as fs:
            fs.read_bytes("config.yaml")
    assert exc.value.status == 409


def test_file_helper_symlink_backups_rejected(tmp_path, monkeypatch):
    profile = "pavel"
    home = tmp_path / profile
    hermes = home / ".hermes"
    hermes.mkdir(parents=True)
    other = tmp_path / "outside_dir"
    other.mkdir()

    (hermes / "backups").symlink_to(other)

    entry = fake_entry(home, profile)
    monkeypatch.setattr(control, "HOME_ROOT", tmp_path)

    with pytest.raises(control.ControlError, match="profile_file_unsafe") as exc:
        with control.ProfileFs(profile, entry) as fs:
            fs.ensure_dir("backups", mode=0o700)
    assert exc.value.status == 409


def test_file_helper_symlink_inbox_rejected(tmp_path, monkeypatch):
    profile = "pavel"
    home = tmp_path / profile
    hermes = home / ".hermes"
    zoom = hermes / "zoom-native"
    zoom.mkdir(parents=True)
    other = tmp_path / "outside_inbox"
    other.mkdir()

    (zoom / "inbox").symlink_to(other)

    entry = fake_entry(home, profile)
    monkeypatch.setattr(control, "HOME_ROOT", tmp_path)

    with pytest.raises(control.ControlError, match="profile_file_unsafe") as exc:
        with control.ProfileFs(profile, entry) as fs:
            fs.ensure_dir("zoom-native/inbox", mode=0o700)
    assert exc.value.status == 409


def test_file_helper_wrong_owner_rejected(tmp_path, monkeypatch):
    profile = "pavel"
    home = tmp_path / profile
    hermes = home / ".hermes"
    hermes.mkdir(parents=True)
    monkeypatch.setattr(control, "HOME_ROOT", tmp_path)

    wrong_uid = os.getuid() + 999
    entry = SimpleNamespace(pw_uid=wrong_uid, pw_gid=os.getgid(), pw_dir=str(home), pw_name=profile)

    with pytest.raises(control.ControlError, match="profile_home_unsafe") as exc:
        with control.ProfileFs(profile, entry) as fs:
            fs.read_bytes(".env")
    assert exc.value.status == 409


def test_file_helper_too_large_rejected(tmp_path, monkeypatch):
    profile = "pavel"
    home = tmp_path / profile
    hermes = home / ".hermes"
    hermes.mkdir(parents=True)
    large_file = hermes / ".env"
    large_file.write_bytes(b"A" * (256 * 1024 + 1))

    entry = fake_entry(home, profile)
    monkeypatch.setattr(control, "HOME_ROOT", tmp_path)

    with pytest.raises(control.ControlError, match="profile_file_too_large") as exc:
        with control.ProfileFs(profile, entry) as fs:
            fs.read_bytes(".env")
    assert exc.value.status == 413


# ---------------------------------------------------------------------------
# 2. Invite Tests
# ---------------------------------------------------------------------------

def test_invite_accept_non_owner_profile_rejected(tmp_path, monkeypatch):
    invites_dir = tmp_path / "invites"
    invites_dir.mkdir()
    monkeypatch.setattr(control, "WORKSPACE_INVITE_DIR", invites_dir)
    monkeypatch.setattr(control, "WORKSPACE_STATE_DIR", tmp_path / "ws_state")

    token = "abcdefghijklmnopqrstuvwxyz0123456789"
    invite_file = invites_dir / f"{token}.json"
    invite_file.write_text(json.dumps({
        "schema": "hermes.workspace-invite/v1",
        "profile": "unowned-profile",
        "owner_user_id": 111,
        "expires_at": int(time.time()) + 3600,
        "used": False,
    }), encoding="utf-8")

    monkeypatch.setattr(control, "_owned_item", lambda uid, prof: (_ for _ in ()).throw(control.ControlError("profile_not_found", 404)))

    with pytest.raises(control.ControlError, match="profile_not_found") as exc:
        control._ws_accept(actor=222, name="Bob", token=token)
    assert exc.value.status == 404


def test_invite_double_accept_returns_404(tmp_path, monkeypatch):
    invites_dir = tmp_path / "invites"
    ws_dir = tmp_path / "ws_state"
    invites_dir.mkdir()
    ws_dir.mkdir()
    monkeypatch.setattr(control, "WORKSPACE_INVITE_DIR", invites_dir)
    monkeypatch.setattr(control, "WORKSPACE_STATE_DIR", ws_dir)

    token = "abcdefghijklmnopqrstuvwxyz0123456789"
    invite_file = invites_dir / f"{token}.json"
    invite_file.write_text(json.dumps({
        "schema": "hermes.workspace-invite/v1",
        "profile": "pavel",
        "owner_user_id": 111,
        "expires_at": int(time.time()) + 3600,
        "used": False,
    }), encoding="utf-8")

    monkeypatch.setattr(control, "_owned_item", lambda uid, prof: {"profile": prof, "owner_user_id": uid})
    monkeypatch.setattr(control, "_ws_sync_env", lambda prof, actor, add: False)
    _entry, hermes = fake_profile(monkeypatch, tmp_path, "pavel")
    notifications = []
    monkeypatch.setattr(control, "_send_manager_telegram_message", lambda cid, msg: notifications.append((cid, msg)) or True)

    first = control._ws_accept(actor=222, name="Bob", token=token)
    assert first["accepted"] is True
    tenant_copy = json.loads((hermes / "workspace-members.json").read_text(encoding="utf-8"))
    assert tenant_copy["owner_user_id"] == 111 and [m["user_id"] for m in tenant_copy["members"]] == [222]
    assert len(notifications) == 1
    assert "Bob" in notifications[0][1]

    with pytest.raises(control.ControlError, match="invite_invalid") as exc:
        control._ws_accept(actor=333, name="Charlie", token=token)
    assert exc.value.status == 404


def test_invite_remove_nonexistent_member_or_owner_fails(tmp_path, monkeypatch):
    ws_dir = tmp_path / "ws_state"
    ws_dir.mkdir()
    monkeypatch.setattr(control, "WORKSPACE_STATE_DIR", ws_dir)
    monkeypatch.setattr(control, "_owned_item", lambda uid, prof: {"profile": prof, "owner_user_id": uid})

    profile = "pavel"
    owner_id = 111
    (ws_dir / f"{profile}.json").write_text(json.dumps({
        "schema": "hermes.workspace-members/v1",
        "profile": profile,
        "owner_user_id": owner_id,
        "members": [{"user_id": 222, "name": "Alice", "role": "member"}],
        "grants": [],
        "pending": [],
    }), encoding="utf-8")

    # Removing owner fails
    with pytest.raises(control.ControlError, match="workspace_owner_cannot_be_removed") as exc:
        control._ws_remove(owner=owner_id, profile=profile, member=owner_id)
    assert exc.value.status == 409

    # Removing nonexistent member fails
    with pytest.raises(control.ControlError, match="workspace_member_not_found") as exc:
        control._ws_remove(owner=owner_id, profile=profile, member=999)
    assert exc.value.status == 404


def test_invite_send_message_called_on_accept(tmp_path, monkeypatch):
    invites_dir = tmp_path / "invites"
    ws_dir = tmp_path / "ws_state"
    invites_dir.mkdir()
    ws_dir.mkdir()
    monkeypatch.setattr(control, "WORKSPACE_INVITE_DIR", invites_dir)
    monkeypatch.setattr(control, "WORKSPACE_STATE_DIR", ws_dir)

    token = "abcdefghijklmnopqrstuvwxyz0123456789"
    invite_file = invites_dir / f"{token}.json"
    invite_file.write_text(json.dumps({
        "schema": "hermes.workspace-invite/v1",
        "profile": "pavel",
        "owner_user_id": 777,
        "expires_at": int(time.time()) + 3600,
        "used": False,
    }), encoding="utf-8")

    monkeypatch.setattr(control, "_owned_item", lambda uid, prof: {"profile": prof, "owner_user_id": uid})
    monkeypatch.setattr(control, "_ws_sync_env", lambda prof, actor, add: False)
    _entry, hermes = fake_profile(monkeypatch, tmp_path, "pavel")

    sent_messages = []
    monkeypatch.setattr(control, "_send_manager_telegram_message", lambda chat_id, text: sent_messages.append((chat_id, text)) or True)

    res = control._ws_accept(actor=888, name="David", token=token)
    assert res["accepted"] is True
    assert (hermes / "workspace-members.json").is_file()
    assert len(sent_messages) == 1
    chat_id, text = sent_messages[0]
    assert chat_id == 777
    assert "David" in text
    assert "888" in text
    assert "pavel" in text


# ---------------------------------------------------------------------------
# 3. Secret Tests
# ---------------------------------------------------------------------------

def test_delete_secret_disallowed_name_returns_400(monkeypatch):
    monkeypatch.setattr(control, "_require_session", lambda payload, **kwargs: 123)
    monkeypatch.setattr(control, "_owned_item", lambda uid, prof: {"profile": prof, "owner_user_id": uid})

    with pytest.raises(control.ControlError, match="secret_not_allowed") as exc:
        control.dispatch({
            "op": "delete_secret",
            "session": "valid-token",
            "profile": "pavel",
            "name": "DISALLOWED_SECRET",
        })
    assert exc.value.status == 400

    headers = {"Authorization": "Bearer valid-token"}
    with pytest.raises(api.ApiError, match="secret_not_allowed") as a_exc:
        api.route("DELETE", "/v1/hermes/pavel/secrets/DISALLOWED_SECRET", {}, headers)
    assert a_exc.value.status == 400


# ---------------------------------------------------------------------------
# 4. Session Tests
# ---------------------------------------------------------------------------

def test_session_sensitive_op_reauth_required_when_old():
    store = control.SessionStore(ttl=3600)
    now = time.time()
    old_auth_date = int(now) - 1000  # > 15 minutes (900 seconds)
    token, _ = store.create(user_id=123, auth_date=old_auth_date, now=now)

    # Non-sensitive resolve succeeds
    uid = store.resolve(token, sensitive=False, now=now)
    assert uid == 123

    # Sensitive resolve fails with reauth_required (401)
    with pytest.raises(control.ControlError, match="reauth_required") as exc:
        store.resolve(token, sensitive=True, now=now)
    assert exc.value.status == 401

    # Fresh session passes sensitive check
    fresh_token, _ = store.create(user_id=123, auth_date=int(now), now=now)
    uid_fresh = store.resolve(fresh_token, sensitive=True, now=now)
    assert uid_fresh == 123


def test_session_limit_5_per_user_fifo_eviction():
    store = control.SessionStore(ttl=3600)
    now = 1_000_000.0

    tokens = []
    for i in range(6):
        tok, _ = store.create(user_id=555, auth_date=int(now + i), now=now + i)
        tokens.append(tok)

    # Oldest token (tokens[0]) should have been evicted
    with pytest.raises(control.ControlError, match="session_invalid") as exc:
        store.resolve(tokens[0], now=now + 10)
    assert exc.value.status == 401

    # Remaining 5 tokens (1..5) should still be valid
    for tok in tokens[1:]:
        assert store.resolve(tok, now=now + 10) == 555


def test_session_logout_invalidates_token(monkeypatch):
    store = control.SessionStore(ttl=3600)
    token, _ = store.create(user_id=999, auth_date=int(time.time()))
    monkeypatch.setattr(control, "SESSIONS", store)

    assert store.resolve(token) == 999

    res = control.dispatch({"op": "logout", "session": token})
    assert res == {"logged_out": True}

    with pytest.raises(control.ControlError, match="session_invalid") as exc:
        store.resolve(token)
    assert exc.value.status == 401


# ---------------------------------------------------------------------------
# 5. API Tests
# ---------------------------------------------------------------------------

def test_api_handler_timeout_is_10():
    assert api.Handler.timeout == 10


def test_api_semaphore_overflow_returns_503(monkeypatch):
    # Acquire all 32 permits
    permits = [api._HANDLER_SEMAPHORE.acquire(blocking=False) for _ in range(32)]
    assert all(permits)

    try:
        class DummyRequest:
            def __init__(self):
                self.written = b""

            def sendall(self, data):
                self.written += data

        class DummyServer(api.BoundedThreadingMixIn):
            def shutdown_request(self, request):
                pass

        dummy_req = DummyRequest()
        server = DummyServer()
        server.process_request(dummy_req, ("127.0.0.1", 12345))
        assert b"503 Service Unavailable" in dummy_req.written
        assert b"server_overloaded" in dummy_req.written
    finally:
        for _ in range(32):
            api._HANDLER_SEMAPHORE.release()


def test_api_unix_socket_server_healthz(tmp_path):
    sock_path = tmp_path / "api_test.sock"
    server = api.BoundedThreadingUnixHTTPServer(str(sock_path), api.Handler)

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)

    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(sock_path))
        req = b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        client.sendall(req)

        resp = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            resp += chunk
        client.close()

        assert b"HTTP/1.0 200 OK" in resp or b"HTTP/1.1 200 OK" in resp
        assert b'{"ok":true' in resp
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=2)
        if sock_path.exists():
            sock_path.unlink()


def test_invite_list_and_revoke(tmp_path, monkeypatch):
    invites_dir = tmp_path / "invites"
    ws_dir = tmp_path / "ws_state"
    invites_dir.mkdir()
    ws_dir.mkdir()
    monkeypatch.setattr(control, "WORKSPACE_INVITE_DIR", invites_dir)
    monkeypatch.setattr(control, "WORKSPACE_STATE_DIR", ws_dir)
    monkeypatch.setattr(control, "_owned_item", lambda uid, prof: {"profile": prof, "owner_user_id": uid})

    profile = "pavel"
    owner_id = 111

    created = control._ws_invite(owner_id, profile)
    assert "invite_url" in created
    token = created["invite_url"].split("invite_")[-1]

    inv_list = control._ws_list_invites(owner_id, profile)
    tokens = [item["token"] for item in inv_list["items"]]
    assert token in tokens

    rev = control._ws_revoke_invite(owner_id, profile, token)
    assert rev == {"revoked": True}

    inv_list2 = control._ws_list_invites(owner_id, profile)
    tokens2 = [item["token"] for item in inv_list2["items"]]
    assert token not in tokens2

    with pytest.raises(control.ControlError, match="invite_invalid") as exc:
        control._ws_accept(actor=222, name="Bob", token=token)
    assert exc.value.status == 404


def test_ops_audit_log_format_and_rotation(tmp_path, monkeypatch):
    audit_file = tmp_path / "ops.log"
    monkeypatch.setattr(control, "OPS_AUDIT_FILE", audit_file)
    monkeypatch.setattr(control, "MAX_OPS_AUDIT_BYTES", 500)

    control._audit_op("restart", actor_user_id=123, profile="pavel", result="success", details={"service": "pavel-hermes.service"}, ts=1700000000)
    assert audit_file.exists()
    lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry == {
        "actor_user_id": 123,
        "details": {"service": "pavel-hermes.service"},
        "op": "restart",
        "profile": "pavel",
        "result": "success",
        "timestamp": 1700000000,
    }

    for i in range(10):
        control._audit_op("set_secret", actor_user_id=123, profile="pavel", result="success", details={"secret": f"KEY_{i}"})

    rotated = tmp_path / "ops.log.1"
    assert rotated.exists()
    assert audit_file.exists()


# ---------------------------------------------------------------------------
# 6. Review follow-ups (architect)
# ---------------------------------------------------------------------------

def test_sensitive_ops_cover_review_list():
    expected = {
        "create_workspace_invite", "accept_workspace_invite", "revoke_workspace_invite",
        "remove_workspace_member", "grant_workspace_tool", "revoke_workspace_tool",
        "set_secret", "delete_secret", "bind_zoom", "capability_action",
    }
    assert expected <= set(control.SENSITIVE_OPS)
    assert "restart" not in control.SENSITIVE_OPS


def test_path_based_helpers_are_gone():
    source = CONTROL_PATH.read_text(encoding="utf-8")
    assert "def _atomic_text(" not in source
    assert "def _tenant_zoom_dir(" not in source
    assert "preexec_fn" not in source


def test_safe_yaml_load_isolates_bombs_and_parses_plain_yaml():
    assert control._safe_yaml_load("a: 1\nb: [x, y]\n") == {"a": 1, "b": ["x", "y"]}
    bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n"
    prev = "a"
    for name in "bcdefghij":
        bomb += f"{name}: &{name} [*{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}]\n"
        prev = name
    with pytest.raises(control.ControlError, match="config_invalid"):
        control._safe_yaml_load(bomb)


def test_team_ops_are_audited(tmp_path, monkeypatch):
    audit_file = tmp_path / "ops.log"
    monkeypatch.setattr(control, "OPS_AUDIT_FILE", audit_file)
    monkeypatch.setattr(control, "_require_session", lambda payload, **kwargs: 111)
    monkeypatch.setattr(control, "_owned_item", lambda uid, prof: {"profile": prof, "owner_user_id": uid})
    monkeypatch.setattr(control, "_ws_invite", lambda owner, profile: {"invite_url": "https://t.me/x?startapp=invite_abc", "expires_at": 1})
    res = control.dispatch({"op": "create_workspace_invite", "session": "s", "profile": "pavel"})
    assert "invite_url" in res
    entry = json.loads(audit_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert entry["op"] == "create_workspace_invite" and entry["result"] == "success" and entry["profile"] == "pavel"


def test_guard_blocks_live_profile_lookup(tmp_path, monkeypatch):
    """Without an explicit tmp home a profile operation must fail loudly instead of reaching /home."""
    invites_dir = tmp_path / "invites"
    invites_dir.mkdir()
    monkeypatch.setattr(control, "WORKSPACE_INVITE_DIR", invites_dir)
    monkeypatch.setattr(control, "WORKSPACE_STATE_DIR", tmp_path / "ws_state")
    monkeypatch.setattr(control, "_owned_item", lambda uid, prof: {"profile": prof, "owner_user_id": uid})
    monkeypatch.setattr(control, "_ws_sync_env", lambda prof, actor, add: False)
    monkeypatch.setattr(control, "_send_manager_telegram_message", lambda cid, msg: True)
    token = "guardguardguardguardguardguard12"
    (invites_dir / f"{token}.json").write_text(json.dumps({
        "schema": "hermes.workspace-invite/v1", "profile": "pavel", "owner_user_id": 111,
        "expires_at": int(time.time()) + 3600, "used": False,
    }), encoding="utf-8")
    with pytest.raises(RuntimeError, match="live profile lookup"):
        control._ws_accept(actor=222, name="Bob", token=token)
    with pytest.raises(KeyError):
        control.pwd.getpwnam("pavel")
