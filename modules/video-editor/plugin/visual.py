from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import engine

MAX_FRAMES = 8
MIN_FRAMES = 3
MAX_WINDOW_SECONDS = 30.0
MAX_IMAGE_BYTES = 1_200_000
CANVAS_WIDTH = 1200
FONT_REGULAR = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONT_BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    try:
        return ImageFont.truetype(str(path), size=size)
    except OSError:
        return ImageFont.load_default()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _job_json(path: Path, studio: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or not _inside(path, studio):
        return {}
    return _read_json(path)


def _probe_media(path: Path) -> tuple[float, int, int]:
    proc = engine._run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", str(path),
    ], timeout=20)
    data = json.loads(proc.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    duration = float((data.get("format") or {}).get("duration") or 0.0)
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if duration <= 0 or width <= 0 or height <= 0:
        raise engine.VideoEditorError("video_visual_probe_failed")
    return duration, width, height


def _target_media(job: Path, meta: dict[str, Any], target: str) -> tuple[Path, float, str]:
    studio = job / "studio"
    target = str(target or "").strip()
    sources = meta.get("sources") or {}
    if target in sources:
        item = sources[target]
        path = Path(str(item.get("file") or job / "sources" / item.get("filename", "")))
        duration = float(item.get("duration") or 0.0)
        kind = "source"
    elif target == "cut":
        path = studio / "cut.mp4"
        duration = float(_job_json(studio / "timeline.json", studio).get("predicted_duration") or 0.0)
        kind = "cut"
    elif target in {"master", "final"}:
        path = studio / "out" / ("final.mp4" if target == "final" else "master.mp4")
        duration = 0.0
        kind = "master"
    else:
        raise engine.VideoEditorError("video_visual_target_invalid")
    if path.is_symlink() or not path.is_file() or not _inside(path, job):
        raise engine.VideoEditorError("video_visual_target_not_available")
    if duration <= 0:
        duration, _, _ = _probe_media(path)
    return path.resolve(strict=True), duration, kind


def _source_words(studio: Path, source: str) -> list[dict[str, Any]]:
    candidates = [p for p in sorted((studio / "transcripts").glob(f"{source}.*.verbatim.json")) if not p.is_symlink() and p.is_file() and _inside(p, studio)]
    if not candidates:
        return []
    data = _read_json(candidates[-1])
    words = data.get("words") or []
    return [w for w in words if isinstance(w, dict)]


def _words_near(words: list[dict[str, Any]], at: float, radius: float = 1.15) -> str:
    picked = []
    lo, hi = at - radius, at + radius
    for word in words:
        try:
            start, end = float(word.get("start")), float(word.get("end"))
        except (TypeError, ValueError):
            continue
        if end >= lo and start <= hi:
            text = str(word.get("text") or "").strip()
            if text:
                picked.append(text)
    return " ".join(picked)[:120]


def _caption_near(studio: Path, at: float) -> str:
    data = _job_json(studio / "captions.json", studio)
    chunks = data.get("chunks") or []
    picked = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        try:
            start, end = float(chunk.get("start")), float(chunk.get("end"))
        except (TypeError, ValueError):
            continue
        if end >= at - 1.2 and start <= at + 1.2:
            text = str(chunk.get("text") or "").strip()
            if text:
                picked.append(text)
    return " ".join(picked)[:120]


def _mapped_cut_snippet(studio: Path, meta: dict[str, Any], at: float) -> str:
    timeline = _job_json(studio / "timeline.json", studio)
    speed = float(timeline.get("speed") or 1.0)
    for segment in timeline.get("segments") or []:
        try:
            out_start = float(segment.get("out_start"))
            out_end = float(segment.get("out_end"))
        except (TypeError, ValueError):
            continue
        if out_start - 0.02 <= at <= out_end + 0.02:
            source = str(segment.get("source") or "")
            source_start = float(segment.get("source_start") or 0.0)
            source_at = source_start + max(0.0, at - out_start) * speed
            return _words_near(_source_words(studio, source), source_at)
    return ""


def _snippet(studio: Path, meta: dict[str, Any], target: str, kind: str, at: float) -> str:
    if kind == "source":
        return _words_near(_source_words(studio, target), at)
    if kind == "master":
        text = _caption_near(studio, at)
        if text:
            return text
    return _mapped_cut_snippet(studio, meta, at)


def _sample_times(start: float, end: float, count: int, seams: list[float] | None = None) -> list[float]:
    span = end - start
    inset = min(0.08, span / 20.0)
    lo, hi = start + inset, end - inset
    if hi <= lo:
        return [(start + end) / 2.0 for _ in range(count)]
    candidates = [float(s) for s in (seams or []) if lo + 0.09 <= float(s) <= hi - 0.09]
    max_seams = max(1, count // 2)
    if len(candidates) > max_seams:
        picks = [round(i * (len(candidates) - 1) / (max_seams - 1)) for i in range(max_seams)] if max_seams > 1 else [0]
        candidates = [candidates[i] for i in picks]
    forced = []
    for seam in candidates:
        forced.extend((seam - 0.08, seam + 0.08))
    even = [lo + (hi - lo) * i / max(1, count - 1) for i in range(count)]
    times = []
    for value in [*forced, *even]:
        value = min(hi, max(lo, value))
        if not any(abs(value - existing) < 0.025 for existing in times):
            times.append(value)
        if len(times) >= count:
            break
    return sorted(times)


def _extract_frame(video: Path, at: float, output: Path) -> None:
    engine._run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1",
        "-vf", "scale=560:-2:flags=lanczos", "-q:v", "3", str(output),
    ], timeout=30)
    if not output.is_file() or output.stat().st_size <= 0:
        raise engine.VideoEditorError("video_visual_frame_extract_failed")


