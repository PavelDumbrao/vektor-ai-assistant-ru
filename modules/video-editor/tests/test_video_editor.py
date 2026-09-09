from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
plugin = importlib.import_module("plugin")
engine = importlib.import_module("plugin.engine")


def profile(tmp_path: Path, monkeypatch):
    home = tmp_path / "user"
    hermes = home / ".hermes"
    videos = hermes / "cache" / "videos"
    workspace = home / "workspace"
    videos.mkdir(parents=True)
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    return home, hermes, videos, workspace


def test_source_path_is_profile_scoped(tmp_path, monkeypatch):
    _, _, videos, _ = profile(tmp_path, monkeypatch)
    video = videos / "clip.mp4"
    video.write_bytes(b"video")
    assert engine.validate_source_path(str(video)) == video.resolve()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")
    with pytest.raises(engine.VideoEditorError, match="outside_profile_roots"):
        engine.validate_source_path(str(outside))


def test_source_symlink_is_rejected(tmp_path, monkeypatch):
    _, _, videos, workspace = profile(tmp_path, monkeypatch)
    real = workspace / "real.mp4"
    real.write_bytes(b"video")
    link = videos / "link.mp4"
    link.symlink_to(real)
    with pytest.raises(engine.VideoEditorError, match="symlink_rejected"):
        engine.validate_source_path(str(link))


def test_edl_ranges_are_bounded():
    meta = {"sources": {"source_01": {"filename": "source_01.mp4", "duration": 10.0}}}
    clean = engine._normalize_ranges(meta, [{"source": "source_01", "start": 1.0, "end": 3.0, "zoom": 1.1}])
    assert clean[0]["source"] == "source_01"
    assert clean[0]["zoom"] == 1.1
    with pytest.raises(engine.VideoEditorError, match="out_of_bounds"):
        engine._normalize_ranges(meta, [{"source": "source_01", "start": 9.0, "end": 12.0}])


def test_feedback_is_profile_local(tmp_path, monkeypatch):
    _, hermes, _, _ = profile(tmp_path, monkeypatch)
    result = engine.feedback("cutting", "Не оставлять длинные вдохи", "длинные вдохи режь")
    assert result["ok"] is True
    text = (hermes / "video_editor" / "taste.md").read_text()
    assert "Не оставлять длинные вдохи" in text
    assert "длинные вдохи режь" in text




def test_job_source_is_an_independent_snapshot(tmp_path):
    src = tmp_path / "src.mp4"
    dst = tmp_path / "dst.mp4"
    src.write_bytes(b"first")
    engine._copy_source(src, dst)
    assert src.stat().st_ino != dst.stat().st_ino
    src.write_bytes(b"changed")
    assert dst.read_bytes() == b"first"


def test_cleanup_old_jobs_is_bounded_to_job_ids(tmp_path, monkeypatch):
    _, hermes, _, _ = profile(tmp_path, monkeypatch)
    old = hermes / "video_editor" / "jobs" / "012345abcdef"
    old.mkdir(parents=True)
    keep = hermes / "video_editor" / "jobs" / "do-not-delete"
    keep.mkdir()
    os.utime(old, (1, 1))
    assert engine.cleanup_old_jobs(now=engine.JOB_TTL_HOURS * 3600 + 2) == 1
    assert not old.exists()
    assert keep.exists()

def test_transcription_quality_models_are_distinct():
    assert engine.model_path("fast").name == "ggml-small.bin"
    assert engine.model_path("quality").name == "ggml-medium.bin"
    with pytest.raises(engine.VideoEditorError, match="quality_invalid"):
        engine.model_path("ultra")


def test_runtime_lock_does_not_require_write_permission(tmp_path, monkeypatch):
    lock = tmp_path / "runtime.lock"
    lock.write_text("")
    lock.chmod(0o444)
    monkeypatch.setattr(engine, "LOCK_PATH", lock)
    with engine.runtime_lock():
        assert lock.read_text() == ""


def test_prepare_schema_exposes_bounded_quality_modes():
    quality = plugin.PREPARE_SCHEMA["parameters"]["properties"]["transcription_quality"]
    assert quality["enum"] == ["fast", "quality"]
    assert quality["default"] == "fast"

def test_plugin_registers_only_video_editor_tools(monkeypatch):
    monkeypatch.setattr(engine, "runtime_ready", lambda: True)
    seen = []
    class Ctx:
        def register_tool(self, **kwargs):
            seen.append(kwargs)
    plugin.register(Ctx())
    assert {x["name"] for x in seen} == {
        "video_editor_prepare", "video_editor_takes", "video_editor_render",
        "video_editor_status", "video_editor_feedback",
    }
    assert {x["toolset"] for x in seen} == {"video_editor"}
