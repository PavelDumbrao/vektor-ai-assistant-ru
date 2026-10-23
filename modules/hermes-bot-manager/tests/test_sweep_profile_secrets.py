from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
import sweep_profile_secrets as sps


@pytest.fixture
def fake_platform_env(tmp_path: Path) -> Path:
    env_file = tmp_path / "platform.env"
    env_file.write_text(
        "TAVILY_API_KEY=tvly-secret-1234567890\n"
        "LLM_API_KEY=sk-llm-secret-1234567890\n"
        "OPENROUTER_API_KEY=sk-or-secret-1234567890\n",
        encoding="utf-8",
    )
    return env_file


def test_scan_two_keys_in_env_tavily_first_reports_both(tmp_path: Path, fake_platform_env: Path):
    """(а) два ключа в .env, Tavily первым -> отчет содержит оба"""
    home = tmp_path / "home" / ".hermes"
    home.mkdir(parents=True)
    # Put Tavily first in .env
    (home / ".env").write_text(
        "TAVILY_API_KEY=tvly-secret-1234567890\n"
        "LLM_API_KEY=sk-llm-secret-1234567890\n",
        encoding="utf-8",
    )
    values = sps.load_platform_values(fake_platform_env)
    res = sps.scan(home, values)

    # Neither should be accepted because .env contains LLM_API_KEY which is not in ACCEPTED
    assert res.accepted == []
    vars_found = {var for rel, var in res.hits if rel == ".env"}
    assert vars_found == {"TAVILY_API_KEY", "LLM_API_KEY"}


def test_scan_accepted_exception_when_only_tavily(tmp_path: Path, fake_platform_env: Path):
    home = tmp_path / "home" / ".hermes"
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "TAVILY_API_KEY=tvly-secret-1234567890\n"
        "OTHER_UNMATCHED=something\n",
        encoding="utf-8",
    )
    values = sps.load_platform_values(fake_platform_env)
    res = sps.scan(home, values)

    assert res.hits == []
    assert res.accepted == [(".env", "TAVILY_API_KEY")]


def test_sweep_and_scan_symlink_not_read_or_moved(tmp_path: Path, fake_platform_env: Path):
    """(б) симлинк .env.bak-x -> наружу не читается и не переносится, скрипт не падает"""
    home = tmp_path / "home" / ".hermes"
    home.mkdir(parents=True)
    store = tmp_path / "store"

    # Outside secret file
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    outside_file = outside / "secret.env"
    outside_file.write_text("LLM_API_KEY=sk-llm-secret-1234567890\n", encoding="utf-8")

    # Symlink inside home pointing outside
    symlink_path = home / ".env.bak-x"
    symlink_path.symlink_to(outside_file)

    values = sps.load_platform_values(fake_platform_env)

    # 1. Sweep must NOT move the symlink
    moved = sps.sweep(home, store, apply=True, values=values)
    assert symlink_path not in moved
    assert symlink_path.is_symlink()
    assert outside_file.exists()
    assert not (store / ".env.bak-x").exists()

    # 2. Scan must NOT follow symlink into outside_file
    res = sps.scan(home, values)
    assert res.hits == []
    assert res.accepted == []
    assert res.skipped == 0


def test_sweep_zip_archive_with_env_inside_is_transferred(tmp_path: Path, fake_platform_env: Path):
    """(в) zip с .env внутри -> переносится"""
    home = tmp_path / "home" / ".hermes"
    home.mkdir(parents=True)
    store = tmp_path / "store"

    # Create zip file with .env containing a secret
    zip_path = home / "archive-backup.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(".env", "LLM_API_KEY=sk-llm-secret-1234567890\n")

    values = sps.load_platform_values(fake_platform_env)
    moved = sps.sweep(home, store, apply=True, values=values)

    assert zip_path in moved
    assert not zip_path.exists()
    assert (store / "archive-backup.zip").exists()


def test_scan_large_file_key_at_end_found(tmp_path: Path, fake_platform_env: Path):
    """(г) большой файл с ключом в конце -> найден"""
    home = tmp_path / "home" / ".hermes"
    home.mkdir(parents=True)

    # File larger than 8MB chunk (e.g. 10MB), secret at the very end
    large_file = home / "large_data.log"
    padding_size = 10 * 1024 * 1024
    with open(large_file, "wb") as f:
        f.write(b"A" * padding_size)
        f.write(b"\nOPENROUTER_API_KEY=sk-or-secret-1234567890\n")

    values = sps.load_platform_values(fake_platform_env)
    res = sps.scan(home, values)

    assert res.skipped == 0
    assert ("large_data.log", "OPENROUTER_API_KEY") in res.hits


def test_scan_file_exceeding_512mb_skipped(tmp_path: Path, fake_platform_env: Path, monkeypatch):
    home = tmp_path / "home" / ".hermes"
    home.mkdir(parents=True)
    huge_file = home / "huge.bin"
    huge_file.write_bytes(b"content")

    # Simulate st_size > 512MB
    orig_lstat = os.lstat
    def fake_lstat(path, *args, **kwargs):
        st = orig_lstat(path, *args, **kwargs)
        if str(path).endswith("huge.bin"):
            class FakeStat:
                st_mode = st.st_mode
                st_size = 600 * 1024 * 1024
            return FakeStat()
        return st
    monkeypatch.setattr(os, "lstat", fake_lstat)

    values = sps.load_platform_values(fake_platform_env)
    res = sps.scan(home, values)
    assert res.skipped == 1
    assert res.hits == []
