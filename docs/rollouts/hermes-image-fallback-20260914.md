# Hermes image fallback rollout checkpoint - 2026-09-14

Source PR: #95, merged as `9875409d8f45b783432ac30a91d0360c34ac4b74`.
Provider plugin version: `1.1.0`.
Expected plugin SHA256: `75d09f6b71eaedd1789977a00188189ed0ed7ddbb0f4bb943f5952605bdcca87`.

Production chain:
1. Primary: GRSAI `gpt-image-2.5`.
2. Fallback 1: Lingsuan `gpt-image-2.5-sunburst` via `LLM_API_KEY`.
3. Fallback 2: Lingsuan `gpt-image-2.5-flare-firefly` via `FALLBACK_LLM_API_KEY`.

Verification:
- Fallback 1 provider-level canary: forced GRSAI failure -> sunburst success.
- Fallback 2 provider-level canary: forced sunburst 502 -> flare-firefly success.
- Fallback 2 artifact size: 122406 bytes.
- Profiles pavel, baysangur, vyacheslav, bebov, salavat all have plugin SHA match.
- All five profiles have GRSAI_API_KEY, LLM_API_KEY, FALLBACK_LLM_API_KEY present.
- All five configs remain `grsai/gpt-image-2.5`.
- All five systemd services active with NRestarts=0 at final audit.
- Bebov and Salavat Telegram gateways reconnected after rollout on 2026-09-14.

Bebov note:
- Imported legacy profile lacked GRSAI_API_KEY.
- Key was restored from root-owned platform secrets using manager internals without printing the secret.
- Root-only pre-change `.env` backup was created under `/root/backups/bebov-image-fallback-*`.
