#!/usr/bin/env python3
"""Strict loader for Hermes Forge capability and Agent Package manifests."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = REPO_ROOT / "server" / "forge"
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
RUNTIME_RE = re.compile(r"^>=0\.[0-9]+,<0\.[0-9]+$")
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{1,79}$")
ACTION_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")
SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
SECRET_VALUE_RE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{12,}|\bgh[pousr]_[A-Za-z0-9]{20,}|"
    r"\b[0-9]{6,}:[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{16,})"
)

class CatalogError(ValueError):
    pass
CAPABILITY_KEYS = {
    "schema", "id", "name", "version", "publisher", "trust_tier",
    "availability", "kind", "summary", "runtime", "connection",
    "permissions", "health", "provision", "retention", "metering",
}
CONNECTION_KEYS = {"mode", "secret_names", "oauth_scopes"}
PERMISSION_KEYS = {"network", "filesystem", "action_default"}
HEALTH_KEYS = {"operation"}
PROVISION_KEYS = {"install", "uninstall"}
AGENT_KEYS = {
    "schema", "id", "name", "version", "publisher", "role", "summary",
    "runtime", "models", "memory", "capabilities", "permissions",
    "health", "onboarding",
}
MODEL_KEYS = {"primary", "fallback"}
MEMORY_KEYS = {"passive_secretary"}
CAPABILITY_REF_KEYS = {"required", "optional"}
AGENT_HEALTH_KEYS = {"suite"}
ONBOARDING_KEYS = {"questions"}

TRUST_TIERS = {"official", "verified", "community"}
AVAILABILITY = {"available", "planned", "deprecated"}
KINDS = {"builtin", "connector", "mcp", "plugin", "shared_tool"}
CONNECTION_MODES = {"none", "personal_secret", "platform", "oauth", "telegram"}
NETWORK_PERMISSIONS = {"none", "platform", "external_api"}
FILESYSTEM_PERMISSIONS = {"none", "tenant_config", "tenant_workspace", "shared_runtime"}
ACTION_POLICIES = {"observe", "approval", "autonomous", "owner_only", "disabled"}
RETENTION = {"none", "tenant_private", "platform_aggregate"}
METERING = {"none", "provider_usage", "platform_usage"}
HEALTH_OPERATIONS = {
    "none", "maton-connections", "telegram-secretary-ready",
    "web-search-ready", "image-studio-ready", "video-editor-ready",
}
INSTALL_OPERATIONS = {
    "none", "ensure-maton", "ensure-telegram-secretary", "noop",
    "ensure-image-studio", "ensure-video-editor",
}
UNINSTALL_OPERATIONS = {
    "none", "disable-maton", "disable-telegram-secretary",
    "disable-image-studio", "disable-video-editor",
}
HEALTH_SUITES = {"personal-hermes-smoke-v1"}


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogError(f"{label}_must_be_mapping")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise CatalogError(f"{label}_fields_invalid")


def _string(value: Any, label: str, *, minimum: int = 1, maximum: int = 240) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise CatalogError(f"{label}_invalid")
    if "\n" in value or "\r" in value:
        raise CatalogError(f"{label}_invalid")
    return value


def _enum(value: Any, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise CatalogError(f"{label}_invalid")
    return value


def _string_list(value: Any, label: str, pattern: re.Pattern[str], *, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise CatalogError(f"{label}_invalid")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not pattern.fullmatch(item):
            raise CatalogError(f"{label}_invalid")
        result.append(item)
    if len(set(result)) != len(result):
        raise CatalogError(f"{label}_duplicate")
    return result


def _reject_sensitive_literals(value: Any) -> None:
    if isinstance(value, str) and SECRET_VALUE_RE.search(value):
        raise CatalogError("sensitive_literal_forbidden")
    if isinstance(value, dict):
        for item in value.values():
            _reject_sensitive_literals(item)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_literals(item)


def _load_yaml(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CatalogError("manifest_path_invalid")
    if path.stat().st_size > 64 * 1024:
        raise CatalogError("manifest_too_large")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    result = _mapping(payload, "manifest")
    _reject_sensitive_literals(result)
    return result


def _runtime(value: Any) -> str:
    text = _string(value, "runtime", maximum=32)
    if not RUNTIME_RE.fullmatch(text):
        raise CatalogError("runtime_invalid")
    return text


def validate_capability(payload: dict[str, Any]) -> dict[str, Any]:
    value = _mapping(payload, "capability")
    _exact_keys(value, CAPABILITY_KEYS, "capability")
    if value["schema"] != "hermes.capability/v1":
        raise CatalogError("capability_schema_invalid")
    if not ID_RE.fullmatch(_string(value["id"], "capability_id", maximum=64)):
        raise CatalogError("capability_id_invalid")
    if not SEMVER_RE.fullmatch(_string(value["version"], "capability_version", maximum=32)):
        raise CatalogError("capability_version_invalid")
    _string(value["name"], "capability_name", maximum=80)
    _string(value["publisher"], "capability_publisher", maximum=80)
    _string(value["summary"], "capability_summary", maximum=240)
    _enum(value["trust_tier"], TRUST_TIERS, "trust_tier")
    availability = _enum(value["availability"], AVAILABILITY, "availability")
    _enum(value["kind"], KINDS, "kind")
    _runtime(value["runtime"])

    connection = _mapping(value["connection"], "connection")
    _exact_keys(connection, CONNECTION_KEYS, "connection")
    mode = _enum(connection["mode"], CONNECTION_MODES, "connection_mode")
    names = _string_list(connection["secret_names"], "secret_names", SECRET_NAME_RE, maximum=8)
    scopes = connection["oauth_scopes"]
    if not isinstance(scopes, list) or len(scopes) > 24:
        raise CatalogError("oauth_scopes_invalid")
    if any(not isinstance(item, str) or not 1 <= len(item) <= 120 for item in scopes):
        raise CatalogError("oauth_scopes_invalid")
    if len(set(scopes)) != len(scopes):
        raise CatalogError("oauth_scopes_duplicate")
    if mode == "personal_secret" and not names:
        raise CatalogError("personal_secret_name_required")
    if mode != "personal_secret" and names:
        raise CatalogError("secret_names_not_allowed")
    if mode != "oauth" and scopes:
        raise CatalogError("oauth_scopes_not_allowed")

    permissions = _mapping(value["permissions"], "permissions")
    _exact_keys(permissions, PERMISSION_KEYS, "permissions")
    _enum(permissions["network"], NETWORK_PERMISSIONS, "network_permission")
    _enum(permissions["filesystem"], FILESYSTEM_PERMISSIONS, "filesystem_permission")
    _enum(permissions["action_default"], ACTION_POLICIES, "action_default")

    health = _mapping(value["health"], "health")
    _exact_keys(health, HEALTH_KEYS, "health")
    health_op = _enum(health["operation"], HEALTH_OPERATIONS, "health_operation")
    provision = _mapping(value["provision"], "provision")
    _exact_keys(provision, PROVISION_KEYS, "provision")
    install_op = _enum(provision["install"], INSTALL_OPERATIONS, "install_operation")
    uninstall_op = _enum(provision["uninstall"], UNINSTALL_OPERATIONS, "uninstall_operation")
    _enum(value["retention"], RETENTION, "retention")
    _enum(value["metering"], METERING, "metering")

    if availability == "planned":
        if install_op != "none" or uninstall_op != "none" or health_op != "none":
            raise CatalogError("planned_capability_must_be_inert")
    elif availability == "available" and health_op == "none":
        raise CatalogError("available_capability_health_required")
    return value


def validate_agent(payload: dict[str, Any]) -> dict[str, Any]:
    value = _mapping(payload, "agent")
    _exact_keys(value, AGENT_KEYS, "agent")
    if value["schema"] != "hermes.agent/v1":
        raise CatalogError("agent_schema_invalid")
    for key in ("id", "role"):
        if not ID_RE.fullmatch(_string(value[key], f"agent_{key}", maximum=64)):
            raise CatalogError(f"agent_{key}_invalid")
    if not SEMVER_RE.fullmatch(_string(value["version"], "agent_version", maximum=32)):
        raise CatalogError("agent_version_invalid")
    _string(value["name"], "agent_name", maximum=80)
    _string(value["publisher"], "agent_publisher", maximum=80)
    _string(value["summary"], "agent_summary", maximum=240)
    _runtime(value["runtime"])

    models = _mapping(value["models"], "models")
    _exact_keys(models, MODEL_KEYS, "models")
    for key in MODEL_KEYS:
        text = _string(models[key], f"model_{key}", maximum=80)
        if not MODEL_RE.fullmatch(text):
            raise CatalogError(f"model_{key}_invalid")

    memory = _mapping(value["memory"], "memory")
    _exact_keys(memory, MEMORY_KEYS, "memory")
    if not isinstance(memory["passive_secretary"], bool):
        raise CatalogError("memory_passive_secretary_invalid")

    refs = _mapping(value["capabilities"], "capabilities")
    _exact_keys(refs, CAPABILITY_REF_KEYS, "capabilities")
    required = _string_list(refs["required"], "required_capabilities", ID_RE, maximum=32)
    optional = _string_list(refs["optional"], "optional_capabilities", ID_RE, maximum=32)
    if set(required) & set(optional):
        raise CatalogError("capability_reference_overlap")
    policies = _mapping(value["permissions"], "agent_permissions")
    if len(policies) > 64:
        raise CatalogError("agent_permissions_too_many")
    for action, policy in policies.items():
        if not isinstance(action, str) or not ACTION_RE.fullmatch(action):
            raise CatalogError("agent_permission_action_invalid")
        _enum(policy, ACTION_POLICIES, "agent_permission_policy")

    health = _mapping(value["health"], "agent_health")
    _exact_keys(health, AGENT_HEALTH_KEYS, "agent_health")
    _enum(health["suite"], HEALTH_SUITES, "agent_health_suite")

    onboarding = _mapping(value["onboarding"], "onboarding")
    _exact_keys(onboarding, ONBOARDING_KEYS, "onboarding")
    _string_list(onboarding["questions"], "onboarding_questions", ACTION_RE, maximum=20)
    return value


def _manifest_files(directory: Path) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise CatalogError("catalog_directory_invalid")
    return sorted(path for path in directory.glob("*.yaml") if path.is_file() and not path.is_symlink())


def load_catalog(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    root = root.resolve()
    capability_dir = root / "catalog" / "capabilities"
    package_root = root / "agent-packages"
    capabilities: dict[str, dict[str, Any]] = {}
    for path in _manifest_files(capability_dir):
        manifest = validate_capability(_load_yaml(path))
        cap_id = manifest["id"]
        if path.stem != cap_id:
            raise CatalogError("capability_filename_mismatch")
        if cap_id in capabilities:
            raise CatalogError("capability_duplicate")
        capabilities[cap_id] = manifest

    if package_root.is_symlink() or not package_root.is_dir():
        raise CatalogError("agent_package_directory_invalid")
    agents: dict[str, dict[str, Any]] = {}
    for directory in sorted(path for path in package_root.iterdir() if path.is_dir() and not path.is_symlink()):
        manifest_path = directory / "agent.yaml"
        manifest = validate_agent(_load_yaml(manifest_path))
        agent_id = manifest["id"]
        if directory.name != agent_id:
            raise CatalogError("agent_directory_mismatch")
        if agent_id in agents:
            raise CatalogError("agent_duplicate")
        refs = manifest["capabilities"]["required"] + manifest["capabilities"]["optional"]
        for cap_id in refs:
            capability = capabilities.get(cap_id)
            if capability is None:
                raise CatalogError(f"capability_reference_missing:{cap_id}")
            if capability["availability"] != "available":
                raise CatalogError(f"capability_reference_not_available:{cap_id}")
        agents[agent_id] = manifest

    if not capabilities or not agents:
        raise CatalogError("catalog_empty")
    return {"schema": "hermes.catalog/v1", "capabilities": capabilities, "agents": agents}


def public_catalog(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    catalog = load_catalog(root)
    return {
        "schema": catalog["schema"],
        "capabilities": [catalog["capabilities"][key] for key in sorted(catalog["capabilities"])],
        "agents": [catalog["agents"][key] for key in sorted(catalog["agents"])],
    }


def catalog_json(root: Path = DEFAULT_ROOT) -> str:
    return json.dumps(public_catalog(root), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def catalog_digest(root: Path = DEFAULT_ROOT) -> str:
    return hashlib.sha256(catalog_json(root).encode("utf-8")).hexdigest()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "public"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    if args.action == "validate":
        data = load_catalog(args.root)
        print(json.dumps({
            "ok": True,
            "capabilities": len(data["capabilities"]),
            "agents": len(data["agents"]),
            "sha256": catalog_digest(args.root),
        }, sort_keys=True))
    else:
        print(json.dumps(public_catalog(args.root), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
