import asyncio
import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from ai_fixer_social.broker import validate_request
from ai_fixer_social.db import StateStore
from ai_fixer_social.outbound import get_attempt, outbound_lock, publish_post
from ai_fixer_social.rich_post import prepare_article
from ai_fixer_social.telegram import TelegramClient
from test_broker import plugin
from ai_fixer_plugin.preview import deliver_rich_preview


def article_data():
    return {
        "html": "<h1>Тест статьи</h1><p>" + "Полезный текст. " * 100 + "</p>"
        '<tg-slideshow><img src="tg://photo?id=first"/><img src="tg://photo?id=second"/>'
        "<figcaption>Два кадра</figcaption></tg-slideshow><h2>Вывод</h2><p>Всё на месте.</p>",
        "photos": [
            {
                "id": name,
                "data": base64.b64encode(b"\x89PNG\r\n\x1a\n" + name.encode()).decode(),
            }
            for name in ("first", "second")
        ],
    }


class RichPostTests(unittest.TestCase):
    def test_bot_api_multipart_is_one_rich_message(self):
        captured = []

        def reply(request):
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "message_id": 99,
                        "chat": {"id": -100},
                        "rich_message": {"blocks": [{"type": "slideshow"}]},
                    },
                },
            )

        async def run():
            client = TelegramClient("test-token")
            await client.close()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
            try:
                return await client.send_rich_post(
                    chat_id=-100, article=prepare_article(article_data())
                )
            finally:
                await client.close()

        result = asyncio.run(run())
        self.assertEqual(result["message_id"], 99)
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0].url.path.endswith("/sendRichMessage"))
        body = captured[0].content
        self.assertIn(b"attach://photo_0", body)
        self.assertIn(b'name="photo_1"', body)
        self.assertNotIn(b"sendMediaGroup", body)

    def test_long_article_and_two_slides_one_native_object(self):
        article = prepare_article(article_data())
        self.assertGreater(article.visible_chars, 1024)
        self.assertEqual(article.slideshows, [2])
        self.assertEqual(len(article.files()), 2)
        self.assertEqual(
            article.wire()["media"][1]["media"]["media"], "attach://photo_1"
        )

    def test_remote_images_are_not_fetched(self):
        data = article_data()
        data["html"] = data["html"].replace(
            "tg://photo?id=first", "https://example.com/private.jpg"
        )
        with self.assertRaisesRegex(ValueError, "local_photo_id"):
            prepare_article(data)

    def test_dangerous_html_rejected(self):
        for content in (
            "<script>run()</script>",
            '<p onclick="run()">Текст</p>',
            '<a href="javascript:run()">x</a>',
            "<p>Не закрыто",
            "<!--test--><p>x</p>",
        ):
            with self.subTest(content=content), self.assertRaises(ValueError):
                prepare_article({"html": content, "photos": []})

    def test_missing_and_unused_image_ids_rejected(self):
        data = article_data()
        data["photos"].pop()
        with self.assertRaisesRegex(ValueError, "reference_mismatch"):
            prepare_article(data)

    def test_duplicate_ids_rejected(self):
        data = article_data()
        data["photos"][1]["id"] = "first"
        with self.assertRaisesRegex(ValueError, "duplicate_article_photo_id"):
            prepare_article(data)

    def test_single_photo_is_not_called_a_carousel(self):
        data = article_data()
        data["html"] = data["html"].replace('<img src="tg://photo?id=second"/>', "")
        with self.assertRaisesRegex(ValueError, "2_to_10"):
            prepare_article(data)

    def test_nested_carousels_rejected(self):
        data = article_data()
        data["html"] = data["html"].replace(
            "<tg-slideshow>", "<tg-slideshow><tg-slideshow>"
        )
        with self.assertRaisesRegex(ValueError, "nested_slideshow"):
            prepare_article(data)

    def test_markup_and_photo_order_affect_confirmation_hash(self):
        data = article_data()
        expected = prepare_article(data).payload_hash
        changed = copy.deepcopy(data)
        changed["html"] = changed["html"].replace("Полезный", "Новый", 1)
        self.assertNotEqual(expected, prepare_article(changed).payload_hash)
        changed = copy.deepcopy(data)
        changed["photos"].reverse()
        self.assertNotEqual(expected, prepare_article(changed).payload_hash)

    def test_html_size_and_long_dash_limits(self):
        for text in ("Я" * 32769, "Нельзя — так"):
            with self.assertRaises(ValueError):
                prepare_article({"html": "<p>" + text + "</p>", "photos": []})

    def test_18000_russian_characters_are_not_18000_bytes(self):
        html = "<h1>Я</h1><p>" + "я" * 17999 + "</p>"
        self.assertGreater(len(html.encode("utf-8")), 32768)
        self.assertEqual(
            prepare_article({"html": html, "photos": []}).visible_chars, 18000
        )

    def test_entities_and_markup_do_not_inflate_text_count(self):
        html = "<p><b>" + "&amp;" * 18000 + "</b></p>"
        self.assertEqual(
            prepare_article({"html": html, "photos": []}).visible_chars, 18000
        )

    def test_full_character_boundary_and_separate_html_budget(self):
        self.assertEqual(
            prepare_article(
                {"html": "<p>" + "Я" * 32768 + "</p>", "photos": []}
            ).visible_chars,
            32768,
        )
        with self.assertRaisesRegex(ValueError, "128_kib"):
            prepare_article(
                {"html": "<p>" + "&#0000032;" * 20000 + "</p>", "photos": []}
            )

    def test_plaintext_is_not_a_photo(self):
        data = article_data()
        data["photos"][0]["data"] = base64.b64encode(b"private data").decode()
        with self.assertRaisesRegex(ValueError, "png_or_jpeg"):
            prepare_article(data)


class RichDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.settings = SimpleNamespace(
            telegram_channel_id=-100, telegram_channel_username="test"
        )
        self.sent = []

        async def send(**kwargs):
            self.sent.append(kwargs)
            return {"message_id": 201}

        self.telegram = SimpleNamespace(send_rich_post=send)

    def publish(self, **kwargs):
        return asyncio.run(
            publish_post(
                self.settings,
                self.store,
                self.telegram,
                request_id=kwargs.pop("request_id", "long-test-v1"),
                text="",
                article=article_data(),
                **kwargs,
            )
        )

    def test_dry_run_does_not_publish(self):
        result = self.publish(dry_run=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["slideshow_sizes"], [2])
        self.assertEqual(self.sent, [])
        self.assertIsNone(get_attempt(self.store, "post:long-test-v1"))

    def test_public_article_uses_shared_dedup_and_target(self):
        dry = self.publish(dry_run=True)
        self.assertTrue(self.publish(confirm_hash=dry["payload_hash"])["ok"])
        self.assertTrue(self.publish(confirm_hash=dry["payload_hash"])["deduplicated"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["chat_id"], -100)

    def test_public_article_keeps_confirmation_lock_and_cooldown(self):
        self.assertEqual(
            self.publish(confirm_hash="bad")["error"], "confirmation_payload_mismatch"
        )
        with outbound_lock(self.store):
            self.assertEqual(self.publish()["error"], "outbound_busy")
        self.store.remember_root(-200, 1, channel_post_id=30)
        self.assertEqual(self.publish()["error"], "channel_six_hour_cooldown")
        self.assertFalse(self.sent)

    def test_private_rich_preview_is_idempotent_and_not_a_publication(self):
        sender = Mock(return_value={"message_id": 22, "has_rich_message": True})
        args = dict(
            owner_id="1",
            request_id="long-preview-v1",
            article=article_data(),
            ledger_path=self.root / "previews.sqlite3",
            sender=sender,
        )
        first = deliver_rich_preview(**args)
        second = deliver_rich_preview(**args)
        self.assertFalse(first["published"])
        self.assertEqual(first["delivery"], "owner_dm_rich_message")
        self.assertTrue(second["deduplicated"])
        self.assertEqual(sender.call_count, 1)

    def test_rich_preview_timeout_is_not_retried(self):
        sender = Mock(side_effect=TimeoutError("secret"))
        args = dict(
            owner_id="1",
            request_id="long-preview-v1",
            article=article_data(),
            ledger_path=self.root / "previews.sqlite3",
            sender=sender,
        )
        self.assertFalse(deliver_rich_preview(**args)["retry_allowed"])
        self.assertEqual(
            deliver_rich_preview(**args)["error"], "previous_preview_requires_readback"
        )
        self.assertEqual(sender.call_count, 1)

    def test_publication_requires_broker_hash_and_owner_approval(self):
        with self.assertRaises(ValueError):
            validate_request(
                {
                    "op": "long_publish",
                    "request_id": "test-v1",
                    "article": article_data(),
                }
            )
        controller = plugin.Controller("1")
        controller.observe(
            session_id="s",
            turn_id="t",
            sender_id="1",
            platform="telegram",
            chat_type="dm",
            raw_user_message="Опубликуй",
            is_internal_event=False,
        )
        args = {
            "op": "long_publish",
            "request_id": "test-v1",
            "article_path": "/example.json",
            "confirm_hash": "hash",
        }
        self.assertEqual(
            json.loads(controller.handle(args, session_id="s"))["error"],
            "single_use_approval_required",
        )
        self.assertEqual(
            controller.guard(
                tool_name="ai_fixer", args=args, session_id="s", turn_id="t"
            )["action"],
            "approve",
        )

    def test_article_and_photo_paths_are_scoped(self):
        image = self.root / "photo.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
        path = self.root / "article.json"
        path.write_text(
            json.dumps(
                {
                    "html": '<p>Текст</p><img src="tg://photo?id=x"/>',
                    "photos": [{"id": "x", "path": str(image)}],
                }
            )
        )
        with patch.object(plugin, "MEDIA_ROOT", self.root):
            payload = plugin.request_payload(
                {
                    "op": "long_validate",
                    "request_id": "test-v1",
                    "article_path": str(path),
                }
            )
            self.assertEqual(
                prepare_article(payload["article"]).summary()["photo_count"], 1
            )
            with self.assertRaises(ValueError):
                plugin.request_payload(
                    {
                        "op": "long_preview",
                        "request_id": "test-v1",
                        "article_path": str(path),
                        "chat_id": 2,
                    }
                )
        with patch.object(plugin, "MEDIA_ROOT", self.root / "other"):
            with self.assertRaises(ValueError):
                plugin.request_payload(
                    {
                        "op": "long_validate",
                        "request_id": "test-v1",
                        "article_path": str(path),
                    }
                )
