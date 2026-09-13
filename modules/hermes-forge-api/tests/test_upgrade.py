from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("forge_api_upgrade", MODULE_ROOT / "upgrade.py")
upgrade = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(upgrade)


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "static").mkdir()
    (source / "api.py").write_text("print('api-v2')\n", encoding="utf-8")
    (source / "static/index.html").write_text("<h1>Kitchen</h1>\n", encoding="utf-8")
    kitchen = tmp_path / "kitchen.py"
    kitchen.write_text("PLAN_SCHEMA='hermes.kitchen-plan/v1'\n", encoding="utf-8")
    target = tmp_path / "live"
    systemd = tmp_path / "systemd"
    monkeypatch.setattr(upgrade, "SOURCE", source)
    monkeypatch.setattr(upgrade, "KITCHEN_SOURCE", kitchen)
    monkeypatch.setattr(upgrade, "TARGET", target)
    monkeypatch.setattr(upgrade, "RELEASES", target / "releases")
    monkeypatch.setattr(upgrade, "CURRENT", target / "current")
    monkeypatch.setattr(upgrade, "SYSTEMD", systemd)
    monkeypatch.setattr(upgrade, "DROPIN_DIR", systemd / f"{upgrade.SERVICE}.d")
    monkeypatch.setattr(
        upgrade, "DROPIN", systemd / f"{upgrade.SERVICE}.d/30-versioned-release.conf"
    )
    return source, target


