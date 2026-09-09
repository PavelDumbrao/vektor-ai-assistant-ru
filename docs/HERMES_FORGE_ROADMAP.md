# Hermes Forge Implementation Roadmap

**Date:** 2026-09-09
**Companion:** `docs/HERMES_FORGE_WHITEPAPER.md`

## Objective

Move the existing Hermes Managed Bot control plane and Vektor fleet infrastructure to a self-service AI Employee Factory without destabilizing the current live assistants.

The roadmap is dependency-ordered. Each milestone should be small enough to review, test, deploy and roll back independently.

## Ground rules

- Do not rebuild the Hermes runtime from scratch.
- Do not migrate current clients just to satisfy a new abstraction.
- Preserve deny-by-default Forge access.
- Preserve per-tenant Linux/process/data isolation.
- Keep secrets and mutable tenant data out of Git.
- Prefer contract-first control-plane APIs over direct UI-to-filesystem operations.
- Keep privileged provisioning separate from public web requests.
- Add new releases beside old releases; never upgrade shared runtime in place.
- Every live rollout gets E2E verification and a checkpoint.
- Marketplace, billing and multi-node scheduling are downstream milestones.

## P0 - Prove current Managed Bot path

**Goal:** demonstrate that the existing Forge manager can create and manage one real child bot end to end before adding more layers.
Tasks:

1. Create one authorized test Managed Bot through `@ProAIHermesBot`.
2. Confirm `managed_bot` update arrives and token is stored only in root-protected managed state.
3. Confirm restricted bot access is applied as expected.
4. Bind the bot to a disposable/test Hermes profile, not an important production profile.
5. Verify service start, Telegram identity and one real request/response.
6. Verify `/my`, `/status`, restart behavior and failure reporting.
7. Verify an unauthorized Telegram user cannot create, list or control Hermes.
8. Add automated tests for any gap found during the live run.

Acceptance criteria:

- at least one child bot is present in Forge managed state;
- no token appears in Git, normal logs or user-facing responses;
- test Hermes survives manager restart;
- unauthorized user flow remains deny-by-default;
- rollback/removal procedure is documented.

Deliverable suggestion: one PR containing only fixes/tests discovered by the E2E proof, plus a sanitized checkpoint.

## P1 - Desired-state schema

**Goal:** define what one Hermes instance should look like before automating provisioning.

Create a versioned `HermesInstance` schema with fields for tenant, owner, bot binding, Agent Package, runtime release, capabilities, model policy, memory policy, lifecycle and connection metadata.

The schema contains references/status only. It never contains raw secret values.
Tasks:

1. Add JSON Schema or equivalent validation.
2. Add lifecycle enum and transition rules.
3. Add schema fixtures for current Pavel/Baysangur/Vyacheslav/Bebov profiles without secrets.
4. Build a read-only importer that can represent existing fleet state in the schema.
5. Add drift reporting between registry desired state and actual runtime facts.
6. Do not make the reconciler mutate production yet.

Acceptance criteria:

- all existing profiles can be represented without private data leakage;
- invalid identifiers/versions/capabilities are rejected before provisioning;
- schema changes are versioned and test-covered;
- current fleet continues running unchanged.

## P2 - Bounded Provisioner API

**Goal:** turn today’s administrative scripts into a narrow machine-operable lifecycle service.

Provisioner operations should be explicit, for example:

- prepare tenant;
- provision tenant DB/role;
- bind pinned release;
- install package/capability;
- write validated configuration/secret reference;
- start/restart/stop service;
- health check;
- upgrade/rollback;
- deprovision with explicit destructive confirmation.

Do not expose generic shell execution through Forge API.
Tasks:

1. Wrap existing shared-runtime/Profile Factory operations behind typed functions or job handlers.
2. Validate owner/profile/package identifiers before privileged operations.
3. Add idempotency keys and reconciliation-safe retries.
4. Capture structured step status without secret-bearing command output.
5. Add rollback checkpoints before each destructive/runtime-changing step.
6. Run Provisioner as a separate privileged service/worker with minimal exposed interface.
7. Add integration tests against a disposable tenant.

