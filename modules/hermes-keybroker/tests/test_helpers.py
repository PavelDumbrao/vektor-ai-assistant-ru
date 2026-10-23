"""Test helpers and mock utilities for keybroker tests."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from aiohttp import web, test_utils
from keybroker.app import BrokerApp


class MockUpstream:
    """Mock upstream server tracking received requests."""

    def __init__(self):
        self.app = web.Application()
        self.server: test_utils.TestServer | None = None
        self.received_requests: list[dict[str, Any]] = []

    async def start(self) -> str:
        self.server = test_utils.TestServer(self.app)
        await self.server.start_server()
        return str(self.server.make_url(""))

    async def stop(self) -> None:
        if self.server:
            await self.server.close()


def create_test_broker(
    tmpdir: str,
    lanes: list[dict[str, Any]],
    routes: list[dict[str, Any]],
    admin_port: int | None = None,
) -> tuple[BrokerApp, str, str]:
    """Write test configs to tmpdir and create a BrokerApp instance."""
    if admin_port is None:
        admin_port = test_utils.unused_port()

    lanes_path = os.path.join(tmpdir, "lanes.json")
    routes_path = os.path.join(tmpdir, "routes.json")

    with open(lanes_path, "w", encoding="utf-8") as f:
        json.dump({"lanes": lanes}, f)

    with open(routes_path, "w", encoding="utf-8") as f:
        json.dump({"routes": routes}, f)

    app = BrokerApp(
        bind="127.0.0.1",
        admin_port=admin_port,
        lanes_path=lanes_path,
        routes_path=routes_path,
        state_dir=tmpdir,
    )
    return app, lanes_path, routes_path
