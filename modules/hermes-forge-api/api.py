#!/usr/bin/env python3
"""Unprivileged stdlib HTTP API and Mini App static server for Hermes Forge."""
from __future__ import annotations

import importlib.util
import hashlib
import hmac
import json
import mimetypes
import os
import re
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

CONTROL_SOCKET = Path(os.environ.get("FORGE_CONTROL_SOCKET", "/run/proai-hermes-forge/control.sock"))
STATIC_ROOT = Path(__file__).resolve().parent / "static"
LISTEN_HOST = os.environ.get("FORGE_API_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("FORGE_API_PORT", "8650"))
LISTEN_SOCKET = os.environ.get("LISTEN_SOCKET", "").strip()
MAX_BODY = 64 * 1024
ZOOM_WEBHOOK_SECRET_ENV = "ZOOM_WEBHOOK_SECRET_TOKEN"
ZOOM_WEBHOOK_MAX_SKEW_SECONDS = 5 * 60
ZOOM_WEBHOOK_EVENTS = frozenset({
    "meeting.ended",
    "recording.completed",
    "recording.transcript_completed",
})
CATALOG_PATH = Path(os.environ.get("FORGE_CATALOG_PATH", "/opt/proai-hermes-forge-catalog/current/catalog.json"))
MAX_CATALOG_BYTES = 512 * 1024
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,80}$")
ALLOWED_PERSONAL_SECRETS = frozenset({"MCP_MATON_API_KEY"})
CATALOG_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_HANDLER_SEMAPHORE = threading.Semaphore(32)


