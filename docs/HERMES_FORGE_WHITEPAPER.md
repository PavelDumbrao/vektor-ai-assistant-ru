# Hermes Forge Whitepaper

**Status:** Product/architecture source of truth
**Version:** 0.1
**Date:** 2026-09-09
**Project:** Hermes Forge / Pro AI

## 1. Executive summary

Hermes Forge is a control plane and product layer for provisioning, configuring, securing, operating and extending personal AI employees built on Hermes/Vektor runtime.

The product must not be perceived as "a Telegram bot creator". The intended user experience is closer to hiring and managing a digital employee: choose an agent, connect business systems, grant bounded permissions, verify health, and let the agent work.

The core product formula is:

```text
Telegram Bot -> Telegram Mini App -> Hermes Control Plane -> Profile Registry
             -> Secret Vault -> Provisioner -> Hermes Runtime
```

Telegram Bot is the entry point and notification channel. The Mini App is the primary management UI. Hermes Forge is the control plane. Hermes is the execution agent.

The strategic goal is to turn today's manually operated Hermes fleet into a self-service AI workforce platform where a user can create, connect, configure and operate an AI employee without SSH, VPS access, systemd, YAML, `.env` editing or BotFather knowledge.
## 2. Product thesis

The market is moving from model access and prompt builders toward managed AI workers with runtime, tools, memory, permissions, governance and measurable outcomes.

Hermes Forge should occupy the layer between low-level agent infrastructure and finished business work:

- users buy or activate a ready AI employee, not a prompt;
- developers publish repeatable Agent Packages, not one-off VPS setups;
- the platform provisions isolated runtime and data automatically;
- integrations are installed as capabilities through a catalog;
- secrets are managed through a write-only vault interface;
- actions have explicit permission and approval policies;
- health, activity, cost and errors are observable from one control plane.

The product promise is intentionally simple:

> Choose an AI employee. Connect your business. Approve access. Start working.

The infrastructure behind that promise may be complex. The user experience must not be.

## 3. Product principles

1. **Outcome first.** Expose jobs and results, not implementation details.
2. **Deny by default.** New users, tools and actions receive no access until explicitly granted.
3. **Tenant isolation.** Personal data, secrets, memory and processes remain tenant-scoped.
4. **Reproducibility.** Product logic lives in Git; mutable client state does not become source of truth.
5. **No secret readback.** A secret may be stored, checked, replaced or deleted, but never revealed again.
6. **Human control over consequential actions.** Each tool declares whether an action is read-only, approval-gated or autonomous.
7. **Model independence.** Agent identity and business logic must not be coupled to one LLM provider.
8. **Composable capabilities.** Tools, skills, memory and integrations are installable modules with contracts.
9. **Observable execution.** Every significant agent action should have status, provenance, cost and error context.
10. **Safe upgrades.** Runtime and Agent Packages are versioned, tested and rollbackable.

## 4. Current implementation baseline

This section records facts observed on the live VPS and in the repository on 2026-09-09. It is deliberately separated from target-state proposals.

### 4.1 Live control plane

The VPS runs `proai-hermes-manager.service` from `/opt/proai-hermes-manager/manager.py`.

The manager bot is `@ProAIHermesBot`. Telegram reports `can_manage_bots=true`, so native Managed Bot provisioning is enabled.

The current flow already supports:

- native Telegram `request_managed_bot` creation;
- `managed_bot` event handling;
- child token retrieval through Telegram Bot API;
- root-only token storage;
- owner/profile matching by Telegram identity;
- atomic token installation into an existing Hermes profile;
- systemd enable/restart for matched profiles;
- `awaiting_profile` registration when no existing profile is found;
- deny-by-default admission and private-chat-only operation.
### 4.2 Live fleet and shared runtime

The current reproducible fleet snapshot includes Pavel, Baysangur, Vyacheslav and Bebov profiles. Their Hermes processes run independently under separate Linux users and systemd services.

`/opt/vektor` already provides a shared immutable runtime layout with versioned releases, pinned Python/tooling, tenant profile registrations, upgrade tooling, backups and rollback procedures.

Current modules include Passive Secretary, Maton onboarding, Focus Assistant, AI Fixer, image provider integration, profile tools, shared runtime and the Hermes bot manager. A shared video-editor capability is also being introduced.

