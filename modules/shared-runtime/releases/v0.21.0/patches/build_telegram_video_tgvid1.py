#!/usr/bin/env python3
"""Derive `<parent>-tgvid1` releases from the 4 live `*-lm2` releases (run on VPS as root).

Modeled on /opt/vektor/admin/diagnostics/build_long_media_lm2.py (fleet convention: cp -a parent -> tmp,
patch code, re-bind core metadata, code-manifest.json, runtime.json with parent chain + verification,
validate program tree, neutral import check, rename into place).

Change set (from the dev tree /opt/vektor-dev/tgvid1/hermes-agent, reviewed + tested):
  * new file plugins/platforms/telegram/video_meta.py
  * plugins/platforms/telegram/adapter.py: `send_video` block replaced (byte-identical across the 4 parents),
    plus the import line(s) the dev adapter adds.
Usage: build_telegram_video_tgvid1.py [--dry-run]
"""
from __future__ import annotations

import difflib
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
DEV = Path("/opt/vektor-dev/tgvid1/hermes-agent")
SUFFIX = "-tgvid1"
PARENTS = [
    "hermes-0.21.0-29112bef-vektor3-baysangur-tglf1-vtm1-tgrel2-pmfix2-pi2-li2-lm2",
    "hermes-0.21.0-29112bef-vektor3-pavel-tglf2-vtm1-tgrel8-pmfix2-pi2-lm2",
    "hermes-0.21.0-29112bef-vektor3-vyacheslav-tglf1-vtm1-tgrel2-pmfix2-pi2-li2-lm2",
    "hermes-0.21.0-29112bef-vektor4-modern-tglf1-vtm1-tgrel2-pmfix2-pi2-li2-groupss3-lm2",
]
ADAPTER_REL = "plugins/platforms/telegram/adapter.py"
META_REL = "plugins/platforms/telegram/video_meta.py"
DRY = "--dry-run" in sys.argv

sys.path.insert(0, str(UP))
builder = runpy.run_path(str(UP / "prepare_runtime.py"))
release_mod = runpy.run_path(str(UP / "prepare_release.py"))
layout = runpy.run_path(str(UP / "shared_runtime_layout.py"))


def send_video_block(text: str) -> tuple[int, int]:
    """[start, end) line indexes of the `async def send_video` method (up to the next method def)."""
    lines = text.splitlines(keepends=True)
    start = next(i for i, l in enumerate(lines) if l.startswith("    async def send_video("))
    end = next(i for i in range(start + 1, len(lines)) if re.match(r"^    (async )?def ", lines[i]))
    return start, end


dev_adapter = (DEV / ADAPTER_REL).read_text(encoding="utf-8")
dev_meta = (DEV / META_REL).read_text(encoding="utf-8")
ds, de = send_video_block(dev_adapter)
NEW_BLOCK = "".join(dev_adapter.splitlines(keepends=True)[ds:de])
# The dev adapter adds a parenthesised 3-line import; use the single-line equivalent in the releases.
assert "from plugins.platforms.telegram.video_meta import (" in dev_adapter, "dev adapter has no video_meta import"
NEW_IMPORTS = ["from plugins.platforms.telegram.video_meta import video_send_kwargs\n"]
assert "video_meta" in NEW_BLOCK or "video_send_kwargs" in NEW_BLOCK, "dev send_video does not use video_meta"


