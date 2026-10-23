#!/usr/bin/env python3
"""Tests for offboard_purge module."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import offboard_purge
from offboard_purge import PurgePlan, is_safe, is_valid_owner, plan, purge


class RecordingRunner:
    """Mock runner that logs commands and returns configurable responses."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.call_kwargs: list[dict[str, Any]] = []
        self.responses = responses or {}

    def __call__(self, cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        cmd_list = list(cmd)
        self.calls.append(cmd_list)
        self.call_kwargs.append(kwargs)
        cmd_str = " ".join(cmd_list)

        for key, resp in self.responses.items():
            if key in cmd_str:
                if isinstance(resp, Exception):
                    raise resp
                if isinstance(resp, int):
                    return subprocess.CompletedProcess(cmd_list, returncode=resp, stdout="", stderr="")
                return resp

        # Default behaviors:
        # systemctl is-active -> returncode=3 (inactive)
        if len(cmd_list) >= 2 and cmd_list[0] == "systemctl" and cmd_list[1] == "is-active":
            return subprocess.CompletedProcess(cmd_list, returncode=3, stdout="inactive\n", stderr="")

        # pgrep -u -> returncode=1 (no processes)
        if len(cmd_list) >= 2 and cmd_list[0] == "pgrep":
            return subprocess.CompletedProcess(cmd_list, returncode=1, stdout="", stderr="")

        # docker exec ... psql -> returncode=0, stdout="1\n" (database exists)
        if "psql" in cmd_list:
            return subprocess.CompletedProcess(cmd_list, returncode=0, stdout="1\n", stderr="")

        return subprocess.CompletedProcess(cmd_list, returncode=0, stdout="", stderr="")


def make_pwnam(owner: str, home_root: Path, uid: int = 1001) -> Any:
    return lambda u: SimpleNamespace(
        pw_name=u,
        pw_uid=uid,
        pw_gid=uid,
        pw_dir=str(home_root / u),
    )


def test_invalid_owner_names_rejected_without_runner(tmp_path: Path) -> None:
    """Invalid names ('../x', 'root', empty, etc.) must fail immediately without invoking runner."""
    runner = RecordingRunner()
    invalid_names = ["../x", "root", "", None, 123, "-invalid", "bad/name", "bad;cmd", "x" * 41]

    for name in invalid_names:
        assert not is_valid_owner(name)

        # purge must reject upfront with no runner calls
        initial_call_count = len(runner.calls)
        res = purge(name, runner=runner, home_root=tmp_path / "home")
        assert res["ok"] is False
        assert res["failed_step"] == "validate_owner"
        assert len(runner.calls) == initial_call_count

        # plan must raise ValueError
        with pytest.raises(ValueError):
            plan(name, home_root=tmp_path / "home")


def test_is_safe_active_unit(tmp_path: Path) -> None:
    """is_safe must fail if <owner>-hermes.service is active."""
    owner = "h503899482"
    home_root = tmp_path / "home"
    (home_root / owner).mkdir(parents=True, exist_ok=True)
    p = plan(owner, home_root=home_root)

    # Return returncode=0 for systemctl is-active (unit is active)
    runner = RecordingRunner(responses={"is-active": subprocess.CompletedProcess(["systemctl"], 0, stdout="active\n")})
    fake_pwnam = make_pwnam(owner, home_root, uid=1001)

    safe, detail = is_safe(p, runner=runner, getpwnam=fake_pwnam, home_root=home_root)
    assert safe is False
    assert "unit_active" in detail


def test_is_safe_home_is_symlink(tmp_path: Path) -> None:
    """is_safe must fail if home directory is a symlink."""
    owner = "h503899482"
    home_root = tmp_path / "home"
    home_root.mkdir(parents=True, exist_ok=True)
    target_dir = tmp_path / "other_target"
    target_dir.mkdir(parents=True, exist_ok=True)

    home_symlink = home_root / owner
    home_symlink.symlink_to(target_dir)

    p = plan(owner, home_root=home_root)
    runner = RecordingRunner()
    fake_pwnam = make_pwnam(owner, home_root, uid=1001)

    safe, detail = is_safe(p, runner=runner, getpwnam=fake_pwnam, home_root=home_root)
    assert safe is False
    assert "home_is_symlink" in detail


def test_is_safe_uid_too_low(tmp_path: Path) -> None:
    """is_safe must fail if owner uid < 1000."""
    owner = "h503899482"
    home_root = tmp_path / "home"
    (home_root / owner).mkdir(parents=True, exist_ok=True)
    p = plan(owner, home_root=home_root)
    runner = RecordingRunner()

    for bad_uid in (0, 999):
        fake_pwnam = make_pwnam(owner, home_root, uid=bad_uid)
        safe, detail = is_safe(p, runner=runner, getpwnam=fake_pwnam, home_root=home_root)
        assert safe is False
        assert "uid_too_low" in detail


def test_is_safe_user_missing(tmp_path: Path) -> None:
    """is_safe must fail if owner does not exist in system."""
    owner = "h503899482"
    home_root = tmp_path / "home"
    (home_root / owner).mkdir(parents=True, exist_ok=True)
    p = plan(owner, home_root=home_root)
    runner = RecordingRunner()

    def missing_pwnam(u: str) -> None:
        raise KeyError(f"getpwnam: name not found: {u}")

    safe, detail = is_safe(p, runner=runner, getpwnam=missing_pwnam, home_root=home_root)
    assert safe is False
    assert "owner_missing" in detail


def test_is_safe_user_processes_running(tmp_path: Path) -> None:
    """is_safe must fail if processes for the owner are running."""
    owner = "h503899482"
    home_root = tmp_path / "home"
    (home_root / owner).mkdir(parents=True, exist_ok=True)
    p = plan(owner, home_root=home_root)
    fake_pwnam = make_pwnam(owner, home_root, uid=1001)

    # pgrep returning 0 means matching processes exist
    runner = RecordingRunner(responses={"pgrep": subprocess.CompletedProcess(["pgrep"], 0, stdout="1234\n")})
    safe, detail = is_safe(p, runner=runner, getpwnam=fake_pwnam, home_root=home_root)
    assert safe is False
    assert "user_processes_running" in detail


def test_full_purge_fixture_and_command_order(tmp_path: Path) -> None:
    """Full purge on fixture: checks command order (archive -> units -> lanectl -> dropdb -> userdel -> registry),

    verifies fleet policy update with .bak and deletion of root-state files.
    """
    owner = "h503899482"
    home_root = tmp_path / "home"
    owner_home = home_root / owner
    owner_home.mkdir(parents=True, exist_ok=True)
    (owner_home / ".hermes").mkdir()
    (owner_home / ".hermes" / "config.yaml").write_text("dummy", encoding="utf-8")

    backup_root = tmp_path / "backups"
    systemd_root = tmp_path / "systemd"
    systemd_control_root = tmp_path / "systemd.control"
    profile_root = tmp_path / "profiles"
    policy_path = tmp_path / "fleet-policy.json"
    forge_state_root = tmp_path / "forge-state"

    systemd_root.mkdir(parents=True, exist_ok=True)
    systemd_control_root.mkdir(parents=True, exist_ok=True)
    profile_root.mkdir(parents=True, exist_ok=True)
    forge_state_root.mkdir(parents=True, exist_ok=True)

    # Create dummy unit files and drop-in dirs
    unit_service = systemd_root / f"{owner}-hermes.service"
    unit_service.write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")

    unit_timer = systemd_root / f"{owner}-hermes-passive-secretary-retention.timer"
    unit_timer.write_text("[Timer]\nOnCalendar=daily\n", encoding="utf-8")

    dropin_dir = systemd_root / f"{owner}-hermes.service.d"
    dropin_dir.mkdir(parents=True, exist_ok=True)
    (dropin_dir / "override.conf").write_text("[Service]\nRestart=always\n", encoding="utf-8")

    control_dropin_dir = systemd_control_root / f"{owner}-hermes.service.d"
    control_dropin_dir.mkdir(parents=True, exist_ok=True)
    (control_dropin_dir / "50-CPUAccounting.conf").write_text("[Service]\nCPUAccounting=yes\n", encoding="utf-8")

    # Create profile json
    profile_file = profile_root / f"{owner}.json"
    profile_file.write_text(json.dumps({"owner": owner, "version": "1.0"}), encoding="utf-8")

    # Create fleet policy
    initial_policy = {
        "tracks": {"stable": {"release_id": "hermes-0.21.0"}},
        "profiles": {
            owner: {"track": "stable", "channel": "stable"},
            "h999000000": {"track": "stable", "channel": "canary"},
        },
    }
    policy_path.write_text(json.dumps(initial_policy, indent=2), encoding="utf-8")

    # Create forge root state files
    forge_files = []
    for cat in ("workspace-members", "workspace-invites", "zoom-bindings", "desired"):
        cat_dir = forge_state_root / cat
        cat_dir.mkdir(parents=True, exist_ok=True)
        fpath = cat_dir / f"{owner}.json"
        fpath.write_text(json.dumps({"owner": owner}), encoding="utf-8")
        forge_files.append(fpath)

    runner = RecordingRunner()
    fake_pwnam = make_pwnam(owner, home_root, uid=1001)

    res = purge(
        owner,
        backup_root=backup_root,
        home_root=home_root,
        systemd_root=systemd_root,
        systemd_control_root=systemd_control_root,
        profile_root=profile_root,
        policy_path=policy_path,
        forge_state_root=forge_state_root,
        runner=runner,
        getpwnam=fake_pwnam,
        now=1700000000,
    )

    # 1. Purge result structure
    assert res["ok"] is True
    assert res["owner"] == owner
    assert res["purged_at"] == 1700000000
    assert "archive" in res
    assert res["archive"].endswith(".tar.zst")
    assert backup_root.exists()
    assert (backup_root.stat().st_mode & 0o777) == 0o700

    # 2. Command sequence verification: archive -> units -> lanectl -> dropdb -> userdel
    cmds = runner.calls
    tar_idx = next(i for i, c in enumerate(cmds) if c[0] == "tar")
    psql_idx = next(i for i, c in enumerate(cmds) if "psql" in c)
    pgdump_idx = next(i for i, c in enumerate(cmds) if "pg_dump" in c)
    disable_indices = [i for i, c in enumerate(cmds) if len(c) >= 2 and c[0] == "systemctl" and c[1] == "disable"]
    daemon_reload_idx = next(i for i, c in enumerate(cmds) if c == ["systemctl", "daemon-reload"])
    reset_failed_idx = next(i for i, c in enumerate(cmds) if c == ["systemctl", "reset-failed"])
    lanectl_idx = next(i for i, c in enumerate(cmds) if c[0].endswith("lanectl") and "remove" in c)
    dropdb_idx = next(i for i, c in enumerate(cmds) if "dropdb" in c)
    userdel_idx = next(i for i, c in enumerate(cmds) if c[0].endswith("userdel") and "-r" in c)

    # Ordering assertions:
    # archive commands
    assert tar_idx < psql_idx < pgdump_idx
    # archive must complete before units disabling
    assert pgdump_idx < disable_indices[0]
    # units disable -> daemon-reload -> reset-failed
    assert disable_indices[-1] < daemon_reload_idx < reset_failed_idx
    # units -> lanectl
    assert reset_failed_idx < lanectl_idx
    # lanectl -> dropdb
    assert lanectl_idx < dropdb_idx
    # dropdb -> userdel
    assert dropdb_idx < userdel_idx

    # 3. Unit files and drop-in dirs removed
    assert not unit_service.exists()
    assert not unit_timer.exists()
    assert not dropin_dir.exists()
    assert not control_dropin_dir.exists()

    # 4. Fleet policy updated with .bak backup
    bak_policy_path = policy_path.with_suffix(policy_path.suffix + ".bak")
    assert bak_policy_path.exists()
    bak_content = json.loads(bak_policy_path.read_text(encoding="utf-8"))
    assert owner in bak_content["profiles"]

    new_policy = json.loads(policy_path.read_text(encoding="utf-8"))
    assert owner not in new_policy["profiles"]
    assert "h999000000" in new_policy["profiles"]  # other profile untouched

    # 5. Profile registry and forge state files removed
    assert not profile_file.exists()
    for fpath in forge_files:
        assert not fpath.exists()


def test_purge_stops_on_archive_failure(tmp_path: Path) -> None:
    """When archive creation fails, further destructive actions must NOT be executed."""
    owner = "h503899482"
    home_root = tmp_path / "home"
    (home_root / owner).mkdir(parents=True, exist_ok=True)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({"profiles": {owner: {}}}), encoding="utf-8")
    profile_json = tmp_path / f"{owner}.json"
    profile_json.write_text("{}", encoding="utf-8")

    # Simulate tar failing with error code 2
    runner = RecordingRunner(responses={"tar": subprocess.CompletedProcess(["tar"], returncode=2, stderr="disk full")})
    fake_pwnam = make_pwnam(owner, home_root, uid=1001)

    res = purge(
        owner,
        home_root=home_root,
        policy_path=policy_path,
        profile_root=tmp_path,
        runner=runner,
        getpwnam=fake_pwnam,
    )

    assert res["ok"] is False
    assert res["failed_step"] == "archive"

    # Verify that NO subsequent destructive commands were run:
    for call in runner.calls:
        assert "disable" not in call
        assert "lanectl" not in call
        assert "dropdb" not in call
        assert "userdel" not in call

    # Policy and profile json must remain untouched
    assert policy_path.exists()
    assert owner in json.loads(policy_path.read_text(encoding="utf-8"))["profiles"]
    assert profile_json.exists()


