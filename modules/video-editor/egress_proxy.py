#!/usr/bin/env python3
from __future__ import annotations

import ipaddress
import selectors
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"http", "https"}
DEFAULT_PORTS = {"http": 80, "https": 443}
CONNECT_TIMEOUT = 10.0
TUNNEL_IDLE_TIMEOUT = 30.0
TUNNEL_MAX_SECONDS = 75.0
DEFAULT_BUDGET = 48 * 1024 * 1024
MAX_CONNECTION_BYTES = 32 * 1024 * 1024


def _global_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return bool(ip.is_global)


def resolve_public(host: str, port: int) -> list[str]:
    if not host or host.lower().rstrip(".") in {"localhost", "localhost.localdomain"}:
        raise ValueError("non_public_host")
    try:
        direct = ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        direct = None
    if direct is not None:
        if not direct.is_global:
            raise ValueError("non_public_host")
        return [str(direct)]
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("dns_resolution_failed") from exc
    ips: list[str] = []
    for item in infos:
        raw = str(item[4][0]).split("%", 1)[0]
        if raw not in ips:
            ips.append(raw)
    if not ips or not all(_global_ip(ip) for ip in ips):
        raise ValueError("non_public_host")
    return ips


def validate_public_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.hostname:
        raise ValueError("unsafe_url")
    if parsed.username or parsed.password:
        raise ValueError("unsafe_credentials")
    port = parsed.port or DEFAULT_PORTS[parsed.scheme]
    if port != DEFAULT_PORTS[parsed.scheme]:
        raise ValueError("unsafe_port")
    resolve_public(parsed.hostname, port)
    return parsed.geturl()


class EgressBudget:
    def __init__(self, maximum: int = DEFAULT_BUDGET):
        self.maximum = max(1024 * 1024, int(maximum))
        self.used = 0
        self.exceeded = False
        self._lock = threading.Lock()

    def consume(self, amount: int) -> bool:
        with self._lock:
            self.used += max(0, int(amount))
            if self.used > self.maximum:
                self.exceeded = True
                return False
            return True


def connect_pinned(ips: list[str], port: int) -> socket.socket:
    last: OSError | None = None
    for raw in ips:
        ip = ipaddress.ip_address(raw)
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        try:
            address = (raw, port, 0, 0) if ip.version == 6 else (raw, port)
            sock.connect(address)
            sock.settimeout(None)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
    raise OSError("public_connect_failed") from last


def tunnel(client: socket.socket, upstream: socket.socket, budget: EgressBudget) -> None:
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, upstream)
    selector.register(upstream, selectors.EVENT_READ, client)
    started = last_activity = time.monotonic()
    connection_bytes = 0
    try:
        while True:
            now = time.monotonic()
            if now - started > TUNNEL_MAX_SECONDS or now - last_activity > TUNNEL_IDLE_TIMEOUT:
                break
            events = selector.select(timeout=1.0)
            if not events:
                continue
            for key, _ in events:
                source = key.fileobj
                target = key.data
                try:
                    data = source.recv(64 * 1024)
                except OSError:
                    return
                if not data:
                    return
                connection_bytes += len(data)
                if connection_bytes > MAX_CONNECTION_BYTES or not budget.consume(len(data)):
                    return
                try:
                    target.sendall(data)
                except OSError:
                    return
                last_activity = time.monotonic()
    finally:
        selector.close()
        try:
            upstream.close()
        except OSError:
            pass


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], budget: EgressBudget):
        self.budget = budget
        super().__init__(address, ProxyHandler)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VektorPublicEgress/1.0"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _deny(self, code: int = 403) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_CONNECT(self) -> None:
        try:
            target = urlsplit("//" + self.path)
            if not target.hostname or target.username or target.password:
                raise ValueError("invalid_connect_target")
            port = target.port or 443
            if port != 443:
                raise ValueError("unsafe_connect_port")
            ips = resolve_public(target.hostname, port)
            upstream = connect_pinned(ips, port)
        except (ValueError, OSError):
            self._deny(403)
            return
        self.send_response(200, "Connection Established")
        self.send_header("Proxy-Agent", self.server_version)
        self.end_headers()
        self.close_connection = True
        tunnel(self.connection, upstream, self.server.budget)

    def _plain_http(self) -> None:
        if self.command not in {"GET", "HEAD", "OPTIONS"}:
            self._deny(405)
            return
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme != "http":
                raise ValueError("plain_proxy_requires_http")
            validate_public_url(self.path)
            port = parsed.port or 80
            ips = resolve_public(parsed.hostname or "", port)
            upstream = connect_pinned(ips, port)
        except (ValueError, OSError):
            self._deny(403)
            return
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        hop = {"proxy-connection", "connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer"}
        headers = []
        for key, value in self.headers.items():
            if key.lower() in hop or key.lower() == "host":
                continue
            headers.append(f"{key}: {value}\r\n")
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        request = f"{self.command} {target} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n" + "".join(headers) + "\r\n"
        try:
            upstream.sendall(request.encode("iso-8859-1", errors="replace"))
            upstream.settimeout(TUNNEL_IDLE_TIMEOUT)
            sent = 0
            while True:
                data = upstream.recv(64 * 1024)
                if not data:
                    break
                sent += len(data)
                if sent > MAX_CONNECTION_BYTES or not self.server.budget.consume(len(data)):
                    break
                self.connection.sendall(data)
        except OSError:
            pass
        finally:
            upstream.close()
            self.close_connection = True

    def do_GET(self) -> None:
        self._plain_http()

    def do_HEAD(self) -> None:
        self._plain_http()

    def do_OPTIONS(self) -> None:
        self._plain_http()

    def do_POST(self) -> None:
        self._deny(405)

    def do_PUT(self) -> None:
        self._deny(405)

    def do_DELETE(self) -> None:
        self._deny(405)

    def do_PATCH(self) -> None:
        self._deny(405)


class PublicEgressProxy:
    def __init__(self, maximum_bytes: int = DEFAULT_BUDGET):
        self.budget = EgressBudget(maximum_bytes)
        self.server = ProxyServer(("127.0.0.1", 0), self.budget)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "PublicEgressProxy":
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