### 4.3 Current memory substrate

Production recall currently includes exact-date search, Russian full-text search, trigram typo/fuzzy fallback, source/sender/date filters and explicit provenance between live and imported history.

Semantic/vector memory is a planned second layer, not a production fact yet. The documented direction is tenant-local embeddings plus vector, FTS and fuzzy retrieval combined through ranking/fallback logic.

### 4.4 Main current product gap

The manager can attach a newly created Managed Bot to an **existing** Hermes profile, but it does not yet create a completely new tenant/profile end to end.

Therefore the current system is a strong managed infrastructure prototype, but not yet a fully self-service SaaS factory.

The highest-priority transition is:

```text
awaiting_profile -> automatic tenant provisioning -> health verification -> active
```
## 5. Target user experience

The user should manage Hermes as an employee, not as infrastructure.

### 5.1 Entry point

The user opens `@ProAIHermesBot` and sees a small set of product actions:

- `🤖 My Hermes`
- `🔌 Connections`
- `🧩 Tools`
- `🧠 Personality & Memory`
- `📊 Status`
- `⚙️ Management`

The Telegram bot should remain intentionally thin. It handles onboarding entry, important approvals, alerts and deep links into the Mini App.

The Telegram Mini App becomes the primary control surface for configuration and operational visibility.

### 5.2 "My Hermes" screen

The default screen should answer, at a glance:

- Which Hermes is this?
- Is it online?
- Which bot/channel represents it?
- Which runtime and Agent Package version is installed?
- Which brain/model policy is active?
- Which memory layer is enabled?
- Which tools and integrations are connected?
- When did it last work?
- Does anything need attention?
Example:

```text
HERMES SALAVAT
🟢 Online
@SalavatHermesBot

Brain: GPT 5.6 Sol
Images: GPT Image 2.5
Memory: Passive Secretary 🟢
Tools: 7 connected

Integrations
Maton 🟢  Telegram 🟢  Google 🔴  CRM 🔴

Last activity: 3 minutes ago
[Open Hermes] [Restart] [Configure]
```

The exact providers above are configuration examples, not permanent product dependencies.

### 5.3 MVP screens

The first Mini App release should deliberately contain only three primary screens:

1. **My Hermes** - identity, status, runtime, models, memory, last activity, restart.
2. **Connections** - integrations, health, connect/reconnect/disconnect flows.
3. **Secrets** - write-only personal credentials with masked status and rotation controls.

This scope removes the largest onboarding and support bottlenecks before building a marketplace or advanced dashboards.
## 6. Control-plane architecture

Hermes Forge should be split into explicit responsibilities rather than growing one Telegram process indefinitely.

```text
Telegram Bot / Mini App
        |
        v
Forge API + Auth
        |
        +--> Profile Registry
        +--> Secret Vault
        +--> Integration Registry
        +--> Agent Package Registry
        +--> Provisioning Jobs
        +--> Health / Activity API
        +--> Approval Service
        |
        v
Provisioner / Runtime Operator
        |
        v
Tenant Hermes Runtime
```

### 6.1 Telegram Bot

Responsibilities:
- identity bootstrap through Telegram;
- native Managed Bot creation;
- notifications and alerts;
- approval prompts for consequential actions;
- deep links into Mini App screens.

It must not become the main configuration database or secret-entry surface.
### 6.2 Telegram Mini App

Responsibilities:
- authenticated management UI;
- secret submission over HTTPS;
- connections and tool catalog;
- status, logs and health summaries;
- personality/memory controls;
- model policy selection;
- user/permission management.

The Mini App should never receive raw platform secrets and should never receive stored personal secrets back from the backend.

### 6.3 Forge API

The Forge API is the authoritative control-plane boundary. It should expose narrow, tenant-scoped operations such as:

- `GET /me/hermes`
- `POST /me/hermes/provision`
- `POST /me/hermes/restart`
- `GET /me/integrations`
- `POST /me/integrations/{id}/connect`
- `POST /me/secrets/{name}`
- `DELETE /me/secrets/{name}`
- `POST /me/health-check`

