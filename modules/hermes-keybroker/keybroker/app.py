"""Application lifecycle, multi-site orchestration, SIGHUP handling, and admin API."""

from __future__ import annotations

import asyncio
from collections import defaultdict
import logging
import os
import signal
import sys
import time
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientSession, web

from keybroker.config import BrokerConfig, Lane, Route, load_lanes, load_routes
from keybroker.proxy import create_lane_handler
from keybroker.usage import UsageTracker, _get_utc_month_range

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
        self.lane_states: dict[int, str] = {}
        self.in_flight: dict[int, int] = defaultdict(int)
        self.semaphores: dict[int, asyncio.Semaphore] = {}
        self.reserved_chars: dict[str, int] = defaultdict(int)

        self.last_reload_ts: float | None = None
        self.last_reload_ok: bool | None = None
        self.last_reload_added: int = 0
        self.last_reload_removed: int = 0
        self.last_reload_changed: int = 0
        self._reload_lock: asyncio.Lock = asyncio.Lock()
        self._reload_task: asyncio.Task[None] | None = None

        self.admin_site: tuple[web.TCPSite, web.AppRunner] | None = None
        self.session: ClientSession | None = None
        self._purge_task: asyncio.Task[None] | None = None
        self._stopped = False

    def get_in_flight(self, port: int) -> int:
        return self.in_flight.get(port, 0)

    def inc_in_flight(self, port: int) -> None:
        self.in_flight[port] = self.in_flight.get(port, 0) + 1

    def dec_in_flight(self, port: int) -> None:
        self.in_flight[port] = max(0, self.in_flight.get(port, 0) - 1)

    def get_semaphore(self, port: int, concurrency: int = 16) -> asyncio.Semaphore:
        if port not in self.semaphores:
            self.semaphores[port] = asyncio.Semaphore(concurrency)
        return self.semaphores[port]

    def get_reserved_chars(self, client: str) -> int:
        return self.reserved_chars.get(client, 0)

    def add_reserved_chars(self, client: str, amount: int) -> None:
        self.reserved_chars[client] = self.reserved_chars.get(client, 0) + amount

    def remove_reserved_chars(self, client: str, amount: int) -> None:
        self.reserved_chars[client] = max(0, self.reserved_chars.get(client, 0) - amount)

    async def start(self) -> None:
        """Initialize session, load configs, start sites and background tasks."""
        self.usage_tracker.start()
        connector = aiohttp.TCPConnector(limit=0, limit_per_host=64, ttl_dns_cache=300)
        self.session = ClientSession(connector=connector, auto_decompress=False)

        # Load routes (never reloaded on SIGHUP)
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

        # Start lane sites (one failure does not crash the broker)
        for lane in lanes_list:
            self.lanes[lane.port] = lane
            try:
                site, runner = await self._start_lane(lane)
                self.lane_sites[lane.port] = (lane, site, runner)
                self.lane_states[lane.port] = "ok"
            except Exception as exc:
                logger.error("Failed to start lane %s on port %d: %s", lane.client, lane.port, exc)
                self.lane_states[lane.port] = "failed"

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
            broker_app=self,
        )
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app, access_log=None, keepalive_timeout=30)
        await runner.setup()
        site = web.TCPSite(runner, host=self.bind, port=lane.port)
        try:
            await site.start()
        except Exception:
            await runner.cleanup()
            raise
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

        runner = web.AppRunner(app, access_log=None, keepalive_timeout=30)
        await runner.setup()
        site = web.TCPSite(runner, host=self.bind, port=self.admin_port)
        await site.start()
        self.admin_site = (site, runner)
        logger.info("Admin site listening on %s:%d", self.bind, self.admin_port)

    async def _admin_healthz(self, request: web.Request) -> web.Response:
        """Admin healthcheck endpoint."""
        lanes_data = []
        for port, lane in sorted(self.lanes.items(), key=lambda x: x[0]):
            lanes_data.append({
                "client": lane.client,
                "port": lane.port,
                "limits": {
                    "elevenlabs_chars_month": lane.limits.elevenlabs_chars_month,
                    "rpm": lane.limits.rpm,
                    "concurrency": lane.limits.concurrency,
                },
                "in_flight": self.in_flight.get(port, 0),
                "state": self.lane_states.get(port, "ok"),
            })

        routes_without_key = [r.prefix for r in self.routes if not r.get_api_key()]
        last_reload_data = {
            "ts": self.last_reload_ts,
            "ok": self.last_reload_ok,
            "added": self.last_reload_added,
            "removed": self.last_reload_removed,
            "changed": self.last_reload_changed,
        }
        return web.json_response({
            "ok": True,
            "lanes": lanes_data,
            "routes": len(self.routes),
            "queue_depth": self.usage_tracker.queue_depth,
            "dropped_records": self.usage_tracker.dropped_records,
            "last_reload": last_reload_data,
            "routes_without_key": routes_without_key,
        })

    async def _admin_usage(self, request: web.Request) -> web.Response:
        """Admin usage aggregates endpoint with bad_month validation."""
        client = request.query.get("client")
        month = request.query.get("month")
        if month:
            try:
                _get_utc_month_range(month)
            except ValueError:
                return web.json_response({"error": "bad_month"}, status=400)
        else:
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
        """Reload lanes.json under lock: start new, stop removed, preserve or restart existing."""
        async with self._reload_lock:
            return await self._reload_lanes_unlocked()

    async def _reload_lanes_unlocked(self) -> bool:
        now_ts = time.time()
        errors = 0
        added = 0
        removed = 0
        changed = 0

        try:
            new_lanes_list = load_lanes(self.lanes_path)
        except Exception as exc:
            logger.error("Failed to reload lanes from %s: %s", self.lanes_path, exc)
            self.last_reload_ts = now_ts
            self.last_reload_ok = False
            logger.info("reload: +0 added, -0 removed, ~0 changed, errors=1")
            return False

        new_by_port = {l.port: l for l in new_lanes_list}
        current_ports = set(self.lanes.keys())
        new_ports = set(new_by_port.keys())

        # Remove stopped lanes
        for port in (current_ports - new_ports):
            logger.info("Stopping lane on port %d", port)
            await self._stop_lane(port)
            self.lanes.pop(port, None)
            self.lane_states.pop(port, None)
            self.semaphores.pop(port, None)
            removed += 1

        # Add new lanes
        for port in (new_ports - current_ports):
            lane = new_by_port[port]
            self.lanes[port] = lane
            try:
                site, runner = await self._start_lane(lane)
                self.lane_sites[port] = (lane, site, runner)
                self.lane_states[port] = "ok"
                added += 1
            except Exception as exc:
                logger.error("Failed to start lane %s on port %d: %s", lane.client, port, exc)
                self.lane_states[port] = "failed"
                errors += 1

        # Update existing
        for port in (current_ports & new_ports):
            old_lane = self.lanes[port]
            new_lane = new_by_port[port]
            if old_lane.client != new_lane.client or old_lane.uid != new_lane.uid:
                # client or uid changed -> restart lane
                logger.info("Client or UID changed for port %d, restarting lane", port)
                await self._stop_lane(port)
                self.lanes[port] = new_lane
                try:
                    site, runner = await self._start_lane(new_lane)
                    self.lane_sites[port] = (new_lane, site, runner)
                    self.lane_states[port] = "ok"
                    changed += 1
                except Exception as exc:
                    logger.error("Failed to restart lane on port %d: %s", port, exc)
                    self.lane_states[port] = "failed"
                    errors += 1
            else:
                # Update limits in-place without dropping connections
                old_concurrency = old_lane.limits.concurrency
                old_lane.limits = new_lane.limits
                self.lanes[port] = old_lane
                if port in self.lane_sites:
                    site, runner = self.lane_sites[port][1], self.lane_sites[port][2]
                    self.lane_sites[port] = (old_lane, site, runner)
                if port in self.semaphores and old_concurrency != new_lane.limits.concurrency:
                    self.semaphores[port] = asyncio.Semaphore(new_lane.limits.concurrency)
                changed += 1

        self.last_reload_ts = now_ts
        self.last_reload_ok = (errors == 0)
        self.last_reload_added = added
        self.last_reload_removed = removed
        self.last_reload_changed = changed

        logger.info(
            "reload: +%d added, -%d removed, ~%d changed, errors=%d",
            added,
            removed,
            changed,
            errors,
        )
        return errors == 0

    async def _purge_loop(self) -> None:
        """Purge records older than 13 months: first run at +60s, then every 24h in executor."""
        loop = asyncio.get_running_loop()
        try:
            await asyncio.sleep(60)
            if not self._stopped:
                rowcount = await loop.run_in_executor(None, self.usage_tracker.purge_older_than, 395)
                logger.info("Purged %d old usage records", rowcount)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Error during initial usage purge")

        while not self._stopped:
            try:
                await asyncio.sleep(86400)
                rowcount = await loop.run_in_executor(None, self.usage_tracker.purge_older_than, 395)
                logger.info("Purged %d old usage records", rowcount)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error during daily usage purge")

    async def stop(self) -> None:
        """Gracefully shut down all sites and release resources (timeout 20s)."""
        if self._stopped:
            return
        self._stopped = True

        if self._purge_task and not self._purge_task.done():
            self._purge_task.cancel()
            try:
                await self._purge_task
            except asyncio.CancelledError:
                pass

        try:
            # Stop admin site
            if self.admin_site:
                site, runner = self.admin_site
                await site.stop()
                await runner.cleanup()
                self.admin_site = None

            # Stop all lane sites in parallel
            sites = list(self.lane_sites.values())
            if sites:
                await asyncio.gather(*(s[1].stop() for s in sites), return_exceptions=True)
                await asyncio.gather(*(s[2].cleanup() for s in sites), return_exceptions=True)
            self.lane_sites.clear()
        finally:
            if self.session and not self.session.closed:
                await self.session.close()
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

    # Ignore SIGHUP before registering async handler
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    loop.add_signal_handler(
        signal.SIGHUP,
        lambda: setattr(app, "_reload_task", asyncio.create_task(app.reload_lanes())),
    )

    await app.start()
    logger.info("ProAI key broker started successfully")

    await stop_event.wait()
    logger.info("Shutting down key broker...")
    await app.stop()
    logger.info("Key broker stopped")
