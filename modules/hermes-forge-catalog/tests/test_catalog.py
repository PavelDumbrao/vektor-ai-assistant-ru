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
SPEC = importlib.util.spec_from_file_location("forge_catalog", MODULE_ROOT / "catalog.py")
catalog = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(catalog)


def test_repository_catalog_is_valid_and_deterministic():
    first = catalog.load_catalog()
    second = catalog.load_catalog()
    assert first == second
    assert len(first["capabilities"]) == 8
    assert set(first["agents"]) == {"personal-hermes"}
    assert len(catalog.catalog_digest()) == 64


def test_json_schemas_accept_repository_manifests():
    capability_schema = json.loads((REPO_ROOT / "server/forge/schemas/capability-v1.schema.json").read_text())
    agent_schema = json.loads((REPO_ROOT / "server/forge/schemas/agent-package-v1.schema.json").read_text())
    for path in sorted((REPO_ROOT / "server/forge/catalog/capabilities").glob("*.yaml")):
        jsonschema.Draft202012Validator(capability_schema).validate(yaml.safe_load(path.read_text()))
    package = yaml.safe_load(
        (REPO_ROOT / "server/forge/agent-packages/personal-hermes/agent.yaml").read_text()
    )
    jsonschema.Draft202012Validator(agent_schema).validate(package)


def test_available_and_planned_capabilities_have_correct_execution_contracts():
    data = catalog.load_catalog()
    for capability in data["capabilities"].values():
        if capability["availability"] == "available":
            assert capability["health"]["operation"] != "none"
        if capability["availability"] == "planned":
            assert capability["health"]["operation"] == "none"
            assert capability["provision"] == {"install": "none", "uninstall": "none"}


def test_personal_package_references_only_available_capabilities():
    data = catalog.load_catalog()
    package = data["agents"]["personal-hermes"]
    refs = package["capabilities"]["required"] + package["capabilities"]["optional"]
    assert refs
    assert all(data["capabilities"][item]["availability"] == "available" for item in refs)


def test_unknown_fields_and_arbitrary_operations_fail_closed():
    source = catalog.load_catalog()["capabilities"]["maton"]
    extra = copy.deepcopy(source)
    extra["command"] = "echo forbidden"
    with pytest.raises(catalog.CatalogError, match="capability_fields_invalid"):
        catalog.validate_capability(extra)

    arbitrary = copy.deepcopy(source)
    arbitrary["provision"]["install"] = "run-arbitrary-shell"
    with pytest.raises(catalog.CatalogError, match="install_operation_invalid"):
        catalog.validate_capability(arbitrary)


def test_sensitive_literal_detector_rejects_values_without_blocking_secret_names():
    source = copy.deepcopy(catalog.load_catalog()["capabilities"]["maton"])
    assert source["connection"]["secret_names"] == ["MCP_MATON_API_KEY"]
    suspicious = "gh" + "p_" + ("a" * 24)
    source["summary"] = suspicious
    with pytest.raises(catalog.CatalogError, match="sensitive_literal_forbidden"):
        catalog._reject_sensitive_literals(source)


def test_planned_capability_cannot_become_executable():
    source = copy.deepcopy(catalog.load_catalog()["capabilities"]["github"])
    source["provision"]["install"] = "ensure-maton"
    with pytest.raises(catalog.CatalogError, match="planned_capability_must_be_inert"):
        catalog.validate_capability(source)


def test_agent_cannot_reference_planned_capability(tmp_path):
    root = tmp_path / "forge"
    shutil.copytree(REPO_ROOT / "server/forge", root)
    path = root / "agent-packages/personal-hermes/agent.yaml"
    payload = yaml.safe_load(path.read_text())
    payload["capabilities"]["optional"].append("github")
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    with pytest.raises(catalog.CatalogError, match="capability_reference_not_available:github"):
        catalog.load_catalog(root)


def test_public_catalog_is_list_based_and_contains_no_runtime_state():
    public = catalog.public_catalog()
    assert isinstance(public["capabilities"], list)
    assert isinstance(public["agents"], list)
    serialized = json.dumps(public, sort_keys=True)
    for forbidden in ("owner_telegram_id", "bot_id", "token_path", "memory_content"):
        assert forbidden not in serialized
    assert "MCP_MATON_API_KEY" in serialized
    assert "secret_names" in serialized
