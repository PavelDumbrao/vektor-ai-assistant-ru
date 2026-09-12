from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from . import critic_client, engine, visual

PROTOCOL_VERSION = 1
MAX_WINDOWS = 4
MAX_ISSUES = 12
MAX_AUTO_FIX_LOOPS = 2
VALID_STAGES = {"cut", "master"}
VALID_VERDICTS = {"pass", "fix"}
VALID_SEVERITIES = {"low", "medium", "high"}
VALID_CATEGORIES = {
    "jump_cut", "gesture", "blink", "framing", "caption", "overlay",
    "composition", "proof", "thumbnail", "audio_visual_sync", "pacing", "hook", "other",
}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _safe_json(path: Path, root: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or not _inside(path, root):
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(job: Path, meta: dict[str, Any], stage: str) -> tuple[Path, str]:
    studio = job / "studio"
    if stage == "cut":
        path, target = studio / "cut.mp4", "cut"
    else:
        preferred = studio / "out" / "final.mp4"
        path = preferred if preferred.is_file() else studio / "out" / "master.mp4"
        target = "final" if path.name == "final.mp4" else "master"
    if path.is_symlink() or not path.is_file() or not _inside(path, studio):
        raise engine.VideoEditorError(f"video_director_{stage}_artifact_missing")
    return path.resolve(strict=True), target


def _fingerprint(path: Path) -> dict[str, Any]:
    info = path.stat()
    return {
        "sha256": _sha256(path),
        "bytes": int(info.st_size),
    }


def _qa_dir(studio: Path) -> Path:
    path = studio / "verify"
    if path.is_symlink():
        raise engine.VideoEditorError("video_director_verify_dir_unsafe")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def _receipt_path(studio: Path, stage: str) -> Path:
    return _qa_dir(studio) / f"director_{stage}.json"


def _pending_path(studio: Path, stage: str) -> Path:
    return _qa_dir(studio) / f"director_{stage}_pending.json"


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def invalidate_master_qa(studio: Path) -> None:
    for path in (_receipt_path(studio, "master"), _pending_path(studio, "master")):
        _unlink(path)


def invalidate_cut_qa(studio: Path) -> None:
    for stage in ("cut", "master"):
        _unlink(_receipt_path(studio, stage))
        _unlink(_pending_path(studio, stage))


def _remove_bound_path(path: Path, studio: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        path.unlink()
        return
    try:
        path.resolve(strict=True).relative_to(studio.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise engine.VideoEditorError("video_downstream_path_unsafe") from exc
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def invalidate_for_new_cut(studio: Path) -> None:
    """Remove every artifact whose timeline becomes stale after an EDL change."""
    for name in ("cut.mp4", "preview.mp4", "cut_graded.mp4", "captions.json", "cards.json", "proof.json", "sfx.json", "timeline.json"):
        _remove_bound_path(studio / name, studio)
    for name in ("composition", "out", "verify"):
        _remove_bound_path(studio / name, studio)
    visual_dir = studio / "visual"
    if visual_dir.is_dir() and not visual_dir.is_symlink():
        for pattern in ("timeline_cut_*.jpg", "timeline_master_*.jpg", "timeline_final_*.jpg"):
            for path in visual_dir.glob(pattern):
                if path.is_symlink() or path.is_file():
                    path.unlink(missing_ok=True)


def invalidate_enrichment(studio: Path) -> None:
    """Keep the approved cut, but invalidate everything authored on its timeline."""
    invalidate_master_qa(studio)
    for name in ("cut_graded.mp4", "captions.json", "cards.json", "proof.json", "sfx.json"):
        _remove_bound_path(studio / name, studio)
    for name in ("composition", "out"):
        _remove_bound_path(studio / name, studio)
    visual_dir = studio / "visual"
    if visual_dir.is_dir() and not visual_dir.is_symlink():
        for pattern in ("timeline_master_*.jpg", "timeline_final_*.jpg"):
            for path in visual_dir.glob(pattern):
                if path.is_symlink() or path.is_file():
                    path.unlink(missing_ok=True)


def _timeline(studio: Path) -> dict[str, Any]:
    return _safe_json(studio / "timeline.json", studio)


def _edl(studio: Path) -> dict[str, Any]:
    return _safe_json(studio / "edl.json", studio)


def _duration(studio: Path, artifact: Path) -> float:
    timeline = _timeline(studio)
    if timeline.get("predicted_duration"):
        try:
            return float(timeline["predicted_duration"])
        except (TypeError, ValueError):
            pass
    duration, _, _ = visual._probe_media(artifact)
    return duration


def _clamped_window(center: float, duration: float, width: float, *, kind: str, reason: str, score: float) -> dict[str, Any]:
    width = min(max(0.8, width), max(0.8, duration))
    start = max(0.0, center - width / 2.0)
    end = min(duration, start + width)
    start = max(0.0, end - width)
    return {"start": round(start, 3), "end": round(end, 3), "kind": kind, "reason": reason, "score": round(score, 3)}


def _seam_candidates(studio: Path, duration: float) -> list[dict[str, Any]]:
    timeline = _timeline(studio)
    segments = [x for x in (timeline.get("segments") or []) if isinstance(x, dict)]
    seams = timeline.get("seams") or []
    ranges = [x for x in (_edl(studio).get("ranges") or []) if isinstance(x, dict)]
    ranked: list[dict[str, Any]] = []
    for index, raw_seam in enumerate(seams):
        try:
            seam = float(raw_seam)
        except (TypeError, ValueError):
            continue
        if not 0.05 < seam < duration - 0.05:
            continue
        left = segments[index] if index < len(segments) else {}
        right = segments[index + 1] if index + 1 < len(segments) else {}
        left_edl = ranges[index] if index < len(ranges) else {}
        right_edl = ranges[index + 1] if index + 1 < len(ranges) else {}
        score = 1.0
        reasons: list[str] = []
        if left.get("source") and right.get("source") and left.get("source") != right.get("source"):
            score += 4.0
            reasons.append("source change")
        try:
            left_len = float(left.get("out_end")) - float(left.get("out_start"))
            right_len = float(right.get("out_end")) - float(right.get("out_start"))
            shortest = min(left_len, right_len)
            if shortest < 0.75:
                score += 2.5; reasons.append("very short adjacent beat")
            elif shortest < 1.5:
                score += 1.25; reasons.append("short adjacent beat")
        except (TypeError, ValueError):
            pass
        try:
            zoom_left = float(left_edl.get("zoom") or 1.0)
            zoom_right = float(right_edl.get("zoom") or 1.0)
            if abs(zoom_left - zoom_right) >= 0.12:
                score += 2.0; reasons.append("large zoom step")
            elif abs(zoom_left - zoom_right) >= 0.06:
                score += 1.0; reasons.append("zoom step")
        except (TypeError, ValueError):
            pass
        if seam < 3.0:
            score += 1.0
            reasons.append("opening retention zone")
        left_beat = str(left_edl.get("beat") or left.get("beat") or "").strip()
        right_beat = str(right_edl.get("beat") or right.get("beat") or "").strip()
        if left_beat and right_beat and left_beat != right_beat:
            score += 0.5
            reasons.append("beat transition")
        ranked.append({
            "center": seam,
            "score": score,
            "reason": ", ".join(reasons) or "cut seam",
            "kind": "seam",
        })
    ranked.sort(key=lambda item: (-float(item["score"]), float(item["center"])))
    return ranked


def _add_unique(windows: list[dict[str, Any]], candidate: dict[str, Any]) -> None:
    center = (float(candidate["start"]) + float(candidate["end"])) / 2.0
    if any(abs(center - (float(item["start"]) + float(item["end"])) / 2.0) < 0.65 for item in windows):
        return
    windows.append(candidate)


def _cut_windows(studio: Path, artifact: Path) -> list[dict[str, Any]]:
    duration = _duration(studio, artifact)
    windows: list[dict[str, Any]] = []
    opening_end = min(duration, 2.4)
    if opening_end >= 0.8:
        _add_unique(windows, {
            "start": 0.0, "end": round(opening_end, 3), "kind": "opening",
            "reason": "opening hook, eye contact and first visual beat", "score": 9.0,
        })
    for seam in _seam_candidates(studio, duration):
        _add_unique(windows, _clamped_window(
            float(seam["center"]), duration, 1.9,
            kind="seam", reason=str(seam["reason"]), score=float(seam["score"]),
        ))
        if len(windows) >= MAX_WINDOWS:
            break
    if len(windows) < 2 and duration > 3.0:
        _add_unique(windows, _clamped_window(
            duration * 0.5, duration, min(2.5, duration),
            kind="midpoint", reason="overall gesture/framing continuity sample", score=1.0,
        ))
    return windows[:MAX_WINDOWS]


def _event_candidates(studio: Path, duration: float) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for filename, key, score in (("cards.json", "cards", 3.5), ("proof.json", "beats", 4.0)):
        data = _safe_json(studio / filename, studio)
        for item in data.get(key) or []:
            if not isinstance(item, dict):
                continue
            try:
                start = float(item.get("start") or 0.0)
                span = float(item.get("duration") or 0.0)
            except (TypeError, ValueError):
                continue
            for point, label in ((start, "entry"), (start + span, "exit")):
                if 0.1 < point < duration - 0.1:
                    candidates.append({"center": point, "score": score, "reason": f"{filename[:-5]} {label}", "kind": "visual_event"})
    return candidates


def _master_windows(studio: Path, artifact: Path) -> list[dict[str, Any]]:
    duration = _duration(studio, artifact)
    windows: list[dict[str, Any]] = []
    opening_end = min(duration, 2.5)
    if opening_end >= 0.8:
        _add_unique(windows, {
            "start": 0.0, "end": round(opening_end, 3), "kind": "opening",
            "reason": "final opening, caption readability and first design beat", "score": 10.0,
        })
    candidates = _event_candidates(studio, duration)
    candidates.extend(_seam_candidates(studio, duration)[:2])
    manifest = _safe_json(studio / "out" / "manifest.json", studio)
    thumbs = [x for x in (manifest.get("thumbnails") or []) if isinstance(x, dict)]
    if thumbs:
        try:
            at = float(thumbs[len(thumbs) // 2].get("at"))
            candidates.append({"center": at, "score": 2.5, "reason": "thumbnail/face quality sample", "kind": "thumbnail"})
        except (TypeError, ValueError):
            pass
    candidates.sort(key=lambda item: (-float(item.get("score") or 0), float(item.get("center") or 0)))
    for item in candidates:
        _add_unique(windows, _clamped_window(
            float(item["center"]), duration, 2.0,
            kind=str(item.get("kind") or "final"),
            reason=str(item.get("reason") or "final composition sample"),
            score=float(item.get("score") or 1.0),
        ))
        if len(windows) >= MAX_WINDOWS:
            break
    if len(windows) < 2 and duration > 3.0:
        _add_unique(windows, _clamped_window(duration * 0.5, duration, 2.5, kind="midpoint", reason="final composition midpoint", score=1.0))
    return windows[:MAX_WINDOWS]


def _valid_receipt(studio: Path, stage: str, artifact: Path) -> dict[str, Any] | None:
    receipt = _safe_json(_receipt_path(studio, stage), studio)
    if not receipt or receipt.get("verdict") != "pass":
        return None
    fingerprint = _fingerprint(artifact)
    if receipt.get("artifact_sha256") != fingerprint["sha256"]:
        return None
    if int(receipt.get("artifact_bytes") or -1) != fingerprint["bytes"]:
        return None
    return receipt


def protocol_enabled(meta: dict[str, Any]) -> bool:
    try:
        return int(meta.get("director_protocol_version") or 0) >= PROTOCOL_VERSION
    except (TypeError, ValueError):
        return False


def require_cut_approval(job: Path, meta: dict[str, Any], studio: Path) -> None:
    if not protocol_enabled(meta):
        return
    artifact, _ = _artifact(job, meta, "cut")
    if not _valid_receipt(studio, "cut", artifact):
        raise engine.VideoEditorError("video_director_cut_qa_required")


def cut_approval_status(job: Path, meta: dict[str, Any], studio: Path) -> dict[str, Any]:
    if not protocol_enabled(meta):
        return {"required": False, "approved": None, "protocol_version": 0}
    try:
        artifact, _ = _artifact(job, meta, "cut")
        receipt = _valid_receipt(studio, "cut", artifact)
    except engine.VideoEditorError:
        receipt = None
    return {"required": True, "approved": bool(receipt), "protocol_version": PROTOCOL_VERSION}


def master_approval_status(job: Path, meta: dict[str, Any], studio: Path) -> dict[str, Any]:
    if not protocol_enabled(meta):
        return {"required": False, "approved": None, "protocol_version": 0}
    try:
        artifact, _ = _artifact(job, meta, "master")
        receipt = _valid_receipt(studio, "master", artifact)
    except engine.VideoEditorError:
        receipt = None
    return {"required": True, "approved": bool(receipt), "protocol_version": PROTOCOL_VERSION}


def _existing_pending(studio: Path, stage: str, fingerprint: dict[str, Any]) -> dict[str, Any]:
    pending = _safe_json(_pending_path(studio, stage), studio)
    if not pending:
        return {}
    if pending.get("artifact_sha256") != fingerprint["sha256"]:
        return {}
    if int(pending.get("artifact_bytes") or -1) != fingerprint["bytes"]:
        return {}
    token = str(pending.get("qa_token") or "")
    if not 16 <= len(token) <= 256:
        return {}
    return pending


def _failure_count(meta: dict[str, Any], stage: str) -> int:
    try:
        return max(0, int(((meta.get("director_qa") or {}).get(stage) or {}).get("failures") or 0))
    except (TypeError, ValueError):
        return 0


def _native_video_critic(artifact: Path, stage: str, fingerprint: dict[str, Any]) -> dict[str, Any]:
    health = critic_client.health()
    if not health.get("ok"):
        return {"status": "unavailable", "error": str(health.get("error") or "broker_unavailable")[:120]}
    if not health.get("enabled") or stage not in (health.get("stages") or []):
        return {"status": "disabled", "model": health.get("model")}
    try:
        result = critic_client.critique(artifact, stage, str(fingerprint["sha256"]))
    except critic_client.CriticError as exc:
        return {"status": "unavailable", "error": str(exc)[:120], "model": health.get("model")}
    report = result.get("report") if isinstance(result, dict) else None
    if not isinstance(report, dict):
        return {"status": "unavailable", "error": "critic_report_invalid", "model": health.get("model")}
    return {
        "status": "ok",
        "model": result.get("model") or health.get("model"),
        "provider": result.get("provider") or health.get("upstream"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "proxy_bytes": result.get("proxy_bytes"),
        "report": report,
    }


def qa(job_id: str, stage: str = "cut") -> dict[str, Any]:
    stage = str(stage or "cut").strip().lower()
    if stage not in VALID_STAGES:
        raise engine.VideoEditorError("video_director_stage_invalid")
    job = engine._job_dir(job_id)
    meta = engine._read_meta(job)
    studio = job / "studio"
    if stage == "cut" and meta.get("state") not in {"needs_visual_qa", "verified", "enriched", "mastered"}:
        raise engine.VideoEditorError("video_director_cut_not_ready")
    if stage == "master" and meta.get("state") not in {"needs_final_visual_qa", "mastered"}:
        raise engine.VideoEditorError("video_director_master_not_ready")
    artifact, target = _artifact(job, meta, stage)
    fingerprint = _fingerprint(artifact)
    pending = _existing_pending(studio, stage, fingerprint)
    token = str(pending.get("qa_token") or secrets.token_urlsafe(24))
    windows = _cut_windows(studio, artifact) if stage == "cut" else _master_windows(studio, artifact)
    if not windows:
        raise engine.VideoEditorError("video_director_no_visual_windows")
    attempt = _failure_count(meta, stage) + 1
    views: list[dict[str, Any]] = []
    content: list[dict[str, Any]] = []
    instruction = (
        f"Director QA stage={stage}. Review ALL attached visual windows against the actual pixels. "
        "Check gesture continuity, eye/blink state, framing, jump cuts, caption/overlay readability, "
        "and whether design beats help rather than obstruct. Audio/transcript remains timing authority. "
        f"After review call video_editor_director_approve with qa_token={token!r}. "
        "Use verdict=fix for any blocking medium/high issue."
    )
    content.append({"type": "text", "text": instruction})
    for index, window in enumerate(windows, 1):
        question = f"Director QA window {index}: {window['reason']}"
        view = visual.timeline_view(
            job_id, target, float(window["start"]), float(window["end"]),
            frames=6 if window["kind"] == "seam" else 4, question=question,
        )
        image_part = next((part for part in view.get("content") or [] if isinstance(part, dict) and part.get("type") == "image_url"), None)
        if image_part is None:
            raise engine.VideoEditorError("video_director_visual_result_missing")
        content.append({"type": "text", "text": f"Window {index}/{len(windows)} · {window['kind']} · {window['start']:.2f}-{window['end']:.2f}s · {window['reason']}"})
        content.append(image_part)
        views.append({**window, "image_path": (view.get("meta") or {}).get("image_path"), "sample_times": (view.get("meta") or {}).get("sample_times") or []})
    cloud_critic = _native_video_critic(artifact, stage, fingerprint)
    if cloud_critic.get("status") == "ok":
        report = cloud_critic.get("report") or {}
        content.append({"type": "text", "text": (
            "Native full-video critic watched the entire current artifact via Gemini 3.8 Flash. "
            "Treat this as a second-director opinion and explicitly acknowledge it in approval.\n"
            + json.dumps(report, ensure_ascii=False, separators=(",", ":"))[:7000]
        )})
    elif cloud_critic.get("status") == "unavailable":
        content.append({"type": "text", "text": "Native full-video critic is temporarily unavailable; local Director QA remains authoritative and the pipeline may continue fail-open."})
    pending_payload = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "job_id": job_id,
        "stage": stage,
        "qa_token": token,
        "artifact_sha256": fingerprint["sha256"],
        "artifact_bytes": fingerprint["bytes"],
        "artifact_name": artifact.name,
        "target": target,
        "attempt": attempt,
        "windows": views,
        "cloud_critic": cloud_critic,
        "created_at": int(time.time()),
    }
    engine._atomic_json(_pending_path(studio, stage), pending_payload)
    summary = (
        f"Director QA {stage} for {job_id}: {len(windows)} visual windows, "
        f"artifact_sha256={fingerprint['sha256'][:12]}, attempt={attempt}. "
        f"qa_token={token}. Review all images before approval."
    )
    return {
        "_multimodal": True,
        "content": content,
        "text_summary": summary,
        "meta": {
            "job_id": job_id,
            "stage": stage,
            "qa_token": token,
            "artifact_sha256": fingerprint["sha256"],
            "attempt": attempt,
            "windows": views,
            "cloud_critic": cloud_critic,
            "automatic_correction_budget_remaining": max(0, MAX_AUTO_FIX_LOOPS - _failure_count(meta, stage)),
        },
    }


def _clean_issues(raw_issues: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if raw_issues is None:
        return []
    if not isinstance(raw_issues, list) or len(raw_issues) > MAX_ISSUES:
        raise engine.VideoEditorError("video_director_issues_invalid")
    issues: list[dict[str, Any]] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            raise engine.VideoEditorError("video_director_issue_invalid")
        category = str(raw.get("category") or "other").strip().lower()
        severity = str(raw.get("severity") or "medium").strip().lower()
        if category not in VALID_CATEGORIES or severity not in VALID_SEVERITIES:
            raise engine.VideoEditorError("video_director_issue_taxonomy_invalid")
        detail = " ".join(str(raw.get("detail") or "").split()).strip()
        action = " ".join(str(raw.get("action") or "").split()).strip()
        if not detail or len(detail) > 360 or len(action) > 360:
            raise engine.VideoEditorError("video_director_issue_text_invalid")
        item: dict[str, Any] = {"category": category, "severity": severity, "detail": detail}
        if action:
            item["action"] = action
        if raw.get("at") is not None:
            try:
                at = float(raw.get("at"))
            except (TypeError, ValueError) as exc:
                raise engine.VideoEditorError("video_director_issue_time_invalid") from exc
            if at < 0:
                raise engine.VideoEditorError("video_director_issue_time_invalid")
            item["at"] = round(at, 3)
        issues.append(item)
    return issues


def approve(
    job_id: str,
    stage: str,
    qa_token: str,
    verdict: str,
    summary: str,
    issues: list[dict[str, Any]] | None = None,
    cloud_critic_acknowledged: bool = False,
) -> dict[str, Any]:
    stage = str(stage or "").strip().lower()
    verdict = str(verdict or "").strip().lower()
    if stage not in VALID_STAGES or verdict not in VALID_VERDICTS:
        raise engine.VideoEditorError("video_director_approval_invalid")
    token = str(qa_token or "").strip()
    if not 16 <= len(token) <= 256 or any(ch.isspace() for ch in token):
        raise engine.VideoEditorError("video_director_token_invalid")
    summary = " ".join(str(summary or "").split()).strip()
    if not 8 <= len(summary) <= 1200:
        raise engine.VideoEditorError("video_director_summary_invalid")
    clean_issues = _clean_issues(issues)
    if verdict == "pass" and any(item["severity"] in {"medium", "high"} for item in clean_issues):
        raise engine.VideoEditorError("video_director_pass_has_blocking_issues")

    job = engine._job_dir(job_id)
    meta = engine._read_meta(job)
    studio = job / "studio"
    artifact, _ = _artifact(job, meta, stage)
    fingerprint = _fingerprint(artifact)
    pending = _safe_json(_pending_path(studio, stage), studio)
    if not pending or not secrets.compare_digest(str(pending.get("qa_token") or ""), token):
        raise engine.VideoEditorError("video_director_pending_token_mismatch")
    if pending.get("artifact_sha256") != fingerprint["sha256"] or int(pending.get("artifact_bytes") or -1) != fingerprint["bytes"]:
        raise engine.VideoEditorError("video_director_artifact_changed_since_review")
    cloud_critic = pending.get("cloud_critic") if isinstance(pending.get("cloud_critic"), dict) else {}
    if cloud_critic.get("status") == "ok" and not bool(cloud_critic_acknowledged):
        raise engine.VideoEditorError("video_director_cloud_critic_ack_required")
    attempt = int(pending.get("attempt") or 1)
    receipt = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "job_id": job_id,
        "stage": stage,
        "verdict": verdict,
        "summary": summary,
        "issues": clean_issues,
        "artifact_sha256": fingerprint["sha256"],
        "artifact_bytes": fingerprint["bytes"],
        "artifact_name": artifact.name,
        "attempt": attempt,
        "cloud_critic": cloud_critic,
        "cloud_critic_acknowledged": bool(cloud_critic_acknowledged),
        "qa_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "reviewed_by": "hermes",
        "reviewed_at": int(time.time()),
    }
    engine._atomic_json(_receipt_path(studio, stage), receipt)
    _unlink(_pending_path(studio, stage))

    director_state = meta.setdefault("director_qa", {})
    stage_state = director_state.setdefault(stage, {})
    failures = max(_failure_count(meta, stage), int(stage_state.get("failures") or 0))
    if verdict == "fix":
        failures += 1
    stage_state.update({
        "last_verdict": verdict,
        "last_attempt": attempt,
        "failures": failures,
        "artifact_sha256": fingerprint["sha256"],
        "reviewed_at": receipt["reviewed_at"],
    })
    exhausted = failures >= MAX_AUTO_FIX_LOOPS and verdict == "fix"
    stage_state["automatic_correction_budget_exhausted"] = exhausted
    if stage == "cut":
        if verdict == "pass":
            if meta.get("state") == "needs_visual_qa":
                meta["state"] = "verified"
        else:
            invalidate_enrichment(studio)
            meta["state"] = "needs_fix"
    else:
        if verdict == "pass":
            meta["state"] = "mastered"
            meta["mastered_at"] = int(time.time())
            meta["output"] = str(artifact)
        else:
            meta["state"] = "enriched"
    meta["director_protocol_version"] = PROTOCOL_VERSION
    meta["director_escalation_required"] = bool(exhausted)
    engine._atomic_json(job / "job.json", meta)
    next_step = "Director QA passed. Continue the pipeline."
    if verdict == "fix":
        next_step = (
            "Automatic correction budget exhausted; stop and surface the remaining visual issue to the owner."
            if exhausted else
            "Fix the reported visual issues, rerender the affected artifact, then run Director QA again."
        )
    return {
        "ok": verdict == "pass",
        "job_id": job_id,
        "stage": stage,
        "verdict": verdict,
        "state": meta.get("state"),
        "issues": clean_issues,
        "automatic_correction_budget_exhausted": exhausted,
        "next": next_step,
    }
