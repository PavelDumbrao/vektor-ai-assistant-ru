from __future__ import annotations

import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
import updater
from store import AnalyticsStore


def policy(release="hermes-new"):
    return {
        "schema_version": 1,
        "enabled": True,
        "defaults": {"forge_new": {"track": "modern", "channel": "stable"}},
        "tracks": {"modern": {
            "stable": {"release_id": release, "stages": [10, 50, 100], "healthy_cycles_to_advance": 2},
            "preview": {"release_id": release, "stages": [100], "healthy_cycles_to_advance": 1},
            "canary": {"release_id": release, "stages": [100], "healthy_cycles_to_advance": 1},
        }},
        "profiles": {},
    }


def test_new_forge_profiles_default_to_modern_stable(monkeypatch):
    monkeypatch.setattr(updater, "verified_release", lambda *_: None)
    assert updater.assignment("h503899482", policy()) == {"track": "modern", "channel": "stable"}
    assert updater.assignment("legacy_named", policy()) is None


def test_cohort_is_deterministic_and_never_zero_for_positive_percent():
    owners = [f"h{10000+i}" for i in range(20)]
    first = updater.cohort(owners, 10)
    assert len(first) == 2
    assert first == updater.cohort(list(reversed(owners)), 10)


def test_stable_rollout_advances_after_two_healthy_cycles(monkeypatch, tmp_path):
    store = AnalyticsStore(tmp_path / "analytics" / "db.sqlite3")
    p = policy()
    profiles = [{"owner": "h10001", "release_id": "hermes-new"}]
    monkeypatch.setattr(updater, "verified_release", lambda *_: None)
    monkeypatch.setattr(updater, "profile_healthy", lambda *_: True)

    first = updater.run_channel(store, p, profiles, "modern", "stable")
    state = store.get_rollout("modern", "stable")
    assert first["failed"] == 0
    assert state["rollout_percent"] == 10
    assert state["healthy_cycles"] == 1

    updater.run_channel(store, p, profiles, "modern", "stable")
    state = store.get_rollout("modern", "stable")
    assert state["rollout_percent"] == 50
    assert state["healthy_cycles"] == 0


def test_upgrade_failure_pauses_channel(monkeypatch, tmp_path):
    store = AnalyticsStore(tmp_path / "analytics" / "db.sqlite3")
    p = policy()
    p["profiles"]["h10001"] = {"track": "modern", "channel": "stable"}
    profiles = [{"owner": "h10001", "release_id": "hermes-old"}]
    monkeypatch.setattr(updater, "verified_release", lambda *_: None)
    monkeypatch.setattr(updater, "run_upgrade", lambda *_: ("failed", "upgrade_failed"))
    result = updater.run_channel(store, p, profiles, "modern", "stable")
    assert result["failed"] == 1
    assert result["paused"] == 1
    assert bool(store.get_rollout("modern", "stable")["paused"])
