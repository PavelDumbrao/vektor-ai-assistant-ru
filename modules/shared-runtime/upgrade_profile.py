#!/usr/bin/env python3
"""Switch an already-managed idle profile to a verified release with rollback.

The administrator must first prove old/new SessionDB compatibility on copies.
Rollback changes only the program binding; it never overwrites newer user data.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import runpy
import stat
import tarfile
import tempfile
from pathlib import Path

import prepare_runtime as builder
import switch_profile as common

ROOT = Path('/opt/vektor')
UNIT_ROOT = Path('/etc/systemd/system')


def atomic_link(path: Path, target: Path, uid: int, gid: int):
    fd, raw = tempfile.mkstemp(prefix='.vektor-link-', dir=path.parent)
    os.close(fd)
    temporary = Path(raw)
    temporary.unlink()
    try:
        temporary.symlink_to(target)
        os.lchown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def snapshot(home: Path, backup: Path):
    """Archive private state while its gateway is stopped, never following links."""
    archive = backup / 'profile-state.tar.gz'
    excluded = {'hermes-agent', 'backups', 'logs', 'state-snapshots'}

    def keep(member):
        parts = Path(member.name).parts
        if len(parts) > 1 and parts[1] in excluded:
            return None
        if member.isfifo() or member.isdev():
            return None
        return member

    with tarfile.open(archive, 'x:gz', dereference=False) as output:
        output.add(home, arcname='profile', filter=keep)
    archive.chmod(0o600)
    with tarfile.open(archive, 'r:gz') as check:
        names = check.getnames()
        for name in ('.env', 'config.yaml', 'SOUL.md', 'state.db'):
            if f'profile/{name}' not in names:
                raise ValueError('private_snapshot_incomplete')
    return {'archive_sha256': builder.digest(archive), 'archive_bytes': archive.stat().st_size}


def upgrade(owner: str, release_id: str, *, apply: bool):
    if os.geteuid() != 0 or not common.OWNER_RE.fullmatch(owner):
        raise ValueError('administrator_and_valid_owner_required')
    layout = runpy.run_path(str(common.helper_path()))
    if not layout['RELEASE_RE'].fullmatch(release_id):
        raise ValueError('invalid_release_id')
    entry = pwd.getpwnam(owner)
    home = Path(entry.pw_dir) / '.hermes'
    binding = layout['load_shared_runtime'](owner, home, entry.pw_uid)
    if binding is None:
        raise ValueError('managed_profile_required')
    release = ROOT / 'releases' / release_id
    ready = layout['_json'](release / 'runtime.json')
    if ready.get('state') != 'ready' or ready.get('release_id') != release_id:
        raise ValueError('release_not_verified')
    if ready.get('schema_rollback_compatible') is not True:
        raise ValueError('database_compatibility_not_verified')
    code = release / 'hermes-agent'
    layout['validate_program_tree'](code)
    layout['validate_program_tree'](release / 'venv')
    expected = json.loads((release / 'code-manifest.json').read_text())
    if builder.source_manifest(code) != expected:
        raise ValueError('release_code_changed')
    old_code = binding['code']
    old_manifest = binding['release'] / 'code-manifest.json'
    layout['_root_node'](old_manifest, directory=False)
    old_expected = json.loads(old_manifest.read_text())
    if builder.source_manifest(old_code) != old_expected:
        raise ValueError('current_runtime_drift_requires_reconciliation')
    if code == old_code:
        return {'owner': owner, 'state': 'already_current', 'release_id': release_id}
    state = common.service_state(owner)
    gateway = json.loads((home / 'gateway_state.json').read_text())
    leases = json.loads((home / 'runtime/active_sessions.json').read_text())['entries']
    if (state.get('User') != owner or state.get('WorkingDirectory') != str(home)
            or state.get('ActiveState') != 'active' or leases != []
            or gateway.get('active_agents', 0) != 0
            or str(gateway.get('pid')) != state.get('MainPID')):
        raise ValueError('profile_not_idle_or_identity_changed')
    registration = ROOT / 'profiles' / f'{owner}.json'
    old_registration = registration.read_bytes()
    new_registration = json.dumps({'schema_version': 1, 'owner': owner, 'release_id': release_id}).encode()
    unit = UNIT_ROOT / f'{owner}-hermes.service'
    layout['_root_node'](unit, directory=False)
    unit_hash = builder.digest(unit)
    protected = common.protected_hashes(home)
    link = home / 'hermes-agent'
    receipt = {'owner': owner, 'old_release_id': old_code.parent.name,
               'release_id': release_id, 'preflight': 'passed'}
    if not apply:
        return receipt
    backup = Path(tempfile.mkdtemp(prefix=f'upgrade-{owner}-', dir=ROOT / 'backups'))
    common.atomic_write(backup / 'registration-before.json', old_registration)
    common.atomic_write(backup / 'unit-before.service', unit.read_bytes())
    stopped = changed = False
    try:
        # Recheck after slow tree validation, before interrupting a gateway.
        fresh = json.loads((home / 'gateway_state.json').read_text())
        if (common.service_state(owner) != state or fresh.get('active_agents', 0)
                or json.loads((home / 'runtime/active_sessions.json').read_text())['entries']):
            raise ValueError('profile_changed_before_stop')
        common.command('systemctl', 'stop', f'{owner}-hermes.service')
        stopped = True
        if (common.service_state(owner).get('MainPID') != '0'
                or builder.digest(unit) != unit_hash or link.resolve() != old_code
                or registration.read_bytes() != old_registration):
            raise RuntimeError('concurrent_change_requires_review')
        if (builder.source_manifest(old_code) != old_expected
                or builder.source_manifest(code) != expected):
            raise RuntimeError('runtime_changed_during_upgrade')
        receipt.update(snapshot(home, backup))
        if any(builder.digest(path) != value for path, value in protected.items()):
            raise ValueError('private_configuration_changed_before_switch')
        atomic_link(link, code, entry.pw_uid, entry.pw_gid)
        changed = True
        common.atomic_write(registration, new_registration, 0o644)
        layout['load_shared_runtime'](owner, home, entry.pw_uid)
        common.command('systemctl', 'start', f'{owner}-hermes.service')
        live = common.wait_ready(owner, home)
        live_gateway = json.loads((home / 'gateway_state.json').read_text())
        if live_gateway.get('code_version') != ready['version']:
            raise ValueError('running_gateway_version_mismatch')
        if any(builder.digest(path) != value for path, value in protected.items()):
            raise ValueError('private_configuration_changed_after_switch')
        receipt.update(state='active', pid=live['MainPID'], version=ready['version'],
                       protected_files_unchanged=True, backup=str(backup))
        common.atomic_write(backup / 'upgrade.json', json.dumps(receipt, indent=2).encode())
        return receipt
    except Exception:
        if stopped:
            if builder.digest(unit) != unit_hash:
                raise RuntimeError('rollback_requires_review_unit_changed')
            if changed:
                if link.resolve() != code or registration.read_bytes() not in (old_registration, new_registration):
                    raise RuntimeError('rollback_requires_review_binding_changed')
                common.command('systemctl', 'stop', f'{owner}-hermes.service')
                atomic_link(link, old_code, entry.pw_uid, entry.pw_gid)
                common.atomic_write(registration, old_registration, 0o644)
            common.command('systemctl', 'start', f'{owner}-hermes.service')
            common.wait_ready(owner, home)
        receipt.update(state='rolled_back' if stopped else 'refused', backup=str(backup))
        common.atomic_write(backup / 'upgrade.json', json.dumps(receipt, indent=2).encode())
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--release-id', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('administrator_required')
    fd = os.open('/run/lock/vektor-shared-runtime.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid or info.st_nlink != 1 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError('upgrade_lock_untrusted')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(upgrade(args.owner, args.release_id, apply=args.apply)), flush=True)
    finally:
        os.close(fd)
