"""Test 8: Multi-tenant lanes on separate ports with correct client/port in accounting."""

import os
import sqlite3
import tempfile
import unittest
from aiohttp import ClientSession, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestMultitenantLanes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["MULTI_TEST_KEY"] = "multi-test-key"

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("MULTI_TEST_KEY", None)

    async def test_multiple_lanes_separate_accounting(self):
        async def mock_handler(req: web.Request):
            return web.json_response({"ok": True})

        self.mock_upstream.app.router.add_get("/v1/models", mock_handler)
        upstream_url = await self.mock_upstream.start()

        port_bebov = test_utils.unused_port()
        port_alice = test_utils.unused_port()

        lanes = [
            {"client": "bebov", "uid": 1009, "port": port_bebov},
            {"client": "alice", "uid": 1010, "port": port_alice},
        ]
        routes = [
            {
                "prefix": "/models",
                "upstream": upstream_url,
                "kind": "openai",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "MULTI_TEST_KEY"},
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        async with ClientSession() as session:
            # Request to bebov's lane
            url_bebov = f"http://127.0.0.1:{port_bebov}/models/v1/models"
            async with session.get(url_bebov) as resp:
                self.assertEqual(resp.status, 200)

            # Request to alice's lane
            url_alice = f"http://127.0.0.1:{port_alice}/models/v1/models"
            async with session.get(url_alice) as resp:
                self.assertEqual(resp.status, 200)

        await self.app.usage_tracker.flush()

        db_path = os.path.join(self.tmpdir.name, "usage.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT client, port, route, status FROM requests ORDER BY id ASC").fetchall()
        conn.close()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["client"], "bebov")
        self.assertEqual(rows[0]["port"], port_bebov)
        self.assertEqual(rows[0]["status"], 200)

        self.assertEqual(rows[1]["client"], "alice")
        self.assertEqual(rows[1]["port"], port_alice)
        self.assertEqual(rows[1]["status"], 200)
