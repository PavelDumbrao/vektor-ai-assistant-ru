#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path("/opt/vektor/video-editor")
ENGINE_REPO = "https://github.com/ranahaani/i-hate-editing.git"
ENGINE_COMMIT = "e8ea406bc2440ca8fc8d1b239c8758e9de112388"
WHISPER_RELEASE = "v1.9.2"
WHISPER_COMMIT = "306c88f4d1286aec1bf96e544632897886af5501"
WHISPER_ASSET = "whisper-bin-ubuntu-x64.tar.gz"
WHISPER_URL = f"https://github.com/ggml-org/whisper.cpp/releases/download/{WHISPER_RELEASE}/{WHISPER_ASSET}"
WHISPER_SHA256 = "46811a3ecf584307480a220b9ef5ff81b7b22dc41577cbc274ce3afc61f753b1"
MODEL_REVISION = "c521a4b02f422512d734391fdf08bb08c0862f68"
MODELS = {
    "fast": ("ggml-small.bin", "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"),
    "quality": ("ggml-medium.bin", "6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208"),
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, target: Path, expected: str) -> None:
    if target.is_file() and sha256(target) == expected:
        print(f"cached={target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".download-", dir=target.parent)
    os.close(fd)
    tmp = Path(name)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ProAI-Hermes-video-editor/0.1"})
        with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as out:
            while True:
                chunk = resp.read(8 * 1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        actual = sha256(tmp)
        if actual != expected:
            raise RuntimeError(f"sha256_mismatch:{target.name}:{actual}")
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def prepare_engine() -> Path:
    target = ROOT / "engine" / ENGINE_COMMIT
    if target.is_dir() and (target / ".git").is_dir():
        head = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip()
        if head == ENGINE_COMMIT:
            return target
        raise RuntimeError("engine_checkout_wrong_commit")
    if target.exists():
        raise RuntimeError("engine_target_not_clean")
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout", ENGINE_REPO, str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "fetch", "--depth", "1", "origin", ENGINE_COMMIT], check=True)
    subprocess.run(["git", "-C", str(target), "checkout", "--detach", ENGINE_COMMIT], check=True)
    if subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip() != ENGINE_COMMIT:
        raise RuntimeError("engine_pin_verification_failed")
    return target


def prepare_whisper() -> Path:
    archive = ROOT / "downloads" / WHISPER_ASSET
    download(WHISPER_URL, archive, WHISPER_SHA256)
    release_root = ROOT / "whisper" / WHISPER_RELEASE
    extracted = release_root / "whisper-bin-ubuntu-x64"
    cli = extracted / "whisper-cli"
    if not cli.is_file():
        release_root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:gz") as tf:
            members = tf.getmembers()
            if any(m.name.startswith("/") or ".." in Path(m.name).parts for m in members):
                raise RuntimeError("unsafe_whisper_archive")
            tf.extractall(release_root)
    if not cli.is_file():
        raise RuntimeError("whisper_cli_missing_after_extract")
    bindir = ROOT / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    launcher = bindir / "whisper-cli"
    if launcher.exists() or launcher.is_symlink():
        launcher.unlink()
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"REAL = {str(cli)!r}\n"
        f"LIBDIR = {str(extracted)!r}\n"
        "args = list(sys.argv[1:])\n"
        "model = ''\n"
        "for i, arg in enumerate(args[:-1]):\n"
        "    if arg in ('-m', '--model'):\n"
        "        model = args[i + 1]\n"
        "        break\n"
        "if model.endswith('ggml-small.bin'):\n"
        "    if '-bs' not in args and '--beam-size' not in args:\n"
        "        args += ['-bs', '1']\n"
        "    if '-bo' not in args and '--best-of' not in args:\n"
        "        args += ['-bo', '1']\n"
        "env = os.environ.copy()\n"
        "env['LD_LIBRARY_PATH'] = LIBDIR + (os.pathsep + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')\n"
        "os.execve(REAL, [REAL, *args], env)\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


def prepare_models(skip: bool) -> dict[str, Path]:
    if skip:
        return {}
    out = {}
    for quality, (name, digest) in MODELS.items():
        target = ROOT / "models" / name
        url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/{MODEL_REVISION}/{name}"
        download(url, target, digest)
        out[quality] = target
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-model", action="store_true")
    args = ap.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("root_required")
    ROOT.mkdir(parents=True, exist_ok=True)
    ROOT.chmod(0o755)
    engine = prepare_engine()
    whisper = prepare_whisper()
    models = prepare_models(args.skip_model)
    lock = ROOT / "runtime.lock"
    lock.touch(exist_ok=True)
    # Profiles need only flock() permission, not write permission.
    lock.chmod(0o644)
    manifest = {
        "engine": {"repo": ENGINE_REPO, "commit": ENGINE_COMMIT, "path": str(engine)},
        "whisper": {"release": WHISPER_RELEASE, "commit": WHISPER_COMMIT, "asset_sha256": WHISPER_SHA256, "path": str(whisper)},
        "models": {quality: {"name": MODELS[quality][0], "revision": MODEL_REVISION, "sha256": MODELS[quality][1], "path": str(path)} for quality, path in models.items()},
    }
    (ROOT / "runtime.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (ROOT / "runtime.json").chmod(0o644)
    print("runtime_prepared=true")
    print(f"engine_commit={ENGINE_COMMIT}")
    print(f"whisper_release={WHISPER_RELEASE}")
    print(f"models={'skipped' if not models else ','.join(sorted(models))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
