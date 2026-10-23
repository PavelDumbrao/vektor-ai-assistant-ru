"""Test 15: Security checks for encoded paths, query preservation, and header cleansing."""

import asyncio
import os
import tempfile
import unittest
from aiohttp import ClientSession, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestSecurity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["SEC_TEST_KEY"] = "sec-key-real"

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("SEC_TEST_KEY", None)

    async def _send_raw_get(self, port: int, path: str) -> tuple[int, str]:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        req = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode("utf-8")
        writer.write(req)
        await writer.drain()
        data = await reader.read()
        writer.close()
        await writer.wait_closed()

        header_part = data.split(b"\r\n\r\n")[0].decode("utf-8")
        status_line = header_part.split("\r\n")[0]
        status = int(status_line.split(" ")[1])
        return status, data.decode("utf-8", errors="replace")

    async def test_encoded_characters_in_path_rejected(self):
        upstream_called = False

        async def handler(req: web.Request):
            nonlocal upstream_called
            upstream_called = True
            return web.json_response({"ok": True})

        self.mock_upstream.app.router.add_get("/v1/models", handler)
        upstream_url = await self.mock_upstream.start()
        lane_port = test_utils.unused_port()

        lanes = [{"client": "bebov", "uid": 1009, "port": lane_port}]
        routes = [
            {
                "prefix": "/lingsuan/v1",
                "upstream": upstream_url + "/v1",
                "kind": "openai",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "SEC_TEST_KEY"},
                "allow": [{"path": "^/models.*$"}]
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        # Paths with % must be rejected with 400 bad_path
        bad_encoded_paths = [
            "/lingsuan/v1/models/%252e%252e",
            "/lingsuan/v1/models/%2e",
            "/lingsuan/v1/models/%09",
        ]

        for p in bad_encoded_paths:
            status, _ = await self._send_raw_get(lane_port, p)
            self.assertEqual(status, 400, f"Expected 400 for '{p}', got {status}")
            self.assertFalse(upstream_called, f"Upstream called for '{p}'")

    async def test_query_preservation_and_headers_cleansing(self):
        upstream_info = {}

        async def query_handler(req: web.Request):
            upstream_info["raw_path"] = req.raw_path
            upstream_info["query_string"] = req.query_string
            upstream_info["headers"] = dict(req.headers)
            # count Accept-Encoding occurrences in raw headers
            upstream_info["ae_count"] = sum(1 for k, _ in req.raw_headers if k.lower() == b"accept-encoding")
            return web.json_response({"ok": True})

        self.mock_upstream.app.router.add_get("/v1/models", query_handler)
        upstream_url = await self.mock_upstream.start()
        lane_port = test_utils.unused_port()

        lanes = [{"client": "bebov", "uid": 1009, "port": lane_port}]
        routes = [
            {
                "prefix": "/custom",
                "upstream": upstream_url + "/v1",
                "kind": "generic",
                "auth": {"header": "x-custom-key", "format": "{key}", "key_env": "SEC_TEST_KEY"},
                "allow": [{"path": "^/models$"}]
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        client_headers = {
            "Authorization": "Bearer client-junk",
            "XI-API-KEY": "client-xi-junk",
            "Cookie": "session=secret123",
            "X-Forwarded-For": "10.0.0.1",
            "X-Forwarded-Proto": "https",
            "X-Real-IP": "10.0.0.2",
            "Via": "1.1 proxy",
            "X-Custom-Key": "client-custom-override-attempt",
            "accept-encoding": "gzip, deflate, br",
        }

        # Query containing // and %23
        url = f"http://127.0.0.1:{lane_port}/custom/models?redirect=//example.com&tag=%231"
        async with ClientSession() as session:
            async with session.get(url, headers=client_headers) as resp:
                self.assertEqual(resp.status, 200)

        # 1. Query string preserved as-is without re-encoding
        self.assertIn("redirect=//example.com", upstream_info["raw_path"])
        self.assertIn("%231", upstream_info["raw_path"])

        # 2. Mixed-case and forbidden headers stripped
        received_h = upstream_info["headers"]
        self.assertNotIn("Cookie", received_h)
        self.assertNotIn("cookie", received_h)
        self.assertNotIn("X-Forwarded-For", received_h)
        self.assertNotIn("x-forwarded-for", received_h)
        self.assertNotIn("X-Forwarded-Proto", received_h)
        self.assertNotIn("X-Real-IP", received_h)
        self.assertNotIn("Via", received_h)
        self.assertNotIn("XI-API-KEY", received_h)
        self.assertNotIn("xi-api-key", received_h)
        self.assertNotIn("Authorization", received_h)

        # 3. Custom key injected by broker, client's X-Custom-Key does not pass
        self.assertEqual(received_h.get("x-custom-key"), "sec-key-real")

        # 4. Exactly one Accept-Encoding: identity
        self.assertEqual(received_h.get("Accept-Encoding"), "identity")
        self.assertEqual(upstream_info["ae_count"], 1)
