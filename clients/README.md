# Client Wish Registry

This directory is the source of truth for structured client requirements.

Rules:
- Store requirements, decisions, integration state, and message references only.
- Never store raw Telegram chats, voice files, transcripts, API keys, tokens, session strings, or `.env` values.
- Client wishes do not automatically authorize implementation. Follow capture -> research -> proposed architecture -> Pavel approval -> implementation unless the capability is already approved.
- Reusable behavior belongs in `capabilities/`; client-specific configuration belongs in `clients/<profile>/`.

Allowed request statuses:
`captured`, `needs_clarification`, `research`, `proposed`, `approved`, `in_progress`, `deployed`, `deferred`.

Allowed commercial values:
`included`, `paid_addon`, `future_product`, `unknown`.

Telegram evidence is referenced as message IDs only, for example `telegram:1165483`.
