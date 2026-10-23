"""Streaming reverse-proxy handler for keybroker."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any

import multidict
import yarl
from aiohttp import (
    ClientConnectionError,
    ClientConnectionResetError,
    ClientError,
    ClientOSError,
    ClientPayloadError,
    ClientSession,
    ClientTimeout,
    ServerDisconnectedError,
    web,
)

from keybroker.config import Lane, Route
from keybroker.usage import RequestRecord, UsageTracker, safe_int

logger = logging.getLogger("keybroker.proxy")
access_logger = logging.getLogger("keybroker.access")

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

CLIENT_AUTH_HEADERS = {
    "authorization",
    "xi-api-key",
    "x-api-key",
    "api-key",
}

ALLOWED_PATH_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~!$&'()*+,;=:@/-"
)


def validate_raw_path(raw_path: str) -> bool:
    """Validate raw path component before '?'. Must be in allowed characters, no traversal, no //."""
    if not raw_path:
        return False
    path_part = raw_path.split("?")[0]
    if not path_part.startswith("/"):
        return False
    if "//" in path_part:
        return False
    for ch in path_part:
        if ch not in ALLOWED_PATH_CHARS:
            return False
    for seg in path_part.split("/"):
        if seg == "." or seg == "..":
            return False
    return True


def find_matching_route(path: str, routes: list[Route]) -> tuple[Route | None, str]:
    """Find route with longest matching prefix. Returns (route, subpath)."""
    for route in routes:
        prefix = route.prefix
        if path == prefix:
            return route, ""
        if path.startswith(prefix + "/"):
            return route, path[len(prefix) :]
        if prefix.endswith("/") and path.startswith(prefix):
            return route, path[len(prefix) :]
    return None, ""


def build_upstream_url(route: Route, subpath: str, raw_query: str) -> yarl.URL:
    """Build upstream URL using yarl.URL(target, encoded=True) with defense-in-depth path check."""
    upstream_base = route.upstream.rstrip("/")
    if subpath:
        if not subpath.startswith("/"):
            subpath = "/" + subpath
        target_base = upstream_base + subpath
    else:
        target_base = upstream_base

    if raw_query:
        target = f"{target_base}?{raw_query}"
    else:
        target = target_base

    url = yarl.URL(target, encoded=True)

    # Defense in depth: verify url.raw_path starts with upstream's configured path
    upstream_parsed = yarl.URL(route.upstream)
    upstream_path = upstream_parsed.raw_path.rstrip("/")
    if upstream_path:
        if not url.raw_path.startswith(upstream_path):
            raise ValueError("Upstream path escape detected")
        rest = url.raw_path[len(upstream_path) :]
        if rest and not rest.startswith("/"):
            raise ValueError("Upstream path escape detected")

    return url


def filter_request_headers(
    headers: Any,
    route: Route,
    api_key: str,
) -> multidict.CIMultiDict[str]:
    """Filter headers case-insensitively into CIMultiDict, setting identity encoding and route auth."""
    out = multidict.CIMultiDict()
    auth_header_lower = route.auth.header.lower()

    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower in HOP_BY_HOP_HEADERS or k_lower.startswith("proxy-"):
            continue
        if k_lower in (
            "host",
            "content-length",
            "authorization",
            "xi-api-key",
            "x-api-key",
            "api-key",
            auth_header_lower,
            "cookie",
            "forwarded",
            "x-real-ip",
            "via",
            "accept-encoding",
        ):
            continue
        if k_lower.startswith("x-forwarded-"):
            continue
        out[k] = v

    # Set our identity encoding and route auth
    out["Accept-Encoding"] = "identity"
    out[route.auth.header] = route.auth.format.format(key=api_key)
    return out


def filter_response_headers(headers: Any) -> multidict.CIMultiDict[str]:
    """Filter hop-by-hop headers and set-cookie from upstream response."""
    out = multidict.CIMultiDict()
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower in HOP_BY_HOP_HEADERS or k_lower.startswith("proxy-"):
            continue
        if k_lower == "set-cookie":
            continue
        out[k] = v
    return out


