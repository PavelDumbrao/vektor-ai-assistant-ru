"""Test 17: Disconnect accounting (client disconnect during stream produces usage record with chars and client_disconnected error)."""

import asyncio
import os
import sqlite3
import tempfile
import unittest
from aiohttp import test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestDisconnectAccounting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["DISC_TEST_KEY"] = "disc-key"

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("DISC_TEST_KEY", None)

    async def test_client_disconnect_records_chars_and_status(self):
        async def slow_stream_handler(req: web.Request):
            resp = web.StreamResponse(
                status=200,
                headers={"character-cost": "350", "Content-Type": "audio/mpeg"},
            )
            await resp.prepare(req)
            # Write first chunk
            await resp.write(b"AUDIO_CHUNK_1")
            # Wait a bit before writing chunk 2
            await asyncio.sleep(0.5)
            try:
                await resp.write(b"AUDIO_CHUNK_2")
                await resp.write_eof()
            except Exception:
                pass
            return resp

        self.mock_upstream.app.router.add_get("/v1/stream", slow_stream_handler)
        upstream_url = await self.mock_upstream.start()
        lane_port = test_utils.unused_port()

        lanes = [{"client": "bebov", "uid": 1009, "port": lane_port}]
        routes = [
            {
                "prefix": "/tts",
                "upstream": upstream_url,
                "kind": "elevenlabs",
                "auth": {"header": "xi-api-key", "format": "{key}", "key_env": "DISC_TEST_KEY"},
                "allow": "*",
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        # Connect with raw socket and abruptly close after receiving initial data
        reader, writer = await asyncio.open_connection("127.0.0.1", lane_port)
        req = f"GET /tts/v1/stream HTTP/1.1\r\nHost: 127.0.0.1:{lane_port}\r\n\r\n".encode("utf-8")
        writer.write(req)
        await writer.drain()

        # Read until headers and first chunk
        data = await reader.read(256)
        self.assertIn(b"200 OK", data)
        self.assertIn(b"AUDIO_CHUNK_1", data)

        # Abruptly close socket from client side while stream is active
        writer.close()
        await writer.wait_closed()

        # Wait for broker to finish error handling and flush queue
        await asyncio.sleep(0.7)
        await self.app.usage_tracker.flush()

        db_path = os.path.join(self.tmpdir.name, "usage.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM requests").fetchall()
        conn.close()

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["chars"], 350)
        self.assertEqual(row["error"], "client_disconnected")
        self.assertGreater(row["resp_bytes"], 0)
