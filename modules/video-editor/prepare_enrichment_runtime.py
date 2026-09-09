#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path("/opt/vektor/video-editor")
ENGINE = ROOT / "engine" / "e8ea406bc2440ca8fc8d1b239c8758e9de112388"
HYPERFRAMES_VERSION = "0.8.30"
HYPERFRAMES_INTEGRITY = "sha512-SH+Cn0Ct0ZYSWqZ09QfBszQ93dOhBDxYXG5ARdjiFGGGNGhr2Rldyk8OFBKq85qhhiH/PdPspnNki39i6k6mEg=="
GSAP_VERSION = "3.14.2"
GSAP_INTEGRITY = "sha512-P8/mMxVLU7o4+55+1TCnQrPmgjPKnwkzkXOK1asnR9Jg2lna4tEY5qBJjMmAaOBDDZWtlRjBXjLa0w53G/uBLA=="
PLAYWRIGHT_VERSION = "1.62.0"
HYPERFRAMES_CHROME_VERSION = "152.0.7977.30"
HYPERFRAMES_CHROME_SHA256 = "6642ab58861e8dedcd195e1fe090f76dae4cff8476f971911a11781e7ec7d2da"
HYPERFRAMES_HOME = ROOT / "hyperframes-home"
HYPERFRAMES_BROWSER = HYPERFRAMES_HOME / ".cache" / "hyperframes" / "chrome" / "chrome-headless-shell" / f"linux-{HYPERFRAMES_CHROME_VERSION}" / "chrome-headless-shell-linux64" / "chrome-headless-shell"
NPM_CACHE = ROOT / "npm-cache"
CAPTURE_VENV = ROOT / "capture-venv"
BROWSERS = ROOT / "playwright-browsers"
SFX = ROOT / "sfx"


def run(cmd: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None) -> None:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    subprocess.run(cmd, check=True, env=merged, cwd=str(cwd) if cwd else None)



def make_shared_readonly(root: Path) -> None:
    if not root.exists():
        return
    for item in [root, *root.rglob("*")]:
        try:
            if item.is_dir():
                item.chmod(0o755)
            elif item.is_file():
                mode = item.stat().st_mode
                item.chmod(0o755 if mode & 0o111 else 0o644)
        except OSError:
            pass

def prepare_hyperframes(module: Path) -> None:
    NPM_CACHE.mkdir(parents=True, exist_ok=True)
    source = module / "hyperframes-runtime"
    package = json.loads((source / "package-lock.json").read_text(encoding="utf-8"))
    packages = package.get("packages") or {}
    pinned = packages.get("node_modules/hyperframes") or {}
    gsap = packages.get("node_modules/gsap") or {}
    if pinned.get("version") != HYPERFRAMES_VERSION or pinned.get("integrity") != HYPERFRAMES_INTEGRITY:
        raise RuntimeError("hyperframes_lock_invalid")
    if gsap.get("version") != GSAP_VERSION or gsap.get("integrity") != GSAP_INTEGRITY:
        raise RuntimeError("gsap_lock_invalid")
    target = ROOT / "hyperframes"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "package.json", target / "package.json")
    shutil.copy2(source / "package-lock.json", target / "package-lock.json")
    run(["npm", "ci", "--ignore-scripts", "--cache", str(NPM_CACHE)], cwd=target)
    installed = json.loads((target / "node_modules" / "hyperframes" / "package.json").read_text(encoding="utf-8"))
    installed_gsap = json.loads((target / "node_modules" / "gsap" / "package.json").read_text(encoding="utf-8"))
    if installed.get("version") != HYPERFRAMES_VERSION:
        raise RuntimeError("hyperframes_install_wrong_version")
    if installed_gsap.get("version") != GSAP_VERSION:
        raise RuntimeError("gsap_install_wrong_version")
    bindir = ROOT / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    wrapper = bindir / "npx"
    real = target / "node_modules" / ".bin" / "hyperframes"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"REAL={str(real)!r}\n"
        f"PIN='hyperframes@{HYPERFRAMES_VERSION}'\n"
        "args=sys.argv[1:]\n"
        "if len(args) >= 3 and args[0] == '--yes' and args[1] == PIN and args[2] in {'check','render'}:\n"
        "    os.execv(REAL, [REAL, *args[2:]])\n"
        "sys.stderr.write('video-editor npx wrapper blocked command\\n')\n"
        "raise SystemExit(64)\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    prepare_hyperframes_browser(target)
    # No network or broad CLI surface is needed at runtime; compose.py only calls check/render.



