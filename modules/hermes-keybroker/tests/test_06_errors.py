"""Test 6: Error handling (404 unknown route, 503 missing key, 502 unreachable upstream, 501 websocket)."""

import os
import tempfile
import unittest
from aiohttp import ClientSession, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestErrorResponses(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["AVAILABLE_KEY"] = "exists-123"
        # Make sure MISSING_KEY is NOT in environment
        os.environ.pop("MISSING_KEY", None)

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("AVAILABLE_KEY", None)

    async def test_error_status_codes(self):
        upstream_url = await self.mock_upstream.start()
        closed_port = test_utils.unused_port()
        lane_port = test_utils.unused_port()

        lanes = [{"client": "bebov", "uid": 1009, "port": lane_port}]
        routes = [
            {
                "prefix": "/valid/route",
                "upstream": upstream_url,
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "AVAILABLE_KEY"},
            },
            {
                "prefix": "/no-key/route",
                "upstream": upstream_url,
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "MISSING_KEY"},
            },
            {
                "prefix": "/dead/upstream",
                "upstream": f"http://127.0.0.1:{closed_port}",
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "AVAILABLE_KEY"},
                "timeout_s": 2.0,
            },
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        async with ClientSession() as session:
            # 1. Unknown prefix -> 404 {"error": "unknown_route"}
            async with session.get(f"http://127.0.0.1:{lane_port}/completely/unknown") as resp:
                self.assertEqual(resp.status, 404)
                data = await resp.json()
                self.assertEqual(data.get("error"), "unknown_route")

            # 2. Route without key in env -> 503 {"error": "route_unavailable"}
            async with session.get(f"http://127.0.0.1:{lane_port}/no-key/route/test") as resp:
                self.assertEqual(resp.status, 503)
                data = await resp.json()
                self.assertEqual(data.get("error"), "route_unavailable")

            # 3. Upstream unreachable (closed port) -> 502 {"error": "upstream_unreachable"}
            async with session.get(f"http://127.0.0.1:{lane_port}/dead/upstream/test") as resp:
                self.assertEqual(resp.status, 502)
                data = await resp.json()
                self.assertEqual(data.get("error"), "upstream_unreachable")

            # 4. WebSocket (Upgrade) -> 501 {"error": "not_implemented"}
            ws_headers = {"Upgrade": "websocket", "Connection": "Upgrade"}
            async with session.get(f"http://127.0.0.1:{lane_port}/valid/route/ws", headers=ws_headers) as resp:
                self.assertEqual(resp.status, 501)
                data = await resp.json()
                self.assertEqual(data.get("error"), "not_implemented")
