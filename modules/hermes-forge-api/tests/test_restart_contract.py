from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTROL_SPEC = importlib.util.spec_from_file_location("forge_restart_control", ROOT / "control.py")
control = importlib.util.module_from_spec(CONTROL_SPEC)
assert CONTROL_SPEC.loader is not None
CONTROL_SPEC.loader.exec_module(control)

INSTALL_SPEC = importlib.util.spec_from_file_location("forge_restart_install", ROOT / "install_restart_contract.py")
installer = importlib.util.module_from_spec(INSTALL_SPEC)
assert INSTALL_SPEC.loader is not None
INSTALL_SPEC.loader.exec_module(installer)

def test_restart_uses_native_sigusr1_reload_and_waits_for_new_pid(monkeypatch):
    health = iter([
        {"healthy": True, "active_agents": 0},
        {"healthy": True, "active_agents": 0},
    ])
    gateway = iter([{"pid": 111}, {"pid": 222}])
    properties = []

    monkeypatch.setattr(control, "_health", lambda _profile: next(health))
    monkeypatch.setattr(control, "_active_sessions", lambda _profile: 0)
    monkeypatch.setattr(control, "_safe_json", lambda _path: next(gateway))

    def props(_service, names):
        properties.append(names)
        if names == ("MainPID",):
            return {"MainPID": "222"}
        return {
            "MainPID": "111", "ExecReload": "/bin/kill -USR1 $MAINPID",
            "SuccessExitStatus": "75", "RestartForceExitStatus": "75",
        }

    monkeypatch.setattr(control, "_systemd_properties", props)
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(control.subprocess, "run", run)
    result = control._restart("pavel")
    assert result["healthy"] is True
    assert calls[0][0] == ["/usr/bin/systemctl", "reload", "pavel-hermes.service"]
    assert all("restart" not in call[0] for call in calls)
    assert ("MainPID",) in properties


def test_restart_fails_closed_without_planned_restart_contract(monkeypatch):
    monkeypatch.setattr(control, "_health", lambda _profile: {"healthy": True, "active_agents": 0})
    monkeypatch.setattr(control, "_active_sessions", lambda _profile: 0)
    monkeypatch.setattr(control, "_safe_json", lambda _path: {"pid": 111})
    monkeypatch.setattr(control, "_systemd_properties", lambda *_: {
        "MainPID": "111", "ExecReload": "/bin/kill -USR1 $MAINPID",
        "SuccessExitStatus": "", "RestartForceExitStatus": "75",
    })
    monkeypatch.setattr(control.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("reload must not run")))
    with pytest.raises(control.ControlError, match="planned_restart_contract_missing"):
        control._restart("pavel")

def test_existing_fleet_migration_is_dry_run_then_idempotent(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    systemd = tmp_path / "systemd"
    backups = tmp_path / "backups"
    profiles.mkdir()
    systemd.mkdir()
    backups.mkdir()
    for owner in ("pavel", "bebov"):
        (profiles / f"{owner}.json").write_text(json.dumps({"owner": owner}), encoding="utf-8")
        (systemd / f"{owner}-hermes.service").write_text("[Service]\nExecReload=/bin/kill -USR1 $MAINPID\nRestartForceExitStatus=75\n", encoding="utf-8")

    monkeypatch.setattr(installer, "PROFILE_ROOT", profiles)
    monkeypatch.setattr(installer, "SYSTEMD_ROOT", systemd)
    monkeypatch.setattr(installer, "BACKUP_ROOT", backups)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.os, "chown", lambda *_args: None)
    calls = []

    def fake_properties(owner):
        dropin = systemd / f"{owner}-hermes.service.d" / installer.DROPIN_NAME
        return {
            "ExecReload": "/bin/kill -USR1 $MAINPID",
            "RestartForceExitStatus": "75",
            "SuccessExitStatus": "75" if dropin.exists() else "",
        }

    monkeypatch.setattr(installer, "properties", fake_properties)
    monkeypatch.setattr(installer, "run", lambda *args, **_kwargs: calls.append(args) or SimpleNamespace(stdout=""))
    dry = installer.install(False)
    assert dry["applied"] is False
    assert not list(systemd.glob("*.service.d/*"))

    applied = installer.install(True)
    assert applied["applied"] is True
    assert applied["changed"] == 2
    for owner in ("pavel", "bebov"):
        dropin = systemd / f"{owner}-hermes.service.d" / installer.DROPIN_NAME
        assert dropin.read_text() == installer.DROPIN_TEXT
    assert any(call[1:] == ("daemon-reload",) for call in calls)

    calls.clear()
    second = installer.install(True)
    assert second["changed"] == 0
    assert any(call[1:] == ("daemon-reload",) for call in calls)


def test_canonical_forge_templates_mark_exit75_as_success():
    templates = [
        ROOT.parents[1] / "config/hermes.service.template",
        ROOT.parents[1] / "server/hermes.service.template",
        ROOT.parent / "hermes-bot-manager/templates/hermes.service.template",
    ]
    for path in templates:
        text = path.read_text(encoding="utf-8")
        assert "SuccessExitStatus=75" in text
        assert "RestartForceExitStatus=75" in text
        assert "ExecReload=/bin/kill -USR1 $MAINPID" in text


def test_existing_fleet_migration_rolls_back_partial_apply(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    systemd = tmp_path / "systemd"
    backups = tmp_path / "backups"
    profiles.mkdir()
    systemd.mkdir()
    backups.mkdir()
    for owner in ("pavel", "bebov"):
        (profiles / f"{owner}.json").write_text(json.dumps({"owner": owner}), encoding="utf-8")
        (systemd / f"{owner}-hermes.service").write_text("[Service]\nExecReload=/bin/kill -USR1 $MAINPID\nRestartForceExitStatus=75\n", encoding="utf-8")
    monkeypatch.setattr(installer, "PROFILE_ROOT", profiles)
    monkeypatch.setattr(installer, "SYSTEMD_ROOT", systemd)
    monkeypatch.setattr(installer, "BACKUP_ROOT", backups)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.os, "chown", lambda *_args: None)
    monkeypatch.setattr(installer, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=""))

    def fake_properties(owner):
        dropin = systemd / f"{owner}-hermes.service.d" / installer.DROPIN_NAME
        success = dropin.exists() and owner == "pavel"
        return {
            "ExecReload": "/bin/kill -USR1 $MAINPID",
            "RestartForceExitStatus": "75",
            "SuccessExitStatus": "75" if success else "",
        }

    monkeypatch.setattr(installer, "properties", fake_properties)
    with pytest.raises(RuntimeError, match="restart_contract_verify_failed"):
        installer.install(True)
    for owner in ("pavel", "bebov"):
        dropin = systemd / f"{owner}-hermes.service.d" / installer.DROPIN_NAME
        assert not dropin.exists()