Acceptance criteria:

- public/control-plane process cannot request arbitrary commands;
- repeating the same provisioning job does not duplicate tenant resources;
- failure at any step reports a bounded state and can retry/rollback;
- no existing production tenant is modified during tests.

## P3 - Automatic clean-tenant provisioning

**Goal:** eliminate `awaiting_profile` as a manual operator dependency for an approved new user.

Flow:

`Managed Bot created -> HermesInstance requested -> tenant provisioned -> package installed -> verified -> active`

Tasks:

1. Generate a safe owner/tenant slug independently from untrusted display names.
2. Allocate tenant runtime identity through the Provisioner.
3. Provision separate PostgreSQL tenant database/role.
4. Bind a pinned verified runtime release.
5. Install a minimal official starter package/template.
6. Bind the Managed Bot token without exposing it to the user.
7. Generate sanitized baseline config and tenant-private SOUL/USER placeholders.
8. Start service and execute health suite.
9. Mark `active` only after all required checks pass.
10. Send activation notification with deep link to control panel.

Acceptance criteria:

- a brand-new authorized Telegram user can receive a running Hermes without SSH;
- operator intervention is not required on the happy path;
- failed provisioning leaves no ambiguous half-active tenant;
- lifecycle state is visible to Forge;
- deletion/rollback of the disposable test tenant is documented and tested.

## P4 - Forge API and Mini App auth

**Goal:** introduce a stable HTTPS control-plane boundary before adding UI features.

Tasks:

1. Implement Telegram Mini App init-data verification server-side.
2. Map Telegram identity to tenant membership and owner/operator role.
3. Issue short-lived tenant-scoped sessions.
4. Add read APIs for Hermes status, package, runtime, connections and health.
5. Add bounded mutation APIs for restart, connection changes and health checks.
6. Add replay/rate-limit protections for sensitive mutations.
7. Add structured error codes suitable for UI display.
8. Ensure raw systemd/filesystem internals are not exposed unnecessarily.

Acceptance criteria:

- forged user IDs cannot access another tenant;
- direct unauthenticated API calls are rejected;
- user can read only their authorized Hermes instances;
- one restart/health mutation works entirely through the API.
## P5 - Write-only Secret service

**Goal:** let users connect personal credentials without ever sending them through Telegram chat or exposing them after storage.

Tasks:

1. Define secret metadata model: tenant, secret name, capability, created/updated timestamps, validation state and optional safe fingerprint.
2. Add `set/replace/delete/status/check` operations; intentionally omit `get value`.
3. Accept secret submissions only over authenticated HTTPS.
4. Redact request bodies and secret-bearing exceptions from logs/tracing.
5. For MVP, atomically inject required values into tenant `.env` with strict owner/mode.
6. Add provider-specific validators/handshakes that return only status/error class.
7. Add audit events for set/replace/delete/check without values.
8. Add tests proving API/admin UI cannot retrieve stored secret values.
9. Document future vault migration behind the same abstraction.

Acceptance criteria:

- secret never appears in Telegram messages, Git, API read responses or ordinary logs;
- owner can replace/delete and observe status changes;
- admin control surface also has no reveal operation;
- dependent capability moves to `needs_reconnect` after deletion/invalid credential.

## P6 - Mini App MVP

**Goal:** ship the three-screen control panel.

### Screen 1: My Hermes

Show identity, bot, lifecycle status, runtime/package version, model policy, memory status, connected tool count, last activity and primary alerts.

Actions: Open Hermes, Restart, Test Hermes, Configure.
### Screen 2: Connections

Show catalog/list of installed and available capabilities with states such as `connected`, `not_connected`, `degraded`, `needs_reconnect`.

Initial connection target: Maton or another integration that proves personal-secret submission plus real handshake.

Actions: Connect, Check, Reconnect, Disconnect.

### Screen 3: Secrets

