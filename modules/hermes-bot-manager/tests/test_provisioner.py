from __future__ import annotations

import importlib.util
import json
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
    monkeypatch.setattr(provisioner, "_allocate_broker_lane", lambda *_: calls.append("lane") or 21001)
    monkeypatch.setattr(provisioner, "_render_profile", lambda *_: calls.append("profile") or hermes)
    monkeypatch.setattr(provisioner, "_ensure_profile_env", lambda *_: calls.append("env"))
    monkeypatch.setattr(provisioner, "_ensure_proaisaver_header", lambda *_: calls.append("saver-header"))
    monkeypatch.setattr(provisioner, "_install_profile_hooks", lambda *_: calls.append("hooks"))
    monkeypatch.setattr(provisioner, "_bind_runtime", lambda *_: calls.append("runtime"))
    monkeypatch.setattr(provisioner, "_prepare_lazy_packages", lambda *_: calls.append("lazy"))
    monkeypatch.setattr(provisioner, "_install_service", lambda *_: calls.append("service"))
    monkeypatch.setattr(provisioner, "_provision_database", lambda *_: calls.append("database"))
    monkeypatch.setattr(provisioner, "_install_passive_secretary", lambda *_: calls.append("secretary"))
    monkeypatch.setattr(provisioner, "_install_workspace_members", lambda *_: calls.append("workspace-members"))
    monkeypatch.setattr(provisioner, "_install_maton", lambda *_: calls.append("maton"))
    monkeypatch.setattr(provisioner, "_install_voice_picker", lambda *_: calls.append("voice-picker"))
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
        "record:provisioning", "lane", "profile", "env", "saver-header", "hooks", "runtime", "lazy", "service",
        "database", "secretary", "workspace-members", "maton", "voice-picker", "grsai",
        "memory-prepare", "memory-check", "start", "health", "memory-enable",
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
    monkeypatch.setattr(provisioner, '_allocate_broker_lane', lambda *a: 21001)
    for name in ('_render_profile', '_ensure_profile_env', '_ensure_proaisaver_header', '_install_profile_hooks', '_bind_runtime',
        '_prepare_lazy_packages', '_install_service', '_provision_database', '_install_passive_secretary', '_install_workspace_members',
        '_install_maton', '_install_voice_picker', '_install_grsai', '_prepare_living_memory'):
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


def test_render_profile_precreates_private_shared_metrics_paths(monkeypatch, tmp_path):
    import os
    monkeypatch.setattr(provisioner, "MANAGER_ROOT", ROOT)
    home = tmp_path / "h503899482"
    entry = SimpleNamespace(
        pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(home), pw_name="h503899482")
    hermes = provisioner._render_profile(instance(), entry, 21001)
    for relative in ("telemetry", "telemetry/shared_metrics", "telemetry/shared_metrics/outbox"):
        path = hermes / relative
        assert path.is_dir() and not path.is_symlink()
        assert path.stat().st_uid == os.getuid()
        assert path.stat().st_mode & 0o077 == 0


def test_ensure_profile_env(monkeypatch, tmp_path):
    hermes = tmp_path / ".hermes"
    hermes.mkdir(parents=True)
    entry = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir=str(tmp_path), pw_name="h503899482")
    token = "9000000001:AAABBBCCCDDDEEEFFFGGGHHHJJJ"
    platform_values = {
        "LLM_API_KEY": "sk-real-secret-llm-key-long-value-12345",
        "FALLBACK_LLM_API_KEY": "sk-real-secret-fallback-key-67890",
        "GEMINI_LLM_API_KEY": "sk-real-secret-gemini-key-11111",
        "OPENROUTER_API_KEY": "sk-real-secret-openrouter-key-22222",
        "ELEVENLABS_API_KEY": "sk-real-secret-elevenlabs-key-33333",
        "GRSAI_API_KEY": "sk-real-secret-grsai-key-44444",
        "PROAISAVER_MCP_TOKEN": "sk-real-secret-proaisaver-token-55555",
        "LINGSUAN_OPUS_KEY": "sk-real-secret-lingsuan-opus-66666",
        "UNOROUTER_API_KEY": "sk-real-secret-unorouter-77777",
        "MINIMAX_API_KEY": "sk-real-secret-minimax-88888",
        "TAVILY_API_KEY": "tvly-real-secret-tavily-key-99999",
    }
    monkeypatch.setattr(provisioner, "_atomic_write", lambda p, c, **k: p.write_text(c, encoding="utf-8"))
    broker_port = 21001
    provisioner._ensure_profile_env(instance(), entry, hermes, token, platform_values, broker_port)

    env_path = hermes / ".env"
    env_content = env_path.read_text(encoding="utf-8")
    env_dict = provisioner._read_env(env_path)

    for key, secret in platform_values.items():
        if key == "TAVILY_API_KEY":
            assert env_dict.get(key) == secret
            assert secret in env_content
        else:
            assert secret not in env_content
            assert env_dict.get(key) != secret

    for key, placeholder in provisioner.BROKER_PLACEHOLDERS.items():
        assert env_dict.get(key) == placeholder

    assert env_dict.get("GRSAI_BASE_URL") == f"http://127.0.0.1:{broker_port}/grsai"
    assert env_dict.get("LINGSUAN_IMAGE_BASE_URL") == f"http://127.0.0.1:{broker_port}/lingsuan-image"
    assert env_dict.get("LIVING_MEMORY_FALLBACK_BASE") == f"http://127.0.0.1:{broker_port}/lingsuan-opus/v1"
    assert env_dict.get("HERMES_OPENROUTER_STT_ENDPOINT") == f"http://127.0.0.1:{broker_port}/stt/v1/audio/transcriptions"

    assert "MINIMAX_API_KEY" not in env_dict
    assert env_dict.get("TELEGRAM_BOT_TOKEN") == token