def log_request(
    client: str,
    port: int,
    route: str,
    status: int,
    duration_ms: int,
    req_bytes: int,
    resp_bytes: int,
    chars: int | None,
    model: str | None,
    error: str | None = None,
) -> None:
    """Log single request line without sensitive data, with error=<код>."""
    access_logger.info(
        "client=%s port=%d route=%s status=%d ms=%d req=%d resp=%d chars=%d model=%s error=%s",
        client,
        port,
        route,
        status,
        duration_ms,
        req_bytes,
        resp_bytes,
        chars or 0,
        model or "",
        error or "",
    )


class PayloadTooLargeError(Exception):
    """Raised when request body exceeds maximum allowed size."""


def is_payload_too_large(exc: BaseException | None) -> bool:
    """Inspect exception cause and context chain for PayloadTooLargeError."""
    cur = exc
    while cur is not None:
        if isinstance(cur, PayloadTooLargeError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


async def handle_stt_request(
    request: web.Request,
    route: Route,
    subpath: str,
    api_key: str,
    lane: Lane,
    usage_tracker: UsageTracker,
    session: ClientSession,
    start_time: float,
) -> web.Response:
    """Handle kind: stt requests with strict headers, multipart limits and model ladder."""
    max_bytes = route.max_body_mb * 1024 * 1024

    # 1. Content-Length check
    if request.content_length is None:
        duration_ms = int((time.time() - start_time) * 1000)
        usage_tracker.record(
            RequestRecord(
                ts=time.time(),
                client=lane.client,
                port=lane.port,
                route=route.prefix,
                kind=route.kind,
                method=request.method,
                path=request.path,
                status=411,
                req_bytes=0,
                resp_bytes=0,
                duration_ms=duration_ms,
                error="length_required",
            )
        )
        log_request(lane.client, lane.port, route.prefix, 411, duration_ms, 0, 0, 0, "", "length_required")
        return web.json_response({"error": "length_required"}, status=411)

    if request.content_length > max_bytes:
        duration_ms = int((time.time() - start_time) * 1000)
        usage_tracker.record(
            RequestRecord(
                ts=time.time(),
                client=lane.client,
                port=lane.port,
                route=route.prefix,
                kind=route.kind,
                method=request.method,
                path=request.path,
                status=413,
                req_bytes=0,
                resp_bytes=0,
                duration_ms=duration_ms,
                error="payload_too_large",
            )
        )
        log_request(lane.client, lane.port, route.prefix, 413, duration_ms, 0, 0, 0, "", "payload_too_large")
        return web.json_response({"error": "payload_too_large"}, status=413)

    content_type = request.content_type.lower()
    req_bytes = 0
    fields: dict[str, str] = {}
    file_bytes = b""
    filename = ""

    if "multipart" in content_type:
        try:
            reader = await request.multipart()
            part_count = 0
            while True:
                part = await reader.next()
                if part is None:
                    break
                part_count += 1
                if part_count > 8:
                    return web.json_response({"error": "too_many_parts"}, status=400)

                part_name = part.name
                if part_name == "file":
                    filename = part.filename or "audio.ogg"
                    chunks = []
                    while True:
                        chunk = await part.read_chunk()
                        if not chunk:
                            break
                        req_bytes += len(chunk)
                        if req_bytes > max_bytes:
                            raise PayloadTooLargeError()
                        chunks.append(chunk)
                    file_bytes = b"".join(chunks)
                else:
                    text_chunks = []
                    text_bytes = 0
                    while True:
                        chunk = await part.read_chunk()
                        if not chunk:
                            break
                        req_bytes += len(chunk)
                        text_bytes += len(chunk)
                        if req_bytes > max_bytes:
                            raise PayloadTooLargeError()
                        if text_bytes > 4096:
                            return web.json_response({"error": "field_too_large"}, status=400)
                        text_chunks.append(chunk)
                    fields[part_name] = b"".join(text_chunks).decode("utf-8", errors="replace")
        except PayloadTooLargeError:
            duration_ms = int((time.time() - start_time) * 1000)
            usage_tracker.record(
                RequestRecord(
                    ts=time.time(),
                    client=lane.client,
                    port=lane.port,
                    route=route.prefix,
                    kind=route.kind,
                    method=request.method,
                    path=request.path,
                    status=413,
                    req_bytes=req_bytes,
                    resp_bytes=0,
                    duration_ms=duration_ms,
                    error="payload_too_large",
                )
            )
            log_request(lane.client, lane.port, route.prefix, 413, duration_ms, req_bytes, 0, 0, "", "payload_too_large")
            return web.json_response({"error": "payload_too_large"}, status=413)

        ext = os.path.splitext(filename)[1].lstrip(".").lower()
        if not ext or ext == "oga":
            ext = "ogg"

        b64_audio = base64.b64encode(file_bytes).decode("ascii")
        payload: dict[str, Any] = {
            "model": fields.get("model", ""),
            "input_audio": {
                "data": b64_audio,
                "format": ext,
            },
        }
        if "language" in fields:
            payload["language"] = fields["language"]
        if "prompt" in fields:
            payload["prompt"] = fields["prompt"]
        if "temperature" in fields:
            try:
                payload["temperature"] = float(fields["temperature"])
            except (ValueError, TypeError):
                pass
        response_format = fields.get("response_format", "json")
        req_model = fields.get("model", "")

    elif "application/json" in content_type:
        raw_body = await request.read()
        req_bytes = len(raw_body)
        try:
            body_dict = json.loads(raw_body.decode("utf-8"))
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)

        if not isinstance(body_dict, dict) or "input_audio" not in body_dict:
            return web.json_response({"error": "stt_body_unsupported"}, status=400)

        payload = body_dict
        req_model = payload.get("model", "")
        response_format = request.query.get("response_format", "json")
    else:
        return web.json_response({"error": "unsupported_media_type"}, status=415)

    # Build model trial list
    ladder = list(route.ladder)
    if req_model and req_model in ladder:
        models_to_try = [req_model] + [m for m in ladder if m != req_model]
    elif ladder:
        models_to_try = ladder
    elif req_model:
        models_to_try = [req_model]
    else:
        models_to_try = ["openai/whisper-large-v3-turbo"]

    raw_query = request.raw_path.split("?", 1)[1] if "?" in request.raw_path else ""
    try:
        target_url = build_upstream_url(route, subpath, raw_query)
    except ValueError:
        return web.json_response({"error": "bad_path"}, status=400)

    auth_header = route.auth.header
    auth_value = route.auth.format.format(key=api_key)
    req_headers = multidict.CIMultiDict({
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
        auth_header: auth_value,
    })

    last_error = ""
    last_status = 502
    chosen_model = ""
    success_text = None

    timeout = ClientTimeout(total=route.timeout_s, sock_connect=15, sock_read=route.timeout_s)

    for current_model in models_to_try:
        payload["model"] = current_model
        # Only send allowed fields to upstream
        upstream_payload = {
            k: v
            for k, v in payload.items()
            if k in {"model", "input_audio", "language", "prompt", "temperature", "response_format"}
        }
        payload_bytes = json.dumps(upstream_payload).encode("utf-8")
        try:
            async with session.post(
                target_url,
                headers=req_headers,
                data=payload_bytes,
                timeout=timeout,
                allow_redirects=False,
            ) as up_resp:
                if up_resp.status == 429 or up_resp.status >= 500:
                    last_status = up_resp.status
                    last_error = f"http_{up_resp.status}"
                    continue

                if 200 <= up_resp.status < 300:
                    try:
                        res_json = await up_resp.json()
                        text_val = res_json.get("text")
                        if text_val is None or str(text_val).strip() == "":
                            last_status = up_resp.status
                            last_error = "empty_text"
                            continue
                        success_text = str(text_val)
                        chosen_model = current_model
                        break
                    except Exception:
                        last_status = 502
                        last_error = "invalid_upstream_json"
                        continue
                else:
                    last_status = up_resp.status
                    last_error = f"http_{up_resp.status}"
                    break
        except (ClientError, asyncio.TimeoutError):
            last_status = 502
            last_error = "upstream_unreachable"
            continue

    duration_ms = int((time.time() - start_time) * 1000)

    if success_text is not None:
        resp_headers = {"X-Keybroker-Model": chosen_model}
        if response_format == "text":
            resp_body = success_text.encode("utf-8")
            response = web.Response(
                body=resp_body,
                content_type="text/plain",
                charset="utf-8",
                headers=resp_headers,
            )
        else:
            resp_dict = {"text": success_text}
            resp_body = json.dumps(resp_dict).encode("utf-8")
            response = web.Response(
                body=resp_body,
                content_type="application/json",
                charset="utf-8",
                headers=resp_headers,
            )

        resp_bytes = len(resp_body)
        usage_tracker.record(
            RequestRecord(
                ts=time.time(),
                client=lane.client,
                port=lane.port,
                route=route.prefix,
                kind=route.kind,
                method=request.method,
                path=request.path,
                status=200,
                req_bytes=req_bytes,
                resp_bytes=resp_bytes,
                duration_ms=duration_ms,
                model=chosen_model,
            )
        )
        log_request(
            lane.client,
            lane.port,
            route.prefix,
            200,
            duration_ms,
            req_bytes,
            resp_bytes,
            0,
            chosen_model,
        )
        return response

    error_code = last_error or "upstream_unreachable"
    usage_tracker.record(
        RequestRecord(
            ts=time.time(),
            client=lane.client,
            port=lane.port,
            route=route.prefix,
            kind=route.kind,
            method=request.method,
            path=request.path,
            status=last_status,
            req_bytes=req_bytes,
            resp_bytes=0,
            duration_ms=duration_ms,
            error=error_code,
        )
    )
    log_request(
        lane.client,
        lane.port,
        route.prefix,
        last_status,
        duration_ms,
        req_bytes,
        0,
        0,
        "",
        error_code,
    )
    return web.json_response({"error": error_code}, status=last_status)


