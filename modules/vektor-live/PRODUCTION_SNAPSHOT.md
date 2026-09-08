# Production snapshot: ВЕКТОР Live

`VERSION` is the repository snapshot version, not the Gemini model version.

## Source of truth

This module captures the live code observed on 2026-09-08:

- VPS server/web source from `/opt/vektor-live`;
- Mac Hands source from `~/Desktop/vektor-live/hands`;
- all 16 files shared by VPS and the Mac copy were SHA-256 identical at capture time;
- the 20 production source files are frozen in `releases/2026.09.08.1/manifest.json`.

The original production files are copied byte-for-byte. Repository-only files such as
`VERSION`, this document, tests, the safe `.env.example`, and `hands/requirements.txt`
are intentionally outside the production-file hash set.

## Excluded on purpose

Never commit `.env`, API keys, Hermes keys, link/admin/hands keys, logs, task state,
`.venv`, `__pycache__`, backups, or host-specific launchers. The current Mac launcher
contains deployment-specific SSH routing and is therefore not part of the public snapshot.

## Observed live topology

`Chrome/Gemini Live ↔ Vektor Live on VPS ↔ Hermes API`, plus an outbound WebSocket
from `hands/vektor_hands.py` on macOS to the VPS `/hands` endpoint. Server and Mac
validate the same mode/action allowlist independently.

Observed runtime at capture: Gemini `gemini-3.1-flash-live-preview`, Hermes model
`hermes-agent`, VPS container `vektor-live` healthy, Mac Hands process running on
macOS 26.6.2 with Python 3.13.3. Runtime voice and credentials remain deployment state.

## Restore boundary

Git restores programs, tests and dependency declarations. It does not restore secrets,
OAuth/session data, runtime task journals, Traefik configuration outside this compose file,
or macOS privacy permissions. See `server/RESTORE.md` for the recovery procedure.
