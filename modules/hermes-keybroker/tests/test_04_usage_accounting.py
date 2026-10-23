"""Test 4: Usage accounting in SQLite DB (OpenAI tokens, ElevenLabs chars, SSE usage)."""

import json
import os
import sqlite3
import tempfile
import unittest
from aiohttp import ClientSession, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestUsageAccounting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["API_KEY"] = "test-token"

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("API_KEY", None)

    async def test_usage_extraction_and_db_records(self):
        # 1. Non-streaming OpenAI endpoint
        async def openai_handler(req: web.Request):
            return web.json_response({
                "id": "chatcmpl-123",
                "model": "gpt-4o-custom",
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {
                    "prompt_tokens": 17,
                    "completion_tokens": 33,
                    "total_tokens": 50,
                },
            })

        # 2. ElevenLabs endpoint with x-character-count header
        async def elevenlabs_handler(req: web.Request):
            return web.Response(
                body=b"AUDIO_DATA_BYTES",
                status=200,
                headers={"x-character-count": "240", "Content-Type": "audio/mpeg"},
            )

        # 3. SSE endpoint with usage in last chunk
        async def sse_handler(req: web.Request):
            resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
            await resp.prepare(req)
            chunk1 = json.dumps({"model": "gpt-4o-mini", "choices": [{"delta": {"content": "one"}}]})
            await resp.write(f"data: {chunk1}\n\n".encode("utf-8"))
            chunk2 = json.dumps({
                "model": "gpt-4o-mini",
                "choices": [],
                "usage": {"prompt_tokens": 45, "completion_tokens": 55},
            })
            await resp.write(f"data: {chunk2}\n\n".encode("utf-8"))
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        self.mock_upstream.app.router.add_post("/v1/chat/completions", openai_handler)
        self.mock_upstream.app.router.add_post("/v1/text-to-speech/voice1", elevenlabs_handler)
        self.mock_upstream.app.router.add_post("/v1/stream", sse_handler)

        upstream_url = await self.mock_upstream.start()
        lane_port = test_utils.unused_port()

        lanes = [{"client": "bebov", "uid": 1009, "port": lane_port}]
        routes = [
            {
                "prefix": "/openai",
                "upstream": upstream_url,
                "kind": "openai",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "API_KEY"},
            },
            {
                "prefix": "/elevenlabs",
                "upstream": upstream_url,
                "kind": "elevenlabs",
                "auth": {"header": "xi-api-key", "format": "{key}", "key_env": "API_KEY"},
            },
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        async with ClientSession() as session:
            # Call 1: OpenAI JSON
            url1 = f"http://127.0.0.1:{lane_port}/openai/v1/chat/completions"
            async with session.post(url1, json={"prompt": "hello"}) as resp:
                self.assertEqual(resp.status, 200)
                await resp.read()

            # Call 2: ElevenLabs
            url2 = f"http://127.0.0.1:{lane_port}/elevenlabs/v1/text-to-speech/voice1"
            async with session.post(url2, json={"text": "hello elevenlabs"}) as resp:
                self.assertEqual(resp.status, 200)
                await resp.read()

            # Call 3: SSE with usage
            url3 = f"http://127.0.0.1:{lane_port}/openai/v1/stream"
            async with session.post(url3, json={"stream": True}) as resp:
                self.assertEqual(resp.status, 200)
                await resp.read()

        # Flush usage tracker to ensure all records written to SQLite
        await self.app.usage_tracker.flush()

        # Inspect SQLite DB directly
        db_path = os.path.join(self.tmpdir.name, "usage.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        rows = cursor.execute("SELECT * FROM requests ORDER BY id ASC").fetchall()
        self.assertEqual(len(rows), 3)

        # 1. Verify OpenAI non-streaming JSON record
        r1 = rows[0]
        self.assertEqual(r1["client"], "bebov")
        self.assertEqual(r1["port"], lane_port)
        self.assertEqual(r1["route"], "/openai")
        self.assertEqual(r1["kind"], "openai")
        self.assertEqual(r1["status"], 200)
        self.assertGreater(r1["req_bytes"], 0)
        self.assertGreater(r1["resp_bytes"], 0)
        self.assertGreaterEqual(r1["duration_ms"], 0)
        self.assertEqual(r1["model"], "gpt-4o-custom")
        self.assertEqual(r1["prompt_tokens"], 17)
        self.assertEqual(r1["completion_tokens"], 33)

        # 2. Verify ElevenLabs record with character count
        r2 = rows[1]
        self.assertEqual(r2["client"], "bebov")
        self.assertEqual(r2["route"], "/elevenlabs")
        self.assertEqual(r2["kind"], "elevenlabs")
        self.assertEqual(r2["status"], 200)
        self.assertEqual(r2["chars"], 240)
        self.assertGreater(r2["req_bytes"], 0)
        self.assertGreater(r2["resp_bytes"], 0)

        # 3. Verify SSE stream record with usage extracted
        r3 = rows[2]
        self.assertEqual(r3["client"], "bebov")
        self.assertEqual(r3["route"], "/openai")
        self.assertEqual(r3["status"], 200)
        self.assertEqual(r3["model"], "gpt-4o-mini")
        self.assertEqual(r3["prompt_tokens"], 45)
        self.assertEqual(r3["completion_tokens"], 55)

        conn.close()
