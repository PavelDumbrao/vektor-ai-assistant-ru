"""Test 18: In-flight limits, concurrency semaphore, rpm exclusion, and ElevenLabs character reservation."""

import asyncio
import os
import tempfile
import unittest
from aiohttp import ClientSession, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestLimitsInflightConcurrency(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["LIMIT_TEST_KEY"] = "limit-key"

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("LIMIT_TEST_KEY", None)

    async def test_rpm_inflight_and_concurrency_semaphore(self):
        async def slow_handler(req: web.Request):
            await asyncio.sleep(0.15)
            return web.json_response({"ok": True})

        self.mock_upstream.app.router.add_get("/slow", slow_handler)
        upstream_url = await self.mock_upstream.start()
        lane_port = test_utils.unused_port()

        lanes = [
            {
                "client": "bebov",
                "uid": 1009,
                "port": lane_port,
                "limits": {
                    "rpm": 5,
                    "concurrency": 2,
                },
            }
        ]
        routes = [
            {
                "prefix": "/api",
                "upstream": upstream_url,
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "LIMIT_TEST_KEY"},
                "allow": "*",
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        async with ClientSession() as session:
            url = f"http://127.0.0.1:{lane_port}/api/slow"

            # 1. Test concurrency: 2
            # Launch 2 simultaneous requests that take 0.15s, and a 3rd immediate one
            t1 = asyncio.create_task(session.get(url))
            t2 = asyncio.create_task(session.get(url))
            await asyncio.sleep(0.02)  # Give t1 and t2 time to acquire semaphore

            # 3rd simultaneous request must get 429 concurrency limit immediately
            async with session.get(url) as resp3:
                self.assertEqual(resp3.status, 429)
                data3 = await resp3.json()
                self.assertEqual(data3.get("limit"), "concurrency")

            r1 = await t1
            r2 = await t2
            self.assertEqual(r1.status, 200)
            self.assertEqual(r2.status, 200)
            await r1.release()
            await r2.release()

        # 2. Test 10 parallel requests with rpm: 5 (from lane with concurrency: 10)
        # Update lane concurrency to 10 so semaphore doesn't trigger first
        self.app.lanes[lane_port].limits.concurrency = 10
        self.app.semaphores[lane_port] = asyncio.Semaphore(10)

        async with ClientSession() as session:
            url = f"http://127.0.0.1:{lane_port}/api/slow"
            # We already had 2 successful requests above.
            # With rpm: 5, only 3 more requests can succeed, the rest must get 429 rpm limit!
            tasks = [asyncio.create_task(session.get(url)) for _ in range(8)]
            responses = await asyncio.gather(*tasks)
            statuses = [r.status for r in responses]
            for r in responses:
                await r.release()

            # Out of 8, exactly 3 succeed (total 5 for the window), and 5 get 429
            successes = sum(1 for s in statuses if s == 200)
            limited = sum(1 for s in statuses if s == 429)
            self.assertEqual(successes, 3)
            self.assertEqual(limited, 5)

    async def test_elevenlabs_character_reservation(self):
        async def tts_handler(req: web.Request):
            await asyncio.sleep(0.1)
            return web.Response(
                body=b"AUDIO",
                status=200,
                headers={"character-cost": "55", "Content-Type": "audio/mpeg"},
            )

        self.mock_upstream.app.router.add_post("/v1/tts", tts_handler)
        upstream_url = await self.mock_upstream.start()
        lane_port = test_utils.unused_port()

        lanes = [
            {
                "client": "bebov",
                "uid": 1009,
                "port": lane_port,
                "limits": {
                    "elevenlabs_chars_month": 100,
                    "concurrency": 10,
                },
            }
        ]
        routes = [
            {
                "prefix": "/elevenlabs",
                "upstream": upstream_url,
                "kind": "elevenlabs",
                "auth": {"header": "xi-api-key", "format": "{key}", "key_env": "LIMIT_TEST_KEY"},
                "allow": "*",
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        async with ClientSession() as session:
            url = f"http://127.0.0.1:{lane_port}/elevenlabs/v1/tts"
            # Request 1 reserves 60 chars (100 - 60 = 40 remaining)
            t1 = asyncio.create_task(session.post(url, json={"text": "A" * 60}))
            await asyncio.sleep(0.02)  # In-flight reservation active

            # Request 2 needs 60 chars -> 60 + 60 = 120 > 100 -> rejected with 429!
            async with session.post(url, json={"text": "B" * 60}) as resp2:
                self.assertEqual(resp2.status, 429)
                data2 = await resp2.json()
                self.assertEqual(data2.get("limit"), "elevenlabs_chars_month")

            resp1 = await t1
            self.assertEqual(resp1.status, 200)
            await resp1.release()

        # Flush and check DB has character-cost recorded
        await self.app.usage_tracker.flush()
        totals = self.app.usage_tracker.month_totals("bebov", "elevenlabs", "2026-10")
        self.assertEqual(totals.chars, 55)