def patch_adapter(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if "video_meta" in text:
        return
    lines = text.splitlines(keepends=True)
    s, e = send_video_block(text)
    old_block = "".join(lines[s:e])
    # sanity: parent block must equal the dev tree's ORIGINAL block (same region in the unpatched vektor4 release)
    ref = ROOT / "releases" / PARENTS[3] / "hermes-agent" / ADAPTER_REL
    rs, re_ = send_video_block(ref.read_text(encoding="utf-8"))
    ref_block = "".join(ref.read_text(encoding="utf-8").splitlines(keepends=True)[rs:re_])
    if old_block != ref_block:
        raise RuntimeError(f"send_video block differs from reference in {path}")
    lines[s:e] = [NEW_BLOCK]
    # insert imports after the last top-level import line in the first 400 lines
    last_import = max(i for i, l in enumerate(lines[:400]) if l.startswith(("import ", "from ")))
    lines[last_import + 1:last_import + 1] = NEW_IMPORTS
    path.write_text("".join(lines), encoding="utf-8")


def derived_id(parent_id: str) -> str:
    """Release ids are validated as ^hermes-[A-Za-z0-9_.-]{1,80}$ (upgrade_profile, hermes_instance):
    use the short suffix -tv1 when -tgvid1 would exceed the limit (vektor4 line)."""
    candidate = parent_id + SUFFIX
    if len(candidate) - len("hermes-") <= 80:
        return candidate
    return parent_id + "-tv1"


results = []
for parent_id in PARENTS:
    target_id = derived_id(parent_id)
    parent = ROOT / "releases" / parent_id
    target = ROOT / "releases" / target_id
    if target.exists():
        print("exists, skip:", target_id)
        continue
    if DRY:
        # show the diff that would be applied to this parent's adapter
        text = (parent / "hermes-agent" / ADAPTER_REL).read_text(encoding="utf-8")
        s, e = send_video_block(text)
        old = "".join(text.splitlines(keepends=True)[s:e])
        print(f"== {target_id}: adapter diff lines:",
              sum(1 for _ in difflib.unified_diff(old.splitlines(), NEW_BLOCK.splitlines(), lineterm="")))
        continue
    tmp = ROOT / "releases" / ("." + target_id + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    subprocess.run(["cp", "-a", "--reflink=auto", str(parent), str(tmp)], check=True)
    code = tmp / "hermes-agent"
    (code / META_REL).write_text(dev_meta, encoding="utf-8")
    os.chmod(code / META_REL, 0o644)
    patch_adapter(code / ADAPTER_REL)
    vlink = code / "venv"
    if vlink.is_symlink() or vlink.exists():
        vlink.unlink()
    vlink.symlink_to("../venv")
    project = tomllib.loads((code / "pyproject.toml").read_text())["project"]
    profile = release_mod["metadata"](project)
    builder["bind_core_metadata"](profile, code, tmp / "venv")
    builder["make_readable_program_tree"](tmp)
    subprocess.run([str(tmp / "venv/bin/python"), "-m", "py_compile", str(code / ADAPTER_REL), str(code / META_REL)], check=True)
    manifest = builder["source_manifest"](code)
    (tmp / "code-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
    runtime = json.loads((tmp / "runtime.json").read_text())
    runtime.update({"release_id": target_id, "parent_release_id": parent_id,
                    "code_sha256": builder["manifest_digest"](manifest),
                    "schema_rollback_compatible": True, "state": "ready"})
    runtime["verification"] = {
        **dict(runtime.get("verification") or {}),
        "purpose": "telegram_video_dimensions_tgvid1",
        "dependencies_identical": True,
        "changed_files": [ADAPTER_REL, META_REL],
        "source_tree": str(DEV),
        "runtime_relocated_from": parent_id,
        "core_metadata_rebound": True,
    }
    (tmp / "runtime.json").write_text(json.dumps(runtime, indent=2))
    layout["validate_program_tree"](code)
    layout["validate_program_tree"](tmp / "venv")
    if builder["source_manifest"](code) != manifest:
        raise RuntimeError(f"manifest drift before publish: {target_id}")
    out = subprocess.check_output([str(tmp / "venv/bin/python"), "-I", "-c",
                                   "import plugins.platforms.telegram.video_meta as m, plugins.platforms.telegram.adapter as a; print(m.__file__); print(a.__file__)"],
                                  cwd="/", text=True).splitlines()
    if not all(str(code) in x for x in out):
        raise RuntimeError(f"import points elsewhere before publish: {target_id}: {out}")
    os.rename(tmp, target)
    code = target / "hermes-agent"
    venv = target / "venv"
    builder["bind_core_metadata"](profile, code, venv)
    builder["make_readable_program_tree"](target)
    layout["validate_program_tree"](code)
    layout["validate_program_tree"](venv)
    out2 = subprocess.check_output([str(venv / "bin/python"), "-I", "-c",
                                    "import plugins.platforms.telegram.video_meta as m, plugins.platforms.telegram.adapter as a; print(m.__file__); print(a.__file__)"],
                                   cwd="/", text=True).splitlines()
    if not all(str(code) in x for x in out2):
        raise RuntimeError(f"final import points elsewhere: {target_id}: {out2}")
    if builder["source_manifest"](code) != manifest:
        raise RuntimeError(f"code manifest drift after publish: {target_id}")
    runtime = json.loads((target / "runtime.json").read_text())
    runtime["verification"]["final_path_metadata_rebound"] = True
    runtime["verification"]["neutral_import_verified"] = True
    (target / "runtime.json").write_text(json.dumps(runtime, indent=2))
    nlink = os.stat(target / "runtime.json").st_nlink
    results.append({"release_id": target_id, "parent": parent_id, "code_sha256": runtime["code_sha256"], "runtime_json_nlink": nlink})
    print(json.dumps(results[-1]), flush=True)
print("done:", len(results), "releases")
