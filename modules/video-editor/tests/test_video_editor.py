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


def test_run_failure_includes_bounded_stderr_and_stdout(monkeypatch):
    import subprocess
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 2, stdout="detailed stdout", stderr="short stderr")
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(engine.VideoEditorError) as exc:
        engine._run(["fake-command"])
    text = str(exc.value)
    assert "stderr=short stderr" in text
    assert "stdout=detailed stdout" in text


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

def test_tool_guard_returns_registry_compatible_json_string():
    import json
    raw = plugin._guard(lambda value: {"ok": True, "value": value}, {"value": "привет"})
    assert isinstance(raw, str)
    assert json.loads(raw) == {"ok": True, "value": "привет"}


def test_tool_guard_serializes_video_editor_errors():
    import json
    def fail():
        raise engine.VideoEditorError("boom")
    raw = plugin._guard(fail, {})
    assert json.loads(raw) == {"ok": False, "error": "boom"}


def test_plugin_registers_only_video_editor_tools(monkeypatch):
    monkeypatch.setattr(engine, "runtime_ready", lambda: True)
    seen = []
    class Ctx:
        def register_tool(self, **kwargs):
            seen.append(kwargs)
    plugin.register(Ctx())
    assert {x["name"] for x in seen} == {
        "video_editor_prepare", "video_editor_takes", "video_editor_timeline_view",
        "video_editor_director_qa", "video_editor_director_approve", "video_editor_render",
        "video_editor_status", "video_editor_feedback", "video_editor_captions",
        "video_editor_caption_approve", "video_editor_cards", "video_editor_capture",
        "video_editor_proof", "video_editor_sound", "video_editor_look", "video_editor_master",
    }
    assert {x["toolset"] for x in seen} == {"video_editor"}
    timeline = next(x for x in seen if x["name"] == "video_editor_timeline_view")
    assert timeline["timeout_seconds"] == 120
    director_qa = next(x for x in seen if x["name"] == "video_editor_director_qa")
    assert director_qa["timeout_seconds"] == 420
    master = next(x for x in seen if x["name"] == "video_editor_master")
    assert master["timeout_seconds"] == 1200


def test_prepare_schema_exposes_asr_provider_modes():
    mode = plugin.PREPARE_SCHEMA["parameters"]["properties"]["asr_provider"]
    assert mode["enum"] == ["auto", "openrouter", "local"]
    assert mode["default"] == "auto"


def test_asr_normalise_prefers_word_timestamps():
    from plugin import asr_client
    items, granularity = asr_client._normalise({"words": [
        {"word": "привет", "start": 0.2, "end": 0.7},
        {"word": "мир", "start": 0.8, "end": 1.1},
    ]}, 5.0)
    assert granularity == "word"
    assert items[0] == {"start": 5.2, "end": 5.7, "text": "привет"}
    assert items[1]["text"] == "мир"


