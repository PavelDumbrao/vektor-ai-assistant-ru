"""Test 20: Lifecycle, reload, healthz, and usage edge cases.

Covers:
- SIGHUP changed rpm takes effect without restarting lane.
- SIGHUP client change on port re-routes accounting to new client name.
- Startup with an occupied port does not crash broker; other lanes work, /healthz reports failed state.
- /usage?month=2026-13 returns 400 bad_month.
- flush() after close() does not hang (test timeout 5s).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import unittest

from aiohttp import ClientSession, test_utils, web

from keybroker.usage import RequestRecord, UsageTracker
from tests.test_helpers import MockUpstream, create_test_broker


class TestLifecycleAndHealthz(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["LIFECYCLE_TEST_KEY"] = "sk-lifecycle-test-key"

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("LIFECYCLE_TEST_KEY", None)

    async def test_sighup_rpm_update_and_client_rename(self):
        """SIGHUP dynamically updates rpm limits without restart and restarts lane when client changes."""
        async def ping(request: web.Request):
            return web.json_response({"pong": True})

        self.mock_upstream.app.router.add_get("/ping", ping)
        upstream_url = await self.mock_upstream.start()

        port = test_utils.unused_port()
        initial_lanes = [{"client": "alice", "uid": 1001, "port": port, "limits": {"rpm": 1}}]
        routes = [
            {
                "prefix": "/api",
                "upstream": upstream_url,
                "kind": "generic",
                "allow": "*",
                "auth": {
                    "header": "Authorization",
                    "format": "Bearer {key}",
                    "key_env": "LIFECYCLE_TEST_KEY",
                },
            }
        ]

        self.app, lanes_file, _ = create_test_broker(self.tmpdir.name, initial_lanes, routes)
        await self.app.start()

        async with ClientSession() as session:
            url = f"http://127.0.0.1:{port}/api/ping"

            # 1. First request succeeds (200)
            async with session.get(url) as resp:
                self.assertEqual(resp.status, 200)

            # 2. Second request hits rpm: 1 limit -> 429 lane_limit
            async with session.get(url) as resp:
                self.assertEqual(resp.status, 429)
                data = await resp.json()
                self.assertEqual(data.get("error"), "lane_limit")

            # 3. Update rpm to 10 in lanes.json and reload
            with open(lanes_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"lanes": [{"client": "alice", "uid": 1001, "port": port, "limits": {"rpm": 10}}]},
                    f,
                )
            reloaded = await self.app.reload_lanes()
            self.assertTrue(reloaded)

            # 4. Third request immediately succeeds with new limit
            async with session.get(url) as resp:
                self.assertEqual(resp.status, 200)

            # 5. Change client from 'alice' to 'bob' on the same port
            with open(lanes_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"lanes": [{"client": "bob", "uid": 1002, "port": port, "limits": {"rpm": 10}}]},
                    f,
                )
            reloaded = await self.app.reload_lanes()
            self.assertTrue(reloaded)

            # 6. Make request under new client
            async with session.get(url) as resp:
                self.assertEqual(resp.status, 200)

            # Verify usage accounting recorded request under 'bob'
            await self.app.usage_tracker.flush()
            bob_aggs = self.app.usage_tracker.aggregate(client="bob", month="2026-10")
            self.assertTrue(any(item["route"] == "/api" and item["requests"] >= 1 for item in bob_aggs))

    async def test_startup_with_occupied_port(self):
        """Startup with one occupied port continues running other lanes and marks failed in /healthz."""
        async def ping(request: web.Request):
            return web.json_response({"pong": True})

        self.mock_upstream.app.router.add_get("/ping", ping)
        upstream_url = await self.mock_upstream.start()

        # Occupy a port with a raw socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        occupied_port = sock.getsockname()[1]
        healthy_port = test_utils.unused_port()

        lanes = [
            {"client": "fail_client", "uid": 1001, "port": occupied_port},
            {"client": "healthy_client", "uid": 1002, "port": healthy_port},
        ]
        routes = [
            {
                "prefix": "/api",
                "upstream": upstream_url,
                "kind": "generic",
                "allow": "*",
                "auth": {
                    "header": "Authorization",
                    "format": "Bearer {key}",
                    "key_env": "LIFECYCLE_TEST_KEY",
                },
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        # Should not raise exception
        await self.app.start()

        try:
            async with ClientSession() as session:
                # Healthy lane responds
                async with session.get(f"http://127.0.0.1:{healthy_port}/api/ping") as resp:
                    self.assertEqual(resp.status, 200)

                # Admin healthz reports states
                async with session.get(f"http://127.0.0.1:{self.app.admin_port}/healthz") as resp:
                    self.assertEqual(resp.status, 200)
                    data = await resp.json()
                    self.assertTrue(data.get("ok"))
                    states = {l["port"]: l["state"] for l in data.get("lanes", [])}
                    self.assertEqual(states.get(occupied_port), "failed")
                    self.assertEqual(states.get(healthy_port), "ok")
        finally:
            sock.close()

    async def test_usage_invalid_month_returns_400(self):
        """Querying /usage?month=2026-13 returns 400 bad_month."""
        port = test_utils.unused_port()
        lanes = [{"client": "client1", "uid": 1001, "port": port}]
        routes = [
            {
                "prefix": "/api",
                "upstream": "http://127.0.0.1:9",
                "kind": "generic",
                "allow": "*",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "LIFECYCLE_TEST_KEY"},
            }
        ]
        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        async with ClientSession() as session:
            admin_url = f"http://127.0.0.1:{self.app.admin_port}/usage?month=2026-13"
            async with session.get(admin_url) as resp:
                self.assertEqual(resp.status, 400)
                data = await resp.json()
                self.assertEqual(data.get("error"), "bad_month")

    async def test_flush_after_close_does_not_hang(self):
        """UsageTracker flush() called after close() must not hang (5s timeout)."""
        db_file = os.path.join(self.tmpdir.name, "usage_test.db")
        tracker = UsageTracker(db_file)
        tracker.start()

        tracker.record(
            RequestRecord(
                ts=100.0,
                client="test",
                port=8080,
                route="/test",
                kind="generic",
                method="GET",
                path="/test/path",
                status=200,
                req_bytes=10,
                resp_bytes=20,
                duration_ms=5,
            )
        )
        await tracker.close()

        # Must finish well within 5 seconds without hanging
        await asyncio.wait_for(tracker.flush(), timeout=5.0)
