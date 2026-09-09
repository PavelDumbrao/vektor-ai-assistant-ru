from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import asr_client

POP_MARGIN_DB = 3.0
DRIFT_LIMIT = 0.15


def _sh(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace")


def _duration(path: Path) -> float:
    proc = _sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)])
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def _peak_db(path: Path, start: float, duration: float) -> float | None:
    proc = _sh(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{max(0,start):.3f}", "-t", f"{duration:.3f}", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"])
    match = re.search(r"max_volume:\s*(-?[\d.]+) dB", proc.stderr)
    return float(match.group(1)) if match else None


def _norm_token(value: str) -> str:
    return re.sub(r"[^\w]+", "", value.lower())


def _repeated_runs(tokens: list[str]) -> list[str]:
    words = []
    for token in tokens:
        for part in str(token).split():
            clean = _norm_token(part)
            if clean:
                words.append(clean)
    max_n = max(1, min(15, len(words) // 2))
    hits = []
    for width in range(1, max_n + 1):
        index = 0
        while index + 2 * width <= len(words):
            left = words[index:index + width]
            right = words[index + width:index + 2 * width]
            if left == right and all(len(item) > 1 for item in left):
                hits.append(" ".join(left)); index += width
            else:
                index += 1
    hits = sorted(set(hits), key=lambda value: -len(value.split()))
    kept = []
    for hit in hits:
        if not any(hit in existing for existing in kept):
            kept.append(hit)
    return kept


def _transcribe_window(video: Path, start: float, duration: float, language: str, work: Path, index: int) -> tuple[str, list[str]]:
    clip = work / f"seam_{index:03d}.mp3"
    proc = _sh(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(0,start):.3f}", "-t", f"{duration:.3f}", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k", str(clip)])
    if proc.returncode != 0 or not clip.is_file():
        raise RuntimeError("seam_audio_extract_failed")
    result = asr_client._call_broker(clip.read_bytes(), language)
    words = result.get("words") or []
    text = str(result.get("text") or "").strip()
    tokens = [str(item.get("word") or item.get("text") or "") for item in words] if words else text.split()
    return text, tokens


def verify(studio: Path, language: str, window: float = 3.0) -> dict[str, Any]:
    timeline_path = studio / "timeline.json"
    if not timeline_path.is_file():
        raise RuntimeError("timeline_missing")
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    video = Path(str(timeline.get("output") or ""))
    if not video.is_file():
        raise RuntimeError("rendered_video_missing")
    actual = _duration(video)
    predicted = float(timeline.get("predicted_duration") or 0)
    drift = abs(actual - predicted)
    problems = []
    if drift > DRIFT_LIMIT:
        problems.append(f"duration drift {drift:.3f}s (actual {actual:.2f}s vs predicted {predicted:.2f}s)")
    verify_dir = studio / "verify"
    verify_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    with tempfile.TemporaryDirectory(prefix="broker-verify-", dir=verify_dir) as tmp:
        work = Path(tmp)
        for index, at in enumerate(timeline.get("seams") or []):
            point = float(at)
            start = max(0.0, point - window)
            span = min(window * 2, max(0.1, actual - start))
            text, tokens = _transcribe_window(video, start, span, language, work, index)
            repeats = _repeated_runs(tokens)
            seam_peak = _peak_db(video, point - 0.03, 0.06)
            before = _peak_db(video, point - 0.25, 0.20)
            after = _peak_db(video, point + 0.05, 0.20)
            pop = seam_peak is not None and before is not None and after is not None and seam_peak > before + POP_MARGIN_DB and seam_peak > after + POP_MARGIN_DB
            frame = verify_dir / f"seam_{index:03d}_{point:.2f}s.png"
            _sh(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{point:.3f}", "-i", str(video), "-frames:v", "1", str(frame)])
            if repeats:
                problems.append(f"seam {index} at {point:.2f}s repeats: {', '.join(repeats)}")
            if pop:
                problems.append(f"seam {index} at {point:.2f}s may pop")
            reports.append({"seam": index, "at": round(point,3), "text": text, "repeated": repeats, "audio": {"seam_db": seam_peak, "before_db": before, "after_db": after, "pop": pop}, "frame": str(frame) if frame.is_file() else None})
    report = {"video": str(video), "duration": round(actual,2), "predicted_duration": predicted, "drift": round(drift,3), "provider": "openrouter", "seams": reports, "problems": problems}
    path = verify_dir / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return report
