# Hermes Forge Kitchen Plan v1

Hermes Kitchen compiles a validated Agent Package plus explicit optional capabilities into an immutable plan. This first slice is deliberately **compile-only**.

Input:

- one existing Agent Package ID;
- zero or more capability IDs already declared optional by that package;
- the canonical Forge catalog source.

Output:

- `hermes.kitchen-plan/v1` for future trusted execution;
- `hermes.kitchen-preview/v1` for owner-facing review;
- exact catalog SHA-256 and deterministic plan SHA-256.

Required package capabilities are always included. Selection order does not affect the plan digest.

The compiler has no apply/install/restart path and performs no network calls, subprocess execution, tenant reads or persistent writes.

## Internal plan vs public preview

The internal plan contains only bounded operation IDs already validated by the Forge Catalog, plus required secret **names** and OAuth scopes. It never contains secret values or tenant state.

The public preview additionally removes:

- secret variable names;
- provision install/uninstall operation IDs;
- internal health operation IDs;
- filesystem paths and tenant identifiers.

For a personal-secret connection the preview reports only `secret_required: true`.

## CLI

```bash
python3 modules/hermes-forge-kitchen/kitchen.py preview \
  --agent personal-hermes \
  --with-capability maton \
  --with-capability image-studio
```

`plan` prints the internal validated plan. `preview` prints the owner-safe projection. Neither command writes anything.