The concrete HTTP schema is future work. The architectural rule is contract-first: the Mini App and Telegram bot call a stable control-plane API instead of editing runtime files directly.

### 6.4 Provisioner

The Provisioner owns privileged lifecycle operations. It turns a validated desired state into a tenant runtime without exposing root capabilities to the web-facing API.
Provisioner responsibilities:
- allocate tenant identity and runtime directories;
- create or bind tenant database credentials;
- select a pinned Hermes release;
- install an Agent Package and required capabilities;
- write tenant-owned configuration atomically;
- create/enable the runtime service;
- run E2E health checks;
- mark the profile active only after verification;
- perform versioned upgrade and rollback.

The web API should submit provisioning intent. A bounded privileged worker executes it.

## 7. Secret architecture

Secrets are a first-class product subsystem, not a convenience field in settings.

### 7.1 Two secret classes

**Platform secrets** are owned by Hermes Forge infrastructure and are never visible to clients. Examples include shared model gateways, shared monitoring credentials and platform-managed providers.

**Personal secrets** belong to one tenant/Hermes. Examples include Maton, customer CRM credentials, customer Supabase keys, private APIs and other business integrations.

Google Workspace and similar OAuth integrations should prefer delegated OAuth tokens over asking users to paste long-lived API credentials.

### 7.2 Write-only invariant

Forge may know that a secret exists, its metadata, validation state and optional safe fingerprint. Forge UI must never expose the stored secret value after submission.
Allowed operations are:

- `create/set` - accept a new value once over authenticated HTTPS;
- `check` - perform a bounded provider handshake without returning the secret;
- `replace/rotate` - overwrite with a new value;
- `delete/revoke` - remove the stored credential and disable dependent capability;
- `status` - return only metadata such as connected/error/last_checked.

Example UI state:

```text
Maton
Status: CONNECTED
MCP: https://mcp.maton.ai
Secret: ••••••••••7K2Q
Last check: 2 minutes ago
Tools discovered: 14
[Check] [Replace] [Delete]
```

The suffix is optional safe metadata captured at submission time; it must never be reconstructed by reading the secret back for display.

### 7.3 MVP storage vs target vault

For the first controlled VPS release, personal secrets may continue to be injected atomically into tenant-owned `.env` files with strict ownership/mode and secret-safe logging, because this matches the current Hermes runtime contract.

Target state should move control-plane secret persistence behind a dedicated vault abstraction with encryption at rest, version/rotation metadata and auditable access. Runtime injection then becomes an implementation detail of the Provisioner.

Important limitation: while Forge runs on a shared root-administered VPS, the infrastructure administrator remains technically privileged at the operating-system level. "No secret readback" is therefore a product/API invariant, not a claim that root is cryptographically unable to inspect tenant files.
## 8. Integration and tool catalog

Hermes Forge should expose capabilities through a catalog instead of requiring manual configuration edits.

Initial catalog candidates:

| Capability | User-facing purpose | Connection model |
|---|---|---|
| Maton | External services through MCP | personal secret + handshake |
| Google Workspace | Gmail, Calendar, Drive | OAuth |
| Telegram Secretary | Passive business/group memory | owner consent + Telegram setup |
| Web Search | Internet research | platform or tenant provider |
| Image Studio | Image generation | platform provider |
| Video Editor | Transcript-driven video editing | shared runtime tool |
| Finance | Personal/business financial data | approved connector/OAuth |
| GitHub | Repositories, issues, PR workflows | OAuth/app token |

Each catalog entry should declare:

- capability ID and semantic version;
- publisher and trust tier;
- required Agent Package/runtime version;
- required secrets or OAuth scopes;
- filesystem/network/tool permissions;
- health-check contract;
- install/uninstall hooks;
- data retention behavior;
- estimated/actual execution cost where measurable.

Clicking **Connect** should create a desired-state change. Forge validates prerequisites, provisions the capability, performs a real handshake and only then marks it connected.
## 9. Agent Package v1

The key product abstraction is an installable, versioned **Agent Package**. A package describes the employee role and required capabilities independently from one mutable tenant runtime.

Proposed repository shape:

```text
agents/sales-hermes/
  agent.yaml
  SOUL.md
  skills/
  tools/
  policies/
  evals/
  onboarding/
  migrations/
```

