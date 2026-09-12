#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import pwd
import re
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOST = os.getenv("VEKTOR_VIDEO_CRITIC_HOST", "127.0.0.1")
PORT = int(os.getenv("VEKTOR_VIDEO_CRITIC_PORT", "8778"))
MODELS = ("gemini-3.8-flash-medium", "gemini-3.8-flash-low", "gemini-3.8-flash-high")
MODEL = MODELS[0]
UPSTREAM_URL = "https://lingsuan.top/v1/chat/completions"
PRIVATE_ROOT = Path(os.getenv("VEKTOR_VIDEO_PRIVATE_ROOT", "/opt/vektor/video-editor/private"))
SECRET_FILE = PRIVATE_ROOT / "lingsuan.env"
CLIENT_ROOT = PRIVATE_ROOT / "critic-clients"
POLICY_ROOT = PRIVATE_ROOT / "critic-policies"
TMP_ROOT = PRIVATE_ROOT / "critic-tmp"
PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
MAX_LOCAL_BODY = 32 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
MAX_PROXY_BYTES = 24 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DURATION = 1200.0
UPSTREAM_TIMEOUT = 90.0
GATE = threading.BoundedSemaphore(1)
VALID_STAGES = {"cut", "master"}


def _secure_regular(path: Path, *, expected_uid: int = 0, max_bytes: int = 65536) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError("secure_file_missing") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("secure_file_unsafe")
    if info.st_uid != expected_uid or (info.st_mode & 0o077):
        raise RuntimeError("secure_file_permissions_unsafe")
    if info.st_size <= 0 or info.st_size > max_bytes:
        raise RuntimeError("secure_file_size_invalid")
    return info


def _secure_root_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or (info.st_mode & 0o077):
        raise RuntimeError("private_directory_unsafe")
    path.chmod(0o700)
def _lingsuan_key() -> str:
    _secure_regular(SECRET_FILE, max_bytes=4096)
    for line in SECRET_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("LINGSUAN_API_KEY="):
            value = line.partition("=")[2].strip()
            if 32 <= len(value) <= 256 and not any(ch.isspace() for ch in value):
                return value
    raise RuntimeError("lingsuan_key_missing")


def _client_token(profile: str) -> str:
    if not PROFILE_RE.fullmatch(profile):
        return ""
    path = CLIENT_ROOT / f"{profile}.token"
    try:
        _secure_regular(path, max_bytes=512)
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, RuntimeError):
        return ""
    return value if 32 <= len(value) <= 256 and not any(ch.isspace() for ch in value) else ""


def _authorized(headers) -> tuple[bool, str]:
    profile = str(headers.get("X-Vektor-Profile", "")).strip().lower()
    supplied = str(headers.get("X-Vektor-Critic-Token", "")).strip()
    expected = _client_token(profile)
    return bool(expected and supplied and hmac.compare_digest(expected, supplied)), profile


def _policy(profile: str) -> dict[str, Any]:
    path = POLICY_ROOT / f"{profile}.json"
    try:
        _secure_regular(path, max_bytes=4096)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, json.JSONDecodeError):
        return {"enabled": False, "stages": []}
    stages = [x for x in data.get("stages", []) if x in VALID_STAGES]
    return {"enabled": bool(data.get("enabled")), "stages": stages}
def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_video_path(profile: str, value: str) -> Path:
    entry = pwd.getpwnam(profile)
    root = (Path(entry.pw_dir) / ".hermes" / "video_editor" / "jobs").resolve(strict=True)
    raw = Path(str(value or "").strip())
    if not raw.is_absolute() or raw.is_symlink():
        raise RuntimeError("critic_path_invalid")
    try:
        path = raw.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("critic_path_missing") from exc
    if not _inside(path, root) or not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
        raise RuntimeError("critic_path_outside_profile_jobs")
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError("critic_path_symlink_rejected")
    info = path.stat()
    if info.st_uid != entry.pw_uid or info.st_size <= 0 or info.st_size > MAX_SOURCE_BYTES:
        raise RuntimeError("critic_path_ownership_or_size_invalid")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
def _probe(path: Path) -> float:
    proc = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
    ], capture_output=True, text=True, errors="replace", timeout=30)
    try:
        duration = float(proc.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("critic_duration_invalid") from exc
    if proc.returncode != 0 or duration <= 0 or duration > MAX_DURATION:
        raise RuntimeError("critic_duration_not_supported")
    return duration


def _proxy_once(source: Path, target: Path, *, size: int, fps: int, crf: int, audio_k: int) -> None:
    vf = f"scale={size}:{size}:force_original_aspect_ratio=decrease,fps={fps}"
    proc = subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", f"{audio_k}k",
        "-ac", "1", "-ar", "16000", "-movflags", "+faststart", str(target),
    ], capture_output=True, text=True, errors="replace", timeout=900)
    if proc.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
        raise RuntimeError("critic_proxy_failed")


