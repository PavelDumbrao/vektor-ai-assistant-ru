from __future__ import annotations

import hashlib
import json
import os
import pwd
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BROKER_URL = os.getenv("HERMES_VIDEO_ASR_BROKER", "http://127.0.0.1:8777").rstrip("/")
MODEL = "openai/whisper-large-v3-turbo"
CHUNK_SECONDS = min(240, max(30, int(os.getenv("HERMES_VIDEO_ASR_CHUNK_SECONDS", "180"))))


class ASRError(RuntimeError):
    pass


def _hermes_home() -> Path:
    raw = os.getenv("HERMES_HOME")
    return Path(raw).expanduser().resolve() if raw else (Path.home() / ".hermes").resolve()


def _profile() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name.lower()


def _token() -> str:
    path = _hermes_home() / "video_editor" / "asr_token"
    try:
        info = path.lstat()
    except OSError:
        return ""
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or (info.st_mode & 0o077) or info.st_size > 512:
        return ""
    value = path.read_text(encoding="utf-8").strip()
    if not 32 <= len(value) <= 256 or any(ch.isspace() for ch in value):
        return ""
    return value


def available() -> bool:
    token = _token()
    if not token:
        return False
    req = urllib.request.Request(
        BROKER_URL + "/v1/health",
        headers={"X-Vektor-Profile": _profile(), "X-Vektor-ASR-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as response:
            data = json.loads(response.read().decode("utf-8"))
            return bool(data.get("ok"))
    except Exception:
        return False


def _source_key(path: Path) -> str:
    stat = path.stat()
    raw = f"{path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}".encode()
    return hashlib.sha1(raw).hexdigest()[:12]


def _duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, errors="replace",
    )
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise ASRError("asr_duration_invalid") from exc


def _extract_mp3(source: Path, target: Path, start: float, duration: float) -> None:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k", str(target),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0 or not target.is_file():
        raise ASRError("asr_audio_extract_failed")


def _call_broker(audio: bytes, language: str) -> dict[str, Any]:
    token = _token()
    if not token:
        raise ASRError("asr_broker_token_missing")
    req = urllib.request.Request(
        BROKER_URL + "/v1/transcribe",
        data=audio,
        headers={
            "Content-Type": "audio/mpeg",
            "Content-Length": str(len(audio)),
            "X-Vektor-Profile": _profile(),
            "X-Vektor-ASR-Token": token,
            "X-Audio-Format": "mp3",
            "X-Language": language,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=95) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ASRError(f"asr_broker_http_{exc.code}") from exc
    except Exception as exc:
        raise ASRError("asr_broker_unavailable") from exc
    if not isinstance(data, dict) or not data.get("ok"):
        raise ASRError(str(data.get("error") if isinstance(data, dict) else "asr_broker_invalid"))
    return data


def _normalise(result: dict[str, Any], offset: float) -> tuple[list[dict[str, Any]], str]:
    words = []
    for item in result.get("words") or []:
        try:
            text = str(item.get("word") or item.get("text") or "").strip()
            start = float(item.get("start")) + offset
            end = float(item.get("end")) + offset
        except (TypeError, ValueError):
            continue
        if text and end > start:
            words.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    if words:
        return words, "word"
    for item in result.get("segments") or []:
        try:
            text = str(item.get("text") or "").strip()
            start = float(item.get("start")) + offset
            end = float(item.get("end")) + offset
        except (TypeError, ValueError):
            continue
        if text and end > start:
            words.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return words, "segment"


def transcribe(source: Path, studio: Path, language: str) -> dict[str, Any]:
    total = _duration(source)
    if total <= 0:
        raise ASRError("asr_duration_invalid")
    transcript_dir = studio / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    key = _source_key(source)
    output = transcript_dir / f"{source.stem}.{key}.verbatim.json"
    all_items: list[dict[str, Any]] = []
    granularity = "word"
    usage_seconds = 0.0
    usage_cost = 0.0
    elapsed = 0.0
    with tempfile.TemporaryDirectory(prefix="video-asr-", dir=transcript_dir) as tmp:
        root = Path(tmp)
        start = 0.0
        index = 0
        while start < total - 0.05:
            span = min(float(CHUNK_SECONDS), total - start)
            piece = root / f"chunk_{index:03d}.mp3"
            _extract_mp3(source, piece, start, span)
            result = _call_broker(piece.read_bytes(), language)
            items, current_granularity = _normalise(result, start)
            if not items:
                raise ASRError("asr_broker_empty_transcript")
            if current_granularity != "word":
                granularity = "segment"
            all_items.extend(items)
            usage = result.get("usage") or {}
            try:
                usage_seconds += float(usage.get("seconds") or 0)
                usage_cost += float(usage.get("cost") or 0)
                elapsed += float(result.get("elapsed_seconds") or 0)
            except (TypeError, ValueError):
                pass
            start += span
            index += 1
    payload = {
        "source": str(source.resolve()),
        "language": language,
        "mode": "verbatim",
        "model": MODEL,
        "provider": "openrouter",
        "granularity": granularity,
        "words": all_items,
        "usage": {"seconds": round(usage_seconds, 3), "cost": usage_cost},
        "broker_elapsed_seconds": round(elapsed, 3),
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(output, 0o600)
    return {"output": str(output), "items": len(all_items), "granularity": granularity, "usage": payload["usage"], "elapsed_seconds": payload["broker_elapsed_seconds"]}