`agent.yaml` should be declarative and machine-validated. It may include:

- package ID, name, publisher and version;
- role and expected outcomes;
- compatible Hermes runtime range;
- model policy and fallbacks;
- required/optional capabilities;
- memory policy;
- trigger types and schedules;
- action/approval policies;
- onboarding questions;
- health checks and eval suites;
- pricing/metering metadata when commerce is enabled.

The package must never contain tenant secrets, private IDs, customer memory or copied mutable state from an existing client.
Example conceptual manifest:

```yaml
schema: hermes.agent/v1
id: sales-hermes
name: Sales Hermes
version: 1.0.0
runtime: ">=0.21,<0.22"
role: sales
models:
  primary: platform/default-reasoning
  fallback: platform/default-fast
memory:
  passive_secretary: true
capabilities:
  required: [web-search]
  optional: [gmail, calendar, crm]
permissions:
  crm.read: autonomous
  crm.write: approval
  email.draft: autonomous
  email.send: approval
health:
  suite: sales-hermes-smoke-v1
```

The exact schema is intentionally not frozen by this whitepaper. It should be introduced through a separate reviewed specification and JSON Schema tests.

## 10. Provisioning lifecycle

A clean tenant should progress through explicit states rather than hidden shell steps:

`requested -> identity_created -> bot_bound -> runtime_prepared -> secrets_pending -> capabilities_installing -> verifying -> active`

Failure states must be explicit and retryable. A failed step must not silently produce an apparently healthy Hermes.
Provisioning must be idempotent: repeating a request after timeout must reconcile desired state instead of creating duplicate Linux users, databases, bots or services.

A successful first-time flow should require no operator SSH access:

1. Admin admits/invites the user, or future commercial signup authorizes them.
2. User chooses an Agent Package.
3. User creates or binds a Telegram Managed Bot.
4. Forge creates a tenant/profile identity.
5. Provisioner allocates database/runtime resources.
6. Required integrations are requested through Mini App.
7. Secrets/OAuth grants are written through the control plane.
8. Agent Package capabilities are installed.
9. E2E health/eval suite runs.
10. Profile becomes `active` only if required checks pass.
11. User receives a Telegram notification and can open the new Hermes.

## 11. Permission and autonomy model

Permissions should be defined around **business actions**, not only technical tools.

Every action class should resolve to one of three user-visible autonomy levels:

- **Read / Observe** - may fetch data but cannot mutate external state.
- **Approval required** - prepares an action and asks the owner before execution.
- **Autonomous** - may execute within explicit bounded policy.

Examples:

| Action | Default policy |
|---|---|
| Search web | autonomous |
| Read connected calendar | autonomous |
| Draft an email | autonomous |
| Send external email | approval |
| Change CRM stage | approval |
| Delete data / rotate credential | owner-only control-plane action |
The platform should distinguish at least three human roles:

- **Owner** - controls tenant, secrets, billing, destructive actions and delegation.
- **Operator** - can use Hermes and inspect allowed status without seeing secrets.
- **Platform Admin** - operates infrastructure and support functions through audited controls.

Telegram groups and additional users must be explicit grants. Existing deny-by-default behavior remains the baseline.

## 12. Health, observability and verification

The product needs a single user action: **Test Hermes**.

A health run should verify only safe bounded checks and return a structured result:

- service/process active;
- Telegram identity reachable;
- configured model route responds;
- required database is reachable;
- memory plugin can read/write a synthetic probe where safe;
- installed capabilities load;
- each integration passes its declared handshake;
- required filesystem ownership/modes are correct;
- Agent Package version matches registry desired state.

Health output should be redacted and user-readable:

```text
Overall: DEGRADED
Telegram: OK
Runtime: OK
Memory: OK
Maton: AUTH_EXPIRED
Google: NOT_CONNECTED
Last successful task: 12 min ago
```

Operational logs should be split between user-safe activity records and privileged infrastructure logs. Neither may log secret values.
## 13. Mini App authentication and session model

Telegram Mini App authentication should be validated server-side from Telegram-signed init data. Client-supplied user IDs are never trusted on their own.

Recommended session flow:

1. Mini App opens from `@ProAIHermesBot`.
2. Frontend sends Telegram init data to Forge API over HTTPS.
3. Backend validates signature, freshness and Telegram identity.
4. Backend resolves identity to an allowed Forge account and tenant membership.
5. Backend issues a short-lived, tenant-scoped application session.
6. Sensitive mutations require CSRF/replay-safe request semantics and server-side authorization.

The browser must not receive VPS credentials, filesystem paths with operational authority, systemd access, database admin credentials or platform provider secrets.

## 14. Desired-state registry

Forge should evolve from editing runtime artifacts directly toward a desired-state registry.

For each Hermes instance the registry should know:

- tenant and owner identity;
- Telegram bot binding;
- Agent Package and version;
- Hermes runtime release;
- enabled capabilities and requested versions;
- connection state without secret values;
- model policy;
- memory policy;
- autonomy/approval policy;
- lifecycle state and last reconciliation result.

The runtime is then reconciled from this desired state. This makes drift detectable and enables safe upgrades, restore and future multi-host scheduling.
## 15. Runtime isolation model

The current one-Linux-user-per-Hermes model is a useful baseline and should be preserved until a stronger isolation layer is deliberately introduced.

Minimum tenant boundaries:

- separate Linux user and home;
- separate `HERMES_HOME`;
- separate runtime process/service;
- separate tenant database/role;
- separate secret material;
- separate memory, workspace and browser/session state;
- no tenant write access to shared immutable runtime code;
- tenant-scoped temp directories.

Shared artifacts may include pinned code releases, common binaries, model gateways and platform monitoring, provided tenants cannot mutate them.

Future deployment modes may include containers, microVMs or separate hosts for higher-assurance tenants. They are not required for the first SaaS proof if the current isolation contract is explicitly documented and continuously tested.

## 16. Model layer

Hermes identity must be separated from model provider identity.

A package should request model capabilities such as:

- reasoning;
- fast/cheap fallback;
- image generation;
- speech/transcription;
- vision/video analysis.

Forge maps these capabilities to current provider routes. This allows platform-wide model changes without rewriting the Agent Package or user personality.

Advanced tenants may later bring their own provider credentials or select an approved model policy, but the default path should remain managed and simple.
## 17. Memory and personality product model

Personality, business context and memory should be separate concepts in Forge UI and storage contracts.

**Personality** defines stable interaction behavior: role, tone, principles, boundaries and decision style. It maps to sanitized package defaults plus tenant-specific `SOUL` configuration.

**User/Business context** contains confirmed facts and preferences about the owner/company. It maps to tenant-specific user/business knowledge, not to package source code.

**Operational memory** contains conversation/event history, structured state and recall indexes.

The UI may expose:

- role and purpose;
- tone/personalization controls;
- connected knowledge sources;
- memory status and retention policy;
- clear/reset/import operations with explicit scope;
- provenance for imported versus live memory.

Long-term memory operations must remain tenant-scoped and auditable. Adding semantic embeddings must not silently change which external provider receives private archive content.

## 18. Tool execution and MCP

MCP is a preferred capability-access protocol where it reduces custom integration work, but Forge must not couple its product model to MCP alone.

A tool/capability may be implemented through MCP, HTTP API, local process, plugin, browser automation or another bounded adapter. The Agent Package references a stable capability contract; runtime adapters implement it.

This keeps the architecture HTTP-first and contract-first while allowing MCP-native tools to plug in cleanly.
## 19. From tool catalog to Agent Store

The marketplace is a later distribution layer, not the first product milestone.

Recommended trust tiers:

- **Hermes Official** - built and operated by the platform team;
- **Verified Partner** - third-party package passing security, schema and eval requirements;
- **Community** - visible with stronger warnings and constrained permissions until reviewed.

A Store card should show more than an avatar and description. It should include:

- job/outcome;
- required integrations;
- permissions/autonomy level;
- package/runtime version;
- publisher/trust tier;
- eval/health evidence;
- expected usage/cost model;
- real example run where safe;
- install/hire action.

The Store becomes viable only after the platform can reliably install and operate official packages. Building a marketplace before repeatable self-service provisioning would create supply without a dependable execution substrate.

## 20. AI Workforce dashboard

After the single-Hermes control panel works, Forge can expand to an organization-level view:

