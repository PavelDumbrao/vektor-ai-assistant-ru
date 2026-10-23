"""Safety net for every test here: no test may touch a real client profile under /home.

Every path control.py takes into a client home goes through `_profile_paths()` -> `pwd.getpwnam()`.
Test files load control.py as private module instances (not in sys.modules), so the guard patches the one
object they all share: the `pwd` module. A test that needs a profile provides its own fake entry under tmp_path
by monkeypatching `control._profile_paths` (see `fake_profile` in test_hardening.py).
(23.10.2026: an invite-accept test run as root rewrote /home/pavel/.hermes/workspace-members.json.)
"""
from __future__ import annotations

import pwd

import pytest


def _refuse_pwd(name):
    raise KeyError(f"tests must not resolve real accounts (pwd.getpwnam({name!r})); patch control._profile_paths to a tmp home")


@pytest.fixture(autouse=True)
def _no_live_profiles(monkeypatch):
    monkeypatch.setattr(pwd, "getpwnam", _refuse_pwd)