def _render_waveform(video: Path, start: float, end: float, output: Path) -> bool:
    try:
        engine._run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(video),
            "-filter_complex", "aformat=channel_layouts=mono,showwavespic=s=1120x150:colors=white:scale=sqrt",
            "-frames:v", "1", str(output),
        ], timeout=30)
    except engine.VideoEditorError:
        return False
    return output.is_file() and output.stat().st_size > 0


def _wrap(text: str, width: int = 34, lines: int = 2) -> list[str]:
    if not text:
        return []
    rows = textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False)
    return rows[:lines]


def _paste_contained(canvas: Image.Image, image: Image.Image, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    target = ImageOps.contain(image.convert("RGB"), (x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
    x = x0 + (x1 - x0 - target.width) // 2
    y = y0 + (y1 - y0 - target.height) // 2
    canvas.paste(target, (x, y))


def _compose_sheet(
    frame_paths: list[Path], times: list[float], snippets: list[str], waveform: Path | None,
    *, target: str, start: float, end: float, seams: list[float], question: str,
) -> Image.Image:
    cols = min(4, len(frame_paths))
    rows = int(math.ceil(len(frame_paths) / cols))
    margin, gap = 30, 14
    cell_w = (CANVAS_WIDTH - 2 * margin - gap * (cols - 1)) // cols
    image_h, text_h = 350, 92
    header_h, wave_h, footer_h = 92, 180, 48
    cell_h = image_h + text_h
    canvas_h = header_h + rows * cell_h + max(0, rows - 1) * gap + wave_h + footer_h
    canvas = Image.new("RGB", (CANVAS_WIDTH, canvas_h), "#101215")
    draw = ImageDraw.Draw(canvas)
    draw.text((margin, 20), f"Hermes visual timeline · {target} · {start:.2f}s → {end:.2f}s", font=_font(25, bold=True), fill="white")
    if question:
        draw.text((margin, 56), question[:120], font=_font(17), fill="#c9d1d9")

    for i, (path, at, snippet) in enumerate(zip(frame_paths, times, snippets)):
        row, col = divmod(i, cols)
        x = margin + col * (cell_w + gap)
        y = header_h + row * (cell_h + gap)
        draw.rounded_rectangle((x, y, x + cell_w, y + cell_h - 4), radius=12, fill="#1b1f24", outline="#343b43", width=2)
        with Image.open(path) as frame:
            _paste_contained(canvas, frame, (x + 5, y + 5, x + cell_w - 5, y + image_h - 2))
        draw.rounded_rectangle((x + 10, y + 10, x + 86, y + 42), radius=8, fill="#000000")
        draw.text((x + 18, y + 15), f"{at:.2f}s", font=_font(15, bold=True), fill="white")
        ty = y + image_h + 8
        for line in _wrap(snippet, width=max(18, cell_w // 9), lines=2):
            draw.text((x + 10, ty), line, font=_font(15), fill="#e6edf3")
            ty += 24

    wave_y = header_h + rows * cell_h + max(0, rows - 1) * gap + 14
    draw.text((margin, wave_y), "AUDIO WAVEFORM", font=_font(15, bold=True), fill="#8b949e")
    wave_box = (margin, wave_y + 26, CANVAS_WIDTH - margin, wave_y + 156)
    draw.rectangle(wave_box, fill="#161b22", outline="#343b43", width=1)
    if waveform and waveform.is_file():
        with Image.open(waveform) as wave:
            _paste_contained(canvas, wave, wave_box)
    else:
        draw.text((margin + 18, wave_y + 75), "audio waveform unavailable", font=_font(16), fill="#8b949e")

    span = max(0.001, end - start)
    for seam in seams:
        if start <= seam <= end:
            x = margin + int((seam - start) / span * (CANVAS_WIDTH - 2 * margin))
            draw.line((x, wave_y + 26, x, wave_y + 156), fill="#ff6b6b", width=3)
            draw.text((x + 4, wave_y + 30), f"CUT {seam:.2f}", font=_font(12, bold=True), fill="#ffb4b4")
    draw.text((margin, canvas_h - 35), "Inspect gesture continuity, eye/blink state, framing, motion and cut discontinuities. Audio remains the timing authority.", font=_font(14), fill="#8b949e")
    return canvas


def _save_bounded_jpeg(image: Image.Image, path: Path) -> bytes:
    working = image
    for quality in (82, 74, 66, 58):
        buf = io.BytesIO()
        working.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        payload = buf.getvalue()
        if len(payload) <= MAX_IMAGE_BYTES:
            path.write_bytes(payload)
            os.chmod(path, 0o600)
            return payload
        working = working.resize((int(working.width * 0.88), int(working.height * 0.88)), Image.Resampling.LANCZOS)
    raise engine.VideoEditorError("video_visual_image_too_large")


def timeline_view(
    job_id: str, target: str, start: float, end: float, frames: int = 6, question: str = "",
) -> dict[str, Any]:
    job = engine._job_dir(job_id)
    meta = engine._read_meta(job)
    studio = job / "studio"
    if studio.is_symlink() or not studio.is_dir() or not _inside(studio, job):
        raise engine.VideoEditorError("video_visual_studio_unsafe")
    video, duration, kind = _target_media(job, meta, target)
    try:
        start_f, end_f = float(start), float(end)
        frame_count = int(frames)
    except (TypeError, ValueError) as exc:
        raise engine.VideoEditorError("video_visual_range_invalid") from exc
    if start_f < 0 or end_f <= start_f or end_f > duration + 0.05:
        raise engine.VideoEditorError("video_visual_range_out_of_bounds")
    if end_f - start_f > MAX_WINDOW_SECONDS:
        raise engine.VideoEditorError("video_visual_window_too_large")
    if not MIN_FRAMES <= frame_count <= MAX_FRAMES:
        raise engine.VideoEditorError("video_visual_frame_count_invalid")
    question = " ".join(str(question or "").split()).strip()[:240]

    visual_dir = studio / "visual"
    if visual_dir.is_symlink():
        raise engine.VideoEditorError("video_visual_directory_symlink_rejected")
    visual_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(visual_dir, 0o700)
    seams = []
    if kind != "source":
        seams = [float(x) for x in (_job_json(studio / "timeline.json", studio).get("seams") or [])]
    times = _sample_times(start_f, min(end_f, duration), frame_count, seams)
    snippets = [_snippet(studio, meta, target, kind, at) for at in times]
    digest = hashlib.sha256(f"{target}:{start_f:.3f}:{end_f:.3f}:{frame_count}".encode()).hexdigest()[:12]
    final_path = visual_dir / f"timeline_{target}_{digest}.jpg"
    if final_path.is_symlink():
        raise engine.VideoEditorError("video_visual_output_symlink_rejected")

    with tempfile.TemporaryDirectory(prefix=".visual-", dir=visual_dir) as tmp_name:
        tmp = Path(tmp_name)
        os.chmod(tmp, 0o700)
        frame_paths = []
        for index, at in enumerate(times):
            frame_path = tmp / f"frame_{index:02d}.jpg"
            _extract_frame(video, at, frame_path)
            frame_paths.append(frame_path)
        waveform_path = tmp / "waveform.png"
        has_waveform = _render_waveform(video, start_f, end_f, waveform_path)
        sheet = _compose_sheet(
            frame_paths, times, snippets, waveform_path if has_waveform else None,
            target=target, start=start_f, end=end_f, seams=seams, question=question,
        )
        staged = tmp / "timeline.jpg"
        payload = _save_bounded_jpeg(sheet, staged)
        os.replace(staged, final_path)
        os.chmod(final_path, 0o600)

    samples_text = "; ".join(
        f"{at:.2f}s: {snippet or '[no nearby speech]'}" for at, snippet in zip(times, snippets)
    )
    instruction = (
        "Visual timeline attached natively. Inspect the actual pixels now. "
        "Use it to judge gesture continuity, eye/blink state, framing, body motion, "
        "jump cuts and whether a zoom/card/B-roll beat is visually justified. "
        "Do not infer speech timing from frames; audio/transcript remains authoritative."
    )
    if question:
        instruction += f"\n\nFocus question: {question}"
    summary = (
        f"Visual timeline for {job_id}/{target}, {start_f:.2f}-{end_f:.2f}s, "
        f"{frame_count} frames, {len(payload)} JPEG bytes. Samples: {samples_text}"
    )
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": instruction},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii")}},
        ],
        "text_summary": summary,
        "meta": {
            "job_id": job_id,
            "target": target,
            "start": round(start_f, 3),
            "end": round(end_f, 3),
            "sample_times": [round(x, 3) for x in times],
            "seams_in_window": [round(x, 3) for x in seams if start_f <= x <= end_f],
            "image_path": str(final_path),
            "image_bytes": len(payload),
        },
    }
