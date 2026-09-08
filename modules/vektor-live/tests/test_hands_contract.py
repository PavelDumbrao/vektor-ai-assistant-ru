import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

def literal_assignment(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")

class HandsContractTest(unittest.TestCase):
    def test_server_and_mac_allowlists_are_identical(self):
        server = literal_assignment(ROOT / "app/hands_hub.py", "ALLOWED")
        mac = literal_assignment(ROOT / "hands/vektor_hands.py", "ALLOWED")
        self.assertEqual(server, mac)
        self.assertEqual(server["watch"], {"screenshot", "zoom", "find"})
        self.assertIn("point", server["hint"])
        self.assertTrue({"click", "type", "key", "open_url"}.issubset(server["act"]))

    def test_real_click_implementation_is_present(self):
        text = (ROOT / "hands/scripts/jxaclick.js").read_text(encoding="utf-8")
        for marker in ["CGEventLeftMouseDown", "CGEventLeftMouseUp", "CGEventPost"]:
            self.assertIn(marker, text)

    def test_accessibility_and_zoom_contract_exists(self):
        hands = (ROOT / "hands/vektor_hands.py").read_text(encoding="utf-8")
        server = (ROOT / "app/server.py").read_text(encoding="utf-8")
        self.assertIn("find_elements", hands)
        self.assertIn("zoom_at", hands)
        self.assertIn('name="find_on_screen"', server)
        self.assertIn('name="look_closer"', server)

    def test_mode_is_announced_on_connect_and_change(self):
        server = (ROOT / "app/server.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(server.count("await self.announce_mode()"), 2)

if __name__ == "__main__":
    unittest.main()
