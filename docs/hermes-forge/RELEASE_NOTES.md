# Hermes Forge Release Notes

## 2026-09-22 - Source-of-truth and Telegram Group Self-Service

### Added

- Canonical Hermes Forge operational tree under ops/hermes-forge/.
- Architecture, deployment and production-sync documentation.
- Durable Telegram passive-group consent registry.
- Owner-only self-service onboarding for new work groups.
- Bot membership and read-permission health checks before capture is enabled.
- Recovery for groups where the bot was already present but the original membership event was missed.
- Explicit approval result showing bot, read, capture and outbound status.
- Regression coverage for group enrollment and neighboring Passive Secretary contracts.

### Safety behavior

- Unknown group messages remain fail-closed.
- A non-owner cannot enroll a group.
- The owner's recovery-triggering message is not archived.
- Pending or denied groups cannot leak through a legacy static allowlist.
- Secrets, tenant data, Telegram sessions, archives, exports and built releases remain outside Git.

### Verification

- New group self-service regression: 4/4 passed.
- Targeted neighboring regression suite: 37/37 passed.
- Production canary rights audit: all previously approved groups present and readable.
- Client configuration and Passive Secretary settings remained unchanged during the canary.

### Production finding

A release-layout and import-provenance debt was discovered: a copied Python virtual environment can resolve modules from its parent release. This is tracked as P0 in PRODUCTION_SYNC.md and must be hardened before broad fleet rollout.

### Rollback model

Hermes profiles continue to use immutable releases with per-profile symlink switching. Rollback points to the previously verified release and restarts only the affected profile service.
