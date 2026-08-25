from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


INSTALLER = Path(__file__).resolve().parents[1] / "install.py"
spec = importlib.util.spec_from_file_location("maton_installer_under_test", INSTALLER)
installer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = installer
assert spec.loader is not None
spec.loader.exec_module(installer)


class MatonInstallerTests(unittest.TestCase):
    def test_install_is_idempotent_and_discovers_plugin(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / ".hermes"
            home.mkdir()
            (home / "config.yaml").write_text(
                yaml.safe_dump(
                    {
                        "plugins": {"enabled": [], "disabled": []},
                        "platforms": {
                            "telegram": {
                                "extra": {
                                    "command_menu": {"priority": ["help", "new"]}
                                }
                            }
                        },
                        "mcp_servers": {
                            "maton": {
                                "enabled": False,
                                "url": "https://mcp.maton.ai",
                                "headers": {
                                    "Authorization": "Bearer ${MCP_MATON_API_KEY}"
                                },
                            }
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (home / ".env").write_text("A=1\n", encoding="utf-8")
            os.chmod(home / "config.yaml", 0o600)
            os.chmod(home / ".env", 0o600)

            installer.install(home)
            installer.install(home)

            config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
            self.assertEqual(config["plugins"]["enabled"].count("maton-onboarding"), 1)
            self.assertIs(config["mcp_servers"]["maton"]["enabled"], False)
            self.assertEqual(
                config["platforms"]["telegram"]["extra"]["command_menu"]["priority"],
                ["maton", "help", "new"],
            )
            self.assertTrue((home / "plugins/maton-onboarding/__init__.py").is_file())
            self.assertTrue((home / "plugins/maton-onboarding/plugin.yaml").is_file())

            old_home = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = str(home)
            try:
                from hermes_cli.plugins import PluginManager

                manager = PluginManager()
                manager.discover_and_load()
                self.assertTrue(manager.has_hook("pre_gateway_dispatch"))
                self.assertIn("maton", manager._plugin_commands)
                loaded = manager._plugins["maton-onboarding"]
                self.assertTrue(loaded.enabled)
                self.assertIsNone(loaded.error)
            finally:
                if old_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = old_home


if __name__ == "__main__":
    unittest.main()
