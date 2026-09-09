# Hermes Forge Marketing Site

Static public landing page for Hermes Forge.

## Runtime
- nginx static container
- existing external Docker network: `n8n_default`
- TLS/reverse proxy: existing Traefik
- no backend, database or secrets
- default preview host: `hermesforge.srv1250550.hstgr.cloud`

## Product CTA
Primary CTA opens `https://t.me/ProAIHermesBot`.

## Design workflow
Read `DESIGN.md` before visual changes. The site intentionally avoids generic AI SaaS defaults and uses the Hermes Forge industrial foundry direction.

## Local validation
```bash
cd modules/hermes-forge-site
docker compose config
python3 -m http.server 8765 -d static
```

## Production preview
```bash
cd modules/hermes-forge-site
docker compose up -d
curl -fsS https://hermesforge.srv1250550.hstgr.cloud/healthz
```

## Custom domain
Target canonical host: `forge.proaicommunity.online`.

DNS must contain:
- type: `A`
- name: `forge`
- value: `31.97.199.12`

After DNS resolves to this VPS, set:
```bash
HERMES_FORGE_SITE_HOSTNAME=forge.proaicommunity.online docker compose up -d --force-recreate
```

Do not reuse `forge.srv1250550.hstgr.cloud`: that hostname belongs to the authenticated Hermes Forge Mini App/API edge.
