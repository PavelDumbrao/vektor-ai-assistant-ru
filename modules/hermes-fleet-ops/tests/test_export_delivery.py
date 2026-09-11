from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


exporter = load("forge_metrics_exporter_test", "export_shared_metrics.py")
enroller = load("forge_metrics_enroller_test", "enroll_shared_metrics_exporters.py")


def test_exporter_refuses_root(monkeypatch):
    monkeypatch.setattr(exporter.os, "geteuid", lambda: 0)
    with pytest.raises(exporter.ExportError, match="root_refused"):
        exporter.tenant_paths()


def test_exporter_skips_missing_native_database_without_creating_state(monkeypatch, tmp_path):
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    config = hermes / "config.yaml"
    database = hermes / "telemetry/shared_metrics/metrics.sqlite3"
    monkeypatch.setattr(exporter, "tenant_paths", lambda: ("pavel", 1001, hermes, config, database))
    monkeypatch.setattr(exporter, "telemetry_enabled", lambda *_: True)
    monkeypatch.setattr(exporter, "database_ready", lambda *_: False)
    result = exporter.export_once()
    assert result == {"status": "no_state", "created": False, "outbox_files": 0}
    assert not database.exists()


def test_exporter_calls_native_store_only_after_safety_gates(monkeypatch, tmp_path):
    hermes = tmp_path / ".hermes"
    outbox = hermes / "telemetry/shared_metrics/outbox"
    outbox.mkdir(parents=True)
    (outbox / "package.json").write_text("{}", encoding="utf-8")
    config = hermes / "config.yaml"
    database = hermes / "telemetry/shared_metrics/metrics.sqlite3"
    database.write_text("db", encoding="utf-8")
    monkeypatch.setattr(exporter, "tenant_paths", lambda: ("pavel", 1001, hermes, config, database))
    monkeypatch.setattr(exporter, "telemetry_enabled", lambda *_: True)
    monkeypatch.setattr(exporter, "database_ready", lambda *_: True)

    shared = ModuleType("hermes_cli.observability.shared_metrics")
    calls = []

    class Store:
        def create_and_export_package_if_due(self):
            calls.append("export")
            return outbox / "package.json"

    shared.SharedMetricsStore = Store
    packages = ModuleType("hermes_cli")
    observability = ModuleType("hermes_cli.observability")
    monkeypatch.setitem(sys.modules, "hermes_cli", packages)
    monkeypatch.setitem(sys.modules, "hermes_cli.observability", observability)
    monkeypatch.setitem(sys.modules, "hermes_cli.observability.shared_metrics", shared)

    result = exporter.export_once()
    assert calls == ["export"]
    assert result == {"status": "ok", "created": True, "outbox_files": 1}


def test_telemetry_enabled_requires_explicit_true(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"telemetry": {"shared_metrics": {"enabled": False}}}), encoding="utf-8")
    config.chmod(0o600)
    assert exporter.telemetry_enabled(config, os.getuid()) is False


def test_enroller_validates_registry_and_exact_timer_names(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    for owner in ("pavel", "h503899482"):
        (profiles / f"{owner}.json").write_text(json.dumps({"owner": owner}), encoding="utf-8")
    monkeypatch.setattr(enroller, "PROFILE_ROOT", profiles)
    monkeypatch.setattr(enroller.pwd, "getpwnam", lambda owner: SimpleNamespace(
        pw_uid=1001, pw_dir=f"/home/{owner}", pw_name=owner,
    ))
    assert enroller.profile_owners() == ["h503899482", "pavel"]
    assert enroller.timer_unit("pavel") == "proai-hermes-shared-metrics-export@pavel.timer"


def test_enroller_starts_only_missing_fixed_timer_units(monkeypatch):
    monkeypatch.setattr(enroller.os, "geteuid", lambda: 0)
    monkeypatch.setattr(enroller, "profile_owners", lambda: ["pavel", "bebov"])
    monkeypatch.setattr(enroller, "is_enabled", lambda unit: unit.endswith("@pavel.timer"))
    calls = []
    def run(args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(enroller.subprocess, "run", run)
    result = enroller.enroll()
    assert result == {"profiles": 2, "enrolled": 1, "already": 1, "failed": 0}
    assert calls == [[
        "/usr/bin/systemctl", "enable", "--now",
        "proai-hermes-shared-metrics-export@bebov.timer",
    ]]


def test_export_systemd_contract_has_no_network_and_tenant_write_scope():
    unit = (ROOT / "proai-hermes-shared-metrics-export@.service").read_text(encoding="utf-8")
    assert "User=%i" in unit
    assert "Group=%i" in unit
    assert "PrivateNetwork=true" in unit
    assert "RestrictAddressFamilies=AF_UNIX" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=read-only" in unit
    assert "ReadWritePaths=-/home/%i/.hermes/telemetry/shared_metrics" in unit
    assert "EnvironmentFile=" not in unit
    assert "http://" not in unit and "https://" not in unit


def test_enrollment_service_and_timer_are_local_only():
    service = (ROOT / "proai-hermes-shared-metrics-enroll.service").read_text(encoding="utf-8")
    timer = (ROOT / "proai-hermes-shared-metrics-enroll.timer").read_text(encoding="utf-8")
    tenant_timer = (ROOT / "proai-hermes-shared-metrics-export@.timer").read_text(encoding="utf-8")
    assert "PrivateNetwork=true" in service
    assert "ProtectHome=true" in service
    assert "ProtectSystem=full" in service
    assert "OnUnitActiveSec=15min" in timer
    assert "OnUnitActiveSec=1h" in tenant_timer
    assert "RandomizedDelaySec=15min" in tenant_timer


def test_fleet_installer_bundles_exporter_and_enrollment_timer():
    source = (ROOT / "install.py").read_text(encoding="utf-8")
    assert 'EXPORTER_TARGET = Path("/opt/proai-hermes-shared-metrics-exporter")' in source
    assert '"export_shared_metrics.py"' in source
    assert '"enroll_shared_metrics_exporters.py"' in source
    assert '"proai-hermes-shared-metrics-export@.service"' in source
    assert '"proai-hermes-shared-metrics-export@.timer"' in source
    assert '"proai-hermes-shared-metrics-enroll.timer"' in source
    assert 'atomic_copy(SOURCE / "export_shared_metrics.py", EXPORTER_TARGET / "export_shared_metrics.py", 0o644)' in source
