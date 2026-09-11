from __future__ import annotations

import copy
import importlib.util
import json
import shutil
from pathlib import Path

import jsonschema
import pytest
import yaml

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = MODULE_ROOT.parents[1]
SPEC = importlib.util.spec_from_file_location("forge_kitchen", MODULE_ROOT / "kitchen.py")
kitchen = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(kitchen)


def test_personal_plan_is_deterministic_and_includes_required_capabilities():
    first = kitchen.compile_plan("personal-hermes", ["maton", "image-studio"])
    second = kitchen.compile_plan("personal-hermes", ["image-studio", "maton"])
    assert first == second
    assert first["schema"] == "hermes.kitchen-plan/v1"
    assert first["selected_optional"] == ["image-studio", "maton"]
    rows = {item["id"]: item for item in first["capabilities"]}
    assert set(rows) == {"telegram-secretary", "web-search", "maton", "image-studio"}
    assert rows["telegram-secretary"]["required"] is True
    assert rows["web-search"]["required"] is True
    assert rows["maton"]["required"] is False
    assert rows["image-studio"]["required"] is False
    assert first["effective_runtime"] == ">=0.21,<0.22"
    assert len(first["catalog_sha256"]) == 64
    assert len(first["plan_sha256"]) == 64
    kitchen.verify_plan(first)


