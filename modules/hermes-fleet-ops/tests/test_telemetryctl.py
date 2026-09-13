from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
import telemetryctl


def test_enable_changes_only_bounded_telemetry_setting(monkeypatch, tmp_path):
    profile_root = tmp_path / "profiles"
    profile_root.mkdir()
    (profile_root / "testowner.json").write_text("{}")
    home = tmp_path / "home"
    hermes = home / ".hermes"
    (hermes / "backups").mkdir(parents=True)
    config = hermes / "config.yaml"
    config.write_text("model:\n  default: test-model\nsecret_marker: keep-me\n")
    monkeypatch.setattr(telemetryctl, "PROFILE_ROOT", profile_root)
    monkeypatch.setattr(telemetryctl.os, "geteuid", lambda: 0)
    monkeypatch.setattr(telemetryctl.pwd, "getpwnam", lambda _owner: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(home)))
    result = telemetryctl.configure("testowner", True)
    loaded = yaml.safe_load(config.read_text())
    assert result["changed"] is True
    assert loaded["telemetry"]["shared_metrics"]["enabled"] is True
    assert loaded["model"]["default"] == "test-model"
    assert loaded["secret_marker"] == "keep-me"
    assert Path(result["backup"]).joinpath("config.yaml").is_file()


def _profile_fixture(monkeypatch, tmp_path, config_text: str):
    profile_root = tmp_path / "profiles"
    profile_root.mkdir()
    (profile_root / "testowner.json").write_text("{}")
    home = tmp_path / "home"
    hermes = home / ".hermes"
    (hermes / "backups").mkdir(parents=True)
    config = hermes / "config.yaml"
    config.write_text(config_text)
    config.chmod(0o600)
    monkeypatch.setattr(telemetryctl, "PROFILE_ROOT", profile_root)
    monkeypatch.setattr(telemetryctl.os, "geteuid", lambda: 0)
    monkeypatch.setattr(telemetryctl.pwd, "getpwnam", lambda _owner: SimpleNamespace(
        pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(home)))
    return hermes, config


def test_idempotent_enable_prepares_private_storage(monkeypatch, tmp_path):
    hermes, _config = _profile_fixture(
        monkeypatch, tmp_path, "telemetry:\n  shared_metrics:\n    enabled: true\n")
    result = telemetryctl.configure("testowner", True)
    assert result == {"owner": "testowner", "enabled": True, "changed": False, "restart_required": False}
    for relative in ("telemetry", "telemetry/shared_metrics", "telemetry/shared_metrics/outbox"):
        path = hermes / relative
        assert path.is_dir() and not path.is_symlink()
        assert path.stat().st_uid == os.getuid()
        assert path.stat().st_mode & 0o077 == 0


def test_enable_rejects_symlinked_telemetry_directory(monkeypatch, tmp_path):
    hermes, _config = _profile_fixture(monkeypatch, tmp_path, "{}\n")
    target = tmp_path / "foreign"
    target.mkdir()
    (hermes / "telemetry").symlink_to(target)
    import pytest
    with pytest.raises(ValueError, match="telemetry_directory_unsafe"):
        telemetryctl.configure("testowner", True)


def test_disable_does_not_create_telemetry_storage(monkeypatch, tmp_path):
    hermes, _config = _profile_fixture(
        monkeypatch, tmp_path, "telemetry:\n  shared_metrics:\n    enabled: false\n")
    result = telemetryctl.configure("testowner", False)
    assert result["changed"] is False
    assert not (hermes / "telemetry").exists()
