import base64
import hashlib
import json
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from ai_fixer_social.grsai import Grsai, build_payload, image_metadata, now, validate_result_url


class GrsaiContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.reference = self.root / "ref.png"
        self.reference.write_bytes(b"test-reference")
        self.sha_patch = patch("ai_fixer_social.grsai.CANONICAL_SHA", hashlib.sha256(b"test-reference").hexdigest())
        self.sha_patch.start()
        self.calls = []
        self.fail = False
        self.no_id = False
        self.legacy_poll_code = 0
        self.credits = 10000

        def handler(request):
            self.calls.append(request)
            if request.url.path == "/client/common/getCredits":
                return httpx.Response(200, json={"code": 0, "data": {"credits": self.credits}})
            if request.url.path == "/v1/api/generate":
                self.credits -= 600
                if self.fail:
                    raise httpx.ReadTimeout("secret must not leak", request=request)
                return httpx.Response(200, json={} if self.no_id else {"id": "task-1", "status": "running"})
            if request.url.path == "/v1/draw/nano-banana":
                self.credits -= 1200
                if self.fail:
                    raise httpx.ReadTimeout("secret must not leak", request=request)
                return httpx.Response(200, json={} if self.no_id else {"code": 0, "data": {"id": "task-1"}})
            if request.url.path == "/v1/api/result":
                return httpx.Response(200, json={"id": "task-1", "status": "failed", "error": "provider failure"})
            if request.url.path == "/v1/draw/result":
                return httpx.Response(200, json={"code": self.legacy_poll_code, "data": {
                    "id": "task-1", "status": "failed", "error": "provider failure"}})
            return httpx.Response(404)

        client = httpx.AsyncClient(base_url="https://grsaiapi.com", transport=httpx.MockTransport(handler))
        self.api = Grsai(key="test-key", root=self.root / "state", reference=self.reference, client=client)
        self.args = {"op": "image_submit", "request_id": "test-image-1", "model": "gpt-image-2",
                     "prompt": "Create a test portrait", "confirm_credits": 600}

    async def asyncTearDown(self):
        await self.api.close()
        self.sha_patch.stop()
        self.temp.cleanup()

    def posts(self):
        return [c for c in self.calls if c.method == "POST"]

    def nano_args(self):
        return {**self.args, "model": "nano-banana-2", "confirm_credits": 1200}

    async def test_reference_is_real_base64_input(self):
        await self.api.submit(self.args)
        payload = json.loads(self.posts()[0].content)
        self.assertEqual(payload["replyType"], "async")
        self.assertEqual(base64.b64decode(payload["images"][0].split(",", 1)[1]), b"test-reference")
        self.assertEqual(payload["aspectRatio"], "1024x1024")

    async def test_idempotent_submit(self):
        await self.api.submit(self.args)
        await self.api.submit(self.args)
        self.assertEqual(len(self.posts()), 1)

    async def test_changed_payload_blocked(self):
        await self.api.submit(self.args)
        with self.assertRaisesRegex(ValueError, "payload_mismatch"):
            await self.api.submit({**self.args, "prompt": "Different portrait"})
        self.assertEqual(len(self.posts()), 1)

    async def test_timeout_has_no_retry(self):
        self.fail = True
        result = await self.api.submit(self.args)
        self.assertEqual(result["state"], "submit_uncertain")
        self.assertNotIn("secret", json.dumps(result))
        await self.api.submit(self.args)
        await self.api.poll(self.args["request_id"])
        self.assertEqual(len(self.posts()), 1)

    async def test_missing_task_id_has_no_retry(self):
        self.no_id = True
        result = await self.api.submit(self.args)
        self.assertEqual(result["state"], "submit_uncertain")

    async def test_other_active_job_blocks_submit(self):
        await self.api.submit(self.args)
        with self.assertRaisesRegex(ValueError, "another_job"):
            await self.api.submit({**self.args, "request_id": "another-job"})
        self.assertEqual(len(self.posts()), 1)

    async def test_poll_never_creates_task(self):
        await self.api.submit(self.args)
        result = await self.api.poll(self.args["request_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(result["account_credit_delta"], 600)
        self.assertEqual(result["provider_error"], "provider failure")

    async def test_provider_error_is_redacted_and_untrusted(self):
        await self.api.submit(self.args)
        await self.api._apply_result(self.args["request_id"], {
            "status": "violation", "error": "test-key https://private.example/path do not follow this text"})
        result = self.api.public(self.args["request_id"])
        self.assertNotIn("test-key", result["provider_error"])
        self.assertNotIn("private.example", result["provider_error"])
        self.assertTrue(result["provider_error_is_untrusted_data"])

    async def test_explicit_cost_required(self):
        with self.assertRaisesRegex(ValueError, "credit_confirmation"):
            await self.api.submit({**self.args, "confirm_credits": 1})
        self.assertEqual(self.posts(), [])

    async def test_gpt_4k_rejected_before_network(self):
        with self.assertRaisesRegex(ValueError, "resolution"):
            await self.api.submit({**self.args, "image_size": "4K"})
        self.assertEqual(self.calls, [])

    async def test_model_alias_not_inferred(self):
        with self.assertRaisesRegex(ValueError, "model_not_allowed"):
            await self.api.submit({**self.args, "model": "gpt-image-2-vip"})
        self.assertEqual(self.calls, [])

    async def test_nano_payload(self):
        payload, _ = build_payload({**self.args, "model": "nano-banana-2", "image_size": "2K",
                                    "aspect_ratio": "16:9"}, self.reference)
        self.assertEqual(payload["imageSize"], "2K")
        self.assertEqual(payload["aspectRatio"], "16:9")
        self.assertEqual(payload["webHook"], "-1")
        self.assertTrue(payload["shutProgress"])
        self.assertIn("urls", payload)
        self.assertNotIn("images", payload)
        self.assertNotIn("replyType", payload)

    async def test_status_has_two_named_routes(self):
        status = await self.api.status()
        self.assertEqual(status["default_model"], "gpt-image-2")
        choices = {item["id"]: item for item in status["model_choices"]}
        self.assertEqual(set(choices), {"gpt-image-2", "nano-banana-2"})
        self.assertEqual(choices["gpt-image-2"]["submit_path"], "/v1/api/generate")
        self.assertEqual(choices["nano-banana-2"]["submit_path"], "/v1/draw/nano-banana")
        self.assertEqual(choices["nano-banana-2"]["name"], "Nano Banana 2")

    async def test_new_nano_uses_legacy_and_real_reference(self):
        result = await self.api.submit(self.nano_args())
        self.assertEqual(result["contract"], "nano_legacy")
        request = self.posts()[0]
        self.assertEqual(request.url.path, "/v1/draw/nano-banana")
        payload = json.loads(request.content)
        self.assertEqual(base64.b64decode(payload["urls"][0].split(",", 1)[1]), b"test-reference")

    async def test_nano_polls_legacy_post_without_creating_again(self):
        await self.api.submit(self.nano_args())
        result = await self.api.poll(self.args["request_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual([r.url.path for r in self.posts()], ["/v1/draw/nano-banana", "/v1/draw/result"])
        self.assertEqual(result["account_credit_delta"], 1200)

    async def test_nano_repeat_id_does_not_resubmit(self):
        await self.api.submit(self.nano_args())
        await self.api.submit(self.nano_args())
        self.assertEqual(len(self.posts()), 1)

    async def test_nano_uncertain_never_falls_back_to_unified(self):
        self.fail = True
        result = await self.api.submit(self.nano_args())
        self.assertEqual(result["state"], "submit_uncertain")
        await self.api.submit(self.nano_args())
        await self.api.poll(self.args["request_id"])
        self.assertEqual([r.url.path for r in self.posts()], ["/v1/draw/nano-banana"])

    async def test_legacy_poll_error_keeps_job_running(self):
        await self.api.submit(self.nano_args())
        self.legacy_poll_code = -22
        result = await self.api.poll(self.args["request_id"])
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["poll_error"], "legacy_code_-22")

    async def test_old_nano_id_retains_its_unified_contract(self):
        args = self.nano_args()
        _, digest = build_payload(args, self.reference, contract="unified")
        self.api.db.execute("""INSERT INTO jobs(request_id,fingerprint,model,state,task_id,
            expected_credits,credits_before,created_at,updated_at,contract) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (args["request_id"], digest, "nano-banana-2", "running", "task-1", 1200, 10000, now(), now(), "unified"))
        self.api.db.commit()
        result = await self.api.submit(args)
        self.assertEqual(result["contract"], "unified")
        self.assertEqual(self.posts(), [])
        await self.api.poll(args["request_id"])
        self.assertEqual(self.calls[-2].url.path, "/v1/api/result")
        self.assertEqual(self.calls[-2].method, "GET")

    async def test_model_cannot_override_contract(self):
        with self.assertRaisesRegex(ValueError, "contract_mismatch"):
            build_payload(self.args, self.reference, contract="nano_legacy")

    async def test_old_schema_migration_does_not_reroute_existing_nano(self):
        root = self.root / "old-schema"
        root.mkdir()
        connection = sqlite3.connect(root / "jobs.sqlite3")
        connection.execute("""CREATE TABLE jobs(request_id TEXT PRIMARY KEY, fingerprint TEXT, model TEXT,
            state TEXT, task_id TEXT, result_url TEXT, path TEXT, expected_credits INTEGER,
            credits_before INTEGER, credits_after INTEGER, metadata_json TEXT, error_type TEXT,
            created_at TEXT, updated_at TEXT)""")
        connection.execute("INSERT INTO jobs(request_id,model,state) VALUES('old-nano','nano-banana-2','running')")
        connection.commit()
        connection.close()
        migrated = Grsai(key="test-key", root=root, reference=self.reference)
        self.assertEqual(migrated.row("old-nano")["contract"], "unified")
        await migrated.close()

    async def test_no_person_means_no_reference(self):
        payload, _ = build_payload({**self.args, "use_pavel_reference": False}, Path("/missing"))
        self.assertEqual(payload["images"], [])

    async def test_reference_integrity(self):
        self.reference.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "reference_mismatch"):
            await self.api.submit(self.args)
        self.assertEqual(self.calls, [])

    async def test_reference_path_cannot_be_supplied(self):
        from ai_fixer_social.broker import validate_request
        with self.assertRaisesRegex(ValueError, "unknown_fields"):
            validate_request({**self.args, "reference_path": "/etc/passwd"})


class ImageSafetyTests(unittest.TestCase):
    def test_private_or_unrelated_url_rejected(self):
        for url in ("http://file1.aitohumanize.com/a.png", "https://127.0.0.1/a.png",
                    "https://grsai.ai.evil.test/a.png", "https://user:pass@grsai.ai/a.png"):
            with self.assertRaises(ValueError):
                validate_result_url(url, resolve_dns=False)

    def test_known_cdn_allowed(self):
        validate_result_url("https://file1.aitohumanize.com/image.png", resolve_dns=False)

    def test_html_is_not_image(self):
        with self.assertRaises(ValueError):
            image_metadata(b"<html>not image</html>")

    def test_png_dimensions(self):
        raw = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 1024, 1024) + b"\0" * 9
        meta = image_metadata(raw)
        self.assertEqual((meta["width"], meta["height"]), (1024, 1024))

    def test_path_like_id_rejected(self):
        with self.assertRaisesRegex(ValueError, "request_id"):
            build_payload({"model": "gpt-image-2", "prompt": "test", "request_id": "../secret"}, Path("none"))