Show only secret metadata and masked/fingerprint representation captured safely at submission.

Actions: Add, Replace, Delete, Check.

Tasks:

1. Build responsive Telegram Mini App shell.
2. Use Forge API exclusively; no direct filesystem/service calls.
3. Add loading/error/degraded states for every operation.
4. Add confirmation for destructive mutations.
5. Deep-link from bot menu and activation notifications.
6. Add browser/Mini App E2E tests for tenant isolation and secret non-readback.

Acceptance criteria:

- owner can operate the happy path from a phone without SSH;
- current status is understandable without infrastructure vocabulary;
- all secret operations obey write-only contract;
- restart and health check results are visible in UI.

## P7 - Integration Catalog v1

**Goal:** make capabilities installable/reconcilable rather than hardcoded tenant customization.
Tasks:

1. Define capability manifest and lifecycle hooks.
2. Represent current modules such as Passive Secretary, Maton and shared video editor as catalog entries without changing their runtime behavior.
3. Declare required secrets/OAuth scopes and health contract per capability.
4. Add install/upgrade/uninstall desired-state transitions.
5. Add trust/source metadata: Official initially.
6. Add cost metadata where measurable.
7. Add UI cards for at least Maton, Telegram Secretary, Web Search and Image Studio.

Acceptance criteria:

- enabling/disabling a capability is reflected in desired and actual state;
- capability install is idempotent;
- required connection prerequisites are visible before activation;
- one failed capability does not make unrelated capabilities look healthy/unhealthy incorrectly.

## P8 - Agent Package v1 specification

**Goal:** turn role + personality + tools + policies + evals into a portable product artifact.

Tasks:

1. Create `agents/` source-of-truth directory.
2. Define `agent.yaml` v1 schema and JSON Schema validation.
3. Define allowed references to capabilities, model policies and memory policies.
4. Define package-local `SOUL` defaults and tenant override rules.
5. Define onboarding questions and output mapping into tenant state.
6. Define package health/eval suite reference.
7. Define install, upgrade and rollback semantics.
8. Add package signing/checksum strategy before third-party publishing.
9. Add CI that validates all packages without tenant secrets.

Acceptance criteria:

- a package can be installed into a disposable clean tenant;
- package source contains no customer-specific mutable state;
- invalid capability/version/policy references fail CI;
- package upgrade can be rolled back independently of tenant memory/secrets.
## P9 - Health, evals and activity model

**Goal:** make `Test Hermes` and operational state trustworthy enough for self-service support.

Tasks:

1. Define structured health result schema.
2. Separate runtime health, integration health and package evals.
3. Add synthetic probes that do not leak or mutate real customer data unnecessarily.
4. Track last successful activity and last failure category.
5. Add user-safe activity feed with provenance/cost where available.
6. Add alerts for degraded required integrations and repeated runtime failures.
7. Add fleet-level admin health view without exposing tenant secrets.

Acceptance criteria:

- user can distinguish `offline`, `degraded`, `needs_reconnect` and `healthy`;
- health failures point to an actionable subsystem;
- secret-bearing provider errors are redacted;
- a package cannot be marked verified if required evals fail.

## P10 - First official employee

**Goal:** prove the platform as a product, not just infrastructure.

Recommended first package: **Personal Executive Hermes**.

Minimum capabilities:

- Telegram conversation;
- web/research;
- personal memory/recall;
- one personal integration requiring secure connection;
- approval mechanism for at least one consequential action;
- health/eval suite;
- clear onboarding and role description.

Run this package with several clean pilot tenants. Measure activation time, support intervention, cost and failure modes before multiplying package count.
## P11 - Metering, billing and workforce view

**Goal:** introduce economics only after reliable activation and operations exist.

Tasks:

1. Attribute model/tool/media execution cost to tenant and Agent Package.
2. Add usage ledger independent from provider billing pages.
3. Define subscription plan and included usage semantics.
4. Add hard/soft usage limits and low-balance/limit alerts where applicable.
5. Add organization/workforce model for multiple Hermes employees.
6. Build fleet dashboard for owner: status, tasks, approvals, cost and failures.
7. Add invoice/payment provider only after internal metering reconciles correctly.

