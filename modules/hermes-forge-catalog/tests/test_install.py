from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = MODULE_ROOT.parents[1]
CATALOG_SPEC = importlib.util.spec_from_file_location("catalog", MODULE_ROOT / "catalog.py")
catalog = importlib.util.module_from_spec(CATALOG_SPEC)
assert CATALOG_SPEC.loader is not None
sys.modules["catalog"] = catalog
CATALOG_SPEC.loader.exec_module(catalog)
INSTALL_SPEC = importlib.util.spec_from_file_location("forge_catalog_install", MODULE_ROOT / "install.py")
install = importlib.util.module_from_spec(INSTALL_SPEC)
assert INSTALL_SPEC.loader is not None
INSTALL_SPEC.loader.exec_module(install)


def test_install_creates_immutable_release_and_atomic_current(tmp_path):
    target = tmp_path / "catalog"
    result = install.install(catalog.DEFAULT_ROOT, target)
    digest = catalog.catalog_digest()
    assert result["sha256"] == digest
    assert (target / "current").is_symlink()
    assert (target / "current").resolve() == target / "releases" / digest
    assert (target / "current/catalog.sha256").read_text().strip() == digest
    public = json.loads((target / "current/catalog.json").read_text())
    assert public == catalog.public_catalog()
    assert (target / "current/source/schemas/capability-v1.schema.json").is_file()


def test_reinstall_reuses_same_immutable_release(tmp_path):
    target = tmp_path / "catalog"
    first = install.install(catalog.DEFAULT_ROOT, target)
    second = install.install(catalog.DEFAULT_ROOT, target)
    assert second["sha256"] == first["sha256"]
    assert second["previous"] == first["sha256"]
    releases = [path for path in (target / "releases").iterdir() if path.is_dir()]
    assert len(releases) == 1


def test_install_rejects_non_symlink_current(tmp_path):
    target = tmp_path / "catalog"
    target.mkdir()
    (target / "current").mkdir()
    with pytest.raises(RuntimeError, match="catalog_current_not_symlink"):
        install.install(catalog.DEFAULT_ROOT, target)
