#!/usr/bin/env python3
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.getenv("VEKTOR_VIDEO_ASR_HOST", "127.0.0.1")
PORT = int(os.getenv("VEKTOR_VIDEO_ASR_PORT", "8777"))
MODEL = "openai/whisper-large-v3-turbo"
OPENROUTER_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
PRIVATE_ROOT = Path(os.getenv("VEKTOR_VIDEO_PRIVATE_ROOT", "/opt/vektor/video-editor/private"))
CLIENT_ROOT = PRIVATE_ROOT / "clients"
MAX_BODY = min(4 * 1024 * 1024, max(256 * 1024, int(os.getenv("VEKTOR_VIDEO_ASR_MAX_BODY", str(4 * 1024 * 1024)))))
MAX_RESPONSE = 8 * 1024 * 1024
MAX_CONCURRENCY = min(4, max(1, int(os.getenv("VEKTOR_VIDEO_ASR_CONCURRENCY", "2"))))
QUEUE_WAIT_SECONDS = min(180.0, max(1.0, float(os.getenv("VEKTOR_VIDEO_ASR_QUEUE_WAIT", "120"))))
UPSTREAM_TIMEOUT = min(120.0, max(10.0, float(os.getenv("VEKTOR_VIDEO_ASR_UPSTREAM_TIMEOUT", "75"))))
PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
FORMATS = {"wav", "mp3", "flac", "m4a", "ogg", "webm", "aac"}
GATE = threading.BoundedSemaphore(max(1, MAX_CONCURRENCY))


def _secure_regular(path: Path, *, expected_uid: int = 0, max_bytes: int = 4096) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError("secret_file_missing") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("secret_file_unsafe")
    if info.st_uid != expected_uid or (info.st_mode & 0o077):
        raise RuntimeError("secret_file_permissions_unsafe")
    if info.st_size <= 0 or info.st_size > max_bytes:
        raise RuntimeError("secret_file_size_invalid")
    return info


def _secure_root_dir(path: Path) -> None:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or (info.st_mode & 0o077):
        raise RuntimeError("private_directory_unsafe")


def _openrouter_key() -> str:
    direct = os.getenv("OPENROUTER_API_KEY", "").strip()
    if direct:
        return direct
    env_path = Path(os.getenv("VEKTOR_VIDEO_OPENROUTER_ENV", str(PRIVATE_ROOT / "openrouter.env")))
    _secure_regular(env_path)
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("OPENROUTER_API_KEY="):
            value = line.partition("=")[2].strip()
            if len(value) >= 16 and not any(ch.isspace() for ch in value):
                return value
    raise RuntimeError("openrouter_key_missing")


def _client_token(profile: str) -> str:
    if not PROFILE_RE.fullmatch(profile):
        return ""
    path = CLIENT_ROOT / f"{profile}.token"
    try:
        _secure_regular(path, max_bytes=512)
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, RuntimeError):
        return ""
    if not 32 <= len(value) <= 256 or any(ch.isspace() for ch in value):
        return ""
    return value


def _authorized(headers) -> bool:
    profile = str(headers.get("X-Vektor-Profile", "")).strip()
    supplied = str(headers.get("X-Vektor-ASR-Token", "")).strip()
    expected = _client_token(profile)
    return bool(expected and supplied and hmac.compare_digest(supplied, expected))


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(20.0, max(1.0, float(retry_after)))
        except ValueError:
            pass
    return min(20.0, float(2 ** max(1, attempt)))


def _post_openrouter(audio: bytes, audio_format: str, language: str) -> dict:
    payload = {
        "model": MODEL,
        "input_audio": {
            "data": base64.b64encode(audio).decode("ascii"),
            "format": audio_format,
        },
        "language": language,
        "temperature": 0,
        "response_format": "verbose_json",
        "timestamp_granularities": ["word", "segment"],
    }
    body = _json_bytes(payload)
    key = _openrouter_key()
    last = "openrouter_failed"
    for attempt in range(1, 5):
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=body,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise RuntimeError("openrouter_response_too_large")
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                    raise RuntimeError("openrouter_invalid_response")
                return data
        except urllib.error.HTTPError as exc:
            last = f"openrouter_http_{exc.code}"
            if exc.code != 429 and exc.code < 500:
                break
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            time.sleep(_retry_delay(attempt, retry_after))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError):
            last = "openrouter_transport_or_response_error"
            time.sleep(_retry_delay(attempt, None))
    raise RuntimeError(last)


class BrokerServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16


class Handler(BaseHTTPRequestHandler):
    server_version = "VektorVideoASR/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _reply(self, status: int, payload: dict) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path not in {"/health", "/v1/health"}:
            self._reply(404, {"ok": False, "error": "not_found"})
            return
        if self.path == "/v1/health" and not _authorized(self.headers):
            self._reply(403, {"ok": False, "error": "forbidden"})
            return
        try:
            _openrouter_key()
        except RuntimeError:
            self._reply(503, {"ok": False, "error": "key_missing"})
            return
        self._reply(200, {"ok": True, "model": MODEL, "max_concurrency": MAX_CONCURRENCY})

    def do_POST(self) -> None:
        if self.path != "/v1/transcribe":
            self._reply(404, {"ok": False, "error": "not_found"})
            return
        if not _authorized(self.headers):
            self._reply(403, {"ok": False, "error": "forbidden"})
            return
        audio_format = self.headers.get("X-Audio-Format", "").strip().lower()
        language = self.headers.get("X-Language", "").strip().lower()
        if audio_format not in FORMATS or not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", language):
            self._reply(400, {"ok": False, "error": "invalid_audio_metadata"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._reply(413, {"ok": False, "error": "audio_too_large"})
            return
        if not GATE.acquire(timeout=QUEUE_WAIT_SECONDS):
            self._reply(503, {"ok": False, "error": "broker_busy"})
            return
        started = time.monotonic()
        try:
            self.connection.settimeout(30.0)
            audio = self.rfile.read(length)
            if len(audio) != length:
                self._reply(400, {"ok": False, "error": "short_body"})
                return
            result = _post_openrouter(audio, audio_format, language)
            self._reply(200, {
                "ok": True,
                "model": MODEL,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "text": result.get("text", ""),
                "language": result.get("language"),
                "duration": result.get("duration"),
                "words": result.get("words") or [],
                "segments": result.get("segments") or [],
                "usage": result.get("usage") or {},
            })
        except Exception as exc:
            self._reply(502, {"ok": False, "error": str(exc)[:120]})
        finally:
            GATE.release()


def main() -> int:
    if HOST not in {"127.0.0.1", "::1"}:
        raise RuntimeError("broker_must_bind_loopback")
    PRIVATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    CLIENT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    _secure_root_dir(PRIVATE_ROOT)
    _secure_root_dir(CLIENT_ROOT)
    server = BrokerServer((HOST, PORT), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
