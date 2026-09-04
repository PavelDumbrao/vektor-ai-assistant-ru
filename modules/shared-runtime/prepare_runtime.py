#!/usr/bin/env python3
"""Prepare versioned root-owned runtimes; never switches or stops a profile.

Run by the VPS administrator. Source Python is inspected after dropping to its
owner. Existing venvs are not moved: new environments are created at their final
paths, with exact public-package pins and uv's shared hardlink cache. The local
Hermes source is not built/executed as root; a plain .pth and distribution
metadata bind each new environment to its preserved code version.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import pwd
import re
import runpy
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


ROOT = Path("/opt/vektor")
OWNER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]*$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
EXCLUDED_DIRS = {
    ".git", "venv", ".venv", "node_modules", "__pycache__", ".pytest_cache",
    ".ruff_cache", "dist", "build", "web_dist", ".cache",
}
EXCLUDED_FILES = {"auth.json", "credentials.json", "secrets.json", "token.json"}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def progress(stage: str, **values) -> None:
    print(json.dumps({"stage": stage, **values}, ensure_ascii=False), flush=True)


def safe_env(entry=None) -> dict[str, str]:
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONNOUSERSITE": "1",
        "UV_NO_CONFIG": "1", "UV_CACHE_DIR": str(ROOT / "cache/uv"),
        "UV_PYTHON_INSTALL_DIR": str(ROOT / "python"), "UV_LINK_MODE": "hardlink",
    }
    if entry is not None:
        env.update(HOME=entry.pw_dir, USER=entry.pw_name, LOGNAME=entry.pw_name)
    return env


def command(args, *, entry=None, cwd=None, env=None, timeout=300) -> str:
    options = {}
    if entry is not None:
        options.update(user=entry.pw_uid, group=entry.pw_gid, extra_groups=(), umask=0o077)
    result = subprocess.run(
        list(map(str, args)), cwd=cwd, env=env or safe_env(entry),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=timeout, check=False, **options,
    )
    if result.returncode:
        # Do not echo arbitrary output from an owner-controlled interpreter.
        raise RuntimeError(f"command_failed:{Path(str(args[0])).name}:{result.returncode}")
    return result.stdout.strip()


def inspect_profile(owner: str) -> dict:
    if not OWNER_RE.fullmatch(owner):
        raise ValueError("invalid_owner")
    entry = pwd.getpwnam(owner)
    home = Path(entry.pw_dir)
    source = home / ".hermes/hermes-agent"
    if entry.pw_uid <= 0 or home != Path("/home") / owner or source.is_symlink():
        raise ValueError("source_is_not_a_legacy_owner_runtime")
    for path in (home, home / ".hermes", source):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != entry.pw_uid or info.st_mode & 0o022:
            raise ValueError("unsafe_source_directory")
    program = """
