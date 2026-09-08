import tempfile
import unittest
from pathlib import Path

from ai_fixer_social.db import StateStore
from ai_fixer_social.models import ReplyDecision


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name) / "state.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_offset_roundtrip(self):
        self.assertIsNone(self.store.get_int("offset"))
        self.store.set_int("offset", 42)
        self.assertEqual(self.store.get_int("offset"), 42)

    def test_thread_root_roundtrip(self):
        self.store.remember_root(-100, 77, channel_post_id=3008, root_text="Пост")
        self.assertTrue(self.store.root_exists(-100, 77))
        self.assertEqual(self.store.root_text(-100, 77), "Пост")

    def test_decision_marks_message_processed(self):
        decision = ReplyDecision("ignore", "low", 1.0, "", "noise", "")
        self.store.record_decision(-100, 8, decision)
        self.assertTrue(self.store.is_processed(-100, 8))

    def test_publication_idempotency_record(self):
        self.store.record_publication(
            request_id="post-1",
            channel_id=-100,
            message_id=9,
            public_url="https://t.me/test/9",
            payload_hash="abc",
        )
        row = self.store.get_publication("post-1")
        self.assertEqual(row["message_id"], 9)

    def test_reply_rate_limit(self):
        self.store.record_reply(
            chat_id=-100,
            source_message_id=10,
            reply_message_id=11,
            thread_id=5,
            user_id=7,
        )
        allowed, reason = self.store.reply_limits_ok(
            thread_id=5,
            user_id=8,
            per_hour=10,
            per_thread_hour=1,
            per_day=40,
            user_cooldown_minutes=60,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "thread_hourly_limit")


if __name__ == "__main__":
    unittest.main()

