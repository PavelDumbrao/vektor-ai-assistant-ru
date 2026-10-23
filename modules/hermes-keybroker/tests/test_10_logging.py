"""Test 10: Logging guarantees (no secret leakage, access line formatting)."""

import logging
import os
import tempfile
import unittest
from aiohttp import ClientSession, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class ListLogHandler(logging.Handler):
    """Memory handler collecting formatted log records."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.messages.append(self.format(record))


class TestLogging(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        self.super_secret = "SUPER_SECRET_TOKEN_XYZ_999"
        os.environ["SECRET_KEY_ENV"] = self.super_secret

        self.log_handler = ListLogHandler()
        root_logger = logging.getLogger()
        root_logger.addHandler(self.log_handler)
        root_logger.setLevel(logging.INFO)

    async def asyncTearDown(self):
        root_logger = logging.getLogger()
        root_logger.removeHandler(self.log_handler)
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("SECRET_KEY_ENV", None)

    async def test_logs_contain_no_secrets_and_have_access_line(self):
        async def mock_handler(req: web.Request):
            return web.json_response({"result": "ok"})

        self.mock_upstream.app.router.add_get("/data", mock_handler)
        upstream_url = await self.mock_upstream.start()
        lane_port = test_utils.unused_port()

        lanes = [{"client": "bebov", "uid": 1009, "port": lane_port}]
        routes = [
            {
                "prefix": "/service",
                "upstream": upstream_url,
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "Bearer {key}", "key_env": "SECRET_KEY_ENV"},
                "allow": [
                    {"methods": ["GET"], "path": "^/data$"}
                ],
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        sensitive_hdr_val = "SENSITIVE_SECRET_HEADER_VALUE_888"

        async with ClientSession() as session:
            # 1. Successful request (200)
            url = f"http://127.0.0.1:{lane_port}/service/data"
            async with session.get(url, headers={"X-Secret-Custom": sensitive_hdr_val}) as resp:
                self.assertEqual(resp.status, 200)

            # 2. Forbidden path request (403)
            url_403 = f"http://127.0.0.1:{lane_port}/service/admin_forbidden"
            async with session.get(url_403, headers={"X-Secret-Custom": sensitive_hdr_val}) as resp:
                self.assertEqual(resp.status, 403)

            # 3. Bad path request (400)
            url_400 = f"http://127.0.0.1:{lane_port}/service//data"
            async with session.get(url_400, headers={"X-Secret-Custom": sensitive_hdr_val}) as resp:
                self.assertEqual(resp.status, 400)

        # 1. Verify that the secret key and header values are NEVER present in any log message
        for msg in self.log_handler.messages:
            self.assertNotIn(
                self.super_secret,
                msg,
                f"Secret key found in log message: {msg}",
            )
            self.assertNotIn(
                sensitive_hdr_val,
                msg,
                f"Sensitive header value found in log message: {msg}",
            )

        # 2. Verify that there are request log lines containing client=, route=, status= for 200, 403, 400
        found_200 = any("status=200" in msg and "client=bebov" in msg for msg in self.log_handler.messages)
        found_403 = any("status=403" in msg and "client=bebov" in msg for msg in self.log_handler.messages)
        found_400 = any("status=400" in msg and "client=bebov" in msg for msg in self.log_handler.messages)

        self.assertTrue(found_200, "Missing 200 access log line")
        self.assertTrue(found_403, "Missing 403 access log line")
        self.assertTrue(found_400, "Missing 400 access log line")