def _make_proxy(source: Path, root: Path) -> tuple[Path, float]:
    duration = _probe(source)
    target = root / "critic-proxy.mp4"
    # Full-video critic needs narrative/pacing context, not frame-by-frame seam
    # inspection. Local Director QA owns the micro-cut pixels, so 2 fps keeps
    # the whole timeline while materially reducing upstream video tokens/time.
    _proxy_once(source, target, size=480, fps=2, crf=35, audio_k=48)
    if target.stat().st_size > MAX_PROXY_BYTES:
        target.unlink(missing_ok=True)
        _proxy_once(source, target, size=360, fps=1, crf=37, audio_k=32)
    if target.stat().st_size > MAX_PROXY_BYTES:
        raise RuntimeError("critic_proxy_too_large")
    target.chmod(0o600)
    return target, duration
def _prompt(stage: str) -> str:
    phase = "base edited cut before graphics" if stage == "cut" else "final edited master with captions, graphics and sound"
    return f"""You are the second video director reviewing a {phase}.
Watch the ENTIRE attached video, including audio. Return one final JSON object only.
Schema: {{"verdict":"pass|fix","summary":"short Russian summary","issues":[{{"category":"jump_cut|gesture|blink|framing|caption|overlay|composition|proof|thumbnail|audio_visual_sync|pacing|hook|other","severity":"low|medium|high","at":12.3,"detail":"what is visibly wrong","action":"concrete edit"}}]}}.
Use timestamps in seconds. Mark verdict=fix when any medium/high issue would make a professional social-video editor change the cut or final design.
Inspect: opening hook, pacing/dead air, jump cuts, gesture continuity, eye/blink states, framing, audio-video sync, and semantic coherence. For final masters also inspect caption readability, cards/B-roll/overlays, visual clutter and whether effects distract from speech.
Do not invent issues. Low severity is advisory. Be concise; maximum 10 issues."""


def _extract_json(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    preferred = [x for x in candidates if str(x.get("verdict") or "").lower() in {"pass", "fix"}]
    return (preferred or candidates)[-1] if (preferred or candidates) else None
def _normalise_report(raw: dict[str, Any] | None, fallback_text: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"verdict": "review", "summary": fallback_text[:1200], "issues": [], "structured": False}
    verdict = str(raw.get("verdict") or "review").strip().lower()
    if verdict not in {"pass", "fix"}:
        verdict = "review"
    summary = " ".join(str(raw.get("summary") or fallback_text).split()).strip()[:1200]
    allowed_categories = {"jump_cut","gesture","blink","framing","caption","overlay","composition","proof","thumbnail","audio_visual_sync","pacing","hook","other"}
    issues = []
    for item in (raw.get("issues") or [])[:10]:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "other").strip().lower()
        severity = str(item.get("severity") or "medium").strip().lower()
        if category not in allowed_categories: category = "other"
        if severity not in {"low","medium","high"}: severity = "medium"
        detail = " ".join(str(item.get("detail") or "").split()).strip()[:360]
        action = " ".join(str(item.get("action") or "").split()).strip()[:360]
        if not detail: continue
        clean: dict[str, Any] = {"category":category,"severity":severity,"detail":detail}
        if action: clean["action"] = action
        try:
            if item.get("at") is not None: clean["at"] = round(max(0.0,float(item["at"])),3)
        except (TypeError,ValueError): pass
        issues.append(clean)
    return {"verdict": verdict, "summary": summary, "issues": issues, "structured": True}