`My AI Workforce`

The dashboard should surface status, current task, waiting approvals, errors, cost, completed work and measurable outcomes across agents.
A useful top-level summary could show:

- tasks completed this period;
- hours saved estimate;
- agent/tool execution cost;
- approvals waiting;
- unhealthy integrations;
- most active agents;
- incidents and recoveries;
- business outcome metrics supplied by the Agent Package.

The platform should avoid pretending ROI is known when it is not. Estimated savings and revenue attribution must state the formula/source.

## 21. Commercial model

Initial monetization hypotheses, not validated pricing:

1. **Agent subscription** - monthly fee per active Agent Package/employee.
2. **Execution usage** - model, media, browser, compute or third-party tool usage with transparent markup.
3. **Marketplace commission** - percentage of third-party Agent Package revenue.
4. **Business onboarding** - paid setup, migration and integration work for B2B customers.
5. **Private Forge** - dedicated/private control plane, governance and support for larger organizations.

A plausible SMB packaging experiment:

- simple personal Hermes: `$49-99/month + usage`;
- specialized employee: `$99-299/month + usage`;
- business workforce: `$500-5,000/month` depending on agents/integrations/support;
- onboarding/integration: `$1,000-10,000+` for non-standard business environments.

These numbers are product hypotheses and require real customer validation and unit-economics measurement.
## 22. Security model and threat boundaries

The Forge control plane is high-value infrastructure because it can affect agent identities, credentials and runtime state. Security requirements therefore belong in the product contract.

Minimum controls:

- deny-by-default identity and tenant authorization;
- server-side validation of every control-plane action;
- no trust in Mini App client claims without Telegram/backend verification;
- no secrets in Git, URLs, analytics, error payloads or normal logs;
- no secret readback endpoint;
- atomic secret/config writes with restrictive ownership/modes;
- bounded privileged Provisioner instead of arbitrary shell from web requests;
- allowlisted operations and validated identifiers before service/filesystem actions;
- approval gates for destructive or externally consequential actions;
- audit trail for secret replacement/deletion, permission changes, installs and restarts;
- rate limits and replay protection for sensitive endpoints;
- immutable package/release identifiers and verified artifacts;
- backup/rollback without copying stale secrets over newer tenant state.

High-risk actions should require recent owner authentication and explicit intent. Examples include deleting memory, rotating credentials, granting new operators, enabling autonomous outbound messaging and removing a tenant.

## 23. Failure model

Forge should make failure states visible and recoverable.

Examples:

- provider credential expired -> integration `needs_reconnect`;
- health check failed -> profile `degraded`, not silently `active`;
- package install partially failed -> reconciliation retries or rolls back;
- runtime release unhealthy -> switch back to last verified release;
- Telegram bot ownership/management changed -> bot binding flagged for owner action;
- external provider outage -> use declared fallback where safe, otherwise surface degraded capability.
## 24. MVP definition

Hermes Forge v0.1 is complete only when a newly admitted user can reach a working Hermes without operator SSH access.

Required E2E journey:

1. Open `@ProAIHermesBot`.
2. Open Mini App.
3. Select the first official Agent Package.
4. Create/bind a Telegram Managed Bot.
5. Forge automatically creates the tenant/profile.
6. Runtime/database/service are provisioned.
7. User connects at least one personal integration through the Mini App.
8. Secret is accepted over HTTPS, stored without readback and validated.
9. User sees `CONNECTED`, not the raw credential.
10. User runs **Test Hermes**.
11. Required health checks pass.
12. Hermes reaches `ACTIVE`.
13. User sends a real task to their own bot and receives a correct response.
14. User can restart Hermes from the Mini App.
15. User can replace/delete the integration secret and observe state change.

### 24.1 MVP screens

Only three primary screens are required:

- **My Hermes**
- **Connections**
- **Secrets**

Tools Catalog may initially be embedded inside Connections. Personality editor, workforce analytics, billing and public marketplace can follow after this flow is reliable.
## 25. Success metrics

The first product metrics should measure reliability and onboarding friction before vanity growth.

Operational metrics:

- median time from admitted user to `ACTIVE` Hermes;
- percentage of provisioning runs completed without admin intervention;
- percentage of health checks passing on first run;
- integration reconnect rate;
- runtime restart/recovery success rate;
- secret exposure incidents: target zero;
- cross-tenant isolation incidents: target zero.

