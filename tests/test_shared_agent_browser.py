from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "shared_agent_browser_test", ROOT / "modules/shared-runtime/install_agent_browser.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def archive(extra=()):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as output:
        files = [("package/package.json", json.dumps({"name": "agent-browser", "dependencies": {}}).encode()),
                 ("package/LICENSE", b"synthetic license"),
                 ("package/bin/agent-browser-linux-x64", b"synthetic executable"),
                 ("package/bin/agent-browser-darwin-arm64", b"not needed")]
        for name, content in files + list(extra):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            output.addfile(member, io.BytesIO(content))
    return raw.getvalue()


class SharedBrowserArtifactTests(unittest.TestCase):
    def test_only_current_native_platform_is_extracted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            installer.extract_native_package(archive(), root)
            self.assertTrue((root / installer.NATIVE).is_file())
            self.assertTrue((root / "LICENSE").is_file())
            self.assertFalse((root / "bin/agent-browser-darwin-arm64").exists())

    def test_archive_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ValueError, "path_escape"):
                installer.extract_native_package(archive((("package/../../escape", b"no"),)), Path(raw))

    def test_integrity_is_checked_before_installation(self):
        data = archive()
        expected = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
        installer.verify_integrity(data, expected)
        with self.assertRaisesRegex(ValueError, "integrity_mismatch"):
            installer.verify_integrity(data + b"changed", expected)


if __name__ == "__main__":
    unittest.main()