Acceptance criteria:

- internal usage totals reconcile against provider/runtime evidence within an agreed tolerance;
- billing never depends on an LLM-estimated cost string;
- owner can see current period usage before invoice;
- disabling billing does not break core runtime state.

## P12 - Marketplace

**Goal:** open distribution only when install, operation, evals and metering are stable.

Tasks:

1. Add publisher identity and trust tiers: Official, Verified Partner, Community.
2. Add package submission pipeline and automated schema/security checks.
3. Add permission/capability disclosure on store cards.
4. Add eval evidence and compatible runtime versions.
5. Add install/hire flow using existing Agent Package lifecycle.
6. Add pricing, publisher revenue accounting and platform commission.
7. Add review/revoke/deprecate process for unsafe or abandoned packages.
8. Add package provenance and immutable release artifacts.

Acceptance criteria:

- third-party package cannot bypass declared permissions/provisioner contracts;
- uninstall/deprecation is safe and does not delete tenant-owned memory/secrets by surprise;
- publisher payout can be reconciled to actual paid package usage/subscriptions.
## Critical path

The minimum dependency chain to a sellable self-service proof is:

```text
P0 Managed Bot E2E
 -> P1 Desired State
 -> P2 Bounded Provisioner
 -> P3 Clean Tenant Provisioning
 -> P4 Forge API/Auth
 -> P5 Write-only Secrets
 -> P6 Mini App MVP
 -> P9 Health/Evals
 -> P10 First Official Employee
```

P7 Catalog and P8 Agent Package v1 may overlap with P5-P6 once the desired-state/provisioner contracts are stable, but they should not block proving the first end-to-end employee.

P11 Billing/Workforce and P12 Marketplace are explicitly downstream.

## Recommended next five PRs

1. **`test: prove Hermes Forge managed-bot E2E`**
   Live proof fixes, test fixture, sanitized checkpoint, no architecture rewrite.

2. **`feat: add Hermes instance desired-state schema`**
   Schema, validation, lifecycle states, current-fleet read-only projection.

3. **`feat: add bounded Hermes provisioner jobs`**
   Typed lifecycle operations, idempotency, structured status, disposable-tenant tests.

4. **`feat: provision clean Hermes tenant from Forge`**
   Replace happy-path `awaiting_profile` with automatic approved-user provisioning.

5. **`feat: add Forge API and Telegram Mini App auth`**
   Read status + restart/health mutation first; no secret UI until Secret service contract lands.

This sequence delivers infrastructure leverage before frontend scope expands.
## Release gates

### Gate A - Infrastructure proof

Passed when a disposable clean user reaches active Hermes without manual SSH and can be removed/rolled back safely.

### Gate B - Self-service proof

Passed when that user can connect one personal integration, replace/delete its secret, run health check and restart Hermes entirely through Telegram + Mini App.

### Gate C - Product proof

Passed when several clean pilot users activate the first official employee with measured onboarding time, support load and unit cost.

### Gate D - Commercial proof

Passed when paid tenants remain active, metering reconciles and gross margin/support economics are understood.

### Gate E - Marketplace readiness

Passed only after third-party packages can be validated, installed, permission-bounded, upgraded, rolled back, metered and revoked safely.

## Stop conditions

Pause expansion and fix platform fundamentals if any of the following occurs:

- cross-tenant data or secret exposure;
- provisioning requires recurring manual root intervention;
- package/version state cannot be reconciled reliably;
- health UI reports healthy while required runtime/integration is broken;
- secrets appear in logs, chat history, Git or API read responses;
- rollback can overwrite newer tenant memory or credentials;
- per-tenant unit cost/support load is unknown before aggressive growth.

The objective is not maximum feature count. The objective is a repeatable, secure, observable path from **Hire** to **Working AI employee**.
