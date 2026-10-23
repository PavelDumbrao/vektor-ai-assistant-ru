"""Test 13: Path normalization and rejection of bad paths (.., ., //, \\, %00)."""

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from aiohttp import test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestBadPath(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["BAD_PATH_KEY"] = "test-key"

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("BAD_PATH_KEY", None)

    async def _send_raw_request(self, port: int, path: str) -> tuple[int, dict]:
        """Send a raw HTTP request without client-side normalization."""
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        req = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode("utf-8")
        writer.write(req)
        await writer.drain()
        data = await reader.read()
        writer.close()
        await writer.wait_closed()

        # Parse HTTP status and body
        header_part, _, body_part = data.partition(b"\r\n\r\n")
        status_line = header_part.split(b"\r\n")[0].decode("utf-8")
        status = int(status_line.split(" ")[1])
        body_json = {}
        try:
            body_json = json.loads(body_part.decode("utf-8"))
        except Exception:
            pass
        return status, body_json

    async def test_bad_paths_rejected_with_400(self):
        upstream_called = False

        async def handler(req: web.Request):
            nonlocal upstream_called
            upstream_called = True
            return web.json_response({"ok": True})

        self.mock_upstream.app.router.add_get("/v1/x", handler)
        self.mock_upstream.app.router.add_get("/user", handler)
        upstream_url = await self.mock_upstream.start()
        lane_port = test_utils.unused_port()

        lanes = [{"client": "bebov", "uid": 1009, "port": lane_port}]
        routes = [
            {
                "prefix": "/elevenlabs",
                "upstream": upstream_url,
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "BAD_PATH_KEY"},
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        bad_paths = [
            "/elevenlabs/v1/../user",
            "/elevenlabs//v1/x",
            "/elevenlabs/./v1/x",
            "/elevenlabs/v1\\x",
            "/elevenlabs/%00v1",
        ]

        for p in bad_paths:
            status, body = await self._send_raw_request(lane_port, p)
            self.assertEqual(
                status,
                400,
                f"Path '{p}' was expected to return 400, got {status}",
            )
            self.assertEqual(body.get("error"), "bad_path")
            self.assertFalse(
                upstream_called,
                f"Upstream was unexpectedly called for bad path '{p}'",
            )

        # Verify DB accounting for bad paths
        await self.app.usage_tracker.flush()
        db_path = os.path.join(self.tmpdir.name, "usage.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT status, error FROM requests WHERE status = 400").fetchall()
        conn.close()

        self.assertEqual(len(rows), len(bad_paths))
        for r in rows:
            self.assertEqual(r["status"], 400)
            self.assertEqual(r["error"], "bad_path")
