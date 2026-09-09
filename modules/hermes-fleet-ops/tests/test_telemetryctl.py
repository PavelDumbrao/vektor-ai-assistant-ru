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
