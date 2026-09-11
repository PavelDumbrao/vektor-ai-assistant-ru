#!/usr/bin/env python3
"""Compile Hermes Forge Agent Packages into deterministic, non-mutating plans."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FORGE_ROOT = REPO_ROOT / "server" / "forge"
CATALOG_SOURCE = REPO_ROOT / "modules" / "hermes-forge-catalog" / "catalog.py"
PLAN_SCHEMA = "hermes.kitchen-plan/v1"
PREVIEW_SCHEMA = "hermes.kitchen-preview/v1"
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
RUNTIME_RE = re.compile(r"^>=0\.([0-9]+),<0\.([0-9]+)$")


class KitchenError(ValueError):
    """Raised when a Kitchen request cannot compile deterministically."""


def _load_catalog_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hermes_forge_catalog", CATALOG_SOURCE)
    if spec is None or spec.loader is None:
        raise KitchenError("catalog_loader_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CATALOG = _load_catalog_module()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _runtime_interval(value: str) -> tuple[int, int]:
    match = RUNTIME_RE.fullmatch(value)
    if match is None:
        raise KitchenError("runtime_contract_invalid")
    lower, upper = (int(match.group(1)), int(match.group(2)))
    if lower >= upper:
        raise KitchenError("runtime_contract_invalid")
    return lower, upper


def _effective_runtime(values: Iterable[str]) -> str:
    intervals = [_runtime_interval(value) for value in values]
    if not intervals:
        raise KitchenError("runtime_contract_missing")
    lower = max(item[0] for item in intervals)
    upper = min(item[1] for item in intervals)
    if lower >= upper:
        raise KitchenError("runtime_contract_conflict")
    return f">=0.{lower},<0.{upper}"


def _normalize_selection(values: Iterable[str]) -> list[str]:
    result = list(values)
    if any(not isinstance(item, str) or not ID_RE.fullmatch(item) for item in result):
        raise KitchenError("optional_capability_id_invalid")
    if len(set(result)) != len(result):
        raise KitchenError("optional_capability_duplicate")
    return sorted(result)


def _capability_plan(capability: dict[str, Any], *, required: bool) -> dict[str, Any]:
    connection = capability["connection"]
    provision = capability["provision"]
    return {
        "id": capability["id"],
        "name": capability["name"],
        "version": capability["version"],
        "publisher": capability["publisher"],
        "trust_tier": capability["trust_tier"],
        "required": required,
        "kind": capability["kind"],
        "summary": capability["summary"],
        "runtime": capability["runtime"],
        "connection": {
            "mode": connection["mode"],
            "secret_names": list(connection["secret_names"]),
            "oauth_scopes": list(connection["oauth_scopes"]),
        },
        "permissions": dict(capability["permissions"]),
        "execution": {
            "install": provision["install"],
            "uninstall": provision["uninstall"],
            "health_operation": capability["health"]["operation"],
        },
        "retention": capability["retention"],
        "metering": capability["metering"],
    }


def compile_plan(
    agent_id: str,
    selected_optional: Iterable[str] = (),
    *,
    root: Path = DEFAULT_FORGE_ROOT,
) -> dict[str, Any]:
    if not isinstance(agent_id, str) or not ID_RE.fullmatch(agent_id):
        raise KitchenError("agent_id_invalid")
    catalog = CATALOG.load_catalog(root)
    package = catalog["agents"].get(agent_id)
    if package is None:
        raise KitchenError("agent_not_found")

    selected = _normalize_selection(selected_optional)
    required_ids = sorted(package["capabilities"]["required"])
    optional_ids = set(package["capabilities"]["optional"])
    undeclared = [item for item in selected if item not in optional_ids]
    if undeclared:
        raise KitchenError(f"optional_capability_not_declared:{undeclared[0]}")

    chosen_ids = sorted(set(required_ids) | set(selected))
    rows: list[dict[str, Any]] = []
    runtime_contracts = [package["runtime"]]
    for capability_id in chosen_ids:
        capability = catalog["capabilities"].get(capability_id)
        if capability is None:
            raise KitchenError(f"capability_missing:{capability_id}")
        if capability["availability"] != "available":
            raise KitchenError(f"capability_not_available:{capability_id}")
        runtime_contracts.append(capability["runtime"])
        rows.append(
            _capability_plan(
                capability,
                required=capability_id in set(required_ids),
            )
        )

    core: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "catalog_sha256": CATALOG.catalog_digest(root),
        "agent": {
            key: package[key]
            for key in ("id", "name", "version", "publisher", "role", "summary")
        },
        "effective_runtime": _effective_runtime(runtime_contracts),
        "models": dict(package["models"]),
        "memory": dict(package["memory"]),
        "selected_optional": selected,
        "capabilities": rows,
        "permissions": dict(package["permissions"]),
        "health": dict(package["health"]),
        "onboarding": {"questions": list(package["onboarding"]["questions"])},
    }
    return {**core, "plan_sha256": _sha256(core)}


def verify_plan(plan: dict[str, Any]) -> None:
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise KitchenError("plan_schema_invalid")
    digest = plan.get("plan_sha256")
    catalog_digest = plan.get("catalog_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise KitchenError("plan_digest_invalid")
    if not isinstance(catalog_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", catalog_digest
    ):
        raise KitchenError("catalog_digest_invalid")
    core = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if _sha256(core) != digest:
        raise KitchenError("plan_digest_mismatch")
    capabilities = plan.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        raise KitchenError("plan_capabilities_invalid")


def public_preview(plan: dict[str, Any]) -> dict[str, Any]:
    verify_plan(plan)
    public_capabilities: list[dict[str, Any]] = []
    for item in plan["capabilities"]:
        connection = item["connection"]
        public_capabilities.append(
            {
                key: item[key]
                for key in (
                    "id", "name", "version", "publisher", "trust_tier",
                    "required", "kind", "summary", "permissions",
                    "retention", "metering",
                )
            }
        )
        public_capabilities[-1]["connection"] = {
            "mode": connection["mode"],
            "secret_required": bool(connection["secret_names"]),
            "oauth_scopes": list(connection["oauth_scopes"]),
        }

    return {
        "schema": PREVIEW_SCHEMA,
        "catalog_sha256": plan["catalog_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "agent": dict(plan["agent"]),
        "effective_runtime": plan["effective_runtime"],
        "models": dict(plan["models"]),
        "memory": dict(plan["memory"]),
        "selected_optional": list(plan["selected_optional"]),
        "capabilities": public_capabilities,
        "permissions": dict(plan["permissions"]),
        "health_suite": plan["health"]["suite"],
        "onboarding_questions": list(plan["onboarding"]["questions"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "preview"))
    parser.add_argument("--agent", required=True)
    parser.add_argument(
        "--with-capability",
        action="append",
        default=[],
        dest="optional_capabilities",
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_FORGE_ROOT)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        plan = compile_plan(
            args.agent,
            args.optional_capabilities,
            root=args.root,
        )
    except (KitchenError, CATALOG.CatalogError) as exc:
        parser.error(str(exc))
    payload = plan if args.action == "plan" else public_preview(plan)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
