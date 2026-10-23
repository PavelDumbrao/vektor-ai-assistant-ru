"""Configuration loading and validation for keybroker."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("keybroker.config")

DEFAULT_LANES_PATH = "/etc/proai-keybroker/lanes.json"
DEFAULT_ROUTES_PATH = "/etc/proai-keybroker/routes.json"
DEFAULT_STATE_DIR = "/var/lib/proai-keybroker"
DEFAULT_BIND = "127.0.0.1"
DEFAULT_ADMIN_PORT = 21000
DEFAULT_LOG_LEVEL = "INFO"


@dataclass
class LaneLimits:
    elevenlabs_chars_month: int | None = None
    rpm: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> LaneLimits:
        if not data:
            return cls()
        elevenlabs_chars_month = data.get("elevenlabs_chars_month")
        if elevenlabs_chars_month is not None:
            elevenlabs_chars_month = int(elevenlabs_chars_month)
            if elevenlabs_chars_month < 0:
                raise ValueError("elevenlabs_chars_month limit must be non-negative")

        rpm = data.get("rpm")
        if rpm is not None:
            rpm = int(rpm)
            if rpm < 0:
                raise ValueError("rpm limit must be non-negative")

        return cls(elevenlabs_chars_month=elevenlabs_chars_month, rpm=rpm)


@dataclass
class Lane:
    client: str
    uid: int
    port: int
    limits: LaneLimits

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Lane:
        client = data.get("client")
        if not client or not isinstance(client, str):
            raise ValueError(f"Invalid or missing client name in lane: {data}")

        uid = data.get("uid", 0)
        try:
            uid = int(uid)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid uid for lane {client}: {uid}")

        port = data.get("port")
        if port is None:
            raise ValueError(f"Missing port for lane {client}")
        try:
            port = int(port)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid port for lane {client}: {port}")

        if not (1024 <= port <= 65535):
            raise ValueError(f"Port {port} for lane {client} must be in range 1024..65535")

        limits = LaneLimits.from_dict(data.get("limits"))
        return cls(client=client.strip(), uid=uid, port=port, limits=limits)


@dataclass
class RouteAllowRule:
    path: str
    pattern: re.Pattern[str]
    methods: set[str] | None = None  # None means any HTTP method is allowed


@dataclass
class RouteAuth:
    header: str
    format: str
    key_env: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RouteAuth:
        header = data.get("header")
        fmt = data.get("format")
        key_env = data.get("key_env")
        if not header or not isinstance(header, str):
            raise ValueError(f"Invalid auth.header in route auth: {data}")
        if not fmt or not isinstance(fmt, str):
            raise ValueError(f"Invalid auth.format in route auth: {data}")
        if not key_env or not isinstance(key_env, str):
            raise ValueError(f"Invalid auth.key_env in route auth: {data}")
        return cls(header=header.strip(), format=fmt, key_env=key_env.strip())


@dataclass
class Route:
    prefix: str
    upstream: str
    kind: str
    auth: RouteAuth
    timeout_s: float
    max_body_mb: int = 64
    ladder: list[str] = field(default_factory=list)
    allow: list[RouteAllowRule] | None = None

    def get_api_key(self) -> str | None:
        key = os.environ.get(self.auth.key_env)
        if key:
            stripped = key.strip()
            return stripped if stripped else None
        return None

    def is_allowed(self, method: str, subpath: str) -> bool:
        if self.allow is None:
            return True
        check_path = subpath if subpath else "/"
        if not check_path.startswith("/"):
            check_path = "/" + check_path
        method_upper = method.upper()
        for rule in self.allow:
            if rule.methods is not None and method_upper not in rule.methods:
                continue
            if rule.pattern.search(check_path):
                return True
        return False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Route:
        prefix = data.get("prefix")
        if not prefix or not isinstance(prefix, str) or not prefix.startswith("/"):
            raise ValueError(f"Invalid route prefix (must start with '/'): {prefix}")
        # Normalize trailing slash if not root
        if len(prefix) > 1 and prefix.endswith("/"):
            prefix = prefix.rstrip("/")

        upstream = data.get("upstream")
        if not upstream or not isinstance(upstream, str) or not (
            upstream.startswith("http://") or upstream.startswith("https://")
        ):
            raise ValueError(f"Invalid route upstream URL: {upstream}")

        kind = data.get("kind", "generic")
        valid_kinds = {"openai", "stt", "elevenlabs", "generic", "mcp"}
        if kind not in valid_kinds:
            raise ValueError(f"Unknown route kind '{kind}', expected one of {valid_kinds}")

        auth_raw = data.get("auth")
        if not isinstance(auth_raw, dict):
            raise ValueError(f"Missing or invalid auth in route {prefix}")
        auth = RouteAuth.from_dict(auth_raw)

        # Default timeouts
        timeout_s = data.get("timeout_s")
        if timeout_s is None:
            if kind in {"openai", "mcp"}:
                timeout_s = 600.0
            else:
                timeout_s = 180.0
        else:
            timeout_s = float(timeout_s)

        max_body_mb = int(data.get("max_body_mb", 64))
        ladder = [str(m).strip() for m in data.get("ladder", []) if str(m).strip()]

        allow_raw = data.get("allow")
        allow_rules: list[RouteAllowRule] | None = None
        if allow_raw is not None:
            if not isinstance(allow_raw, list):
                raise ValueError(f"Route '{prefix}': 'allow' must be a list")
            allow_rules = []
            for idx, rule in enumerate(allow_raw):
                if not isinstance(rule, dict):
                    raise ValueError(f"Route '{prefix}': allow rule #{idx} must be a dict")
                p = rule.get("path")
                if not p or not isinstance(p, str):
                    raise ValueError(f"Route '{prefix}': allow rule #{idx} missing or invalid 'path'")
                try:
                    compiled_pattern = re.compile(p)
                except re.error as e:
                    raise ValueError(f"Route '{prefix}': invalid regex in allow rule '{p}': {e}")

                methods_raw = rule.get("methods")
                methods_set: set[str] | None = None
                if methods_raw is not None:
                    if isinstance(methods_raw, str):
                        methods_set = {m.strip().upper() for m in methods_raw.split("|") if m.strip()}
                    elif isinstance(methods_raw, list):
                        methods_set = {str(m).strip().upper() for m in methods_raw if str(m).strip()}
                    else:
                        raise ValueError(f"Route '{prefix}': allow rule 'methods' must be list or string")
                allow_rules.append(RouteAllowRule(path=p, pattern=compiled_pattern, methods=methods_set))

        return cls(
            prefix=prefix,
            upstream=upstream.rstrip("/"),
            kind=kind,
            auth=auth,
            timeout_s=timeout_s,
            max_body_mb=max_body_mb,
            ladder=ladder,
            allow=allow_rules,
        )


def load_lanes(path: str) -> list[Lane]:
    """Load and validate lanes from JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    lanes_raw = data.get("lanes")
    if not isinstance(lanes_raw, list):
        raise ValueError(f"Expected 'lanes' list in {path}")

    lanes: list[Lane] = []
    seen_ports: set[int] = set()

    for item in lanes_raw:
        lane = Lane.from_dict(item)
        if lane.port in seen_ports:
            raise ValueError(f"Duplicate port {lane.port} in lanes config")
        seen_ports.add(lane.port)
        lanes.append(lane)

    return lanes


