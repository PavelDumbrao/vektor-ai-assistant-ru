"""Test 12: Route allowlist (methods and paths regex, order before limits, open routes without allow)."""

import os
import sqlite3
import tempfile
import unittest
from aiohttp import ClientSession, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestRouteAllowlist(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["ALLOW_TEST_KEY"] = "allow-test-key"

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("ALLOW_TEST_KEY", None)

    async def test_allowlist_filtering_and_order_before_limits(self):
        upstream_calls = 0

        async def chat_handler(req: web.Request):
            nonlocal upstream_calls
            upstream_calls += 1
            return web.json_response({"result": "chat_ok"})

        async def open_handler(req: web.Request):
            nonlocal upstream_calls
            upstream_calls += 1
            return web.json_response({"result": "open_ok"})

        self.mock_upstream.app.router.add_post("/v1/chat/completions", chat_handler)
        self.mock_upstream.app.router.add_delete("/v1/chat/completions", chat_handler)
        self.mock_upstream.app.router.add_get("/any/path", open_handler)
        upstream_url = await self.mock_upstream.start()
        lane_port = test_utils.unused_port()

        lanes = [
            {
                "client": "bebov",
                "uid": 1009,
                "port": lane_port,
                "limits": {"rpm": 10},
            }
        ]
        routes = [
            {
                "prefix": "/openai",
                "upstream": upstream_url,
                "kind": "openai",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "ALLOW_TEST_KEY"},
                "allow": [
                    {"methods": ["POST"], "path": "^/v1/chat/completions$"},
                    {"methods": ["GET"], "path": "^/v1/models$"},
                ],
            },
            {
                "prefix": "/open",
                "upstream": upstream_url,
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "ALLOW_TEST_KEY"},
                # No allow field: route is open
            },
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        async with ClientSession() as session:
            # 1. Allowed method + path -> succeeds to upstream
            url_allowed = f"http://127.0.0.1:{lane_port}/openai/v1/chat/completions"
            async with session.post(url_allowed, json={"prompt": "hi"}) as resp:
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                self.assertEqual(data.get("result"), "chat_ok")
            self.assertEqual(upstream_calls, 1)

            # At this point, rpm limit (1) is exhausted.
            # 2. Forbidden path -> 403 (checked before limits, upstream NOT called)
            url_forbidden_path = f"http://127.0.0.1:{lane_port}/openai/v1/voices/admin"
            async with session.post(url_forbidden_path, json={}) as resp:
                self.assertEqual(resp.status, 403)
                data = await resp.json()
                self.assertEqual(data.get("error"), "path_not_allowed")
            # Upstream must NOT be called
            self.assertEqual(upstream_calls, 1)

            # 3. Forbidden method on allowed path (DELETE on /v1/chat/completions) -> 403
            async with session.delete(url_allowed) as resp:
                self.assertEqual(resp.status, 403)
                data = await resp.json()
                self.assertEqual(data.get("error"), "path_not_allowed")
            self.assertEqual(upstream_calls, 1)

            # 4. Route with allow="*" -> passes through (open route)
            async with session.get(f"http://127.0.0.1:{lane_port}/open/any/path") as resp:
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                self.assertEqual(data.get("result"), "open_ok")
            self.assertEqual(upstream_calls, 2)

        # 5. Flush and verify accounting for 403
        await self.app.usage_tracker.flush()
        db_path = os.path.join(self.tmpdir.name, "usage.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT status, error FROM requests WHERE status = 403").fetchall()
        conn.close()

        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r["status"], 403)
            self.assertEqual(r["error"], "path_not_allowed")
