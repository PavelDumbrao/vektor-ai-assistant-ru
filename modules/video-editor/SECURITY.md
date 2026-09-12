# Video Editor Security Notes

## Trust boundaries

Profile Hermes code is unprivileged. It can read only profile-owned video/audio paths accepted by the plugin and write only its own `HERMES_HOME/video_editor` job tree.

Shared execution data under `/opt/vektor/video-editor` is root-owned and read-only to profiles. The OpenRouter key is stored separately under `private/openrouter.env` with mode `0600 root:root`.

The ASR broker listens on `127.0.0.1`, accepts audio bytes rather than server file paths, authenticates a profile capability token, limits request size/concurrency and never returns the provider secret.

The native-video critic uses a separate root-owned secret (`private/lingsuan.env`, `0600`) and a separate per-profile `critic_token`. Its broker binds only to `127.0.0.1:8778`, runs with read-only `/home` access plus `CAP_DAC_READ_SEARCH`, and accepts only regular video artifacts inside the authenticated profile's own `~/.hermes/video_editor/jobs` tree. The caller must provide the current SHA-256; a mismatch is rejected before upload. Source videos are never read wholesale into RAM: ffmpeg first creates a complete low-bitrate proxy capped at 24 MB, and only that bounded proxy is base64-encoded for Lingsuan.

Cloud video review is profile-policy gated (`off`, `final`, or `all`). It is intentionally fail-open: local artifact-bound Director QA remains mandatory even when Lingsuan is unavailable, and a cloud failure cannot silently convert into a pass. When a Gemini report is present, Hermes must explicitly acknowledge it before the artifact can be approved.

## Deliberately unavailable upstream surfaces

- `proofread.py --llm` arbitrary shell command
- `review.py --lan` unauthenticated network server
- unrestricted upstream `capture.py` URLs
- HyperFrames `publish`, cloud/capture/import or arbitrary `npx`
- fabricated proof assets

## Known dependency advisory

`npm audit --omit=dev` on HyperFrames 0.8.30 currently reports no high/critical findings and one moderate advisory chain through `adm-zip` (symlink traversal while extracting untrusted ZIP archives).

The affected archive extraction surface is not reachable through this module: the runtime wrapper allows only HyperFrames `check` and `render`; publish/import/capture archive workflows are blocked, and proof capture uses the separate hardened Playwright runner.

Review this boundary again before changing the HyperFrames pin or expanding the allowed CLI commands.

## Browser network policy

The proof runner resolves each HTTPS hostname and requires all resolved addresses to be globally routable. Every browser request is intercepted and revalidated, so redirects and subresources cannot be used to reach loopback, RFC1918, link-local or cloud metadata endpoints.

No cookies or authenticated browser profile are loaded. Authenticated proof is a future feature requiring per-client credential isolation.