def test_ensure_proaisaver_header(monkeypatch, tmp_path):
    hermes = tmp_path / ".hermes"
    hermes.mkdir(parents=True)
    entry = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir=str(tmp_path), pw_name="h503899482")
    monkeypatch.setattr(provisioner, "_atomic_write", lambda p, c, **k: p.write_text(c, encoding="utf-8"))

    # Config with Authorization: Bearer keybroker-saver remains untouched and does not raise proaisaver_token_conflict
    config_file = hermes / "config.yaml"
    template_with_broker = (
        "mcp_servers:\n"
        "  proaisaver:\n"
        "    enabled: true\n"
        "    url: http://127.0.0.1:21001/saver/mcp/\n"
        "    headers:\n"
        "      Authorization: Bearer keybroker-saver\n"
    )
    config_file.write_text(template_with_broker, encoding="utf-8")
    provisioner._ensure_proaisaver_header(entry, hermes, {"PROAISAVER_MCP_TOKEN": "real-saver-secret-token"})
    assert config_file.read_text(encoding="utf-8") == template_with_broker

    # Config with empty Bearer is populated as before
    template_empty_bearer = (
        "mcp_servers:\n"
        "  proaisaver:\n"
        "    enabled: true\n"
        "    headers:\n"
        "      Authorization: Bearer\n"
    )
    config_file.write_text(template_empty_bearer, encoding="utf-8")
    provisioner._ensure_proaisaver_header(entry, hermes, {"PROAISAVER_MCP_TOKEN": "real-saver-secret-token"})
    assert "Authorization: Bearer real-saver-secret-token" in config_file.read_text(encoding="utf-8")


def test_allocate_broker_lane(monkeypatch, tmp_path):
    lanes_file = tmp_path / "lanes.json"
    monkeypatch.setattr(provisioner, "LANES_PATH", lanes_file)
    entry = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir=str(tmp_path), pw_name="h503899482")
    inst = instance()
    run_calls = []

    # (a) Successful add -> port from file
    lanes_file.write_text(json.dumps({"lanes": [{"client": inst.owner_linux, "port": 21005}]}), encoding="utf-8")
    monkeypatch.setattr(provisioner, "_run", lambda args, **k: run_calls.append(args))
    port = provisioner._allocate_broker_lane(inst, entry)
    assert port == 21005
    assert len(run_calls) == 1
    assert run_calls[0] == [str(provisioner.LANECTL), "add", "--client", inst.owner_linux, *provisioner.LANE_LIMITS]

    # (b) _run raises ProvisionError, lane exists in file -> return its port
    lanes_file.write_text(json.dumps({"lanes": [{"client": inst.owner_linux, "port": 21010}]}), encoding="utf-8")
    def failing_run(*args, **kwargs):
        raise provisioner.ProvisionError("subprocess_failed:lanectl")
    monkeypatch.setattr(provisioner, "_run", failing_run)
    assert provisioner._allocate_broker_lane(inst, entry) == 21010

    # (c) Lane missing in file -> ProvisionError("broker_lane_unavailable")
    lanes_file.write_text(json.dumps({"lanes": [{"client": "other-user", "port": 21015}]}), encoding="utf-8")
    with pytest.raises(provisioner.ProvisionError, match="broker_lane_unavailable"):
        provisioner._allocate_broker_lane(inst, entry)


def test_render_profile_replaces_broker_port(monkeypatch, tmp_path):
    template_dir = tmp_path / "templates" / "personal"
    template_dir.mkdir(parents=True)
    (template_dir / "config.yaml").write_text("url: http://127.0.0.1:@BROKER_PORT@/test\n", encoding="utf-8")
    (template_dir / "SOUL.md").write_text("Hello @OWNER@ on port @BROKER_PORT@\n", encoding="utf-8")
    (template_dir / "USER.md").write_text("User @OWNER@\n", encoding="utf-8")

    monkeypatch.setattr(provisioner, "MANAGER_ROOT", tmp_path)
    monkeypatch.setattr(provisioner, "_atomic_write", lambda p, c, **k: p.write_text(c, encoding="utf-8"))

    home = tmp_path / "home" / "h503899482"
    entry = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir=str(home), pw_name="h503899482")

    hermes = provisioner._render_profile(instance(), entry, 21042)
    rendered_cfg = (hermes / "config.yaml").read_text(encoding="utf-8")
    assert "http://127.0.0.1:21042/test" in rendered_cfg
    assert "@BROKER_PORT@" not in rendered_cfg

    rendered_soul = (hermes / "SOUL.md").read_text(encoding="utf-8")
    assert "21042" in rendered_soul
    assert "@BROKER_PORT@" not in rendered_soul
