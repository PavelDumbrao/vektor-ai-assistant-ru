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


def test_catalog_is_public_but_tenant_routes_remain_protected(monkeypatch, tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"schema":"hermes.catalog/v1","capabilities":[],"agents":[]}', encoding="utf-8")
    monkeypatch.setattr(api, "CATALOG_PATH", catalog)
    status, result = api.route("GET", "/v1/catalog", {}, {})
    assert status == 200
    assert result["schema"] == "hermes.catalog.public/v1"
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


def test_catalog_route_is_public_read_only_and_sanitized(monkeypatch, tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        '{"schema":"hermes.catalog/v1","capabilities":[{"id":"maton","name":"Maton","summary":"External services","version":"1.0.0","publisher":"Hermes Official","trust_tier":"official","kind":"mcp","availability":"available","connection":{"mode":"personal_secret","secret_names":["MCP_MATON_API_KEY"]},"permissions":{"action_default":"approval"},"metering":"provider_usage","provision":{"install":"ensure-maton"},"health":{"operation":"maton-connections"}}],"agents":[{"id":"personal-hermes","name":"Personal Hermes","summary":"Personal employee","version":"1.0.0","publisher":"Hermes Official","role":"personal-assistant","capabilities":{"required":["web-search"],"optional":["maton"]},"permissions":{"secret.manage":"owner_only"}}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(api, "CATALOG_PATH", catalog)
    monkeypatch.setattr(api, "call_control", lambda payload: (_ for _ in ()).throw(AssertionError("catalog must not use root control")))
    status, result = api.route("GET", "/v1/catalog", {}, {})
    assert status == 200
    assert result["schema"] == "hermes.catalog.public/v1"
    assert result["capabilities"][0]["id"] == "maton"
    assert result["capabilities"][0]["connection_mode"] == "personal_secret"
    assert result["agents"][0]["required_capabilities"] == ["web-search"]
    serialized = __import__("json").dumps(result)
    for forbidden in ("secret_names", "MCP_MATON_API_KEY", "provision", "ensure-maton", "health", "secret.manage"):
        assert forbidden not in serialized


def test_catalog_rejects_invalid_or_oversized_payload(monkeypatch, tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"schema":"wrong","capabilities":[],"agents":[]}', encoding="utf-8")
    monkeypatch.setattr(api, "CATALOG_PATH", catalog)
    with pytest.raises(api.ApiError, match="catalog_invalid"):
        api.route("GET", "/v1/catalog", {}, {})
    catalog.write_text("x" * 256, encoding="utf-8")
    monkeypatch.setattr(api, "MAX_CATALOG_BYTES", 128)
    with pytest.raises(api.ApiError, match="catalog_invalid"):
        api.route("GET", "/v1/catalog", {}, {})


def test_catalog_ui_exposes_read_only_tools_and_employees_tabs():
    root = MODULE.parent
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert 'data-tab="tools"' in html
    assert 'data-tab="employees"' in html
    assert 'id="tools-list"' in html
    assert 'id="employees-list"' in html
    assert 'request("/v1/catalog")' in js
    assert '/v1/catalog/install' not in js
    assert '/v1/catalog/enable' not in js
    assert 'install-capability' not in html
    assert 'enable-capability' not in html
