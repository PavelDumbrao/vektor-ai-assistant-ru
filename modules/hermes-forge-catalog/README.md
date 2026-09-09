# Hermes Forge Catalog v1

This module validates and installs the declarative product catalog used by Hermes Forge.

Canonical source lives under `server/forge/`:

- `schemas/` — JSON Schema specifications;
- `catalog/capabilities/` — capability manifests;
- `agent-packages/<id>/agent.yaml` — AI employee packages.

The loader is intentionally stricter than generic YAML parsing. Unknown fields, unknown operation IDs, invalid references and sensitive-looking literal values fail closed.

## Execution boundary

Manifest files are data, not executable scripts. `provision.install`, `provision.uninstall` and `health.operation` accept only operation IDs compiled into the validator. They can never contain shell commands, paths to scripts or arbitrary subprocess arguments.

`availability: planned` is inert by contract: it cannot declare install/uninstall/health operations and cannot be referenced by an Agent Package.

Tenant credentials are never stored in catalog manifests. A capability may declare the name of a required write-only secret, such as `MCP_MATON_API_KEY`, but never its value.

## Initial catalog

Available in v1:
- Maton;
- Telegram Secretary;
- Web Search;
- Image Studio;
- Video Editor.

Visible but explicitly planned:
- Google Workspace;
- GitHub;
- Finance.

Initial employee package: `personal-hermes@1.0.0`.

## Production layout

`install.py` creates immutable releases under:

`/opt/proai-hermes-forge-catalog/releases/<catalog-sha256>/`

and atomically switches:

`/opt/proai-hermes-forge-catalog/current`

The installed release contains the validated public `catalog.json`, digest, release metadata and the canonical source manifests/schemas. No tenant state is copied into this tree.