class ApiError(RuntimeError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def _zoom_webhook_secret() -> str:
    value = str(os.environ.get(ZOOM_WEBHOOK_SECRET_ENV) or "").strip()
    if len(value) < 20 or len(value) > 512:
        raise ApiError("zoom_webhook_unconfigured", 503)
    return value


def _zoom_webhook_signature_valid(headers: Any, raw: bytes, *, now: int | None = None) -> bool:
    timestamp = str(headers.get("x-zm-request-timestamp") or "").strip()
    signature = str(headers.get("x-zm-signature") or "").strip()
    try:
        stamp = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(time.time()) if now is None else int(now)
    if abs(current - stamp) > ZOOM_WEBHOOK_MAX_SKEW_SECONDS:
        return False
    secret = _zoom_webhook_secret()
    message = b"v0:" + timestamp.encode("ascii") + b":" + raw
    expected = "v0=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _zoom_webhook_payload(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError("zoom_webhook_json_invalid", 400) from None
    if not isinstance(payload, dict):
        raise ApiError("zoom_webhook_json_invalid", 400)
    return payload


def _zoom_validation_response(payload: dict[str, Any]) -> dict[str, str]:
    plain = str(((payload.get("payload") or {}).get("plainToken") or "")).strip()
    if not plain or len(plain) > 512:
        raise ApiError("zoom_validation_invalid", 400)
    digest = hmac.new(_zoom_webhook_secret().encode("utf-8"), plain.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"plainToken": plain, "encryptedToken": digest}


def _zoom_event_envelope(payload: dict[str, Any]) -> dict[str, Any] | None:
    event = str(payload.get("event") or "").strip()
    if event not in ZOOM_WEBHOOK_EVENTS:
        return None
    outer = payload.get("payload")
    obj = outer.get("object") if isinstance(outer, dict) else None
    if not isinstance(outer, dict) or not isinstance(obj, dict):
        raise ApiError("zoom_event_invalid", 400)
    account_id = str(outer.get("account_id") or obj.get("account_id") or "").strip()
    host_id = str(obj.get("host_id") or "").strip()
    meeting_uuid = str(obj.get("uuid") or "").strip()
    meeting_id = str(obj.get("id") or "").strip()
    if not account_id or not host_id or not meeting_uuid or not meeting_id:
        raise ApiError("zoom_event_invalid", 400)
    return {
        "event": event,
        "event_ts": int(payload.get("event_ts") or 0),
        "account_id": account_id[:128],
        "host_id": host_id[:128],
        "meeting_id": meeting_id[:64],
        "meeting_uuid": meeting_uuid[:256],
    }


def _load_kitchen_module() -> Any:
    local = Path(__file__).resolve().with_name("kitchen.py")
    source = Path(__file__).resolve().parents[1] / "hermes-forge-kitchen" / "kitchen.py"
    for candidate in (local, source):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        spec = importlib.util.spec_from_file_location("hermes_forge_kitchen_api", candidate)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise RuntimeError("kitchen_compiler_unavailable")


KITCHEN = _load_kitchen_module()


def call_control(payload: dict[str, Any], *, timeout: float = 90.0) -> dict[str, Any]:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > MAX_BODY:
        raise ApiError("request_too_large", 413)
    client: socket.socket | None = None
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
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


def _verified_catalog_document(path: Path | None = None) -> tuple[dict[str, Any], str]:
    path = CATALOG_PATH if path is None else path
    marker = path.with_name("catalog.sha256")
    if path.is_symlink() or not path.is_file() or marker.is_symlink() or not marker.is_file():
        raise ApiError("catalog_unavailable", 503)
    size = path.stat().st_size
    if size <= 0 or size > MAX_CATALOG_BYTES or marker.stat().st_size > 128:
        raise ApiError("catalog_invalid", 503)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        digest = marker.read_text(encoding="utf-8").strip()
    except Exception:
        raise ApiError("catalog_invalid", 503) from None
    if not isinstance(payload, dict) or not CATALOG_DIGEST_RE.fullmatch(digest):
        raise ApiError("catalog_invalid", 503)
    return payload, digest


def _kitchen_preview(body: dict[str, Any]) -> dict[str, Any]:
    allowed = {"agent_id", "optional_capabilities"}
    if not isinstance(body, dict) or set(body) - allowed:
        raise ApiError("kitchen_request_invalid", 400)
    agent_id = body.get("agent_id")
    optional = body.get("optional_capabilities", [])
    if not isinstance(agent_id, str) or not isinstance(optional, list) or len(optional) > 32:
        raise ApiError("kitchen_request_invalid", 400)
    if any(not isinstance(item, str) for item in optional):
        raise ApiError("kitchen_request_invalid", 400)
    payload, digest = _verified_catalog_document()
    try:
        plan = KITCHEN.compile_catalog_document(payload, digest, agent_id, optional)
        return KITCHEN.public_preview(plan)
    except KITCHEN.KitchenError as exc:
        code = str(exc)[:100]
        client_error = code.startswith(("agent_id_", "agent_not_found", "optional_capability_"))
        raise ApiError(code, 400 if client_error else 503) from None


def _public_catalog(path: Path | None = None) -> dict[str, Any]:
    path = CATALOG_PATH if path is None else path
    if path.is_symlink() or not path.is_file():
        raise ApiError("catalog_unavailable", 503)
    size = path.stat().st_size
    if size <= 0 or size > MAX_CATALOG_BYTES:
        raise ApiError("catalog_invalid", 503)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise ApiError("catalog_invalid", 503) from None
    if not isinstance(payload, dict) or payload.get("schema") != "hermes.catalog/v1":
        raise ApiError("catalog_invalid", 503)
    capabilities = payload.get("capabilities")
    agents = payload.get("agents")
    if not isinstance(capabilities, list) or not isinstance(agents, list):
        raise ApiError("catalog_invalid", 503)

    public_capabilities = []
    for item in capabilities:
        if not isinstance(item, dict):
            raise ApiError("catalog_invalid", 503)
        connection = item.get("connection") or {}
        permissions = item.get("permissions") or {}
        if not isinstance(connection, dict) or not isinstance(permissions, dict):
            raise ApiError("catalog_invalid", 503)
        public_capabilities.append({
            "id": str(item.get("id") or "")[:64],
            "name": str(item.get("name") or "")[:100],
            "summary": str(item.get("summary") or "")[:500],
            "version": str(item.get("version") or "")[:40],
            "publisher": str(item.get("publisher") or "")[:100],
            "trust_tier": str(item.get("trust_tier") or "")[:32],
            "kind": str(item.get("kind") or "")[:32],
            "availability": str(item.get("availability") or "")[:32],
            "connection_mode": str(connection.get("mode") or "")[:32],
            "action_default": str(permissions.get("action_default") or "")[:32],
            "metering": str(item.get("metering") or "")[:32],
        })
    public_agents = []
    for item in agents:
        if not isinstance(item, dict):
            raise ApiError("catalog_invalid", 503)
        capabilities_value = item.get("capabilities") or {}
        if not isinstance(capabilities_value, dict):
            raise ApiError("catalog_invalid", 503)
        required = capabilities_value.get("required", [])
        optional = capabilities_value.get("optional", [])
        if not isinstance(required, list) or not isinstance(optional, list):
            raise ApiError("catalog_invalid", 503)
        public_agents.append({
            "id": str(item.get("id") or "")[:64],
            "name": str(item.get("name") or "")[:100],
            "summary": str(item.get("summary") or "")[:500],
            "version": str(item.get("version") or "")[:40],
            "publisher": str(item.get("publisher") or "")[:100],
            "role": str(item.get("role") or "")[:64],
            "required_capabilities": [str(x)[:64] for x in required if isinstance(x, str)][:32],
            "optional_capabilities": [str(x)[:64] for x in optional if isinstance(x, str)][:32],
        })
    return {"schema": "hermes.catalog.public/v1", "capabilities": public_capabilities, "agents": public_agents}


def route(method: str, path: str, body: dict[str, Any], headers: Any) -> tuple[int, dict[str, Any]]:
    if method == "GET" and path == "/healthz":
        return 200, {"ok": True}
    if method == "POST" and path == "/v1/auth/telegram":
        return 200, call_control({"op": "authenticate", "init_data": str(body.get("init_data") or "")})
    if method == "GET" and path == "/v1/catalog":
        return 200, _public_catalog()
    if method == "POST" and path == "/v1/kitchen/preview":
        return 200, _kitchen_preview(body)
    session = _session(headers)
    if method == "POST" and path == "/v1/auth/logout":
        return 200, call_control({"op": "logout", "session": session})
    if method == "POST" and path.startswith("/v1/invites/") and path.endswith("/accept"):
        token=path[len("/v1/invites/"):-len("/accept")].strip("/")
        return 200, call_control({"op":"accept_workspace_invite","session":session,"token":token,"actor_name":str(body.get("actor_name") or "Участник")})
    if method == "GET" and path == "/v1/hermes":
        return 200, call_control({"op": "list_hermes", "session": session})
    match = re.fullmatch(r"/v1/hermes/([a-z0-9][a-z0-9_-]{1,39})(?:/(.*))?", path)
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
    if method == "GET" and suffix == "invites":
        return 200, call_control({"op": "list_workspace_invites", **base})
    m_inv = re.fullmatch(r"invites/([A-Za-z0-9_-]{16,64})", suffix)
    if method == "DELETE" and m_inv:
        return 200, call_control({"op": "revoke_workspace_invite", **base, "token": m_inv.group(1)})
    m_inv_rev = re.fullmatch(r"invites/([A-Za-z0-9_-]{16,64})/revoke", suffix)
    if method == "POST" and m_inv_rev:
        return 200, call_control({"op": "revoke_workspace_invite", **base, "token": m_inv_rev.group(1)})
    if method == "GET" and suffix == "members": return 200, call_control({"op":"list_workspace_members",**base})
    if method == "POST" and suffix == "members/invite": return 200, call_control({"op":"create_workspace_invite",**base})
    m=re.fullmatch(r"members/(\d{5,20})",suffix)
    if method == "DELETE" and m: return 200, call_control({"op":"remove_workspace_member",**base,"member_user_id":int(m.group(1))})
    g=re.fullmatch(r"members/(\d{5,20})/grants",suffix)
    if method == "POST" and g: return 200, call_control({"op":"grant_workspace_tool",**base,"member_user_id":int(g.group(1)),"tool_name":str(body.get("tool_name") or ""),"ttl_days":int(body.get("ttl_days") or 0)})
    if method == "DELETE" and g: return 200, call_control({"op":"revoke_workspace_tool",**base,"member_user_id":int(g.group(1)),"tool_name":str(body.get("tool_name") or "")})
    if method == "GET" and suffix == "connections":
        return 200, call_control({"op": "list_connections", **base})
    if method == "POST" and suffix == "connections/zoom/bind":
        requested = str(body.get("connection_id") or "").strip()
        return 200, call_control({"op": "bind_zoom", **base, "connection_id": requested})
    if method == "GET" and suffix == "capabilities":
        return 200, call_control({"op": "list_capabilities", **base})
    capability_action = re.fullmatch(r"capabilities/([a-z0-9-]{2,64})/(enable|disable)", suffix)
    if method == "POST" and capability_action:
        return 200, call_control({
            "op": "capability_action", **base,
            "capability_id": capability_action.group(1),
            "action": capability_action.group(2),
        })
    if method == "GET" and suffix == "secrets":
        return 200, call_control({"op": "list_secrets", **base})
    if suffix.startswith("secrets/"):
        secret = re.fullmatch(r"secrets/([A-Z][A-Z0-9_]{2,80})", suffix)
        if not secret or not SECRET_RE.fullmatch(secret.group(1)) or secret.group(1) not in ALLOWED_PERSONAL_SECRETS:
            raise ApiError("secret_not_allowed", 400)
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
    timeout = 10

    def address_string(self) -> str:
        if isinstance(self.client_address, str):
            return self.client_address or "unix"
        if not self.client_address:
            return "unix"
        try:
            return super().address_string()
        except Exception:
            return "unknown"

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

    def _raw_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
        except ValueError:
            raise ApiError("content_length_invalid", 400) from None
        if length < 0 or length > MAX_BODY:
            raise ApiError("request_too_large", 413)
        return self.rfile.read(length)

    def _zoom_webhook(self) -> None:
        try:
            raw = self._raw_body()
            if not _zoom_webhook_signature_valid(self.headers, raw):
                raise ApiError("zoom_webhook_signature_invalid", 401)
            payload = _zoom_webhook_payload(raw)
            if str(payload.get("event") or "") == "endpoint.url_validation":
                self._json(200, _zoom_validation_response(payload))
                return
            envelope = _zoom_event_envelope(payload)
            if envelope is None:
                self._json(200, {"ok": True, "ignored": True})
                return
            result = call_control({"op": "zoom_webhook_event", "event": envelope}, timeout=2.0)
            self._json(200, {"ok": True, "result": result})
        except ApiError as exc:
            self._json(exc.status, {"ok": False, "error": exc.code})
        except Exception:
            self._json(500, {"ok": False, "error": "internal_error"})

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
        csp = (
            "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "frame-ancestors https://web.telegram.org https://*.telegram.org; base-uri 'none'; form-action 'none'; object-src 'none'"
        )
        self.send_header("Content-Security-Policy", csp)
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
        if urllib_path(self.path) == "/v1/webhooks/zoom":
            self._zoom_webhook()
            return
        self._api()

    def do_PUT(self) -> None:
        self._api()

    def do_DELETE(self) -> None:
        self._api()


def urllib_path(value: str) -> str:
    return value.split("?", 1)[0]


class BoundedThreadingMixIn:
    def process_request(self, request: Any, client_address: Any) -> None:
        if not _HANDLER_SEMAPHORE.acquire(blocking=False):
            try:
                err_body = b'{"ok":false,"error":"server_overloaded"}'
                resp = (
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json; charset=utf-8\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: " + str(len(err_body)).encode("ascii") + b"\r\n\r\n" + err_body
                )
                if hasattr(request, "sendall"):
                    request.sendall(resp)
            except Exception:
                pass
            finally:
                self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            _HANDLER_SEMAPHORE.release()


class BoundedThreadingHTTPServer(BoundedThreadingMixIn, ThreadingHTTPServer):
    daemon_threads = True


class BoundedThreadingUnixHTTPServer(BoundedThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve() -> None:
    if os.geteuid() == 0:
        raise SystemExit("refusing to run public API as root")
    if LISTEN_SOCKET:
        sock_path = Path(LISTEN_SOCKET)
        sock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        if sock_path.is_symlink() or sock_path.is_file() or sock_path.is_socket():
            sock_path.unlink()
        server = BoundedThreadingUnixHTTPServer(str(sock_path), Handler)
        try:
            os.chmod(str(sock_path), 0o660)
            try:
                import grp
                www_gid = grp.getgrnam("www-data").gr_gid
                os.chown(str(sock_path), -1, www_gid)
            except Exception:
                pass
            server.serve_forever(poll_interval=0.5)
        finally:
            server.server_close()
            try:
                if sock_path.exists():
                    sock_path.unlink()
            except OSError:
                pass
    else:
        server = BoundedThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    serve()
