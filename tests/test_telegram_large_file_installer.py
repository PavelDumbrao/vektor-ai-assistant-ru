import importlib.util
import os
import pwd
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

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


class TelegramLargeFileInstallerTests(unittest.TestCase):
    def test_dropin_contains_no_shared_group_or_token(self):
        owner = "alice"
        tenant_hash = "a" * 64
        text = installer._dropin_content(owner, tenant_hash).decode()
        self.assertNotIn("SupplementaryGroups=", text)
        self.assertNotIn("SupplementaryGroups=telegram-transcriber", text)
        self.assertIn("BindReadOnlyPaths=", text)
        self.assertIn("InaccessiblePaths=/opt/telegram-transcriber-bot", text)
        self.assertIn(tenant_hash, text)
        self.assertNotIn("0000000000:", text)

    @unittest.skipUnless(shutil.which("setfacl") and shutil.which("getfacl"), "acl tools unavailable")
    def test_acl_access_default_and_restore(self):
        nobody = str(pwd.getpwnam("nobody").pw_uid)
        with tempfile.TemporaryDirectory() as raw:
            tmp_path = Path(raw)
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
            self.assertIn("default:user:nobody:r-x", _acl_text(tenant))
            self.assertIn("default:user:nobody:r-x", _acl_text(media))
            self.assertIn("user:nobody:r--", _acl_text(source))
            inherited = media / "new.bin"
            inherited.write_bytes(b"new")
            inherited.chmod(0o640)
            inherited_acl = _acl_text(inherited)
            self.assertIn("user:nobody:r-x", inherited_acl)
            self.assertIn("mask::r--", inherited_acl)
            installer._restore_acl(before)
            self.assertNotIn("user:nobody:", _acl_text(tenant))
            self.assertNotIn("user:nobody:", _acl_text(media))
            self.assertNotIn("user:nobody:", _acl_text(source))

    @unittest.skipUnless(shutil.which("setfacl") and shutil.which("getfacl"), "acl tools unavailable")
    def test_acl_symlink_directory_is_refused(self):
        nobody = str(pwd.getpwnam("nobody").pw_uid)
        with tempfile.TemporaryDirectory() as raw:
            tmp_path = Path(raw)
            tenant = tmp_path / "tenant"
            tenant.mkdir()
            outside = tmp_path / "outside"
            outside.mkdir()
            (tenant / "link").symlink_to(outside, target_is_directory=True)
            before = installer._snapshot_acl(tenant)
            try:
                with self.assertRaisesRegex(RuntimeError, "telegram_tenant_tree_contains_symlink"):
                    installer._apply_tenant_acl(nobody, tenant)
            finally:
                installer._restore_acl(before)


if __name__ == "__main__":
    unittest.main()
