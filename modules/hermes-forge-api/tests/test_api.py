from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "api.py"
spec = importlib.util.spec_from_file_location("forge_api", MODULE)
api = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(api)


def test_auth_route_forwards_only_init_data(monkeypatch):
    seen = []
    monkeypatch.setattr(api, "call_control", lambda payload: seen.append(payload) or {"session": "opaque"})
    status, result = api.route("POST", "/v1/auth/telegram", {"init_data": "signed"}, {})
    assert status == 200
    assert result["session"] == "opaque"
    assert seen == [{"op": "authenticate", "init_data": "signed"}]


def test_protected_route_requires_session():
    with pytest.raises(api.ApiError, match="session_required"):
        api.route("GET", "/v1/hermes", {}, {})


def test_secret_route_forwards_value_to_control_but_never_logs(monkeypatch):
    seen = []
    monkeypatch.setattr(api, "call_control", lambda payload: seen.append(payload) or {"secret": {"configured": True, "last4": "7890"}})
    headers = {"Authorization": "Bearer opaque-session"}
    status, result = api.route("PUT", "/v1/hermes/pavel/secrets/MCP_MATON_API_KEY", {"value": "maton-test-secret-1234567890"}, headers)
    assert status == 200
    assert seen[0]["op"] == "set_secret"
    assert seen[0]["profile"] == "pavel"
    assert seen[0]["session"] == "opaque-session"
    assert result == {"secret": {"configured": True, "last4": "7890"}}


def test_unknown_route_fails_closed(monkeypatch):
    monkeypatch.setattr(api, "call_control", lambda payload: {})
    with pytest.raises(api.ApiError, match="not_found"):
        api.route("POST", "/v1/hermes/pavel/shell", {}, {"Authorization": "Bearer x"})


def test_public_server_refuses_root(monkeypatch):
    monkeypatch.setattr(api.os, "geteuid", lambda: 0)
    with pytest.raises(SystemExit, match="refusing"):
        api.serve()
