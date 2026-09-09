# Hermes Bot Manager

Central Telegram control plane for `@ProAIHermesBot` (`Hermes Forge | Pro AI`).
It uses Telegram Managed Bots so every client owns their bot while the manager
handles technical provisioning.

## Flow

1. User opens Hermes Forge and chooses **Create Hermes**.
2. Telegram shows the native `request_managed_bot` creation UI.
3. The manager receives a `managed_bot` update.
4. It calls `getManagedBotToken` and stores the child token in a root-only file.
5. Access is switched to restricted by default: owner + Pavel tech admin.
6. If the creator Telegram ID matches an existing Hermes profile, the token is
   atomically installed as `TELEGRAM_BOT_TOKEN` and its systemd service starts.
7. Otherwise the bot is registered as `awaiting_profile`.

No client bot token is committed to Git or returned in normal logs.

## Telegram prerequisite

The manager bot must have **Bot Management Mode** enabled in the BotFather
Mini App. Verify with `getMe`: `can_manage_bots` must be `true`.

The bot itself exposes:

- `/start` and `/menu` — main control surface;
- `/new` — native managed-bot creation;
- `/my` — bots owned by the current Telegram user;
- `/status` — manager capability and user bot count;
- `/help` — concise help.

Admission is deny-by-default. The bot works only in private chats. Pavel is the
admin; existing provisioned Hermes profile owners are trusted automatically.
Additional users can be admitted only by Pavel with `/allow <telegram_id>` and
removed with `/deny <telegram_id>`. `/allowed` lists explicit additions.

## Runtime

- application: `/opt/proai-hermes-manager/manager.py`;
- secret: `/etc/proai-hermes-manager.env`, root `0600`;
- state: `/opt/proai-hermes-manager/state`, root `0700`;
- service: `proai-hermes-manager.service`;
- child tokens: `state/managed/<bot_id>.env`, root `0600`.

## Safety rules

- Never overwrite an active Hermes profile that already has a non-empty
  `TELEGRAM_BOT_TOKEN`; such a case is marked `manual_review_existing_token`.
- Profile matching is Telegram-ID based and must resolve to exactly one profile.
- Child bot tokens are never stored inside `state.json`.
- The manager service is the only component allowed to read the manager token.
- Managed bots default to restricted access: owner + Pavel tech admin.
- Hermes Forge itself is private-chat only and deny-by-default. Unknown users
  never receive the menu or managed-bot creation button.
- Every callback and `managed_bot` event is re-authorized server-side, so an
  old cached button cannot bypass admission control.
- Admin-only admission commands are `/allow`, `/deny`, and `/allowed`.
- All profile names used in systemd operations are validated before execution.

## Verification

```bash
python3 -m py_compile manager.py configure.py
systemctl is-active proai-hermes-manager.service
```

After enabling Bot Management Mode, `getMe` must report
`can_manage_bots=true` before client onboarding is considered production-ready.
