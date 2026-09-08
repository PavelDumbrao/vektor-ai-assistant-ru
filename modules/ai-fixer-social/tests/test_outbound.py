import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ai_fixer_social.db import StateStore
from ai_fixer_social.outbound import get_attempt, outbound_lock, publish_post


class FakeTelegram:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_text(self, **args):
        self.sent.append(args)
        if self.fail:
            raise TimeoutError("secret must never escape")
        return {"message_id": 101}


class OutboundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name) / "state.sqlite3")
        self.settings = SimpleNamespace(telegram_channel_id=-100, telegram_channel_username="test")
        self.telegram = FakeTelegram()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def publish(self, **kwargs):
        return asyncio.run(publish_post(self.settings, self.store, self.telegram,
                                        request_id=kwargs.pop("request_id", "vektor-test-1"),
                                        text=kwargs.pop("text", "<b>Привет</b>"), **kwargs))

    def test_dry_run_does_not_send_or_reserve(self):
        result = self.publish(dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.telegram.sent, [])
        self.assertIsNone(get_attempt(self.store, "post:vektor-test-1"))

    def test_same_id_deduplicates(self):
        self.assertTrue(self.publish()["ok"])
        self.assertTrue(self.publish()["deduplicated"])
        self.assertEqual(len(self.telegram.sent), 1)

    def test_same_payload_different_id_deduplicates(self):
        self.publish()
        self.assertTrue(self.publish(request_id="different-id")["deduplicated"])
        self.assertEqual(len(self.telegram.sent), 1)

    def test_same_id_different_payload_blocked(self):
        self.publish()
        self.assertEqual(self.publish(text="Другой текст")["error"], "request_id_payload_mismatch")

    def test_confirmation_mismatch(self):
        self.assertEqual(self.publish(confirm_hash="bad")["error"], "confirmation_payload_mismatch")
        self.assertEqual(self.telegram.sent, [])

    def test_uncertain_is_not_retried(self):
        self.telegram.fail = True
        first = self.publish()
        self.assertEqual(first["error"], "send_result_uncertain")
        self.assertNotIn("secret", str(first))
        self.assertEqual(self.publish()["error"], "previous_attempt_requires_readback")
        self.assertEqual(self.publish(request_id="other-id")["error"], "unresolved_publication_requires_readback")
        self.assertEqual(len(self.telegram.sent), 1)

    def test_external_post_cooldown(self):
        self.store.remember_root(-200, 45, channel_post_id=88)
        self.assertEqual(self.publish()["error"], "channel_six_hour_cooldown")
        self.assertEqual(self.telegram.sent, [])

    def test_own_post_cooldown(self):
        self.publish()
        self.assertEqual(self.publish(request_id="other-id", text="Новый пост")["error"], "channel_six_hour_cooldown")

    def test_cross_process_lock(self):
        with outbound_lock(self.store):
            self.assertEqual(self.publish()["error"], "outbound_busy")
        self.assertEqual(self.telegram.sent, [])

    def test_invalid_request_id(self):
        self.assertEqual(self.publish(request_id="../path")["error"], "invalid_request_id")

    def test_invalid_html(self):
        self.assertFalse(self.publish(text="<script>foo</script>")["ok"])

