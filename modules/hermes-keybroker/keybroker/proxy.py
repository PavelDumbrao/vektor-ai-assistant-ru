"""Streaming reverse-proxy handler for keybroker."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from keybroker.config import Lane, Route
from keybroker.usage import RequestRecord, UsageTracker

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


def is_bad_path(path: str, raw_path: str = "") -> bool:
    """Check for traversal (..), dot (.), empty segment (//), or invalid characters (\\, %00)."""
    if "\\" in path or "\\" in raw_path:
        return True
    if "%00" in path.lower() or "%00" in raw_path.lower() or "\x00" in path:
        return True
    if "//" in path or "//" in raw_path:
        return True
    for seg in path.split("/"):
        if seg == ".." or seg == ".":
            return True
    if raw_path:
        raw_norm = raw_path.split("?")[0]
        for seg in raw_norm.split("/"):
            if seg == ".." or seg == ".":
                return True
    return False


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


def build_upstream_url(route: Route, subpath: str, query_string: str) -> str:
    """Build upstream URL without duplicate slashes."""
    upstream_base = route.upstream.rstrip("/")
    if subpath:
        if not subpath.startswith("/"):
            subpath = "/" + subpath
        url = upstream_base + subpath
    else:
        url = upstream_base

    if query_string:
        url = f"{url}?{query_string}"
    return url


def filter_request_headers(
    headers: Any,
    route: Route,
    api_key: str,
) -> dict[str, str]:
    """Filter hop-by-hop and auth headers, injecting route's configured auth."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower in HOP_BY_HOP_HEADERS or k_lower.startswith("proxy-"):
            continue
        if k_lower in CLIENT_AUTH_HEADERS or k_lower in ("host", "content-length"):
            continue
        out[k] = v

    # Add route's configured auth header
    auth_header = route.auth.header
    auth_value = route.auth.format.format(key=api_key)
    out[auth_header] = auth_value
    # Always take uncompressed bodies from upstream: the shared session does not decompress, every client
    # accepts identity, and usage extraction (JSON/SSE) must see plain text.
    out["Accept-Encoding"] = "identity"
    return out


def filter_response_headers(headers: Any) -> dict[str, str]:
    """Filter hop-by-hop headers from upstream response."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower in HOP_BY_HOP_HEADERS or k_lower.startswith("proxy-"):
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
) -> None:
    """Log single request line without sensitive data."""
    access_logger.info(
        "client=%s port=%d route=%s status=%d ms=%d req=%d resp=%d chars=%d model=%s",
        client,
        port,
        route,
        status,
        duration_ms,
        req_bytes,
        resp_bytes,
        chars or 0,
        model or "",
    )


