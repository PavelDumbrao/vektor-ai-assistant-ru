from __future__ import annotations

import json
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
import hermes_instance as mod


def make_instance():
    return mod.HermesInstance(
        instance_id="hermes-503899482",
        owner_linux="h503899482",
        owner_telegram_id=503899482,
        bot_id=9000000001,
        bot_username="salavatai_bot",
        bot_name="Salavat AI",
        release_id="hermes-0.21.0-test",
    ).validate()


def test_owner_is_stable_and_not_derived_from_bot_name():
    assert mod.owner_for_telegram_id(503899482) == "h503899482"
    assert make_instance().owner_linux == "h503899482"


def test_roundtrip_and_atomic_write(tmp_path):
    instance = make_instance()
    path = tmp_path / "instances" / "hermes-503899482.json"
    instance.write_atomic(path)
    assert path.stat().st_mode & 0o077 == 0
    loaded = mod.HermesInstance.from_dict(json.loads(path.read_text()))
    assert loaded == instance


def test_rejects_noncanonical_bot_username():
    instance = make_instance()
    bad = instance.__class__(**{**instance.__dict__, "bot_username": "SalavatAI_bot"})
    try:
        bad.validate()
    except ValueError as exc:
        assert str(exc) == "bot_username_not_canonical"
    else:
        raise AssertionError("uppercase username must fail desired-state validation")


def test_rejects_owner_mismatch():
    instance = make_instance()
    bad = instance.__class__(**{**instance.__dict__, "owner_linux": "salavat"})
    try:
        bad.validate()
    except ValueError as exc:
        assert str(exc) == "owner_linux_mismatch"
    else:
        raise AssertionError("owner mismatch must fail")
