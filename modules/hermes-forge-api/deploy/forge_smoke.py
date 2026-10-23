"""Prod smoke for the Forge API after deploy: signs a Mini App initData for the admin with the manager bot token
(read by control.py's own loader, never printed) and walks the read-only endpoints through the API socket/port.
Usage: python3 -I forge_smoke.py [unix:/run/.../api.sock | tcp:127.0.0.1:8650] [profile]
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import importlib.util
import json
import socket
import sys
import time
import urllib.parse
from pathlib import Path

CONTROL = Path("/opt/proai-hermes-forge-api/control.py")
spec = importlib.util.spec_from_file_location("forge_control_smoke", CONTROL)
control = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(control)

target = sys.argv[1] if len(sys.argv) > 1 else "unix:/run/proai-hermes-forge-api/api.sock"
profile = sys.argv[2] if len(sys.argv) > 2 else "pavel"
ADMIN_ID = 450206471


class UnixConn(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost", timeout=30)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect(self._path)


def conn() -> http.client.HTTPConnection:
    if target.startswith("unix:"):
        return UnixConn(target[5:])
    host, port = target[4:].rsplit(":", 1)
    return http.client.HTTPConnection(host, int(port), timeout=30)


def init_data() -> str:
    token = control._manager_token()
    now = int(time.time())
    fields = {"auth_date": str(now), "user": json.dumps({"id": ADMIN_ID, "first_name": "Smoke"}, separators=(",", ":"))}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


def call(method: str, path: str, body: dict | None = None, session: str | None = None) -> tuple[int, dict]:
    c = conn()
    headers = {"Content-Type": "application/json"}
    if session:
        headers["Authorization"] = "Bearer " + session
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    r = c.getresponse()
    raw = r.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        payload = {"raw": raw[:200].decode("utf-8", "replace")}
    c.close()
    return r.status, payload


def show(label: str, status: int, payload: dict, ok_when: int = 200) -> bool:
    ok = status == ok_when and payload.get("ok") is True
    summary = payload.get("result") if ok else payload.get("error") or payload
    text = json.dumps(summary, ensure_ascii=False)[:160] if not isinstance(summary, str) else summary
    print(("OK  " if ok else "BAD ") + f"{label}: {status} {text}")
    return ok


results = []
st, p = call("GET", "/healthz")
results.append(show("healthz", st, p))
st, p = call("POST", "/v1/auth/telegram", {"init_data": init_data()})
results.append(show("auth", st, {"ok": p.get("ok"), "result": {"user": (p.get("result") or {}).get("user")}}))
session = (p.get("result") or {}).get("session")
if session:
    st, p = call("GET", "/v1/hermes", session=session)
    results.append(show("list_hermes", st, {"ok": p.get("ok"), "result": [i.get("profile") for i in (p.get("result") or {}).get("items", [])]}))
    for suffix in ("", "/health", "/capabilities", "/members", "/invites", "/connections", "/secrets"):
        st, p = call("GET", f"/v1/hermes/{profile}{suffix}", session=session)
        res = p.get("result")
        brief = {"ok": p.get("ok"), "result": (list(res.keys())[:8] if isinstance(res, dict) else res)}
        results.append(show(f"GET {profile}{suffix}", st, brief if p.get("ok") else p))
    st, p = call("DELETE", f"/v1/hermes/{profile}/secrets/NOT_ALLOWED", {}, session=session)
    results.append(show("delete_secret not allowed -> 400", st, p, ok_when=400) or (st == 400 and p.get("error") == "secret_not_allowed"))
    st, p = call("POST", "/v1/auth/logout", {}, session=session)
    results.append(show("logout", st, p))
    st, p = call("GET", "/v1/hermes", session=session)
    results.append(st == 401)
    print(("OK  " if st == 401 else "BAD ") + f"session after logout: {st}")
print("SMOKE", "PASS" if all(results) else "FAIL", f"({sum(1 for r in results if r)}/{len(results)})")
sys.exit(0 if all(results) else 1)
