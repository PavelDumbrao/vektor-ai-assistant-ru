"""Safety net for every test module here: no test may touch a real client profile under /home.

Each test file loads control.py as its own module instance, so the guard patches every loaded instance.
A test that needs a profile must provide its own fake entry under tmp_path via monkeypatch of `_profile_paths`.
(23.10.2026: an invite-accept test run as root rewrote /home/pavel/.hermes/workspace-members.json.)
"""
from __future__ import annotations

import sys

import pytest


def _control_modules():
    return [
        module for module in list(sys.modules.values())
        if (getattr(module, "__file__", "") or "").endswith("hermes-forge-api/control.py")
    ]


def _refuse_profile(profile):
    raise RuntimeError(f"test touched live profile lookup for {profile!r}: patch control._profile_paths to a tmp home")


def _refuse_pwd(name):
    raise KeyError(f"test tried pwd.getpwnam({name!r}); patch control._profile_paths instead")


@pytest.fixture(autouse=True)
def _no_live_profiles(monkeypatch, tmp_path):
    for module in _control_modules():
        monkeypatch.setattr(module, "HOME_ROOT", tmp_path / "home", raising=False)
        monkeypatch.setattr(module, "_profile_paths", _refuse_profile, raising=False)
        monkeypatch.setattr(module.pwd, "getpwnam", _refuse_pwd, raising=False)
