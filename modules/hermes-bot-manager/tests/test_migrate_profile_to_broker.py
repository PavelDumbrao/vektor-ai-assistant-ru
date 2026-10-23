from __future__ import annotations

import json
import os
import pwd
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
import migrate_profile_to_broker as mpb


@pytest.fixture
def test_setup(tmp_path: Path, monkeypatch):
    home = tmp_path / "home" / "testuser"
    hermes = home / ".hermes"
    hermes.mkdir(parents=True)
    store = tmp_path / "private-backups"

    # Fake user
    user_entry = SimpleNamespace(
        pw_name="testuser",
        pw_uid=os.getuid(),
        pw_gid=os.getgid(),
        pw_dir=str(home),
    )
    monkeypatch.setattr(pwd, "getpwnam", lambda u: user_entry if u == "testuser" else (_ for _ in ()).throw(KeyError(u)))

    # Release directory
    release_dir = tmp_path / "releases" / "v0.21.0-kb1"
    release_dir.mkdir(parents=True)
    (release_dir / "runtime.json").write_text(
        json.dumps({"state": "ready", "verification": {"env_override_verified": True}}),
        encoding="utf-8",
    )
    agent_dir = release_dir / "hermes-agent"
    agent_dir.mkdir()
    (hermes / "hermes-agent").symlink_to(agent_dir)

    # lanes.json
    lanes_file = tmp_path / "lanes.json"
    lanes_file.write_text(
        json.dumps({"lanes": [{"client": "testuser", "port": 21005, "uid": os.getuid()}]}),
        encoding="utf-8",
    )

    # platform.env
    platform_file = tmp_path / "platform.env"
    platform_file.write_text(
        "LLM_API_KEY=sk-real-platform-secret-123456\n"
        "FALLBACK_LLM_API_KEY=sk-real-fallback-secret-123456\n"
        "TAVILY_API_KEY=tvly-real-platform-secret-123456\n",
        encoding="utf-8",
    )

    # Initial .env
    (hermes / ".env").write_text(
        "export LLM_API_KEY = \"sk-real-platform-secret-123456\"\n"
        "FALLBACK_LLM_API_KEY=sk-real-fallback-secret-123456\n"
        "TAVILY_API_KEY=tvly-real-platform-secret-123456\n"
        "MINIMAX_API_KEY=sk-minimax-old-123456\n"
        "CUSTOM_VAR=keep_me\n",
        encoding="utf-8",
    )

    # Initial config.yaml
    cfg_data = {
        "model": {
            "default": "gpt-6.1-sol",
            "base_url": "https://lingsuan.top/v1",
            "api_key": "sk-real-platform-secret-123456",
        },
        "providers": {
            "primary": {"api": "https://lingsuan.top/v1", "key_env": "LLM_API_KEY"},
        },
        "tts": {"elevenlabs": {"voice": "default"}},
    }
    (hermes / "config.yaml").write_text(yaml.safe_dump(cfg_data), encoding="utf-8")

    return SimpleNamespace(
        home=home,
        hermes=hermes,
        store=store,
        lanes_file=lanes_file,
        platform_file=platform_file,
        release_dir=release_dir,
    )


