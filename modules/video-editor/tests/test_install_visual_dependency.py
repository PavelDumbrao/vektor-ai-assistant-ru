from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("video_editor_install_test", MODULE / "install.py")
installer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(installer)


def _runtime_python(tmp_path: Path) -> tuple[Path, Path]:
    hermes = tmp_path / ".hermes"
    python = hermes / "hermes-agent" / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    return hermes, python


def test_pillow_preflight_uses_profile_runtime_python(tmp_path, monkeypatch):
    hermes, python = _runtime_python(tmp_path)
    seen = []
    def fake_run(args, **kwargs):
        seen.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    installer.require_profile_pillow(hermes)
    assert seen[0][0][0] == str(python)
    assert "from PIL import" in seen[0][0][2]
    assert seen[0][1]["timeout"] == 15


def test_pillow_preflight_fails_closed_when_import_fails(tmp_path, monkeypatch):
    hermes, _ = _runtime_python(tmp_path)
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1),
    )
    with pytest.raises(RuntimeError, match="hermes_pillow_missing"):
        installer.require_profile_pillow(hermes)


def test_pillow_preflight_requires_runtime_python(tmp_path):
    with pytest.raises(RuntimeError, match="hermes_runtime_python_missing"):
        installer.require_profile_pillow(tmp_path / ".hermes")
