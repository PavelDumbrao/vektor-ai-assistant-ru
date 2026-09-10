from __future__ import annotations

import base64
import importlib
import json
import os
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
engine = importlib.import_module("plugin.engine")
visual = importlib.import_module("plugin.visual")
plugin = importlib.import_module("plugin")

JOB_ID = "012345abcdef"


def _job(tmp_path: Path, monkeypatch, *, with_cut: bool = False) -> tuple[Path, Path]:
    home = tmp_path / "user"
    hermes = home / ".hermes"
    job = hermes / "video_editor" / "jobs" / JOB_ID
    studio = job / "studio"
    source = job / "sources" / "source_01.mp4"
    source.parent.mkdir(parents=True)
    studio.mkdir(parents=True)
    source.write_bytes(b"fake-video")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    (job / "job.json").write_text(json.dumps({
        "state": "verified" if with_cut else "prepared",
        "sources": {"source_01": {
            "file": str(source), "filename": source.name, "original_name": "clip.mp4",
            "duration": 40.0, "width": 720, "height": 1280, "fps": "30/1", "bytes": 10,
        }},
    }))
    transcripts = studio / "transcripts"
    transcripts.mkdir()
    (transcripts / "source_01.test.verbatim.json").write_text(json.dumps({
        "words": [
            {"start": 1.0, "end": 1.3, "text": "первый"},
            {"start": 3.0, "end": 3.3, "text": "жест"},
            {"start": 5.0, "end": 5.3, "text": "проверить"},
            {"start": 7.0, "end": 7.3, "text": "кадр"},
        ]
    }, ensure_ascii=False))
    if with_cut:
        cut = studio / "cut.mp4"
        cut.write_bytes(b"fake-cut")
        (studio / "timeline.json").write_text(json.dumps({
            "predicted_duration": 5.0,
            "speed": 1.0,
            "segments": [{
                "source": "source_01", "source_start": 2.0, "source_end": 7.0,
                "out_start": 0.0, "out_end": 5.0,
            }],
            "seams": [2.5],
        }))
    return job, studio


def _fake_media(monkeypatch):
    def frame(_video: Path, at: float, output: Path):
        image = Image.new("RGB", (360, 640), (30 + int(at * 10) % 120, 55, 80))
        ImageDraw.Draw(image).text((20, 20), f"{at:.2f}", fill="white")
        image.save(output, "JPEG")
    def waveform(_video: Path, _start: float, _end: float, output: Path):
        image = Image.new("RGB", (1120, 150), "#161b22")
        ImageDraw.Draw(image).line((0, 75, 1120, 75), fill="white", width=3)
        image.save(output, "PNG")
        return True
    monkeypatch.setattr(visual, "_extract_frame", frame)
    monkeypatch.setattr(visual, "_render_waveform", waveform)


def test_timeline_view_returns_native_multimodal_image(tmp_path, monkeypatch):
    job, _ = _job(tmp_path, monkeypatch)
    _fake_media(monkeypatch)
    result = visual.timeline_view(
        JOB_ID, "source_01", 0.5, 7.5, frames=4,
        question="Проверь жест и моргание перед склейкой",
    )
    assert result["_multimodal"] is True
    assert result["content"][0]["type"] == "text"
    url = result["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    payload = base64.b64decode(url.split(",", 1)[1])
    assert 0 < len(payload) <= visual.MAX_IMAGE_BYTES
    output = Path(result["meta"]["image_path"])
    assert output.parent == job / "studio" / "visual"
    assert output.stat().st_mode & 0o777 == 0o600
    assert "Focus question" in result["content"][0]["text"]


def test_timeline_view_maps_cut_time_back_to_source_words(tmp_path, monkeypatch):
    _, _ = _job(tmp_path, monkeypatch, with_cut=True)
    _fake_media(monkeypatch)
    result = visual.timeline_view(JOB_ID, "cut", 0.0, 4.0, frames=3)
    assert result["meta"]["seams_in_window"] == [2.5]
    assert any(abs(t - 2.42) < 0.02 for t in result["meta"]["sample_times"])
    assert any(abs(t - 2.58) < 0.02 for t in result["meta"]["sample_times"])
    assert "жест" in result["text_summary"] or "проверить" in result["text_summary"]


def test_timeline_view_rejects_arbitrary_target_and_large_window(tmp_path, monkeypatch):
    _job(tmp_path, monkeypatch)
    _fake_media(monkeypatch)
    with pytest.raises(engine.VideoEditorError, match="target_invalid"):
        visual.timeline_view(JOB_ID, "/etc/passwd", 0.0, 1.0, frames=3)
    with pytest.raises(engine.VideoEditorError, match="window_too_large"):
        visual.timeline_view(JOB_ID, "source_01", 0.0, 31.0, frames=3)


def test_timeline_view_rejects_symlinked_visual_directory(tmp_path, monkeypatch):
    _, studio = _job(tmp_path, monkeypatch)
    _fake_media(monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (studio / "visual").symlink_to(outside, target_is_directory=True)
    with pytest.raises(engine.VideoEditorError, match="directory_symlink_rejected"):
        visual.timeline_view(JOB_ID, "source_01", 0.0, 2.0, frames=3)


def test_visual_guard_preserves_multimodal_success(monkeypatch):
    expected = {"_multimodal": True, "content": [{"type": "text", "text": "ok"}], "text_summary": "ok"}
    monkeypatch.setattr(visual, "timeline_view", lambda **_kwargs: expected)
    result = plugin._visual_guard({"job_id": JOB_ID, "target": "source_01", "start": 0, "end": 1})
    assert result is expected


def test_timeline_view_does_not_follow_transcript_symlink(tmp_path, monkeypatch):
    _, studio = _job(tmp_path, monkeypatch)
    _fake_media(monkeypatch)
    for item in (studio / "transcripts").iterdir():
        item.unlink()
    outside = tmp_path / "secret.json"
    outside.write_text('{"words":[{"start":1,"end":2,"text":"SECRET_SHOULD_NOT_LEAK"}]}')
    (studio / "transcripts" / "source_01.evil.verbatim.json").symlink_to(outside)
    result = visual.timeline_view(JOB_ID, "source_01", 0.5, 2.5, frames=3)
    assert "SECRET_SHOULD_NOT_LEAK" not in result["text_summary"]
