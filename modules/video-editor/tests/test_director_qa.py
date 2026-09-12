from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
engine = importlib.import_module("plugin.engine")
director = importlib.import_module("plugin.director")

JOB_ID = "012345abcdef"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _job(tmp_path: Path, monkeypatch, *, state: str = "needs_visual_qa"):
    home = tmp_path / "user"
    hermes = home / ".hermes"
    job = hermes / "video_editor" / "jobs" / JOB_ID
    studio = job / "studio"
    (studio / "out").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    (studio / "cut.mp4").write_bytes(b"cut-v1")
    (studio / "out" / "master.mp4").write_bytes(b"master-v1")
    _write_json(studio / "timeline.json", {
        "predicted_duration": 10.0,
        "seams": [1.2, 5.0, 8.4],
        "segments": [
            {"source": "source_01", "out_start": 0, "out_end": 1.2},
            {"source": "source_01", "out_start": 1.2, "out_end": 5.0},
            {"source": "source_02", "out_start": 5.0, "out_end": 8.4},
            {"source": "source_02", "out_start": 8.4, "out_end": 10.0},
        ],
    })
    _write_json(studio / "edl.json", {"ranges": [
        {"source": "source_01", "start": 0, "end": 1.2, "beat": "hook", "zoom": 1.0},
        {"source": "source_01", "start": 2, "end": 5.8, "beat": "setup", "zoom": 1.2},
        {"source": "source_02", "start": 0, "end": 3.4, "beat": "proof", "zoom": 1.0},
        {"source": "source_02", "start": 4, "end": 5.6, "beat": "payoff", "zoom": 1.05},
    ]})
    meta = {"job_id": JOB_ID, "state": state, "director_protocol_version": 1, "sources": {}}
    _write_json(job / "job.json", meta)
    return job, studio


def _fake_visual(monkeypatch):
    calls = []
    def fake(job_id, target, start, end, frames=6, question=""):
        calls.append((job_id, target, start, end, frames, question))
        return {
            "_multimodal": True,
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}},
            ],
            "text_summary": "visual",
            "meta": {"image_path": f"/tmp/{len(calls)}.jpg", "sample_times": [start, end]},
        }
    monkeypatch.setattr(director.visual, "timeline_view", fake)
    return calls


def _approve_cut(job: Path, monkeypatch) -> dict:
    _fake_visual(monkeypatch)
    packet = director.qa(JOB_ID, "cut")
    token = packet["meta"]["qa_token"]
    return director.approve(
        JOB_ID, "cut", token, "pass",
        "Opening and risky seams are visually continuous and acceptable.",
        [],
    )


def test_cut_qa_is_multimodal_and_risk_ranked(tmp_path, monkeypatch):
    _, studio = _job(tmp_path, monkeypatch)
    calls = _fake_visual(monkeypatch)
    result = director.qa(JOB_ID, "cut")
    assert result["_multimodal"] is True
    assert 2 <= len(calls) <= director.MAX_WINDOWS
    assert calls[0][1] == "cut"
    assert calls[0][2] == 0.0
    assert any(abs(call[2] - 4.05) < 0.2 for call in calls[1:])
    assert len([p for p in result["content"] if p.get("type") == "image_url"]) == len(calls)
    pending = json.loads((studio / "verify" / "director_cut_pending.json").read_text())
    assert pending["artifact_sha256"] == director._sha256(studio / "cut.mp4")
    assert pending["qa_token"] == result["meta"]["qa_token"]


def test_cut_approval_is_bound_to_exact_artifact(tmp_path, monkeypatch):
    job, studio = _job(tmp_path, monkeypatch)
    result = _approve_cut(job, monkeypatch)
    assert result["state"] == "verified"
    meta = engine._read_meta(job)
    director.require_cut_approval(job, meta, studio)
    receipt = json.loads((studio / "verify" / "director_cut.json").read_text())
    assert receipt["verdict"] == "pass"
    assert receipt["artifact_sha256"] == director._sha256(studio / "cut.mp4")

    (studio / "cut.mp4").write_bytes(b"cut-v2")
    with pytest.raises(engine.VideoEditorError, match="cut_qa_required"):
        director.require_cut_approval(job, engine._read_meta(job), studio)


