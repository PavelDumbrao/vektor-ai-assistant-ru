# Hermes Forge Bot Manager

Central Telegram entry/control plane for `@ProAIHermesBot` (`Hermes Forge | Pro AI`). The Forge bot already exists. Clients use it to create their own Telegram Managed Bot; that child bot remains owned by the client and becomes the Telegram identity of their Hermes.

## Current hiring flow

1. Authorized user opens `@ProAIHermesBot` and chooses **Нанять AI-ассистента**.
2. User enters display name and bot username directly in chat.
3. Forge validates/canonicalizes the username and opens Telegram's native managed-bot confirmation.
4. Telegram creates the client-owned child bot and sends Forge a `managed_bot` update.
5. Forge retrieves the child token into root-only managed state and applies restricted access.
6. If Telegram identity already maps to an existing Hermes profile, the token is installed using the legacy safe bind path.
7. Otherwise Forge writes a versioned `HermesInstance` desired-state record and starts the bounded provisioner systemd unit.
8. The provisioner creates/reconciles the Linux tenant, shared runtime binding, PostgreSQL tenant, plugins and service.
9. Forge reports `active` only after runtime/Telegram health succeeds.

The normal new-client lifecycle is `provisioning -> active` or explicit `provision_failed`; `awaiting_profile` is no longer the intended product path.
## Runtime components

- manager: `/opt/proai-hermes-manager/manager.py`;
- manager secret: `/etc/proai-hermes-manager.env`, root `0600`;
- platform secrets: `/etc/proai-hermes-platform.env`, root `0600`;
- desired state: `/opt/proai-hermes-manager/state/instances/*.json`, root `0600`;
- child bot tokens: `/opt/proai-hermes-manager/state/managed/<bot_id>.env`, root `0600`;
- provisioning receipts: `/opt/proai-hermes-manager/state/provisioning/*.json`, root `0600`;
- manager service: `proai-hermes-manager.service`;
- bounded worker: `proai-hermes-provisioner@.service`.

The manager is intentionally sandboxed with `ProtectSystem=full`. It does not perform Linux-user/systemd/database mutations itself. It writes desired state and asks systemd to start a validated provisioner instance. The provisioner has no arbitrary-command API.

`seed_platform_secrets.py` is a one-time migration helper for the current VPS. It copies only approved provider keys into the dedicated platform file and never prints values.

## Existing assistant imports

Bots that existed before Managed Bots can be registered in Forge without recreation.
Pavel can run `/importprofile <linux_owner>` in a private Forge chat. The manager
resolves the profile owner, reads the existing bot identity, and stores only safe
metadata under `state.imported`; the existing bot token is never copied into state.
Imported assistants appear in **Мои AI-ассистенты** with live systemd status.
Telegram-level managed-bot privileges are not retroactively added: those are available
only for bots originally created through Telegram's managed-bot flow.

## Internationalization

Forge keeps client-facing copy outside the control-plane logic in `i18n.py`.
The current locale set contains 25 languages: Russian, English, Spanish, German, French,
Portuguese, Chinese, Arabic, Hindi, Turkish, Italian, Japanese, Korean, Indonesian,
Vietnamese, Polish, Ukrainian, Dutch, Persian, Hebrew, Thai, Bengali, Urdu, Malay, and Filipino.

- On first interaction Forge reads Telegram `user.language_code`.
- A per-user choice is stored in `state.locales` and wins over auto-detection.
- `🌐 Language` and `/language` expose the manual selector.
- Unknown explicit Telegram locales fall back to English.
- Legacy users with no language code retain Russian behavior.
- Missing translated strings fall back to English rather than breaking a flow.
- Adding a language requires a catalog entry, not new handlers or provisioning logic.

Private profile data, memories, tokens, and tenant boundaries are independent
of UI locale; changing language changes presentation only.

## Living Memory is included for every new client

The bounded provisioner installs Living Memory v0.1 automatically. It is a platform default, not an optional capability toggle. No client needs a separate memory setup step.

Creation order:
1. Validate platform primary and distinct OpenRouter reserve credentials before creating the Linux account.
2. Render only the generic tenant template; do not clone any existing client's memory.
3. Install the root-owned Living Memory release with that tenant's private storage. Keep its timer stopped during preparation.
4. Keep built-in memory reading and write approval enabled; set `memory.nudge_interval=0` to avoid competing per-turn background learning.
5. Run owner-scoped configuration checks and synthetic primary/reserve model probes, with no real conversation sent.
6. Start Hermes and require current systemd/gateway PID agreement, running gateway and connected Telegram.
7. Enable and verify the daily Living Memory timer, then publish `active` with a `health.living_memory` receipt.

A failed model, gateway or timer check must not publish `active`. Scoped cleanup stops only the new tenant's memory timer/service. Retrying provisioning preserves existing user memory and uses the same installed release.

Default schedule is 04:00 Europe/Moscow with up to 30 minutes of per-profile delay. Review scope remains only direct Telegram conversations between this owner and Hermes. GPT-5.6 Sol uses high reasoning; reserve is GPT-5.6 Sol through OpenRouter with a different credential. Missing reserve access blocks onboarding readiness rather than silently creating a reduced-quality memory agent.

The installer vendors `modules/living-memory` and `living_memory_onboarding.py`. Deploying this factory change does not reinstall, restart or migrate existing tenants.

Verification: automated factory/negative-path tests are part of Server snapshot CI. An isolated Linux canary additionally exercises the real installer, both provider routes, systemd timer/readiness, sandboxed empty scan and cross-tenant read denials without creating a public Telegram bot.
