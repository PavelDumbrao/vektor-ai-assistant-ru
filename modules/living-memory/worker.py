#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import pwd
import sys
import traceback
import uuid
from collections import Counter
from pathlib import Path

from core import (
    append_audit,
    apply_operations,
    batch_interactions,
    compile_summary,
    create_snapshot,
    expire_temporary,
    extract_interactions,
    living_dir,
    load_config,
    load_cursor,
    load_state,
    rollback_snapshot,
    scrub_forgotten_from_snapshots,
    save_cursor,
    save_state,
    state_metrics,
    sync_compiled_user_memory,
    validate_operations,
)
from provider import curate_batch, curate_batch_fallback, probe_routes, resolve_routes

CONTRACT_RETRYABLE_REASONS = frozenset({
    "invalid_enum_or_confidence", "no_valid_user_evidence",
    "pattern_needs_two_user_messages", "memory_id_not_active", "merge_ids_invalid",
})


def _should_contract_retry(accepted: list[dict], rejected: list[dict]) -> bool:
    return (
        not accepted
        and any(str(item.get("reason")) in CONTRACT_RETRYABLE_REASONS for item in rejected)
    )


def _assert_owner(owner: str) -> tuple[pwd.struct_passwd, Path]:
    entry = pwd.getpwnam(owner)
    home = Path(entry.pw_dir) / ".hermes"
    if os.geteuid() != entry.pw_uid:
        raise RuntimeError("living_memory_must_run_as_profile_owner")
    if not home.is_dir() or home.is_symlink() or home.stat().st_uid != entry.pw_uid:
        raise RuntimeError("living_memory_profile_home_unsafe")
    return entry, home


def _run_lock(home: Path):
    root = living_dir(home)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    path = root / "run.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError("living_memory_run_already_active")
    return fd


def _latest_snapshot(home: Path) -> Path:
    root = living_dir(home) / "snapshots"
    items = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if root.exists() else []
    if not items:
        raise RuntimeError("living_memory_snapshot_missing")
    return items[0]


