import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text().strip()
SPEC = json.loads((ROOT / "releases" / VERSION / "manifest.json").read_text())

class ReleaseContractTest(unittest.TestCase):
    def test_version_matches(self):
        self.assertEqual(SPEC["version"], VERSION)

    def test_all_production_hashes_match(self):
        for rel, expected in SPEC["files_sha256"].items():
            path = ROOT / rel
            self.assertTrue(path.is_file(), rel)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected, rel)

    def test_vps_shared_files_are_declared(self):
        self.assertEqual(len(SPEC["shared_vps_files"]), 16)
        self.assertTrue(all(p in SPEC["files_sha256"] for p in SPEC["shared_vps_files"]))

if __name__ == "__main__":
    unittest.main()
