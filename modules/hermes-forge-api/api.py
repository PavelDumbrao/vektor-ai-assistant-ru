#!/usr/bin/env python3
"""Unprivileged stdlib HTTP API and Mini App static server for Hermes Forge."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

CONTROL_SOCKET = Path(os.environ.get("FORGE_CONTROL_SOCKET", "/run/proai-hermes-forge/control.sock"))
STATIC_ROOT = Path(__file__).resolve().parent / "static"
LISTEN_HOST = os.environ.get("FORGE_API_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("FORGE_API_PORT", "8650"))
MAX_BODY = 64 * 1024
PROFILE_RE = re.compile(r"^[a-z0-9_-]{2,40}$")
SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,80}$")


class ApiError(RuntimeError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def call_control(payload: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > MAX_BODY:
        raise ApiError("request_too_large", 413)
    client: socket.socket | None = None
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(90)
        client.connect(str(CONTROL_SOCKET))
        client.sendall(raw)
        response = b""
        while not response.endswith(b"\n") and len(response) <= MAX_BODY:
            chunk = client.recv(8192)
            if not chunk:
                break
            response += chunk
    except (OSError, TimeoutError):
        raise ApiError("control_unavailable", 503) from None
    finally:
        try:
            if client is not None:
                client.close()
        except Exception:
            pass
    if not response.endswith(b"\n") or len(response) > MAX_BODY:
        raise ApiError("control_invalid_response", 503)
    try:
        payload = json.loads(response.decode("utf-8"))
    except Exception:
        raise ApiError("control_invalid_response", 503) from None
    if payload.get("ok") is not True:
        raise ApiError(str(payload.get("error") or "control_error")[:80], int(payload.get("status") or 500))
    result = payload.get("result")
    return result if isinstance(result, dict) else {"value": result}


def _session(headers: Any) -> str:
    auth = str(headers.get("Authorization") or "")
    if not auth.startswith("Bearer "):
        raise ApiError("session_required", 401)
    token = auth[7:].strip()
    if not token or len(token) > 128:
        raise ApiError("session_invalid", 401)
    return token


def route(method: str, path: str, body: dict[str, Any], headers: Any) -> tuple[int, dict[str, Any]]:
    if method == "GET" and path == "/healthz":
        return 200, {"ok": True}
    if method == "POST" and path == "/v1/auth/telegram":
        return 200, call_control({"op": "authenticate", "init_data": str(body.get("init_data") or "")})
    session = _session(headers)
    if method == "GET" and path == "/v1/hermes":
        return 200, call_control({"op": "list_hermes", "session": session})
    match = re.fullmatch(r"/v1/hermes/([a-z0-9_-]{2,40})(?:/(.*))?", path)
    if not match:
        raise ApiError("not_found", 404)
    profile, suffix = match.group(1), match.group(2) or ""
    if not PROFILE_RE.fullmatch(profile):
        raise ApiError("profile_invalid", 400)
    base = {"session": session, "profile": profile}
    if method == "GET" and not suffix:
        return 200, call_control({"op": "get_hermes", **base})
    if method == "GET" and suffix == "health":
        return 200, call_control({"op": "health", **base})
    if method == "POST" and suffix == "health-check":
        return 200, call_control({"op": "health_check", **base})
    if method == "POST" and suffix == "restart":
        return 200, call_control({"op": "restart", **base})
    if method == "GET" and suffix == "connections":
        return 200, call_control({"op": "list_connections", **base})
    if method == "GET" and suffix == "secrets":
        return 200, call_control({"op": "list_secrets", **base})
    secret = re.fullmatch(r"secrets/([A-Z][A-Z0-9_]{2,80})", suffix)
    if secret and SECRET_RE.fullmatch(secret.group(1)):
        name = secret.group(1)
        if method == "PUT":
            value = body.get("value")
            if not isinstance(value, str):
                raise ApiError("secret_value_invalid", 400)
            return 200, call_control({"op": "set_secret", **base, "name": name, "value": value})
        if method == "DELETE":
            return 200, call_control({"op": "delete_secret", **base, "name": name})
    connection = re.fullmatch(r"connections/([a-z0-9_-]{2,40})/test", suffix)
    if method == "POST" and connection:
        return 200, call_control({"op": "test_connection", **base, "connection_id": connection.group(1)})
    raise ApiError("not_found", 404)


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesForge/0.1"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        # Never let stdlib access logging accidentally include Authorization or bodies.
        return

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cache-Control", "no-store")

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return {}
        try:
            length = int(raw_length)
        except ValueError:
            raise ApiError("content_length_invalid", 400) from None
        if length < 0 or length > MAX_BODY:
            raise ApiError("request_too_large", 413)
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError("json_invalid", 400) from None
        if not isinstance(payload, dict):
            raise ApiError("json_object_required", 400)
        return payload

    def _api(self) -> None:
        try:
            body = self._body() if self.command in {"POST", "PUT", "DELETE"} else {}
            status, result = route(self.command, urllib_path(self.path), body, self.headers)
            self._json(status, {"ok": True, "result": result})
        except ApiError as exc:
            self._json(exc.status, {"ok": False, "error": exc.code})
        except Exception:
            self._json(500, {"ok": False, "error": "internal_error"})

    def _static(self) -> None:
        path = urllib_path(self.path)
        mapping = {"/": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
        name = mapping.get(path)
        if name is None:
            self.send_error(404)
            return
        file = STATIC_ROOT / name
        if file.is_symlink() or not file.is_file():
            self.send_error(404)
            return
        raw = file.read_bytes()
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors *")
        self.send_header("Content-Type", (mimetypes.guess_type(name)[0] or "application/octet-stream") + ("; charset=utf-8" if name.endswith((".html", ".js", ".css")) else ""))
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if urllib_path(self.path).startswith("/v1/") or urllib_path(self.path) == "/healthz":
            self._api()
        else:
            self._static()

    def do_POST(self) -> None:
        self._api()

    def do_PUT(self) -> None:
        self._api()

    def do_DELETE(self) -> None:
        self._api()


def urllib_path(value: str) -> str:
    return value.split("?", 1)[0]


def serve() -> None:
    if os.geteuid() == 0:
        raise SystemExit("refusing to run public API as root")
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    serve()