async def handle_generic_streaming(
    request: web.Request,
    route: Route,
    subpath: str,
    api_key: str,
    lane: Lane,
    usage_tracker: UsageTracker,
    session: ClientSession,
    start_time: float,
    cached_body: bytes | None = None,
) -> web.StreamResponse:
    """Streaming reverse-proxy handler for HTTP requests with reliable accounting on disconnects."""
    max_bytes = route.max_body_mb * 1024 * 1024
    req_bytes = len(cached_body) if cached_body is not None else 0

    if request.content_length is not None and request.content_length > max_bytes:
        duration_ms = int((time.time() - start_time) * 1000)
        usage_tracker.record(
            RequestRecord(
                ts=time.time(),
                client=lane.client,
                port=lane.port,
                route=route.prefix,
                kind=route.kind,
                method=request.method,
                path=request.path,
                status=413,
                req_bytes=0,
                resp_bytes=0,
                duration_ms=duration_ms,
                error="payload_too_large",
            )
        )
        log_request(lane.client, lane.port, route.prefix, 413, duration_ms, 0, 0, 0, "", "payload_too_large")
        return web.json_response({"error": "payload_too_large"}, status=413)

    raw_query = request.raw_path.split("?", 1)[1] if "?" in request.raw_path else ""
    try:
        target_url = build_upstream_url(route, subpath, raw_query)
    except ValueError:
        duration_ms = int((time.time() - start_time) * 1000)
        usage_tracker.record(
            RequestRecord(
                ts=time.time(),
                client=lane.client,
                port=lane.port,
                route=route.prefix,
                kind=route.kind,
                method=request.method,
                path=request.path,
                status=400,
                req_bytes=0,
                resp_bytes=0,
                duration_ms=duration_ms,
                error="bad_path",
            )
        )
        log_request(lane.client, lane.port, route.prefix, 400, duration_ms, 0, 0, 0, "", "bad_path")
        return web.json_response({"error": "bad_path"}, status=400)

    out_headers = filter_request_headers(request.headers, route, api_key)

    async def body_streamer():
        nonlocal req_bytes
        async for chunk in request.content.iter_chunked(65536):
            req_bytes += len(chunk)
            if req_bytes > max_bytes:
                raise PayloadTooLargeError()
            yield chunk

    if cached_body is not None:
        data: Any = cached_body
    else:
        data = body_streamer() if request.can_read_body else None

    timeout = ClientTimeout(total=route.timeout_s, sock_connect=15, sock_read=route.timeout_s)

    upstream_ctx = None
    upstream_resp = None
    try:
        upstream_ctx = session.request(
            method=request.method,
            url=target_url,
            headers=out_headers,
            data=data,
            timeout=timeout,
            allow_redirects=False,
        )
        upstream_resp = await upstream_ctx.__aenter__()
    except Exception as exc:
        duration_ms = int((time.time() - start_time) * 1000)
        if is_payload_too_large(exc):
            usage_tracker.record(
                RequestRecord(
                    ts=time.time(),
                    client=lane.client,
                    port=lane.port,
                    route=route.prefix,
                    kind=route.kind,
                    method=request.method,
                    path=request.path,
                    status=413,
                    req_bytes=req_bytes,
                    resp_bytes=0,
                    duration_ms=duration_ms,
                    error="payload_too_large",
                )
            )
            log_request(lane.client, lane.port, route.prefix, 413, duration_ms, req_bytes, 0, 0, "", "payload_too_large")
            return web.json_response({"error": "payload_too_large"}, status=413)

        usage_tracker.record(
            RequestRecord(
                ts=time.time(),
                client=lane.client,
                port=lane.port,
                route=route.prefix,
                kind=route.kind,
                method=request.method,
                path=request.path,
                status=502,
                req_bytes=req_bytes,
                resp_bytes=0,
                duration_ms=duration_ms,
                error="upstream_unreachable",
            )
        )
        log_request(lane.client, lane.port, route.prefix, 502, duration_ms, req_bytes, 0, 0, "", "upstream_unreachable")
        return web.json_response({"error": "upstream_unreachable"}, status=502)

    # Headers received from upstream
    resp_headers = filter_response_headers(upstream_resp.headers)
    resp = web.StreamResponse(status=upstream_resp.status, reason=upstream_resp.reason, headers=resp_headers)

    # Chars immediately from header character-cost or x-character-count
    cost_h = upstream_resp.headers.get("character-cost") or upstream_resp.headers.get("x-character-count")
    chars = safe_int(cost_h)

    content_type = upstream_resp.content_type.lower()
    is_openai_json = (route.kind == "openai" and "application/json" in content_type)
    is_sse = ("text/event-stream" in content_type)

    openai_buffer = bytearray()
    sse_line_buffer = ""
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    resp_bytes = 0
    error_code: str | None = None
    final_status = upstream_resp.status

    try:
        await resp.prepare(request)
        async for chunk in upstream_resp.content.iter_any():
            resp_bytes += len(chunk)

            # Slow reader protection: 60s timeout per chunk write
            try:
                await asyncio.wait_for(resp.write(chunk), timeout=60.0)
            except (asyncio.TimeoutError, ConnectionResetError, ClientConnectionResetError):
                error_code = "client_disconnected"
                raise
            except asyncio.CancelledError:
                error_code = "client_disconnected"
                raise

            if is_openai_json and len(openai_buffer) < 2 * 1024 * 1024:
                openai_buffer.extend(chunk)

            if is_sse:
                sse_line_buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in sse_line_buffer:
                    line, sse_line_buffer = sse_line_buffer.split("\n", 1)
                    line = line.strip()
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str and data_str != "[DONE]":
                            try:
                                obj = json.loads(data_str)
                                if "model" in obj and not model:
                                    model = obj["model"]
                                if "usage" in obj and isinstance(obj["usage"], dict):
                                    u = obj["usage"]
                                    p_tok = u.get("prompt_tokens") if "prompt_tokens" in u else u.get("input_tokens")
                                    c_tok = u.get("completion_tokens") if "completion_tokens" in u else u.get("output_tokens")
                                    if p_tok is not None:
                                        prompt_tokens = safe_int(p_tok)
                                    if c_tok is not None:
                                        completion_tokens = safe_int(c_tok)
                            except Exception:
                                pass

        await resp.write_eof()
    except (ConnectionResetError, ClientConnectionResetError, asyncio.CancelledError):
        if not error_code:
            error_code = "client_disconnected"
        raise
    except asyncio.TimeoutError:
        error_code = "upstream_timeout"
        final_status = 504
    except (ClientPayloadError, ServerDisconnectedError, ClientOSError):
        error_code = "upstream_disconnected"
    finally:
        if upstream_ctx:
            await upstream_ctx.__aexit__(None, None, None)

        duration_ms = int((time.time() - start_time) * 1000)

        # Post-process OpenAI non-streaming JSON
        if is_openai_json and openai_buffer:
            try:
                parsed = json.loads(openai_buffer.decode("utf-8", errors="replace"))
                if "model" in parsed:
                    model = parsed["model"]
                if "usage" in parsed and isinstance(parsed["usage"], dict):
                    u = parsed["usage"]
                    p_tok = u.get("prompt_tokens") if "prompt_tokens" in u else u.get("input_tokens")
                    c_tok = u.get("completion_tokens") if "completion_tokens" in u else u.get("output_tokens")
                    prompt_tokens = safe_int(p_tok)
                    completion_tokens = safe_int(c_tok)
            except Exception:
                pass

        if not error_code and final_status >= 400:
            error_code = f"http_{final_status}"

        usage_tracker.record(
            RequestRecord(
                ts=time.time(),
                client=lane.client,
                port=lane.port,
                route=route.prefix,
                kind=route.kind,
                method=request.method,
                path=request.path,
                status=final_status,
                req_bytes=req_bytes,
                resp_bytes=resp_bytes,
                duration_ms=duration_ms,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                chars=chars,
                error=error_code,
            )
        )
        log_request(
            lane.client,
            lane.port,
            route.prefix,
            final_status,
            duration_ms,
            req_bytes,
            resp_bytes,
            chars,
            model,
            error_code,
        )

    return resp


