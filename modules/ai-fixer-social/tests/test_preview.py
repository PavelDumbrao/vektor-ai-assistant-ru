import base64
import json
import sqlite3
import tempfile
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_broker import plugin
from ai_fixer_plugin.preview import deliver_preview, send_owner_photo


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sender = Mock(return_value={"message_id": 12, "has_photo": True, "caption_chars": 4})
        self.args = dict(owner_id="1", request_id="preview-v2", text="<b>Тест</b>",
                         raw=b"\x89PNG\r\n\x1a\nimage", ledger_path=self.root / "previews.sqlite3", sender=self.sender)

    def test_one_photo_caption_and_persistent_dedup(self):
        first = deliver_preview(**self.args)
        second = deliver_preview(**self.args)
        self.assertTrue(first["ok"])
        self.assertFalse(first["published"])
        self.assertTrue(second["deduplicated"])
        self.sender.assert_called_once_with("1", "<b>Тест</b>", self.args["raw"])
        self.assertEqual((self.root / "previews.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_same_payload_new_id_does_not_duplicate(self):
        deliver_preview(**self.args)
        result = deliver_preview(**{**self.args, "request_id": "another-id"})
        self.assertTrue(result["deduplicated"])
        self.assertEqual(self.sender.call_count, 1)

    def test_request_payload_change_rejected(self):
        deliver_preview(**self.args)
        result = deliver_preview(**{**self.args, "text": "Другой текст"})
        self.assertEqual(result["error"], "preview_request_id_payload_mismatch")
        self.assertEqual(self.sender.call_count, 1)

    def test_timeout_never_retries_even_with_new_id(self):
        self.sender.side_effect = TimeoutError("private credential must not escape")
        first = deliver_preview(**self.args)
        second = deliver_preview(**{**self.args, "request_id": "different-id", "text": "Другой"})
        self.assertFalse(first["retry_allowed"])
        self.assertNotIn("credential", json.dumps(first))
        self.assertEqual(second["error"], "previous_preview_requires_readback")
        self.assertEqual(self.sender.call_count, 1)

    def test_long_caption_rejected_without_send_or_split(self):
        with self.assertRaises(ValueError):
            deliver_preview(**{**self.args, "text": "😀" * 513})
        self.sender.assert_not_called()

    def test_sending_reserved_before_network(self):
        def sender(*args):
            conn = sqlite3.connect(self.args["ledger_path"])
            try:
                self.assertEqual(conn.execute("SELECT status FROM previews").fetchone()[0], "sending")
            finally:
                conn.close()
            return {"message_id": 12, "has_photo": True}
        deliver_preview(**{**self.args, "sender": sender})

    def test_native_transport_sends_only_one_photo_with_html_caption(self):
        calls = []
        class Bot:
            username = "vektor_assist_bot"

            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def send_photo(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(photo=[True], chat=SimpleNamespace(id=1, type="private"),
                                       caption="Тест", message_id=12)

        config = SimpleNamespace(platforms={"telegram": SimpleNamespace(
            enabled=True, token="test-only", home_channel=SimpleNamespace(chat_id="1"))})
        modules = {"gateway.config": SimpleNamespace(Platform=SimpleNamespace(TELEGRAM="telegram"),
                                                     load_gateway_config=lambda: config),
                   "telegram": SimpleNamespace(Bot=Bot)}
        with patch.dict(sys.modules, modules):
            result = send_owner_photo("1", "<b>Тест</b>", self.args["raw"])
        self.assertEqual(result["message_id"], 12)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["chat_id"], 1)
        self.assertEqual(calls[0]["caption"], "<b>Тест</b>")
        self.assertEqual(calls[0]["parse_mode"], "HTML")
        self.assertFalse(calls[0]["show_caption_above_media"])

    def test_caption_path_rejects_missing_photo(self):
        controller = plugin.Controller("1")
        controller.observe(session_id="s", turn_id="t", sender_id="1", platform="telegram",
                           chat_type="dm", raw_user_message="Покажи", is_internal_event=False)
        with patch.object(plugin.subprocess, "run") as broker:
            result = json.loads(controller.handle({"op": "preview", "request_id": "preview-v2", "text": "Тест"}, session_id="s"))
            self.assertFalse(result["ok"])
            broker.assert_not_called()

    def test_group_owner_rejected(self):
        with self.assertRaises(ValueError):
            deliver_preview(**{**self.args, "owner_id": "-100123"})
        self.sender.assert_not_called()

    def test_handler_enforces_owner_and_validates_without_publishing(self):
        controller = plugin.Controller("1")
        controller.observe(session_id="s", turn_id="t", sender_id="1", platform="telegram",
                           chat_type="dm", raw_user_message="Покажи превью", is_internal_event=False)
        photo = self.root / "photo.png"
        photo.write_bytes(self.args["raw"])
        args = {"op": "preview", "request_id": "preview-v2", "text": "Тест", "photo_path": str(photo)}
        completed = SimpleNamespace(returncode=0, stdout='{"ok":true,"dry_run":true}')
        with patch.object(plugin, "MEDIA_ROOT", self.root), patch.object(plugin.subprocess, "run", return_value=completed) as broker, patch.object(plugin, "deliver_preview", return_value={"ok": True}) as preview:
            self.assertTrue(json.loads(controller.handle(args, session_id="s"))["ok"])
            payload = json.loads(broker.call_args.kwargs["input"])
            self.assertEqual(payload["op"], "validate")
            self.assertEqual(base64.b64decode(payload["photo_b64"]), self.args["raw"])
            self.assertEqual(preview.call_args.kwargs["owner_id"], "1")
            self.assertFalse(json.loads(controller.handle({**args, "chat_id": "2"}, session_id="s"))["ok"])
            controller.observe(session_id="s", turn_id="t", sender_id="2", platform="telegram",
                               chat_type="dm", raw_user_message="Покажи", is_internal_event=False)
            self.assertEqual(json.loads(controller.handle(args, session_id="s"))["error"], "owner_dm_required")
            self.assertEqual(preview.call_count, 1)

    def test_handler_stops_on_validation_error(self):
        controller = plugin.Controller("1")
        controller.observe(session_id="s", turn_id="t", sender_id="1", platform="telegram",
                           chat_type="dm", raw_user_message="Покажи", is_internal_event=False)
        photo = self.root / "photo.png"
        photo.write_bytes(self.args["raw"])
        args = {"op": "preview", "request_id": "preview-v2", "text": "Тест", "photo_path": str(photo)}
        completed = SimpleNamespace(returncode=0, stdout='{"ok":false,"errors":["caption_too_long"]}')
        with patch.object(plugin, "MEDIA_ROOT", self.root), patch.object(plugin.subprocess, "run", return_value=completed), patch.object(plugin, "deliver_preview") as preview:
            self.assertFalse(json.loads(controller.handle(args, session_id="s"))["ok"])
            preview.assert_not_called()
