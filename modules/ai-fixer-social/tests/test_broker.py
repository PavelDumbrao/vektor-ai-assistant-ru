import base64
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_fixer_social.broker import decode_photo, validate_request
import ai_fixer_social.rich_post as rich_post
import ai_fixer_social.telegram_content as telegram_content
import ai_fixer_social.post_validation as post_validation


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "integrations/hermes/ai_fixer/__init__.py"
spec = importlib.util.spec_from_file_location("ai_fixer_plugin", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
sys.modules[f"{spec.name}.rich_post"] = rich_post
sys.modules[f"{spec.name}.telegram_content"] = telegram_content
sys.modules[f"{spec.name}.post_validation"] = post_validation
spec.loader.exec_module(plugin)


class BrokerTests(unittest.TestCase):
    def test_unknown_operation(self):
        with self.assertRaises(ValueError):
            validate_request({"op": "getUpdates"})

    def test_arbitrary_target_rejected(self):
        with self.assertRaises(ValueError):
            validate_request({"op": "status", "chat_id": 123})

    def test_arbitrary_path_rejected(self):
        with self.assertRaises(ValueError):
            validate_request({"op": "validate", "text": "abc", "request_id": "x", "photo_path": "/etc/passwd"})

    def test_invalid_limit(self):
        for value in (0, 21, True, "10"):
            with self.assertRaises(ValueError):
                validate_request({"op": "comments", "limit": value})

    def test_publish_requires_hash(self):
        with self.assertRaises(ValueError):
            validate_request({"op": "publish", "text": "abc", "request_id": "test"})

    def test_plaintext_is_not_image(self):
        with self.assertRaises(ValueError):
            decode_photo(base64.b64encode(b"private-token-data").decode())

    def test_png_recognized(self):
        self.assertEqual(decode_photo(base64.b64encode(b"\x89PNG\r\n\x1a\ncontent").decode())[1], ".png")

    def test_bad_base64_rejected(self):
        with self.assertRaises(ValueError):
            decode_photo("invalid")


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.controller = plugin.Controller("1")
        self.observe()

    def observe(self, **kwargs):
        data = dict(session_id="session", turn_id="turn", sender_id="1", platform="telegram",
                    chat_type="dm", raw_user_message="Проверь редактора", is_internal_event=False)
        data.update(kwargs)
        self.controller.observe(**data)

    def test_owner_dm_authorized(self):
        self.assertTrue(self.controller.authorized("session"))

    def test_wrong_owner_revokes(self):
        self.observe(sender_id="2")
        self.assertFalse(self.controller.authorized("session"))

    def test_group_revokes(self):
        self.observe(chat_type="group")
        self.assertFalse(self.controller.authorized("session"))

    def test_cron_revokes(self):
        self.observe(is_internal_event=True)
        self.assertFalse(self.controller.authorized("session"))

    def test_no_raw_message_revokes(self):
        self.observe(raw_user_message=None)
        self.assertFalse(self.controller.authorized("session"))

    def test_model_supplied_owner_rejected(self):
        result = self.controller.handle({"op": "status", "owner_id": "1"}, session_id="session")
        self.assertFalse(json.loads(result)["ok"])

    def test_write_without_pretool_guard_is_denied(self):
        result = self.controller.handle({"op": "publish", "text": "abc"}, session_id="session")
        self.assertEqual(json.loads(result)["error"], "single_use_approval_required")

    def test_write_requires_approval(self):
        result = self.controller.guard(tool_name="ai_fixer", args={"op": "publish", "text": "abc"},
                                       session_id="session", turn_id="turn", tool_call_id="call")
        self.assertEqual(result["action"], "approve")
        self.assertIn("abc", result["message"])

    def test_changed_payload_cannot_use_permit(self):
        self.controller.guard(tool_name="ai_fixer", args={"op": "publish", "text": "abc"},
                              session_id="session", turn_id="turn", tool_call_id="call")
        result = self.controller.handle({"op": "publish", "text": "changed"}, session_id="session")
        self.assertEqual(json.loads(result)["error"], "single_use_approval_required")

    def test_new_turn_revokes_permit(self):
        self.controller.guard(tool_name="ai_fixer", args={"op": "reply", "message_id": 1},
                              session_id="session", turn_id="turn", tool_call_id="call")
        self.observe(turn_id="turn2")
        result = self.controller.handle({"op": "reply", "message_id": 1}, session_id="session")
        self.assertEqual(json.loads(result)["error"], "single_use_approval_required")

    def test_outside_image_path_rejected(self):
        with self.assertRaises(ValueError):
            plugin.request_payload({"op": "validate", "photo_path": str(PLUGIN_PATH)})

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "media").mkdir()
            (root / "secret.png").write_bytes(b"not a reference")
            (root / "media/link.png").symlink_to(root / "secret.png")
            with patch.object(plugin, "MEDIA_ROOT", root / "media"):
                with self.assertRaises(ValueError):
                    plugin.request_payload({"op": "validate", "photo_path": str(root / "media/link.png")})