def test_approval_rejects_artifact_changed_after_review(tmp_path, monkeypatch):
    _, studio = _job(tmp_path, monkeypatch)
    _fake_visual(monkeypatch)
    packet = director.qa(JOB_ID, "cut")
    (studio / "cut.mp4").write_bytes(b"changed-after-view")
    with pytest.raises(engine.VideoEditorError, match="artifact_changed_since_review"):
        director.approve(
            JOB_ID, "cut", packet["meta"]["qa_token"], "pass",
            "The visual review passed before the artifact changed unexpectedly.", [],
        )


def test_pass_cannot_hide_blocking_issue(tmp_path, monkeypatch):
    _job(tmp_path, monkeypatch)
    _fake_visual(monkeypatch)
    packet = director.qa(JOB_ID, "cut")
    with pytest.raises(engine.VideoEditorError, match="pass_has_blocking_issues"):
        director.approve(
            JOB_ID, "cut", packet["meta"]["qa_token"], "pass",
            "There is still a visible jump that needs a correction before shipping.",
            [{"category": "jump_cut", "severity": "high", "at": 5.0, "detail": "Abrupt hand discontinuity"}],
        )


def test_two_failed_cycles_exhaust_automatic_budget(tmp_path, monkeypatch):
    job, studio = _job(tmp_path, monkeypatch)
    _fake_visual(monkeypatch)
    last = None
    for attempt in range(2):
        packet = director.qa(JOB_ID, "cut")
        last = director.approve(
            JOB_ID, "cut", packet["meta"]["qa_token"], "fix",
            "The hand jumps across the cut and the seam needs another edit.",
            [{"category": "gesture", "severity": "medium", "at": 5.0, "detail": "Hand position changes abruptly", "action": "Move the seam"}],
        )
        if attempt == 0:
            meta = engine._read_meta(job)
            meta["state"] = "needs_visual_qa"
            engine._atomic_json(job / "job.json", meta)
            (studio / "cut.mp4").write_bytes(b"cut-second-attempt")
    assert last is not None
    assert last["automatic_correction_budget_exhausted"] is True
    assert engine._read_meta(job)["director_escalation_required"] is True


def test_new_cut_invalidates_timeline_derived_artifacts(tmp_path, monkeypatch):
    _, studio = _job(tmp_path, monkeypatch)
    for name in ("captions.json", "cards.json", "proof.json", "sfx.json", "cut_graded.mp4"):
        (studio / name).write_bytes(b"stale")
    (studio / "composition").mkdir()
    (studio / "composition" / "index.html").write_text("stale")
    (studio / "verify").mkdir(exist_ok=True)
    (studio / "verify" / "director_cut.json").write_text("{}")
    director.invalidate_for_new_cut(studio)
    for name in ("cut.mp4", "timeline.json", "captions.json", "cards.json", "proof.json", "sfx.json", "cut_graded.mp4"):
        assert not (studio / name).exists()
    assert not (studio / "composition").exists()
    assert not (studio / "out").exists()
    assert not (studio / "verify").exists()


def test_master_qa_pass_is_only_path_to_mastered_for_protocol_job(tmp_path, monkeypatch):
    job, studio = _job(tmp_path, monkeypatch, state="needs_final_visual_qa")
    _fake_visual(monkeypatch)
    packet = director.qa(JOB_ID, "master")
    assert packet["meta"]["stage"] == "master"
    result = director.approve(
        JOB_ID, "master", packet["meta"]["qa_token"], "pass",
        "Final opening, overlays, framing and thumbnail sample are visually clean.",
        [{"category": "thumbnail", "severity": "low", "at": 5.0, "detail": "Face is clear and eyes are open"}],
    )
    assert result["state"] == "mastered"
    meta = engine._read_meta(job)
    assert meta["state"] == "mastered"
    assert meta["output"].endswith("master.mp4")
    status = director.master_approval_status(job, meta, studio)
    assert status["required"] is True
    assert status["approved"] is True


def test_legacy_job_does_not_require_director_receipt(tmp_path, monkeypatch):
    job, studio = _job(tmp_path, monkeypatch, state="verified")
    meta = engine._read_meta(job)
    meta.pop("director_protocol_version", None)
    engine._atomic_json(job / "job.json", meta)
    director.require_cut_approval(job, meta, studio)
    assert director.cut_approval_status(job, meta, studio) == {
        "required": False, "approved": None, "protocol_version": 0,
    }


def test_protocol_enrichment_requires_cut_receipt(tmp_path, monkeypatch):
    job, studio = _job(tmp_path, monkeypatch, state="verified")
    enrichment = importlib.import_module("plugin.enrichment")
    with pytest.raises(engine.VideoEditorError, match="cut_qa_required"):
        enrichment._require_cut_ready(job, engine._read_meta(job), studio)
    _approve_cut(job, monkeypatch)
    enrichment._require_cut_ready(job, engine._read_meta(job), studio)


