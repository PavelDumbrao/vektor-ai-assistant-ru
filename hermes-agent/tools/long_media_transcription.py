"""Deterministic long audio/video preparation for Hermes STT.

This module has no provider credentials and performs no network I/O.  It only
probes media and, when needed, extracts the first audio stream into bounded
16 kHz mono PCM WAV chunks that are safely below typical cloud STT upload
limits.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_CHUNK_SECONDS = 480
DEFAULT_DURATION_THRESHOLD_SECONDS = 1200
DEFAULT_MAX_CHUNKS = 180
VIDEO_SUFFIXES = {".mp4", ".mpeg", ".mpg", ".webm", ".mov", ".mkv", ".m4v", ".avi"}


@dataclass(frozen=True)
class MediaProbe:
    duration_seconds: Optional[float]
    has_audio: bool
    has_video: bool
    size_bytes: int


@dataclass(frozen=True)
class MediaChunk:
    path: str
    index: int
    start_seconds: int


@dataclass
class PreparedLongMedia:
    source_path: str
    temp_dir: str
    chunks: list[MediaChunk]
    probe: MediaProbe
    chunk_seconds: int

    def cleanup(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)


def _run(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def probe_media(file_path: str) -> MediaProbe:
    path = Path(file_path)
    size = path.stat().st_size
    ffprobe = shutil.which("ffprobe")
    suffix_video = path.suffix.lower() in VIDEO_SUFFIXES
    if not ffprobe:
        return MediaProbe(None, True, suffix_video, size)

    proc = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            str(path),
        ],
        timeout=30,
    )
    if proc.returncode != 0:
        return MediaProbe(None, True, suffix_video, size)
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return MediaProbe(None, True, suffix_video, size)

    streams = payload.get("streams") or []
    stream_types = {str(s.get("codec_type", "")).lower() for s in streams if isinstance(s, dict)}
    raw_duration = (payload.get("format") or {}).get("duration")
    try:
        duration = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration = None
    if duration is not None and (not math.isfinite(duration) or duration < 0):
        duration = None

    return MediaProbe(
        duration_seconds=duration,
        has_audio=("audio" in stream_types) or not streams,
        has_video=("video" in stream_types) or suffix_video,
        size_bytes=size,
    )


def should_prepare_long_media(
    file_path: str,
    *,
    provider_is_local: bool,
    cloud_max_bytes: int,
    duration_threshold_seconds: int = DEFAULT_DURATION_THRESHOLD_SECONDS,
) -> tuple[bool, MediaProbe]:
    probe = probe_media(file_path)
    if probe.has_video:
        return True, probe
    if not provider_is_local and probe.size_bytes > cloud_max_bytes:
        return True, probe
    if (
        not provider_is_local
        and probe.duration_seconds is not None
        and probe.duration_seconds > duration_threshold_seconds
    ):
        return True, probe
    return False, probe


def prepare_long_media(
    file_path: str,
    *,
    chunk_seconds: int = DEFAULT_CHUNK_SECONDS,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
    probe: Optional[MediaProbe] = None,
) -> PreparedLongMedia:
    if not 60 <= int(chunk_seconds) <= 600:
        raise ValueError("long-media chunk_seconds must be between 60 and 600")
    if not 1 <= int(max_chunks) <= 720:
        raise ValueError("long-media max_chunks must be between 1 and 720")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for long audio/video transcription")

    source = Path(file_path)
    media_probe = probe or probe_media(str(source))
    if media_probe.has_audio is False:
        raise RuntimeError("media file has no audio stream to transcribe")

    if media_probe.duration_seconds:
        estimated = int(math.ceil(media_probe.duration_seconds / chunk_seconds))
        if estimated > max_chunks:
            raise RuntimeError(
                f"media is too long for configured transcription limit "
                f"({estimated} chunks > {max_chunks})"
            )

    temp_dir = tempfile.mkdtemp(prefix="hermes-long-media-")
    pattern = str(Path(temp_dir) / "chunk_%05d.wav")
    duration = media_probe.duration_seconds
    timeout = 900
    if duration is not None:
        timeout = max(120, min(1800, int(duration / 4) + 120))

    proc = _run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "segment",
            "-segment_time",
            str(int(chunk_seconds)),
            "-reset_timestamps",
            "1",
            pattern,
        ],
        timeout=timeout,
    )
    if proc.returncode != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        detail = (proc.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else "unknown ffmpeg error"
        raise RuntimeError(f"ffmpeg could not extract audio: {tail[:240]}")

    paths = sorted(Path(temp_dir).glob("chunk_*.wav"))
    if not paths:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError("ffmpeg produced no audio chunks")
    if len(paths) > max_chunks:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(f"media produced too many chunks ({len(paths)} > {max_chunks})")

    chunks = [
        MediaChunk(path=str(p), index=i, start_seconds=i * int(chunk_seconds))
        for i, p in enumerate(paths)
    ]
    return PreparedLongMedia(
        source_path=str(source),
        temp_dir=temp_dir,
        chunks=chunks,
        probe=media_probe,
        chunk_seconds=int(chunk_seconds),
    )


def format_timestamp(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