def create_lane_handler(
    lane: Lane,
    routes: list[Route],
    usage_tracker: UsageTracker,
    session: ClientSession,
    broker_app: Any,
):
    """Create request handler for a specific lane with in-flight limits and concurrency."""

    async def handler(request: web.Request) -> web.StreamResponse:
        nonlocal lane
        lane = broker_app.lanes.get(lane.port, lane)
        start_time = time.time()
        raw_path = getattr(request, "raw_path", "")
        path = request.path

        # 1. Path validation (raw path check)
        if not validate_raw_path(raw_path):
            duration_ms = int((time.time() - start_time) * 1000)
            usage_tracker.record(
                RequestRecord(
                    ts=time.time(),
                    client=lane.client,
                    port=lane.port,
                    route="",
                    kind="",
                    method=request.method,
                    path=path,
                    status=400,
                    req_bytes=0,
                    resp_bytes=0,
                    duration_ms=duration_ms,
                    error="bad_path",
                )
            )
            log_request(lane.client, lane.port, "", 400, duration_ms, 0, 0, 0, "", "bad_path")
            return web.json_response({"error": "bad_path"}, status=400)

        # 2. WebSocket check
        if "Upgrade" in request.headers or "upgrade" in request.headers.get("Connection", "").lower():
            duration_ms = int((time.time() - start_time) * 1000)
            usage_tracker.record(
                RequestRecord(
                    ts=time.time(),
                    client=lane.client,
                    port=lane.port,
                    route="",
                    kind="",
                    method=request.method,
                    path=path,
                    status=501,
                    req_bytes=0,
                    resp_bytes=0,
                    duration_ms=duration_ms,
                    error="not_implemented",
                )
            )
            log_request(lane.client, lane.port, "", 501, duration_ms, 0, 0, 0, "", "not_implemented")
            return web.json_response({"error": "not_implemented"}, status=501)

        # 3. RPM limit check (checked FIRST before route match to throttle junk requests)
        if lane.limits.rpm is not None:
            current_rpm = usage_tracker.rpm_count(lane.port, start_time) + broker_app.get_in_flight(lane.port)
            if current_rpm >= lane.limits.rpm:
                duration_ms = int((time.time() - start_time) * 1000)
                usage_tracker.record(
                    RequestRecord(
                        ts=time.time(),
                        client=lane.client,
                        port=lane.port,
                        route="",
                        kind="",
                        method=request.method,
                        path=path,
                        status=429,
                        req_bytes=0,
                        resp_bytes=0,
                        duration_ms=duration_ms,
                        error="lane_limit",
                    )
                )
                log_request(lane.client, lane.port, "", 429, duration_ms, 0, 0, 0, "", "lane_limit")
                return web.json_response(
                    {
                        "error": "lane_limit",
                        "limit": "rpm",
                        "used": current_rpm,
                        "limit_value": lane.limits.rpm,
                    },
                    status=429,
                )

        # 4. Match route
        route, subpath = find_matching_route(path, routes)
        if route is None:
            duration_ms = int((time.time() - start_time) * 1000)
            usage_tracker.record(
                RequestRecord(
                    ts=time.time(),
                    client=lane.client,
                    port=lane.port,
                    route="",
                    kind="",
                    method=request.method,
                    path=path,
                    status=404,
                    req_bytes=0,
                    resp_bytes=0,
                    duration_ms=duration_ms,
                    error="unknown_route",
                )
            )
            log_request(lane.client, lane.port, "", 404, duration_ms, 0, 0, 0, "", "unknown_route")
            return web.json_response({"error": "unknown_route"}, status=404)

        # 5. Check route allowlist
        if not route.is_allowed(request.method, subpath):
            duration_ms = int((time.time() - start_time) * 1000)
            usage_tracker.record(
                RequestRecord(
                    ts=time.time(),
                    client=lane.client,
                    port=lane.port,
                    route=route.prefix,
                    kind=route.kind,
                    method=request.method,
                    path=path,
                    status=403,
                    req_bytes=0,
                    resp_bytes=0,
                    duration_ms=duration_ms,
                    error="path_not_allowed",
                )
            )
            log_request(lane.client, lane.port, route.prefix, 403, duration_ms, 0, 0, 0, "", "path_not_allowed")
            return web.json_response({"error": "path_not_allowed"}, status=403)

        # 6. Check route API key
        api_key = route.get_api_key()
        if not api_key:
            duration_ms = int((time.time() - start_time) * 1000)
            usage_tracker.record(
                RequestRecord(
                    ts=time.time(),
                    client=lane.client,
                    port=lane.port,
                    route=route.prefix,
                    kind=route.kind,
                    method=request.method,
                    path=path,
                    status=503,
                    req_bytes=0,
                    resp_bytes=0,
                    duration_ms=duration_ms,
                    error="route_unavailable",
                )
            )
            log_request(lane.client, lane.port, route.prefix, 503, duration_ms, 0, 0, 0, "", "route_unavailable")
            return web.json_response({"error": "route_unavailable"}, status=503)

        # 7. Concurrency limit check
        sem = broker_app.get_semaphore(lane.port, lane.limits.concurrency)
        if sem.locked():
            duration_ms = int((time.time() - start_time) * 1000)
            usage_tracker.record(
                RequestRecord(
                    ts=time.time(),
                    client=lane.client,
                    port=lane.port,
                    route=route.prefix,
                    kind=route.kind,
                    method=request.method,
                    path=path,
                    status=429,
                    req_bytes=0,
                    resp_bytes=0,
                    duration_ms=duration_ms,
                    error="lane_limit",
                )
            )
            log_request(lane.client, lane.port, route.prefix, 429, duration_ms, 0, 0, 0, "", "lane_limit")
            return web.json_response(
                {
                    "error": "lane_limit",
                    "limit": "concurrency",
                    "used": lane.limits.concurrency,
                    "limit_value": lane.limits.concurrency,
                },
                status=429,
            )

        # 8. Elevenlabs in-flight reservation and monthly limits
        cached_body: bytes | None = None
        reserved_chars = 0
        current_month = time.strftime("%Y-%m", time.gmtime(start_time))

        if route.kind == "elevenlabs" and lane.limits.elevenlabs_chars_month is not None:
            content_type = request.content_type.lower()
            if request.method.upper() == "POST" and "application/json" in content_type:
                if request.content_length and request.content_length > 256 * 1024:
                    return web.json_response({"error": "payload_too_large"}, status=413)
                raw_body = await request.read()
                if len(raw_body) > 256 * 1024:
                    return web.json_response({"error": "payload_too_large"}, status=413)
                cached_body = raw_body
                try:
                    body_json = json.loads(raw_body.decode("utf-8"))
                    text_val = body_json.get("text", "")
                    reserved_chars = len(str(text_val))
                except Exception:
                    reserved_chars = 0

            used_chars = usage_tracker.month_totals(lane.client, "elevenlabs", current_month).chars
            reserved_inflight = broker_app.get_reserved_chars(lane.client)
            if used_chars + reserved_inflight + reserved_chars > lane.limits.elevenlabs_chars_month:
                duration_ms = int((time.time() - start_time) * 1000)
                usage_tracker.record(
                    RequestRecord(
                        ts=time.time(),
                        client=lane.client,
                        port=lane.port,
                        route=route.prefix,
                        kind=route.kind,
                        method=request.method,
                        path=path,
                        status=429,
                        req_bytes=len(cached_body) if cached_body else 0,
                        resp_bytes=0,
                        duration_ms=duration_ms,
                        error="lane_limit",
                    )
                )
                log_request(lane.client, lane.port, route.prefix, 429, duration_ms, 0, 0, 0, "", "lane_limit")
                return web.json_response(
                    {
                        "error": "lane_limit",
                        "limit": "elevenlabs_chars_month",
                        "used": used_chars + reserved_inflight,
                        "limit_value": lane.limits.elevenlabs_chars_month,
                    },
                    status=429,
                )

        # 9. Track in-flight and execute upstream call under semaphore
        broker_app.inc_in_flight(lane.port)
        if reserved_chars > 0:
            broker_app.add_reserved_chars(lane.client, reserved_chars)

        try:
            async with sem:
                if route.kind == "stt" and (subpath == "/audio/transcriptions" or path.endswith("/audio/transcriptions")):
                    return await handle_stt_request(
                        request, route, subpath, api_key, lane, usage_tracker, session, start_time
                    )
                return await handle_generic_streaming(
                    request, route, subpath, api_key, lane, usage_tracker, session, start_time, cached_body=cached_body
                )
        finally:
            broker_app.dec_in_flight(lane.port)
            if reserved_chars > 0:
                broker_app.remove_reserved_chars(lane.client, reserved_chars)

    return handler
