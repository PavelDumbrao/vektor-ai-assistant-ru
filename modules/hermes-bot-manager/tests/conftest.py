"""Keep hermes-bot-manager tests hermetic.

The suite must give the same result on a laptop, a GitHub runner and the production
host, and it must never touch the host's systemd or the live manager state:
- every ``systemctl`` call goes to a fake (tests that need a specific unit state
  still monkeypatch ``subprocess.run`` themselves; their patch wins);
- the manager state root points at a throwaway directory.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

_STATE_ROOT = tempfile.mkdtemp(prefix="hermes-manager-test-")
# Set before the test modules import manager.py / provisioner.py (they read it at import time).
os.environ["PROAI_HERMES_MANAGER_ROOT"] = _STATE_ROOT

_real_run = subprocess.run


def _fake_systemctl(argv: list[str]) -> SimpleNamespace:
    verb = next((a for a in argv[1:] if not a.startswith("-")), "")
    if verb == "show":
        # A provisioner unit that is still running: neither finished nor failed.
        return SimpleNamespace(returncode=0, stdout="ActiveState=activating\nResult=success\n", stderr="")
    if verb == "is-active":
        return SimpleNamespace(returncode=3, stdout="inactive\n", stderr="")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture(autouse=True)
def _hermetic_systemctl(monkeypatch):
    def run(args, *a, **kw):
        argv = [str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)]
        if argv and os.path.basename(argv[0]) == "systemctl":
            return _fake_systemctl(argv)
        return _real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)


def pytest_unconfigure(config):
    shutil.rmtree(_STATE_ROOT, ignore_errors=True)
