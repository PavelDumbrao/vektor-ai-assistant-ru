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
    monkeypatch.setattr(provisioner, "_platform_values", lambda: {"LLM_API_KEY": "x", "FALLBACK_LLM_API_KEY": "y"})
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
    monkeypatch.setattr(provisioner, "_start_service", lambda *_: calls.append("start"))
    monkeypatch.setattr(provisioner, "_wait_healthy", lambda *_args, **_kwargs: calls.append("health") or {"service": True})

    result = provisioner.provision(instance(), tmp_path / "token.env")
    assert result["state"] == "active"
    assert calls == [
        "record:provisioning", "profile", "env", "runtime", "service",
        "database", "secretary", "maton", "grsai", "start", "health",
        "record:active",
    ]


def test_failure_is_recorded(monkeypatch, tmp_path):
    states = []
    fake_release = tmp_path / "release"
    fake_release.mkdir()
    monkeypatch.setattr(provisioner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(provisioner, "_safe_token", lambda *_: "test-token-not-secret")
    monkeypatch.setattr(provisioner, "_platform_values", lambda: {"LLM_API_KEY": "x", "FALLBACK_LLM_API_KEY": "y"})
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
