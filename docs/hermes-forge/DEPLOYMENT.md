# Hermes Forge Deployment

## Desired pipeline

Git -> tests -> immutable release -> canary -> acceptance -> fleet rollout

Direct edits inside an active release are emergency-only and must be ported back to Git immediately.

## Canary requirements

Before switching a profile:

- source commit and release provenance recorded;
- rollback release known;
- profile configuration and settings hashes recorded;
- new runtime imports successfully;
- targeted tests pass.

After switching:

- service is active/running;
- NRestarts is stable;
- active symlink resolves to the expected release;
- profile config and settings hashes are unchanged unless intentionally migrated;
- functional smoke passes;
- no new critical startup errors;
- critical module __file__ paths resolve inside the target release.

## Rollback

Rollback changes only the profile release link and restarts that profile service. Tenant data should remain compatible unless a release explicitly declares otherwise.

## Secrets

Do not commit:

- .env files;
- Telegram sessions;
- Bot tokens;
- API keys;
- database DSNs;
- tenant configuration containing credentials;
- exported chats or files.
