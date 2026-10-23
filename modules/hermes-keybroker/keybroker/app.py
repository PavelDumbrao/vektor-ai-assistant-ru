"""Application lifecycle, multi-site orchestration, SIGHUP handling, and admin API."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientSession, web

from keybroker.config import BrokerConfig, Lane, Route, load_lanes, load_routes
from keybroker.proxy import create_lane_handler
from keybroker.usage import UsageTracker

logger = logging.getLogger("keybroker.app")


class BrokerApp:
    """Manages all lane sites, admin site, and usage tracking."""

    def __init__(
        self,
        bind: str,
        admin_port: int,
        lanes_path: str,
        routes_path: str,
        state_dir: str,
    ):
        self.bind = bind
        self.admin_port = admin_port
        self.lanes_path = lanes_path
        self.routes_path = routes_path
        self.state_dir = state_dir

        db_path = os.path.join(state_dir, "usage.db")
        self.usage_tracker = UsageTracker(db_path)

        self.routes: list[Route] = []
        self.lanes: dict[int, Lane] = {}
        self.lane_sites: dict[int, tuple[Lane, web.TCPSite, web.AppRunner]] = {}
        self.admin_site: tuple[web.TCPSite, web.AppRunner] | None = None
        self.session: ClientSession | None = None
        self._purge_task: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self) -> None:
        """Initialize session, load configs, start sites and background tasks."""
        self.usage_tracker.start()
        self.session = ClientSession(auto_decompress=False)

        # Load routes
        self.routes = load_routes(self.routes_path)
        logger.info("Loaded %d route(s):", len(self.routes))
        for r in self.routes:
            upstream_host = urlparse(r.upstream).netloc or r.upstream
            logger.info("  Route %s -> %s (kind=%s)", r.prefix, upstream_host, r.kind)

        # Load lanes
        lanes_list = load_lanes(self.lanes_path)
        logger.info("Loaded %d lane(s):", len(lanes_list))
        for l in lanes_list:
            logger.info("  Lane client=%s uid=%d port=%d", l.client, l.uid, l.port)

        # Start lane sites
        for lane in lanes_list:
            site, runner = await self._start_lane(lane)
            self.lane_sites[lane.port] = (lane, site, runner)
            self.lanes[lane.port] = lane

        # Start admin site
        await self._start_admin()

        # Start background purge task
        self._purge_task = asyncio.create_task(self._purge_loop())

    async def _start_lane(self, lane: Lane) -> tuple[web.TCPSite, web.AppRunner]:
        """Start a TCPSite for a single lane."""
        app = web.Application()
        handler = create_lane_handler(
            lane=lane,
            routes=self.routes,
            usage_tracker=self.usage_tracker,
            session=self.session,  # type: ignore[arg-type]
        )
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host=self.bind, port=lane.port)
        await site.start()
        return site, runner

    async def _stop_lane(self, port: int) -> None:
        """Stop and cleanup TCPSite for a lane."""
        if port in self.lane_sites:
            lane, site, runner = self.lane_sites.pop(port)
            await site.stop()
            await runner.cleanup()

    async def _start_admin(self) -> None:
        """Start admin site with /healthz and /usage."""
        app = web.Application()
        app.router.add_get("/healthz", self._admin_healthz)
        app.router.add_get("/usage", self._admin_usage)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host=self.bind, port=self.admin_port)
        await site.start()
        self.admin_site = (site, runner)
        logger.info("Admin site listening on %s:%d", self.bind, self.admin_port)

    async def _admin_healthz(self, request: web.Request) -> web.Response:
        """Admin healthcheck endpoint."""
        routes_without_key = [r.prefix for r in self.routes if not r.get_api_key()]
        data = {
            "ok": True,
            "lanes": len(self.lane_sites),
            "routes": len(self.routes),
            "routes_without_key": routes_without_key,
        }
        return web.json_response(data)

    async def _admin_usage(self, request: web.Request) -> web.Response:
        """Admin usage aggregates endpoint."""
        client = request.query.get("client")
        month = request.query.get("month")
        if not month:
            month = time.strftime("%Y-%m", time.gmtime())

        aggs = self.usage_tracker.aggregate(client=client, month=month)
        return web.json_response(
            {
                "ok": True,
                "client": client,
                "month": month,
                "routes": aggs,
                "by_route": {r["route"]: r for r in aggs},
            }
        )

    async def reload_lanes(self) -> bool:
        """Reload lanes.json: start new, stop removed, preserve existing."""
        try:
            new_lanes_list = load_lanes(self.lanes_path)
        except Exception as exc:
            logger.error("Failed to reload lanes from %s: %s", self.lanes_path, exc)
            return False

        new_by_port = {l.port: l for l in new_lanes_list}
        current_ports = set(self.lane_sites.keys())
        new_ports = set(new_by_port.keys())

        # Remove stopped lanes
        for port in (current_ports - new_ports):
            logger.info("Stopping lane on port %d", port)
            await self._stop_lane(port)
            self.lanes.pop(port, None)

        # Add new lanes
        for port in (new_ports - current_ports):
            lane = new_by_port[port]
            logger.info("Starting new lane %s on port %d", lane.client, port)
            try:
                site, runner = await self._start_lane(lane)
                self.lane_sites[port] = (lane, site, runner)
                self.lanes[port] = lane
            except Exception as exc:
                logger.error("Failed to start lane on port %d: %s", port, exc)

        # Update existing lane configs (e.g. limits) without interrupting connections
        for port in (current_ports & new_ports):
            new_lane = new_by_port[port]
            self.lanes[port] = new_lane
            site, runner = self.lane_sites[port][1], self.lane_sites[port][2]
            self.lane_sites[port] = (new_lane, site, runner)

        return True

    async def _purge_loop(self) -> None:
        """Daily background purge of records older than 13 months."""
        while not self._stopped:
            try:
                await asyncio.sleep(86400)
                self.usage_tracker.purge_older_than(days=395)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error during usage purge")

    async def stop(self) -> None:
        """Gracefully shut down all sites and release resources."""
        if self._stopped:
            return
        self._stopped = True

        if self._purge_task and not self._purge_task.done():
            self._purge_task.cancel()
            try:
                await self._purge_task
            except asyncio.CancelledError:
                pass

        # Stop admin site
        if self.admin_site:
            site, runner = self.admin_site
            await site.stop()
            await runner.cleanup()
            self.admin_site = None

        # Stop all lane sites
        for port in list(self.lane_sites.keys()):
            await self._stop_lane(port)

        # Close client session
        if self.session and not self.session.closed:
            await self.session.close()

        # Flush and close usage tracker
        await self.usage_tracker.close()


async def main() -> None:
    """CLI entrypoint."""
    config = BrokerConfig.from_env()

    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = BrokerApp(
        bind=config.bind,
        admin_port=config.admin_port,
        lanes_path=config.lanes_path,
        routes_path=config.routes_path,
        state_dir=config.state_dir,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _sig_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _sig_handler)

    # Attach SIGHUP reload handler
    loop.add_signal_handler(
        signal.SIGHUP,
        lambda: asyncio.create_task(app.reload_lanes()),
    )

    await app.start()
    logger.info("ProAI key broker started successfully")

    await stop_event.wait()
    logger.info("Shutting down key broker...")
    await app.stop()
    logger.info("Key broker stopped")
