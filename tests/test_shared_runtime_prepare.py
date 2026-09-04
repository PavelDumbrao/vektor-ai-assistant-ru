from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "shared_runtime_prepare_test", ROOT / "modules/shared-runtime/prepare_runtime.py")
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


class SharedCodeTests(unittest.TestCase):
    def test_identical_code_is_shared_without_linking_mutable_sources(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            first, second = root / "source1", root / "source2"
            first.mkdir()
            second.mkdir()
            (first / "same.py").write_text("shared = True\n")
            (second / "same.py").write_text("shared = True\n")
            (first / "variant.py").write_text("variant = 1\n")
            (second / "variant.py").write_text("variant = 2\n")
            shared = {}
            prepare.copy_code(first, root / "release1", prepare.source_manifest(first), shared)
            counts = prepare.copy_code(second, root / "release2", prepare.source_manifest(second), shared)
            self.assertEqual(counts["shared_files"], 1)
            self.assertTrue(os.path.samefile(root / "release1/same.py", root / "release2/same.py"))
            self.assertFalse(os.path.samefile(first / "same.py", root / "release1/same.py"))
            self.assertFalse(os.path.samefile(root / "release1/variant.py", root / "release2/variant.py"))

    def test_venv_node_modules_and_secret_files_are_not_published(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw)
            (source / "core.py").write_text("VALUE = 1\n")
            (source / ".env").write_text("not-a-real-secret")
            for name in ("venv", "node_modules", "__pycache__"):
                (source / name).mkdir()
                (source / name / "private.txt").write_text("not published")
            self.assertEqual(set(prepare.source_manifest(source)), {"core.py"})

    def test_source_symlink_cannot_publish_private_files_outside_code(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            (base / "code").mkdir()
            (base / "private.txt").write_text("not published")
            (base / "code/link").symlink_to(base / "private.txt")
            with self.assertRaisesRegex(ValueError, "escapes_code"):
                prepare.source_manifest(base / "code")

    def test_resume_does_not_overwrite_changed_staged_code(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source = base / "source"
            source.mkdir()
            (source / "code.py").write_text("VALUE = 1\n")
            manifest = prepare.source_manifest(source)
            prepare.copy_code(source, base / "release", manifest, {})
            (base / "release/code.py").write_text("VALUE = 2\n")
            with self.assertRaisesRegex(ValueError, "staged_source_file_changed"):
                prepare.copy_code(source, base / "release", manifest, {})


if __name__ == "__main__":
    unittest.main()
