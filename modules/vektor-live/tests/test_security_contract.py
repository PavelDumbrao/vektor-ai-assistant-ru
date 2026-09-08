import re
import subprocess
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = re.compile(r"\b\d{7,12}:[A-Za-z0-9_-]{30,}\b")
PROVIDER = re.compile(r"\b(?:sk-proj-|sk-or-v1-|ghp_|github_pat_)[A-Za-z0-9_-]{25,}\b")
PRIVATE_NAMES = {".env", "credentials.json", "secrets.json", "auth.json"}

class SecurityContractTest(unittest.TestCase):
    def test_no_private_runtime_files_are_tracked(self):
        repo = ROOT.parents[1]
        tracked = subprocess.run(
            ["git", "ls-files", "modules/vektor-live"],
            cwd=repo, check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        bad = []
        for raw in tracked:
            rel = Path(raw).relative_to("modules/vektor-live")
            if rel.name in PRIVATE_NAMES or any(part in {"logs", "state", ".venv", "__pycache__"} for part in rel.parts):
                bad.append(str(rel))
        self.assertEqual(bad, [])

    def test_no_obvious_credentials_in_text_sources(self):
        bad = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix in {".pyc"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if TOKEN.search(text) or PROVIDER.search(text):
                bad.append(str(path.relative_to(ROOT)))
        self.assertEqual(bad, [])

    def test_secrets_are_environment_backed(self):
        config = (ROOT / "app/config.py").read_text(encoding="utf-8")
        for name in ["GEMINI_API_KEY", "HERMES_API_KEY", "VEKTOR_ADMIN_KEY", "VEKTOR_HANDS_KEY"]:
            self.assertIn(name, config)
        self.assertIn("secrets.compare_digest", (ROOT / "app/server.py").read_text(encoding="utf-8"))

    def test_password_fields_are_blocked_on_mac(self):
        hands = (ROOT / "hands/vektor_hands.py").read_text(encoding="utf-8")
        self.assertIn("AXSecureTextField", hands)

if __name__ == "__main__":
    unittest.main()
