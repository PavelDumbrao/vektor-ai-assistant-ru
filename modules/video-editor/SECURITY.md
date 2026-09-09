# Video Editor Security Notes

## Trust boundaries

Profile Hermes code is unprivileged. It can read only profile-owned video/audio paths accepted by the plugin and write only its own `HERMES_HOME/video_editor` job tree.

Shared execution data under `/opt/vektor/video-editor` is root-owned and read-only to profiles. The OpenRouter key is stored separately under `private/openrouter.env` with mode `0600 root:root`.

The ASR broker listens on `127.0.0.1`, accepts audio bytes rather than server file paths, authenticates a profile capability token, limits request size/concurrency and never returns the provider secret.

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
