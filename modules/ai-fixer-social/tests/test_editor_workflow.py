import asyncio
import base64
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ai_fixer_social import editor
from ai_fixer_social.analytics import build_report, channel_analytics
from ai_fixer_social.db import StateStore
from ai_fixer_social.outbound import publish_post
from test_broker import plugin


class Telegram:
    def __init__(self):
        self.sent = []
        self.fail = False
        self.rights = True

    async def get_me(self):
        return {"id": 9, "username": "editor"}

    async def get_chat_member(self, chat_id, user_id):
        return {"can_post_messages": self.rights}

    async def send_spec(self, *, chat_id, spec):
        self.sent.append({"chat_id": chat_id, "params": spec.params.copy(), "files": spec.files.copy()})
        if self.fail:
            raise TimeoutError("private-secret-must-not-escape")
        return {"message_ids": [15], "messages": [{"message_id": 15, "text": spec.params.get("text", ""), "chat": {"id": chat_id}}]}


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root / "state.sqlite3")
        self.settings = SimpleNamespace(telegram_channel_id=-100, telegram_channel_username="ProAiCommunity", pavel_user_id=1, telegram_bot_username="editor")
        self.telegram = Telegram()
        self.content = {"spec": {"kind": "text", "text": "<b>Проверяем редактора</b>\n\nТест только в личке."}}
        self.created = editor.create_draft(self.store, self.settings, draft_id="test-draft-v1", content=self.content, rubric="AI мем")
        editor.bind_preview(self.store, draft_id=self.created["draft_id"], bind_token=self.created["bind_token"], message_id=7)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def row(self):
        return editor.get_draft(self.store, self.created["draft_id"])

    def data(self, action):
        return "af:" + self.row()["nonce"] + ":" + action

    def click(self, action, **kw):
        args = dict(data=self.data(action), user_id=1, chat_id=1, message_id=7, callback_id="q1")
        args.update(kw)
        return editor.click_draft(self.store, self.settings, **args)

    def queue_now(self):
        self.click("publish")
        self.click("confirm")

    def run_due(self):
        return asyncio.run(editor.process_due(self.settings, self.store, self.telegram))

    def test_draft_starts_unapproved_no_send(self):
        self.assertFalse(self.run_due()["processed"])
        self.assertEqual(self.telegram.sent, [])
        self.assertIsNone(self.row()["approved_at"])

    def test_publication_requires_two_distinct_owner_clicks(self):
        with self.assertRaises(ValueError):
            self.click("confirm")
        self.click("publish")
        self.assertFalse(self.run_due()["processed"])
        self.click("confirm")
        self.assertTrue(self.run_due()["ok"])
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertNotIn("reply_markup", self.telegram.sent[0]["params"])
        self.assertEqual(self.telegram.sent[0]["chat_id"], -100)

    def test_wrong_owner_chat_message_never_consumes_nonce(self):
        original = self.row()["nonce"]
        for kw in ({"user_id": 2}, {"chat_id": 2}, {"message_id": 8}, {"user_id": True}):
            with self.assertRaises(ValueError):
                self.click("publish", **kw)
            self.assertEqual(original, self.row()["nonce"])

    def test_old_callback_cannot_approve_new_phase(self):
        self.click("publish")
        stale = self.data("confirm")
        self.click("back")
        self.click("queue")
        self.click("hour")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.click("confirm", data=stale)
        self.assertIsNone(self.row()["approved_at"])

    def test_immutable_snapshot_and_media_bytes(self):
        photo = {"spec": {"kind": "photo", "caption": "Мем", "filename": "test.png", "file_b64": base64.b64encode(b"\x89PNG\r\n\x1a\noriginal").decode()}}
        created = editor.create_draft(self.store, self.settings, draft_id="photo-test-v1", content=photo)
        photo["spec"]["caption"] = "Другой текст"
        self.assertIn("Мем", editor.get_draft(self.store, created["draft_id"])["content_json"])
        with self.assertRaisesRegex(ValueError, "immutable"):
            editor.create_draft(self.store, self.settings, draft_id=created["draft_id"], content=photo)

    def test_same_content_deduplicates_before_private_send(self):
        second = editor.create_draft(self.store, self.settings, draft_id="another-id", content=self.content)
        self.assertEqual(second["draft_id"], self.created["draft_id"])
        self.assertEqual(second["preview_message_id"], 7)

    def test_binding_cannot_be_changed(self):
        with self.assertRaises(ValueError):
            editor.bind_preview(self.store, draft_id=self.created["draft_id"], bind_token="wrong", message_id=8)
        with self.assertRaises(ValueError):
            editor.bind_preview(self.store, draft_id=self.created["draft_id"], bind_token=self.created["bind_token"], message_id=8)

    def test_queue_persists_after_reopen(self):
        self.queue_now()
        self.store.close()
        self.store = StateStore(self.root / "state.sqlite3")
        self.assertTrue(self.run_due()["ok"])
        self.assertFalse(self.run_due()["processed"])
        self.assertEqual(len(self.telegram.sent), 1)

    def test_send_timeout_never_retried(self):
        self.queue_now()
        self.telegram.fail = True
        self.assertEqual(self.run_due()["status"], "uncertain")
        self.assertFalse(self.run_due()["processed"])
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertNotIn("private-secret", self.row()["result_json"])
        with self.assertRaises(ValueError):
            editor.schedule_plan(self.store, draft_id=self.row()["draft_id"], scheduled_at=(datetime.now(UTC) + timedelta(days=1)).isoformat())

    def test_interrupted_claim_fails_closed(self):
        self.queue_now()
        self.store.conn.execute("UPDATE editor_drafts SET status='sending'")
        self.store.conn.commit()
        self.run_due()
        self.assertEqual(self.row()["status"], "uncertain")
        self.assertEqual(self.telegram.sent, [])

    def test_misfire_does_not_catch_up(self):
        self.queue_now()
        self.store.conn.execute("UPDATE editor_drafts SET scheduled_at=scheduled_at-1000")
        self.store.conn.commit()
        self.assertEqual(self.run_due()["status"], "expired")
        self.assertEqual(self.telegram.sent, [])

    def test_cancel_removes_future_send(self):
        self.queue_now()
        self.click("cancel")
        self.assertFalse(self.run_due()["processed"])
        self.assertEqual(self.row()["status"], "canceled")

    def test_status_refresh_does_not_change_approval(self):
        self.queue_now()
        original = self.row()["approval_source"]
        self.click("status", callback_id="later-status")
        self.assertEqual(original, self.row()["approval_source"])
        self.run_due()
        result = self.click("status")
        self.assertIn("message_id: 15", result["notice"])
        self.assertEqual(self.row()["status"], "sent")

    def test_reschedule_exact_hash_and_timezone(self):
        first = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        plan = editor.schedule_plan(self.store, draft_id=self.row()["draft_id"], scheduled_at=first)
        editor.set_schedule(self.store, draft_id=self.row()["draft_id"], scheduled_at=first, confirm_hash=plan["confirm_hash"])
        second = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        with self.assertRaisesRegex(ValueError, "confirmation_payload_mismatch"):
            editor.set_schedule(self.store, draft_id=self.row()["draft_id"], scheduled_at=second, confirm_hash=plan["confirm_hash"])
        plan2 = editor.schedule_plan(self.store, draft_id=self.row()["draft_id"], scheduled_at=second)
        editor.set_schedule(self.store, draft_id=self.row()["draft_id"], scheduled_at=second, confirm_hash=plan2["confirm_hash"])
        self.assertEqual(self.row()["scheduled_at"], datetime.fromisoformat(second).timestamp())
        self.assertFalse(self.run_due()["processed"])
        for value in ("2026-09-05T10:00:00", "bad", datetime.now(UTC).isoformat(), (datetime.now(UTC) + timedelta(days=31)).isoformat()):
            with self.assertRaises(ValueError):
                editor.parse_schedule(value)

    def test_lost_rights_and_snapshot_corruption_block(self):
        self.queue_now()
        self.telegram.rights = False
        self.assertEqual(self.run_due()["status"], "blocked")
        self.assertEqual(self.telegram.sent, [])
        self.store.conn.execute("UPDATE editor_drafts SET status='queued',payload_hash='wrong'")
        self.store.conn.commit()
        self.telegram.rights = True
        self.assertEqual(self.run_due()["result"]["error"], "snapshot_integrity_failure")
        self.assertEqual(self.telegram.sent, [])

    def test_queue_reserves_slot_for_other_publishers(self):
        self.queue_now()
        result = asyncio.run(publish_post(self.settings, self.store, self.telegram, request_id="heartbeat-test", text="Другой пост"))
        self.assertEqual(result["error"], "approved_queue_reserves_slot")
        self.assertEqual(self.telegram.sent, [])

    def test_expired_preview_cannot_approve(self):
        self.store.conn.execute("UPDATE editor_drafts SET expires_at=1")
        self.store.conn.commit()
        with self.assertRaisesRegex(ValueError, "expired"):
            self.click("publish")

    def test_edit_and_image_do_not_generate_or_publish(self):
        self.click("image")
        self.assertEqual(self.row()["status"], "image_requested")
        self.assertFalse(self.run_due()["processed"])
        with self.assertRaises(ValueError):
            self.click("publish")

    def test_callback_size_and_registry_no_capability_leak(self):
        for buttons in editor.keyboard(self.row())["inline_keyboard"]:
            for button in buttons:
                self.assertLessEqual(len(button["callback_data"].encode()), 64)
        public = editor.summary(self.row())
        for key in ("nonce", "bind_token", "content_json"):
            self.assertNotIn(key, public)

    def test_model_cannot_call_transport_only_ops(self):
        controller = plugin.Controller("1")
        controller.observe(session_id="s", turn_id="t", sender_id="1", platform="telegram", chat_type="dm", raw_user_message="проверка", is_internal_event=False)
        with patch.object(plugin.subprocess, "run") as run:
            result = json.loads(controller.handle({"op": "draft_controls", "draft_id": "test-draft-v1"}, session_id="s"))
            self.assertFalse(result["ok"])
            run.assert_not_called()
        for op in ("queue_set", "queue_cancel"):
            result = json.loads(controller.handle({"op": op, "draft_id": "test-draft-v1"}, session_id="s"))
            self.assertEqual(result["error"], "single_use_approval_required")

    def test_callback_adapter_checks_real_owner_before_broker(self):
        module = sys.modules[plugin.__name__ + ".editor_gateway"]
        query = SimpleNamespace(from_user=SimpleNamespace(id=2), message=SimpleNamespace(chat=SimpleNamespace(id=1, type="private")), answer=AsyncMock())
        with patch.object(module, "broker") as call:
            asyncio.run(module.CallbackHandler(1)(SimpleNamespace(callback_query=query), SimpleNamespace(bot=SimpleNamespace(id=9))))
            call.assert_not_called()
            query.answer.assert_awaited_once()

    def test_missing_runtime_callback_hook_blocks_preview_before_send(self):
        module = sys.modules[plugin.__name__ + ".editor_gateway"]
        with patch.object(module, "callback_supported", return_value=False), patch.object(module, "broker") as call:
            result = module.draft_preview(owner_id=1, request_id="new-test", content=self.content, rubric="", ledger_path=self.root / "preview.sqlite3")
            self.assertEqual(result["error"], "telegram_callback_adapter_missing")
            self.assertFalse(result["sent"])
            call.assert_not_called()

    def test_native_021_factory_scopes_callback_and_preserves_other_handlers(self):
        module = sys.modules[plugin.__name__ + ".editor_gateway"]
        ctx = SimpleNamespace(register_telegram_handler=Mock(), register_middleware=Mock())
        application = SimpleNamespace(add_handler=Mock())
        handler_type = Mock(side_effect=lambda callback, **kw: (callback, kw))
        module.register_callbacks(ctx, "1")
        ctx.register_middleware.assert_not_called()
        factory = ctx.register_telegram_handler.call_args.args[0]
        with patch.dict(sys.modules, {"telegram.ext": SimpleNamespace(CallbackQueryHandler=handler_type)}):
            factory(application, object())
        application.add_handler.assert_called_once()
        callback, options = application.add_handler.call_args.args[0]
        self.assertEqual(options, {"pattern": "^af:", "block": True})
        self.assertEqual(callback.owner_id, 1)

    def test_photo_and_article_payloads(self):
        photo = self.root / "test.png"
        photo.write_bytes(b"\x89PNG\r\n\x1a\ntest")
        with patch.object(plugin, "MEDIA_ROOT", self.root):
            payload = plugin.request_payload({"op": "draft_preview", "request_id": "test", "text": "Мем", "photo_path": str(photo)})
        self.assertEqual(payload["content"]["spec"]["caption"], "Мем")
        self.assertEqual(editor.prepare_content(payload["content"])[1], "photo")
        self.assertEqual(editor.prepare_content({"article": {"html": "<h1>Статья</h1><p>Текст</p>", "photos": []}})[1], "rich")

    def test_analytics_no_double_album_count_and_no_fabricated_rubrics(self):
        posts = [{"message_id": mid, "views": 100, "forwards": 5, "reaction_count": 10, "comments": 2, "grouped_id": "album", "text_preview": "Текст"} for mid in (1, 2)]
        report = build_report({"channel": "ProAiCommunity", "observed_at": "now", "posts": posts, "scope": "fixed"}, self.store)
        self.assertEqual(report["sample_posts"], 1)
        self.assertEqual(report["total_forwards_in_sample"], 5)
        self.assertEqual(report["posts"][0]["reaction_rate_percent"], 10)
        self.assertEqual(report["rubrics"][0]["rubric"], "не размечена")

    def test_analytics_mismatched_target_rejected(self):
        response = SimpleNamespace(status_code=200, json=lambda: {"channel_id": -200, "channel": "other"})
        client = AsyncMock()
        client.get.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch.dict("os.environ", {"AI_FIXER_ANALYTICS_API_KEY": "test-only"}), patch("ai_fixer_social.analytics.httpx.AsyncClient", return_value=context):
            result = asyncio.run(channel_analytics(self.settings, self.store))
        self.assertEqual(result["error"], "analytics_target_mismatch")

    def test_engine_metrics_strip_identities(self):
        path = Path(__file__).resolve().parents[1] / "integrations/telegram_engine/ai_fixer_analytics.py"
        spec = importlib.util.spec_from_file_location("engine_analytics_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        message = SimpleNamespace(id=1, date=datetime.now(UTC), message="test", views=100, forwards=2,
            reactions=SimpleNamespace(results=[SimpleNamespace(reaction=SimpleNamespace(emoticon="👍"), count=3)], recent_reactions=[{"user_id": 999}]),
            replies=SimpleNamespace(replies=4, recent_repliers=[999]), grouped_id=None)
        result = module.post_metrics(message, "ProAiCommunity")
        self.assertEqual(result["reaction_count"], 3)
        self.assertNotIn("999", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
