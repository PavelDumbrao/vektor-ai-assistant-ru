from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "api.py"
spec = importlib.util.spec_from_file_location("forge_api", MODULE)
api = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(api)


def _write_installed_catalog(tmp_path: Path) -> Path:
    catalog = api.KITCHEN._catalog_module()
    payload = catalog.public_catalog()
    digest = catalog.catalog_digest()
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "catalog.sha256").write_text(digest + "\n", encoding="utf-8")
    return path


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
    assert 'data-tab="kitchen"' in html
    assert 'data-panel="kitchen"' in html
    assert 'id="tools-list"' in html
    assert 'id="employees-list"' in html
    assert 'id="kitchen-preview-button"' in html
    assert 'request("/v1/catalog")' in js
    assert 'request("/v1/kitchen/preview"' in js
    assert '/v1/catalog/install' not in js
    assert '/v1/catalog/enable' not in js
    assert 'install-capability' not in html
    assert 'enable-capability' not in html


def test_kitchen_ui_is_preview_only_safe_and_available_without_existing_hermes():
    root = MODULE.parent
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert "Это только рецепт" in html
    assert "Preview only" in html
    assert 'dataset.kitchenAgent=item.id' in js
    assert 'dataset.kitchenCapability=id' in js
    assert 'optional_capabilities:[...kitchenOptional].sort()' in js
    assert 'renderKitchenPreview(data)' in js
    assert "innerHTML" not in js
    assert "/v1/kitchen/apply" not in js
    assert "/v1/kitchen/install" not in js
    assert "kitchen-apply" not in html
    assert "kitchen-install" not in html
    handler = js.index('const kitchenAgentButton=event.target.closest("button[data-kitchen-agent]")')
    preview = js.index('const kitchenPreviewButton=event.target.closest("#kitchen-preview-button")')
    current_guard = js.index("if (!current) return;", handler)
    assert handler < current_guard
    assert preview < current_guard


def test_kitchen_ui_preview_does_not_render_internal_contract_names():
    js = (MODULE.parent / "static" / "app.js").read_text(encoding="utf-8")
    kitchen_start = js.index("function renderKitchenPreview")
    kitchen_end = js.index("async function loadKitchenPreview", kitchen_start)
    renderer = js[kitchen_start:kitchen_end]
    for forbidden in (
        "secret_names", "health_operation", "execution",
        "ensure-maton", "disable-maton", "MCP_MATON_API_KEY",
    ):
        assert forbidden not in renderer


def test_capabilities_route_is_authenticated_and_forwards_owner_scope(monkeypatch):
    seen = []
    monkeypatch.setattr(api, "call_control", lambda payload: seen.append(payload) or {"schema": "hermes.capability-state/v1", "items": []})
    headers = {"Authorization": "Bearer opaque-session"}
    status, result = api.route("GET", "/v1/hermes/pavel/capabilities", {}, headers)
    assert status == 200
    assert result["schema"] == "hermes.capability-state/v1"
    assert seen == [{"op": "list_capabilities", "session": "opaque-session", "profile": "pavel"}]
    with pytest.raises(api.ApiError, match="session_required"):
        api.route("GET", "/v1/hermes/pavel/capabilities", {}, {})


def test_tools_ui_exposes_only_bounded_existing_capability_actions():
    app = (MODULE.parent / "static/app.js").read_text(encoding="utf-8")
    assert "loadCapabilityStates" in app
    assert "capabilityPresentation" in app
    assert 'new Set(["image-studio","video-editor","web-search"])' in app
    assert "capabilities/${id}/${action}" in app
    assert "capabilities/install" not in app
    assert "capabilities/uninstall" not in app
    assert 'dataset.capabilitySettings="maton"' in app


def test_capability_action_route_forwards_only_bounded_action(monkeypatch):
    seen = []
    monkeypatch.setattr(api, "call_control", lambda payload: seen.append(payload) or {"action_id": "a" * 32, "outcome": "success"})
    headers = {"Authorization": "Bearer opaque-session"}
    status, result = api.route("POST", "/v1/hermes/pavel/capabilities/web-search/disable", {}, headers)
    assert status == 200
    assert result["outcome"] == "success"
    assert seen == [{
        "op": "capability_action", "session": "opaque-session", "profile": "pavel",
        "capability_id": "web-search", "action": "disable",
    }]
    with pytest.raises(api.ApiError, match="not_found"):
        api.route("POST", "/v1/hermes/pavel/capabilities/web-search/install", {}, headers)


