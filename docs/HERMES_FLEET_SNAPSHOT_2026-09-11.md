# Hermes Fleet Snapshot — 2026-09-11

This document records the observed production state used to reconcile GitHub with the live Hermes fleet. It is metadata-only: no tenant secrets, messages, prompts, tool arguments, or tool results are included.

## Source baseline

- GitHub base: `50e1f75b0001202ae1ff28d318c784fe8ac2f64b`
- Base includes PR #61, the local privacy-safe SharedMetrics exporter source.
- Open pull requests at reconciliation start: none.
- Reconciliation intentionally did not restart, pause, reconfigure, or switch any Hermes runtime.

## Verified live releases

Every release below has a live `runtime.json` satisfying all three release gates: `state=ready`, exact `release_id`, and `schema_rollback_compatible=true`.

| Profile | Live release |
| --- | --- |
| pavel | `hermes-0.21.0-29112bef-vektor3-pavel-tglf2-vtm1-tgrel8` |
| baysangur | `hermes-0.21.0-29112bef-vektor3-baysangur-tglf1-vtm1-tgrel2` |
| vyacheslav | `hermes-0.21.0-29112bef-vektor3-vyacheslav-tglf1-vtm1-tgrel2` |
| bebov | `hermes-0.21.0-29112bef-vektor4-modern-tglf1-vtm1-tgrel2` |
| salavat | `hermes-0.21.0-29112bef-vektor3-vyacheslav-tglf1-vtm1-tgrel2` |

## Runtime health observed

At reconciliation time all five Hermes systemd services were `active`.

No runtime mutation was performed because concurrent tenant work was visible: a live `hermes_kernel_runner.py` process existed for `vyacheslav`. Bebov also had recent retryable passive-media `status=500` warnings in its journal, so its process was deliberately left untouched.

## Privacy-safe analytics observed

Central Forge analytics already contained real aggregate data:

- metric packages: `10`
- metric counter rows: `132`
- health profiles: `5`
- `hermes.model_route.count`: `1147`
- `hermes.task_run.started`: `905`
- `hermes.task_run.finished`: `757`
- `hermes.tool_call.count`: `259`
- `hermes.skill.load.count`: `18`
- `hermes.client.active`: `5`
- `hermes.tool_approval.count`: `2`

Packages were present for `pavel` (4), `vyacheslav` (3), and `bebov` (3). These counters are the bounded SharedMetrics contract and do not contain conversation text.

## Known reconciliation drift

The live fleet policy is still pinned to the pre-`tgrel` release IDs for all five profiles. Drift count at snapshot time: `5`.

This snapshot does **not** reconcile that live policy. Pinned channels are observe-only, and concurrent Hermes work is in progress. Policy reconciliation must be a separate operational gate after the active client work is clear.

## SharedMetrics delivery state

PR #61 is now present in GitHub `main`, but the new `proai-hermes-shared-metrics-*` systemd units were not installed on the VPS at snapshot time. Existing central analytics are already populated, so no duplicate exporter deployment was attempted during reconciliation.

## Reconciliation scope

This snapshot updates GitHub metadata to the verified live release IDs only. It does not assert that every operational process is idle, does not modify fleet policy, and does not deploy any service.

Next operational work should begin only after a fresh runtime-idle/concurrent-work check and a fresh comparison against GitHub `main`.
