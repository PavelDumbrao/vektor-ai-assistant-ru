#!/usr/bin/env python3
"""Install the version-pinned Hermes core passive-routing patch without restart."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path


EXPECTED_COMMIT = "3c27eb6234bf91b8ceee9e9071591b31e9b148cb"
PATCHED_FILES = (
    "agent/conversation_loop.py",
    "agent/tool_dispatch_helpers.py",
    "agent/turn_context.py",
    "gateway/run.py",
    "gateway/turn_context.py",
    "hermes_cli/plugins.py",
    "run_agent.py",
    "tools/approval.py",
    "tools/transcription_tools.py",
    "plugins/platforms/telegram/adapter.py",
    "plugins/platforms/telegram/passive_media.py",
    "plugins/platforms/telegram/passive_updates.py",
)
BASE_SHA256 = {
    "agent/conversation_loop.py": (
        "eca90bba22034fd2ca3314d994de63a642439ff1c31c7a1319a3ecbecc64a9a9"
    ),
    "agent/tool_dispatch_helpers.py": (
        "e660918cfbe1eb9546282721461e487b9f0b5c4faf666c5cd2c858dd7e62f7a4"
    ),
    "agent/turn_context.py": (
        "a0e136367b64007d7b49ea006ab0aa7dcc66b12134b512a463a03bd69fb8a90c"
    ),
    "gateway/run.py": (
        "0b749a90ff5740b5c8ce9d138f869aca19295f4c458e3b680e9be9fd7b0fb2ec"
    ),
    "gateway/turn_context.py": (
        "4063efecb3e0832bee101d7fbb31c910310d80f60dbbb3f74736138482ed689d"
    ),
    "hermes_cli/plugins.py": (
        "2cb8c309ce7747331669f22060a7e357e9c7a865f07cc779f30f17f8ce90daf5"
    ),
    "run_agent.py": (
        "7d22f38b5eac3b2951fa28aec5b2b06ec007bc96d88e5d35278481bf2ab52122"
    ),
    "tools/approval.py": (
        "651b2ad8041aad4c862ff793937646c3541de9786b8fbabc8301665ef7c3cfbc"
    ),
    "tools/transcription_tools.py": (
        "36ee10a0e9ec9e67c4071cae45f1257d2aa7b8ba6f8e0a28f512d88bd0b11f76"
    ),
    "plugins/platforms/telegram/adapter.py": (
        "9fb01bf3069abbce0f93ad7ae06d215143be8c308a7cf7017a893d49b36b6641"
    ),
    "plugins/platforms/telegram/passive_media.py": None,
    "plugins/platforms/telegram/passive_updates.py": None,
}
PATCHED_SHA256 = {
    "agent/conversation_loop.py": (
        "99cb4e2551715ece1ccafd7eef0346c3768a5773ab70c488eeb6634b6c7977bc"
    ),
    "agent/tool_dispatch_helpers.py": (
        "f89e66b7898c810ac2efcf07aa2cfb35fbeceb4fba22ee078cba66808b86b8af"
    ),
    "agent/turn_context.py": (
        "b9449f2360403ab961e8a39eda89b8a3744c63cd253d68d2fdfecbc890d437a6"
    ),
    "gateway/run.py": (
        "68ffe24f1611a6894bafe78a8f4ea8a869b5884dde100b1525060ec98154b2af"
    ),
    "gateway/turn_context.py": (
        "58bed0055c3ba28ef0f6dbf3a4747301ca6597f07314a47cf1a35559bd61c427"
    ),
    "hermes_cli/plugins.py": (
        "ce9d9a8c14ff37c3749cf28c4d0ee85acbeee4e03b698dd985017c6c36cbc680"
    ),
    "run_agent.py": (
        "9a769d16a843826b3ebd663a881ed8531d3d3d68bdd7f8bf11086ac8096ad48c"
    ),
    "tools/approval.py": (
        "6fed585306d0aac9e77b22fa6866d577c3baa99bdbb6999243ed697308d13ce5"
    ),
    "tools/transcription_tools.py": (
        "88acaa013e84cd544b082f582ef17d7ea1667de361e1a34af0b7b0632bbc111d"
    ),
    "plugins/platforms/telegram/adapter.py": (
        "0e6e0684eb854a576078f4acf7661dc39c092826423934b3c15829355367a3bf"
    ),
    "plugins/platforms/telegram/passive_media.py": (
        "2896b8fe56bba55e50bf9367b846a7e4f9a10a03a6967b3c61f6c686eaaadaae"
    ),
    "plugins/platforms/telegram/passive_updates.py": (
        "c75d246eca8a56927e5898d250b7cc5fbafa705787dfdf155ba23d49775c5343"
    ),
}

# Exact hashes of previously deployed, reviewed patch revisions that may be
# upgraded in place.  This is deliberately per-file and version-pinned: an
# arbitrary dirty checkout must still fail before any target mutation.  Keep
# these values when refreshing PATCHED_SHA256 for a new release so an existing
# client can move forward without first restoring the clean upstream base.
MIGRATION_SHA256 = {
    "agent/conversation_loop.py": (
        "79d577caa465ae53003d2b2282c5a8db75d4f604d9df3a20603dc64f92004966",
    ),
    "agent/tool_dispatch_helpers.py": (
        "b2748f0947aa031a40f68e5c7e3f471eb5f1264b83c8beb388781d28c3826fc2",
    ),
    "agent/turn_context.py": (
        "7612c7358eb1903a461999df9e0f723fb4af097cb62a56f7d0b0e33edd7f9301",
    ),
    "gateway/run.py": (
        "10ae956def15b7aa6df98f207075600845c7b8a52b24f4b00e640db41593e793",
        "ea2321834f3ce76dde5f77213baf9a819d2afddecded6548f9b584fd1ef52a53",
        "15e406b999a66663b0b39a093cc5ca6ca0070f099230902abf23851fad722139",
    ),
    "hermes_cli/plugins.py": (
        "ce9d9a8c14ff37c3749cf28c4d0ee85acbeee4e03b698dd985017c6c36cbc680",
    ),
    "plugins/platforms/telegram/adapter.py": (
        "7b0f15df01947f9cc9eb0f1d5172722ee1a62d5b4273d2174aedd2a78db940fa",
        "56954745a4e4272c682fc0c719b371c20871ab6d528b70fdfd06cca1e5b05c7f",
        "9de3a370a23c9e0a4a83eeed03822a571376788598edaaa58264ffcfb2e35a07",
        "b7f7866f2ba96f8d8cc582edc18704752c20a873cbfa05de5db3695f80a33853",
        "ebb118aa9718243a1b8f6fddf149d00c32f9ae590553043b910c3c71c829719b",
        "81a905c8910ed288725679f0e1b0def5ff80d6a3631c7234dc952021846b4d4d",
        "427f483036fe1e832167a83dc21d8f4487aabd05ff9492da283684cc3e63c5be",
    ),
    "plugins/platforms/telegram/passive_media.py": (
        "bed0619a9d5f03f909831905d621d4bc517fe20526d5ffc6123fd520222ca546",
        "2fbbc918652612512dc9c964f382385bf5171c8cd201761e6fa53eab29665a96",
        "a833b9b5b20a264c98514a17aba3412a82875630a4fe09591fed0a8a286acb62",
        "14f09172e803fa5bc8c8fa393ca8095aaa5a8d977aa4ed6669cb95e214645158",
        "64b2b7e023b5b188c86c48febf06a9eee1056b78507c2bfcb301e154ca8b8cfd",
    ),
    "plugins/platforms/telegram/passive_updates.py": (
        "eace366f3f510f0ba615b560c1215a377857b156377ea0d3a8cfe652f0e7782c",
        "47b3ce9decb47294dddfaf8df85ee6c8b4f01c61c47f4427e8ea9466c6ad4c69",
        "84ac8ae5e1c043704817938c178552948b3e8eab022366b2353fa8c0727f9980",
        "d55e89a25228c756d86fdde73274ed43605105273fc37c1063e45c17a155684b",
        "9df0bb6ca323b273e9a7c1197872b696533bf8636bd9b8d5deb739d94d47deee",
    ),
}


class CorePatchError(RuntimeError):
    pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_regular_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CorePatchError("Durability target is unsafe")
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_mkdir_parents(path: Path, *, boundary: Path) -> None:
    missing: list[Path] = []
    current = path
    while current != boundary and not current.exists():
        missing.append(current)
        current = current.parent
    if current != boundary and not current.exists():
        raise CorePatchError("Backup path is unsafe")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in reversed(missing):
        os.chmod(directory, 0o700)
        _fsync_directory(directory)
        _fsync_directory(directory.parent)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_copy(source: Path, target: Path) -> None:
    if target.is_symlink() or target.parent.is_symlink():
        raise CorePatchError(f"Refusing symlinked target: {target}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o644)
        with source.open("rb") as input_handle, os.fdopen(fd, "wb") as output_handle:
            fd = -1
            shutil.copyfileobj(input_handle, output_handle)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        os.replace(temp, target)
        _fsync_directory(target.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        if temp.exists():
            temp.unlink()


def _git_head(repo: Path) -> str:
    result = subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            f"safe.directory={repo}",
            "-C",
            str(repo),
            "rev-parse",
            "HEAD",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
        env={
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": "/tmp",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    if result.returncode != 0:
        raise CorePatchError("Could not verify target Hermes commit")
    return result.stdout.strip()


def _require_root_owned_node(path: Path, *, directory: bool) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CorePatchError("Patch source trust check failed") from exc
    expected_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not expected_type
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise CorePatchError("Patch source trust check failed")


def _require_root_owned_ancestor_chain(path: Path) -> None:
    if not path.is_absolute():
        raise CorePatchError("Patch source trust check failed")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise CorePatchError("Patch source trust check failed") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0
        ):
            raise CorePatchError("Patch source trust check failed")
        # A root-owned sticky directory such as /tmp cannot be used to replace
        # another root-owned entry. Other group/world-writable ancestors are
        # not acceptable trust anchors.
        if stat.S_IMODE(info.st_mode) & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise CorePatchError("Patch source trust check failed")


def _require_root_owned_sources(source_repo: Path) -> None:
    """Ensure root does not consume installer code or patches writable by a client."""

    launcher = Path(__file__).absolute()
    _require_root_owned_ancestor_chain(launcher.parent)
    _require_root_owned_node(launcher, directory=False)
    _require_root_owned_ancestor_chain(source_repo.parent)
    _require_root_owned_node(source_repo, directory=True)
    checked_directories = {source_repo}
    for relative in PATCHED_FILES:
        current = source_repo
        for part in Path(relative).parts[:-1]:
            current /= part
            if current not in checked_directories:
                _require_root_owned_node(current, directory=True)
                checked_directories.add(current)
        _require_root_owned_node(source_repo / relative, directory=False)
    if set(PATCHED_SHA256) != set(PATCHED_FILES) or any(
        _sha256(source_repo / relative) != PATCHED_SHA256[relative]
        for relative in PATCHED_FILES
    ):
        raise CorePatchError("Patch source hash mismatch")


def _require_no_symlink_components(path: Path, *, code: str) -> None:
    if not path.is_absolute():
        raise CorePatchError(code)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise CorePatchError(code) from exc
        if stat.S_ISLNK(info.st_mode):
            raise CorePatchError(code)


def _require_selected_owned_directory(path: Path, *, uid: int) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CorePatchError("Target Hermes layout is unsafe") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise CorePatchError("Target Hermes layout is unsafe")


def _require_selected_owned_regular(path: Path, *, uid: int) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CorePatchError("Target Hermes layout is unsafe") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise CorePatchError("Target Hermes layout is unsafe")


def _require_target_layout(
    target_repo_raw: Path,
    *,
    linux_home: Path,
    uid: int,
) -> Path:
    expected = linux_home / ".hermes" / "hermes-agent"
    if (
        not target_repo_raw.is_absolute()
        or target_repo_raw != expected
        or not linux_home.is_absolute()
    ):
        raise CorePatchError("Target Hermes path is noncanonical")

    _require_no_symlink_components(linux_home, code="Target Hermes layout is unsafe")
    _require_no_symlink_components(expected, code="Target Hermes layout is unsafe")
    try:
        if (
            linux_home.resolve(strict=True) != linux_home
            or expected.resolve(strict=True) != expected
        ):
            raise CorePatchError("Target Hermes path is noncanonical")
    except OSError as exc:
        raise CorePatchError("Target Hermes layout is unsafe") from exc

    hermes_home = linux_home / ".hermes"
    backups = hermes_home / "backups"
    for directory in (linux_home, hermes_home, expected, backups):
        _require_selected_owned_directory(directory, uid=uid)

    checked_directories: set[Path] = {expected}
    for relative in PATCHED_FILES:
        target = expected / relative
        current = expected
        for part in Path(relative).parts[:-1]:
            current /= part
            if current not in checked_directories:
                _require_selected_owned_directory(current, uid=uid)
                checked_directories.add(current)
        if target.exists() or target.is_symlink():
            _require_selected_owned_regular(target, uid=uid)

    return expected


def _owner_environment(*, owner: str, linux_home: Path) -> dict[str, str]:
    return {
        "HOME": str(linux_home),
        "USER": owner,
        "LOGNAME": owner,
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
    }


def _compile_current_owner(
    *,
    target_repo: Path,
    owner: str,
    linux_home: Path,
) -> subprocess.CompletedProcess[str]:
    if os.geteuid() == 0:
        raise CorePatchError("Owner apply refused privileged execution")
    python_bin = target_repo / "venv" / "bin" / "python"
    if not python_bin.is_file():
        raise CorePatchError("Target Hermes Python is missing or unsafe")
    return subprocess.run(
        [str(python_bin), "-I", "-m", "py_compile"]
        + [str(target_repo / relative) for relative in PATCHED_FILES],
        cwd=str(target_repo),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
        env=_owner_environment(owner=owner, linux_home=linux_home),
    )


def _changed_files(source_repo: Path, target_repo: Path) -> list[str]:
    changed: list[str] = []
    for relative in PATCHED_FILES:
        source = source_repo / relative
        target = target_repo / relative
        if not source.is_file() or target.parent.is_symlink():
            raise CorePatchError(f"Unsafe or missing patch source: {relative}")
        if target.is_file() and _sha256(target) == _sha256(source):
            continue
        base_hash = BASE_SHA256[relative]
        migration_hashes = set(MIGRATION_SHA256.get(relative, ()))
        if target.is_file():
            target_hash = _sha256(target)
            accepted_hashes = migration_hashes
            if base_hash is not None:
                accepted_hashes.add(base_hash)
            if target_hash not in accepted_hashes:
                raise CorePatchError(f"Target drift detected: {relative}")
        elif target.exists() or base_hash is not None:
            raise CorePatchError(f"Target drift detected: {relative}")
        changed.append(relative)
    return changed


def _apply_as_owner(source_repo: Path, target_repo: Path, owner: str) -> Path | None:
    try:
        owner_entry = pwd.getpwnam(owner)
    except KeyError as exc:
        raise CorePatchError("Unknown Linux user") from exc
    uid = int(owner_entry.pw_uid)
    gid = int(owner_entry.pw_gid)
    if (
        uid <= 0
        or os.geteuid() != uid
        or os.getegid() != gid
        or os.getgroups()
    ):
        raise CorePatchError("Owner apply identity mismatch")
    linux_home = Path(owner_entry.pw_dir)
    source_repo = source_repo.resolve(strict=True)
    _require_root_owned_sources(source_repo)
    target_repo = _require_target_layout(
        target_repo,
        linux_home=linux_home,
        uid=uid,
    )
    if _git_head(target_repo) != EXPECTED_COMMIT:
        raise CorePatchError(
            "Unsupported Hermes commit; rebase and re-run regression tests first"
        )
    changed = _changed_files(source_repo, target_repo)
    if not changed:
        return None

    # Recheck in the unprivileged process immediately before its first target
    # mutation. Any remaining owner-controlled race is confined to that same
    # UID and cannot redirect a root filesystem write.
    target_repo = _require_target_layout(
        target_repo,
        linux_home=linux_home,
        uid=uid,
    )
    hermes_home = target_repo.parent
    backup = hermes_home / "backups" / f"passive-secretary-core-{time.time_ns()}"
    backup.mkdir(mode=0o700)
    os.chmod(backup, 0o700)
    _fsync_directory(backup)
    _fsync_directory(backup.parent)
    manifest = {"commit": EXPECTED_COMMIT, "files": {}}
    for relative in changed:
        target = target_repo / relative
        existed = target.is_file()
        manifest["files"][relative] = {"existed": existed}
        if existed:
            backup_target = backup / relative
            _private_mkdir_parents(backup_target.parent, boundary=backup)
            shutil.copy2(target, backup_target)
            os.chmod(backup_target, 0o600)
            _fsync_regular_file(backup_target)
            _fsync_directory(backup_target.parent)
    manifest_path = backup / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(manifest_path, 0o600)
    _fsync_regular_file(manifest_path)
    _fsync_directory(backup)

    try:
        for relative in changed:
            _atomic_copy(source_repo / relative, target_repo / relative)
        result = _compile_current_owner(
            target_repo=target_repo,
            owner=owner,
            linux_home=linux_home,
        )
        if result.returncode != 0:
            raise CorePatchError("Installed core patch did not compile")
    except Exception:
        for relative in reversed(changed):
            target = target_repo / relative
            backup_target = backup / relative
            if backup_target.is_file():
                _atomic_copy(backup_target, target)
            elif target.exists() and not target.is_symlink():
                target.unlink()
        raise
    return backup


def _stage_trusted_bundle(source_repo: Path, stage: Path) -> tuple[Path, Path]:
    os.chmod(stage, 0o755)
    runner = stage / "install_core_patch.py"
    shutil.copyfile(Path(__file__), runner)
    os.chmod(runner, 0o555)
    staged_source = stage / "source"
    staged_source.mkdir(mode=0o755)
    directories = {staged_source}
    for relative in PATCHED_FILES:
        destination = staged_source / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        current = destination.parent
        while current != staged_source.parent:
            directories.add(current)
            if current == staged_source:
                break
            current = current.parent
        shutil.copyfile(source_repo / relative, destination)
        os.chmod(destination, 0o444)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        os.chmod(directory, 0o555)
    return runner, staged_source


def _run_apply_as_owner(
    *,
    runner: Path,
    source_repo: Path,
    target_repo: Path,
    owner: str,
    uid: int,
    gid: int,
    linux_home: Path,
) -> Path | None:
    result = subprocess.run(
        [
            "/usr/bin/python3",
            str(runner),
            "--internal-apply",
            "--source-repo",
            str(source_repo),
            "--target-repo",
            str(target_repo),
            "--owner",
            owner,
        ],
        cwd="/",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=120,
        check=False,
        env=_owner_environment(owner=owner, linux_home=linux_home),
        user=uid,
        group=gid,
        extra_groups=(),
        umask=0o077,
    )
    if result.returncode != 0:
        raise CorePatchError("Core patch owner apply failed")
    try:
        payload = json.loads(result.stdout)
    except Exception as exc:
        raise CorePatchError("Core patch owner result invalid") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise CorePatchError("Core patch owner result invalid")
    backup_raw = payload.get("backup")
    if backup_raw is None:
        return None
    backup = Path(str(backup_raw))
    expected_parent = target_repo.parent / "backups"
    if not backup.is_absolute() or backup.parent != expected_parent:
        raise CorePatchError("Core patch owner result invalid")
    return backup


def install(source_repo: Path, target_repo: Path, owner: str) -> Path | None:
    if os.geteuid() != 0:
        raise CorePatchError("Core patch installation must run as root")
    try:
        owner_entry = pwd.getpwnam(owner)
    except KeyError as exc:
        raise CorePatchError("Unknown Linux user") from exc
    uid = int(owner_entry.pw_uid)
    gid = int(owner_entry.pw_gid)
    if uid <= 0:
        raise CorePatchError("Core patch owner must be unprivileged")
    source_repo_raw = source_repo
    if not source_repo_raw.is_absolute() or source_repo_raw.is_symlink():
        raise CorePatchError("Patch source trust check failed")
    try:
        source_repo = source_repo_raw.resolve(strict=True)
    except OSError as exc:
        raise CorePatchError("Patch source trust check failed") from exc
    if source_repo != source_repo_raw:
        raise CorePatchError("Patch source trust check failed")
    _require_root_owned_sources(source_repo)
    linux_home = Path(owner_entry.pw_dir)
    target_repo = _require_target_layout(
        target_repo,
        linux_home=linux_home,
        uid=uid,
    )

    # Root only copies already trusted code into an immutable /tmp bundle.
    # Every write below the client-owned ~/.hermes tree happens in the spawned
    # process after uid/gid/groups have been irreversibly dropped.
    with tempfile.TemporaryDirectory(prefix="hermes-core-stage-", dir="/tmp") as raw:
        runner, staged_source = _stage_trusted_bundle(source_repo, Path(raw))
        return _run_apply_as_owner(
            runner=runner,
            source_repo=staged_source,
            target_repo=target_repo,
            owner=owner,
            uid=uid,
            gid=gid,
            linux_home=linux_home,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--internal-apply", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-repo", required=True, type=Path)
    parser.add_argument("--target-repo", required=True, type=Path)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    try:
        backup = (
            _apply_as_owner(args.source_repo, args.target_repo, args.owner)
            if args.internal_apply
            else install(args.source_repo, args.target_repo, args.owner)
        )
    except CorePatchError as exc:
        print(f"error={exc}", file=__import__("sys").stderr)
        return 1
    if args.internal_apply:
        print(json.dumps({"ok": True, "backup": str(backup) if backup else None}))
        return 0
    print("installed=true" if backup else "installed=already_current")
    if backup:
        print(f"backup={backup}")
    print("restart_required=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
