# Hermes Forge API MVP

This module is the security boundary between the Telegram Mini App and privileged Hermes runtime operations.

## Processes

- `proai-hermes-forge-api.service`: `www-data`, listens only on `127.0.0.1:8650`, serves the Mini App and JSON API. It cannot read `/home`, Forge root state, manager bot token or invoke `systemctl`.
- `proai-hermes-forge-control.service`: root-only bounded control daemon on `/run/proai-hermes-forge/control.sock`. The socket is `root:www-data 0660` and each peer is checked with `SO_PEERCRED`.

## Authentication

The Mini App sends Telegram `WebApp.initData` once to `/v1/auth/telegram`. Only the root control daemon can validate it because only that process can read the Forge manager bot token. Successful validation creates an opaque in-memory 30-minute session. The public API only forwards the opaque session token; sessions disappear on control-daemon restart.

## MVP API

- `POST /v1/auth/telegram`
- `POST /v1/kitchen/preview` — public, read-only Agent Package + capability selection preview compiled from the verified installed catalog
- `GET /v1/hermes`
- `GET /v1/hermes/{profile}`
- `GET /v1/hermes/{profile}/health`
- `POST /v1/hermes/{profile}/health-check`
- `POST /v1/hermes/{profile}/restart`
- `GET /v1/hermes/{profile}/connections`
- `GET /v1/hermes/{profile}/capabilities` — read-only installed/enabled/health state for catalog capabilities
- `POST /v1/hermes/{profile}/capabilities/{id}/enable|disable` — bounded toggle for installed Image Studio, Video Editor and Web Search only
- `GET /v1/hermes/{profile}/secrets`
- `PUT|DELETE /v1/hermes/{profile}/secrets/MCP_MATON_API_KEY`
- `POST /v1/hermes/{profile}/connections/maton/test`

Secrets are write-only. Read endpoints return only presence and optional `last4`; no endpoint returns a stored value. Maton is validated with a read-only request before storage. Secret/config changes get a private per-profile backup and use atomic replacement.

Public Kitchen preview never calls the root control daemon and cannot install, enable, disable, restart or provision anything. The API verifies the immutable installed catalog digest before compiling the redacted preview.

The Mini App exposes the same preview through Hermes Kitchen. A customer can select an Agent Package and only that package's declared optional capabilities even before a Hermes profile exists. The UI renders the returned plan with DOM `textContent`, shows a shortened plan/catalog digest and has no Apply/Install/Create control in v1.

`restart` is bounded: it is refused while `active_agents` or persisted active-session entries are non-zero, and it succeeds only after the same profile returns active with Telegram connected.

The API binds loopback only. HTTPS/reverse-proxy publication and BotFather Mini App URL configuration are separate deployment gates.
## HTTPS edge

Public TLS keeps the API loopback-only. The production path is:

`127.0.0.1:8650 -> 172.18.0.1:8650 socat bridge -> nginx edge -> Traefik TLS`.

`proai-hermes-forge-bridge.service` binds only the Docker gateway and forwards to localhost. The edge container publishes no host ports; it joins the existing external `n8n_default` network and is reachable only through Traefik.

Default MVP hostname: `forge.srv1250550.hstgr.cloud`, which uses the existing Hostinger wildcard DNS. `install_edge.py --hostname ...` can replace the hostname later without changing the API service.

The edge enforces a 64 KiB request-body ceiling, bounded request rate and TLS/HSTS via the existing Traefik certificate resolver.

## Capability Actions v1

Capability toggles are deliberately narrower than the catalog. Only already-installed `image-studio`, `video-editor` and `web-search` can be enabled or disabled here. Maton stays on the write-only secret flow, Telegram Secretary requires a separate consent UX, and planned capabilities remain inert.

Every mutation is owner-scoped and idle-gated before touching config. Forge writes a sanitized root-owned job receipt and audit event, creates a private tenant config backup, mutates only the fixed allowlisted toolset, restarts the exact Hermes profile, verifies the resulting state, and rolls back the config plus restart if verification fails.

Persistent action metadata lives under systemd-managed `/var/lib/proai-hermes-forge` with mode `0700`. Job/audit/desired-state records never contain messages, prompts, tool arguments/results, secret values or arbitrary exception text. The desired-state record is updated only after a successful or already-satisfied action, so a failed mutation cannot become an unimplemented future reconciliation request.
## Planned restart contract

Forge never uses bare `systemctl restart` for a healthy idle Hermes. Managed profiles expose `ExecReload=/bin/kill -USR1 $MAINPID`; Forge validates that contract plus `SuccessExitStatus=75` and `RestartForceExitStatus=75`, then asks systemd to reload the exact owner service. Hermes drains through its native SIGUSR1 restart path and exits with reserved code 75, which systemd treats as a successful planned exit and still relaunches.

Existing profiles are migrated with `install_restart_contract.py`. Dry-run is the default; `--apply` writes only a root-owned systemd drop-in adding `SuccessExitStatus=75`, daemon-reloads, verifies every profile and rolls the whole migration back if verification fails. The migration does not restart Hermes. New Forge profiles inherit the directive from the canonical service template.