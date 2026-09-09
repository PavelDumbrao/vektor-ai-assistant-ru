from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
from plugin import enrichment, engine


def _fixture(tmp_path: Path, extra: str = "") -> tuple[Path, Path]:
    studio = tmp_path / "studio"
    composition = studio / "composition"
    composition.mkdir(parents=True)
    runtime = tmp_path / "gsap.min.js"
    runtime.write_text("window.gsap={};", encoding="utf-8")
    runtime.chmod(0o644)
    html = f'<html><script src="{enrichment.GSAP_CDN}"></script>{extra}</html>'
    (composition / "index.html").write_text(html, encoding="utf-8")
    return studio, runtime


def test_gsap_is_localized_and_remote_script_removed(tmp_path):
    studio, runtime = _fixture(tmp_path)
    target = enrichment._localize_gsap(studio, runtime=runtime, expected_uid=os.getuid())
    html = (studio / "composition" / "index.html").read_text(encoding="utf-8")
    assert enrichment.GSAP_CDN not in html
    assert '<script src="./gsap.min.js"></script>' in html
    assert enrichment.REMOTE_SCRIPT_RE.search(html) is None
    assert target.read_text(encoding="utf-8") == runtime.read_text(encoding="utf-8")
    assert target.stat().st_mode & 0o077 == 0


def test_additional_remote_script_is_fail_closed(tmp_path):
    studio, runtime = _fixture(
        tmp_path, '<script src="https://evil.example/app.js"></script>'
    )
    with pytest.raises(engine.VideoEditorError, match="video_remote_script_blocked"):
        enrichment._localize_gsap(studio, runtime=runtime, expected_uid=os.getuid())
    assert not (studio / "composition" / "gsap.min.js").exists()


def test_gsap_runtime_symlink_is_rejected(tmp_path):
    studio, runtime = _fixture(tmp_path)
    link = tmp_path / "gsap-link.js"
    link.symlink_to(runtime)
    with pytest.raises(engine.VideoEditorError, match="video_gsap_runtime_unsafe"):
        enrichment._localize_gsap(studio, runtime=link, expected_uid=os.getuid())


def test_runtime_lock_pins_hyperframes_and_gsap():
    lock = json.loads(
        (MODULE / "hyperframes-runtime" / "package-lock.json").read_text(encoding="utf-8")
    )
    packages = lock["packages"]
    assert packages["node_modules/hyperframes"]["version"] == "0.8.30"
    assert packages["node_modules/gsap"]["version"] == "3.14.2"
    assert packages["node_modules/gsap"]["integrity"].startswith("sha512-")
