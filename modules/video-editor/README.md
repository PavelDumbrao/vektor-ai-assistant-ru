# Shared Hermes Video Editor

Safe shared Hermes integration of `ranahaani/i-hate-editing`, pinned to commit `e8ea406bc2440ca8fc8d1b239c8758e9de112388` (MIT).

The model is the editor: it reads transcript/timing/taste memory and authors the EDL, captions, cards and proof beats. Deterministic code transcribes, cuts, verifies seams, grades, composites, mixes and packages the result.

## Product flow

`prepare → takes → render/verify → captions/approve → look → cards → real-page proof → sound → master → music/variants/thumbnails`

Every base-cut seam is re-transcribed. A completed render is not considered correct until mechanical and semantic seam checks pass.

## ASR

Primary latency path: root-owned local broker → OpenRouter `openai/whisper-large-v3-turbo` with word/segment timestamps. Profiles receive only a capability token; the OpenRouter key never enters a client home.

Fallback: pinned local `whisper.cpp v1.9.2`, `small` for fast and `medium` for quality. `asr_provider=auto` prefers the broker and falls back locally.

## Shared enrichment runtime

One root-owned runtime is reused by all profiles:

- HyperFrames `0.8.30`, exact npm lock/integrity;
- Chrome Headless Shell `152.0.7977.30`, binary SHA-256 verified;
- Playwright `1.62.0` for hardened public-page capture;
- shared licensed/free SFX library, peak-indexed;
- `ffmpeg`/`ffprobe` for deterministic media work.

The video runtime `npx` wrapper accepts only pinned HyperFrames `check` and `render`; `publish`, cloud/capture/import and unpinned packages are blocked.

## Quality gates

- Voice is normalized to the upstream talking-head target range (`-3..-6 dBFS` peak) before enrichment.
- SFX are individually peak-normalized near `-14 dBFS`, then checked against voice-only cut levels.
- Music uses sidechain ducking and must report `voice preserved` before delivery.
- Captions must be explicitly proofread by Hermes before master rendering.
- Beat-map validation prevents long visually dead stretches.
- Lighting/colour grade is previewed as before/after and stored as reversible `cut_graded.mp4`.

## Proof / B-roll security

`video_editor_capture` is not upstream unrestricted capture. The wrapper accepts public HTTPS on port 443 only, rejects credentials, localhost/private/link-local/non-global IPs, revalidates every browser request and redirect, and runs with no user cookies/login session.

A captured page is a tall still plus text-target coordinates. Hermes then authors a frame-accurate zoom/scroll/highlight beat. Cards are never accepted as substitutes for real proof.

## Delivery

The module uses its own deterministic delivery layer because the pinned upstream `deliver.py` references a missing `variant_filter()` function. Same-aspect variants scale; cross-aspect variants use a blurred background plus the complete composed frame so captions are not silently cropped. Delivery includes thumbnail candidates, spoken-line material, post scaffold and JSON manifest.

## Current operational limits

- Raw Telegram uploads above the public Bot API limit are not yet wired into the multi-tenant Hermes relay. A local `telegram-bot-api --local` daemon already exists on the VPS; profile-isolated file relay is the remaining integration.
- Public proof pages only. Authenticated/private dashboards are intentionally unsupported until a per-client browser credential sandbox exists.
- Music must come from a profile-owned file or an approved licensed shared library.
- Heavy renders are serialized on the current 4-core VPS. A worker/GPU execution tier is the scale-out path for many simultaneous clients.