import importlib.metadata as m, json, sys
core=m.distribution('hermes-agent')
print(json.dumps({
 'python':sys.version.split()[0],
 'packages':{d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions() if d.metadata.get('Name')},
 'metadata':{name:core.read_text(name) for name in ('METADATA','WHEEL','entry_points.txt','top_level.txt') if core.read_text(name) is not None},
 'entry_points':{e.name:e.value for e in core.entry_points if e.group=='console_scripts'}
}))
"""
    data = json.loads(command([source / "venv/bin/python", "-I", "-c", program], entry=entry, cwd=home))
    if not re.fullmatch(r"3\.11\.\d+", data["python"]):
        raise ValueError("unsupported_python_version")
    for name, version in data["packages"].items():
        if not PACKAGE_RE.fullmatch(name) or not VERSION_RE.fullmatch(version):
            raise ValueError("non_registry_dependency_requires_review")
    if data["packages"].get("hermes-agent") != "0.20.0":
        raise ValueError("unsupported_hermes_version")
    data.update(owner=owner, source=str(source))
    return data


def source_manifest(source: Path) -> dict:
    result = {}
    for folder, directories, files in os.walk(source, followlinks=False):
        directories[:] = [name for name in directories if name not in EXCLUDED_DIRS and not name.endswith(".egg-info")]
        for name in list(directories) + files:
            path = Path(folder) / name
            relative = str(path.relative_to(source))
            if path.is_symlink():
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(source):
                    raise ValueError("source_symlink_escapes_code")
                target = os.readlink(path)
                if Path(target).is_absolute():
                    target = os.path.relpath(resolved, path.parent)
                result[relative] = {"kind": "link", "target": target}
                continue
            if path.is_dir():
                continue
            if name.startswith(".env") or name in EXCLUDED_FILES or path.suffix in (".pyc", ".db", ".sqlite", ".log"):
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("source_contains_special_file")
            result[relative] = {"kind": "file", "sha256": digest(path),
                                "mode": 0o755 if info.st_mode & 0o111 else 0o644}
    return result


def manifest_digest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def copy_code(source: Path, destination: Path, manifest: dict, shared: dict) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    counts = {"copied_files": 0, "shared_files": 0}
    for relative, item in sorted(manifest.items()):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if item["kind"] == "link":
            if target.is_symlink():
                if os.readlink(target) != item["target"]:
                    raise ValueError("staged_source_link_changed")
                continue
            target.symlink_to(item["target"])
            continue
        key = (item["sha256"], item["mode"])
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file() or digest(target) != item["sha256"]:
                raise ValueError("staged_source_file_changed")
            shared.setdefault(key, target)
            continue
        if key in shared:
            os.link(shared[key], target)
            counts["shared_files"] += 1
        else:
            shutil.copyfile(source / relative, target)
            target.chmod(item["mode"])
            shared[key] = target
            counts["copied_files"] += 1
    return counts


def make_readable_program_tree(path: Path) -> None:
    for folder, directories, files in os.walk(path, followlinks=False):
        Path(folder).chmod(0o755)
        for name in files + directories:
            child = Path(folder) / name
            if child.is_symlink():
                os.lchown(child, 0, 0)
            elif child.is_file():
                child.chmod(0o755 if child.stat().st_mode & 0o111 else 0o644)
                os.chown(child, 0, 0)
            elif not child.is_dir():
                raise ValueError("unexpected_program_file_type")
        os.chown(folder, 0, 0)


def bind_core_metadata(profile: dict, code: Path, venv: Path) -> None:
    site = venv / "lib/python3.11/site-packages"
    pth = site / "vektor-code.pth"
    pth.write_text(str(code) + "\n", encoding="utf-8")
    version = profile["packages"]["hermes-agent"]
    if not VERSION_RE.fullmatch(version):
        raise ValueError("invalid_core_metadata_version")
    info = site / f"hermes_agent-{version}.dist-info"
    info.mkdir(exist_ok=True)
    written = [pth]
    for name, content in profile["metadata"].items():
        if name not in {"METADATA", "WHEEL", "entry_points.txt", "top_level.txt"}:
            raise ValueError("unexpected_core_metadata_file")
        path = info / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    for name, value in profile["entry_points"].items():
        module, separator, function = value.partition(":")
        if not separator or not PACKAGE_RE.fullmatch(name) or not IDENTIFIER_RE.fullmatch(module) or not IDENTIFIER_RE.fullmatch(function):
            raise ValueError("unsupported_console_entry_point")
        path = venv / "bin" / name
        path.write_text(
            f"#!{venv / 'bin/python'}\nimport sys\nfrom {module} import {function.split('.')[0]}\n"
            f"if __name__ == '__main__':\n    sys.exit({function}())\n", encoding="utf-8")
        path.chmod(0o755)
        written.append(path)
    (info / "INSTALLER").write_text("vektor-shared-runtime\n", encoding="utf-8")
    written.append(info / "INSTALLER")
    (info / "direct_url.json").write_text(json.dumps({"url": code.as_uri(), "dir_info": {"editable": True}}), encoding="utf-8")
    written.append(info / "direct_url.json")
    rows = []
    for path in written:
        checksum = base64.urlsafe_b64encode(bytes.fromhex(digest(path))).decode().rstrip("=")
        rows.append((os.path.relpath(path, site), f"sha256={checksum}", path.stat().st_size))
    rows.append((str((info / "RECORD").relative_to(site)), "", ""))
    output = io.StringIO()
    csv.writer(output).writerows(rows)
    (info / "RECORD").write_text(output.getvalue(), encoding="utf-8")


def import_smoke(profile: dict, code: Path, venv: Path) -> None:
    entry = pwd.getpwnam(profile["owner"])
    with tempfile.TemporaryDirectory(prefix="vektor-runtime-import-") as raw:
        home = Path(raw)
        os.chown(home, entry.pw_uid, entry.pw_gid)
        env = safe_env(entry)
        env["HERMES_HOME"] = str(home)
        program = """