def _status(owner: str, home: Path) -> int:
    state = load_state(home, owner)
    cursor = load_cursor(home)
    config = load_config(home)
    try:
        primary, fallback = resolve_routes(home, config)
        provider_ready = True
        primary_model = primary.get("model")
        fallback_model = fallback.get("model")
        different_route = primary.get("base_url") != fallback.get("base_url")
        different_key = primary.get("api_key") != fallback.get("api_key")
    except Exception:
        provider_ready = False
        primary_model = fallback_model = None
        different_route = different_key = False
    out = {
        "owner": owner,
        "version": state.get("version"),
        "memory": state_metrics(state),
        "last_scan_at": cursor.get("last_scan_at"),
        "provider_ready": provider_ready,
        "primary_model": primary_model,
        "fallback_model": fallback_model,
        "fallback_is_separate_route": different_route,
        "fallback_is_separate_key": different_key,
    }
    print(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    return 0


def run(owner: str, *, apply: bool, initial_hours: int = 24, max_batches: int = 0) -> int:
    _entry, home = _assert_owner(owner)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_PROFILE"] = owner
    lock_fd = _run_lock(home)
    run_id = "lm-" + uuid.uuid4().hex[:12]
    mode = "apply" if apply else "shadow"
    try:
        config = load_config(home)
        state = load_state(home, owner)
        cursor = load_cursor(home)
        before_metrics = state_metrics(state)
        messages = extract_interactions(
            home, config,
            last_message_id=int(cursor.get("last_message_id") or 0),
            initial_hours=initial_hours,
        )
        batches = batch_interactions(messages)
        if max_batches > 0:
            batches = batches[:max_batches]
        processed_messages = [m for batch in batches for m in batch]
        system_prompt = (Path(__file__).resolve().parent / "SYSTEM_PROMPT.md").read_text(encoding="utf-8")

        working = copy.deepcopy(state)
        baseline = copy.deepcopy(state)
        accepted_total: list[dict] = []
        rejected_total: list[dict] = []
        applied_total: list[dict] = []
        routes: list[dict] = []
        primary_failures = 0
        contract_retries = 0
        snapshot_path: Path | None = None

        expired = expire_temporary(working)
        for batch in batches:
            proposal, route, primary_error = curate_batch(home, config, working, batch, system_prompt)
            routes.append(route)
            if primary_error:
                primary_failures += 1
            accepted, rejected = validate_operations(proposal, working, batch)
            if _should_contract_retry(accepted, rejected):
                retry_proposal, retry_route = curate_batch_fallback(home, config, working, batch, system_prompt)
                routes.append(retry_route); contract_retries += 1
                retry_accepted, retry_rejected = validate_operations(retry_proposal, working, batch)
                if _should_contract_retry(retry_accepted, retry_rejected):
                    raise RuntimeError("living_memory_contract_validation_failed")
                accepted, rejected = retry_accepted, retry_rejected
            accepted_total.extend(accepted)
            rejected_total.extend(rejected)
            if apply:
                if snapshot_path is None and any(op.get("action") != "noop" for op in accepted):
                    snapshot_path = create_snapshot(home, baseline, run_id)
                applied_total.extend(apply_operations(working, accepted))

        highest_id = max([int(m["message_id"]) for m in processed_messages], default=int(cursor.get("last_message_id") or 0))
        if apply and (accepted_total or expired):
            if snapshot_path is None:
                snapshot_path = create_snapshot(home, baseline, run_id)
            forgotten = [x for x in applied_total if x.get("action") == "forget"]
            if forgotten:
                scrub_forgotten_from_snapshots(home, forgotten)
            save_state(home, working)
            sync_compiled_user_memory(home, compile_summary(working))
        if apply and processed_messages:
            # Cursor advances only through messages that were actually curated.
            save_cursor(home, highest_id)

        reason_counts = Counter(str(x.get("reason") or "unknown") for x in rejected_total)
        after_metrics = state_metrics(working if apply else state)
        audit = {
            "run_id": run_id,
            "mode": mode,
            "owner": owner,
            "messages_scanned": len(messages),
            "batches": len(batches),
            "accepted_operations": len(accepted_total),
            "rejected_operations": len(rejected_total),
            "rejection_reasons": dict(reason_counts),
            "expired_temporary": expired,
            "primary_failures": primary_failures,
            "contract_retries": contract_retries,
            "routes": routes,
            "before": before_metrics,
            "after": after_metrics,
            "cursor_advanced": bool(apply and processed_messages),
            "snapshot_created": snapshot_path is not None,
        }
        append_audit(home, audit)
        print(json.dumps({
            "ok": True,
            "run_id": run_id,
            "mode": mode,
            "messages_scanned": len(messages),
            "batches": len(batches),
            "changes_accepted": len([x for x in accepted_total if x.get("action") != "noop"]),
            "changes_rejected": len(rejected_total),
            "primary_failures": primary_failures,
            "contract_retries": contract_retries,
            "memory": after_metrics,
        }, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        try:
            append_audit(home, {
                "run_id": run_id, "mode": mode, "owner": owner,
                "ok": False, "error_type": type(exc).__name__,
            })
        except Exception:
            pass
        print(json.dumps({"ok": False, "run_id": run_id, "error": type(exc).__name__}, separators=(",", ":")), file=sys.stderr)
        return 1
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes Living Memory daily curator")
    parser.add_argument("--owner", required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--shadow", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--provider-smoke", action="store_true")
    group.add_argument("--rollback-latest", action="store_true")
    parser.add_argument("--initial-hours", type=int, default=24)
    parser.add_argument("--max-batches", type=int, default=0)
    args = parser.parse_args()
    _entry, home = _assert_owner(args.owner)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_PROFILE"] = args.owner
    if args.status:
        return _status(args.owner, home)
    if args.provider_smoke:
        config = load_config(home)
        system_prompt = (Path(__file__).resolve().parent / "SYSTEM_PROMPT.md").read_text(encoding="utf-8")
        results = probe_routes(home, config, system_prompt)
        ok = all(x.get("ok") for x in results)
        print(json.dumps({"ok": ok, "routes": results}, separators=(",", ":")))
        return 0 if ok else 1
    if args.rollback_latest:
        path = _latest_snapshot(home)
        rollback_snapshot(home, args.owner, path)
        print(json.dumps({"ok": True, "rolled_back": True, "snapshot": path.name}, separators=(",", ":")))
        return 0
    return run(args.owner, apply=bool(args.apply and not args.shadow), initial_hours=max(1, min(args.initial_hours, 168)), max_batches=max(0, args.max_batches))


if __name__ == "__main__":
    raise SystemExit(main())
