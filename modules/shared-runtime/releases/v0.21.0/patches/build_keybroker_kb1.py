#!/usr/bin/env python3
"""Derive `kb1` releases from the 4 live releases (run on VPS as root): the OpenRouter STT endpoint becomes
env-overridable so client bots can route transcription through the local key broker.

Change set (one line in tools/transcription_tools.py):
    OPENROUTER_STT_ENDPOINT = "https://openrouter.ai/api/v1/audio/transcriptions"
 →  OPENROUTER_STT_ENDPOINT = os.getenv("HERMES_OPENROUTER_STT_ENDPOINT", "https://openrouter.ai/api/v1/audio/transcriptions")
Modeled on build_telegram_video_tgvid1.py (fleet convention: cp -a parent → patch → bind_core_metadata →
code-manifest → runtime.json with parent chain + verification → validate → neutral import → rename).
Release ids must match ^hermes-[A-Za-z0-9_.-]{1,80}$: `-kb1` when it fits, else the last suffix is compacted
(`-tgvid1` → `-tgvid1k1`, `-tv1` → `-tk1`).
Usage: build_keybroker_kb1.py [--dry-run]
"""
from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path("/opt/vektor")
UP = ROOT / "admin/upgrades/v0.21.0"
PARENTS = [
    "hermes-0.21.0-29112bef-vektor3-baysangur-tglf1-vtm1-tgrel2-pmfix2-pi2-li2-lm2-tgvid1",
    "hermes-0.21.0-29112bef-vektor3-pavel-tglf2-vtm1-tgrel8-pmfix2-pi2-lm2-tgvid1",
    "hermes-0.21.0-29112bef-vektor3-vyacheslav-tglf1-vtm1-tgrel2-pmfix2-pi2-li2-lm2-tgvid1",
    "hermes-0.21.0-29112bef-vektor4-modern-tglf1-vtm1-tgrel2-pmfix2-pi2-li2-groupss3-lm2-tv1",
]
FILE_REL = "tools/transcription_tools.py"
OLD = 'OPENROUTER_STT_ENDPOINT = "https://openrouter.ai/api/v1/audio/transcriptions"\n'
NEW = ('OPENROUTER_STT_ENDPOINT = os.getenv(\n'
       '    "HERMES_OPENROUTER_STT_ENDPOINT", "https://openrouter.ai/api/v1/audio/transcriptions"\n'
       ')\n')
DRY = "--dry-run" in sys.argv

sys.path.insert(0, str(UP))
builder = runpy.run_path(str(UP / "prepare_runtime.py"))
release_mod = runpy.run_path(str(UP / "prepare_release.py"))
layout = runpy.run_path(str(UP / "shared_runtime_layout.py"))


def derived_id(parent_id: str) -> str:
    for candidate in (parent_id + "-kb1",
                      re.sub(r"-tgvid1$", "-tgvid1k1", parent_id),
                      re.sub(r"-tv1$", "-tk1", parent_id)):
        if candidate != parent_id and len(candidate) - len("hermes-") <= 80:
            return candidate
    raise RuntimeError(f"cannot derive a valid id from {parent_id}")


def patch_file(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if "HERMES_OPENROUTER_STT_ENDPOINT" in text:
        return
    if text.count(OLD) != 1:
        raise RuntimeError(f"anchor not found exactly once in {path}")
    if not re.search(r"^import os$", text, re.M):
        raise RuntimeError(f"`import os` missing in {path}")
    path.write_text(text.replace(OLD, NEW), encoding="utf-8")


results = []
for parent_id in PARENTS:
    target_id = derived_id(parent_id)
    parent = ROOT / "releases" / parent_id
    target = ROOT / "releases" / target_id
    if not parent.is_dir():
        raise RuntimeError(f"parent missing: {parent_id}")
    if target.exists():
        print("exists, skip:", target_id)
        continue
    if DRY:
        text = (parent / "hermes-agent" / FILE_REL).read_text(encoding="utf-8")
        print(f"== {parent_id} -> {target_id} ({len(target_id) - 7} chars): anchor x{text.count(OLD)}")
        continue
    tmp = ROOT / "releases" / ("." + target_id + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    subprocess.run(["cp", "-a", "--reflink=auto", str(parent), str(tmp)], check=True)
    code = tmp / "hermes-agent"
    patch_file(code / FILE_REL)
    vlink = code / "venv"
    if vlink.is_symlink() or vlink.exists():
        vlink.unlink()
    vlink.symlink_to("../venv")
    project = tomllib.loads((code / "pyproject.toml").read_text())["project"]
    profile = release_mod["metadata"](project)
    builder["bind_core_metadata"](profile, code, tmp / "venv")
    builder["make_readable_program_tree"](tmp)
    subprocess.run([str(tmp / "venv/bin/python"), "-m", "py_compile", str(code / FILE_REL)], check=True)
    manifest = builder["source_manifest"](code)
    (tmp / "code-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
    runtime = json.loads((tmp / "runtime.json").read_text())
    runtime.update({"release_id": target_id, "parent_release_id": parent_id,
                    "code_sha256": builder["manifest_digest"](manifest),
                    "schema_rollback_compatible": True, "state": "ready"})
    runtime["verification"] = {
        **dict(runtime.get("verification") or {}),
        "purpose": "keybroker_stt_endpoint_kb1",
        "dependencies_identical": True,
        "changed_files": [FILE_REL],
        "runtime_relocated_from": parent_id,
        "core_metadata_rebound": True,
    }
    (tmp / "runtime.json").write_text(json.dumps(runtime, indent=2))
    layout["validate_program_tree"](code)
    layout["validate_program_tree"](tmp / "venv")
    if builder["source_manifest"](code) != manifest:
        raise RuntimeError(f"manifest drift before publish: {target_id}")
    os.rename(tmp, target)
    code = target / "hermes-agent"
    venv = target / "venv"
    builder["bind_core_metadata"](profile, code, venv)
    builder["make_readable_program_tree"](target)
    layout["validate_program_tree"](code)
    layout["validate_program_tree"](venv)
    out = subprocess.check_output(
        [str(venv / "bin/python"), "-I", "-c",
         "import os; os.environ['HERMES_OPENROUTER_STT_ENDPOINT']='http://127.0.0.1:1/x'; "
         "import tools.transcription_tools as t; print(t.__file__); print(t.OPENROUTER_STT_ENDPOINT)"],
        cwd="/", text=True).splitlines()
    if str(code) not in out[0] or out[1] != "http://127.0.0.1:1/x":
        raise RuntimeError(f"final import/override check failed: {target_id}: {out}")
    if builder["source_manifest"](code) != manifest:
        raise RuntimeError(f"code manifest drift after publish: {target_id}")
    runtime = json.loads((target / "runtime.json").read_text())
    runtime["verification"]["final_path_metadata_rebound"] = True
    runtime["verification"]["neutral_import_verified"] = True
    runtime["verification"]["env_override_verified"] = True
    (target / "runtime.json").write_text(json.dumps(runtime, indent=2))
    results.append({"release_id": target_id, "parent": parent_id, "code_sha256": runtime["code_sha256"]})
    print(json.dumps(results[-1]), flush=True)
print("done:", len(results), "releases")
