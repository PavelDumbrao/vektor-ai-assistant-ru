# Hermes Forge Architecture

Hermes Forge is the control plane around isolated Hermes profiles.

## Source of truth

Canonical repository: PavelDumbrao/vektor-ai-assistant-ru.

The repository owns reusable product and operational logic. A VPS is a deployment target, not a source repository.

## Main layers

1. Forge control plane
   - modules/hermes-forge-api
   - modules/hermes-forge-catalog
   - modules/hermes-forge-kitchen
   - modules/hermes-forge-site
   - modules/hermes-bot-manager
   - modules/hermes-fleet-ops

2. Profile capabilities
   - modules/passive-secretary
   - modules/workspace-members
   - Living Memory and related integrations

3. Operations
   - ops/hermes-forge
   - immutable release construction
   - profile switching
   - upgrades
   - fleet timers and services
   - generic diagnostics and recovery tools

4. Runtime
   - immutable code under /opt/vektor/releases/<release-id>
   - per-profile state under /home/<profile>/.hermes
   - shared operational state under /opt/vektor/

## Isolation rules

- Profiles must not share tenant state, credentials or private archives.
- Runtime state, Telegram sessions, PostgreSQL data, exports and backups are not committed.
- Secret values never belong in release metadata, diagnostics evidence or Git history.
- Generic runtime code must not contain tenant IDs.

## Telegram Passive Secretary

Telegram Business direct messages and approved work groups use separate authorization paths.

Work-group capture is owner-consented:
1. bot is added to a group;
2. the exact owner is asked for consent;
3. Bot API rights are checked;
4. a durable registry records pending, approved or denied state;
5. only approved groups enter capture;
6. unknown group content is fail-closed.

A recovery path lets the exact owner trigger enrollment by sending the first message in a group whose membership event was missed. The trigger message itself is not archived.
