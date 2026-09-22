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
- Targeted Passive Secretary / Telegram / release suite: 45/45 passed.
- Exact GitHub release-contract reproduction: 117 tests passed, 8 skipped.
- Production canary rights audit: all previously approved groups present and readable.
- Client configuration and Passive Secretary settings remained unchanged during the canary.

### Telegram group onboarding hardening

- Removed all passive-group auto-leave behavior.
- Owner-added groups are auto-approved silently after a live Bot API rights check.
- Groups added by unknown admins remain present but capture stays fail-closed.
- A previously denied group can be reopened by the exact owner without remove/re-add.
- Owner denial disables capture but leaves the bot in the group.
- Added six regression scenarios covering owner auto-approve, denied reopen and no-autoleave behavior.

### Fleet post-reboot persistence

- Reconciled legacy active profile services with the canonical 30-profile-env.conf drop-in.
- Verified profile secrets/capability environment is restored on service start.
- Verified Passive Secretary archive access across the active fleet.
- Verified Maton read-only identity calls on Maton-enabled profiles.
- Verified active-release import provenance and scheduled-job heartbeat recovery.

### Production finding

A release-layout and import-provenance debt was discovered: a copied Python virtual environment can resolve modules from its parent release. This is tracked as P0 in PRODUCTION_SYNC.md and must be hardened before broad fleet rollout.

### Rollback model

Hermes profiles continue to use immutable releases with per-profile symlink switching. Rollback points to the previously verified release and restarts only the affected profile service.

## 2026-09-22 - Fathom Owner Post-Meeting Watcher

- Added a generic per-profile Fathom watcher.
- Polls completed recordings without external meeting writes.
- Sends one concise owner brief per new completed recording.
- Uses durable per-profile processed recording IDs to prevent duplicate delivery.
- First normal run seeds existing recordings, so enabling the timer does not flood historical meetings.
- Supports dry-run and explicit latest-recording acceptance smoke.
