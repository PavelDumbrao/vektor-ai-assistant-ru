"""Test 11: Admin endpoints (/healthz, /usage) and admin proxy isolation."""

import os
import tempfile
import time
import unittest
from aiohttp import ClientSession, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestAdminEndpoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["ADMIN_TEST_KEY"] = "admin-test-key"
        # Ensure MISSING_KEY_2 is not set
        os.environ.pop("MISSING_KEY_2", None)

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("ADMIN_TEST_KEY", None)

    async def test_admin_healthz_usage_and_isolation(self):
        async def mock_handler(req: web.Request):
            return web.Response(
                body=b"AUDIO",
                status=200,
                headers={"x-character-count": "150", "Content-Type": "audio/mpeg"},
            )

        self.mock_upstream.app.router.add_post("/v1/tts", mock_handler)
        upstream_url = await self.mock_upstream.start()

        lane_port = test_utils.unused_port()
        admin_port = test_utils.unused_port()

        lanes = [{"client": "bebov", "uid": 1009, "port": lane_port}]
        routes = [
            {
                "prefix": "/elevenlabs",
                "upstream": upstream_url,
                "kind": "elevenlabs",
                "auth": {"header": "xi-api-key", "format": "{key}", "key_env": "ADMIN_TEST_KEY"},
            },
            {
                "prefix": "/offline/v1",
                "upstream": upstream_url,
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "MISSING_KEY_2"},
            },
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes, admin_port=admin_port)
        await self.app.start()

        async with ClientSession() as session:
            # 1. Test /healthz
            async with session.get(f"http://127.0.0.1:{admin_port}/healthz") as resp:
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                self.assertTrue(data["ok"])
                self.assertEqual(len(data["lanes"]), 1)
                self.assertEqual(data["lanes"][0]["client"], "bebov")
                self.assertEqual(data["lanes"][0]["state"], "ok")
                self.assertEqual(data["routes"], 2)
                self.assertEqual(data["routes_without_key"], ["/offline/v1"])

            # 2. Make request on lane port to produce usage
            lane_url = f"http://127.0.0.1:{lane_port}/elevenlabs/v1/tts"
            async with session.post(lane_url, json={"text": "hello"}) as resp:
                self.assertEqual(resp.status, 200)

            # 3. Test /usage?client=bebov&month=YYYY-MM
            current_month = time.strftime("%Y-%m", time.gmtime())
            usage_url = f"http://127.0.0.1:{admin_port}/usage?client=bebov&month={current_month}"
            async with session.get(usage_url) as resp:
                self.assertEqual(resp.status, 200)
                usage_data = await resp.json()
                self.assertTrue(usage_data["ok"])
                self.assertEqual(usage_data["client"], "bebov")
                self.assertEqual(usage_data["month"], current_month)

                routes_data = usage_data["routes"]
                self.assertEqual(len(routes_data), 1)
                eleven_agg = routes_data[0]
                self.assertEqual(eleven_agg["route"], "/elevenlabs")
                self.assertEqual(eleven_agg["requests"], 1)
                self.assertEqual(eleven_agg["errors"], 0)
                self.assertEqual(eleven_agg["chars"], 150)
                self.assertGreater(eleven_agg["resp_bytes"], 0)

            # 4. Admin port does NOT proxy routes (/elevenlabs/... -> 404)
            admin_proxy_attempt = f"http://127.0.0.1:{admin_port}/elevenlabs/v1/tts"
            async with session.post(admin_proxy_attempt, json={"text": "hello"}) as resp:
                self.assertEqual(resp.status, 404)
