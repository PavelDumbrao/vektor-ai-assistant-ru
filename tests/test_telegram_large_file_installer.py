import importlib.util
import os
import pwd
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "modules/telegram-large-file/install.py"
spec = importlib.util.spec_from_file_location("telegram_large_file_install", MODULE)
installer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(installer)


def _acl_text(path: Path) -> str:
    result = subprocess.run(
        ["getfacl", "-cp", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_dropin_contains_no_shared_group_or_token():
    owner = "alice"
    tenant_hash = "a" * 64
    text = installer._dropin_content(owner, tenant_hash).decode()
    assert "SupplementaryGroups=" not in text
    assert "SupplementaryGroups=telegram-transcriber" not in text
    assert "BindReadOnlyPaths=" in text
    assert "InaccessiblePaths=/opt/telegram-transcriber-bot" in text
    assert tenant_hash in text
    assert "0000000000:" not in text


def test_acl_access_default_and_restore(tmp_path):
    if not shutil.which("setfacl") or not shutil.which("getfacl"):
        pytest.skip("acl tools unavailable")
    nobody = str(pwd.getpwnam("nobody").pw_uid)
    tenant = tmp_path / "tenant"
    media = tenant / "videos"
    media.mkdir(parents=True)
    source = media / "clip.mp4"
    source.write_bytes(b"video")
    tenant.chmod(0o750)
    media.chmod(0o750)
    source.chmod(0o640)

    before = installer._snapshot_acl(tenant)
    installer._apply_tenant_acl(nobody, tenant)

    tenant_acl = _acl_text(tenant)
    media_acl = _acl_text(media)
    source_acl = _acl_text(source)
    assert "default:user:nobody:r-x" in tenant_acl
    assert "default:user:nobody:r-x" in media_acl
    assert "user:nobody:r--" in source_acl

    inherited = media / "new.bin"
    inherited.write_bytes(b"new")
    inherited.chmod(0o640)
    inherited_acl = _acl_text(inherited)
    assert "user:nobody:r-x" in inherited_acl
    assert "mask::r--" in inherited_acl

    installer._restore_acl(before)
    assert "user:nobody:" not in _acl_text(tenant)
    assert "user:nobody:" not in _acl_text(media)
    assert "user:nobody:" not in _acl_text(source)


def test_acl_symlink_directory_is_refused(tmp_path):
    if not shutil.which("setfacl"):
        pytest.skip("acl tools unavailable")
    nobody = str(pwd.getpwnam("nobody").pw_uid)
    tenant = tmp_path / "tenant"
    tenant.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tenant / "link").symlink_to(outside, target_is_directory=True)
    before = installer._snapshot_acl(tenant)
    try:
        with pytest.raises(RuntimeError, match="telegram_tenant_tree_contains_symlink"):
            installer._apply_tenant_acl(nobody, tenant)
    finally:
        installer._restore_acl(before)