def test_dry_run_executes_nothing_and_returns_plan(tmp_path: Path) -> None:
    """dry_run=True must execute NO commands, modify nothing, and return PurgePlan."""
    owner = "h503899482"
    home_root = tmp_path / "home"
    (home_root / owner).mkdir(parents=True, exist_ok=True)
    runner = RecordingRunner()

    res = purge(owner, dry_run=True, runner=runner, home_root=home_root)

    assert res["ok"] is True
    assert res["dry_run"] is True
    assert isinstance(res["plan"], PurgePlan)
    assert res["plan"].owner == owner
    assert len(runner.calls) == 0


def test_mutation_archive_before_destructive_actions(tmp_path: Path) -> None:
    """Mutation sensitivity test: assertions must catch if any destructive action is placed before archive."""
    owner = "h503899482"
    home_root = tmp_path / "home"
    (home_root / owner).mkdir(parents=True, exist_ok=True)
    runner = RecordingRunner()
    fake_pwnam = make_pwnam(owner, home_root, uid=1001)

    res = purge(owner, runner=runner, home_root=home_root, getpwnam=fake_pwnam)
    assert res["ok"] is True

    # Check ordering invariant:
    tar_indices = [i for i, c in enumerate(runner.calls) if c[0] == "tar"]
    destructive_indices = [
        i for i, c in enumerate(runner.calls)
        if any(token in c for token in ("disable", "dropdb", "lanectl"))
        or any(token.endswith("userdel") for token in c)
    ]
    assert tar_indices, "tar command missing"
    assert destructive_indices, "destructive commands missing"
    assert max(tar_indices) < min(destructive_indices), "Archive MUST precede all destructive actions"

    # Synthetic mutation test: if order were inverted, assertion must fail
    mutated_calls = list(reversed(runner.calls))
    mutated_tar = [i for i, c in enumerate(mutated_calls) if c[0] == "tar"]
    mutated_destructive = [
        i for i, c in enumerate(mutated_calls)
        if any(token in c for token in ("disable", "dropdb", "lanectl"))
        or any(token.endswith("userdel") for token in c)
    ]
    with pytest.raises(AssertionError):
        assert max(mutated_tar) < min(mutated_destructive)


