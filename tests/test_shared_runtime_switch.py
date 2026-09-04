from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "shared_runtime_switch_test", ROOT / "modules/shared-runtime/switch_profile.py")
switch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(switch)


class UnitEditTests(unittest.TestCase):
    def setUp(self):
        self.original = ('[Unit]\nDescription=Hermes\n\n[Service]\nUser=owner\n'
                         'Environment="HERMES_HOME=/home/owner/.hermes"\n'
                         'Environment="PATH=/home/owner/.hermes/bin:/usr/bin"\n'
                         'ExecStart=/home/owner/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run\n'
                         '\n[Install]\nWantedBy=multi-user.target\n')

    def test_private_temp_does_not_replace_existing_environment_or_launcher(self):
        updated = switch.edit_unit(self.original, Path('/tmp/vkr/1005'))
        self.assertIn('Environment="HERMES_HOME=/home/owner/.hermes"', updated)
        self.assertIn('Environment="PATH=/home/owner/.hermes/bin:/usr/bin"', updated)
        self.assertIn('ExecStart=/home/owner/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run', updated)
        self.assertIn('UMask=0077', updated)
        self.assertIn('Environment="TMPDIR=/tmp/vkr/1005"', updated)
        self.assertLess(updated.index('TMPDIR='), updated.index('[Install]'))

    def test_repeat_is_idempotent(self):
        once = switch.edit_unit(self.original, Path('/tmp/vkr/1005'))
        self.assertEqual(switch.edit_unit(once, Path('/tmp/vkr/1005')), once)

    def test_conflicting_existing_configuration_is_not_overwritten(self):
        for setting in ('UMask=0022', 'Environment="TMPDIR=/custom/private"'):
            with self.subTest(setting=setting):
                original = self.original.replace('[Service]', '[Service]\n' + setting)
                with self.assertRaises(ValueError):
                    switch.edit_unit(original, Path('/tmp/vkr/1005'))

    def test_ambiguous_unit_is_rejected(self):
        with self.assertRaises(ValueError):
            switch.edit_unit('[Service]\n[Service]\n', Path('/tmp/vkr/1005'))


if __name__ == '__main__':
    unittest.main()
