from __future__ import annotations

import json
import os
import types
import unittest
from unittest.mock import patch

import test_shared_runtime_layout as layout_tests
from test_shared_runtime_prepare import prepare
from test_shared_runtime_switch import switch

layout = layout_tests.layout


class MigrationTransactionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = layout_tests.SharedRuntimeLayoutTests("test_registered_shared_runtime_preserves_lexical_launcher_path")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.link.unlink()
        f.link.mkdir()
        if os.getuid() == 0:
            os.chown(f.link, f.uid, os.getgid())
        (f.link / "module.py").write_text("VALUE = 1\n")
        f.registration.unlink()
        (f.root / "admin").mkdir()
        (f.root / "backups").mkdir(mode=0o700)
        (f.root / "admin/prepared-profiles.json").write_text(json.dumps({
            "profiles": {"owner": f.release_id},
        }))
        (f.release / "code-manifest.json").write_text(json.dumps(prepare.source_manifest(f.link)))
        (f.home / "runtime").mkdir()
        (f.home / "runtime/active_sessions.json").write_text('{"entries": []}')
        (f.home / "gateway_state.json").write_text('{"pid": 123, "active_agents": 0}')
        for name in (".env", "config.yaml", "SOUL.md"):
            (f.home / name).write_text("synthetic private configuration\n")
        (f.home / "bin").mkdir()
        (f.home / "bin/uv").write_text("synthetic uv\n")
        (f.root / "tools/uv-0.12.3").mkdir(parents=True)
        (f.root / "tools/uv-0.12.3/uv").write_text("synthetic uv\n")
        self.units = f.base / "units"
        self.tmpfiles = f.base / "tmpfiles"
        self.units.mkdir()
        self.tmpfiles.mkdir()
        (f.base / "tmp").mkdir()
        self.unit = self.units / "owner-hermes.service"
        self.original_unit = ('[Service]\nUser=owner\n'
                              f'WorkingDirectory={f.home}\n'
                              f'ExecStart={f.link}/venv/bin/python -m hermes_cli.main gateway run\n'
                              '[Install]\nWantedBy=multi-user.target\n')
        self.unit.write_text(self.original_unit)
        self.service = {"User": "owner", "WorkingDirectory": str(f.home),
                        "ActiveState": "active", "MainPID": "123"}
        entry = types.SimpleNamespace(pw_name="owner", pw_dir=str(f.home.parent),
                                      pw_uid=f.uid, pw_gid=os.getgid())
        self.changing_unit = False

        def command(*args, **_):
            if args[:2] == ("systemctl", "stop"):
                self.service.update(ActiveState="inactive", MainPID="0")
                if self.changing_unit:
                    self.unit.write_text(self.original_unit + "# independent edit\n")
            elif args[:2] == ("systemctl", "start"):
                self.service.update(ActiveState="active", MainPID="456")
            return ""

        def run_path(path):
            if str(path).endswith("shared_runtime_layout.py"):
                return vars(layout)
            return {"source_manifest": prepare.source_manifest}

        patches = [
            patch.object(switch, "ROOT", f.root), patch.object(switch, "HOME_ROOT", f.home_root),
            patch.object(switch, "UNIT_ROOT", self.units), patch.object(switch, "TMPFILES_ROOT", self.tmpfiles),
            patch.object(switch, "PRIVATE_TMP_ROOT", f.base / "tmp/vkr"),
            patch.object(switch.os, "geteuid", return_value=0),
            patch.object(switch.pwd, "getpwnam", return_value=entry),
            patch.object(switch, "service_state", side_effect=lambda _owner: dict(self.service)),
            patch.object(switch, "command", side_effect=command),
            patch.object(switch.runpy, "run_path", side_effect=run_path),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_success_preserves_private_files_and_old_runtime(self):
        f = self.fixture
        with patch.object(switch, "wait_ready", side_effect=lambda *_: dict(self.service)):
            result = switch.switch("owner", apply=True)
        self.assertEqual(result["state"], "active")
        self.assertEqual(f.link.resolve(), f.code)
        self.assertTrue(f.registration.exists())
        self.assertEqual((f.home / ".env").read_text(), "synthetic private configuration\n")
        self.assertTrue((f.home / "bin/uv").is_symlink())
        backups = list((f.root / "backups").iterdir())
        self.assertEqual((backups[0] / "hermes-agent/module.py").read_text(), "VALUE = 1\n")

    def test_readiness_failure_restores_unit_code_tools_and_registration(self):
        f = self.fixture
        with patch.object(switch, "wait_ready", side_effect=RuntimeError("test readiness failure")):
            with self.assertRaisesRegex(RuntimeError, "test readiness failure"):
                switch.switch("owner", apply=True)
        self.assertFalse(f.link.is_symlink())
        self.assertEqual((f.link / "module.py").read_text(), "VALUE = 1\n")
        self.assertFalse(f.registration.exists())
        self.assertFalse((f.home / "bin/uv").is_symlink())
        self.assertEqual(self.unit.read_text(), self.original_unit)
        self.assertFalse((self.tmpfiles / "vektor-shared-owner.conf").exists())
        self.assertEqual(self.service["ActiveState"], "active")

    def test_concurrent_unit_edit_is_not_overwritten_by_rollback(self):
        self.changing_unit = True
        with self.assertRaisesRegex(RuntimeError, "manual_review_unit_changed"):
            switch.switch("owner", apply=True)
        self.assertIn("# independent edit", self.unit.read_text())
        self.assertFalse(self.fixture.link.is_symlink())


if __name__ == "__main__":
    unittest.main()
