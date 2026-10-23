"""Test 1: Auth substitution and header filtering."""

import os
import tempfile
import unittest
from aiohttp import ClientSession, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestAuthSubstitution(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        self.test_key = "test-key-secret-987"
        os.environ["TEST_UPSTREAM_KEY"] = self.test_key

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("TEST_UPSTREAM_KEY", None)

    async def test_auth_headers_and_host(self):
        recorded_headers = {}

        async def handler(request: web.Request):
            nonlocal recorded_headers
            recorded_headers = dict(request.headers)
            return web.json_response({"ok": True})

        self.mock_upstream.app.router.add_get("/v1/chat", handler)
        upstream_url = await self.mock_upstream.start()
        lane_port = test_utils.unused_port()

        lanes = [{"client": "bebov", "uid": 1009, "port": lane_port}]
        routes = [
            {
                "prefix": "/openai/v1",
                "upstream": upstream_url + "/v1",
                "kind": "openai",
                "auth": {
                    "header": "Authorization",
                    "format": "Bearer {key}",
                    "key_env": "TEST_UPSTREAM_KEY",
                },
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        client_headers = {
            "Authorization": "Bearer junk-incoming-token",
            "xi-api-key": "junk-xi-key",
            "x-api-key": "junk-x-key",
            "api-key": "junk-api-key",
            "X-Custom-Client-Header": "custom-val",
        }

        async with ClientSession() as session:
            url = f"http://127.0.0.1:{lane_port}/openai/v1/chat"
            async with session.get(url, headers=client_headers) as resp:
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                self.assertTrue(data["ok"])

        # Upstream checks
        # 1. Incoming junk auth headers must NOT reach upstream
        self.assertNotIn("xi-api-key", recorded_headers)
        self.assertNotIn("x-api-key", recorded_headers)
        self.assertNotIn("api-key", recorded_headers)

        # 2. Authorization header must be replaced with route secret
        self.assertEqual(recorded_headers.get("Authorization"), f"Bearer {self.test_key}")

        # 3. Host must be the upstream host
        expected_host = f"127.0.0.1:{self.mock_upstream.server.port}"
        self.assertEqual(recorded_headers.get("Host"), expected_host)

        # 4. Custom non-auth header should pass through
        self.assertEqual(recorded_headers.get("X-Custom-Client-Header"), "custom-val")
