from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import engine, final_verify

SFX_ROOT = engine.RUNTIME_ROOT / "sfx"
NPM_CACHE = engine.RUNTIME_ROOT / "npm-cache"
CAPTURE_PYTHON = engine.RUNTIME_ROOT / "capture-venv" / "bin" / "python"
SAFE_CAPTURE = engine.RUNTIME_ROOT / "capture" / "safe_capture.py"
PLAYWRIGHT_BROWSERS = engine.RUNTIME_ROOT / "playwright-browsers"
HYPERFRAMES_BROWSER = engine.RUNTIME_ROOT / "hyperframes-home" / ".cache" / "hyperframes" / "chrome" / "chrome-headless-shell" / "linux-152.0.7977.30" / "chrome-headless-shell-linux64" / "chrome-headless-shell"
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
ASSET_RE = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
HYPERFRAMES_VERSION = "0.8.30"
GSAP_VERSION = "3.14.2"
GSAP_RUNTIME = engine.RUNTIME_ROOT / "hyperframes" / "node_modules" / "gsap" / "dist" / "gsap.min.js"
GSAP_CDN = f"https://cdn.jsdelivr.net/npm/gsap@{GSAP_VERSION}/dist/gsap.min.js"
REMOTE_SCRIPT_RE = re.compile(r"<script\b[^>]*\bsrc=[\"']https?://", re.IGNORECASE)


def _ctx(job_id: str) -> tuple[Path, dict[str, Any], Path]:
    job = engine._job_dir(job_id)
    meta = engine._read_meta(job)
    studio = job / "studio"
    return job, meta, studio