Product metrics:

- activation: user completes first real task within 24 hours;
- connected integrations per active Hermes;
- weekly active Hermes instances;
- tasks completed per Hermes;
- approval completion rate;
- 30-day retained paying tenants once billing exists.

Economic metrics:

- infrastructure + model cost per active tenant;
- gross margin per Agent Package;
- support minutes per new tenant;
- revenue per active Hermes;
- marketplace take rate when third-party packages launch.

## 26. Roadmap overview

**Phase 0 - Stabilize current Forge.** Prove one real Managed Bot E2E and keep deny-by-default security.

**Phase 1 - Self-service Factory.** Implement automatic tenant/profile provisioning and reconciliation.

**Phase 2 - Control Panel MVP.** Launch Mini App with My Hermes, Connections and Secrets.
**Phase 3 - Agent Package v1.** Formalize package manifest, validation, install/upgrade hooks, permissions and evals.

**Phase 4 - Official catalog.** Ship several repeatable official employees/capabilities and measure unit economics.

**Phase 5 - Workforce + billing.** Add organization view, metering, subscriptions and usage controls.

**Phase 6 - Marketplace.** Add publisher onboarding, verification tiers, revenue share and package discovery.

A separate implementation roadmap should break these phases into PR-sized milestones and acceptance criteria.

## 27. First official Agent Packages

Do not launch with twenty weak agents. Start with a small set that exercises different platform capabilities.

Recommended candidates:

1. **Personal Executive Hermes** - memory, Telegram, web, calendar/email integrations, approvals.
2. **Content Hermes** - research, copy, image generation, publishing workflows and optional video editing.
3. **Sales Hermes** - research, CRM/email connections, lead workflows and approval-gated outbound actions.
4. **Secretary Hermes** - passive Telegram Business/group capture, recall, voice transcription and owner-controlled scope.
5. **Research Hermes** - web/browser, documents, structured reports, citations and scheduled monitoring.

The first package used for v0.1 should be the simplest one that still proves provisioning, secret connection, health check and real work. Personal Executive Hermes is the strongest default candidate.

## 28. Deliberate non-goals for v0.1

- public marketplace;
- complex visual workflow builder;
- arbitrary shell execution from the control plane;
- multi-cloud scheduler;
- perfect cryptographic tenant isolation from VPS root;
- dozens of integrations;
- advanced enterprise RBAC;
- A2A orchestration between multiple autonomous employees;
- automatic ROI claims without measured business data.
## 29. Architectural decisions to preserve

The following current project decisions align with the target product and should not be discarded casually:

- GitHub is source of truth for reproducible code/templates.
- Mutable tenant data and secrets remain outside Git.
- Shared runtime is immutable to tenants.
- Per-tenant processes and data stores are isolated.
- Product changes graduate from one-off pilot state into modules/templates/tests.
- Upgrades are prepared as new releases and support rollback.
- Telegram owner consent remains authoritative for Business/group capture scope.
- Outbound behavior is bounded separately from passive receive-only memory.

## 30. Open design questions

These require explicit ADRs before implementation, not accidental decisions inside feature code:

1. Which backend/API stack should power Forge Mini App and reconciliation?
2. Where should desired state live: PostgreSQL, existing registry files, or a migration path from files to DB?
3. Which vault implementation is appropriate after the `.env`-compatible MVP?
4. How should privileged provisioning be isolated from the public API process?
5. What is the canonical capability manifest shared by modules and Agent Packages?
6. Which actions require owner approval by default?
7. How should metering attribute model/tool/media costs to tenant and Agent Package?
8. Which first package is used to prove clean-user onboarding?
9. When should the system move from one VPS to multi-node scheduling?
10. What minimum eval/security bar is required for Verified Partner packages?

## 31. Definition of Hermes Forge

Hermes Forge is not a bot, prompt library or workflow builder.

**Hermes Forge is the control plane, factory and future distribution layer for deployable AI employees.**

Its job is to transform a versioned Agent Package plus authorized customer connections into a healthy, observable, upgradeable and bounded Hermes runtime that can do real work.
