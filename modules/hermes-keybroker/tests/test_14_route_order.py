"""Test 14: Route ordering independent of configuration file order (longest prefix matches first)."""

import os
import tempfile
import unittest
from aiohttp import ClientSession, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestRouteOrder(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.upstream_a = MockUpstream()
        self.upstream_b = MockUpstream()
        os.environ["ORDER_TEST_KEY"] = "order-test-key"

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.upstream_a.stop()
        await self.upstream_b.stop()
        self.tmpdir.cleanup()
        os.environ.pop("ORDER_TEST_KEY", None)

    async def test_longest_prefix_matches_first(self):
        async def handler_a(req: web.Request):
            return web.json_response({"target": "upstream_A", "path": req.path})

        async def handler_b(req: web.Request):
            return web.json_response({"target": "upstream_B", "path": req.path})

        self.upstream_a.app.router.add_get("/x", handler_a)
        self.upstream_b.app.router.add_get("/x", handler_b)

        url_a = await self.upstream_a.start()
        url_b = await self.upstream_b.start()
        lane_port = test_utils.unused_port()

        lanes = [{"client": "bebov", "uid": 1009, "port": lane_port}]

        # "Wrong" order in config: shorter prefix '/a' is listed BEFORE longer prefix '/a/b'
        routes = [
            {
                "prefix": "/a",
                "upstream": url_a,
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "ORDER_TEST_KEY"},
            },
            {
                "prefix": "/a/b",
                "upstream": url_b,
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "ORDER_TEST_KEY"},
            },
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        async with ClientSession() as session:
            # Request to /a/b/x MUST match /a/b and route to upstream_B
            url_target_b = f"http://127.0.0.1:{lane_port}/a/b/x"
            async with session.get(url_target_b) as resp:
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                self.assertEqual(data.get("target"), "upstream_B")
                self.assertEqual(data.get("path"), "/x")

            # Request to /a/x MUST match /a and route to upstream_A
            url_target_a = f"http://127.0.0.1:{lane_port}/a/x"
            async with session.get(url_target_a) as resp:
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                self.assertEqual(data.get("target"), "upstream_A")
                self.assertEqual(data.get("path"), "/x")