def test_kitchen_preview_is_public_read_only_and_redacted(monkeypatch, tmp_path):
    catalog = _write_installed_catalog(tmp_path)
    monkeypatch.setattr(api, "CATALOG_PATH", catalog)
    monkeypatch.setattr(
        api, "call_control",
        lambda payload: (_ for _ in ()).throw(AssertionError("preview must not use root control")),
    )
    status, result = api.route(
        "POST", "/v1/kitchen/preview",
        {"agent_id": "personal-hermes", "optional_capabilities": ["maton", "image-studio"]},
        {},
    )
    assert status == 200
    assert result["schema"] == "hermes.kitchen-preview/v1"
    assert result["agent"]["id"] == "personal-hermes"
    assert result["selected_optional"] == ["image-studio", "maton"]
    assert len(result["catalog_sha256"]) == 64
    assert len(result["plan_sha256"]) == 64
    serialized = json.dumps(result, sort_keys=True)
    for forbidden in (
        "MCP_MATON_API_KEY", "secret_names", "ensure-maton",
        "disable-maton", "maton-connections", "health_operation", '"execution"',
    ):
        assert forbidden not in serialized


def test_kitchen_preview_is_public_but_body_is_bounded(monkeypatch, tmp_path):
    catalog = _write_installed_catalog(tmp_path)
    monkeypatch.setattr(api, "CATALOG_PATH", catalog)
    status, result = api.route(
        "POST", "/v1/kitchen/preview", {"agent_id": "personal-hermes"}, {}
    )
    assert status == 200
    assert result["agent"]["id"] == "personal-hermes"
    with pytest.raises(api.ApiError, match="kitchen_request_invalid"):
        api.route(
            "POST", "/v1/kitchen/preview",
            {"agent_id": "personal-hermes", "command": "install"}, {},
        )
    with pytest.raises(api.ApiError, match="optional_capability_not_declared:github") as exc:
        api.route(
            "POST", "/v1/kitchen/preview",
            {"agent_id": "personal-hermes", "optional_capabilities": ["github"]}, {},
        )
    assert exc.value.status == 400


def test_kitchen_preview_rejects_catalog_digest_drift(monkeypatch, tmp_path):
    catalog = _write_installed_catalog(tmp_path)
    (tmp_path / "catalog.sha256").write_text("0" * 64 + "\n", encoding="utf-8")
    monkeypatch.setattr(api, "CATALOG_PATH", catalog)
    with pytest.raises(api.ApiError, match="catalog_digest_mismatch") as exc:
        api.route(
            "POST", "/v1/kitchen/preview",
            {"agent_id": "personal-hermes", "optional_capabilities": []}, {},
        )
    assert exc.value.status == 503


def test_forge_api_installer_bundles_shared_kitchen_compiler():
    install_path = MODULE.parent / "install.py"
    spec = importlib.util.spec_from_file_location("forge_api_install", install_path)
    installer = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(installer)
    assert installer.KITCHEN_SOURCE == MODULE.parent.parent / "hermes-forge-kitchen" / "kitchen.py"
    assert installer.KITCHEN_SOURCE.is_file()
    source = install_path.read_text(encoding="utf-8")
    assert 'TARGET / "kitchen.py"' in source


def test_deployed_layout_loads_bundled_kitchen_without_repo_dependency(monkeypatch, tmp_path):
    catalog = _write_installed_catalog(tmp_path)
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    shutil.copy2(MODULE, deployed / "api.py")
    shutil.copy2(
        MODULE.parent.parent / "hermes-forge-kitchen" / "kitchen.py",
        deployed / "kitchen.py",
    )
    monkeypatch.setenv("FORGE_CATALOG_PATH", str(catalog))
    spec = importlib.util.spec_from_file_location("forge_api_deployed_layout", deployed / "api.py")
    deployed_api = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(deployed_api)
    status, result = deployed_api.route(
        "POST", "/v1/kitchen/preview",
        {"agent_id": "personal-hermes", "optional_capabilities": ["video-editor"]}, {},
    )
    assert status == 200
    assert result["schema"] == "hermes.kitchen-preview/v1"
    assert result["selected_optional"] == ["video-editor"]
