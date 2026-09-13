from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hermes_instance

SPEC = importlib.util.spec_from_file_location("forge_provisioner", ROOT / "provisioner.py")
provisioner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(provisioner)


def instance():
    return hermes_instance.HermesInstance(
        instance_id="hermes-503899482",
        owner_linux="h503899482",
        owner_telegram_id=503899482,
        bot_id=9000000001,
        bot_username="salavatai_bot",
        bot_name="Salavat AI",
        release_id="hermes-0.21.0-test",
    ).validate()


def test_cli_has_no_arbitrary_command_surface():
    parser = provisioner.build_parser()
    command = next(action for action in parser._actions if action.dest == "command")
    assert command.choices == ("provision",)
    source = (ROOT / "provisioner.py").read_text()
    assert "shell=True" not in source
    assert "os.system(" not in source


def test_provision_runs_bounded_steps_in_order(monkeypatch, tmp_path):
    calls = []
    fake_release = tmp_path / "release"
    fake_release.mkdir()
    (fake_release / "venv").mkdir()
    entry = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir=str(tmp_path / "home"), pw_name="h503899482")
    hermes = Path(entry.pw_dir) / ".hermes"

    monkeypatch.setattr(provisioner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(provisioner, "_safe_token", lambda *_: "test-token-not-secret")
    monkeypatch.setattr(provisioner, "_platform_values", lambda: {"LLM_API_KEY": "x", "FALLBACK_LLM_API_KEY": "y", "OPENROUTER_API_KEY": "z"})
    monkeypatch.setattr(provisioner, "_verified_release", lambda *_: (fake_release, {"version": "0.21.0"}))
    monkeypatch.setattr(provisioner, "_instance_path", lambda *_: tmp_path / "instance.json")
    monkeypatch.setattr(provisioner, "_receipt_path", lambda *_: tmp_path / "receipt.json")
    monkeypatch.setattr(provisioner, "_record", lambda _i, state, **_k: calls.append("record:" + state))
    monkeypatch.setattr(provisioner, "_ensure_account", lambda *_: entry)
    monkeypatch.setattr(provisioner, "_render_profile", lambda *_: calls.append("profile") or hermes)
    monkeypatch.setattr(provisioner, "_ensure_profile_env", lambda *_: calls.append("env"))
    monkeypatch.setattr(provisioner, "_bind_runtime", lambda *_: calls.append("runtime"))
    monkeypatch.setattr(provisioner, "_install_service", lambda *_: calls.append("service"))
    monkeypatch.setattr(provisioner, "_provision_database", lambda *_: calls.append("database"))
    monkeypatch.setattr(provisioner, "_install_passive_secretary", lambda *_: calls.append("secretary"))
    monkeypatch.setattr(provisioner, "_install_maton", lambda *_: calls.append("maton"))
    monkeypatch.setattr(provisioner, "_install_grsai", lambda *_: calls.append("grsai"))
    monkeypatch.setattr(provisioner, "_prepare_living_memory", lambda *_: calls.append("memory-prepare"))
    monkeypatch.setattr(provisioner.living_memory, "check", lambda *_: calls.append("memory-check") or {"version": "0.1.0"})
    monkeypatch.setattr(provisioner.living_memory, "activate", lambda *_: calls.append("memory-enable") or {"timer_enabled": True})
    monkeypatch.setattr(provisioner, "_start_service", lambda *_: calls.append("start"))
    monkeypatch.setattr(provisioner, "_wait_healthy", lambda *_args, **_kwargs: calls.append("health") or {"service": True})

    result = provisioner.provision(instance(), tmp_path / "token.env")
    assert result["state"] == "active"
    assert result["health"]["living_memory"]["timer_enabled"] is True
    assert calls == [
        "record:provisioning", "profile", "env", "runtime", "service",
        "database", "secretary", "maton", "grsai", "memory-prepare", "memory-check", "start", "health", "memory-enable",
        "record:active",
    ]


def test_failure_is_recorded(monkeypatch, tmp_path):
    states = []
    fake_release = tmp_path / "release"
    fake_release.mkdir()
    monkeypatch.setattr(provisioner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(provisioner, "_safe_token", lambda *_: "test-token-not-secret")
    monkeypatch.setattr(provisioner, "_platform_values", lambda: {"LLM_API_KEY": "x", "FALLBACK_LLM_API_KEY": "y", "OPENROUTER_API_KEY": "z"})
    monkeypatch.setattr(provisioner, "_verified_release", lambda *_: (fake_release, {"version": "0.21.0"}))
    monkeypatch.setattr(provisioner, "_instance_path", lambda *_: tmp_path / "instance.json")
    monkeypatch.setattr(provisioner, "_record", lambda _i, state, **kw: states.append((state, kw)))
    monkeypatch.setattr(provisioner, "_ensure_account", lambda *_: (_ for _ in ()).throw(provisioner.ProvisionError("boom")))

    try:
        provisioner.provision(instance(), tmp_path / "token.env")
    except provisioner.ProvisionError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("provision failure must propagate")
    assert states[0][0] == "provisioning"
    assert states[-1][0] == "failed"
    assert states[-1][1]["error_code"] == "boom"


import pytest


@pytest.mark.parametrize('failing_step', ['check', 'start', 'health', 'activate'])
def test_memory_or_startup_failure_never_publishes_ready(monkeypatch, tmp_path, failing_step):
    states, steps = [], []
    def step(name, value=None):
        def call(*args, **kwargs):
            steps.append(name)
            if failing_step == name: raise provisioner.ProvisionError('test_failure')
            return value
        return call
    monkeypatch.setattr(provisioner.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(provisioner, '_safe_token', lambda *_: 'fixture')
    monkeypatch.setattr(provisioner, '_platform_values', lambda: {'LLM_API_KEY':'a','OPENROUTER_API_KEY':'b'})
    monkeypatch.setattr(provisioner, '_verified_release', lambda *_: (tmp_path, {'version':'0.21.0'}))
    monkeypatch.setattr(provisioner, '_instance_path', lambda *_: tmp_path/'instance.json')
    monkeypatch.setattr(provisioner, '_record', lambda _i, state, **kw: states.append((state, kw)))
    monkeypatch.setattr(provisioner, '_ensure_account', lambda *_: SimpleNamespace())
    for name in ('_render_profile', '_ensure_profile_env', '_bind_runtime', '_install_service',
        '_provision_database', '_install_passive_secretary', '_install_maton', '_install_grsai', '_prepare_living_memory'):
        monkeypatch.setattr(provisioner, name, lambda *a: tmp_path)
    monkeypatch.setattr(provisioner, '_start_service', step('start'))
    monkeypatch.setattr(provisioner, '_wait_healthy', step('health', {}))
    monkeypatch.setattr(provisioner.living_memory, 'check', step('check', {}))
    monkeypatch.setattr(provisioner.living_memory, 'activate', step('activate', {}))
    monkeypatch.setattr(provisioner.living_memory, 'abort', step('abort'))
    with pytest.raises(provisioner.ProvisionError, match='test_failure'):
        provisioner.provision(instance())
    assert [state for state, _ in states] == ['provisioning', 'failed']
    assert steps[-1] == 'abort'
    assert states[-1][1]['living_memory_cleanup_failed'] is False
    if failing_step in ('check', 'start', 'health'):
        assert 'activate' not in steps


def test_missing_memory_reserve_does_not_create_linux_account(monkeypatch, tmp_path):
    states = []
    monkeypatch.setattr(provisioner.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(provisioner, '_safe_token', lambda *_: 'fixture')
    monkeypatch.setattr(provisioner, '_platform_values', lambda: {'LLM_API_KEY':'a'})
    monkeypatch.setattr(provisioner, '_verified_release', lambda *_: (tmp_path, {'version':'0.21.0'}))
    monkeypatch.setattr(provisioner, '_instance_path', lambda *_: tmp_path/'instance.json')
    monkeypatch.setattr(provisioner, '_record', lambda _i, state, **kw: states.append((state, kw)))
    monkeypatch.setattr(provisioner, '_ensure_account', lambda *_: pytest.fail('account created too early'))
    with pytest.raises(provisioner.living_memory.MemoryOnboardingError):
        provisioner.provision(instance())
    assert states[-1][0] == 'failed'
    assert states[-1][1]['error_code'] == 'living_memory_platform_credentials_missing'


def test_installer_vendors_memory_and_helper():
    source = (ROOT/'install.py').read_text()
    assert "'living-memory'" in source
    assert "'living_memory_onboarding.py'" in source


@pytest.mark.parametrize('gateway_pid,state,expected', [(123,'running',True),
    (122,'running',False), (123,'starting',False), (0,'running',False)])
def test_ready_requires_current_service_pid_and_running_gateway(monkeypatch, tmp_path, gateway_pid, state, expected):
    import json
    i = instance(); home = tmp_path/i.owner_linux/'.hermes'; home.mkdir(parents=True)
    (home/'gateway_state.json').write_text(json.dumps({'pid':gateway_pid,
        'gateway_state':state, 'code_version':'0.21.0',
        'platforms':{'telegram':{'state':'connected'}}}))
    monkeypatch.setattr(provisioner,'PROFILE_HOME_ROOT',tmp_path)
    clock=iter([0,0,100])
    monkeypatch.setattr(provisioner.time,'monotonic',lambda:next(clock))
    monkeypatch.setattr(provisioner.time,'sleep',lambda *_:None)
    monkeypatch.setattr(provisioner.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout='123\n'))
    identities=[]
    monkeypatch.setattr(provisioner,'_telegram_identity',lambda *_:identities.append(True) or i.bot_username)
    if expected:
        assert provisioner._wait_healthy(i,'fixture','0.21.0')['gateway_pid'] == 123
        assert identities == [True]
    else:
        with pytest.raises(provisioner.ProvisionError,match='profile_health_timeout'):
            provisioner._wait_healthy(i,'fixture','0.21.0')
        assert not identities