class PayloadTooLargeError(Exception):
    """Raised when request body exceeds max_body_mb."""


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
    """Handle kind: stt requests with OpenRouter model ladder."""
    max_bytes = route.max_body_mb * 1024 * 1024
    content_type = request.content_type.lower()
    req_bytes = 0

    fields: dict[str, str] = {}
    file_bytes = b""
    filename = ""

    if "multipart" in content_type:
        try:
            reader = await request.multipart()
            while True:
                part = await reader.next()
                if part is None:
                    break
                part_name = part.name
                if part_name == "file":
                    filename = part.filename or "audio.ogg"
                    chunk_bytes = await part.read()
                    req_bytes += len(chunk_bytes)
                    if req_bytes > max_bytes:
                        raise PayloadTooLargeError()
                    file_bytes = chunk_bytes
                else:
                    text_val = await part.text()
                    req_bytes += len(text_val.encode("utf-8"))
                    if req_bytes > max_bytes:
                        raise PayloadTooLargeError()
                    fields[part_name] = text_val
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
            log_request(lane.client, lane.port, route.prefix, 413, duration_ms, req_bytes, 0, 0, "")
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
        if req_bytes > max_bytes:
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
            log_request(lane.client, lane.port, route.prefix, 413, duration_ms, req_bytes, 0, 0, "")
            return web.json_response({"error": "payload_too_large"}, status=413)

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)

        if "input_audio" not in payload:
            # Fall back to generic handling
            return await handle_generic_streaming(
                request, route, subpath, api_key, lane, usage_tracker, session, start_time
            )

        req_model = payload.get("model", "")
        response_format = request.query.get("response_format", "json")
    else:
        # Other content types on stt endpoint -> generic
        return await handle_generic_streaming(
            request, route, subpath, api_key, lane, usage_tracker, session, start_time
        )

    # Build model trial list
    ladder = list(route.ladder)
    if req_model and req_model in ladder:
        models_to_try = [req_model] + [m for m in ladder if m != req_model]
    elif ladder:
        models_to_try = ladder
    elif req_model:
        models_to_try = [req_model]
    else:
        models_to_try = []

    if not models_to_try:
        models_to_try = ["openai/whisper-large-v3-turbo"]

    target_url = build_upstream_url(route, subpath, request.query_string)
    auth_header = route.auth.header
    auth_value = route.auth.format.format(key=api_key)
    req_headers = {
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",  # we parse the JSON ourselves; the shared session does not decompress
        auth_header: auth_value,
    }

    last_error = ""
    last_status = 502
    chosen_model = ""
    success_text = None

    timeout = ClientTimeout(
        total=route.timeout_s, sock_connect=15, sock_read=route.timeout_s
    )

    for current_model in models_to_try:
        payload["model"] = current_model
        payload_bytes = json.dumps(payload).encode("utf-8")
        try:
            async with session.post(
                target_url,
                headers=req_headers,
                data=payload_bytes,
                timeout=timeout,
            ) as up_resp:
                if up_resp.status == 429 or up_resp.status >= 500:
                    last_status = up_resp.status
                    last_error = f"http_{up_resp.status}"
                    continue

                if up_resp.status == 200 or 200 <= up_resp.status < 300:
                    try:
                        res_json = await up_resp.json()
                        text_val = res_json.get("text")
                        if text_val is None or str(text_val).strip() == "":
                            # Empty text -> fallback to next model
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
                    # Other status like 400, 401
                    last_status = up_resp.status
                    last_error = f"http_{up_resp.status}"
                    break
        except (ClientError, asyncio.TimeoutError) as exc:
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

    # If all models failed
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
) -> web.StreamResponse:
    """Streaming reverse-proxy handler for HTTP requests."""
    max_bytes = route.max_body_mb * 1024 * 1024
    req_bytes = 0

    # Early check on content-length
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
        log_request(lane.client, lane.port, route.prefix, 413, duration_ms, 0, 0, 0, "")
        return web.json_response({"error": "payload_too_large"}, status=413)

    target_url = build_upstream_url(route, subpath, request.query_string)
    out_headers = filter_request_headers(request.headers, route, api_key)

    async def body_streamer():
        nonlocal req_bytes
        async for chunk in request.content.iter_chunked(65536):
            req_bytes += len(chunk)
            if req_bytes > max_bytes:
                raise PayloadTooLargeError()
            yield chunk

    data = body_streamer() if request.can_read_body else None
    timeout = ClientTimeout(
        total=route.timeout_s, sock_connect=15, sock_read=route.timeout_s
    )

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
        log_request(lane.client, lane.port, route.prefix, 413, duration_ms, req_bytes, 0, 0, "")
        return web.json_response({"error": "payload_too_large"}, status=413)
    except (asyncio.TimeoutError, ClientError) as exc:
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
                status=502,
                req_bytes=req_bytes,
                resp_bytes=0,
                duration_ms=duration_ms,
                error="upstream_unreachable",
            )
        )
        log_request(lane.client, lane.port, route.prefix, 502, duration_ms, req_bytes, 0, 0, "")
        return web.json_response({"error": "upstream_unreachable"}, status=502)

    resp_headers = filter_response_headers(upstream_resp.headers)
    resp = web.StreamResponse(status=upstream_resp.status, reason=upstream_resp.reason, headers=resp_headers)

    chars: int | None = None
    # ElevenLabs reports billed characters in `character-cost`; keep the older header name as a fallback.
    char_count_h = upstream_resp.headers.get("character-cost") or upstream_resp.headers.get("x-character-count")
    if char_count_h:
        try:
            chars = int(char_count_h)
        except (ValueError, TypeError):
            pass

    content_type = upstream_resp.content_type.lower()
    is_openai_json = (route.kind == "openai" and "application/json" in content_type)
    is_sse = ("text/event-stream" in content_type)

    openai_buffer = bytearray()
    sse_line_buffer = ""
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    resp_bytes = 0
    read_timeout_occurred = False

    try:
        await resp.prepare(request)
        async for chunk in upstream_resp.content.iter_any():
            resp_bytes += len(chunk)
            await resp.write(chunk)

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
                                    if "prompt_tokens" in u:
                                        prompt_tokens = u["prompt_tokens"]
                                    if "completion_tokens" in u:
                                        completion_tokens = u["completion_tokens"]
                            except Exception:
                                pass

        await resp.write_eof()
    except asyncio.TimeoutError:
        read_timeout_occurred = True
    finally:
        await upstream_ctx.__aexit__(None, None, None)

    duration_ms = int((time.time() - start_time) * 1000)

    # Post-process openai non-streaming JSON
    if is_openai_json and openai_buffer:
        try:
            parsed = json.loads(openai_buffer.decode("utf-8", errors="replace"))
            if "model" in parsed:
                model = parsed["model"]
            if "usage" in parsed and isinstance(parsed["usage"], dict):
                prompt_tokens = parsed["usage"].get("prompt_tokens")
                completion_tokens = parsed["usage"].get("completion_tokens")
        except Exception:
            pass

    status = 504 if read_timeout_occurred else upstream_resp.status
    error_code = "upstream_timeout" if read_timeout_occurred else (
        f"http_{status}" if status >= 400 else None
    )

    usage_tracker.record(
        RequestRecord(
            ts=time.time(),
            client=lane.client,
            port=lane.port,
            route=route.prefix,
            kind=route.kind,
            method=request.method,
            path=request.path,
            status=status,
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
        status,
        duration_ms,
        req_bytes,
        resp_bytes,
        chars,
        model,
    )

    return resp


def create_lane_handler(
    lane: Lane,
    routes: list[Route],
    usage_tracker: UsageTracker,
    session: ClientSession,
):
    """Create request handler for a specific lane."""

    async def handler(request: web.Request) -> web.StreamResponse:
        start_time = time.time()
        path = request.path
        raw_path = getattr(request, "raw_path", "")

        # 0. Path normalization check
        if is_bad_path(path, raw_path):
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
            log_request(lane.client, lane.port, "", 400, duration_ms, 0, 0, 0, "")
            return web.json_response({"error": "bad_path"}, status=400)

        # 1. WebSocket check
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
            log_request(lane.client, lane.port, "", 501, duration_ms, 0, 0, 0, "")
            return web.json_response({"error": "not_implemented"}, status=501)

        # 2. Match route
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
            log_request(lane.client, lane.port, "", 404, duration_ms, 0, 0, 0, "")
            return web.json_response({"error": "unknown_route"}, status=404)

        # 3. Check route allowlist
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
            log_request(lane.client, lane.port, route.prefix, 403, duration_ms, 0, 0, 0, "")
            return web.json_response({"error": "path_not_allowed"}, status=403)

        # 4. Check route API key
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
            log_request(lane.client, lane.port, route.prefix, 503, duration_ms, 0, 0, 0, "")
            return web.json_response({"error": "route_unavailable"}, status=503)

        # 4. Check limits before calling upstream
        # 4a. RPM limit
        if lane.limits.rpm is not None:
            current_rpm = usage_tracker.rpm_count(lane.port, start_time)
            if current_rpm >= lane.limits.rpm:
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
                log_request(lane.client, lane.port, route.prefix, 429, duration_ms, 0, 0, 0, "")
                return web.json_response(
                    {
                        "error": "lane_limit",
                        "limit": "rpm",
                        "used": current_rpm,
                        "limit_value": lane.limits.rpm,
                    },
                    status=429,
                )

        # 4b. Elevenlabs monthly character limit
        if route.kind == "elevenlabs" and lane.limits.elevenlabs_chars_month is not None:
            current_month = time.strftime("%Y-%m", time.gmtime(start_time))
            used_chars = usage_tracker.month_totals(lane.client, "elevenlabs", current_month).chars
            if used_chars >= lane.limits.elevenlabs_chars_month:
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
                log_request(lane.client, lane.port, route.prefix, 429, duration_ms, 0, 0, 0, "")
                return web.json_response(
                    {
                        "error": "lane_limit",
                        "limit": "elevenlabs_chars_month",
                        "used": used_chars,
                        "limit_value": lane.limits.elevenlabs_chars_month,
                    },
                    status=429,
                )

        # 5. Route request to STT handler or Generic streaming
        if route.kind == "stt" and (subpath == "/audio/transcriptions" or path.endswith("/audio/transcriptions")):
            return await handle_stt_request(
                request, route, subpath, api_key, lane, usage_tracker, session, start_time
            )

        return await handle_generic_streaming(
            request, route, subpath, api_key, lane, usage_tracker, session, start_time
        )

    return handler