def test_protocol_master_becomes_candidate_until_final_qa(tmp_path, monkeypatch):
    import contextlib
    import subprocess
    enrichment = importlib.import_module("plugin.enrichment")
    job, studio = _job(tmp_path, monkeypatch, state="enriched")
    meta = engine._read_meta(job)
    (studio / "captions.json").write_text('{"proofread":true,"chunks":[]}', encoding="utf-8")
    (studio / "sfx.json").write_text('{"sounds":[]}', encoding="utf-8")
    monkeypatch.setattr(enrichment, "_require_cut_ready", lambda *_args: None)
    monkeypatch.setattr(director, "protocol_enabled", lambda _meta: True)
    monkeypatch.setattr(director, "invalidate_master_qa", lambda _studio: None)
    monkeypatch.setattr(engine, "runtime_lock", lambda: contextlib.nullcontext())
    monkeypatch.setattr(engine, "_run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="ok", stderr=""))
    def fake_compose(studio_path, _py, _env):
        out = studio_path / "out"
        out.mkdir(exist_ok=True)
        (out / "master.mp4").write_bytes(b"rendered-master")
        return subprocess.CompletedProcess([], 0, stdout="rendered", stderr="")
    monkeypatch.setattr(enrichment, "_compose_self_contained", fake_compose)
    monkeypatch.setattr(enrichment, "_sound_gate", lambda _studio: {"ok": True, "checks": []})
    monkeypatch.setattr(enrichment, "_delivery_package", lambda _s, path, _a: {"master": str(path), "variants": {}, "thumbnails": []})
    monkeypatch.setattr(enrichment.final_verify, "verify", lambda *_args: {"ok": True})

    result = enrichment.master(JOB_ID, aspects=["9:16"])
    assert result["ok"] is True
    assert result["state"] == "needs_final_visual_qa"
    assert result["ship_ready"] is False
    assert result["director_qa_required"] is True
    updated = engine._read_meta(job)
    assert updated["state"] == "needs_final_visual_qa"
    assert "mastered_at" not in updated


def test_cloud_critic_requires_explicit_ack_before_pass(tmp_path, monkeypatch):
    job, _studio = _job(tmp_path, monkeypatch)
    _fake_visual(monkeypatch)
    monkeypatch.setattr(director.critic_client, "health", lambda: {"ok": True, "enabled": True, "stages": ["cut"], "model": "gemini-3.8-flash-medium", "upstream": "lingsuan.top"})
    monkeypatch.setattr(director.critic_client, "critique", lambda *_args, **_kwargs: {
        "ok": True, "model": "gemini-3.8-flash-medium", "provider": "lingsuan.top", "elapsed_seconds": 1.2, "proxy_bytes": 1234,
        "report": {"verdict": "pass", "summary": "Full video looks coherent", "issues": [], "structured": True},
    })
    packet = director.qa(JOB_ID, "cut")
    assert packet["meta"]["cloud_critic"]["status"] == "ok"
    with pytest.raises(engine.VideoEditorError, match="cloud_critic_ack_required"):
        director.approve(JOB_ID, "cut", packet["meta"]["qa_token"], "pass", "Local and cloud review both look clean.", [])
    result = director.approve(JOB_ID, "cut", packet["meta"]["qa_token"], "pass", "Local and cloud review both look clean.", [], cloud_critic_acknowledged=True)
    assert result["state"] == "verified"
    assert engine._read_meta(job)["state"] == "verified"


def test_cloud_critic_unavailable_is_fail_open(tmp_path, monkeypatch):
    _job(tmp_path, monkeypatch)
    _fake_visual(monkeypatch)
    monkeypatch.setattr(director.critic_client, "health", lambda: {"ok": True, "enabled": True, "stages": ["cut"], "model": "gemini-3.8-flash-medium"})
    monkeypatch.setattr(director.critic_client, "critique", lambda *_args, **_kwargs: (_ for _ in ()).throw(director.critic_client.CriticError("temporary")))
    packet = director.qa(JOB_ID, "cut")
    assert packet["meta"]["cloud_critic"]["status"] == "unavailable"
    result = director.approve(
        JOB_ID, "cut", packet["meta"]["qa_token"], "pass",
        "Local Director QA is clean while the optional cloud critic is unavailable.", [],
    )
    assert result["state"] == "verified"
