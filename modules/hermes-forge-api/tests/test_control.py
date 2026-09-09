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


def _install_capability_markers(entry):
    hermes = Path(entry.pw_dir) / ".hermes"
    for relative in (
        "plugins/image_gen/grsai/plugin.yaml",
        "plugins/passive-secretary/plugin.yaml",
        "plugins/passive-secretary/settings.json",
        "plugins/video-editor/plugin.yaml",
        "skills/video-editor/SKILL.md",
        "hermes-agent/agent/web_search_registry.py",
        "hermes-agent/plugins/web/tavily/plugin.yaml",
    ):
        path = hermes / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n" if path.suffix == ".json" else "marker\n", encoding="utf-8")


def test_capability_states_report_bounded_live_readiness(monkeypatch, tmp_path):
    entry, env, config = fake_profile(tmp_path)
    _install_capability_markers(entry)
    env.write_text("SAFE_EXISTING=1\nMCP_MATON_API_KEY=maton-test-secret-1234567890\n", encoding="utf-8")
    config.write_text(yaml.safe_dump({
        "agent": {"disabled_toolsets": []},
        "plugins": {"enabled": ["image_gen/grsai", "passive-secretary", "video-editor"]},
        "mcp_servers": {"maton": {"enabled": True}},
        "image_gen": {"provider": "grsai", "model": "gpt-image-2.5"},
        "search": {"provider": "tavily"},
    }, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(control, "_profile_paths", lambda _profile: (entry, env, config))
    monkeypatch.setattr(control, "_service_state", lambda _profile: "active")
    monkeypatch.setattr(control, "_unit_active", lambda _unit: True)
    monkeypatch.setattr(control, "_unit_enabled", lambda _unit: True)

    result = control._capability_states("pavel")
    rows = {item["id"]: item for item in result["items"]}
    assert result["schema"] == "hermes.capability-state/v1"
    assert rows["image-studio"] == {"id": "image-studio", "installed": True, "enabled": True, "health": "healthy", "reason": "ready"}
    assert rows["telegram-secretary"]["health"] == "healthy"
    assert rows["video-editor"]["health"] == "healthy"
    assert rows["web-search"]["health"] == "healthy"
    assert rows["maton"]["health"] == "unknown"
    assert rows["maton"]["reason"] == "external_check_required"
    assert rows["finance"]["health"] == "planned"
    serialized = json.dumps(result)
    assert "maton-test-secret" not in serialized
    assert str(tmp_path) not in serialized


def test_capability_states_surface_config_drift_without_probing_secrets(monkeypatch, tmp_path):
    entry, env, config = fake_profile(tmp_path)
    _install_capability_markers(entry)
    config.write_text(yaml.safe_dump({
        "agent": {"disabled_toolsets": ["passive_secretary"]},
        "plugins": {"enabled": ["image_gen/grsai", "passive-secretary", "video-editor"]},
        "mcp_servers": {"maton": {"enabled": True}},
        "image_gen": {"provider": "grsai"},
        "search": {"provider": "tavily"},
    }, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(control, "_profile_paths", lambda _profile: (entry, env, config))
    monkeypatch.setattr(control, "_service_state", lambda _profile: "active")
    monkeypatch.setattr(control, "_unit_active", lambda _unit: False)
    monkeypatch.setattr(control, "_unit_enabled", lambda _unit: True)

    rows = {item["id"]: item for item in control._capability_states("pavel")["items"]}
    assert rows["maton"]["health"] == "degraded"
    assert rows["maton"]["reason"] == "config_mismatch"
    assert rows["telegram-secretary"]["enabled"] is False
    assert rows["telegram-secretary"]["health"] == "disabled"
    assert rows["video-editor"]["health"] == "degraded"
    assert rows["video-editor"]["reason"] == "shared_dependency_unavailable"


def test_list_capabilities_dispatch_remains_owner_scoped(monkeypatch):
    control.SESSIONS = control.SessionStore(ttl=1000)
    session, _ = control.SESSIONS.create(42)
    seen = []
    monkeypatch.setattr(control, "_owned_item", lambda user_id, profile: seen.append((user_id, profile)) or {"profile": profile})
    monkeypatch.setattr(control, "_capability_states", lambda profile: {"schema": "hermes.capability-state/v1", "items": []})
    result = control.dispatch({"op": "list_capabilities", "session": session, "profile": "pavel"})
    assert result["schema"] == "hermes.capability-state/v1"
    assert seen == [(42, "pavel")]


def test_capability_state_ids_match_catalog_v1_manifests():
    repo = MODULE.parents[2]
    catalog_dir = repo / "server/forge/catalog/capabilities"
    manifest_ids = set()
    for path in catalog_dir.glob("*.yaml"):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        manifest_ids.add(str(payload.get("id") or ""))
    assert manifest_ids == set(control.CAPABILITY_IDS)


def _action_store(monkeypatch, tmp_path):
    root = tmp_path / "forge-action-state"
    monkeypatch.setattr(control, "ACTION_STATE_ROOT", root)
    monkeypatch.setattr(control, "ACTION_JOBS_DIR", root / "jobs")
    monkeypatch.setattr(control, "ACTION_DESIRED_DIR", root / "desired")
    monkeypatch.setattr(control, "ACTION_AUDIT_DIR", root / "audit")
    monkeypatch.setattr(control, "ACTION_AUDIT_FILE", root / "audit/capability-actions.jsonl")
    return root


def _action_profile(monkeypatch, tmp_path):
    entry, env, config = fake_profile(tmp_path)
    _install_capability_markers(entry)
    config.write_text(yaml.safe_dump({
        "agent": {"disabled_toolsets": []},
        "plugins": {"enabled": ["image_gen/grsai", "passive-secretary", "video-editor"]},
        "mcp_servers": {"maton": {"enabled": False}},
        "image_gen": {"provider": "grsai", "model": "gpt-image-2.5"},
        "search": {"provider": "tavily"},
    }, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(control, "_profile_paths", lambda _profile: (entry, env, config))
    monkeypatch.setattr(control, "_profile_release", lambda _profile: "hermes-test-release")
    monkeypatch.setattr(control, "_service_state", lambda _profile: "active")
    monkeypatch.setattr(control, "_unit_active", lambda _unit: True)
    monkeypatch.setattr(control, "_unit_enabled", lambda _unit: True)
    monkeypatch.setattr(control, "_active_sessions", lambda _profile: 0)
    monkeypatch.setattr(control, "_health", lambda _profile: {"healthy": True, "active_agents": 0})
    return entry, env, config


def test_capability_action_disable_and_enable_persists_sanitized_state(monkeypatch, tmp_path):
    root = _action_store(monkeypatch, tmp_path)
    _, _, config = _action_profile(monkeypatch, tmp_path)
    monkeypatch.setattr(control, "_restart", lambda _profile: {"healthy": True})

    disabled = control._capability_action(42, "pavel", "image-studio", "disable")
    assert disabled["outcome"] == "success"
    assert disabled["capability"]["enabled"] is False
    assert "image_gen" in yaml.safe_load(config.read_text())["agent"]["disabled_toolsets"]

    enabled = control._capability_action(42, "pavel", "image-studio", "enable")
    assert enabled["outcome"] == "success"
    assert enabled["capability"]["enabled"] is True
    assert "image_gen" not in yaml.safe_load(config.read_text())["agent"]["disabled_toolsets"]

    jobs = sorted((root / "jobs").glob("capability-*.json"))
    assert len(jobs) == 2
    for path in jobs:
        payload = json.loads(path.read_text())
        assert payload["state"] == "success"
        assert payload["actor_user_id"] == 42
        assert payload["profile"] == "pavel"
        assert "secret" not in json.dumps(payload).lower()

    desired = json.loads((root / "desired/pavel.json").read_text())
    assert desired["capabilities"]["image-studio"]["enabled"] is True
    audit = (root / "audit/capability-actions.jsonl").read_text()
    assert '"outcome":"requested"' in audit
    assert '"outcome":"success"' in audit
    assert "MCP_MATON_API_KEY" not in audit


def test_capability_action_rolls_back_config_when_restart_fails(monkeypatch, tmp_path):
    root = _action_store(monkeypatch, tmp_path)
    _, _, config = _action_profile(monkeypatch, tmp_path)
    original = config.read_text()
    calls = []

    def restart(_profile):
        calls.append(1)
        if len(calls) == 1:
            raise control.ControlError("restart_failed", 503)
        return {"healthy": True}

    monkeypatch.setattr(control, "_restart", restart)
    with pytest.raises(control.ControlError, match="restart_failed"):
        control._capability_action(42, "pavel", "video-editor", "disable")
    assert config.read_text() == original
    assert len(calls) == 2
    job = json.loads(next((root / "jobs").glob("capability-*.json")).read_text())
    assert job["state"] == "rolled_back"
    assert job["error_code"] == "restart_failed"
    assert not (root / "desired/pavel.json").exists()


def test_capability_action_blocks_busy_before_config_mutation(monkeypatch, tmp_path):
    root = _action_store(monkeypatch, tmp_path)
    _, _, config = _action_profile(monkeypatch, tmp_path)
    original = config.read_text()
    monkeypatch.setattr(control, "_health", lambda _profile: {"healthy": True, "active_agents": 1})
    monkeypatch.setattr(control, "_restart", lambda _profile: (_ for _ in ()).throw(AssertionError("restart must not run")))
    with pytest.raises(control.ControlError, match="profile_busy"):
        control._capability_action(42, "pavel", "web-search", "disable")
    assert config.read_text() == original
    job = json.loads(next((root / "jobs").glob("capability-*.json")).read_text())
    assert job["state"] == "blocked"
    assert job["error_code"] == "profile_busy"


def test_capability_action_rejects_connection_consent_and_planned_targets(monkeypatch, tmp_path):
    root = _action_store(monkeypatch, tmp_path)
    _action_profile(monkeypatch, tmp_path)
    for capability, error in (
        ("maton", "capability_requires_connection"),
        ("telegram-secretary", "capability_requires_consent"),
        ("finance", "capability_planned"),
    ):
        with pytest.raises(control.ControlError, match=error):
            control._capability_action(42, "pavel", capability, "enable")
    jobs = [json.loads(path.read_text()) for path in (root / "jobs").glob("capability-*.json")]
    assert len(jobs) == 3
    assert {item["state"] for item in jobs} == {"rejected"}
    assert {item["error_code"] for item in jobs} == {
        "capability_requires_connection", "capability_requires_consent", "capability_planned",
    }


def test_video_state_respects_video_editor_disabled_toolset(monkeypatch, tmp_path):
    entry, env, config = _action_profile(monkeypatch, tmp_path)
    payload = yaml.safe_load(config.read_text())
    payload["agent"]["disabled_toolsets"] = ["video_editor"]
    config.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    rows = {item["id"]: item for item in control._capability_states("pavel")["items"]}
    assert rows["video-editor"]["installed"] is True
    assert rows["video-editor"]["enabled"] is False
    assert rows["video-editor"]["health"] == "disabled"
    assert rows["video-editor"]["reason"] == "disabled"


def test_control_unit_has_private_persistent_action_state_directory():
    unit = (MODULE.parent / "proai-hermes-forge-control.service").read_text()
    assert "StateDirectory=proai-hermes-forge" in unit
    assert "StateDirectoryMode=0700" in unit


def test_narrow_toolset_patch_preserves_every_other_yaml_byte():
    original = (
        'model:\n  default: "gpt-test" # keep this comment\n'
        'agent:\n  max_turns: 60\n  disabled_toolsets:\n'
        '  - browser-cdp\n  - terminal\n  verbose: false\n'
        "custom:\n  quoted: 'Keep Me'\n"
    )
    disabled, changed = control._patch_disabled_toolset_text(original, "web", False)
    assert changed is True
    assert disabled == original.replace("  verbose: false\n", "  - web\n  verbose: false\n")
    enabled, changed_back = control._patch_disabled_toolset_text(disabled, "web", True)
    assert changed_back is True
    assert enabled == original


def test_narrow_toolset_patch_supports_canonical_empty_inline_list():
    original = "agent:\n  disabled_toolsets: [] # keep\n  verbose: false\n"
    disabled, changed = control._patch_disabled_toolset_text(original, "web", False)
    assert changed is True
    assert disabled == "agent:\n  disabled_toolsets: # keep\n  - web\n  verbose: false\n"
    assert yaml.safe_load(disabled)["agent"]["disabled_toolsets"] == ["web"]
    restored, restored_changed = control._patch_disabled_toolset_text(disabled, "web", True)
    assert restored_changed is True
    assert restored == original


def test_narrow_toolset_patch_rejects_inline_or_ambiguous_yaml():
    inline = "agent:\n  disabled_toolsets: [web, terminal]\n"
    with pytest.raises(control.ControlError, match="config_format_unsupported"):
        control._patch_disabled_toolset_text(inline, "web", True)
    duplicate = "agent:\n  disabled_toolsets:\n  - web\n  disabled_toolsets:\n  - terminal\n"
    with pytest.raises(control.ControlError, match="config_format_unsupported"):
        control._patch_disabled_toolset_text(duplicate, "web", True)