def test_prepare_release_is_immutable_and_deterministic(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    digest, release = upgrade.prepare_release()
    again_digest, again_release = upgrade.prepare_release()
    assert digest == again_digest
    assert release == again_release
    assert release.name == digest
    marker = json.loads((release / "release.json").read_text(encoding="utf-8"))
    assert marker["schema"] == "hermes.forge-api-release/v1"
    assert marker["sha256"] == digest
    assert (release / "api.py").is_file()
    assert (release / "kitchen.py").is_file()
    assert (release / "static/index.html").is_file()


def test_plan_is_dry_run_and_does_not_create_release(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    result = upgrade.plan_upgrade()
    assert result["apply"] is False
    assert len(result["release_sha256"]) == 64
    assert not upgrade.RELEASES.exists()
    assert not upgrade.CURRENT.exists()


def test_apply_switches_only_api_service_after_probe(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(upgrade.os, "geteuid", lambda: 0)
    monkeypatch.setattr(upgrade, "_run", lambda *args, **kwargs: calls.append(tuple(args)))
    monkeypatch.setattr(upgrade, "_wait_for_probes", lambda timeout=20.0: True)
    result = upgrade.apply_upgrade()
    assert result["apply"] is True
    assert Path(upgrade.CURRENT.resolve()).name == result["release_sha256"]
    assert upgrade.DROPIN.read_text(encoding="utf-8") == upgrade._dropin_text()
    assert ("/usr/bin/systemctl", "restart", upgrade.SERVICE) in calls
    assert not any("hermes.service" in " ".join(call) for call in calls)


def test_failed_probe_rolls_back_pointer_and_dropin(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    upgrade.TARGET.mkdir(parents=True)
    old_release = upgrade.TARGET / "old-release"
    old_release.mkdir()
    upgrade.CURRENT.symlink_to(old_release)
    upgrade.DROPIN.parent.mkdir(parents=True)
    old_dropin = b"[Service]\nEnvironment=OLD=1\n"
    upgrade.DROPIN.write_bytes(old_dropin)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(upgrade.os, "geteuid", lambda: 0)
    monkeypatch.setattr(upgrade, "_run", lambda *args, **kwargs: calls.append(tuple(args)))
    monkeypatch.setattr(upgrade, "_wait_for_probes", lambda timeout=20.0: False)
    monkeypatch.setattr(upgrade, "_wait_for_basic_probes", lambda timeout=20.0: True)
    with pytest.raises(RuntimeError, match="activation_probe_failed"):
        upgrade.apply_upgrade()
    assert upgrade.CURRENT.is_symlink()
    assert Path(upgrade.CURRENT.resolve()) == old_release.resolve()
    assert upgrade.DROPIN.read_bytes() == old_dropin
    restarts = [call for call in calls if call[-2:] == ("restart", upgrade.SERVICE)]
    assert len(restarts) == 2


def test_probe_requires_health_catalog_and_kitchen(monkeypatch):
    replies = {
        "/healthz": (200, {"ok": True, "result": {"ok": True}}),
        "/v1/catalog": (200, {"ok": True, "result": {"schema": "hermes.catalog.public/v1"}}),
        "/v1/kitchen/preview": (
            200, {"ok": True, "result": {"schema": "hermes.kitchen-preview/v1"}},
        ),
    }

    def fake_request(path, *, body=None):
        if path == "/v1/kitchen/preview":
            assert body == {"agent_id": "personal-hermes", "optional_capabilities": []}
        return replies[path]

    monkeypatch.setattr(upgrade, "_request", fake_request)
    assert upgrade.probes_ok() is True
    replies["/v1/kitchen/preview"] = (401, {"ok": False, "error": "session_required"})
    assert upgrade.probes_ok() is False


def test_upgrade_source_has_fixed_api_service_boundary():
    source = (MODULE_ROOT / "upgrade.py").read_text(encoding="utf-8")
    assert 'SERVICE = "proai-hermes-forge-api.service"' in source
    assert "pavel-hermes.service" not in source
    assert "bebov-hermes.service" not in source
    assert "shell=True" not in source


def test_bootstrap_installer_refuses_active_api(monkeypatch, tmp_path):
    install_path = MODULE_ROOT / "install.py"
    spec = importlib.util.spec_from_file_location("forge_api_bootstrap_install", install_path)
    installer = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(installer)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.pwd, "getpwnam", lambda name: object())
    monkeypatch.setattr(installer, "service_active", lambda name: True)
    monkeypatch.setattr(installer, "TARGET", tmp_path / "must-not-exist")
    with pytest.raises(RuntimeError, match="active_api_requires_versioned_upgrade"):
        installer.install()
    assert not installer.TARGET.exists()


def test_dropin_write_failure_restores_previous_pointer(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    upgrade.TARGET.mkdir(parents=True)
    old_release = upgrade.TARGET / "old-release"
    old_release.mkdir()
    upgrade.CURRENT.symlink_to(old_release)
    monkeypatch.setattr(upgrade.os, "geteuid", lambda: 0)
    monkeypatch.setattr(upgrade, "_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        upgrade, "_install_dropin",
        lambda: (_ for _ in ()).throw(RuntimeError("dropin_write_failed")),
    )
    monkeypatch.setattr(upgrade, "_wait_for_basic_probes", lambda timeout=20.0: True)
    with pytest.raises(RuntimeError, match="dropin_write_failed"):
        upgrade.apply_upgrade()
    assert Path(upgrade.CURRENT.resolve()) == old_release.resolve()
    assert not upgrade.DROPIN.exists()


def test_concurrent_apply_fails_closed_on_lock(monkeypatch, tmp_path):
    import fcntl

    _configure(monkeypatch, tmp_path)
    upgrade.TARGET.mkdir(parents=True)
    lock = upgrade.TARGET / ".upgrade.lock"
    monkeypatch.setattr(upgrade.os, "geteuid", lambda: 0)
    with lock.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="upgrade_busy"):
            upgrade.apply_upgrade()
    assert not upgrade.CURRENT.exists()
    assert not upgrade.RELEASES.exists()


def test_bootstrap_installer_refuses_existing_versioned_layout(monkeypatch, tmp_path):
    install_path = MODULE_ROOT / "install.py"
    spec = importlib.util.spec_from_file_location("forge_api_bootstrap_versioned", install_path)
    installer = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(installer)
    target = tmp_path / "forge-api"
    (target / "releases").mkdir(parents=True)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.pwd, "getpwnam", lambda name: object())
    monkeypatch.setattr(installer, "service_active", lambda name: False)
    monkeypatch.setattr(installer, "TARGET", target)
    with pytest.raises(RuntimeError, match="versioned_api_requires_upgrade"):
        installer.install()
