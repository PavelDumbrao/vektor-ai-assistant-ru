"""Test 19: Payload size limits and STT body validation.

Covers:
- Chunked body exceeding limit -> 413.
- STT without Content-Length -> 411.
- STT multipart exceeding limit -> 413 before reading whole body.
- STT JSON without input_audio -> 400 stt_body_unsupported.
- STT with unsupported Content-Type (e.g. text/plain) -> 415.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

from aiohttp import ClientSession, FormData, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestPayloadAndSTT(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["PAYLOAD_TEST_KEY"] = "sk-payload-test-key"

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("PAYLOAD_TEST_KEY", None)

    async def test_chunked_body_exceeding_limit_returns_413(self):
        """Chunked request body exceeding max_body_mb returns 413 payload_too_large."""
        upstream_received = []

        async def handler(request: web.Request):
            body = await request.read()
            upstream_received.append(body)
            return web.Response(text="ok")

        self.mock_upstream.app.router.add_post("/v1/generic", handler)
        upstream_url = await self.mock_upstream.start()
        port = test_utils.unused_port()

        lanes = [{"client": "chunk_client", "uid": 1001, "port": port}]
        routes = [
            {
                "prefix": "/generic",
                "upstream": upstream_url + "/v1",
                "kind": "generic",
                "max_body_mb": 1,
                "allow": "*",
                "auth": {
                    "header": "Authorization",
                    "format": "Bearer {key}",
                    "key_env": "PAYLOAD_TEST_KEY",
                },
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        async def chunk_stream():
            # 2 chunks of 600 KB each = 1.2 MB > 1 MB max_body_mb
            yield b"a" * (600 * 1024)
            await asyncio.sleep(0.01)
            yield b"b" * (600 * 1024)

        async with ClientSession() as session:
            url = f"http://127.0.0.1:{port}/generic/generic"
            async with session.post(url, data=chunk_stream()) as resp:
                self.assertEqual(resp.status, 413)
                data = await resp.json()
                self.assertEqual(data.get("error"), "payload_too_large")

        # Upstream should not have completed receiving request
        self.assertEqual(len(upstream_received), 0)

    async def test_stt_content_length_and_format_validation(self):
        """STT route enforces Content-Length (411), max_body_mb (413), input_audio in JSON (400), media type (415)."""
        upstream_calls = []

        async def stt_handler(request: web.Request):
            body = await request.read()
            upstream_calls.append(body)
            return web.json_response({"text": "transcription result"})

        self.mock_upstream.app.router.add_post("/v1/audio/transcriptions", stt_handler)
        upstream_url = await self.mock_upstream.start()
        port = test_utils.unused_port()

        lanes = [{"client": "stt_client", "uid": 1002, "port": port}]
        routes = [
            {
                "prefix": "/stt",
                "upstream": upstream_url + "/v1",
                "kind": "stt",
                "max_body_mb": 1,
                "allow": "*",
                "auth": {
                    "header": "Authorization",
                    "format": "Bearer {key}",
                    "key_env": "PAYLOAD_TEST_KEY",
                },
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        stt_url = f"http://127.0.0.1:{port}/stt/audio/transcriptions"

        async with ClientSession() as session:
            # 1. STT without Content-Length -> 411
            async def chunk_gen():
                yield b"audio chunk"

            async with session.post(stt_url, data=chunk_gen()) as resp:
                self.assertEqual(resp.status, 411)
                data = await resp.json()
                self.assertEqual(data.get("error"), "length_required")

            # 2. STT multipart exceeding limit -> 413 before reading whole body
            fd = FormData()
            fd.add_field("file", b"x" * (2 * 1024 * 1024), filename="voice.ogg", content_type="audio/ogg")
            async with session.post(stt_url, data=fd) as resp:
                self.assertEqual(resp.status, 413)
                data = await resp.json()
                self.assertEqual(data.get("error"), "payload_too_large")

            # 3. STT JSON without input_audio -> 400 stt_body_unsupported
            async with session.post(
                stt_url,
                json={"model": "whisper-large-v3", "language": "en"},
            ) as resp:
                self.assertEqual(resp.status, 400)
                data = await resp.json()
                self.assertEqual(data.get("error"), "stt_body_unsupported")

            # 4. STT with unsupported Content-Type (text/plain) -> 415
            async with session.post(
                stt_url,
                data=b"just plain text",
                headers={"Content-Type": "text/plain"},
            ) as resp:
                self.assertEqual(resp.status, 415)
                data = await resp.json()
                self.assertEqual(data.get("error"), "unsupported_media_type")

        # Upstream was never called for any of these invalid requests
        self.assertEqual(len(upstream_calls), 0)