def _retry_delay(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try: return min(20.0, max(1.0, float(retry_after)))
        except ValueError: pass
    return min(16.0, float(2 ** attempt))


def _post_lingsuan(proxy: Path, stage: str) -> tuple[dict[str, Any], str]:
    video = base64.b64encode(proxy.read_bytes()).decode("ascii")
    key = _lingsuan_key()
    last = "lingsuan_failed"
    for model_index, model in enumerate(MODELS):
        payload = {
            "model": model,
            "messages": [{"role":"user","content":[
                {"type":"text","text":_prompt(stage)},
                {"type":"video_url","video_url":{"url":"data:video/mp4;base64," + video}},
            ]}],
            "temperature": 0.1,
            "max_tokens": 2200,
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        for attempt in range(1, 3):
            req = urllib.request.Request(UPSTREAM_URL, data=body, headers={
                "Authorization": "Bearer " + key, "Content-Type": "application/json",
                "User-Agent": "VektorVideoCritic/0.2",
            }, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES: raise RuntimeError("lingsuan_response_too_large")
                    data = json.loads(raw.decode("utf-8"))
                    text = str(data["choices"][0]["message"].get("content") or "")
                    return _normalise_report(_extract_json(text), text), model
            except urllib.error.HTTPError as exc:
                last = f"lingsuan_http_{exc.code}"
                # 524 is Cloudflare's upstream timeout; move to the next 3.8
                # reasoning tier rather than retrying the same slow model.
                if exc.code == 524:
                    break
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise RuntimeError(last) from exc
                if attempt < 2:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    time.sleep(_retry_delay(attempt, retry_after))
            except (urllib.error.URLError, TimeoutError) as exc:
                last = "lingsuan_timeout_or_transport_error"
                if attempt < 2:
                    time.sleep(_retry_delay(attempt, None))
            except (json.JSONDecodeError, KeyError, IndexError, TypeError, RuntimeError) as exc:
                last = "lingsuan_response_invalid"
                if attempt < 2:
                    time.sleep(_retry_delay(attempt, None))
        if model_index < len(MODELS) - 1:
            time.sleep(0.5)
    raise RuntimeError(last)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class BrokerServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8


class Handler(BaseHTTPRequestHandler):
    server_version = "VektorVideoCritic/0.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self) -> None:
        if self.path == "/health":
            try: _lingsuan_key(); secret_ok = True
            except RuntimeError: secret_ok = False
            self._reply(200 if secret_ok else 503, {"ok":secret_ok,"model":MODEL,"upstream":"lingsuan.top"})
            return
        if self.path != "/v1/health":
            self._reply(404,{"ok":False,"error":"not_found"}); return
        authorized, profile = _authorized(self.headers)
        if not authorized:
            self._reply(403,{"ok":False,"error":"forbidden"}); return
        policy = _policy(profile)
        self._reply(200,{"ok":True,"model":MODEL,"upstream":"lingsuan.top","enabled":policy["enabled"],"stages":policy["stages"]})

    def do_POST(self) -> None:
        if self.path != "/v1/critique":
            self._reply(404,{"ok":False,"error":"not_found"}); return
        authorized, profile = _authorized(self.headers)
        if not authorized:
            self._reply(403,{"ok":False,"error":"forbidden"}); return
        policy = _policy(profile)
        if not policy["enabled"]:
            self._reply(403,{"ok":False,"error":"critic_disabled"}); return
        try: length = int(self.headers.get("Content-Length","0"))
        except ValueError: length = 0
        if length <= 0 or length > MAX_LOCAL_BODY:
            self._reply(413,{"ok":False,"error":"request_too_large"}); return
        try:
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
            stage = str(body.get("stage") or "").strip().lower()
            if stage not in VALID_STAGES or stage not in policy["stages"]:
                self._reply(403,{"ok":False,"error":"critic_stage_disabled"}); return
            source = _validate_video_path(profile, str(body.get("path") or ""))
            expected_sha = str(body.get("artifact_sha256") or "").strip().lower()
            actual_sha = _sha256(source)
            if not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or not hmac.compare_digest(expected_sha, actual_sha):
                self._reply(409,{"ok":False,"error":"artifact_fingerprint_mismatch"}); return
        except (UnicodeDecodeError,json.JSONDecodeError,RuntimeError,KeyError,OSError) as exc:
            self._reply(400,{"ok":False,"error":str(exc)[:100]}); return
        if not GATE.acquire(timeout=60.0):
            self._reply(503,{"ok":False,"error":"critic_busy"}); return
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix=f"{profile}-", dir=TMP_ROOT) as tmp_name:
                tmp = Path(tmp_name); tmp.chmod(0o700)
                proxy, duration = _make_proxy(source, tmp)
                report, used_model = _post_lingsuan(proxy, stage)
                self._reply(200,{
                    "ok":True,"model":used_model,"provider":"lingsuan.top","stage":stage,
                    "artifact_sha256":actual_sha,"duration":round(duration,3),
                    "proxy_bytes":proxy.stat().st_size,"elapsed_seconds":round(time.monotonic()-started,3),
                    "report":report,
                })
        except Exception as exc:
            self._reply(502,{"ok":False,"error":str(exc)[:120]})
        finally:
            GATE.release()
def main() -> int:
    if HOST not in {"127.0.0.1","::1"}:
        raise RuntimeError("critic_broker_must_bind_loopback")
    for path in (PRIVATE_ROOT,CLIENT_ROOT,POLICY_ROOT,TMP_ROOT):
        _secure_root_dir(path)
    _lingsuan_key()
    BrokerServer((HOST,PORT),Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
