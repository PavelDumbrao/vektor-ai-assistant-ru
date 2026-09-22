# Hermes Forge Operations

This directory is the version-controlled operational source of truth for Hermes Forge.

## Contract

- Git contains reproducible code, deployment templates, upgrade tooling and runbooks.
- /opt/vektor/releases contains immutable build artifacts and is not source of truth.
- /home/<profile>/.hermes contains tenant/runtime state and is never copied into Git.
- Secrets stay outside Git and are supplied through environment/secret infrastructure.
- Production-only fixes must be ported back here before the next release is built.

## Layout

- runtime/ generic runtime preparation and profile switching helpers.
- upgrades/ versioned release and upgrade tooling.
- tools/ reusable operational tools.
- diagnostics/ generic diagnostics only; tenant-specific scripts stay outside source control.
- systemd/ production unit templates/snapshots without credentials.

## Release workflow

1. Change source in Git.
2. Run targeted regression tests and secret scan.
3. Build a new immutable release.
4. Canary one profile.
5. Verify service, runtime provenance, hashes and functional smoke.
6. Roll out to the fleet only after canary acceptance.
7. Record the change in docs/hermes-forge/RELEASE_NOTES.md.
