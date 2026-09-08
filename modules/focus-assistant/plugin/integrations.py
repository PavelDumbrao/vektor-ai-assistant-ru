"""Sanitized connection-status helpers for Maton-backed integrations."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


CONNECTIONS_URL = "https://api.maton.ai/connections"


def available() -> bool:
    return bool(os.environ.get("MCP_MATON_API_KEY", "").strip())


def connection_status(args: dict[str, Any]) -> dict[str, Any]:
    key = os.environ.get("MCP_MATON_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Maton key is not configured")
    requested = args.get("apps") or []
    if isinstance(requested, str):
        requested = [requested]
    requested_apps = {str(app).strip().lower() for app in requested if str(app).strip()}
    query = urllib.parse.urlencode({"limit": 100})
    request = urllib.request.Request(
        f"{CONNECTIONS_URL}?{query}",
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "Vektor-Focus-Assistant/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Maton returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Maton is temporarily unavailable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected Maton response")
    raw_items = payload.get("data") or payload.get("connections") or payload.get("items") or []
    services: dict[str, str] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        app = item.get("app") or item.get("application") or item.get("app_name") or item.get("slug")
        if isinstance(app, dict):
            app = app.get("slug") or app.get("name") or app.get("id")
        app_name = str(app or "").strip().lower()
        if not app_name or (requested_apps and app_name not in requested_apps):
            continue
        services[app_name] = str(item.get("status") or item.get("state") or "UNKNOWN").upper()
    if requested_apps:
        for app in requested_apps:
            services.setdefault(app, "NOT_FOUND")
    return {
        "services": [{"app": app, "status": services[app]} for app in sorted(services)],
        "count": len(services),
    }


def json_result(args: dict[str, Any]) -> str:
    try:
        return json.dumps({"ok": True, **connection_status(args)}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps(
            {"ok": False, "error": type(exc).__name__, "message": str(exc)},
            ensure_ascii=False,
        )


def sanitize_connection_tool_result(
    tool_name: str = "",
    result: Any = None,
    **_: Any,
) -> str | None:
    if tool_name not in {
        "mcp__maton__list_connections",
        "mcp__maton__get_connection",
    }:
        return None
    if not isinstance(result, str):
        return json.dumps({"result": "{\"error\":\"connection metadata was not textual\"}"})
    try:
        outer = json.loads(result)
        inner = outer.get("result", outer) if isinstance(outer, dict) else outer
        if isinstance(inner, str):
            inner = json.loads(inner)
        if not isinstance(inner, dict):
            raise ValueError("invalid connection metadata")
        raw_connections = inner.get("connections")
        if isinstance(raw_connections, list):
            connections = []
            for item in raw_connections:
                if not isinstance(item, dict):
                    continue
                connections.append(
                    {
                        "app": item.get("app"),
                        "method": item.get("method"),
                        "status": item.get("status"),
                    }
                )
            safe = {
                "connections": connections,
                "cursor": inner.get("cursor"),
                "total": inner.get("total", len(connections)),
            }
        else:
            safe = {
                "app": inner.get("app"),
                "method": inner.get("method"),
                "status": inner.get("status"),
            }
        return json.dumps({"result": json.dumps(safe, ensure_ascii=False)}, ensure_ascii=False)
    except Exception:
        return json.dumps(
            {"result": "{\"error\":\"connection metadata could not be safely parsed\"}"},
            ensure_ascii=False,
        )

