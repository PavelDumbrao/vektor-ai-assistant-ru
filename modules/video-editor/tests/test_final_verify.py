from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
from plugin import final_verify


def _file(path: Path, size: int = 2048) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    path.chmod(0o600)
    return path


def _studio(tmp_path: Path) -> Path:
    studio = tmp_path / "studio"
    (studio / "out").mkdir(parents=True)
    (studio / "verify").mkdir()
    (studio / "timeline.json").write_text(
        json.dumps({"predicted_duration": 10.0}), encoding="utf-8"
    )
    return studio


def test_safe_output_rejects_symlink_and_outside_root(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    good = _file(root / "good.mp4")
    assert final_verify._safe_output(good, root) is True

    outside = _file(tmp_path / "outside.mp4")
    assert final_verify._safe_output(outside, root) is False

    link = root / "link.mp4"
    link.symlink_to(good)
    assert final_verify._safe_output(link, root) is False


def _patch_media(monkeypatch, *, audio: bool = True, variant_size=(1080, 1920)):
    def fake_probe(path: Path):
        if path.name == "variant.mp4":
            width, height = variant_size
        else:
            width, height = 720, 1280
        return {"ok": True, "width": width, "height": height,
                "duration": 10.0, "audio": audio}
    monkeypatch.setattr(final_verify, "_probe", fake_probe)
    monkeypatch.setattr(final_verify, "_decode_ok", lambda path: (True, ""))
    monkeypatch.setattr(final_verify.engine, "_audio_peak_db", lambda path: -4.0)
    monkeypatch.setattr(final_verify, "_contact_sheet", lambda *args: "/tmp/contact.png")


def _manifest(studio: Path) -> dict:
    variant = _file(studio / "out" / "variant.mp4")
    thumbs = []
    for index in range(3):
        thumb = _file(studio / "out" / f"thumb-{index}.png")
        thumbs.append({"path": str(thumb), "at": index + 0.5})
    return {
        "variants": {"9:16": {"path": str(variant), "style": "scaled"}},
        "thumbnails": thumbs,
    }


def test_verify_accepts_valid_delivery(tmp_path, monkeypatch):
    studio = _studio(tmp_path)
    final = _file(studio / "out" / "final.mp4")
    manifest = _manifest(studio)
    _patch_media(monkeypatch)

    report = final_verify.verify(studio, final, manifest)
    assert report["ok"] is True
    assert report["problems"] == []
    assert report["decode_ok"] is True
    assert report["variants"]["9:16"]["ok"] is True
    assert len([x for x in report["thumbnails"] if x["ok"]]) == 3


def test_verify_rejects_wrong_variant_dimensions(tmp_path, monkeypatch):
    studio = _studio(tmp_path)
    final = _file(studio / "out" / "final.mp4")
    manifest = _manifest(studio)
    _patch_media(monkeypatch, variant_size=(1000, 1900))

    report = final_verify.verify(studio, final, manifest)
    assert report["ok"] is False
    assert report["variants"]["9:16"]["ok"] is False
    assert "variant_9:16_verification_failed" in report["problems"]


def test_verify_rejects_missing_audio(tmp_path, monkeypatch):
    studio = _studio(tmp_path)
    final = _file(studio / "out" / "final.mp4")
    manifest = _manifest(studio)
    _patch_media(monkeypatch, audio=False)

    report = final_verify.verify(studio, final, manifest)
    assert report["ok"] is False
    assert "final_audio_missing" in report["problems"]
