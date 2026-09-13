"""Fixed, tenant-scoped Living Memory onboarding; no arbitrary command API."""
from __future__ import annotations

import hmac
import json
import os
import stat
import subprocess
from pathlib import Path

HOME_ROOT = Path('/home')
ROOT = Path('/opt/vektor/living-memory')
SYSTEMCTL = '/usr/bin/systemctl'
PRIMARY = 'gpt-5.6-sol'
FALLBACK = 'openai/gpt-5.6-sol'


class MemoryOnboardingError(RuntimeError):
    pass


def require_platform_keys(values: dict[str, str]) -> None:
    primary = values.get('LLM_API_KEY', '')
    reserve = values.get('OPENROUTER_API_KEY', '')
    if not primary or not reserve:
        raise MemoryOnboardingError('living_memory_platform_credentials_missing')
    if hmac.compare_digest(primary, reserve):
        raise MemoryOnboardingError('living_memory_distinct_fallback_required')

def validate_profile(instance, entry, home: Path) -> None:
    instance.validate()
    expected = HOME_ROOT / instance.owner_linux / '.hermes'
    if entry.pw_uid <= 0 or entry.pw_name != instance.owner_linux or home != expected:
        raise MemoryOnboardingError('living_memory_owner_mismatch')
    if Path(entry.pw_dir) != expected.parent:
        raise MemoryOnboardingError('living_memory_home_mismatch')
    for path in (home.parent, home, home / 'memories', home / 'backups', home / 'living_memory'):
        if not path.exists() and not path.is_symlink() and path.name == 'living_memory':
            continue
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != entry.pw_uid or (path != home.parent and info.st_mode & 0o077):
            raise MemoryOnboardingError('living_memory_profile_directory_unsafe')
    for name in ('config.yaml', '.env'):
        path = home / name
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != entry.pw_uid or info.st_mode & 0o077:
            raise MemoryOnboardingError('living_memory_profile_file_unsafe')


def prepare(instance, entry, home: Path, vendor: Path, run) -> None:
    validate_profile(instance, entry, home)
    script = vendor / 'living-memory' / 'install.py'
    info = script.lstat()
    if script.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
        raise MemoryOnboardingError('living_memory_installer_unsafe')
    run(['/usr/bin/python3', str(script), '--owner', instance.owner_linux], timeout=60)
    # Keep an idempotent retry quiet until all new-profile checks pass.
    abort(instance, run)

def _worker(instance, entry, home: Path, action: str, timeout: int) -> dict:
    validate_profile(instance, entry, home)
    if action not in ('--status', '--provider-smoke'):
        raise MemoryOnboardingError('living_memory_probe_invalid')
    worker = ROOT / 'profiles' / instance.owner_linux / 'current' / 'worker.py'
    env = {'HOME': entry.pw_dir, 'USER': entry.pw_name, 'LOGNAME': entry.pw_name,
           'HERMES_HOME': str(home), 'HERMES_PROFILE': instance.owner_linux,
           'PATH': '/usr/local/bin:/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
    try:
        result = subprocess.run(
            [str(home / 'hermes-agent/venv/bin/python'), str(worker),
             '--owner', instance.owner_linux, action],
            user=entry.pw_uid, group=entry.pw_gid, extra_groups=(),
            env=env, cwd=home, capture_output=True, text=True, timeout=timeout,
            check=False)
        if result.returncode or len(result.stdout) > 65536:
            raise MemoryOnboardingError('living_memory_probe_failed')
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        if not isinstance(payload, dict):
            raise ValueError('invalid_json_shape')
        return payload
    except MemoryOnboardingError:
        raise
    except Exception:
        # Never expose stdout/stderr, arguments or provider exceptions.
        raise MemoryOnboardingError('living_memory_probe_failed') from None

def check(instance, entry, home: Path) -> dict:
    status = _worker(instance, entry, home, '--status', 45)
    if (status.get('owner') != instance.owner_linux
            or status.get('version') != '0.1.0'
            or status.get('provider_ready') is not True
            or status.get('fallback_is_separate_key') is not True
            or status.get('fallback_is_separate_route') is not True
            or status.get('primary_model') != PRIMARY
            or status.get('fallback_model') != FALLBACK):
        raise MemoryOnboardingError('living_memory_routes_not_ready')
    probe = _worker(instance, entry, home, '--provider-smoke', 240)
    routes = probe.get('routes', [])
    if (probe.get('ok') is not True or len(routes) != 2
            or {r.get('route') for r in routes} != {'primary', 'fallback'}
            or not all(r.get('ok') is True for r in routes)):
        raise MemoryOnboardingError('living_memory_provider_smoke_failed')
    return {'version': '0.1.0', 'provider_ready': True,
            'primary_model': PRIMARY, 'fallback_model': FALLBACK,
            'separate_key': True, 'separate_route': True,
            'scope': 'owner_direct_telegram_only'}


def abort(instance, run) -> None:
    instance.validate()
    unit = 'vektor-living-memory@' + instance.owner_linux
    run([SYSTEMCTL, 'disable', '--now', unit + '.timer'], timeout=30)
    run([SYSTEMCTL, 'stop', unit + '.service'], timeout=30)

def activate(instance, run) -> dict:
    instance.validate()
    if instance.desired_state != 'running':
        raise MemoryOnboardingError('living_memory_instance_not_running')
    unit = 'vektor-living-memory@' + instance.owner_linux + '.timer'
    run([SYSTEMCTL, 'enable', '--now', unit], timeout=30)
    try:
        result = subprocess.run([SYSTEMCTL, 'show', unit,
            '-p', 'ActiveState', '-p', 'UnitFileState', '-p', 'NextElapseUSecRealtime'],
            capture_output=True, text=True, timeout=10, check=False)
        props = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if (result.returncode or props.get('ActiveState') != 'active'
                or props.get('UnitFileState') != 'enabled'
                or not props.get('NextElapseUSecRealtime')):
            raise MemoryOnboardingError('living_memory_timer_not_ready')
    except MemoryOnboardingError:
        raise
    except Exception:
        raise MemoryOnboardingError('living_memory_timer_not_ready') from None
    return {'timer_enabled': True, 'schedule': 'daily 04:00 Europe/Moscow + <=30m'}
