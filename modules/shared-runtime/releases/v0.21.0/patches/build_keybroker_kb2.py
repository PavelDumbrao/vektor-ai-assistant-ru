#!/usr/bin/env python3
"""Derive `kb2` releases from the 4 ORIGINAL live parents (tv1 / tgvid1 lines), run on VPS as root.

kb2 = kb1 (env-overridable OpenRouter STT endpoint in tools/transcription_tools.py) + the same override for the
passive-secretary ASR client in plugins/platforms/telegram/passive_media.py, whose endpoint validation now also
accepts a loopback http endpoint (the key-broker lane) ending in /audio/transcriptions.
Ids: `-kb2` when it fits the 80-char validator limit, else `-tgvid1` → `-tgvid1k2`, `-tv1` → `-tk2`.
`state: ready` is written only after the final verification; a failed build is left as `.<id>.failed`.
Usage: build_keybroker_kb2.py [--dry-run]
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
TT = "tools/transcription_tools.py"
PM = "plugins/platforms/telegram/passive_media.py"
PATCHES = {
    TT: [(
        'OPENROUTER_STT_ENDPOINT = "https://openrouter.ai/api/v1/audio/transcriptions"\n',
        'OPENROUTER_STT_ENDPOINT = (\n'
        '    os.getenv("HERMES_OPENROUTER_STT_ENDPOINT") or "https://openrouter.ai/api/v1/audio/transcriptions"\n'
        ')\n',
    )],
    PM: [
        (
            'OPENROUTER_STT_ENDPOINT = "https://openrouter.ai/api/v1/audio/transcriptions"\n',
            'OPENROUTER_STT_ENDPOINT = (\n'
            '    os.getenv("HERMES_OPENROUTER_STT_ENDPOINT") or "https://openrouter.ai/api/v1/audio/transcriptions"\n'
            ')\n',
        ),
        (
            '        parsed = urlsplit(endpoint)\n'
            '        if (\n'
            '            parsed.scheme != "https"\n'
            '            or parsed.hostname != "openrouter.ai"\n'
            '            or parsed.port not in {None, 443}\n'
            '            or parsed.username is not None\n'
            '            or parsed.password is not None\n'
            '            or parsed.query\n'
            '            or parsed.fragment\n'
            '            or parsed.path != "/api/v1/audio/transcriptions"\n'
            '        ):\n'
            '            raise ValueError("Passive media OpenRouter endpoint is not trusted")\n',
            '        parsed = urlsplit(endpoint)\n'
            '        _clean = parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment\n'
            '        _official = (\n'
            '            parsed.scheme == "https"\n'
            '            and parsed.hostname == "openrouter.ai"\n'
            '            and parsed.port in {None, 443}\n'
            '            and parsed.path == "/api/v1/audio/transcriptions"\n'
            '        )\n'
            '        # Key-broker lane (kb2): a loopback http endpoint that forwards to OpenRouter with the platform key.\n'
            '        _lane = (\n'
            '            parsed.scheme == "http"\n'
            '            and parsed.hostname == "127.0.0.1"\n'
            '            and parsed.path.endswith("/audio/transcriptions")\n'
            '        )\n'
            '        if not (_clean and (_official or _lane)):\n'
            '            raise ValueError("Passive media OpenRouter endpoint is not trusted")\n',
        ),
    ],
}
DRY = "--dry-run" in sys.argv

sys.path.insert(0, str(UP))
builder = runpy.run_path(str(UP / "prepare_runtime.py"))
release_mod = runpy.run_path(str(UP / "prepare_release.py"))
layout = runpy.run_path(str(UP / "shared_runtime_layout.py"))


def derived_id(parent_id: str) -> str:
    for candidate in (parent_id + "-kb2",
                      re.sub(r"-tgvid1$", "-tgvid1k2", parent_id),
                      re.sub(r"-tv1$", "-tk2", parent_id)):
        if candidate != parent_id and len(candidate) - len("hermes-") <= 80:
            return candidate
    raise RuntimeError(f"cannot derive a valid id from {parent_id}")


def patch_file(path: Path, patches: list[tuple[str, str]]) -> None:
    text = path.read_text(encoding="utf-8")
    if not re.search(r"^import os$", text, re.M):
        raise RuntimeError(f"`import os` missing in {path}")
    for old, new in patches:
        if text.count(old) != 1:
            raise RuntimeError(f"anchor not found exactly once in {path}: {old[:60]!r}")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


CHECK = (
    "import os; os.environ['HERMES_OPENROUTER_STT_ENDPOINT']='http://127.0.0.1:1/stt/v1/audio/transcriptions'\n"
    "import tools.transcription_tools as t, plugins.platforms.telegram.passive_media as p\n"
    "assert t.OPENROUTER_STT_ENDPOINT == p.OPENROUTER_STT_ENDPOINT == os.environ['HERMES_OPENROUTER_STT_ENDPOINT']\n"
    "p.OpenRouterWhisperASR(api_key='keybroker-openrouter-xx', endpoint=p.OPENROUTER_STT_ENDPOINT)\n"
    "try:\n    p.OpenRouterWhisperASR(api_key='keybroker-openrouter-xx', endpoint='http://evil.example/audio/transcriptions'); raise SystemExit('untrusted accepted')\n"
    "except ValueError: pass\n"
    "print(t.__file__); print(p.__file__)\n"
)

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
        for rel, patches in PATCHES.items():
            text = (parent / "hermes-agent" / rel).read_text(encoding="utf-8")
            print(f"== {parent_id} -> {target_id} ({len(target_id) - 7} chars): {rel} anchors", [text.count(o) for o, _ in patches])
        continue
    tmp = ROOT / "releases" / ("." + target_id + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        subprocess.run(["cp", "-a", "--reflink=auto", str(parent), str(tmp)], check=True)
        code = tmp / "hermes-agent"
        for rel, patches in PATCHES.items():
            patch_file(code / rel, patches)
        vlink = code / "venv"
        if vlink.is_symlink() or vlink.exists():
            vlink.unlink()
        vlink.symlink_to("../venv")
        project = tomllib.loads((code / "pyproject.toml").read_text())["project"]
        profile = release_mod["metadata"](project)
        builder["bind_core_metadata"](profile, code, tmp / "venv")
        builder["make_readable_program_tree"](tmp)
        subprocess.run([str(tmp / "venv/bin/python"), "-m", "py_compile", str(code / TT), str(code / PM)], check=True)
        manifest = builder["source_manifest"](code)
        (tmp / "code-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
        runtime = json.loads((tmp / "runtime.json").read_text())
        runtime.update({"release_id": target_id, "parent_release_id": parent_id,
                        "code_sha256": builder["manifest_digest"](manifest),
                        "schema_rollback_compatible": True, "state": "building"})
        runtime["verification"] = {
            **dict(runtime.get("verification") or {}),
            "purpose": "keybroker_stt_endpoint_kb2",
            "dependencies_identical": True,
            "changed_files": [TT, PM],
            "runtime_relocated_from": parent_id,
            "core_metadata_rebound": True,
        }
        (tmp / "runtime.json").write_text(json.dumps(runtime, indent=2))
        layout["validate_program_tree"](code)
        layout["validate_program_tree"](tmp / "venv")
        if builder["source_manifest"](code) != manifest:
            raise RuntimeError(f"manifest drift before publish: {target_id}")
        os.rename(tmp, target)
    except Exception:
        if tmp.exists():
            os.rename(tmp, ROOT / "releases" / ("." + target_id + ".failed"))
        raise
    code = target / "hermes-agent"
    venv = target / "venv"
    try:
        builder["bind_core_metadata"](profile, code, venv)
        builder["make_readable_program_tree"](target)
        layout["validate_program_tree"](code)
        layout["validate_program_tree"](venv)
        out = subprocess.check_output([str(venv / "bin/python"), "-I", "-c", CHECK], cwd="/", text=True).splitlines()
        if not all(str(code) in line for line in out[-2:]):
            raise RuntimeError(f"final import points elsewhere: {target_id}: {out}")
        if builder["source_manifest"](code) != manifest:
            raise RuntimeError(f"code manifest drift after publish: {target_id}")
    except Exception:
        os.rename(target, ROOT / "releases" / ("." + target_id + ".failed"))
        raise
    runtime = json.loads((target / "runtime.json").read_text())
    runtime["verification"].update({"final_path_metadata_rebound": True, "neutral_import_verified": True, "env_override_verified": True})
    runtime["state"] = "ready"
    (target / "runtime.json").write_text(json.dumps(runtime, indent=2))
    results.append({"release_id": target_id, "parent": parent_id, "code_sha256": runtime["code_sha256"]})
    print(json.dumps(results[-1]), flush=True)
print("done:", len(results), "releases")
