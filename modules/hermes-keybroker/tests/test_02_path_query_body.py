"""Test 2: Path, query and body (JSON and multipart) byte-for-byte proxying."""

import os
import tempfile
import unittest
from aiohttp import ClientSession, FormData, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestPathQueryBody(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["GENERIC_KEY"] = "test-generic-token"

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("GENERIC_KEY", None)

    async def test_json_and_multipart_proxying(self):
        recorded_requests = []

        async def echo_handler(request: web.Request):
            body_bytes = await request.read()
            recorded_requests.append({
                "path": request.path,
                "query": request.query_string,
                "content_type": request.content_type,
                "body": body_bytes,
            })
            # Return custom status and Content-Type
            return web.Response(
                body=b'{"received": true}',
                status=201,
                content_type="application/vnd.custom+json",
            )

        self.mock_upstream.app.router.add_post("/api/v1/echo", echo_handler)
        upstream_url = await self.mock_upstream.start()
        lane_port = test_utils.unused_port()

        lanes = [{"client": "bebov", "uid": 1009, "port": lane_port}]
        routes = [
            {
                "prefix": "/service/api/v1",
                "upstream": upstream_url + "/api/v1",
                "kind": "generic",
                "auth": {
                    "header": "Authorization",
                    "format": "Bearer {key}",
                    "key_env": "GENERIC_KEY",
                },
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        async with ClientSession() as session:
            # 1. Test JSON request with query string
            json_payload = b'{"hello": "world", "num": 42}'
            url = f"http://127.0.0.1:{lane_port}/service/api/v1/echo?foo=bar&tag=1"
            async with session.post(
                url,
                data=json_payload,
                headers={"Content-Type": "application/json"},
            ) as resp:
                self.assertEqual(resp.status, 201)
                self.assertEqual(resp.content_type, "application/vnd.custom+json")
                resp_text = await resp.text()
                self.assertEqual(resp_text, '{"received": true}')

            # 2. Test Multipart request
            fd = FormData()
            fd.add_field("field1", "value1")
            fd.add_field("file", b"\x00\x01\x02\x03BINARY_DATA\xff", filename="data.bin")

            multipart_url = f"http://127.0.0.1:{lane_port}/service/api/v1/echo?action=upload"
            async with session.post(multipart_url, data=fd) as resp:
                self.assertEqual(resp.status, 201)
                self.assertEqual(resp.content_type, "application/vnd.custom+json")

        self.assertEqual(len(recorded_requests), 2)

        # Check JSON request arrived byte-for-byte
        req1 = recorded_requests[0]
        self.assertEqual(req1["path"], "/api/v1/echo")
        self.assertEqual(req1["query"], "foo=bar&tag=1")
        self.assertEqual(req1["body"], json_payload)

        # Check Multipart request arrived with exact path and query
        req2 = recorded_requests[1]
        self.assertEqual(req2["path"], "/api/v1/echo")
        self.assertEqual(req2["query"], "action=upload")
        self.assertIn(b"BINARY_DATA", req2["body"])
        self.assertIn("multipart/form-data", req2["content_type"])
