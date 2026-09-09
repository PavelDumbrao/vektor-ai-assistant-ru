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
TARGET = Path('/opt/proai-hermes-manager')
SECRET = Path('/etc/proai-hermes-manager.env')
UNIT = Path('/etc/systemd/system/proai-hermes-manager.service')
TOKEN_RE = re.compile(r'^\d{5,}:[A-Za-z0-9_-]{20,}$')


def main() -> int:
    if os.geteuid() != 0:
        raise RuntimeError('root_required')
    token = sys.stdin.read(256).strip()
    if not TOKEN_RE.fullmatch(token):
        raise ValueError('invalid_token_shape')
    TARGET.mkdir(parents=True, exist_ok=True, mode=0o700)
    (TARGET / 'state').mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ('manager.py', 'configure.py'):
        shutil.copy2(HERE / name, TARGET / name)
        os.chmod(TARGET / name, 0o600)
    fd, temp_name = tempfile.mkstemp(prefix='.hermes-manager-', dir='/etc')
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        out.write('PROAI_HERMES_MANAGER_TOKEN=' + token + '\n')
        out.write('PROAI_HERMES_MANAGER_USERNAME=ProAIHermesBot\n')
        out.flush()
        os.fsync(out.fileno())
    os.chmod(temp_name, 0o600)
    os.chown(temp_name, 0, 0)
    os.replace(temp_name, SECRET)
    shutil.copy2(HERE / 'proai-hermes-manager.service', UNIT)
    os.chmod(UNIT, 0o644)
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
