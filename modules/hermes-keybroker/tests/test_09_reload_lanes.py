"""Test 9: Dynamic reload of lanes (SIGHUP function): adding lane, keeping old lane, corrupt JSON resilience."""

import json
import os
import tempfile
import unittest
from aiohttp import ClientSession, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestReloadLanes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["RELOAD_TEST_KEY"] = "reload-key"

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("RELOAD_TEST_KEY", None)

    async def test_lanes_reload_and_error_handling(self):
        async def ping_handler(req: web.Request):
            return web.json_response({"ping": "pong"})

        self.mock_upstream.app.router.add_get("/ping", ping_handler)
        upstream_url = await self.mock_upstream.start()

        port_lane1 = test_utils.unused_port()
        port_lane2 = test_utils.unused_port()

        initial_lanes = [{"client": "client1", "uid": 1001, "port": port_lane1}]
        routes = [
            {
                "prefix": "/api",
                "upstream": upstream_url,
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "RELOAD_TEST_KEY"},
            }
        ]

        self.app, lanes_file, _ = create_test_broker(self.tmpdir.name, initial_lanes, routes)
        await self.app.start()

        async with ClientSession() as session:
            # Lane 1 responds
            async with session.get(f"http://127.0.0.1:{port_lane1}/api/ping") as resp:
                self.assertEqual(resp.status, 200)

            # Lane 2 should not be open yet
            with self.assertRaises(Exception):
                async with session.get(f"http://127.0.0.1:{port_lane2}/api/ping", timeout=0.5):
                    pass

            # 1. Update lanes.json to include Lane 2
            updated_lanes = [
                {"client": "client1", "uid": 1001, "port": port_lane1},
                {"client": "client2", "uid": 1002, "port": port_lane2},
            ]
            with open(lanes_file, "w", encoding="utf-8") as f:
                json.dump({"lanes": updated_lanes}, f)

            # Trigger reload function (same function invoked on SIGHUP)
            success = await self.app.reload_lanes()
            self.assertTrue(success)

            # Now BOTH Lane 1 and Lane 2 respond!
            async with session.get(f"http://127.0.0.1:{port_lane1}/api/ping") as resp:
                self.assertEqual(resp.status, 200)

            async with session.get(f"http://127.0.0.1:{port_lane2}/api/ping") as resp:
                self.assertEqual(resp.status, 200)

            # 2. Corrupt JSON in lanes.json
            with open(lanes_file, "w", encoding="utf-8") as f:
                f.write("CORRUPTED_NOT_A_VALID_JSON{{{")

            # Reload attempt should fail gracefully
            reload_result = await self.app.reload_lanes()
            self.assertFalse(reload_result)

            # Both old lanes must remain alive and responsive!
            async with session.get(f"http://127.0.0.1:{port_lane1}/api/ping") as resp:
                self.assertEqual(resp.status, 200)
            async with session.get(f"http://127.0.0.1:{port_lane2}/api/ping") as resp:
                self.assertEqual(resp.status, 200)
