from __future__ import annotations

import importlib.util
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


INSTALLER = Path(__file__).resolve().parents[1] / "install.py"
spec = importlib.util.spec_from_file_location("voice_picker_installer_under_test", INSTALLER)
installer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = installer
assert spec.loader is not None
spec.loader.exec_module(installer)


class VoicePickerInstallerTests(unittest.TestCase):
    def test_install_is_idempotent_and_discovers_plugin(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / ".hermes"
            home.mkdir(mode=0o700)
            initial_config = {
                "plugins": {"enabled": ["dummy-plugin"], "disabled": ["voice-picker"]},
                "platforms": {
                    "telegram": {
                        "extra": {
                            "business_owner_ids": [12345],
                        }
                    }
                },
                "tts": {
                    "elevenlabs": {
                        "voice_id": "old_id",
                    }
                },
            }
            (home / "config.yaml").write_text(
                yaml.safe_dump(initial_config, sort_keys=False),
                encoding="utf-8",
            )
            os.chmod(home / "config.yaml", 0o600)

            # First install
            backup1 = installer.install(home)
            self.assertTrue(backup1.is_dir())
            self.assertTrue((backup1 / "config.yaml").is_file())
            self.assertEqual(stat.S_IMODE((backup1 / "config.yaml").stat().st_mode), 0o600)

            # Second install (idempotency check)
            backup2 = installer.install(home)
            self.assertTrue(backup2.is_dir())
            self.assertTrue((backup2 / "config.yaml").is_file())
            # Target plugin directory backed up on second run
            self.assertTrue((backup2 / "plugin").is_dir())

            # Verify config.yaml
            config_text = (home / "config.yaml").read_text(encoding="utf-8")
            config = yaml.safe_load(config_text)

            self.assertEqual(config["plugins"]["enabled"].count("voice-picker"), 1)
            self.assertIn("dummy-plugin", config["plugins"]["enabled"])
            self.assertNotIn("voice-picker", config["plugins"].get("disabled", []))
            self.assertEqual(stat.S_IMODE((home / "config.yaml").stat().st_mode), 0o600)

            # Verify target plugin directory and files
            target = home / "plugins" / "voice-picker"
            self.assertTrue(target.is_dir())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

            init_file = target / "__init__.py"
            yaml_file = target / "plugin.yaml"
            self.assertTrue(init_file.is_file())
            self.assertTrue(yaml_file.is_file())
            self.assertEqual(stat.S_IMODE(init_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(yaml_file.stat().st_mode), 0o600)

            # Test discovery and loading via Hermes PluginManager
            old_home = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = str(home)
            try:
                from hermes_cli.plugins import PluginManager

                manager = PluginManager()
                manager.discover_and_load()

                self.assertTrue(manager.has_hook("pre_gateway_dispatch"))
                self.assertIn("golos", manager._plugin_commands)
                self.assertIn("voice_pick", manager._plugin_commands)

                loaded = manager._plugins.get("voice-picker")
                self.assertIsNotNone(loaded)
                self.assertTrue(loaded.enabled)
                self.assertIsNone(loaded.error)
            finally:
                if old_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = old_home

    def test_main_cli_execution(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / ".hermes"
            home.mkdir(mode=0o700)
            (home / "config.yaml").write_text("{}", encoding="utf-8")
            os.chmod(home / "config.yaml", 0o600)

            with patch.object(sys, "argv", ["install.py", "--hermes-home", str(home)]):
                captured = io.StringIO()
                with patch("sys.stdout", captured):
                    exit_code = installer.main()

            self.assertEqual(exit_code, 0)
            output = captured.getvalue()
            self.assertIn("installed=true", output)
            self.assertIn("plugin=voice-picker", output)
            self.assertIn("telegram_command=/golos", output)
            self.assertIn("restart_required=true", output)
            self.assertIn("backup=", output)


if __name__ == "__main__":
    unittest.main()
