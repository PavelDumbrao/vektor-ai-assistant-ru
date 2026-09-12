# Hermes Living Memory v0.1

Daily, silent, evidence-bound memory curation for managed Hermes profiles.

## Purpose

Living Memory scans only the owner's direct Telegram conversation with Hermes, compares new evidence with the current structured memory, and proposes bounded changes. It does not give the LLM filesystem, shell, database-write, or arbitrary tool access.

The curator has one capability: submit structured memory proposals. Deterministic host code validates evidence, privacy, sensitivity, forget intent, confidence and target IDs before any write.

## Memory cells

- identity
- communication
- preferences
- goals
- projects
- people
- decisions
- workflows
- expertise
- corrections
- boundaries
- content_text
- content_images
- content_video
- temporary_context

Structured state lives under `~/.hermes/living_memory/`. A bounded active summary is compiled into one managed block inside the built-in `memories/USER.md`, preserving all non-managed legacy entries.

## Model routing

Primary: `gpt-5.6-sol` with reasoning effort `high` on the profile's primary custom route.

Preferred fallback: `openai/gpt-5.6-sol` with reasoning effort `high` through OpenRouter using `OPENROUTER_API_KEY`. The fallback must be a distinct credential from primary. Existing profile fallback routes are considered only when the OpenRouter key is unavailable.

The worker never logs or stores key values.

## Evidence policy

Only user messages may establish user memories. Assistant messages are context only. Explicit statements and corrections outrank inferred patterns. A pattern requires at least two distinct user-message references. Hypotheses are stored separately and never injected into Hermes until promoted by stronger evidence.

Explicit forget requests remove the current item and scrub its text from Living Memory rollback snapshots, leaving only a non-reversible content hash tombstone.

Sensitive personal information and credentials are blocked from automatic memory. Conversation text is secret-redacted before it is sent to the curator.

## Schedule

The systemd timer runs daily at 04:00 Europe/Moscow with up to 30 minutes of deterministic random delay per profile. The cursor advances only after every processed batch succeeds. If both model routes fail, memory and cursor remain unchanged and the next run can retry safely.

## Operator commands

Status:
`worker.py --owner <owner> --status`

Provider-only smoke, no user transcript and no memory write:
`worker.py --owner <owner> --provider-smoke`

Shadow daily scan, proposals validated but not applied:
`worker.py --owner <owner> --shadow`

Manual apply:
`worker.py --owner <owner> --apply`

Rollback latest Living Memory snapshot:
`worker.py --owner <owner> --rollback-latest`