def test_migrate_profile_success(test_setup):
    """Тест на фикстуре: заглушки проставлены, адреса -> порт, Tavily остался,
    бэкап лежит в хранилище, в домашнем каталоге бэкапа нет."""
    argv = [
        "--owner", "testuser",
        "--apply",
        "--lanes-file", str(test_setup.lanes_file),
        "--platform-env", str(test_setup.platform_file),
        "--store", str(test_setup.store),
    ]

    rc = mpb.main(argv)
    assert rc == 0

    hermes = test_setup.hermes

    # .env assertions
    env_text = (hermes / ".env").read_text(encoding="utf-8")
    assert "LLM_API_KEY=keybroker-lingsuan" in env_text
    assert "FALLBACK_LLM_API_KEY=keybroker-lingsuan-fb" in env_text
    assert "TAVILY_API_KEY=tvly-real-platform-secret-123456" in env_text
    assert "MINIMAX_API_KEY" not in env_text
    assert "CUSTOM_VAR=keep_me" in env_text
    assert "GRSAI_BASE_URL=http://127.0.0.1:21005/grsai" in env_text

    # config.yaml assertions
    cfg = yaml.safe_load((hermes / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["model"]["base_url"] == "http://127.0.0.1:21005/lingsuan/v1"
    assert cfg["model"]["api_key"] == "keybroker-lingsuan"
    assert cfg["providers"]["primary"]["api"] == "http://127.0.0.1:21005/lingsuan/v1"
    assert cfg["tts"]["elevenlabs"]["base_url"] == "http://127.0.0.1:21005/elevenlabs"

    # Backup assertions: backup is in store, NOT in home
    store_backups = list((test_setup.store / "testuser").glob("*"))
    assert len(store_backups) >= 2
    for b in store_backups:
        assert b.is_file() and not b.is_symlink()

    home_backups = list(hermes.glob("*.bak*"))
    assert len(home_backups) == 0


def test_migrate_symlink_refused_before_write(test_setup):
    """симлинк на месте .env -> отказ до записи"""
    hermes = test_setup.hermes
    env_file = hermes / ".env"
    real_env = test_setup.home / "real_env_outside"
    real_env.write_text("SOME_KEY=val\n", encoding="utf-8")
    env_file.unlink()
    env_file.symlink_to(real_env)

    argv = [
        "--owner", "testuser",
        "--apply",
        "--lanes-file", str(test_setup.lanes_file),
        "--platform-env", str(test_setup.platform_file),
        "--store", str(test_setup.store),
    ]

    rc = mpb.main(argv)
    assert rc != 0
    # Nothing was written into store
    assert not (test_setup.store / "testuser").exists()
    # Real env file untouched
    assert real_env.read_text(encoding="utf-8") == "SOME_KEY=val\n"


def test_migrate_unpatched_release_refused_without_flag(test_setup):
    # Set verification to false
    (test_setup.release_dir / "runtime.json").write_text(
        json.dumps({"state": "ready", "verification": {"env_override_verified": False}}),
        encoding="utf-8",
    )

    argv = [
        "--owner", "testuser",
        "--apply",
        "--lanes-file", str(test_setup.lanes_file),
        "--platform-env", str(test_setup.platform_file),
        "--store", str(test_setup.store),
    ]
    rc = mpb.main(argv)
    assert rc == 1

    # Now pass --allow-unpatched-release -> should succeed
    argv.append("--allow-unpatched-release")
    rc = mpb.main(argv)
    assert rc == 0


def test_migrate_rollback_on_env_write_failure(test_setup, monkeypatch):
    orig_cfg = (test_setup.hermes / "config.yaml").read_text(encoding="utf-8")

    # Make writing .env fail
    orig_write = mpb.write_preserving
    def failing_write(path, text, uid, gid, mode=0o600):
        if str(path).endswith(".env"):
            raise OSError("simulated disk full")
        return orig_write(path, text, uid, gid, mode)
    monkeypatch.setattr(mpb, "write_preserving", failing_write)

    argv = [
        "--owner", "testuser",
        "--apply",
        "--lanes-file", str(test_setup.lanes_file),
        "--platform-env", str(test_setup.platform_file),
        "--store", str(test_setup.store),
    ]
    rc = mpb.main(argv)
    assert rc == 1
    # config.yaml must be rolled back to original content
    assert (test_setup.hermes / "config.yaml").read_text(encoding="utf-8") == orig_cfg


def test_migrate_leak_returns_code_2_and_no_restart(test_setup, monkeypatch):
    # Add another platform secret that won't be replaced because it's not in PLACEHOLDERS
    platform_file = test_setup.platform_file
    platform_file.write_text(
        platform_file.read_text(encoding="utf-8") + "UNMANAGED_SECRET=unmanaged-platform-secret-123456\n",
        encoding="utf-8",
    )
    (test_setup.hermes / ".env").write_text(
        (test_setup.hermes / ".env").read_text(encoding="utf-8") + "UNMANAGED_SECRET=unmanaged-platform-secret-123456\n",
        encoding="utf-8",
    )

    restarted = []
    monkeypatch.setattr(mpb.subprocess, "run", lambda *a, **k: restarted.append(True))

    argv = [
        "--owner", "testuser",
        "--apply",
        "--restart",
        "--lanes-file", str(test_setup.lanes_file),
        "--platform-env", str(test_setup.platform_file),
        "--store", str(test_setup.store),
    ]
    rc = mpb.main(argv)
    assert rc == 2
    # Must NOT restart service when leak detected
    assert len(restarted) == 0