def test_userdel_residual_home_cleanup(tmp_path: Path) -> None:
    """If userdel leaves residual home dir, shutil.rmtree removes it if safe."""
    owner = "h503899482"
    home_root = tmp_path / "home"
    owner_home = home_root / owner
    owner_home.mkdir(parents=True, exist_ok=True)
    (owner_home / "leftover.txt").write_text("residual", encoding="utf-8")

    runner = RecordingRunner()
    fake_pwnam = make_pwnam(owner, home_root, uid=1001)

    res = purge(owner, home_root=home_root, runner=runner, getpwnam=fake_pwnam)
    assert res["ok"] is True
    assert not owner_home.exists()


def test_no_shell_true_in_source() -> None:
    """Source code audit: no shell=True or os.system calls allowed."""
    source = (ROOT / "offboard_purge.py").read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "os.system(" not in source


def test_cli_interface() -> None:
    """CLI build_parser and commands sanity check."""
    parser = offboard_purge.build_parser()
    args_plan = parser.parse_args(["plan", "h503899482"])
    assert args_plan.command == "plan"
    assert args_plan.owner == "h503899482"

    args_purge = parser.parse_args(["purge", "h503899482", "--dry-run"])
    assert args_purge.command == "purge"
    assert args_purge.owner == "h503899482"
    assert args_purge.dry_run is True