def test_safe_capture_rejects_local_and_non_http_schemes(monkeypatch):
    import importlib.util
    import egress_proxy
    monkeypatch.setattr(egress_proxy, "resolve_public", lambda host, port: ["93.184.216.34"] if host == "example.com" else (_ for _ in ()).throw(ValueError("non_public_host")))
    path = MODULE / "safe_capture.py"
    spec = importlib.util.spec_from_file_location("safe_capture_test", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert mod.validate_url("http://example.com") == "http://example.com"
    assert mod.validate_url("https://example.com") == "https://example.com"
    with pytest.raises(ValueError):
        mod.validate_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        mod.validate_url("https://127.0.0.1")




def test_caption_approve_rejects_chunk_merging(monkeypatch, tmp_path):
    from plugin import enrichment
    studio = tmp_path / "studio"
    studio.mkdir()
    captions = studio / "captions.json"
    captions.write_text(
        '{"proofread":false,"chunks":[{"start":0.0,"end":0.6,"duration":0.6,"text":"короткий текст","words":[{"text":"короткий","emphasis":false},{"text":"текст","emphasis":false}]}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(enrichment, "_ctx", lambda job_id: (tmp_path, {"state": "verified"}, studio))
    with pytest.raises(engine.VideoEditorError, match="video_caption_text_too_dense:index=0:max_words=4:do_not_merge_chunks"):
        enrichment.caption_approve(
            "012345abcdef",
            [{"index": 0, "text": "это уже слишком длинная фраза"}],
        )
    data = __import__("json").loads(captions.read_text(encoding="utf-8"))
    assert data["proofread"] is False
    assert data["chunks"][0]["text"] == "короткий текст"

def test_caption_approve_rejects_unfixed_dense_chunks(monkeypatch, tmp_path):
    from plugin import enrichment
    studio = tmp_path / "studio"
    studio.mkdir()
    captions = studio / "captions.json"
    captions.write_text(
        '{"proofread":false,"chunks":[{"start":0.0,"end":1.0,"duration":1.0,"text":"раз два три четыре пять","words":[]}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(enrichment, "_ctx", lambda job_id: (tmp_path, {"state": "verified"}, studio))
    with pytest.raises(engine.VideoEditorError, match="video_caption_chunks_still_dense:indices=0:max_words=4:correct_each_chunk_separately"):
        enrichment.caption_approve("012345abcdef", [])
    data = __import__("json").loads(captions.read_text(encoding="utf-8"))
    assert data["proofread"] is False


def test_cards_reject_out_of_timeline(monkeypatch, tmp_path):
    from plugin import enrichment
    studio = tmp_path / "studio"; studio.mkdir()
    (studio / "timeline.json").write_text('{"predicted_duration": 5.0}')
    monkeypatch.setattr(enrichment, "_ctx", lambda job_id: (tmp_path, {"state": "verified"}, studio))
    with pytest.raises(engine.VideoEditorError, match="out_of_bounds"):
        enrichment.cards("012345abcdef", [{"start": 4.5, "duration": 1.0, "big": "late"}])


def test_look_schema_requires_previewable_presets():
    from plugin.look_schema import LOOK_SCHEMA
    props = LOOK_SCHEMA["parameters"]["properties"]
    assert props["preset"]["enum"] == ["none", "warm_lift", "neutral_punch", "cool_clean"]
    assert props["apply"]["default"] is False


def test_compose_uses_pinned_hyperframes_and_private_tmp(monkeypatch, tmp_path):
    import subprocess
    from plugin import enrichment
    studio = tmp_path / "studio"
    studio.mkdir()
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
    monkeypatch.setattr(enrichment.engine, "_run", fake_run)
    monkeypatch.setattr(enrichment, "_localize_gsap", lambda studio: studio / "composition/gsap.min.js")
    monkeypatch.setattr(enrichment, "_pinned_hyperframes_binary", lambda: Path("/pinned/hyperframes"))
    enrichment._compose_self_contained(studio, "python3", {"npm_config_offline": "true"})
    assert calls[1][0] == ["/pinned/hyperframes", "check"]
    assert calls[2][0][0:3] == ["/pinned/hyperframes", "render", "."]
    assert all(call[1]["extra_env"]["TMPDIR"] == str(studio / ".hyperframes-tmp") for call in calls)
    assert (studio / ".hyperframes-tmp").stat().st_mode & 0o777 == 0o700
    assert all("npx" not in call[0] for call in calls)


def test_pinned_hyperframes_version_is_enforced(tmp_path):
    from plugin import enrichment
    binary = tmp_path / "hyperframes.mjs"
    package = tmp_path / "package.json"
    binary.write_text("#!/usr/bin/env node\n")
    binary.chmod(0o755)
    package.write_text('{"version":"0.8.29"}')
    with pytest.raises(engine.VideoEditorError, match="version_mismatch"):
        enrichment._pinned_hyperframes_binary(binary, package, expected_uid=os.getuid())
    package.write_text('{"version":"0.8.30"}')
    assert enrichment._pinned_hyperframes_binary(binary, package, expected_uid=os.getuid()) == binary


def test_sfx_auto_tune_targets_peak(monkeypatch):
    from plugin import enrichment
    monkeypatch.setattr(enrichment, "_source_peak_db", lambda path, duration, volume=1.0: -1.0 + 20.0 * __import__("math").log10(volume))
    tuned = enrichment._tune_sounds([{
        "file": "/tmp/a.mp3", "duration": 1.0, "category": "pop", "hit_at": 1.0
    }], 1.0, target_db=-14.0)
    assert len(tuned) == 1
    assert -14.05 <= tuned[0]["predicted_peak_db"] <= -13.95
    assert 0.05 < tuned[0]["volume"] < 1.0
    assert "base_volume" in tuned[0]


def test_sound_gate_uses_voice_only_cut(monkeypatch, tmp_path):
    from plugin import enrichment
    studio = tmp_path / "studio"; studio.mkdir()
    (studio / "cut.mp4").write_bytes(b"cut")
    (studio / "sfx.json").write_text('{"sounds":[{"file":"/tmp/a.mp3","duration":1.0,"volume":0.2,"category":"pop","hit_at":1.0}]}')
    monkeypatch.setattr(engine, "_audio_peak_db", lambda path: -4.5)
    monkeypatch.setattr(enrichment, "_source_peak_db", lambda path, duration, volume=1.0: -14.0)
    result = enrichment._sound_gate(studio)
    assert result["ok"] is True
    assert result["voice_peak_db"] == -4.5
    assert result["checks"][0]["status"] == "ok"


def test_broker_body_cap_is_bounded():
    import importlib.util
    spec = importlib.util.spec_from_file_location("video_asr_broker_test", MODULE / "asr_broker.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert mod.MAX_BODY <= 4 * 1024 * 1024
    assert mod.HOST == "127.0.0.1"
