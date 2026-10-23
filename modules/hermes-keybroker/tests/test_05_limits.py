"""Test 5: Lane rate limits (elevenlabs_chars_month and rpm)."""

import os
import sqlite3
import tempfile
import time
import unittest
from aiohttp import ClientSession, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestLaneLimits(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["LIMITS_TEST_KEY"] = "test-key"

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("LIMITS_TEST_KEY", None)

    async def test_elevenlabs_monthly_limit_and_rpm(self):
        upstream_calls = 0

        async def upstream_handler(req: web.Request):
            nonlocal upstream_calls
            upstream_calls += 1
            return web.Response(
                body=b"AUDIO",
                status=200,
                headers={"x-character-count": "100", "Content-Type": "audio/mpeg"},
            )

        self.mock_upstream.app.router.add_post("/v1/tts", upstream_handler)
        upstream_url = await self.mock_upstream.start()
        lane_port = test_utils.unused_port()

        lanes = [
            {
                "client": "bebov",
                "uid": 1009,
                "port": lane_port,
                "limits": {
                    "elevenlabs_chars_month": 300,
                    "rpm": 3,
                },
            }
        ]
        routes = [
            {
                "prefix": "/elevenlabs",
                "upstream": upstream_url,
                "kind": "elevenlabs",
                "auth": {
                    "header": "xi-api-key",
                    "format": "{key}",
                    "key_env": "LIMITS_TEST_KEY",
                },
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        # 1. Insert records from previous month into usage.db (should NOT count towards this month's limit)
        past_ts = time.time() - (40 * 86400)  # 40 days ago
        db_path = os.path.join(self.tmpdir.name, "usage.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO requests (ts, client, port, route, kind, method, path, status, req_bytes, resp_bytes, duration_ms, chars)
            VALUES (?, 'bebov', ?, '/elevenlabs', 'elevenlabs', 'POST', '/elevenlabs/v1/tts', 200, 10, 10, 5, 1000)
            """,
            (past_ts, lane_port),
        )
        conn.commit()
        conn.close()

        url = f"http://127.0.0.1:{lane_port}/elevenlabs/v1/tts"

        async with ClientSession() as session:
            # First 3 requests: within rpm (3) and chars (100 each = 300 total)
            for i in range(3):
                async with session.post(url, json={"text": "hi"}) as resp:
                    self.assertEqual(resp.status, 200)

            self.assertEqual(upstream_calls, 3)

            # 4th request: exceeds rpm (3) AND chars (used 300 >= limit 300)
            # Upstream should NOT be called!
            async with session.post(url, json={"text": "exceed"}) as resp:
                self.assertEqual(resp.status, 429)
                data = await resp.json()
                self.assertEqual(data["error"], "lane_limit")
                self.assertIn(data["limit"], ("rpm", "elevenlabs_chars_month"))

            # Crucial check: upstream was NOT called for the 4th request
            self.assertEqual(upstream_calls, 3)
