from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from . import asr_client, seam_verify

ENGINE_COMMIT = "e8ea406bc2440ca8fc8d1b239c8758e9de112388"
RUNTIME_ROOT = Path(os.environ.get("HERMES_VIDEO_EDITOR_RUNTIME", "/opt/vektor/video-editor"))
ENGINE_ROOT = RUNTIME_ROOT / "engine" / ENGINE_COMMIT
BIN_DIR = RUNTIME_ROOT / "bin"
WHISPER_LIB_DIR = RUNTIME_ROOT / "whisper" / "v1.9.2" / "whisper-bin-ubuntu-x64"
DEFAULT_MODELS = {
    "fast": RUNTIME_ROOT / "models" / "ggml-small.bin",
    "quality": RUNTIME_ROOT / "models" / "ggml-medium.bin",
}
LOCK_PATH = RUNTIME_ROOT / "runtime.lock"
SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
MAX_BYTES = int(os.environ.get("HERMES_VIDEO_EDITOR_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))
MAX_DURATION = float(os.environ.get("HERMES_VIDEO_EDITOR_MAX_DURATION", "1200"))
PROCESS_TIMEOUT = int(os.environ.get("HERMES_VIDEO_EDITOR_TIMEOUT", "3600"))
JOB_TTL_HOURS = int(os.environ.get("HERMES_VIDEO_EDITOR_JOB_TTL_HOURS", "168"))
JOB_RE = re.compile(r"^[0-9a-f]{12}$")
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")


class VideoEditorError(RuntimeError):
    pass


def hermes_home() -> Path:
    raw = os.environ.get("HERMES_HOME")
    return Path(raw).expanduser().resolve() if raw else (Path.home() / ".hermes").resolve()


def jobs_root() -> Path:
    root = hermes_home() / "video_editor" / "jobs"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def taste_path() -> Path:
    path = hermes_home() / "video_editor" / "taste.md"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def model_path(quality: str = "fast") -> Path:
    if quality not in DEFAULT_MODELS:
        raise VideoEditorError("video_transcription_quality_invalid")
    key = "HERMES_VIDEO_EDITOR_MODEL_" + quality.upper()
    return Path(os.environ.get(key, str(DEFAULT_MODELS[quality]))).expanduser().resolve()


def runtime_ready() -> bool:
    required = [
        ENGINE_ROOT / "scripts" / "transcribe.py",
        ENGINE_ROOT / "scripts" / "pack.py",
        ENGINE_ROOT / "scripts" / "render.py",
        ENGINE_ROOT / "scripts" / "verify.py",
        BIN_DIR / "whisper-cli",
        model_path("fast"),
    ]
    return all(path.is_file() for path in required) and bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def allowed_source_roots() -> list[Path]:
    home = hermes_home()
    roots = [home / "cache" / "videos", Path.home().resolve() / "workspace"]
    return [root.resolve() for root in roots]


def validate_source_path(value: str) -> Path:
    raw = Path(str(value or "").strip()).expanduser()
    if not raw.is_absolute():
        raise VideoEditorError("video_source_must_be_absolute")
    if raw.is_symlink():
        raise VideoEditorError("video_source_symlink_rejected")
    try:
        path = raw.resolve(strict=True)
        stat = path.stat()
    except OSError as exc:
        raise VideoEditorError("video_source_not_found") from exc
    if not path.is_file():
        raise VideoEditorError("video_source_not_regular_file")
    if not any(_is_within(path, root) for root in allowed_source_roots()):
        raise VideoEditorError("video_source_outside_profile_roots")
    if path.suffix.lower() not in SUPPORTED_VIDEO_EXTS:
        raise VideoEditorError("video_source_type_not_supported")
    if stat.st_size <= 0 or stat.st_size > MAX_BYTES:
        raise VideoEditorError("video_source_size_not_supported")
    return path


def _job_dir(job_id: str) -> Path:
    if not JOB_RE.fullmatch(str(job_id or "")):
        raise VideoEditorError("invalid_video_job_id")
    path = jobs_root() / job_id
    if not path.is_dir() or path.is_symlink():
        raise VideoEditorError("video_job_not_found")
    return path


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    tmp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_meta(job: Path) -> dict[str, Any]:
    path = job / "job.json"
    if not path.is_file():
        raise VideoEditorError("video_job_metadata_missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VideoEditorError("video_job_metadata_invalid") from exc
    if not isinstance(data, dict):
        raise VideoEditorError("video_job_metadata_invalid")
    return data


def _safe_tail(text: str, limit: int = 1200) -> str:
    text = str(text or "").strip()
    return text[-limit:] if len(text) > limit else text


def _run(cmd: list[str], *, cwd: Path | None = None, allowed: tuple[int, ...] = (0,), timeout: int = PROCESS_TIMEOUT, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = str(BIN_DIR) + os.pathsep + env.get("PATH", "")
    env["LD_LIBRARY_PATH"] = str(WHISPER_LIB_DIR) + (os.pathsep + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items()})
    try:
        proc = subprocess.run(
            [str(item) for item in cmd],
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoEditorError("video_editor_process_timeout") from exc
    if proc.returncode not in allowed:
        parts = []
        stderr = _safe_tail(proc.stderr, 1800).strip()
        stdout = _safe_tail(proc.stdout, 1800).strip()
        if stderr:
            parts.append("stderr=" + stderr)
        if stdout and stdout != stderr:
            parts.append("stdout=" + stdout)
        detail = " | ".join(parts)
        raise VideoEditorError(f"video_editor_process_failed:{detail}" if detail else "video_editor_process_failed")
    return proc


@contextlib.contextmanager
def runtime_lock():
    try:
        # The shared lock is root-owned and read-only to client profiles.
        # flock(LOCK_EX) does not require write access to the inode, so a
        # client can serialize work without being able to mutate shared state.
        handle = open(LOCK_PATH, "r")
    except OSError as exc:
        raise VideoEditorError("video_editor_runtime_lock_unavailable") from exc
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _probe(path: Path) -> dict[str, Any]:
    proc = _run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,size:stream=index,codec_type,width,height,r_frame_rate:stream_tags=rotate",
        "-of", "json", str(path),
    ], timeout=60)
    try:
        data = json.loads(proc.stdout)
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise VideoEditorError("video_probe_invalid") from exc
    if duration <= 0 or duration > MAX_DURATION:
        raise VideoEditorError("video_duration_not_supported")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    return {
        "duration": round(duration, 3),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": video.get("r_frame_rate"),
        "bytes": path.stat().st_size,
    }


def _write_profile(studio: Path, language: str, pacing: str, aspect: str, selected_model: Path) -> None:
    profile = (
        f"language: {language}\n"
        "translate_captions: false\n"
        f"aspects: [\"{aspect}\"]\n"
        f"pacing: {pacing}\n"
        "transcription:\n"
        f"  model: {selected_model}\n"
    )
    (studio / "profile.yml").write_text(profile, encoding="utf-8")
    os.chmod(studio / "profile.yml", 0o600)


def _copy_source(src: Path, dst: Path) -> None:
    # A job must be an immutable snapshot. Hardlinks are deliberately avoided:
    # chmod/content changes through either name would mutate the same inode.
    shutil.copy2(src, dst)
    os.chmod(dst, 0o600)


def cleanup_old_jobs(now: float | None = None) -> int:
    if JOB_TTL_HOURS <= 0:
        return 0
    cutoff = (time.time() if now is None else float(now)) - JOB_TTL_HOURS * 3600
    removed = 0
    root = jobs_root()
    for item in root.iterdir():
        if not item.is_dir() or item.is_symlink() or not JOB_RE.fullmatch(item.name):
            continue
        try:
            if item.stat().st_mtime < cutoff:
                shutil.rmtree(item)
                removed += 1
        except OSError:
            continue
    return removed


def _silence_summary(studio: Path, alias: str) -> dict[str, Any]:
    path = studio / "silences" / f"{alias}.json"
    if not path.is_file():
        return {"source": alias, "cut_points": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    points = []
    for gap in data.get("gaps", []):
        if gap.get("quality") not in {"clean", "usable"}:
            continue
        points.append({
            "start": gap.get("start"),
            "end": gap.get("end"),
            "duration": gap.get("duration"),
            "quality": gap.get("quality"),
        })
    return {
        "source": alias,
        "duration": data.get("duration"),
        "speech_time": data.get("speech_time"),
        "cut_points": points,
    }


def _cleanup_broker_outputs(details: list[dict[str, Any]], studio: Path) -> None:
    root = (studio / "transcripts").resolve()
    for detail in details:
        raw = str((detail or {}).get("output") or "").strip()
        if not raw:
            continue
        try:
            path = Path(raw).resolve()
        except OSError:
            continue
        if _is_within(path, root):
            path.unlink(missing_ok=True)


def _transcribe_sources(source_files: list[str], studio: Path, language: str, selected_model: Path, asr_provider: str, py: str) -> tuple[str, list[dict[str, Any]], str | None]:
    asr_used = "local"
    asr_details: list[dict[str, Any]] = []
    broker_error: str | None = None
    if asr_provider in {"auto", "openrouter"}:
        try:
            for source_file in source_files:
                asr_details.append(asr_client.transcribe(Path(source_file), studio, language))
            asr_used = "openrouter"
        except Exception as exc:
            broker_error = str(exc)[:160]
            _cleanup_broker_outputs(asr_details, studio)
            asr_details = []
            if asr_provider == "openrouter":
                raise VideoEditorError(f"video_openrouter_asr_failed:{broker_error}") from exc
    if asr_used != "openrouter":
        _run([
            py, str(ENGINE_ROOT / "scripts" / "transcribe.py"), *source_files,
            "--studio", str(studio), "--lang", language, "--model", str(selected_model),
        ], cwd=ENGINE_ROOT)
    return asr_used, asr_details, broker_error


def prepare(sources: list[str], language: str = "ru", pacing: str = "punchy", aspect: str = "9:16", transcription_quality: str = "fast", asr_provider: str = "auto") -> dict[str, Any]:
    if not runtime_ready():
        raise VideoEditorError("video_editor_runtime_not_ready")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 8:
        raise VideoEditorError("video_sources_count_invalid")
    language = str(language or "ru").strip()
    if not LANG_RE.fullmatch(language):
        raise VideoEditorError("video_language_invalid")
    if pacing not in {"punchy", "balanced", "restrained"}:
        raise VideoEditorError("video_pacing_invalid")
    if aspect not in {"9:16", "1:1", "16:9"}:
        raise VideoEditorError("video_aspect_invalid")
    if transcription_quality not in {"fast", "quality"}:
        raise VideoEditorError("video_transcription_quality_invalid")
    if asr_provider not in {"auto", "openrouter", "local"}:
        raise VideoEditorError("video_asr_provider_invalid")
    selected_model = model_path(transcription_quality)
    if not selected_model.is_file():
        raise VideoEditorError("video_transcription_model_missing")

    validated = [validate_source_path(item) for item in sources]
    cleanup_old_jobs()
    job_id = uuid.uuid4().hex[:12]
    job = jobs_root() / job_id
    job.mkdir(mode=0o700)
    src_dir = job / "sources"
    studio = job / "studio"
    src_dir.mkdir(mode=0o700)
    studio.mkdir(mode=0o700)
    (studio / "silences").mkdir(mode=0o700)

    meta_sources: dict[str, Any] = {}
    for idx, src in enumerate(validated, 1):
        alias = f"source_{idx:02d}"
        target = src_dir / f"{alias}{src.suffix.lower()}"
        _copy_source(src, target)
        info = _probe(target)
        meta_sources[alias] = {
            "file": str(target),
            "filename": target.name,
            "original_name": src.name,
            **info,
        }

    _write_profile(studio, language, pacing, aspect, selected_model)
    global_taste = taste_path()
    if global_taste.is_file():
        shutil.copy2(global_taste, studio / "taste.md")
        os.chmod(studio / "taste.md", 0o600)
    else:
        (studio / "taste.md").write_text("# Taste memory\n\n", encoding="utf-8")
        os.chmod(studio / "taste.md", 0o600)

    meta: dict[str, Any] = {
        "job_id": job_id,
        "state": "preparing",
        "created_at": int(time.time()),
        "engine_commit": ENGINE_COMMIT,
        "language": language,
        "pacing": pacing,
        "aspect": aspect,
        "model": str(selected_model),
        "transcription_quality": transcription_quality,
        "asr_provider_requested": asr_provider,
        "sources": meta_sources,
    }
    _atomic_json(job / "job.json", meta)

    py = shutil.which("python3") or "python3"
    try:
        with runtime_lock():
            source_files = [item["file"] for item in meta_sources.values()]
            asr_used, asr_details, broker_error = _transcribe_sources(
                source_files, studio, language, selected_model, asr_provider, py
            )
            meta["asr_provider"] = asr_used
            meta["asr_details"] = asr_details
            if broker_error:
                meta["asr_fallback_reason"] = broker_error
            _atomic_json(job / "job.json", meta)
            for alias, item in meta_sources.items():
                proc = _run([
                    py, str(ENGINE_ROOT / "scripts" / "silences.py"), item["file"], "--json",
                ], cwd=ENGINE_ROOT, timeout=300)
                silence_file = studio / "silences" / f"{alias}.json"
                silence_file.write_text(proc.stdout, encoding="utf-8")
                os.chmod(silence_file, 0o600)
            _run([py, str(ENGINE_ROOT / "scripts" / "pack.py"), "--studio", str(studio)], cwd=ENGINE_ROOT, timeout=120)
    except Exception:
        meta["state"] = "failed"
        _atomic_json(job / "job.json", meta)
        raise

    meta["state"] = "prepared"
    meta["prepared_at"] = int(time.time())
    _atomic_json(job / "job.json", meta)
    takes = (studio / "takes.md").read_text(encoding="utf-8").splitlines()
    return {
        "ok": True,
        "job_id": job_id,
        "state": meta["state"],
        "sources": [{"source": alias, **{k: v for k, v in item.items() if k in {"filename", "original_name", "duration", "width", "height", "fps", "bytes"}}} for alias, item in meta_sources.items()],
        "takes": "\n".join(takes[:160]),
        "takes_total_lines": len(takes),
        "silences": [_silence_summary(studio, alias) for alias in meta_sources],
        "taste": (studio / "taste.md").read_text(encoding="utf-8")[:5000],
        "asr_provider": meta.get("asr_provider", "local"),
        "asr_details": meta.get("asr_details", []),
        "next": "Read more takes if needed, then author an EDL and call video_editor_render. Cut edges should align with phrase/silence boundaries.",
    }


def read_takes(job_id: str, start_line: int = 1, limit: int = 160, source: str | None = None) -> dict[str, Any]:
    job = _job_dir(job_id)
    meta = _read_meta(job)
    path = job / "studio" / "takes.md"
    if not path.is_file():
        raise VideoEditorError("video_takes_not_ready")
    lines = path.read_text(encoding="utf-8").splitlines()
    start = max(1, int(start_line or 1))
    count = min(240, max(1, int(limit or 160)))
    chunk = lines[start - 1:start - 1 + count]
    if source and source not in (meta.get("sources") or {}):
        raise VideoEditorError("video_source_alias_unknown")
    aliases = [source] if source else list((meta.get("sources") or {}).keys())
    return {
        "ok": True,
        "job_id": job_id,
        "start_line": start,
        "end_line": start + len(chunk) - 1,
        "total_lines": len(lines),
        "text": "\n".join(chunk),
        "silences": [_silence_summary(job / "studio", alias) for alias in aliases],
    }


def _normalize_ranges(meta: dict[str, Any], ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sources = meta.get("sources") or {}
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= 80:
        raise VideoEditorError("video_edl_ranges_invalid")
    filename_to_alias = {item.get("filename"): alias for alias, item in sources.items()}
    clean: list[dict[str, Any]] = []
    for raw in ranges:
        if not isinstance(raw, dict):
            raise VideoEditorError("video_edl_range_invalid")
        source = str(raw.get("source") or "").strip()
        source = filename_to_alias.get(source, source)
        if source not in sources:
            raise VideoEditorError("video_source_alias_unknown")
        try:
            start = float(raw.get("start"))
            end = float(raw.get("end"))
        except (TypeError, ValueError) as exc:
            raise VideoEditorError("video_edl_time_invalid") from exc
        duration = float(sources[source].get("duration") or 0)
        if start < 0 or end <= start or end > duration + 0.05 or end - start < 0.10:
            raise VideoEditorError("video_edl_time_out_of_bounds")
        item: dict[str, Any] = {"source": source, "start": round(start, 3), "end": round(min(end, duration), 3)}
        for key in ("beat", "reason"):
            value = str(raw.get(key) or "").strip()
            if value:
                item[key] = value[:240]
        if raw.get("zoom") is not None:
            zoom = float(raw["zoom"])
            if not 1.0 <= zoom <= 1.8:
                raise VideoEditorError("video_zoom_invalid")
            item["zoom"] = round(zoom, 3)
        if raw.get("zoom_y") is not None:
            zoom_y = float(raw["zoom_y"])
            if not 0.0 <= zoom_y <= 1.0:
                raise VideoEditorError("video_zoom_y_invalid")
            item["zoom_y"] = round(zoom_y, 3)
        clean.append(item)
    return clean



def _audio_peak_db(path: Path) -> float | None:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    match = re.search(r"max_volume:\s*(-?[\d.]+) dB", proc.stderr)
    return float(match.group(1)) if match else None


def _normalize_voice_level(path: Path, target_peak: float = -4.5) -> dict[str, Any]:
    before = _audio_peak_db(path)
    if before is None:
        return {"applied": False, "reason": "no_audio_peak"}
    # Upstream sound rules define healthy talking-head voice peaks as -3..-6 dBFS.
    if -6.0 <= before <= -3.0:
        return {"applied": False, "before_peak_db": before, "after_peak_db": before}
    gain = max(-12.0, min(18.0, target_peak - before))
    temp = path.with_name(path.stem + ".voice-normalized" + path.suffix)
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
         "-c:v", "copy", "-af", f"volume={gain:.3f}dB,alimiter=limit=0.95",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
         "-movflags", "+faststart", str(temp)],
        capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0 or not temp.is_file():
        temp.unlink(missing_ok=True)
        raise VideoEditorError("video_voice_normalization_failed")
    os.replace(temp, path)
    after = _audio_peak_db(path)
    return {"applied": True, "before_peak_db": before, "gain_db": round(gain,3), "after_peak_db": after}

def render(job_id: str, ranges: list[dict[str, Any]], speed: float = 1.0, grade: str = "none", preview: bool = False) -> dict[str, Any]:
    if not runtime_ready():
        raise VideoEditorError("video_editor_runtime_not_ready")
    job = _job_dir(job_id)
    meta = _read_meta(job)
    if meta.get("state") not in {"prepared", "rendered", "needs_fix", "verified", "enriched", "mastered"}:
        raise VideoEditorError("video_job_not_ready_for_render")
    clean_ranges = _normalize_ranges(meta, ranges)
    try:
        speed = float(speed)
    except (TypeError, ValueError) as exc:
        raise VideoEditorError("video_speed_invalid") from exc
    if not 0.75 <= speed <= 1.5:
        raise VideoEditorError("video_speed_invalid")
    if grade not in {"none", "neutral_punch", "warm_lift"}:
        raise VideoEditorError("video_grade_invalid")

    studio = job / "studio"
    edl = {
        "sources": {alias: item["file"] for alias, item in (meta.get("sources") or {}).items()},
        "ranges": clean_ranges,
        "pad": {"in": 0.05, "out": 0.08},
        "speed": round(speed, 4),
        "grade": grade,
    }
    _atomic_json(studio / "edl.json", edl)
    py = shutil.which("python3") or "python3"
    verify_provider = "local"
    verify_log = ""
    with runtime_lock():
        cmd = [py, str(ENGINE_ROOT / "scripts" / "render.py"), "--studio", str(studio)]
        if preview:
            cmd.append("--preview")
        render_proc = _run(cmd, cwd=ENGINE_ROOT)
        rendered_path = studio / ("preview.mp4" if preview else "cut.mp4")
        audio_normalization = _normalize_voice_level(rendered_path)
        broker_failed = None
        if meta.get("asr_provider") == "openrouter":
            try:
                report = seam_verify.verify(studio, str(meta.get("language") or "ru"))
                verify_provider = "openrouter"
                verify_log = f"OpenRouter seam verification: {len(report.get('seams') or [])} seam(s)"
            except Exception as exc:
                broker_failed = str(exc)[:160]
                if meta.get("asr_provider_requested") == "openrouter":
                    raise VideoEditorError(f"video_openrouter_verify_failed:{broker_failed}") from exc
        if verify_provider != "openrouter":
            verify_proc = _run([
                py, str(ENGINE_ROOT / "scripts" / "verify.py"), "--studio", str(studio),
            ], cwd=ENGINE_ROOT, allowed=(0, 2))
            verify_log = _safe_tail(verify_proc.stdout, 4000)
            if broker_failed:
                verify_log = f"OpenRouter verify fallback: {broker_failed}\n" + verify_log

    report_path = studio / "verify" / "report.json"
    if not report_path.is_file():
        raise VideoEditorError("video_verify_report_missing")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    problems = report.get("problems") or []
    meta["state"] = "needs_fix" if problems else "verified"
    meta["last_render_at"] = int(time.time())
    meta["output"] = report.get("video")
    meta["preview"] = bool(preview)
    _atomic_json(job / "job.json", meta)
    seam_reports = []
    for seam in report.get("seams") or []:
        seam_reports.append({
            "at": seam.get("at"),
            "text": seam.get("text"),
            "repeated": seam.get("repeated"),
            "audio": seam.get("audio"),
            "frame": seam.get("frame"),
        })
    return {
        "ok": not bool(problems),
        "job_id": job_id,
        "state": meta["state"],
        "output": report.get("video"),
        "duration": report.get("duration"),
        "mechanical_clean": not bool(problems),
        "problems": problems,
        "seams": seam_reports,
        "render_log": _safe_tail(render_proc.stdout, 2000),
        "verify_log": verify_log,
        "verify_provider": verify_provider,
        "audio_normalization": audio_normalization,
        "judgement_gate": "Mechanical verification is not semantic approval. Read every seam transcript and ensure no clause starts/ends mid-thought before presenting the cut.",
    }


def status(job_id: str) -> dict[str, Any]:
    job = _job_dir(job_id)
    meta = _read_meta(job)
    studio = job / "studio"
    files: dict[str, str] = {}
    for name in ("takes.md", "edl.json", "timeline.json", "cut.mp4", "preview.mp4", "cut_graded.mp4", "captions.json", "cards.json", "proof.json", "sfx.json"):
        path = studio / name
        if path.is_file():
            files[name] = str(path)
    for name in ("master.mp4", "final.mp4", "manifest.json", "post.md"):
        path = studio / "out" / name
        if path.is_file():
            files["out/" + name] = str(path)
    visual = studio / "visual"
    if visual.is_dir() and not visual.is_symlink():
        files["visual"] = str(visual)
    thumbs = studio / "out" / "thumbnails"
    if thumbs.is_dir():
        files["out/thumbnails"] = str(thumbs)
    report = studio / "verify" / "report.json"
    if report.is_file():
        files["verify/report.json"] = str(report)
    return {
        "ok": True,
        "job_id": job_id,
        "state": meta.get("state"),
        "language": meta.get("language"),
        "pacing": meta.get("pacing"),
        "aspect": meta.get("aspect"),
        "asr_provider": meta.get("asr_provider"),
        "look": meta.get("look"),
        "transcription_quality": meta.get("transcription_quality", "quality"),
        "sources": [{"source": alias, "filename": item.get("filename"), "original_name": item.get("original_name"), "duration": item.get("duration")} for alias, item in (meta.get("sources") or {}).items()],
        "files": files,
    }


def feedback(area: str, instruction: str, said: str = "") -> dict[str, Any]:
    area = str(area or "general").strip().lower()
    allowed = {"general", "cutting", "captions", "motion", "sound", "proof", "framing", "hooks"}
    if area not in allowed:
        raise VideoEditorError("video_feedback_area_invalid")
    instruction = " ".join(str(instruction or "").split()).strip()
    said = " ".join(str(said or "").split()).strip()
    if not 3 <= len(instruction) <= 500:
        raise VideoEditorError("video_feedback_instruction_invalid")
    if len(said) > 500:
        raise VideoEditorError("video_feedback_quote_too_long")
    path = taste_path()
    if not path.exists():
        path.write_text("# Taste memory\n\n", encoding="utf-8")
        os.chmod(path, 0o600)
    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    entry = f"- [{stamp}] **{area}**: {instruction}"
    if said:
        entry += f" | said: {said}"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry + "\n")
    os.chmod(path, 0o600)
    return {"ok": True, "area": area, "instruction": instruction, "taste_file": str(path)}
