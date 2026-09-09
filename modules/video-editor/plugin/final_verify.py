from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from . import engine

EXPECTED_SIZES = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}
MAX_DURATION_DRIFT = 0.35


def _probe(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0:
        return {"ok": False, "error": "ffprobe_failed"}
    try:
        data = json.loads(proc.stdout)
        video = next(item for item in data.get("streams", []) if item.get("codec_type") == "video")
        duration = float((data.get("format") or {}).get("duration") or 0)
        return {
            "ok": True,
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "duration": duration,
            "audio": any(item.get("codec_type") == "audio" for item in data.get("streams", [])),
        }
    except Exception:
        return {"ok": False, "error": "ffprobe_invalid"}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _safe_output(path: Path, root: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return path.is_file() and not path.is_symlink() and _inside(path, root) and info.st_size > 1024


def _decode_ok(path: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0?", "-map", "0:a:0?", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    return proc.returncode == 0, engine._safe_tail(proc.stderr, 800)


def _contact_sheet(studio: Path, output: Path, duration: float) -> str | None:
    verify = studio / "verify" / "final"
    verify.mkdir(parents=True, exist_ok=True)
    frames: list[Path] = []
    for index, fraction in enumerate((0.08, 0.28, 0.50, 0.72, 0.92)):
        point = max(0.05, min(duration - 0.05, duration * fraction))
        target = verify / f"frame_{index}_{point:.2f}s.png"
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{point:.3f}", "-i", str(output), "-frames:v", "1", "-vf", "scale=216:-2", str(target)],
            capture_output=True, text=True, errors="replace",
        )
        if proc.returncode != 0 or not target.is_file():
            return None
        target.chmod(0o600)
        frames.append(target)
    contact = verify / "contact.png"
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for frame in frames:
        cmd += ["-i", str(frame)]
    cmd += ["-filter_complex", f"hstack=inputs={len(frames)}", str(contact)]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0 or not contact.is_file():
        return None
    contact.chmod(0o600)
    return str(contact)


def verify(studio: Path, output: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    out_root = (studio / "out").resolve()
    problems: list[str] = []
    if not _safe_output(output, out_root):
        return {"ok": False, "problems": ["final_output_unsafe_or_missing"]}
    output.chmod(0o600)
    media = _probe(output)
    if not media.get("ok"):
        problems.append("final_probe_failed")
        duration = 0.0
    else:
        duration = float(media.get("duration") or 0)
        if int(media.get("width") or 0) <= 0 or int(media.get("height") or 0) <= 0:
            problems.append("final_dimensions_invalid")
        if not media.get("audio"):
            problems.append("final_audio_missing")
        predicted = 0.0
        timeline = studio / "timeline.json"
        if timeline.is_file():
            try:
                predicted = float(json.loads(timeline.read_text(encoding="utf-8")).get("predicted_duration") or 0)
            except Exception:
                predicted = 0.0
        if predicted > 0 and abs(duration - predicted) > MAX_DURATION_DRIFT:
            problems.append(f"final_duration_drift:{abs(duration-predicted):.3f}")
    decode_ok, decode_log = _decode_ok(output)
    if not decode_ok:
        problems.append("final_decode_failed")
    peak = engine._audio_peak_db(output)
    if peak is None:
        problems.append("final_audio_peak_unmeasurable")
    elif peak < -55.0:
        problems.append(f"final_audio_too_quiet:{peak:.1f}")
    elif peak > 0.1:
        problems.append(f"final_audio_clipping:{peak:.1f}")

    variants_report: dict[str, Any] = {}
    for aspect, item in (manifest.get("variants") or {}).items():
        expected = EXPECTED_SIZES.get(aspect)
        raw_path = Path(str((item or {}).get("path") or ""))
        entry: dict[str, Any] = {"path": str(raw_path), "expected": list(expected) if expected else None}
        if expected is None or not _safe_output(raw_path, out_root):
            entry["ok"] = False
            entry["error"] = "variant_unsafe_or_missing"
            problems.append(f"variant_{aspect}_unsafe_or_missing")
            variants_report[aspect] = entry
            continue
        raw_path.chmod(0o600)
        probe = _probe(raw_path)
        entry["probe"] = probe
        good = bool(
            probe.get("ok")
            and (int(probe.get("width") or 0), int(probe.get("height") or 0)) == expected
            and probe.get("audio")
            and abs(float(probe.get("duration") or 0) - duration) <= MAX_DURATION_DRIFT
        )
        entry["ok"] = good
        if not good:
            problems.append(f"variant_{aspect}_verification_failed")
        variants_report[aspect] = entry
    thumbs = []
    for item in manifest.get("thumbnails") or []:
        path = Path(str((item or {}).get("path") or ""))
        ok = _safe_output(path, out_root) and path.suffix.lower() == ".png"
        thumbs.append({"path": str(path), "ok": ok, "at": item.get("at")})
        if ok:
            path.chmod(0o600)
    if len([item for item in thumbs if item["ok"]]) < 3:
        problems.append("thumbnail_candidates_insufficient")

    contact = _contact_sheet(studio, output, duration) if duration > 0 else None
    if not contact:
        problems.append("final_contact_sheet_failed")

    report = {
        "ok": not problems,
        "output": str(output),
        "media": media,
        "audio_peak_db": peak,
        "decode_ok": decode_ok,
        "decode_log": decode_log,
        "variants": variants_report,
        "thumbnails": thumbs,
        "contact_sheet": contact,
        "problems": problems,
    }
    report_path = studio / "verify" / "final_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.chmod(0o600)
    report["report"] = str(report_path)
    return report
