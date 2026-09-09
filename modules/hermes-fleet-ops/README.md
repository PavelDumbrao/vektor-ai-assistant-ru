# Hermes Forge Fleet Operations

This module provides the operational control loop around the Hermes fleet:

- privacy-safe central metrics collection;
- current health and state-change history;
- tool/model usage analytics without conversation content;
- progressive update rings with automatic pause on failure;
- release/profile policy controls;
- update attempt receipts and admin summary data.

## Privacy contract

Central analytics accepts only allowlisted aggregate counters. It never stores prompts, messages, model responses, tool arguments, tool results, documents, API keys, OAuth tokens, session IDs, task IDs or raw tool names.

Tool usage is normalized to bounded families such as `maton`, `web_search`, `image_gen`, `video_editor`, `terminal`, `files` and `passive_secretary`.

The collector rejects unknown metric names, unknown dimensions and unknown dimension values rather than storing arbitrary JSON.

## Update model

A release is never inferred from “whatever directory is newest”. It must be explicitly promoted into a track/channel in `/etc/proai-hermes-fleet-policy.json`.

Channels:
- `canary` - explicit test cohort;
- `preview` - internal/early users;
- `stable` - normal clients, progressive stages `10 -> 50 -> 100` by default;
- `pinned` - observe-only compatibility/pilot hold. The automatic updater never switches a pinned profile; an expected/current release mismatch is reported as `pinned_drift`.

A stable stage advances only after its eligible cohort is current and healthy for the configured number of consecutive updater cycles. A real upgrade failure pauses that track/channel. Busy profiles are deferred and retried later, not treated as broken.

Existing heterogeneous profiles are initially pinned and therefore excluded from automatic switching. The release ID remains an expected/reference value for drift visibility, not an auto-enforced target. New `h<telegram_id>` Forge tenants default to `modern/stable`. Existing profiles can be moved to modern/legacy channels after compatibility is explicitly proven.

Actual profile switching delegates to the existing shared-runtime `upgrade_profile.py`, preserving its idle check, immutable release validation, snapshot, readiness verification and rollback behavior.

## Runtime

- code: `/opt/proai-hermes-fleet-ops`;
- policy: `/etc/proai-hermes-fleet-policy.json`, root controlled;
- analytics DB: `/var/lib/proai-hermes-analytics/forge_analytics.sqlite3`, root `0600`;
- collector timer: every five minutes;
- updater timer: hourly;
- admin report: `python3 /opt/proai-hermes-fleet-ops/report.py summary`.

Promotion example:

```bash
python3 /opt/proai-hermes-fleet-ops/policyctl.py promote \
  --track modern --channel stable --release-id hermes-<verified-release>
```

Resume a paused rollout only after the incident is understood:

```bash
python3 /opt/proai-hermes-fleet-ops/policyctl.py resume --track modern --channel stable
```
