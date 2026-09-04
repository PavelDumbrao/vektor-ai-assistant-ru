from __future__ import annotations

import json
import os
import sys
import tarfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import test_shared_runtime_layout as fixtures

MODULES = Path(__file__).resolve().parents[1] / 'modules/shared-runtime'
sys.path.insert(0, str(MODULES))
import upgrade_profile as upgrade  # noqa: E402
import prepare_release  # noqa: E402


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedRuntimeLayoutTests('test_registered_shared_runtime_preserves_lexical_launcher_path')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        (f.root / 'backups').mkdir(mode=0o700)
        self.new_id = 'hermes-0.21.0-test'
        self.new_release = f.root / 'releases' / self.new_id
        self.new_code = self.new_release / 'hermes-agent'
        self.new_code.mkdir(parents=True)
        (self.new_code / 'module.py').write_text('VALUE = 2\n')
        venv = self.new_release / 'venv'
        (venv / 'bin').mkdir(parents=True)
        (venv / 'bin/python').symlink_to(f.program)
        (self.new_code / 'venv').symlink_to(venv)
        self.ready = {'state': 'ready', 'release_id': self.new_id, 'version': '0.21.0',
                      'schema_rollback_compatible': True}
        (self.new_release / 'runtime.json').write_text(json.dumps(self.ready))
        (self.new_release / 'code-manifest.json').write_text(json.dumps(upgrade.builder.source_manifest(self.new_code)))
        for name in ('.env', 'config.yaml', 'SOUL.md', 'state.db'):
            (f.home / name).write_text('synthetic private data\n')
        (f.home / 'runtime').mkdir()
        (f.home / 'runtime/active_sessions.json').write_text('{"entries": []}')
        (f.home / 'gateway_state.json').write_text('{"pid": 123, "active_agents": 0}')
        self.units = f.base / 'units'
        self.units.mkdir()
        self.unit = self.units / 'owner-hermes.service'
        self.unit.write_text('[Service]\nUser=owner\n')
        self.state = {'User': 'owner', 'WorkingDirectory': str(f.home), 'ActiveState': 'active', 'MainPID': '123'}
        entry = types.SimpleNamespace(pw_uid=f.uid, pw_gid=os.getgid(), pw_dir=str(f.home.parent))
        self.actions = []

        def command(*args, **kwargs):
            self.actions.append(args)
            if args[:2] == ('systemctl', 'stop'):
                self.state.update(ActiveState='inactive', MainPID='0')
            if args[:2] == ('systemctl', 'start'):
                self.state.update(ActiveState='active', MainPID='456')
                (f.home / 'gateway_state.json').write_text(json.dumps({'pid': 456, 'code_version': '0.21.0', 'active_agents': 0}))

        for item in (patch.object(upgrade, 'ROOT', f.root), patch.object(upgrade, 'UNIT_ROOT', self.units),
                     patch.object(upgrade.os, 'geteuid', return_value=0),
                     patch.object(upgrade.pwd, 'getpwnam', return_value=entry),
                     patch.object(upgrade.runpy, 'run_path', return_value=vars(fixtures.layout)),
                     patch.object(upgrade.common, 'service_state', side_effect=lambda _: dict(self.state)),
                     patch.object(upgrade.common, 'command', side_effect=command),
                     patch.object(upgrade.common, 'wait_ready', side_effect=lambda *_: dict(self.state))):
            item.start()
            self.addCleanup(item.stop)

    def test_preview_does_not_stop_or_switch(self):
        result = upgrade.upgrade('owner', self.new_id, apply=False)
        self.assertEqual(result['preflight'], 'passed')
        self.assertEqual(self.fixture.link.resolve(), self.fixture.code)
        self.assertEqual(self.actions, [])

    def test_upgrade_preserves_private_files_and_backup(self):
        f = self.fixture
        before = (f.home / '.env').read_bytes()
        result = upgrade.upgrade('owner', self.new_id, apply=True)
        self.assertEqual(result['version'], '0.21.0')
        self.assertEqual(f.link.resolve(), self.new_code)
        self.assertEqual((f.home / '.env').read_bytes(), before)
        with tarfile.open(Path(result['backup']) / 'profile-state.tar.gz') as archive:
            self.assertIn('profile/state.db', archive.getnames())
            self.assertNotIn('profile/hermes-agent', archive.getnames())
        self.assertEqual((f.code / 'module.py').read_text(), 'VALUE = 1\n')

    def test_readiness_failure_restores_only_code_binding(self):
        f = self.fixture
        with patch.object(upgrade.common, 'wait_ready', side_effect=[RuntimeError('not ready'), dict(self.state)]):
            with self.assertRaisesRegex(RuntimeError, 'not ready'):
                upgrade.upgrade('owner', self.new_id, apply=True)
        self.assertEqual(f.link.resolve(), f.code)
        self.assertEqual(json.loads(f.registration.read_text())['release_id'], f.release_id)
        self.assertEqual(self.state['ActiveState'], 'active')
        self.assertEqual((f.home / '.env').read_text(), 'synthetic private data\n')

    def test_busy_profile_is_never_stopped(self):
        (self.fixture.home / 'runtime/active_sessions.json').write_text('{"entries": [{"id": "busy"}]}')
        with self.assertRaisesRegex(ValueError, 'not_idle'):
            upgrade.upgrade('owner', self.new_id, apply=True)
        self.assertEqual(self.actions, [])

    def test_modified_source_is_refused(self):
        (self.new_code / 'module.py').write_text('VALUE = 3\n')
        with self.assertRaisesRegex(ValueError, 'code_changed'):
            upgrade.upgrade('owner', self.new_id, apply=True)
        self.assertEqual(self.actions, [])

    def test_schema_compatibility_must_be_proved(self):
        self.ready.pop('schema_rollback_compatible')
        (self.new_release / 'runtime.json').write_text(json.dumps(self.ready))
        with self.assertRaisesRegex(ValueError, 'compatibility_not_verified'):
            upgrade.upgrade('owner', self.new_id, apply=True)
        self.assertEqual(self.actions, [])

    def test_requirement_names_cover_extras_and_markers(self):
        text = '# comment\nhttpx[socks]==0.28.1\nruamel_yaml==0.18.17 ; python_version > "3"\n'
        self.assertEqual(prepare_release.package_names(text), {'httpx', 'ruamel-yaml'})


if __name__ == '__main__':
    unittest.main()