def load_routes(path: str) -> list[Route]:
    """Load and validate routes from JSON file, sorted by longest prefix."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    routes_raw = data.get("routes")
    if not isinstance(routes_raw, list):
        raise ValueError(f"Expected 'routes' list in {path}")

    routes: list[Route] = []
    for item in routes_raw:
        route = Route.from_dict(item)
        routes.append(route)

    # Sort descending by prefix length for longest-prefix match
    routes.sort(key=lambda r: len(r.prefix), reverse=True)

    # Log warnings for missing keys or missing allow list at startup
    for r in routes:
        if not r.get_api_key():
            logger.warning(
                "Route '%s' (kind=%s) key_env '%s' is not set in environment",
                r.prefix,
                r.kind,
                r.auth.key_env,
            )
        if r.allow is None:
            logger.warning("Route %s has no allow list", r.prefix)

    return routes


@dataclass
class BrokerConfig:
    bind: str
    admin_port: int
    lanes_path: str
    routes_path: str
    state_dir: str
    log_level: str

    @classmethod
    def from_env(cls) -> BrokerConfig:
        bind = os.environ.get("KEYBROKER_BIND", DEFAULT_BIND).strip()
        admin_port = int(os.environ.get("KEYBROKER_ADMIN_PORT", DEFAULT_ADMIN_PORT))
        lanes_path = os.environ.get("KEYBROKER_LANES", DEFAULT_LANES_PATH).strip()
        routes_path = os.environ.get("KEYBROKER_ROUTES", DEFAULT_ROUTES_PATH).strip()
        state_dir = os.environ.get("KEYBROKER_STATE_DIR", DEFAULT_STATE_DIR).strip()
        log_level = os.environ.get("KEYBROKER_LOG_LEVEL", DEFAULT_LOG_LEVEL).strip().upper()
        return cls(
            bind=bind,
            admin_port=admin_port,
            lanes_path=lanes_path,
            routes_path=routes_path,
            state_dir=state_dir,
            log_level=log_level,
        )
