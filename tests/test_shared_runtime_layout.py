from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "shared_runtime_layout_test", ROOT / "modules/passive-secretary/shared_runtime_layout.py")
layout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(layout)


class SharedRuntimeLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shared-runtime-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "opt/vektor"
        self.home_root = self.base / "home"
        self.home = self.home_root / "owner/.hermes"
        self.uid = os.getuid() or 1
        self.release_id = "hermes-0.20.0-test"
        self.release = self.root / "releases" / self.release_id
        self.code = self.release / "hermes-agent"
        self.venv = self.release / "venv"
        self.program = self.root / "python/3.11/bin/python3.11"
        for directory in (
            self.code, self.venv / "bin", self.program.parent,
            self.root / "profiles", self.home,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.home.chmod(0o700)
        if os.getuid() == 0:
            os.chown(self.home.parent, self.uid, os.getgid())
            os.chown(self.home, self.uid, os.getgid())
        self.program.write_text("synthetic executable", encoding="utf-8")
        self.program.chmod(0o755)
        (self.code / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.venv / "bin/python").symlink_to(self.program)
        (self.code / "venv").symlink_to(self.venv)
        self.link = self.home / "hermes-agent"
        self.link.symlink_to(self.code)
        if os.getuid() == 0:
            os.lchown(self.link, self.uid, os.getgid())
        self.registration = self.root / "profiles/owner.json"
        self.registration.write_text(json.dumps({
            "schema_version": 1, "owner": "owner", "release_id": self.release_id,
        }), encoding="utf-8")
        (self.release / "runtime.json").write_text(json.dumps({
            "state": "ready", "release_id": self.release_id,
        }), encoding="utf-8")
        for name, value in (("RUNTIME_ROOT", self.root), ("HOME_ROOT", self.home_root),
                            ("TRUSTED_UID", os.getuid())):
            item = patch.object(layout, name, value)
            item.start()
            self.addCleanup(item.stop)

    def resolve(self):
        return layout.load_shared_runtime("owner", self.home, self.uid)

    def test_registered_shared_runtime_preserves_lexical_launcher_path(self):
        binding = self.resolve()
        self.assertEqual(binding["code"], self.code)
        self.assertEqual(binding["python_bin"], self.home / "hermes-agent/venv/bin/python")
        self.assertEqual(binding["resolved_python"], self.program)

    def test_legacy_private_layout_is_unchanged(self):
        self.link.unlink()
        self.link.mkdir()
        self.registration.unlink()
        self.assertIsNone(self.resolve())

    def test_unregistered_symlink_is_rejected(self):
        self.registration.unlink()
        with self.assertRaisesRegex(layout.SharedRuntimeError, "registration_missing"):
            self.resolve()

    def test_wrong_owner_release_or_extra_registration_field_is_rejected(self):
        for change in ({"owner": "someone_else"}, {"release_id": "../escape"},
                       {"schema_version": True}, {"python": "/tmp/untrusted"}):
            with self.subTest(change=change):
                data = {"schema_version": 1, "owner": "owner", "release_id": self.release_id}
                data.update(change)
                self.registration.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(layout.SharedRuntimeError):
                    self.resolve()

    def test_writable_registration_is_rejected(self):
        self.registration.chmod(0o666)
        with self.assertRaises(layout.SharedRuntimeError):
            self.resolve()

    def test_writable_program_or_code_is_rejected(self):
        for path in (self.program, self.code / "module.py", self.code):
            with self.subTest(path=path):
                old = path.stat().st_mode & 0o777
                path.chmod(old | 0o022)
                with self.assertRaises(layout.SharedRuntimeError):
                    self.resolve()
                path.chmod(old)

    def test_program_escape_outside_managed_python_is_rejected(self):
        (self.venv / "bin/python").unlink()
        outside = self.base / "other-python"
        outside.write_text("not a trusted interpreter", encoding="utf-8")
        outside.chmod(0o755)
        (self.venv / "bin/python").symlink_to(outside)
        with self.assertRaises(layout.SharedRuntimeError):
            self.resolve()

    def test_unready_release_is_rejected(self):
        (self.release / "runtime.json").write_text(json.dumps({
            "state": "building", "release_id": self.release_id,
        }), encoding="utf-8")
        with self.assertRaisesRegex(layout.SharedRuntimeError, "not_ready"):
            self.resolve()

    def test_link_to_other_release_is_rejected(self):
        other = self.root / "releases/hermes-other/hermes-agent"
        other.mkdir(parents=True)
        self.link.unlink()
        self.link.symlink_to(other)
        if os.getuid() == 0:
            os.lchown(self.link, self.uid, os.getgid())
        with self.assertRaises(layout.SharedRuntimeError):
            self.resolve()

    def test_untrusted_owner_and_noncanonical_home_are_rejected(self):
        for owner, home, uid in (("../owner", self.home, self.uid),
                                 ("owner", self.home.parent, self.uid),
                                 ("owner", self.home, 0)):
            with self.subTest(owner=owner, home=home, uid=uid):
                with self.assertRaises(layout.SharedRuntimeError):
                    layout.load_shared_runtime(owner, home, uid)


if __name__ == "__main__":
    unittest.main()
