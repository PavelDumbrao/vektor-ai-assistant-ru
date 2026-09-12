from __future__ import annotations

import json
import os
import pwd
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BROKER_URL = os.getenv("HERMES_VIDEO_CRITIC_BROKER", "http://127.0.0.1:8778").rstrip("/")
MAX_RESPONSE = 2 * 1024 * 1024


class CriticError(RuntimeError):
    pass


def _home() -> Path:
    raw=os.getenv("HERMES_HOME")
    return Path(raw).expanduser().resolve() if raw else (Path.home()/".hermes").resolve()


def _profile() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name.lower()
def _token() -> str:
    path=_home()/"video_editor"/"critic_token"
    try: info=path.lstat()
    except OSError: return ""
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or (info.st_mode & 0o077) or info.st_size>512:
        return ""
    value=path.read_text(encoding="utf-8").strip()
    return value if 32<=len(value)<=256 and not any(ch.isspace() for ch in value) else ""


def _headers() -> dict[str,str]:
    token=_token()
    if not token: raise CriticError("critic_token_missing")
    return {"X-Vektor-Profile":_profile(),"X-Vektor-Critic-Token":token,"Content-Type":"application/json"}


def health() -> dict[str,Any]:
    try:
        req=urllib.request.Request(BROKER_URL+"/v1/health",headers=_headers())
        with urllib.request.urlopen(req,timeout=3) as response:
            raw=response.read(MAX_RESPONSE+1)
        if len(raw)>MAX_RESPONSE: raise CriticError("critic_response_too_large")
        data=json.loads(raw.decode("utf-8"))
        return data if isinstance(data,dict) else {"ok":False,"error":"critic_health_invalid"}
    except Exception as exc:
        return {"ok":False,"enabled":False,"stages":[],"error":type(exc).__name__}
def critique(path: Path, stage: str, artifact_sha256: str) -> dict[str,Any]:
    status=health()
    if not status.get("ok"): raise CriticError("critic_broker_unavailable")
    if not status.get("enabled") or stage not in (status.get("stages") or []):
        raise CriticError("critic_disabled")
    payload=json.dumps({"path":str(path),"stage":stage,"artifact_sha256":artifact_sha256},separators=(",",":")).encode()
    req=urllib.request.Request(BROKER_URL+"/v1/critique",data=payload,headers={**_headers(),"Content-Length":str(len(payload))},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=240) as response:
            raw=response.read(MAX_RESPONSE+1)
    except urllib.error.HTTPError as exc:
        try: detail=json.loads(exc.read(4096).decode()).get("error")
        except Exception: detail=f"http_{exc.code}"
        raise CriticError(str(detail or f"http_{exc.code}")) from exc
    except Exception as exc:
        raise CriticError("critic_broker_unavailable") from exc
    if len(raw)>MAX_RESPONSE: raise CriticError("critic_response_too_large")
    try: data=json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc: raise CriticError("critic_response_invalid") from exc
    if not isinstance(data,dict) or not data.get("ok"): raise CriticError(str(data.get("error") if isinstance(data,dict) else "critic_failed"))
    return data
