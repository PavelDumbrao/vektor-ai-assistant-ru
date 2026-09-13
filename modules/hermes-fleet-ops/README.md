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

## SharedMetrics local export delivery

Product telemetry stays on the Hermes native privacy boundary. Each enrolled profile gets an hourly randomized systemd timer whose oneshot exporter runs as that tenant Linux user with `PrivateNetwork=true`. The exporter does nothing when telemetry is disabled or no native metrics DB exists; otherwise it invokes only `SharedMetricsStore.create_and_export_package_if_due()` from that profile's pinned Hermes runtime.

The resulting `0600` JSON package remains in the tenant-local `~/.hermes/telemetry/shared_metrics/outbox`. The existing root Forge Collector reads and validates only these bounded v1/v2 packages, drops `install_id`, and stores aggregate counters centrally. No conversation text, prompts, model output, tool arguments/results, documents or secret values are part of this delivery path. Provider incidents are exported only as the bounded `hermes.provider_error.count` dimensions: provider alias, allowlisted model, error category, HTTP class and fallback stage. Raw provider messages, request IDs, credentials and client content are never exported.

A root-only enrollment timer periodically discovers validated profile registry entries and enables the fixed per-tenant exporter timer template. It never reads tenant metrics databases or package bodies. Product packages follow the Hermes native daily package cadence; fleet health and update collection remain on the existing five-minute interval.
## Unified Fleet Telemetry

The root-only analytics database is the single operational observation center for managed Hermes profiles. `report.py summary` combines four bounded layers:

- Hermes service and Telegram health;
- privacy-safe model, tool, approval, skill and task counters;
- Living Memory run aggregates;
- telemetry-delivery and capability status per profile.

Living Memory ingestion reads only its profile-local `audit.jsonl`, requires the file to be tenant-owned/private, and stores only allowlisted counters/statuses: run outcome, messages reviewed, accepted/rejected operations, primary failures, fallback contract retries, cursor advance, and active/hypothesis counts. It never centralizes conversation text, memory text, routes, rejection prose, snapshots, prompts or model responses.

The per-profile observation row also includes whether shared telemetry is enabled, whether the native metrics DB exists, exporter and Living Memory timer states, last Living Memory outcome/time, and Video Editor version. It does not contain media paths, critic reports, credentials or client content.

SharedMetrics delivery is local-only: each tenant exporter runs as that Linux user with networking disabled and writes only to its own telemetry outbox. The root collector validates and ingests those bounded packages. The enrollment timer discovers new registered profiles and enables their fixed exporter timers automatically.

Operator check: `python3 /opt/proai-hermes-fleet-ops/report.py summary`. A healthy rollout has all managed profiles enabled for shared telemetry, active exporter timers, active Living Memory timers, and no recent Living Memory failures.
