#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULES = HERE.parent
TARGET = Path('/opt/proai-hermes-manager')
SECRET = Path('/etc/proai-hermes-manager.env')
UNIT = Path('/etc/systemd/system/proai-hermes-manager.service')
PROVISIONER_UNIT = Path('/etc/systemd/system/proai-hermes-provisioner@.service')
DEFAULT_STABLE_RELEASE = 'hermes-0.21.0-29112bef-vektor4-modern'
TOKEN_RE = re.compile(r'^\d{5,}:[A-Za-z0-9_-]{20,}$')


def main() -> int:
    if os.geteuid() != 0:
        raise RuntimeError('root_required')
    token = sys.stdin.read(256).strip()
    if not TOKEN_RE.fullmatch(token):
        raise ValueError('invalid_token_shape')
    stable_release = os.environ.get('FORGE_STABLE_RELEASE_ID', '').strip()
    if not stable_release and SECRET.is_file():
        for raw in SECRET.read_text(encoding='utf-8').splitlines():
            if raw.startswith('FORGE_STABLE_RELEASE_ID='):
                stable_release = raw.partition('=')[2].strip()
                break
    stable_release = stable_release or DEFAULT_STABLE_RELEASE
    runtime = Path('/opt/vektor/releases') / stable_release / 'runtime.json'
    if not runtime.is_file():
        raise ValueError('stable_release_missing')
    TARGET.mkdir(parents=True, exist_ok=True, mode=0o700)
    (TARGET / 'state').mkdir(parents=True, exist_ok=True, mode=0o700)
    for child in ('managed', 'instances', 'provisioning', 'jobs'):
        (TARGET / 'state' / child).mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ('manager.py', 'configure.py', 'hermes_instance.py', 'provisioner.py', 'seed_platform_secrets.py'):
        shutil.copy2(HERE / name, TARGET / name)
        os.chmod(TARGET / name, 0o600)
    for dirname in ('templates',):
        target = TARGET / dirname
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(HERE / dirname, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    vendor = TARGET / 'vendor'
    vendor.mkdir(mode=0o700, exist_ok=True)
    for module in ('passive-secretary-postgres', 'passive-secretary', 'maton-onboarding', 'grsai-image-provider'):
        target = vendor / module
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(MODULES / module, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache'))
    for root, dirs, files in os.walk(TARGET):
        os.chown(root, 0, 0)
        if Path(root).is_relative_to(TARGET / 'state'):
            os.chmod(root, 0o700)
        for filename in files:
            path = Path(root) / filename
            os.chown(path, 0, 0)
            if path != TARGET / 'avatar.jpg':
                os.chmod(path, 0o600)
    fd, temp_name = tempfile.mkstemp(prefix='.hermes-manager-', dir='/etc')
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        out.write('PROAI_HERMES_MANAGER_TOKEN=' + token + '\n')
        out.write('PROAI_HERMES_MANAGER_USERNAME=ProAIHermesBot\n')
        out.write('FORGE_STABLE_RELEASE_ID=' + stable_release + '\n')
        out.flush()
        os.fsync(out.fileno())
    os.chmod(temp_name, 0o600)
    os.chown(temp_name, 0, 0)
    os.replace(temp_name, SECRET)
    shutil.copy2(HERE / 'proai-hermes-manager.service', UNIT)
    os.chmod(UNIT, 0o644)
    shutil.copy2(HERE / 'proai-hermes-provisioner@.service', PROVISIONER_UNIT)
    os.chmod(PROVISIONER_UNIT, 0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'proai-hermes-manager.service'], check=True)
    print('installed=true')
    print('secret_printed=false')
    print('service=proai-hermes-manager.service')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('error=' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
