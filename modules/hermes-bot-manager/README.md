# Hermes Bot Manager

Central Telegram control plane for `@ProAIHermesBot` (`Hermes Forge | Pro AI`).
It uses Telegram Managed Bots so every client owns their bot while the manager
handles technical provisioning.

## Flow

1. User chooses **Hire AI assistant** (`Нанять AI-ассистента`).
2. Forge asks for the assistant display name in ordinary chat text. The name may be any 1–64 character display name.
3. Forge asks for the Telegram username in ordinary chat text and validates it locally: 5–32 characters, Latin letters/digits/underscore only, mandatory `bot` suffix. Invalid input gets a precise correction message.
4. Forge shows a summary and one **Confirm hire** button. The managed-bot deep link is prefilled with the chosen name and username.
5. Telegram shows the single native ownership confirmation required for managed-bot creation. The user does not re-enter the data there.
6. The new bot is owned by the client exactly like a bot created through BotFather; Hermes Forge is its authorized technical manager.
7. The manager receives the `managed_bot` update, calls `getManagedBotToken`, stores the token root-only and connects it to the prepared Hermes profile.
8. Access is restricted by default and the profile service starts.

If Telegram reports that the username is occupied, the user returns to Forge and simply sends another username in the chat. The draft keeps the chosen display name.

No client bot token is committed to Git or returned in normal logs.

## Telegram prerequisite

The manager bot must have **Bot Management Mode** enabled in the BotFather
Mini App. Verify with `getMe`: `can_manage_bots` must be `true`.

The bot itself exposes:

- `/start` and `/menu` — main control surface;
- `/hire` — start the guided chat-based hiring flow (`/new` and `/create` remain aliases);
- `/my` — bots owned by the current Telegram user;
- `/status` — current number of hired AI assistants;
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
