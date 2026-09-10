# Shared Hermes Video Editor

Safe shared Hermes integration of `ranahaani/i-hate-editing`, pinned to commit `e8ea406bc2440ca8fc8d1b239c8758e9de112388` (MIT).

The model is the editor: it reads transcript/timing/taste memory, requests real pixels on demand when a visual decision matters, and authors the EDL, captions, cards and proof beats. Deterministic code transcribes, samples visual windows, cuts, verifies seams, grades, composites, mixes and packages the result.

## Product flow

`prepare → takes → visual drill-down → render/mechanical verify → mandatory Director QA(cut) → captions/cards/proof/look/sound → master candidate → mandatory Director QA(master) → mastered → delivery`

Every base-cut seam is re-transcribed. For v0.5+ jobs, neither a mechanically clean cut nor a successfully rendered master is ship-ready until the exact artifact SHA has a passing Director QA receipt.

## On-demand visual reasoning

`video_editor_timeline_view` attaches a bounded native multimodal image to the Hermes reasoning turn. It combines 3-8 real frames, exact timestamps, nearby transcript text and an audio waveform. For rendered cuts it marks real seams and preferentially samples immediately before/after them, so the model can judge jump cuts, gesture continuity, blink state and framing instead of guessing from ASR alone.

The tool accepts only an existing job id and a job-local target (`source_XX`, `cut`, or `master`), never an arbitrary filesystem path. Windows are capped at 30 seconds and the JPEG payload is bounded before it enters model context. Outputs stay mode `0600` inside the profile-local job directory.

The visual drill-down pattern is conceptually inspired by `browser-use/video-use` (MIT); this module uses an independent job-scoped implementation rather than copying its helper code.

## Mandatory Director QA

`video_editor_director_qa` automatically chooses a bounded set of visual windows instead of trusting the agent to remember which frames to inspect. Cut QA always includes the opening plus the highest-risk seams, ranked by source changes, short adjacent beats, zoom discontinuities, beat transitions and early-retention position. Final QA includes the opening plus card/proof transitions, risky seams and a face/thumbnail sample.

`video_editor_director_approve` binds the verdict to the SHA-256 and byte size of the exact `cut.mp4` or final/master candidate that was shown. Rerendering invalidates the receipt. Any EDL change deletes timeline-derived captions/cards/proof/SFX/composition/output artifacts; enrichment changes invalidate final approval. Two failed automatic QA cycles exhaust the correction budget and require owner escalation instead of an infinite loop.

Legacy jobs remain readable. The hard Director protocol activates when a job is rerendered under v0.5+, which stamps `director_protocol_version`.

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

The video runtime invokes the pinned local HyperFrames binary directly in offline mode; `publish`, cloud/capture/import and unpinned runtime resolution are outside the production path.

## Quality gates

- Voice is normalized to the upstream talking-head target range (`-3..-6 dBFS` peak) before enrichment.
- SFX are individually peak-normalized near `-14 dBFS`, then checked against voice-only cut levels.
- Music uses sidechain ducking and must report `voice preserved` before delivery.
- Captions must be explicitly proofread by Hermes before master rendering.
- Artifact-bound Director QA blocks enrichment until the cut is visually approved and blocks shipping until the final master candidate is visually approved.
- Beat-map validation prevents long visually dead stretches.
- Lighting/colour grade is previewed as before/after and stored as reversible `cut_graded.mp4`.

## Proof / B-roll security

`video_editor_capture` is not upstream unrestricted capture. The wrapper accepts public HTTPS on port 443 only, rejects credentials, localhost/private/link-local/non-global IPs, revalidates every browser request and redirect, and runs with no user cookies/login session.

A captured page is a tall still plus text-target coordinates. Hermes then authors a frame-accurate zoom/scroll/highlight beat. Cards are never accepted as substitutes for real proof.

## Delivery

The module uses its own deterministic delivery layer because the pinned upstream `deliver.py` references a missing `variant_filter()` function. Same-aspect variants scale; cross-aspect variants use a blurred background plus the complete composed frame so captions are not silently cropped. Delivery includes thumbnail candidates, spoken-line material, post scaffold and JSON manifest.

## Current operational limits

- Large Telegram uploads are wired through the local Bot API with per-tenant read-only mounts and a configurable 1 GB production limit; the public Bot API 20 MB download path is not used for those files.
- Public proof pages only. Authenticated/private dashboards are intentionally unsupported until a per-client browser credential sandbox exists.
- Music must come from a profile-owned file or an approved licensed shared library.
- Heavy renders are serialized on the current 4-core VPS. A worker/GPU execution tier is the scale-out path for many simultaneous clients.
