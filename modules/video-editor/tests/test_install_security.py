from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "video_editor_install_security", MODULE / "install.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_profile_parent_directory_rejects_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(RuntimeError, match="profile_directory_unsafe"):
        installer.safe_owned_dir(link, os.getuid(), os.getgid())


def test_profile_config_rejects_symlink_and_group_write(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("plugins: {}\n")
    config.chmod(0o600)
    assert installer.safe_owned_file(config, os.getuid()) == config
    config.chmod(0o620)
    with pytest.raises(RuntimeError, match="profile_file_unsafe"):
        installer.safe_owned_file(config, os.getuid())
    config.chmod(0o600)
    link = tmp_path / "config-link.yaml"
    link.symlink_to(config)
    with pytest.raises(RuntimeError, match="profile_file_unsafe"):
        installer.safe_owned_file(link, os.getuid())


def test_profile_name_is_bounded():
    assert installer.PROFILE_RE.fullmatch("pavel")
    assert installer.PROFILE_RE.fullmatch("client_01")
    assert not installer.PROFILE_RE.fullmatch("../root")
    assert not installer.PROFILE_RE.fullmatch("Pavel")
