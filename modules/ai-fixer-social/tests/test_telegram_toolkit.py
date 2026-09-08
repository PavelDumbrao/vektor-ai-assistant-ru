import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from ai_fixer_social.broker import validate_request
from ai_fixer_social.db import StateStore
from ai_fixer_social.outbound import (
    get_attempt,
    manage_message,
    publish_post,
    resolve_management,
)
from ai_fixer_social.telegram_content import (
    TelegramRejected,
    execute_spec,
    prepare_spec,
)
from test_broker import plugin
from ai_fixer_plugin.preview import deliver_telegram_preview


POLL = {
    "kind": "poll",
    "question": "Что поручим ИИ?",
    "options": ["Идеи", "Черновик", "Проверку"],
}
QUIZ = {
    "kind": "quiz",
    "question": "Что такое MCP?",
    "options": ["Протокол инструментов", "Память"],
    "correct_option_ids": [0],
    "explanation": "MCP соединяет приложения с инструментами и данными.",
}


def asset(kind="photo", raw=b"\x89PNG\r\n\x1a\nimage", name="preview.png"):
    return {
        "kind": kind,
        "filename": name,
        "file_b64": base64.b64encode(raw).decode(),
        "caption": "<b>Тест</b>",
    }


class TelegramContractTests(unittest.TestCase):
    def test_tool_discovery_prefix_mentions_the_telegram_toolkit(self):
        description = plugin.SCHEMA["description"]
        self.assertIn("tg_capabilities", description[:400])
        self.assertIn("tg_preview", description[:400])
        self.assertIn("опросы", description[:400])

    def test_skill_frontmatter_description_is_validly_quoted(self):
        skill = (
            Path(__file__).resolve().parents[1]
            / "integrations/hermes/ai-fixer-editor/SKILL.md"
        )
        header = skill.read_text().split("---", 2)[1]
        description = next(
            line.split(":", 1)[1].strip()
            for line in header.splitlines()
            if line.startswith("description:")
        )
        self.assertIn("опросы", json.loads(description))
        self.assertIn("platforms: [linux]", header)

    def test_poll_regular_multiple_answers_timer_and_buttons(self):
        spec = prepare_spec(
            {
                **POLL,
                "allows_multiple_answers": True,
                "open_period": 3600,
                "buttons": [[{"text": "Канал", "url": "https://t.me/ProAiCommunity"}]],
            }
        )
        self.assertEqual(spec.method, "sendPoll")
        self.assertEqual(spec.params["type"], "regular")
        self.assertTrue(spec.params["is_anonymous"])
        self.assertEqual(spec.params["options"][0], {"text": "Идеи"})
        self.assertEqual(spec.params["open_period"], 3600)

    def test_quiz_uses_current_correct_option_ids_contract(self):
        spec = prepare_spec(QUIZ)
        self.assertEqual(spec.params["type"], "quiz")
        self.assertEqual(spec.params["correct_option_ids"], [0])
        self.assertNotIn("correct_option_id", spec.params)

    def test_quiz_rejects_invalid_correct_options(self):
        for value in ([], [2], [True], [1, 0], [0, 0]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare_spec({**QUIZ, "correct_option_ids": value})

    def test_non_anonymous_and_unbounded_polls_rejected(self):
        for data in (
            {**POLL, "is_anonymous": False},
            {**POLL, "question": "я" * 301},
            {**POLL, "options": ["one"] * 13},
            {**POLL, "open_period": 4},
            {**POLL, "open_period": 60, "close_date": 99999999},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                prepare_spec(data)

    def test_arbitrary_method_target_and_dangerous_actions_rejected(self):
        for extra in (
            {"chat_id": 2},
            {"method": "deleteMessage"},
            {"business_connection_id": "x"},
            {"allow_paid_broadcast": True},
            {"token": "secret"},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                prepare_spec({**POLL, **extra})
        for kind in ("delete", "ban", "invite", "promote", "send_message"):
            with self.assertRaises(ValueError):
                prepare_spec({"kind": kind, "message_id": 1})

    def test_url_buttons_have_no_callback_or_private_network_escape(self):
        for url in (
            "javascript:x",
            "http://example.com",
            "https://user:pass@example.com",
            "https://127.0.0.1/admin",
            "https://localhost/test",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                prepare_spec(
                    {
                        "kind": "text",
                        "text": "Тест",
                        "buttons": [[{"text": "X", "url": url}]],
                    }
                )
        with self.assertRaises(ValueError):
            prepare_spec(
                {
                    "kind": "text",
                    "text": "Тест",
                    "buttons": [
                        [
                            {
                                "text": "X",
                                "url": "https://example.com",
                                "callback_data": "grant",
                            }
                        ]
                    ],
                }
            )

    def test_all_asset_methods_and_size_limits(self):
        cases = [
            ("photo", b"\x89PNG\r\n\x1a\nimage", "a.png", "sendPhoto"),
            ("video", b"\0\0\0\x20ftypisom", "a.mp4", "sendVideo"),
            ("animation", b"GIF89a123", "a.gif", "sendAnimation"),
            ("audio", b"ID3audio", "a.mp3", "sendAudio"),
            ("voice", b"OggSopus", "a.ogg", "sendVoice"),
            ("document", b"%PDF-1.7", "a.pdf", "sendDocument"),
        ]
        for kind, raw, name, method in cases:
            with self.subTest(kind=kind):
                spec = prepare_spec(asset(kind, raw, name))
                self.assertEqual(spec.method, method)
                self.assertEqual(spec.files["file0"][1], raw)
                self.assertEqual(spec.params["caption"], "<b>Тест</b>")
        with self.assertRaises(ValueError):
            prepare_spec(asset("document", b"secret", ".env"))
        with self.assertRaises(ValueError):
            prepare_spec(asset("photo", b"secret", "a.png"))

    def test_album_is_2_to_10_native_items_and_not_a_longread(self):
        spec = prepare_spec(
            {
                "kind": "album",
                "items": [asset(), asset("video", b"\0\0\0\x20ftypisom", "a.mp4")],
            }
        )
        self.assertEqual(spec.method, "sendMediaGroup")
        self.assertEqual(len(spec.params["media"]), 2)
        self.assertEqual(spec.params["media"][1]["media"], "attach://file1")
        for data in (
            {"kind": "album", "items": [asset()]},
            {"kind": "album", "items": [asset(), asset()], "buttons": []},
        ):
            with self.assertRaises(ValueError):
                prepare_spec(data)

    def test_hash_covers_button_caption_and_bytes(self):
        original = prepare_spec(asset()).payload_hash
        self.assertNotEqual(
            original, prepare_spec({**asset(), "caption": "Новый"}).payload_hash
        )

        self.assertNotEqual(
            original, prepare_spec(asset(raw=b"\x89PNG\r\n\x1a\nother")).payload_hash
        )
        self.assertNotEqual(
            original,
            prepare_spec(
                {**asset(), "buttons": [[{"text": "X", "url": "https://example.com"}]]}
            ).payload_hash,
        )

    def test_rich_article_edit_uses_native_rich_message(self):
        data = {
            "kind": "edit_article",
            "message_id": 8,
            "article": {
                "html": "<h1>Новая статья</h1><p>" + "Текст " * 1000 + "</p>",
                "photos": [],
            },
        }
        spec = prepare_spec(data)
        self.assertEqual(spec.method, "editMessageText")
        self.assertIn("rich_message", spec.params)
        self.assertFalse(spec.creates_message)

    def test_poll_transport_is_one_native_request(self):
        calls = []

        def handle(request):
            calls.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "message_id": 5,
                        "chat": {"id": 1},
                        "poll": {"id": "poll-1"},
                    },
                },
            )

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ) as client:
                return await execute_spec(
                    client, "https://api.example/bot-test", 1, prepare_spec(QUIZ)
                )

        result = asyncio.run(run())
        self.assertEqual(result["message_ids"], [5])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["chat_id"], 1)
        self.assertEqual(calls[0]["correct_option_ids"], [0])

    def test_explicit_rejection_and_error_with_result_are_distinguished(self):
        async def run(payload, status):
            transport = httpx.MockTransport(
                lambda request: httpx.Response(status, json=payload)
            )
            async with httpx.AsyncClient(transport=transport) as client:
                return await execute_spec(
                    client, "https://api.example/bot-test", 1, prepare_spec(POLL)
                )

        with self.assertRaises(TelegramRejected):
            asyncio.run(run({"ok": False, "error_code": 400}, 400))
        with self.assertRaisesRegex(RuntimeError, "requires_readback"):
            asyncio.run(
                run({"ok": False, "error_code": 400, "result": {"message_id": 8}}, 400)
            )
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            asyncio.run(run({"ok": False, "error_code": 502}, 502))


class ToolkitDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.settings = SimpleNamespace(
            telegram_channel_id=-100, telegram_channel_username="test"
        )
        self.calls = []

        async def send_spec(**kwargs):
            self.calls.append(kwargs)
            spec = kwargs["spec"]
            mid = spec.params.get("message_id", 100)
            message = {
                "message_id": mid,
                "chat": {"id": -100},
                "text": spec.params.get("text", ""),
            }
            if spec.kind in {"poll", "quiz"}:
                message["poll"] = {
                    "id": "poll-1",
                    "question": "Q",
                    "options": [],
                    "total_voter_count": 0,
                    "is_closed": False,
                }
            return {"messages": [message], "message_ids": [mid]}

        self.telegram = SimpleNamespace(send_spec=send_spec)

    def publish(self, **kwargs):
        return asyncio.run(
            publish_post(
                self.settings,
                self.store,
                self.telegram,
                request_id="toolkit-poll-v1",
                text="",
                telegram_spec=POLL,
                **kwargs,
            )
        )

    def owned(self, kind="text", message=None):
        self.store.record_telegram_object(
            -100,
            message or {"message_id": 10, "text": "Старый текст", "chat": {"id": -100}},
            kind,
        )

    def manage(self, data, **kwargs):
        return asyncio.run(
            manage_message(
                self.settings,
                self.store,
                self.telegram,
                request_id="toolkit-edit-v1",
                data=data,
                **kwargs,
            )
        )

    def test_public_poll_uses_shared_publication_dedup_and_registry(self):
        dry = self.publish(dry_run=True)
        self.assertTrue(dry["ok"])
        self.assertFalse(self.calls)
        first = self.publish(confirm_hash=dry["payload_hash"])
        self.assertTrue(first["ok"])
        self.assertTrue(self.publish(confirm_hash=dry["payload_hash"])["deduplicated"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.telegram_object(-100, 100)["kind"], "poll")

    def test_foreign_message_cannot_be_edited_or_pinned(self):
        for kind in ("pin", "stop_poll", "edit_text"):
            data = {"kind": kind, "message_id": 999}
            if kind == "edit_text":
                data["text"] = "New"
            self.assertFalse(self.manage(data, dry_run=True)["ok"])
        self.assertFalse(self.calls)

    def test_rich_edit_requires_an_owned_rich_post(self):
        data = {
            "kind": "edit_article",
            "message_id": 10,
            "article": {"html": "<p>Обновление</p>", "photos": []},
        }
        self.owned("text")
        self.assertFalse(self.manage(data, dry_run=True)["ok"])
        self.owned("rich", {"message_id": 10, "rich_message": {"blocks": []}})
        self.assertTrue(self.manage(data, dry_run=True)["ok"])

    def test_edit_preserves_revision_and_requires_exact_confirmation(self):
        self.owned()
        data = {"kind": "edit_text", "message_id": 10, "text": "Новый текст"}
        dry = self.manage(data, dry_run=True)
        self.assertEqual(
            self.manage(data, confirm_hash="wrong")["error"],
            "confirmation_payload_mismatch",
        )
        result = self.manage(data, confirm_hash=dry["payload_hash"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.calls), 1)
        before = self.store.conn.execute(
            "SELECT before_json FROM telegram_revisions"
        ).fetchone()[0]
        self.assertEqual(json.loads(before)["text"], "Старый текст")
        self.assertEqual(
            self.store.telegram_object(-100, 10)["message"]["text"], "Новый текст"
        )
        self.assertTrue(
            self.manage(data, confirm_hash=dry["payload_hash"])["deduplicated"]
        )

    def test_media_replacement_preserves_existing_caption_and_buttons(self):
        self.owned(
            "photo",
            {
                "message_id": 10,
                "caption": "Старый заголовок",
                "caption_entities": [{"type": "bold", "offset": 0, "length": 6}],
                "reply_markup": {
                    "inline_keyboard": [[{"text": "Канал", "url": "https://t.me/test"}]]
                },
            },
        )
        media = asset()
        data = {
            "kind": "edit_media",
            "message_id": 10,
            "media_type": "photo",
            "filename": media["filename"],
            "file_b64": media["file_b64"],
        }
        prepared, _ = resolve_management(self.settings, self.store, data)
        self.assertEqual(prepared.params["media"]["caption"], "Старый заголовок")
        self.assertTrue(prepared.params["media"]["caption_entities"])
        self.assertTrue(prepared.params["reply_markup"])

    def test_poll_update_only_changes_known_aggregate(self):
        self.publish()
        self.assertFalse(
            self.store.update_poll({"id": "unknown", "total_voter_count": 9})
        )
        self.assertTrue(
            self.store.update_poll(
                {"id": "poll-1", "total_voter_count": 3, "is_closed": False}
            )
        )
        self.assertEqual(
            self.store.telegram_object(-100, 100)["message"]["poll"][
                "total_voter_count"
            ],
            3,
        )

    def test_poll_storage_never_keeps_individual_voters(self):
        self.publish()
        self.store.update_poll(
            {
                "id": "poll-1",
                "total_voter_count": 4,
                "user": {"id": 123},
                "recent_voters": [123],
            }
        )
        aggregate = self.store.telegram_object(-100, 100)["message"]["poll"]
        self.assertNotIn("user", aggregate)
        self.assertNotIn("recent_voters", aggregate)
        self.assertEqual(aggregate["question"], "Q")

    def test_native_poll_close_updates_aggregate_and_preserves_revision(self):
        self.publish()

        async def close(**kwargs):
            return {
                "messages": [],
                "message_ids": [100],
                "poll": {"id": "poll-1", "is_closed": True, "total_voter_count": 5},
            }

        self.telegram.send_spec = close
        data = {"kind": "stop_poll", "message_id": 100}
        digest = self.manage(data, dry_run=True)["payload_hash"]
        self.assertTrue(self.manage(data, confirm_hash=digest)["ok"])
        self.assertTrue(
            self.store.telegram_object(-100, 100)["message"]["poll"]["is_closed"]
        )

    def test_blocked_native_guard_does_not_leave_an_approval_permit(self):
        controller = plugin.Controller("1")
        controller.observe(
            session_id="s",
            turn_id="t",
            sender_id="1",
            platform="telegram",
            chat_type="dm",
            raw_user_message="Сделай",
            is_internal_event=False,
        )
        args = {
            "op": "tg_manage",
            "request_id": "edit-v1",
            "telegram_spec_path": "/missing.json",
            "confirm_hash": "x",
        }
        self.assertEqual(
            controller.guard(
                tool_name="ai_fixer", args=args, session_id="s", turn_id="t"
            )["action"],
            "block",
        )
        self.assertNotIn("s", controller.permits)

    def test_uncertain_mutation_never_retries(self):
        self.owned()

        async def fail(**kwargs):
            self.calls.append(kwargs)
            raise TimeoutError("secret must not escape")

        self.telegram.send_spec = fail
        data = {"kind": "pin", "message_id": 10}
        digest = self.manage(data, dry_run=True)["payload_hash"]
        result = self.manage(data, confirm_hash=digest)
        self.assertFalse(result["retry_allowed"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertFalse(self.manage(data, confirm_hash=digest)["ok"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(
            get_attempt(self.store, "manage:toolkit-edit-v1")["status"], "uncertain"
        )

    def test_private_poll_preview_deduplicates_without_publication(self):
        sender = Mock(return_value={"message_id": 50, "has_content": True})
        args = dict(
            owner_id="1",
            request_id="private-poll-v1",
            spec=POLL,
            ledger_path=self.root / "private.sqlite3",
            sender=sender,
        )
        self.assertFalse(deliver_telegram_preview(**args)["published"])
        self.assertTrue(deliver_telegram_preview(**args)["deduplicated"])
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.store.stats()["publications"], 0)

    def test_rejected_request_does_not_freeze_unrelated_private_previews(self):
        sender = Mock(side_effect=TelegramRejected(400))
        args = dict(
            owner_id="1",
            request_id="rejected-poll-v1",
            spec=POLL,
            ledger_path=self.root / "private.sqlite3",
            sender=sender,
        )
        result = deliver_telegram_preview(**args)
        self.assertFalse(result["readback_required"])
        self.assertEqual(result["telegram_error_code"], 400)
        self.assertEqual(
            deliver_telegram_preview(**args)["error"], "previous_request_rejected"
        )
        sender.side_effect = None
        sender.return_value = {"message_id": 7, "has_content": True}
        self.assertTrue(
            deliver_telegram_preview(**{**args, "request_id": "corrected-poll-v2"})[
                "ok"
            ]
        )

    def test_native_plugin_paths_and_permissions(self):
        file = self.root / "poll.json"
        file.write_text(json.dumps(POLL))
        args = {
            "op": "tg_publish",
            "request_id": "poll-v1",
            "telegram_spec_path": str(file),
            "confirm_hash": "x",
        }
        controller = plugin.Controller("1")
        controller.observe(
            session_id="s",
            turn_id="t",
            sender_id="1",
            platform="telegram",
            chat_type="dm",
            raw_user_message="Подготовь",
            is_internal_event=False,
        )
        with patch.object(plugin, "MEDIA_ROOT", self.root):
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
            payload = plugin.request_payload(args)
            self.assertEqual(payload["spec"], POLL)
            validate_request(payload)
            with self.assertRaises(ValueError):
                plugin.request_payload({**args, "chat_id": 2})
        with patch.object(plugin, "MEDIA_ROOT", self.root / "other"):
            with self.assertRaises(ValueError):
                plugin.request_payload(args)
