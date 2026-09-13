from __future__ import annotations
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import living_memory_onboarding as lm
from hermes_instance import HermesInstance


@pytest.fixture
def instance():
    return HermesInstance('hermes-99887766', 'h99887766', 99887766,
        99900001, 'memorytest_bot', 'Memory Test', 'hermes-0.21.0-test').validate()


def ready_status(instance):
    return dict(owner=instance.owner_linux, version='0.1.0', provider_ready=True,
        fallback_is_separate_key=True, fallback_is_separate_route=True,
        primary_model=lm.PRIMARY, fallback_model=lm.FALLBACK)

@pytest.mark.parametrize('values', [{}, {'LLM_API_KEY': 'primary'},
    {'LLM_API_KEY': 'same', 'OPENROUTER_API_KEY': 'same'}])
def test_missing_or_reused_reserve_is_blocked(values):
    with pytest.raises(lm.MemoryOnboardingError):
        lm.require_platform_keys(values)


def test_distinct_reserve_is_valid():
    lm.require_platform_keys({'LLM_API_KEY': 'primary', 'OPENROUTER_API_KEY': 'reserve'})


@pytest.mark.parametrize('field,value', [('owner', 'someone_else'), ('version', '9'),
    ('provider_ready', False), ('fallback_is_separate_key', False),
    ('fallback_is_separate_route', False), ('primary_model', 'weak'),
    ('fallback_model', 'weak')])
def test_bad_route_never_enables_memory(monkeypatch, instance, field, value):
    status = ready_status(instance); status[field] = value
    monkeypatch.setattr(lm, '_worker', lambda *_: status)
    with pytest.raises(lm.MemoryOnboardingError, match='routes_not_ready'):
        lm.check(instance, None, Path('/unused'))


@pytest.mark.parametrize('routes', [[], [{'route': 'primary', 'ok': True}],
    [{'route': 'primary', 'ok': True}, {'route': 'fallback', 'ok': False}],
    [{'route': 'primary', 'ok': True}, {'route': 'primary', 'ok': True}]])
def test_failed_probe_blocks_ready(monkeypatch, instance, routes):
    monkeypatch.setattr(lm, '_worker', lambda *a: ready_status(instance) if a[3] == '--status' else {'ok': True, 'routes': routes})
    with pytest.raises(lm.MemoryOnboardingError, match='provider_smoke_failed'):
        lm.check(instance, None, Path('/unused'))


def test_check_success_filters_private_provider_output(monkeypatch, instance):
    def worker(*args):
        if args[3] == '--status': return ready_status(instance)
        return {'ok': True, 'routes': [{'route': n, 'ok': True, 'private': 'not-for-receipt'} for n in ('primary', 'fallback')]}
    monkeypatch.setattr(lm, '_worker', worker)
    result = lm.check(instance, None, Path('/unused'))
    assert result['provider_ready'] is True
    assert 'not-for-receipt' not in json.dumps(result)


def test_worker_drops_privileges_and_never_inherits_secrets(monkeypatch, instance):
    seen = {}
    monkeypatch.setattr(lm, 'validate_profile', lambda *_: None)
    monkeypatch.setenv('OTHER_CLIENT_SECRET', 'not-for-child')
    def fake_run(args, **kwargs):
        seen.update(kwargs); seen['args'] = args
        return SimpleNamespace(returncode=0, stdout='{"provider_ready":true}', stderr='')
    monkeypatch.setattr(lm.subprocess, 'run', fake_run)
    entry = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir='/home/h99887766', pw_name='h99887766')
    lm._worker(instance, entry, Path(entry.pw_dir)/'.hermes', '--status', 45)
    assert seen['user'] == seen['group'] == 1001
    assert seen['extra_groups'] == ()
    assert 'OTHER_CLIENT_SECRET' not in seen['env']
    assert seen['env']['HERMES_PROFILE'] == instance.owner_linux
    assert seen['args'][-1] == '--status'
    assert seen['timeout'] == 45
    assert not seen.get('shell')


@pytest.mark.parametrize('output', ['', 'oops', '[]', 'x' * 65537])
def test_worker_rejects_bad_output_without_leaking(monkeypatch, instance, output):
    monkeypatch.setattr(lm, 'validate_profile', lambda *_: None)
    monkeypatch.setattr(lm.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=output, stderr='secret-test'))
    entry = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir='/home/h99887766', pw_name='h99887766')
    with pytest.raises(lm.MemoryOnboardingError) as err:
        lm._worker(instance, entry, Path(entry.pw_dir)/'.hermes', '--status', 45)
    assert str(err.value) == 'living_memory_probe_failed'


@pytest.mark.parametrize('props', ['', 'ActiveState=inactive\nUnitFileState=enabled\nNextElapseUSecRealtime=tomorrow',
    'ActiveState=active\nUnitFileState=disabled\nNextElapseUSecRealtime=tomorrow',
    'ActiveState=active\nUnitFileState=enabled\nNextElapseUSecRealtime='])
def test_timer_must_be_enabled_active_and_scheduled(monkeypatch, instance, props):
    monkeypatch.setattr(lm.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=props))
    with pytest.raises(lm.MemoryOnboardingError, match='timer_not_ready'):
        lm.activate(instance, lambda *a, **k: None)


def test_paused_instance_cannot_activate(instance):
    with pytest.raises(lm.MemoryOnboardingError, match='not_running'):
        lm.activate(replace(instance, desired_state='paused'), lambda *a, **k: pytest.fail('no timer writes'))


def test_cleanup_only_touches_target(instance):
    calls = []
    lm.abort(instance, lambda args, **kwargs: calls.append(args))
    assert [c[-1] for c in calls] == ['vektor-living-memory@h99887766.timer', 'vektor-living-memory@h99887766.service']


@pytest.fixture
def profile(tmp_path, monkeypatch, instance):
    monkeypatch.setattr(lm, 'HOME_ROOT', tmp_path)
    home = tmp_path / instance.owner_linux / '.hermes'
    for p in (home, home/'memories', home/'backups'):
        p.mkdir(parents=True, exist_ok=True); p.chmod(0o700)
    for name in ('config.yaml', '.env'):
        p = home/name; p.write_text(''); p.chmod(0o600)
    uid = 1001
    original_lstat = Path.lstat
    def tenant_lstat(path, *args, **kwargs):
        value = original_lstat(path, *args, **kwargs)
        if path.is_relative_to(tmp_path):
            data = list(value); data[4] = uid; data[5] = uid
            return os.stat_result(data)
        return value
    monkeypatch.setattr(Path, 'lstat', tenant_lstat)
    entry = SimpleNamespace(pw_uid=uid, pw_gid=uid, pw_dir=str(home.parent), pw_name=instance.owner_linux)
    return home, entry


@pytest.mark.parametrize('name', ['memories', 'living_memory', 'config.yaml', '.env'])
def test_symlink_tenant_paths_rejected(profile, instance, name):
    home, entry = profile
    target = home / name
    if target.is_dir(): target.rmdir()
    elif target.exists(): target.unlink()
    target.symlink_to(home.parent)
    with pytest.raises(lm.MemoryOnboardingError, match='(directory|file)_unsafe'):
        lm.validate_profile(instance, entry, home)


def test_new_template_enables_daily_not_per_turn_learning():
    config = yaml.safe_load((ROOT/'templates/personal/config.yaml').read_text())
    assert config['memory']['nudge_interval'] == 0
    assert config['memory']['write_approval'] is True
    assert config['memory']['memory_enabled'] is True
    assert config['memory']['user_profile_enabled'] is True