def test_internal_plan_matches_json_schema():
    plan = kitchen.compile_plan("personal-hermes", ["video-editor"])
    schema = json.loads(
        (REPO_ROOT / "server/forge/schemas/kitchen-plan-v1.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(plan)


def test_public_preview_redacts_internal_execution_and_secret_names():
    plan = kitchen.compile_plan("personal-hermes", ["maton", "image-studio"])
    preview = kitchen.public_preview(plan)
    assert preview["schema"] == "hermes.kitchen-preview/v1"
    assert preview["plan_sha256"] == plan["plan_sha256"]
    maton = next(item for item in preview["capabilities"] if item["id"] == "maton")
    assert maton["connection"] == {
        "mode": "personal_secret",
        "secret_required": True,
        "oauth_scopes": [],
    }
    serialized = json.dumps(preview, sort_keys=True)
    for forbidden in (
        "MCP_MATON_API_KEY",
        "secret_names",
        "ensure-maton",
        "disable-maton",
        "maton-connections",
        "health_operation",
        '"execution"',
    ):
        assert forbidden not in serialized


def test_optional_selection_must_be_declared_and_unique():
    with pytest.raises(kitchen.KitchenError, match="optional_capability_not_declared:github"):
        kitchen.compile_plan("personal-hermes", ["github"])
    with pytest.raises(kitchen.KitchenError, match="optional_capability_duplicate"):
        kitchen.compile_plan("personal-hermes", ["maton", "maton"])
    with pytest.raises(kitchen.KitchenError, match="optional_capability_id_invalid"):
        kitchen.compile_plan("personal-hermes", ["../maton"])


def test_unknown_agent_fails_closed():
    with pytest.raises(kitchen.KitchenError, match="agent_not_found"):
        kitchen.compile_plan("missing-agent")


def test_plan_digest_detects_tampering():
    plan = kitchen.compile_plan("personal-hermes", ["maton"])
    tampered = copy.deepcopy(plan)
    tampered["models"]["primary"] = "platform/other-model"
    with pytest.raises(kitchen.KitchenError, match="plan_digest_mismatch"):
        kitchen.verify_plan(tampered)


def _copy_forge(tmp_path: Path) -> Path:
    root = tmp_path / "forge"
    shutil.copytree(REPO_ROOT / "server/forge", root)
    return root


def test_runtime_contract_conflict_fails_closed(tmp_path):
    root = _copy_forge(tmp_path)
    path = root / "catalog/capabilities/image-studio.yaml"
    payload = yaml.safe_load(path.read_text())
    payload["runtime"] = ">=0.20,<0.21"
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    with pytest.raises(kitchen.KitchenError, match="runtime_contract_conflict"):
        kitchen.compile_plan("personal-hermes", ["image-studio"], root=root)


def test_planned_capability_cannot_be_smuggled_into_package(tmp_path):
    root = _copy_forge(tmp_path)
    path = root / "agent-packages/personal-hermes/agent.yaml"
    payload = yaml.safe_load(path.read_text())
    payload["capabilities"]["optional"].append("github")
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    with pytest.raises(kitchen.KitchenError, match="catalog_invalid:capability_reference_not_available:github"):
        kitchen.compile_plan("personal-hermes", ["github"], root=root)


def test_catalog_change_changes_plan_digest_even_when_selection_is_same(tmp_path):
    root = _copy_forge(tmp_path)
    before = kitchen.compile_plan("personal-hermes", [], root=root)
    path = root / "catalog/capabilities/finance.yaml"
    payload = yaml.safe_load(path.read_text())
    payload["summary"] = "Planned finance capability with a changed catalog description."
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    after = kitchen.compile_plan("personal-hermes", [], root=root)
    assert before["catalog_sha256"] != after["catalog_sha256"]
    assert before["plan_sha256"] != after["plan_sha256"]


def test_package_version_change_changes_plan_digest(tmp_path):
    root = _copy_forge(tmp_path)
    before = kitchen.compile_plan("personal-hermes", [], root=root)
    path = root / "agent-packages/personal-hermes/agent.yaml"
    payload = yaml.safe_load(path.read_text())
    payload["version"] = "1.0.1"
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    after = kitchen.compile_plan("personal-hermes", [], root=root)
    assert before["plan_sha256"] != after["plan_sha256"]
    assert after["agent"]["version"] == "1.0.1"


def test_internal_plan_keeps_bounded_execution_contract_for_future_apply():
    plan = kitchen.compile_plan("personal-hermes", ["maton"])
    maton = next(item for item in plan["capabilities"] if item["id"] == "maton")
    assert maton["connection"]["secret_names"] == ["MCP_MATON_API_KEY"]
    assert maton["execution"] == {
        "install": "ensure-maton",
        "uninstall": "disable-maton",
        "health_operation": "maton-connections",
    }


def test_kitchen_source_has_no_execution_or_network_surface():
    source = (MODULE_ROOT / "kitchen.py").read_text(encoding="utf-8")
    for forbidden in (
        "import subprocess",
        "from subprocess",
        "import socket",
        "import requests",
        "urllib.request",
        "systemctl",
        "Popen(",
        "os.system(",
    ):
        assert forbidden not in source


def test_installed_catalog_document_compiles_same_plan_as_source_catalog():
    catalog = kitchen._catalog_module()
    payload = catalog.public_catalog()
    digest = catalog.catalog_digest()
    installed = kitchen.compile_catalog_document(
        payload, digest, "personal-hermes", ["maton", "video-editor"]
    )
    source = kitchen.compile_plan("personal-hermes", ["video-editor", "maton"])
    assert installed == source


def test_installed_catalog_digest_and_shape_fail_closed():
    catalog = kitchen._catalog_module()
    payload = catalog.public_catalog()
    digest = catalog.catalog_digest()
    tampered = copy.deepcopy(payload)
    tampered["agents"][0]["summary"] = "tampered"
    with pytest.raises(kitchen.KitchenError, match="catalog_digest_mismatch"):
        kitchen.compile_catalog_document(tampered, digest, "personal-hermes")

    duplicate = copy.deepcopy(payload)
    duplicate["agents"].append(copy.deepcopy(duplicate["agents"][0]))
    duplicate_digest = kitchen._sha256(duplicate)
    with pytest.raises(kitchen.KitchenError, match="catalog_document_invalid"):
        kitchen.compile_catalog_document(duplicate, duplicate_digest, "personal-hermes")
