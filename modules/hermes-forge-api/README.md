# Hermes Forge API MVP

This module is the security boundary between the Telegram Mini App and privileged Hermes runtime operations.

## Processes

- `proai-hermes-forge-api.service`: `www-data`, listens only on `127.0.0.1:8650`, serves the Mini App and JSON API. It cannot read `/home`, Forge root state, manager bot token or invoke `systemctl`.
- `proai-hermes-forge-control.service`: root-only bounded control daemon on `/run/proai-hermes-forge/control.sock`. The socket is `root:www-data 0660` and each peer is checked with `SO_PEERCRED`.

## Authentication

The Mini App sends Telegram `WebApp.initData` once to `/v1/auth/telegram`. Only the root control daemon can validate it because only that process can read the Forge manager bot token. Successful validation creates an opaque in-memory 30-minute session. The public API only forwards the opaque session token; sessions disappear on control-daemon restart.

## MVP API

- `POST /v1/auth/telegram`
- `GET /v1/hermes`
- `GET /v1/hermes/{profile}`
- `GET /v1/hermes/{profile}/health`
- `POST /v1/hermes/{profile}/health-check`
- `POST /v1/hermes/{profile}/restart`
- `GET /v1/hermes/{profile}/connections`
- `GET /v1/hermes/{profile}/secrets`
- `PUT|DELETE /v1/hermes/{profile}/secrets/MCP_MATON_API_KEY`
- `POST /v1/hermes/{profile}/connections/maton/test`

Secrets are write-only. Read endpoints return only presence and optional `last4`; no endpoint returns a stored value. Maton is validated with a read-only request before storage. Secret/config changes get a private per-profile backup and use atomic replacement.

`restart` is bounded: it is refused while `active_agents` or persisted active-session entries are non-zero, and it succeeds only after the same profile returns active with Telegram connected.

The API binds loopback only. HTTPS/reverse-proxy publication and BotFather Mini App URL configuration are separate deployment gates.