def _duration(studio: Path) -> float:
    path = studio / "timeline.json"
    if not path.is_file():
        raise engine.VideoEditorError("video_timeline_missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    return float(data.get("predicted_duration") or 0)



def _localize_gsap(studio: Path, runtime: Path = GSAP_RUNTIME, expected_uid: int = 0) -> Path:
    composition = studio / "composition"
    index = composition / "index.html"
    if not index.is_file() or index.is_symlink():
        raise engine.VideoEditorError("video_composition_html_missing")
    try:
        info = runtime.lstat()
    except OSError as exc:
        raise engine.VideoEditorError("video_gsap_runtime_missing") from exc
    if runtime.is_symlink() or not runtime.is_file() or info.st_uid != expected_uid or (info.st_mode & 0o022):
        raise engine.VideoEditorError("video_gsap_runtime_unsafe")
    html = index.read_text(encoding="utf-8")
    if html.count(GSAP_CDN) != 1:
        raise engine.VideoEditorError("video_gsap_cdn_tag_unexpected")
    target = composition / "gsap.min.js"
    shutil.copy2(runtime, target)
    target.chmod(0o600)
    html = html.replace(GSAP_CDN, "./gsap.min.js", 1)
    if REMOTE_SCRIPT_RE.search(html):
        target.unlink(missing_ok=True)
        raise engine.VideoEditorError("video_remote_script_blocked")
    index.write_text(html, encoding="utf-8")
    index.chmod(0o600)
    return target


def _compose_self_contained(studio: Path, py: str, npm_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    generate = engine._run(
        [py, str(engine.ENGINE_ROOT / "scripts" / "compose.py"), "--studio", str(studio)],
        cwd=engine.ENGINE_ROOT,
        timeout=900,
        extra_env=npm_env,
    )
    _localize_gsap(studio)
    composition = studio / "composition"
    pin = f"hyperframes@{HYPERFRAMES_VERSION}"
    engine._run(
        ["npx", "--yes", pin, "check"],
        cwd=composition,
        timeout=300,
        extra_env=npm_env,
    )
    out = studio / "out"
    out.mkdir(parents=True, exist_ok=True)
    master = out / "master.mp4"
    render = engine._run(
        ["npx", "--yes", pin, "render", ".", "-o", str(master.resolve())],
        cwd=composition,
        timeout=1800,
        extra_env=npm_env,
    )
    render.stdout = (generate.stdout or "") + "\n" + (render.stdout or "")
    return render

def captions(job_id: str, keywords: list[str] | None = None, fixes: dict[str, str] | None = None) -> dict[str, Any]:
    _, meta, studio = _ctx(job_id)
    if meta.get("state") not in {"verified", "enriched", "mastered"}:
        raise engine.VideoEditorError("video_cut_must_be_verified_first")
    cmd = [shutil.which("python3") or "python3", str(engine.ENGINE_ROOT / "scripts" / "captions.py"), "--studio", str(studio)]
    clean_keywords = []
    for item in keywords or []:
        value = " ".join(str(item).split()).strip()
        if value:
            clean_keywords.append(value[:60])
    if clean_keywords:
        cmd += ["--keywords", ",".join(clean_keywords[:30])]
    for wrong, right in (fixes or {}).items():
        a = " ".join(str(wrong).split()).strip()[:80]
        b = " ".join(str(right).split()).strip()[:80]
        if a and b:
            cmd += ["--fix", f"{a}={b}"]
    proc = engine._run(cmd, cwd=engine.ENGINE_ROOT, timeout=180)
    path = studio / "captions.json"
    if not path.is_file():
        raise engine.VideoEditorError("video_captions_missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "ok": True,
        "job_id": job_id,
        "proofread": bool(data.get("proofread")),
        "chunks": data.get("chunks") or [],
        "log": engine._safe_tail(proc.stdout, 1800),
        "next": "Read every caption chunk, correct ASR/product names, then call video_editor_caption_approve before master rendering.",
    }


def caption_approve(job_id: str, corrections: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    _, meta, studio = _ctx(job_id)
    if meta.get("state") not in {"verified", "enriched", "mastered"}:
        raise engine.VideoEditorError("video_cut_must_be_verified_first")
    path = studio / "captions.json"
    if not path.is_file():
        raise engine.VideoEditorError("video_captions_missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    chunks = data.get("chunks") or []
    for item in corrections or []:
        if not isinstance(item, dict):
            raise engine.VideoEditorError("video_caption_correction_invalid")
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError) as exc:
            raise engine.VideoEditorError("video_caption_index_invalid") from exc
        if index < 0 or index >= len(chunks):
            raise engine.VideoEditorError("video_caption_index_out_of_bounds")
        text = " ".join(str(item.get("text") or "").split()).strip()
        if not text or len(text) > 220:
            raise engine.VideoEditorError("video_caption_text_invalid")
        tokens = text.split()
        emphasis = item.get("emphasis") or []
        emphasis_ids = set()
        for raw in emphasis:
            try:
                pos = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= pos < len(tokens):
                emphasis_ids.add(pos)
        chunks[index]["text"] = text
        chunks[index]["words"] = [
            {"text": token, "emphasis": pos in emphasis_ids}
            for pos, token in enumerate(tokens)
        ]
    data["proofread"] = True
    data["proofread_by"] = "hermes"
    data["proofread_at"] = int(time.time())
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
    meta["state"] = "enriched"
    engine._atomic_json(engine._job_dir(job_id) / "job.json", meta)
    return {"ok": True, "job_id": job_id, "proofread": True, "chunks": chunks}


def cards(job_id: str, cards: list[dict[str, Any]]) -> dict[str, Any]:
    _, meta, studio = _ctx(job_id)
    if meta.get("state") not in {"verified", "enriched", "mastered"}:
        raise engine.VideoEditorError("video_cut_must_be_verified_first")
    if not isinstance(cards, list) or len(cards) > 30:
        raise engine.VideoEditorError("video_cards_invalid")
    total = _duration(studio)
    clean = []
    for raw in cards:
        if not isinstance(raw, dict):
            raise engine.VideoEditorError("video_card_invalid")
        try:
            start = float(raw.get("start"))
            duration = float(raw.get("duration"))
        except (TypeError, ValueError) as exc:
            raise engine.VideoEditorError("video_card_time_invalid") from exc
        if start < 0 or duration < 0.3 or start + duration > total + 0.05:
            raise engine.VideoEditorError("video_card_time_out_of_bounds")
        style = str(raw.get("style") or "split").strip().lower()
        if style not in {"split", "band", "full"}:
            raise engine.VideoEditorError("video_card_style_invalid")
        big = " ".join(str(raw.get("big") or "").split()).strip()
        if not big or len(big) > 140:
            raise engine.VideoEditorError("video_card_headline_invalid")
        item: dict[str, Any] = {"start": round(start, 3), "duration": round(duration, 3), "big": big}
        if style == "band":
            item["style"] = "band"
        elif style == "full":
            item["full"] = True
        for key, limit in (("kicker", 80), ("sub", 180)):
            value = " ".join(str(raw.get(key) or "").split()).strip()
            if value:
                item[key] = value[:limit]
        rows = []
        for row in (raw.get("items") or [])[:6]:
            if not isinstance(row, dict):
                continue
            title = " ".join(str(row.get("title") or "").split()).strip()[:90]
            note = " ".join(str(row.get("note") or "").split()).strip()[:90]
            if title:
                rows.append({"title": title, "note": note})
        if rows:
            item["items"] = rows
        colour = str(raw.get("brand_colour") or "").strip()
        if colour:
            if not HEX_RE.fullmatch(colour):
                raise engine.VideoEditorError("video_card_colour_invalid")
            item["brand_colour"] = colour
        clean.append(item)
    path = studio / "cards.json"
    path.write_text(json.dumps({"cards": clean}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
    return {"ok": True, "job_id": job_id, "cards": clean}


def capture_page(job_id: str, url: str, name: str, find: list[str] | None = None) -> dict[str, Any]:
    _, meta, studio = _ctx(job_id)
    if meta.get("state") not in {"verified", "enriched", "mastered"}:
        raise engine.VideoEditorError("video_cut_must_be_verified_first")
    if not ASSET_RE.fullmatch(str(name or "")):
        raise engine.VideoEditorError("video_proof_asset_name_invalid")
    if not CAPTURE_PYTHON.is_file() or not SAFE_CAPTURE.is_file():
        raise engine.VideoEditorError("video_capture_runtime_not_ready")
    cmd = [str(CAPTURE_PYTHON), str(SAFE_CAPTURE), str(url), "--studio", str(studio), "--name", str(name)]
    for needle in (find or [])[:12]:
        value = " ".join(str(needle).split()).strip()
        if value:
            cmd += ["--find", value[:120]]
    with engine.runtime_lock():
        proc = engine._run(cmd, timeout=90, extra_env={"PLAYWRIGHT_BROWSERS_PATH": str(PLAYWRIGHT_BROWSERS)})
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        payload = json.loads(lines[-1])
    except Exception as exc:
        raise engine.VideoEditorError("video_capture_invalid_response") from exc
    if not payload.get("ok"):
        raise engine.VideoEditorError("video_capture_failed")
    return {"ok": True, "job_id": job_id, **payload}


def proof(job_id: str, beats: list[dict[str, Any]]) -> dict[str, Any]:
    _, meta, studio = _ctx(job_id)
    if meta.get("state") not in {"verified", "enriched", "mastered"}:
        raise engine.VideoEditorError("video_cut_must_be_verified_first")
    if not isinstance(beats, list) or len(beats) > 30:
        raise engine.VideoEditorError("video_proof_beats_invalid")
    total = _duration(studio)
    clean = []
    for raw in beats:
        if not isinstance(raw, dict):
            raise engine.VideoEditorError("video_proof_beat_invalid")
        asset = str(raw.get("asset") or "").strip()
        if not ASSET_RE.fullmatch(asset):
            raise engine.VideoEditorError("video_proof_asset_name_invalid")
        meta_path = studio / "assets" / "proof" / f"{asset}.json"
        if not meta_path.is_file():
            raise engine.VideoEditorError("video_proof_asset_missing")
        page_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        try:
            start = float(raw.get("start")); duration = float(raw.get("duration"))
        except (TypeError, ValueError) as exc:
            raise engine.VideoEditorError("video_proof_time_invalid") from exc
        if start < 0 or duration < 0.4 or start + duration > total + 0.05:
            raise engine.VideoEditorError("video_proof_time_out_of_bounds")
        action = str(raw.get("action") or "zoom").strip().lower()
        if action not in {"zoom", "scroll"}:
            raise engine.VideoEditorError("video_proof_action_invalid")
        item: dict[str, Any] = {"asset": asset, "start": round(start, 3), "duration": round(duration, 3), "action": action}
        target = " ".join(str(raw.get("target") or "").split()).strip()
        if target:
            targets = page_meta.get("targets") or {}
            if target not in targets:
                raise engine.VideoEditorError("video_proof_target_missing")
            item["target"] = target
        if action == "scroll":
            page_h = float((page_meta.get("page_size") or [0, 0])[1] or 0)
            try:
                from_y = float(raw.get("from_y", 0)); to_y = float(raw.get("to_y", page_h))
            except (TypeError, ValueError) as exc:
                raise engine.VideoEditorError("video_proof_scroll_invalid") from exc
            if from_y < 0 or to_y < 0 or from_y > page_h or to_y > page_h:
                raise engine.VideoEditorError("video_proof_scroll_out_of_bounds")
            item["from_y"] = round(from_y, 1); item["to_y"] = round(to_y, 1)
        if bool(raw.get("highlight")):
            if not target:
                raise engine.VideoEditorError("video_proof_highlight_needs_target")
            item["highlight"] = True
            item["highlight_at"] = round(max(0.0, min(duration, float(raw.get("highlight_at", 0.7)))), 3)
        clean.append(item)
    path = studio / "proof.json"
    path.write_text(json.dumps({"beats": clean}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
    return {"ok": True, "job_id": job_id, "beats": clean}


def _source_peak_db(path: str, duration: float, volume: float = 1.0) -> float | None:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-t", f"{max(0.1, float(duration)):.3f}",
         "-af", f"volume={max(0.001, float(volume)):.6f},volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    match = re.search(r"max_volume:\s*(-?[\d.]+) dB", proc.stderr)
    return float(match.group(1)) if match else None


def _tune_sounds(sounds: list[dict[str, Any]], gain_scale: float, target_db: float = -14.0) -> list[dict[str, Any]]:
    tuned = []
    for raw in sounds:
        item = dict(raw)
        raw_peak = _source_peak_db(str(item.get("file") or ""), float(item.get("duration") or 0.6), 1.0)
        if raw_peak is None:
            raise engine.VideoEditorError("video_sfx_peak_unmeasurable")
        base = 10 ** ((target_db - raw_peak) / 20.0)
        volume = max(0.02, min(1.0, base * gain_scale))
        item["base_volume"] = round(base, 6)
        item["volume"] = round(volume, 4)
        item["target_peak_db"] = target_db
        item["predicted_peak_db"] = _source_peak_db(str(item.get("file") or ""), float(item.get("duration") or 0.6), volume)
        tuned.append(item)
    return tuned


def _sound_gate(studio: Path) -> dict[str, Any]:
    sfx_path = studio / "sfx.json"
    cut = studio / "cut.mp4"
    if not sfx_path.is_file() or not cut.is_file():
        raise engine.VideoEditorError("video_sound_gate_inputs_missing")
    voice_peak = engine._audio_peak_db(cut)
    if voice_peak is None:
        raise engine.VideoEditorError("video_voice_peak_unmeasurable")
    sounds = json.loads(sfx_path.read_text(encoding="utf-8")).get("sounds") or []
    checks, problems = [], []
    for item in sounds:
        peak = _source_peak_db(str(item.get("file") or ""), float(item.get("duration") or 0.6), float(item.get("volume") or 0.0))
        status = "ok"
        if peak is None:
            status = "unmeasurable"; problems.append(f"{item.get('category')}: peak unmeasurable")
        elif peak > -9.0 or peak > voice_peak + 0.5:
            status = "too_loud"; problems.append(f"{item.get('category')}: {peak:.1f} dBFS too loud")
        elif peak < -19.0:
            status = "too_quiet"; problems.append(f"{item.get('category')}: {peak:.1f} dBFS too quiet")
        checks.append({"category": item.get("category"), "hit_at": item.get("hit_at"), "peak_db": peak, "voice_peak_db": voice_peak, "status": status})
    return {"ok": not problems, "voice_peak_db": voice_peak, "checks": checks, "problems": problems}


def sound(job_id: str, gain_scale: float = 1.0) -> dict[str, Any]:
    _, meta, studio = _ctx(job_id)
    if meta.get("state") not in {"verified", "enriched", "mastered"}:
        raise engine.VideoEditorError("video_cut_must_be_verified_first")
    if not (SFX_ROOT / "index.json").is_file():
        raise engine.VideoEditorError("video_sfx_runtime_not_ready")
    try:
        gain = float(gain_scale)
    except (TypeError, ValueError) as exc:
        raise engine.VideoEditorError("video_sfx_gain_invalid") from exc
    if not 0.5 <= gain <= 1.5:
        raise engine.VideoEditorError("video_sfx_gain_invalid")
    path = studio / "sfx.json"
    proc_log = ""
    existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    existing_sounds = existing.get("sounds") or []
    if not existing_sounds or not all("base_volume" in item for item in existing_sounds):
        proc = engine._run([
            shutil.which("python3") or "python3", str(engine.ENGINE_ROOT / "scripts" / "sfx.py"),
            "plan", "--studio", str(studio), "--library", str(SFX_ROOT),
        ], cwd=engine.ENGINE_ROOT, timeout=300)
        proc_log = engine._safe_tail(proc.stdout, 2200)
        existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"sounds": []}
        existing_sounds = existing.get("sounds") or []
    tuned = _tune_sounds(existing_sounds, gain)
    path.write_text(json.dumps({"sounds": tuned}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
    return {"ok": True, "job_id": job_id, "sounds": tuned, "planned": bool(proc_log), "log": proc_log}


def _audio_path(value: str) -> Path:
    raw = Path(str(value or "").strip()).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise engine.VideoEditorError("video_music_path_invalid")
    try:
        path = raw.resolve(strict=True)
    except OSError as exc:
        raise engine.VideoEditorError("video_music_not_found") from exc
    roots = [(engine.hermes_home() / "cache" / "audio").resolve(), (Path.home() / "workspace").resolve()]
    if not path.is_file() or not any(engine._is_within(path, root) for root in roots) or path.suffix.lower() not in AUDIO_EXTS:
        raise engine.VideoEditorError("video_music_outside_profile_roots")
    return path



def _probe_video(path: Path) -> tuple[int, int, float]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, errors="replace",
    )
    try:
        data = json.loads(proc.stdout)
        stream = next(item for item in data.get("streams", []) if item.get("width") and item.get("height"))
        return int(stream["width"]), int(stream["height"]), float((data.get("format") or {}).get("duration") or 0)
    except Exception as exc:
        raise engine.VideoEditorError("video_delivery_probe_failed") from exc


def _variant(master: Path, target: Path, width: int, height: int) -> str:
    sw, sh, _ = _probe_video(master)
    same_ratio = abs((sw / sh) - (width / height)) < 0.01
    if same_ratio:
        vf = f"scale={width}:{height}:flags=lanczos"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(master), "-vf", vf,
               "-c:v", "libx264", "-crf", "19", "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "copy", str(target)]
        style = "scaled"
    else:
        fc = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},gblur=sigma=28[bg2];"
            f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fg2];"
            f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2"
        )
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(master), "-filter_complex", fc,
               "-c:v", "libx264", "-crf", "19", "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "copy", str(target)]
        style = "blurred_background"
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0 or not target.is_file():
        raise engine.VideoEditorError("video_variant_render_failed")
    os.chmod(target, 0o600)
    return style


def _delivery_package(studio: Path, master: Path, aspects: list[str]) -> dict[str, Any]:
    out = studio / "out"
    out.mkdir(parents=True, exist_ok=True)
    sw, sh, duration = _probe_video(master)
    sizes = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}
    manifest: dict[str, Any] = {"master": str(master), "duration": round(duration, 2), "source_size": [sw, sh], "variants": {}}
    for aspect in aspects:
        if aspect not in sizes:
            continue
        tw, th = sizes[aspect]
        dst = out / f"{master.stem}_{aspect.replace(':','x')}.mp4"
        if (sw, sh) == (tw, th):
            manifest["variants"][aspect] = {"path": str(master), "style": "native"}
        else:
            style = _variant(master, dst, tw, th)
            manifest["variants"][aspect] = {"path": str(dst), "style": style}

    seams = []
    timeline_path = studio / "timeline.json"
    if timeline_path.is_file():
        seams = [float(x) for x in (json.loads(timeline_path.read_text(encoding="utf-8")).get("seams") or [])]
    blocked: list[tuple[float, float]] = []
    proof_path = studio / "proof.json"
    if proof_path.is_file():
        for item in json.loads(proof_path.read_text(encoding="utf-8")).get("beats") or []:
            start = float(item.get("start") or 0); blocked.append((start, start + float(item.get("duration") or 0)))
    cards_path = studio / "cards.json"
    if cards_path.is_file():
        for item in json.loads(cards_path.read_text(encoding="utf-8")).get("cards") or []:
            if item.get("full"):
                start = float(item.get("start") or 0); blocked.append((start, start + float(item.get("duration") or 0)))

    def usable(point: float) -> bool:
        return not any(start - 0.15 <= point <= end + 0.15 for start, end in blocked)

    thumbs = out / "thumbnails"
    thumbs.mkdir(parents=True, exist_ok=True)
    for old in thumbs.glob("*.png"):
        old.unlink()
    candidates = []
    for index in range(6):
        point = duration * (index + 0.5) / 6.0
        if any(abs(point - seam) < 0.35 for seam in seams) or not usable(point):
            for step in [0.2 * n for n in range(1, 31)]:
                choices = [point + step, point - step]
                found = next((x for x in choices if 0.15 < x < duration - 0.1 and usable(x) and not any(abs(x-seam) < 0.35 for seam in seams)), None)
                if found is not None:
                    point = found; break
        point = max(0.05, min(point, max(0.05, duration - 0.1)))
        target = thumbs / f"t{index}_{point:.1f}s.png"
        proc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{point:.3f}", "-i", str(master), "-frames:v", "1", str(target)], capture_output=True, text=True, errors="replace")
        if proc.returncode == 0 and target.is_file():
            target.chmod(0o600); candidates.append({"path": str(target), "at": round(point, 2)})
    manifest["thumbnails"] = candidates

    lines = []
    captions_path = studio / "captions.json"
    if captions_path.is_file():
        text = " ".join(str(item.get("text") or "") for item in json.loads(captions_path.read_text(encoding="utf-8")).get("chunks") or [])
        for line in re.split(r"(?<=[.?!])\s+", text):
            line = " ".join(line.split()).strip()
            if 20 <= len(line) <= 180:
                lines.append(line)
    manifest["spoken_lines"] = lines[:8]
    post_path = out / "post.md"
    if not post_path.is_file():
        post_path.write_text("# Post copy\n\n## Caption\n\n\n## Titles\n\n- \n- \n- \n\n## Lines actually spoken\n\n" + ("\n".join("- " + line for line in lines[:8]) or "_(no caption lines)_") + "\n", encoding="utf-8")
        post_path.chmod(0o600)
    manifest["post_scaffold"] = str(post_path)
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_path.chmod(0o600)
    return manifest


def master(job_id: str, music_path: str = "", music_level: float = 0.12, aspects: list[str] | None = None) -> dict[str, Any]:
    job, meta, studio = _ctx(job_id)
    if meta.get("state") not in {"verified", "enriched", "mastered"}:
        raise engine.VideoEditorError("video_cut_must_be_verified_first")
    # A reviewed preview is not the final render. Promote the exact approved EDL
    # to full-quality cut.mp4 and re-run seam verification before enrichment.
    if not (studio / "cut.mp4").is_file():
        edl_path = studio / "edl.json"
        if not edl_path.is_file():
            raise engine.VideoEditorError("video_edl_missing")
        edl = json.loads(edl_path.read_text(encoding="utf-8"))
        promoted = engine.render(
            job_id,
            edl.get("ranges") or [],
            speed=float(edl.get("speed", 1.0)),
            grade=str(edl.get("grade") or "none"),
            preview=False,
        )
        if not promoted.get("ok"):
            return {
                "ok": False,
                "job_id": job_id,
                "state": promoted.get("state", "needs_fix"),
                "problems": promoted.get("problems") or [],
                "next": "Fix the approved EDL seam problems before final enrichment.",
            }
        job, meta, studio = _ctx(job_id)
    cap_path = studio / "captions.json"
    if not cap_path.is_file():
        raise engine.VideoEditorError("video_captions_missing")
    cap_data = json.loads(cap_path.read_text(encoding="utf-8"))
    if not cap_data.get("proofread"):
        raise engine.VideoEditorError("video_captions_not_approved")
    if not (studio / "sfx.json").is_file():
        sound(job_id)
    py = shutil.which("python3") or "python3"
    beat_proc = engine._run(
        [py, str(engine.ENGINE_ROOT / "scripts" / "beats.py"), "--studio", str(studio)],
        cwd=engine.ENGINE_ROOT, allowed=(0, 2), timeout=180,
    )
    if beat_proc.returncode != 0:
        return {
            "ok": False,
            "job_id": job_id,
            "state": "needs_design",
            "beat_log": engine._safe_tail(beat_proc.stdout, 5000),
            "next": "Add or retime cards/proof so there are no dead stretches, then call video_editor_master again.",
        }
    npm_env = {"npm_config_cache": str(NPM_CACHE), "npm_config_offline": "true", "PLAYWRIGHT_BROWSERS_PATH": str(PLAYWRIGHT_BROWSERS), "HYPERFRAMES_BROWSER_PATH": str(HYPERFRAMES_BROWSER)}
    with engine.runtime_lock():
        compose_proc = _compose_self_contained(studio, py, npm_env)
    master_path = studio / "out" / "master.mp4"
    if not master_path.is_file():
        raise engine.VideoEditorError("video_master_missing")
    sound_gate = _sound_gate(studio)
    if not sound_gate.get("ok"):
        return {
            "ok": False,
            "job_id": job_id,
            "state": "needs_sound_fix",
            "output": str(master_path),
            "sound_gate": sound_gate,
            "next": "Adjust video_editor_sound gain_scale and rerun master until every source peak is inside the -19..-9 dBFS window and below the normalized voice peak.",
        }
    final_path = master_path
    music_log = ""
    if music_path:
        music = _audio_path(music_path)
        try:
            level = float(music_level)
        except (TypeError, ValueError) as exc:
            raise engine.VideoEditorError("video_music_level_invalid") from exc
        if not 0.02 <= level <= 0.35:
            raise engine.VideoEditorError("video_music_level_invalid")
        final_path = studio / "out" / "final.mp4"
        music_proc = engine._run(
            [py, str(engine.ENGINE_ROOT / "scripts" / "music.py"), str(master_path), str(music), "-o", str(final_path), "--level", str(level)],
            cwd=engine.ENGINE_ROOT, timeout=900,
        )
        music_log = engine._safe_tail(music_proc.stdout, 2500)
        if "voice preserved" not in music_log:
            return {
                "ok": False, "job_id": job_id, "state": "needs_music_fix",
                "output": str(final_path), "music_log": music_log,
                "next": "Lower music_level and rerun master until the sidechain check reports voice preserved.",
            }
    clean_aspects = []
    for aspect in aspects or [meta.get("aspect") or "9:16"]:
        if aspect in {"9:16", "1:1", "16:9"} and aspect not in clean_aspects:
            clean_aspects.append(aspect)
    manifest = _delivery_package(studio, final_path, clean_aspects)
    verification = final_verify.verify(studio, final_path, manifest)
    if not verification.get("ok"):
        return {
            "ok": False,
            "job_id": job_id,
            "state": "needs_final_verify",
            "output": str(final_path),
            "final_verification": verification,
            "next": "Inspect the final verification report/contact sheet and fix the reported media or delivery problems before shipping.",
        }
    meta["state"] = "mastered"
    meta["mastered_at"] = int(time.time())
    meta["output"] = str(final_path)
    engine._atomic_json(job / "job.json", meta)
    return {
        "ok": True,
        "job_id": job_id,
        "state": "mastered",
        "output": str(final_path),
        "manifest": manifest,
        "compose_log": engine._safe_tail(compose_proc.stdout, 2500),
        "sound_gate": sound_gate,
        "music_log": music_log,
        "final_verification": verification,
        "delivery": {"variants": manifest.get("variants", {}), "thumbnails": manifest.get("thumbnails", [])},
    }


def look(job_id: str, preset: str = "neutral_punch", strength: str = "normal", apply: bool = False, at: float | None = None) -> dict[str, Any]:
    job, meta, studio = _ctx(job_id)
    if meta.get("state") not in {"verified", "enriched", "mastered"}:
        raise engine.VideoEditorError("video_cut_must_be_verified_first")
    if preset not in {"none", "warm_lift", "neutral_punch", "cool_clean"}:
        raise engine.VideoEditorError("video_look_preset_invalid")
    if strength not in {"subtle", "normal", "strong"}:
        raise engine.VideoEditorError("video_look_strength_invalid")
    cut = studio / "cut.mp4"
    graded = studio / "cut_graded.mp4"
    if not cut.is_file():
        raise engine.VideoEditorError("video_cut_missing")
    if preset == "none":
        graded.unlink(missing_ok=True)
        meta["look"] = {"preset": "none", "strength": strength, "applied": False}
        engine._atomic_json(job / "job.json", meta)
        return {"ok": True, "job_id": job_id, "preset": "none", "applied": False}
    cmd = [shutil.which("python3") or "python3", str(engine.ENGINE_ROOT / "scripts" / "grade.py"), str(cut), "--preset", preset, "--strength", strength]
    if at is not None:
        try:
            point = float(at)
        except (TypeError, ValueError) as exc:
            raise engine.VideoEditorError("video_look_time_invalid") from exc
        if point < 0 or point > _duration(studio):
            raise engine.VideoEditorError("video_look_time_out_of_bounds")
        cmd += ["--at", str(point)]
    if apply:
        cmd += ["-o", str(graded)]
    else:
        cmd.append("--compare-only")
    proc = engine._run(cmd, cwd=engine.ENGINE_ROOT, timeout=900)
    compare = studio / "verify" / "grade_compare.png"
    after = studio / "verify" / "grade_after.png"
    before = studio / "verify" / "grade_before.png"
    if apply and not graded.is_file():
        raise engine.VideoEditorError("video_grade_output_missing")
    if apply:
        graded.chmod(0o600)
        meta["look"] = {"preset": preset, "strength": strength, "applied": True, "path": str(graded)}
        if meta.get("state") != "mastered":
            meta["state"] = "enriched"
        engine._atomic_json(job / "job.json", meta)
    return {"ok": True, "job_id": job_id, "preset": preset, "strength": strength, "applied": bool(apply), "graded": str(graded) if apply else None, "compare": str(compare) if compare.is_file() else None, "before": str(before) if before.is_file() else None, "after": str(after) if after.is_file() else None, "log": engine._safe_tail(proc.stdout, 2500)}
