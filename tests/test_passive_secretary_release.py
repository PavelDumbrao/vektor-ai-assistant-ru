from __future__ import annotations

import hashlib
import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = REPO_ROOT / "modules" / "passive-secretary"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


installer = load_module(
    "passive_secretary_installer_release_test",
    MODULE_DIR / "install_passive_secretary.py",
)
owner_intent = load_module(
    "passive_secretary_owner_intent_release_test",
    MODULE_DIR / "passive_secretary_plugin" / "owner_intent.py",
)
core_patch = load_module(
    "passive_secretary_core_patch_release_test",
    MODULE_DIR / "install_core_patch.py",
)


class InstallerBundleTests(unittest.TestCase):
    def test_frozen_core_patch_hash_manifest_matches_release_tree(self) -> None:
        patch_root = MODULE_DIR / "hermes-core-patch"
        mismatches = []
        for relative in core_patch.PATCHED_FILES:
            actual = hashlib.sha256((patch_root / relative).read_bytes()).hexdigest()
            expected = core_patch.PATCHED_SHA256[relative]
            if actual != expected:
                mismatches.append((relative, actual, expected))
        self.assertEqual(mismatches, [])

    def test_required_release_manifest_is_complete(self) -> None:
        expected = [MODULE_DIR / "requirements.txt"]
        expected.append(MODULE_DIR / installer.RUNTIME_LAYOUT_FILE)
        expected.extend(
            MODULE_DIR / "passive_secretary_plugin" / filename
            for filename in installer.PLUGIN_FILES
        )
        missing = [str(path.relative_to(REPO_ROOT)) for path in expected if not path.is_file()]
        self.assertEqual(missing, [])

    def test_stage_module_succeeds_without_optional_operator_tools(self) -> None:
        for filename in installer.OPTIONAL_OPERATOR_FILES:
            self.assertFalse((MODULE_DIR / filename).exists())
        with tempfile.TemporaryDirectory(prefix="passive-secretary-release-test-") as raw:
            stage = Path(raw)
            runner = installer._stage_module(MODULE_DIR, stage)
            self.assertTrue(runner.is_file())
            self.assertTrue((stage / installer.RUNTIME_LAYOUT_FILE).is_file())
            for filename in installer.PLUGIN_FILES:
                self.assertTrue((stage / "passive_secretary_plugin" / filename).is_file())
            for filename in installer.OPTIONAL_OPERATOR_FILES:
                self.assertFalse((stage / filename).exists())

    def test_present_optional_operator_tool_is_staged(self) -> None:
        with tempfile.TemporaryDirectory(prefix="passive-secretary-module-test-") as module_raw:
            module_copy = Path(module_raw)
            shutil.copy2(MODULE_DIR / "requirements.txt", module_copy / "requirements.txt")
            shutil.copy2(MODULE_DIR / installer.RUNTIME_LAYOUT_FILE, module_copy / installer.RUNTIME_LAYOUT_FILE)
            shutil.copytree(
                MODULE_DIR / "passive_secretary_plugin",
                module_copy / "passive_secretary_plugin",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            optional_name = installer.OPTIONAL_OPERATOR_FILES[0]
            (module_copy / optional_name).write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            with tempfile.TemporaryDirectory(prefix="passive-secretary-stage-test-") as stage_raw:
                stage = Path(stage_raw)
                installer._stage_module(module_copy, stage)
                self.assertTrue((stage / optional_name).is_file())

    def test_optional_operator_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="passive-secretary-module-test-") as module_raw:
            module_copy = Path(module_raw)
            shutil.copy2(MODULE_DIR / "requirements.txt", module_copy / "requirements.txt")
            shutil.copy2(MODULE_DIR / installer.RUNTIME_LAYOUT_FILE, module_copy / installer.RUNTIME_LAYOUT_FILE)
            shutil.copytree(
                MODULE_DIR / "passive_secretary_plugin",
                module_copy / "passive_secretary_plugin",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            (module_copy / installer.OPTIONAL_OPERATOR_FILES[0]).mkdir()
            with tempfile.TemporaryDirectory(prefix="passive-secretary-stage-test-") as stage_raw:
                with self.assertRaisesRegex(installer.InstallError, "Optional operator source is unsafe"):
                    installer._stage_module(module_copy, Path(stage_raw))


class OutboundSafetyTests(unittest.TestCase):
    def test_only_explicit_owner_prefixes_are_accepted(self) -> None:
        self.assertTrue(owner_intent._is_explicit_owner_command("ОТПРАВИТЬ: Алексею — тест"))
        self.assertTrue(owner_intent._is_explicit_owner_command("ОТВЕТИТЬ: Алексею — тест"))
        self.assertFalse(owner_intent._is_explicit_owner_command("напомни отправить завтра"))
        self.assertFalse(owner_intent._is_explicit_owner_command(" ОТПРАВИТЬ: тест"))

    def test_internal_or_scheduled_event_cannot_authorize_business_send(self) -> None:
        gate = owner_intent.OwnerReplyIntentGate(ttl_seconds=120)
        self.assertFalse(
            gate.observe_turn(
                owner_id="123",
                session_id="session",
                turn_id="turn",
                raw_user_message="ОТПРАВИТЬ: Алексею — тест",
                is_internal_event=True,
            )
        )
        self.assertTrue(
            gate.observe_turn(
                owner_id="123",
                session_id="session",
                turn_id="turn",
                raw_user_message="ОТПРАВИТЬ: Алексею — тест",
                is_internal_event=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