def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def prepare_hyperframes_browser(target: Path) -> None:
    HYPERFRAMES_HOME.mkdir(parents=True, exist_ok=True)
    env = {"HOME": str(HYPERFRAMES_HOME)}
    real = target / "node_modules" / ".bin" / "hyperframes"
    if not HYPERFRAMES_BROWSER.is_file():
        run([str(real), "browser", "ensure"], env=env, cwd=target)
    if not HYPERFRAMES_BROWSER.is_file():
        raise RuntimeError("hyperframes_browser_missing")
    version = subprocess.check_output([str(HYPERFRAMES_BROWSER), "--version"], text=True).strip()
    if HYPERFRAMES_CHROME_VERSION not in version:
        raise RuntimeError("hyperframes_browser_wrong_version")
    if _sha256(HYPERFRAMES_BROWSER) != HYPERFRAMES_CHROME_SHA256:
        raise RuntimeError("hyperframes_browser_sha256_mismatch")
    for item in [HYPERFRAMES_HOME, *HYPERFRAMES_HOME.rglob("*")]:
        if item.is_dir():
            item.chmod(0o755)
        elif item.is_file():
            item.chmod(0o644 if not (item.stat().st_mode & 0o111) else 0o755)

def install_capture_wrappers(module: Path, capture_dir: Path) -> None:
    capture_dir.mkdir(parents=True, exist_ok=True)
    for name in ("safe_capture.py", "egress_proxy.py"):
        source = module / name
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"capture_wrapper_missing:{name}")
        shutil.copy2(source, capture_dir / name)
        os.chmod(capture_dir / name, 0o755 if name == "safe_capture.py" else 0o644)


def prepare_capture(module: Path) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv_missing")
    python_link = CAPTURE_VENV / "bin" / "python"
    rebuild = not python_link.exists()
    if python_link.exists():
        try:
            rebuild = str(python_link.resolve()).startswith("/root/")
        except OSError:
            rebuild = True
    if rebuild and CAPTURE_VENV.exists():
        shutil.rmtree(CAPTURE_VENV)
    if rebuild:
        run([uv, "venv", "--python", "/usr/bin/python3", str(CAPTURE_VENV)])
    run([uv, "pip", "install", "--python", str(CAPTURE_VENV / "bin" / "python"), f"playwright=={PLAYWRIGHT_VERSION}"])
    BROWSERS.mkdir(parents=True, exist_ok=True)
    env = {"PLAYWRIGHT_BROWSERS_PATH": str(BROWSERS)}
    run([str(CAPTURE_VENV / "bin" / "playwright"), "install", "chromium"], env=env)
    make_shared_readonly(CAPTURE_VENV)
    make_shared_readonly(BROWSERS)
    install_capture_wrappers(module, ROOT / "capture")


def prepare_sfx() -> None:
    script = ENGINE / "scripts" / "sfx_library.py"
    if not script.is_file():
        raise RuntimeError("sfx_installer_missing")
    run([shutil.which("python3") or "python3", str(script), "--library", str(SFX), "install"])
    for path in [SFX, *SFX.rglob("*")]:
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o644)


def main() -> int:
    if os.geteuid() != 0:
        raise SystemExit("root_required")
    if not ENGINE.is_dir():
        raise SystemExit("engine_missing")
    module = Path(__file__).resolve().parent
    prepare_hyperframes(module)
    prepare_capture(module)
    prepare_sfx()
    manifest = {
        "hyperframes": {"version": HYPERFRAMES_VERSION, "integrity": HYPERFRAMES_INTEGRITY, "browser_version": HYPERFRAMES_CHROME_VERSION, "browser_sha256": HYPERFRAMES_CHROME_SHA256, "browser_path": str(HYPERFRAMES_BROWSER)},
        "gsap": {"version": GSAP_VERSION, "integrity": GSAP_INTEGRITY, "path": str(ROOT / "hyperframes" / "node_modules" / "gsap" / "dist" / "gsap.min.js")},
        "playwright": {"version": PLAYWRIGHT_VERSION, "browsers_path": str(BROWSERS)},
        "sfx": {"path": str(SFX)},
    }
    path = ROOT / "enrichment.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o644)
    print("enrichment_runtime_prepared=true")
    print(f"hyperframes={HYPERFRAMES_VERSION}")
    print(f"gsap={GSAP_VERSION}")
    print(f"playwright={PLAYWRIGHT_VERSION}")
    print(f"sfx={SFX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