import importlib.metadata as m, json, pathlib, sys
import hermes_cli, telegram, psycopg, docx, openpyxl, pptx, pypdf, reportlab, PIL, openai
from plugins.platforms.telegram.adapter import TelegramAdapter
from tools import cronjob_tools
print(json.dumps({'code':str(pathlib.Path(hermes_cli.__file__).resolve()),'prefix':sys.prefix,
 'packages':{d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions() if d.metadata.get('Name')}}))
"""
        value = json.loads(command([venv / "bin/python", "-I", "-c", program], entry=entry, env=env, cwd=code, timeout=60).splitlines()[-1])
        if not Path(value["code"]).is_relative_to(code) or value["packages"] != profile["packages"]:
            raise ValueError("shared_runtime_import_or_dependency_mismatch")


def prepare(owners: list[str], *, apply: bool, resume: bool = False) -> dict:
    if os.geteuid() != 0:
        raise ValueError("administrator_required")
    if len(set(owners)) != len(owners):
        raise ValueError("duplicate_owner")
    profiles = [inspect_profile(owner) for owner in owners]
    if not profiles or len({p["python"] for p in profiles}) != 1:
        raise ValueError("profiles_require_different_python_versions")
    for profile in profiles[1:]:
        if profile["packages"] != profiles[0]["packages"]:
            raise ValueError("profiles_require_different_dependency_sets")
    manifests = {p["owner"]: source_manifest(Path(p["source"])) for p in profiles}
    dependencies_id = manifest_digest({"python": profiles[0]["python"], "packages": profiles[0]["packages"]})
    releases = {p["owner"]: "hermes-0.20.0-" + manifest_digest(manifests[p["owner"]])[:12] + "-" + dependencies_id[:8] for p in profiles}
    result = {"profiles": releases, "python": profiles[0]["python"],
              "distributions": len(profiles[0]["packages"]), "dependencies_id": dependencies_id}
    if not apply:
        return result
    marker = ROOT / "admin/preparation.json"
    if ROOT.exists():
        if not resume or ROOT.is_symlink() or ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o022:
            raise ValueError("managed_root_already_exists_review_before_reusing")
        if not marker.is_file() or marker.is_symlink() or marker.stat().st_uid != 0:
            raise ValueError("preparation_manifest_missing_or_untrusted")
        if json.loads(marker.read_text())["plan"] != result:
            raise ValueError("preparation_inputs_changed")
    if ROOT.parent.is_symlink() or ROOT.parent.stat().st_uid != 0 or ROOT.parent.stat().st_mode & 0o022:
        raise ValueError("managed_root_parent_untrusted")
    for name in ("tools", "python", "releases", "profiles", "admin", "cache", "backups"):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    ROOT.chmod(0o755)
    (ROOT / "cache").chmod(0o700)
    (ROOT / "backups").chmod(0o700)
    marker.write_text(json.dumps({"state": "building", "plan": result}, indent=2) + "\n", encoding="utf-8")
    uv_sources = [Path("/home") / owner / ".hermes/bin/uv" for owner in owners]
    for owner, path in zip(owners, uv_sources):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != pwd.getpwnam(owner).pw_uid or info.st_mode & 0o022:
            raise ValueError("uv_source_untrusted")
    if len({digest(path) for path in uv_sources}) != 1:
        raise ValueError("uv_binaries_differ")
    uv_dir = ROOT / "tools/uv-0.12.3"
    uv_dir.mkdir(exist_ok=True)
    uv = uv_dir / "uv"
    if uv.exists() and (uv.is_symlink() or digest(uv) != digest(uv_sources[0])):
        raise ValueError("prepared_uv_changed")
    if not uv.exists():
        shutil.copyfile(uv_sources[0], uv)
    uv.chmod(0o755)
    uvx_source = uv_sources[0].with_name("uvx")
    if uvx_source.is_file() and not (uv_dir / "uvx").exists():
        shutil.copyfile(uvx_source, uv_dir / "uvx")
        (uv_dir / "uvx").chmod(0o755)
    if not command([uv, "--version"]).startswith("uv 0.12.3 "):
        raise ValueError("unexpected_uv_version")
    progress("installing_shared_python", version=result["python"])
    command([uv, "--no-config", "python", "install", "--install-dir", ROOT / "python", "--no-bin", result["python"]])
    python = Path(command([uv, "--no-config", "python", "find", "--managed-python", result["python"]]))
    if not python.resolve().is_relative_to(ROOT / "python"):
        raise ValueError("shared_python_outside_managed_root")
    pins = "".join(f"{name}=={version}\n" for name, version in sorted(profiles[0]["packages"].items()) if name != "hermes-agent")
    lock = ROOT / "admin/requirements.lock"
    lock.write_text(pins, encoding="utf-8")
    shared = {}
    for profile in profiles:
        release_id = releases[profile["owner"]]
        release = ROOT / "releases" / release_id
        if (release / "runtime.json").exists():
            ready = json.loads((release / "runtime.json").read_text())
            if ready.get("state") != "ready" or ready.get("release_id") != release_id:
                raise ValueError("existing_release_not_ready")
            for relative, item in manifests[profile["owner"]].items():
                if item["kind"] == "file":
                    candidate = release / "hermes-agent" / relative
                    if candidate.is_symlink() or digest(candidate) != item["sha256"]:
                        raise ValueError("existing_release_code_changed")
                    shared.setdefault((item["sha256"], item["mode"]), candidate)
            import_smoke(profile, release / "hermes-agent", release / "venv")
            continue
        progress("preparing_code", profile=profile["owner"], release_id=release_id)
        code = release / "hermes-agent"
        counts = copy_code(Path(profile["source"]), code, manifests[profile["owner"]], shared)
        (release / "code-manifest.json").write_text(json.dumps(manifests[profile["owner"]], sort_keys=True), encoding="utf-8")
        venv = release / "venv"
        if not (venv / "pyvenv.cfg").exists():
            command([uv, "--no-config", "venv", "--python", python, venv])
        progress("installing_pinned_dependencies", profile=profile["owner"])
        command([uv, "--no-config", "pip", "install", "--python", venv / "bin/python", "--no-build",
                 "--link-mode", "hardlink", "--requirement", lock], cwd=ROOT, timeout=600)
        bind_core_metadata(profile, code, venv)
        if not (code / "venv").is_symlink():
            (code / "venv").symlink_to(venv)
        make_readable_program_tree(release)
        make_readable_program_tree(ROOT / "python")
        helper_path = Path(__file__).with_name("shared_runtime_layout.py")
        if not helper_path.exists():
            helper_path = Path(__file__).resolve().parents[1] / "passive-secretary/shared_runtime_layout.py"
        validator = runpy.run_path(str(helper_path))["validate_program_tree"]
        validator(code)
        validator(venv)
        import_smoke(profile, code, venv)
        if source_manifest(Path(profile["source"])) != manifests[profile["owner"]]:
            raise ValueError("source_changed_during_preparation")
        ready = {"schema_version": 1, "state": "ready", "release_id": release_id,
                 "python": result["python"], "dependencies_sha256": digest(lock), **counts}
        (release / "runtime.json").write_text(json.dumps(ready, indent=2) + "\n", encoding="utf-8")
        progress("release_ready", profile=profile["owner"], **counts)
    (ROOT / "admin/prepared-profiles.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    marker.write_text(json.dumps({"state": "ready", "plan": result}, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owners", nargs="+", required=True)
    parser.add_argument("--apply", action="store_true", help="Prepare new /opt/vektor artifacts; does not switch profiles")
    parser.add_argument("--resume", action="store_true", help="Resume only a matching, administrator-owned preparation")
    args = parser.parse_args()
    print(json.dumps(prepare(args.owners, apply=args.apply, resume=args.resume), ensure_ascii=False))


if __name__ == "__main__":
    main()
