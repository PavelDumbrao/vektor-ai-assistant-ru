# Hermes Forge Production Sync

Updated: 2026-09-22

This file tracks production functionality that still requires normalization into the Git -> build -> deploy pipeline.

## Synced in this branch

- Telegram passive-group durable consent registry.
- Owner-only group enrollment.
- Dynamic approved and blocked group allowlist.
- MY_CHAT_MEMBER onboarding.
- Missed-membership recovery triggered only by the exact owner.
- Passive group permission health-check.
- Approval status summary.
- Regression tests for self-service group onboarding.
- Generic runtime, release and upgrade helpers from /opt/vektor/admin.
- Generic Hermes Forge, Fleet and Living Memory systemd templates.

## Production-only or legacy items deliberately not imported

The following classes of material are intentionally excluded:

- tenant onboarding folders containing profile-specific configuration or assets;
- historical builder scripts hard-coded to named clients or releases;
- diagnostics JSON or SQLite evidence;
- backups, logs, exports and release tarballs;
- real profile state and databases.

They may be generalized later, but must not be copied verbatim.

## P0 deployment debt: Python import provenance

During the 2026-09-22 production canary, a release-layout issue was exposed: a copied venv can retain import-path provenance that resolves code from a parent release. A release symlink alone is therefore not sufficient proof that Python imported modules from the intended release.

Required hardening before broad fleet rollout:

1. the release builder must make Python import provenance release-local or otherwise deterministic;
2. canary must assert module.__file__ for critical modules against the target release;
3. release verification must fail if imports resolve through a parent release;
4. immutable release metadata should include this import-provenance check.

## Current production reference

The production group self-service canary was deployed as a child of the verified li2 runtime. The source implementation is now ported to this Git branch so future releases do not depend on a production-only patch.
