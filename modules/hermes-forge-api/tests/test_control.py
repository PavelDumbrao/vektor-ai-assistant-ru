from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

MODULE = Path(__file__).resolve().parents[1] / "control.py"
spec = importlib.util.spec_from_file_location("forge_control", MODULE)
control = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(control)


def signed_init(token: str, user_id: int = 12345, now: int = 1_800_000_000) -> str:
    values = {
        "auth_date": str(now),
        "query_id": "AAE-test",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":"), ensure_ascii=False),
    }
    data = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**values, "hash": digest})


def fake_profile(tmp_path: Path, name: str = "pavel"):
    root = tmp_path / name
    hermes = root / ".hermes"
    (hermes / "backups").mkdir(parents=True)
    env = hermes / ".env"
    config = hermes / "config.yaml"
    env.write_text("SAFE_EXISTING=1\n", encoding="utf-8")
    config.write_text(yaml.safe_dump({"mcp_servers": {"maton": {"enabled": False}}}, sort_keys=False), encoding="utf-8")
    entry = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(root), pw_name=name)
    return entry, env, config


def test_validate_init_data_accepts_official_hmac_shape():
    token = "123456:" + ("a" * 32)
    raw = signed_init(token)
    result = control.validate_init_data(raw, bot_token=token, now=1_800_000_000)
    assert result["user_id"] == 12345
    assert result["user"]["first_name"] == "Test"


def test_validate_init_data_rejects_malformed_query_as_unauthorized():
    with pytest.raises(control.ControlError, match="init_data_invalid") as exc:
        control.validate_init_data("invalid", bot_token="123456:" + ("a" * 32), now=1_800_000_000)
    assert exc.value.status == 401


def test_validate_init_data_rejects_tampering_and_expiry():
    token = "123456:" + ("a" * 32)
    raw = signed_init(token)
    with pytest.raises(control.ControlError, match="init_data_signature_invalid"):
        control.validate_init_data(raw.replace("Test", "Hacker"), bot_token=token, now=1_800_000_000)
    with pytest.raises(control.ControlError, match="init_data_expired"):
        control.validate_init_data(raw, bot_token=token, now=1_800_004_000)


def test_session_store_expires():
    store = control.SessionStore(ttl=30)
    token, _ = store.create(777, now=100)
    assert store.resolve(token, now=129) == 777
    with pytest.raises(control.ControlError, match="session_invalid"):
        store.resolve(token, now=131)


def test_owned_registry_is_tenant_scoped_and_sanitized(monkeypatch):
    monkeypatch.setattr(control, "_load_registry_state", lambda: {
        "imported": {"1": {"owner_user_id": 42, "profile": "pavel", "bot_id": 1, "username": "pavel_bot", "name": "Pavel", "profile_status": "active", "token_path": "/secret"}},
        "managed": {"2": {"owner_user_id": 99, "profile": "other", "bot_id": 2, "username": "other_bot", "name": "Other", "profile_status": "active", "token_path": "/secret2"}},
    })
    monkeypatch.setattr(control, "_health", lambda profile: {"healthy": True})
    items = [control._public_hermes(item) for item in control._owned_items(42)]
    assert len(items) == 1
    assert items[0]["profile"] == "pavel"
    serialized = json.dumps(items)
    assert "token_path" not in serialized
    assert "/secret" not in serialized
    with pytest.raises(control.ControlError, match="profile_not_found"):
        control._owned_item(42, "other")


def test_secret_set_never_returns_value_and_delete_removes_it(monkeypatch, tmp_path):
    entry, env, config = fake_profile(tmp_path)
    monkeypatch.setattr(control, "_profile_paths", lambda profile: (entry, env, config))
    monkeypatch.setattr(control, "_owned_item", lambda user_id, profile: {"profile": profile})
    monkeypatch.setattr(control, "_maton_validate", lambda value: "valid")
    control.SESSIONS = control.SessionStore(ttl=1000)
    session, _ = control.SESSIONS.create(42)
    secret = "maton-test-secret-1234567890"

    result = control.dispatch({"op": "set_secret", "session": session, "profile": "pavel", "name": "MCP_MATON_API_KEY", "value": secret})
    assert result["secret"]["configured"] is True
    assert result["secret"]["last4"] == secret[-4:]
    assert secret not in json.dumps(result)
    assert control._read_env(env)["MCP_MATON_API_KEY"] == secret
    assert yaml.safe_load(config.read_text())["mcp_servers"]["maton"]["enabled"] is True

    deleted = control.dispatch({"op": "delete_secret", "session": session, "profile": "pavel", "name": "MCP_MATON_API_KEY"})
    assert deleted["secret"]["configured"] is False
    assert "MCP_MATON_API_KEY" not in control._read_env(env)
    assert yaml.safe_load(config.read_text())["mcp_servers"]["maton"]["enabled"] is False


def test_secret_rollback_removes_new_env_when_config_update_fails(monkeypatch, tmp_path):
    entry, env, config = fake_profile(tmp_path)
    env.unlink()
    monkeypatch.setattr(control, "_profile_paths", lambda profile: (entry, env, config))
    backup = control._set_env_secret("pavel", "MCP_MATON_API_KEY", "maton-test-secret-1234567890")
    assert env.exists()
    control._restore_secret_backup("pavel", backup)
    assert not env.exists()
    assert config.exists()


def test_restart_refuses_busy_profile(monkeypatch):
    monkeypatch.setattr(control, "_health", lambda profile: {"active_agents": 1, "healthy": True})
    monkeypatch.setattr(control, "_active_sessions", lambda profile: 0)
    with pytest.raises(control.ControlError, match="profile_busy"):
        control._restart("pavel")


def test_maton_invalid_key_is_not_stored(monkeypatch, tmp_path):
    entry, env, config = fake_profile(tmp_path)
    monkeypatch.setattr(control, "_profile_paths", lambda profile: (entry, env, config))
    monkeypatch.setattr(control, "_owned_item", lambda user_id, profile: {"profile": profile})
    monkeypatch.setattr(control, "_maton_validate", lambda value: "invalid")
    control.SESSIONS = control.SessionStore(ttl=1000)
    session, _ = control.SESSIONS.create(42)
    with pytest.raises(control.ControlError, match="maton_key_invalid"):
        control.dispatch({"op": "set_secret", "session": session, "profile": "pavel", "name": "MCP_MATON_API_KEY", "value": "maton-test-secret-1234567890"})
    assert "MCP_MATON_API_KEY" not in control._read_env(env)
