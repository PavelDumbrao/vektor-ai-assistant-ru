"""Test 3: Server-Sent Events (SSE) streaming verification."""

import asyncio
import os
import tempfile
import unittest
from aiohttp import ClientSession, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestSSEStreaming(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["SSE_TEST_KEY"] = "test-sse-key"

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("SSE_TEST_KEY", None)

    async def test_sse_streaming_not_buffered(self):
        timeline: list[str] = []

        async def sse_handler(request: web.Request):
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
            )
            await resp.prepare(request)

            timeline.append("upstream_sent_chunk1")
            await resp.write(b"data: first chunk\n\n")

            await asyncio.sleep(0.06)
            timeline.append("upstream_sent_chunk2")
            await resp.write(b"data: second chunk\n\n")

            await asyncio.sleep(0.06)
            timeline.append("upstream_sent_chunk3")
            await resp.write(b"data: third chunk\n\n")

            await resp.write_eof()
            return resp

        self.mock_upstream.app.router.add_get("/v1/events", sse_handler)
        upstream_url = await self.mock_upstream.start()
        lane_port = test_utils.unused_port()

        lanes = [{"client": "bebov", "uid": 1009, "port": lane_port}]
        routes = [
            {
                "prefix": "/stream",
                "upstream": upstream_url,
                "kind": "openai",
                "auth": {
                    "header": "Authorization",
                    "format": "Bearer {key}",
                    "key_env": "SSE_TEST_KEY",
                },
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        async with ClientSession() as session:
            url = f"http://127.0.0.1:{lane_port}/stream/v1/events"
            async with session.get(url) as resp:
                self.assertEqual(resp.status, 200)

                # Client reads first chunk
                first_chunk = await resp.content.readuntil(b"\n\n")
                timeline.append("client_received_chunk1")
                self.assertIn(b"first chunk", first_chunk)

                # Crucial check: client MUST receive chunk 1 before upstream has sent chunk 3!
                self.assertNotIn("upstream_sent_chunk3", timeline)

                # Read remaining chunks to finish gracefully
                second_chunk = await resp.content.readuntil(b"\n\n")
                self.assertIn(b"second chunk", second_chunk)

                third_chunk = await resp.content.readuntil(b"\n\n")
                self.assertIn(b"third chunk", third_chunk)

        self.assertIn("client_received_chunk1", timeline)
        self.assertIn("upstream_sent_chunk3", timeline)
        # Verify ordering: client received chunk 1 BEFORE upstream sent chunk 3
        idx_client_chunk1 = timeline.index("client_received_chunk1")
        idx_upstream_chunk3 = timeline.index("upstream_sent_chunk3")
        self.assertLess(idx_client_chunk1, idx_upstream_chunk3)
