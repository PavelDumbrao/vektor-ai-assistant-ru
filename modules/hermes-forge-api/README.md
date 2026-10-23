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

## Security Hardening (October 2026)

### 1. Filesystem & Injection Protections (`control.py`)
- **`ProfileFs` context manager**: All operations on user profile files (`/home/<profile>/.hermes`) use directory file descriptors (`dir_fd`) with `O_DIRECTORY | O_NOFOLLOW`. Symlinks at `.env`, `config.yaml`, `backups`, or `inbox` are rejected with `profile_file_unsafe` (409) without following or touching target files.
- **Ownership validation**: Verifies `st_uid == pw_uid` on all opened directories and files.
- **Client file size limits**: Strict `MAX_CLIENT_FILE_BYTES = 256 * 1024` (256 KiB). Files exceeding this limit raise `profile_file_too_large` (413).
- **Subprocess YAML parser**: Isolated subprocess parsing with memory limit `RLIMIT_AS=256MB` and a 5-second timeout, completely preventing YAML entity expansion / parser hang attacks.
- **Systemctl argument protection**: All unit names validated against `^[a-z0-9@_.-]+$` and passed strictly after `--` delimiters (`systemctl is-active --quiet -- "$unit"`).

### 2. Workspace & Session Hardening (`control.py`)
- **Concurrency control**: Thread-safe workspace modifications guarded by `_WS_LOCK`.
- **Invite security**:
  - `accept_workspace_invite`: Verifies invite profile belongs to owner before acceptance; claims invite atomically (`used=True`) before performing mutations; dispatches Telegram `sendMessage` notification to owner.
  - Member management: Protects against self-removal by owner (409); verifies member existence for remove/grant/revoke (404 `workspace_member_not_found`).
  - Active invite listing (`list_workspace_invites`) and pre-use revocation (`revoke_workspace_invite`).
- **Session management**:
  - `auth_date` from Telegram `init_data` is tracked in session objects.
  - Sensitive operations (`restart`, `set_secret`, `delete_secret`, `grant_workspace_tool`, `revoke_workspace_tool`, `remove_workspace_member`) require `auth_date <= 15 minutes` (`SENSITIVE_AUTH_MAX_AGE_SECONDS = 900`), returning 401 `reauth_required` if expired.
  - User session limit: Max 5 concurrent sessions per `user_id`, evicting the oldest session FIFO upon creating the 6th.
  - `logout` endpoint invalidates current session.
- **Operations audit log**: Sensitive operations (`restart`, `set_secret`, `delete_secret`, `bind_zoom`) logged to `/var/log/proai-hermes-forge/ops.log` in JSON format (`timestamp`, `actor_user_id`, `profile`, `op`, `details`, `result`), rotating at 10 MB to `.1`.

### 3. API Gateway & Mini App Hardening (`api.py`, `app.js`)
- **Socket binding & timeouts**: Supports Unix domain socket via `LISTEN_SOCKET=/run/proai-hermes-forge/api.sock`; HTTP requests capped at `Handler.timeout = 10` seconds.
- **DoS protection**: Semaphore-bounded threading (`BoundedThreadingMixIn`) limited to 32 concurrent requests, immediately returning 503 `server_overloaded` on saturation.
- **Input validation & CSP**:
  - Profile regex: `^[a-z0-9][a-z0-9_-]{1,39}$`.
  - Member user ID regex: `^\d{5,20}$`.
  - Secret names strictly allowlisted (`MCP_MATON_API_KEY`).
  - URL-encoded parameters in client requests.
  - Strict CSP: `frame-ancestors https://web.telegram.org https://*.telegram.org; base-uri 'none'; form-action 'none'; object-src 'none'`.
- **Mini App UX**: Transparent silent re-authentication on 401 `reauth_required`, active invite management, and member ID validation.

### 4. Service Hardening & Deployment Units (`deploy/`)
- **`proai-hermes-forge-api.service`**: `CapabilityBoundingSet=`, `ProtectProc=invisible`, `ProcSubset=pid`, `SystemCallFilter=@system-service`, `IPAddressDeny=any`, `IPAddressAllow=127.0.0.1 172.18.0.0/16`, `LISTEN_SOCKET=/run/proai-hermes-forge/api.sock`.
- **`proai-hermes-forge-control.service`**: Minimal capabilities (`CAP_CHOWN CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH CAP_FOWNER CAP_SETUID CAP_SETGID CAP_KILL`), `NoNewPrivileges=true`, `ProtectSystem=strict`, `ReadWritePaths=/home /var/lib/proai-hermes-forge /run/proai-hermes-forge`.
- **`proai-hermes-forge-bridge.service`**: Socat TCP-to-UNIX bridge forwarding `172.18.0.1:8650` to `/run/proai-hermes-forge/api.sock`.
- **`edge-nginx.conf`**: Rate limiting zones for `/v1/auth/telegram` (5r/s), `/v1/webhooks/zoom` (10r/s), general API (20r/s), and real IP extraction from `172.18.0.0/16`.

## Versioned live API upgrades

`install.py` is bootstrap-only. If `proai-hermes-forge-api.service` is already active it fails closed with `active_api_requires_versioned_upgrade`.

Use `upgrade.py` for an active installation. Without `--apply` it is read-only and prints the deterministic release digest/path.

The upgrader packages only the unprivileged API surface:

- `api.py`;
- the bundled Kitchen compiler;
- Mini App static files.

It does not package or restart root control, Hermes runtimes, tenant configuration, secrets or memory.

On `--apply`, the release is created immutably under `/opt/proai-hermes-forge-api/releases/<sha256>/`, `current` is switched atomically and a systemd drop-in redirects only `proai-hermes-forge-api.service` to the versioned API.

Activation requires successful loopback probes for `/healthz`, `/v1/catalog` and read-only `/v1/kitchen/preview`. Failure restores the previous `current` pointer/drop-in, restarts the previous API and verifies basic health before reporting rollback success.
