# Shared Hermes video editor

Safe Hermes integration of `ranahaani/i-hate-editing`, pinned to an audited commit.

The upstream project is MIT licensed. Its execution model is preserved: transcription and silence analysis create a reading view; the Hermes model makes editorial decisions; deterministic scripts render the EDL; every seam is re-transcribed and mechanically checked.

## Security boundary

Client Hermes profiles do **not** receive shell access through this module. The plugin only accepts video paths inside that profile's own `~/.hermes/cache/videos` or `~/workspace`, rejects symlinks, creates profile-owned job directories, validates EDL ranges, serializes heavy work through a shared lock, and invokes an allowlisted set of pinned scripts.

The server wrapper does not expose upstream `review.py --lan`, arbitrary `proofread.py --llm` shell commands, or unrestricted `capture.py` URLs. Those are intentionally deferred until they have hardened wrappers.

## Runtime

`prepare_runtime.py` installs a root-owned pinned upstream checkout and an official pinned whisper.cpp Ubuntu x64 binary. Two shared pinned Whisper models are downloaded to `/opt/vektor/video-editor/models`: `small` for the default fast CPU path and `medium` for an explicit quality path. `install.py` deploys only the thin plugin + skill into each Hermes profile.

MVP scope: talking-head cut planning, transcript paging, silence cut points, deterministic render, seam verification, and persistent owner taste rules. Captions/cards/B-roll/SFX are phase 2 after the base cut is approved.

## CPU policy

On the current 4-core Hostinger VPS the default is `transcription_quality=fast` (Whisper small + greedy beam/best-of 1). `quality` keeps Whisper medium and upstream decode defaults; it is intentionally much slower and is a future candidate for a GPU worker.
