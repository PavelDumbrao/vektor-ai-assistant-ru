#!/usr/bin/env python3
"""Provision a shared, loopback-only PostgreSQL cluster for Hermes archives.

The command is deliberately root-only. Cluster and tenant secrets are generated
locally, stored in root-only state, and never emitted to stdout, stderr, or a
subprocess argument.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import errno
import fcntl
import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import quote


LAUNCHER_FILE = Path(os.path.abspath(__file__))
MODULE_DIR = LAUNCHER_FILE.resolve().parent
COMPOSE_FILE = MODULE_DIR / "compose.yaml"
DEFAULT_STATE_DIR = Path("/var/lib/hermes-passive-secretary-postgres")
DEFAULT_PORT = 15432
DEFAULT_DOCKER_BIN = Path("/usr/bin/docker")
SYSTEM_PYTHON = Path("/usr/bin/python3")
PROJECT_NAME = "passive-secretary-postgres"
SERVICE_NAME = "postgres"
ADMIN_USER = "passive_secretary_admin"
ADMIN_DATABASE = "postgres"
DATABASE_URL_ENV = "PASSIVE_SECRETARY_DATABASE_URL"
SOURCE_REF_KEY_ENV = "PASSIVE_SECRETARY_SOURCE_REF_KEY"
IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
CLIENT_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
OWNER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,63}$")
CONTAINER_ID_RE = re.compile(r"^[a-f0-9]{12,64}$")
STATE_VERSION = 1


class ProvisionError(RuntimeError):
    """A safe, user-facing provisioning failure with no secret material."""


@dataclass(frozen=True)
class ClusterConfig:
    admin_user: str
    admin_password: str
    admin_database: str
    port: int


@dataclass(frozen=True)
class TenantState:
    version: int
    status: str
    client_id: str
    tenant_id: str
    owner: str
    hermes_env: str
    database: str
    role: str
    password: str
    source_ref_key: str
    pending_password: str = ""
    pending_source_ref_key: str = ""


@dataclass(frozen=True)
class DatabaseInspection:
    role_exists: bool
    role_can_login: bool
    role_is_superuser: bool
    role_can_create_db: bool
    role_can_create_role: bool
    role_can_replicate: bool
    role_bypasses_rls: bool
    role_inherits: bool
    # Both directions matter. ``role_membership_count`` is the number of roles
    # the tenant belongs to; ``role_grantee_count`` is the number of members to
    # which the tenant role itself has been granted.
    role_membership_count: int
    role_grantee_count: int
    role_connection_limit: int
    database_exists: bool
    database_owner: str
    public_can_connect: bool
    non_owner_connect_grantee_count: int
    other_database_connect_count: int


def _validate_identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ProvisionError(
            f"Invalid {label}; use 1-63 lowercase letters, digits, and underscores"
        )
    if value in {ADMIN_USER, "postgres", "public", "pg_database_owner"}:
        raise ProvisionError(f"Reserved {label}")
    return value


def _validate_client_id(value: str) -> str:
    if not CLIENT_RE.fullmatch(value):
        raise ProvisionError(
            "Invalid client-id; use 2-64 lowercase letters, digits, hyphens, or underscores"
        )
    return value


def _validate_port(value: int) -> int:
    if not 1024 <= value <= 65535:
        raise ProvisionError("PostgreSQL port must be between 1024 and 65535")
    return value


def _reject_symlink(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise ProvisionError(f"Refusing symlinked {label}: {path}")


def _reject_symlink_components(path: Path, *, label: str) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ProvisionError(f"Refusing {label} below a symlink: {component}")


def _validate_state_dir_argument(path: Path) -> Path:
    """Accept only the one canonical state path used by this provisioner."""

    if path != DEFAULT_STATE_DIR:
        raise ProvisionError(
            f"state-dir must be exactly {DEFAULT_STATE_DIR}"
        )
    return path


def _assert_safe_root_directory(path: Path, *, label: str) -> None:
    """Fail closed unless *path* is a non-writable root:root directory."""

    try:
        info = path.lstat()
    except OSError as exc:
        raise ProvisionError(f"Cannot inspect {label}: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ProvisionError(f"Refusing symlinked {label}: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise ProvisionError(f"Expected directory for {label}: {path}")
    if info.st_uid != 0 or info.st_gid != 0:
        raise ProvisionError(f"{label} must be owned by root:root: {path}")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ProvisionError(f"{label} must not be group/world writable: {path}")


def _assert_safe_root_file(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProvisionError(f"Cannot inspect {label}: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ProvisionError(f"{label} must be a regular non-symlink file: {path}")
    if info.st_uid != 0 or info.st_gid != 0:
        raise ProvisionError(f"{label} must be owned by root:root: {path}")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ProvisionError(f"{label} must not be group/world writable: {path}")


def _assert_root_owned_runtime() -> None:
    """Refuse Docker orchestration from a mutable or symlinked code bundle."""

    for directory in {LAUNCHER_FILE.parent, MODULE_DIR}:
        for component in reversed((directory, *directory.parents)):
            _assert_safe_root_directory(component, label="provisioner ancestor")
    _assert_safe_root_file(LAUNCHER_FILE, label="provisioner launcher")
    _assert_safe_root_file(COMPOSE_FILE, label="PostgreSQL compose file")


def _validate_docker_binary(value: Path) -> str:
    if value != DEFAULT_DOCKER_BIN:
        raise ProvisionError(f"docker-bin must be exactly {DEFAULT_DOCKER_BIN}")
    for component in reversed((value.parent, *value.parent.parents)):
        _assert_safe_root_directory(component, label="docker binary ancestor")
    _assert_safe_root_file(value, label="docker binary")
    if not os.access(value, os.X_OK):
        raise ProvisionError("docker binary is not executable")
    return str(value)


def _prepare_canonical_state_dir(path: Path) -> None:
    """Create the canonical state directory below already-safe parents.

    Every existing component is checked with ``lstat``.  Only the leaf may be
    missing, and its parent is root-owned and non-writable before ``mkdir``.
    This prevents symlink and parent-rename attacks by unprivileged users.
    """

    _validate_state_dir_argument(path)
    components = list(reversed((path, *path.parents)))
    for component in components[:-1]:
        _assert_safe_root_directory(component, label="state-dir ancestor")

    created = False
    try:
        path.lstat()
    except FileNotFoundError:
        try:
            os.mkdir(path, 0o700)
            created = True
        except OSError as exc:
            raise ProvisionError("Could not create canonical state-dir") from exc
    except OSError as exc:
        raise ProvisionError("Could not inspect canonical state-dir") from exc

    if created:
        # ``mkdir`` is still affected by a permissive process umask.
        os.chmod(path, 0o700)
    _assert_safe_root_directory(path, label="state-dir")
    # A pre-existing safe-but-readable directory is tightened. Unsafe writable
    # directories were rejected above instead of silently repaired.
    os.chmod(path, 0o700)
    if created:
        _fsync_directory(path)
        _fsync_directory(path.parent)


def _assert_private_root_lock_fd(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ProvisionError("Provision lock is not a regular file")
    if info.st_uid != 0 or info.st_gid != 0:
        raise ProvisionError("Provision lock must be owned by root:root")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ProvisionError("Provision lock mode must be exactly 0600")


@contextlib.contextmanager
def _exclusive_state_lock(state_dir: Path):
    """Hold a non-blocking inter-process lock for the caller's whole flow."""

    lock_path = state_dir / ".provision.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ProvisionError("Could not open canonical provision lock") from exc
    try:
        _assert_private_root_lock_fd(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise ProvisionError(
                    "Another PostgreSQL provision or rotation is already running"
                ) from exc
            raise ProvisionError("Could not acquire canonical provision lock") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _provision_lock(state_dir: Path):
    _prepare_canonical_state_dir(state_dir)
    with _exclusive_state_lock(state_dir):
        yield


def _ensure_private_dir(path: Path, *, uid: int, gid: int) -> None:
    if not path.is_absolute():
        raise ProvisionError("State and secret directories must use absolute paths")
    if path == Path(path.anchor):
        raise ProvisionError("Refusing to change permissions on a filesystem root")
    _reject_symlink_components(path, label="directory")
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    _reject_symlink(path, label="directory")
    if not path.is_dir():
        raise ProvisionError(f"Expected directory: {path}")
    os.chmod(path, 0o700)
    os.chown(path, uid, gid)
    if created:
        _fsync_directory(path)
        _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(
    path: Path,
    content: str,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    _reject_symlink(path, label="secret file")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ProvisionError(f"Unsafe parent directory: {path.parent}")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        current = os.fstat(fd)
        if current.st_uid != uid or current.st_gid != gid:
            # Root-owned state may need an explicit owner. Owner-sandboxed
            # Hermes writes are already created with the final uid/gid and must
            # not need retained root privileges.
            os.fchown(fd, uid, gid)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _reject_symlink(path, label="secret file")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _assert_private_root_file(path: Path) -> None:
    _reject_symlink(path, label="cluster secret")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ProvisionError("Cluster secret path is not a regular file")
    if info.st_uid != 0 or info.st_gid != 0:
        raise ProvisionError("Cluster secret must be owned by root:root")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ProvisionError("Cluster secret mode must be exactly 0600")


def _parse_simple_env(content: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ProvisionError("Cluster secret file is malformed")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not value:
            raise ProvisionError("Cluster secret file is malformed")
        if key in result:
            raise ProvisionError("Cluster secret file contains duplicate keys")
        result[key] = value
    return result


def _cluster_env_content(config: ClusterConfig) -> str:
    return (
        f"POSTGRES_USER={config.admin_user}\n"
        f"POSTGRES_PASSWORD={config.admin_password}\n"
        f"POSTGRES_DB={config.admin_database}\n"
        f"POSTGRES_PORT={config.port}\n"
    )


def load_or_create_cluster_config(
    state_dir: Path,
    *,
    requested_port: int | None,
) -> tuple[ClusterConfig, Path, bool]:
    _ensure_private_dir(state_dir, uid=0, gid=0)
    path = state_dir / "cluster.env"
    if path.exists():
        _assert_private_root_file(path)
        values = _parse_simple_env(path.read_text(encoding="utf-8"))
        required = {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_PORT"}
        if set(values) != required:
            raise ProvisionError("Cluster secret file has an unexpected key set")
        try:
            port = _validate_port(int(values["POSTGRES_PORT"]))
        except ValueError as exc:
            raise ProvisionError("Cluster secret contains an invalid port") from exc
        config = ClusterConfig(
            admin_user=values["POSTGRES_USER"],
            admin_password=values["POSTGRES_PASSWORD"],
            admin_database=values["POSTGRES_DB"],
            port=port,
        )
        if config.admin_user != ADMIN_USER or config.admin_database != ADMIN_DATABASE:
            raise ProvisionError("Cluster identity does not match this provisioner")
        if len(config.admin_password) < 32 or any(ch.isspace() for ch in config.admin_password):
            raise ProvisionError("Cluster password is malformed")
        if requested_port is not None and requested_port != config.port:
            raise ProvisionError("Requested port differs from the existing cluster port")
        return config, path, False

    port = _validate_port(requested_port if requested_port is not None else DEFAULT_PORT)
    config = ClusterConfig(
        admin_user=ADMIN_USER,
        admin_password=secrets.token_hex(32),
        admin_database=ADMIN_DATABASE,
        port=port,
    )
    _atomic_write(path, _cluster_env_content(config), mode=0o600, uid=0, gid=0)
    return config, path, True


class CommandRunner:
    """Run bounded commands without reflecting subprocess output into errors."""

    def __call__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        timeout: float = 60,
        operation: str,
    ) -> str:
        child_env = {
            "HOME": "/root",
            "USER": "root",
            "LOGNAME": "root",
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "DOCKER_CONFIG": "/root/.docker",
        }
        if env:
            unexpected = set(env) - {"POSTGRES_CLUSTER_ENV", "PGPASSWORD"}
            if unexpected:
                raise ProvisionError("Unexpected child-process environment key")
            if any(
                not isinstance(value, str)
                or not value
                or "\x00" in value
                or "\n" in value
                or "\r" in value
                for value in env.values()
            ):
                raise ProvisionError("Malformed child-process environment value")
            child_env.update(env)
        try:
            result = subprocess.run(
                list(command),
                cwd=MODULE_DIR,
                env=child_env,
                input=input_text,
                stdin=subprocess.DEVNULL if input_text is None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProvisionError(f"{operation} could not be completed") from exc
        if result.returncode != 0:
            # stderr/stdout may contain a SQL statement supplied over stdin. Do not
            # copy either stream into the exception or terminal output.
            raise ProvisionError(f"{operation} failed")
        return result.stdout.strip()


class PostgresCluster:
    def __init__(
        self,
        *,
        cluster: ClusterConfig,
        cluster_env: Path,
        docker_bin: str,
        runner: Callable[..., str],
    ) -> None:
        self.cluster = cluster
        self.runner = runner
        self.docker_bin = docker_bin
        self.prefix = [
            docker_bin,
            "compose",
            "--project-name",
            PROJECT_NAME,
            "--env-file",
            str(cluster_env),
            "--file",
            str(COMPOSE_FILE),
        ]
        self.compose_env = {"POSTGRES_CLUSTER_ENV": str(cluster_env)}

    def up(self) -> None:
        self.runner(
            [*self.prefix, "up", "--detach"],
            env=self.compose_env,
            timeout=180,
            operation="Starting PostgreSQL",
        )

    def _container_id(self) -> str:
        value = self.runner(
            [*self.prefix, "ps", "--quiet", SERVICE_NAME],
            env=self.compose_env,
            timeout=20,
            operation="Resolving PostgreSQL container",
        ).splitlines()
        if len(value) != 1 or not CONTAINER_ID_RE.fullmatch(value[0]):
            raise ProvisionError("PostgreSQL container identity is unavailable")
        return value[0]

    def wait_until_healthy(
        self,
        *,
        timeout: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        container_id = self._container_id()
        deadline = clock() + timeout
        while clock() < deadline:
            status = self.runner(
                [self.docker_bin, "inspect", "--format={{.State.Health.Status}}", container_id],
                timeout=15,
                operation="Checking PostgreSQL health",
            )
            if status == "healthy":
                return
            if status in {"unhealthy", "exited", "dead"}:
                raise ProvisionError("PostgreSQL entered an unhealthy state")
            sleep(min(2.0, max(0.0, deadline - clock())))
        raise ProvisionError("Timed out waiting for PostgreSQL health")

    def psql(
        self,
        *,
        user: str,
        password: str,
        database: str,
        sql: str,
        secret_sql: bool = False,
        operation: str,
    ) -> str:
        command = [
            *self.prefix,
            "exec",
            "--no-TTY",
            "--env",
            "PGPASSWORD",
            SERVICE_NAME,
            "psql",
            "--no-psqlrc",
            "--set",
            "ON_ERROR_STOP=1",
            "--host",
            "127.0.0.1",
            "--port",
            "5432",
            "--username",
            user,
            "--dbname",
            database,
        ]
        if secret_sql:
            command.extend(["--file", "-"])
            input_text = sql
        else:
            command.extend(["--tuples-only", "--no-align", "--command", sql])
            input_text = None
        return self.runner(
            command,
            env={**self.compose_env, "PGPASSWORD": password},
            input_text=input_text,
            timeout=60,
            operation=operation,
        )

    def admin_scalar(self, sql: str, *, operation: str) -> str:
        return self.psql(
            user=self.cluster.admin_user,
            password=self.cluster.admin_password,
            database=self.cluster.admin_database,
            sql=sql,
            operation=operation,
        )


def _ident(value: str) -> str:
    _validate_identifier(value, "SQL identifier")
    return f'"{value}"'


def _literal(value: str) -> str:
    # Values reaching SQL are either validated identifiers or locally-generated
    # URL-safe secrets. Still quote defensively for future callers.
    return "'" + value.replace("'", "''") + "'"


def inspect_database(cluster: PostgresCluster, *, role: str, database: str) -> DatabaseInspection:
    _validate_identifier(role, "role")
    _validate_identifier(database, "database")
    role_literal = _literal(role)
    database_literal = _literal(database)
    role_row = cluster.admin_scalar(
        "SELECT rolcanlogin::int || '|' || rolsuper::int || '|' || "
        "rolcreatedb::int || '|' || rolcreaterole::int || '|' || "
        "rolreplication::int || '|' || rolbypassrls::int || '|' || "
        "rolinherit::int || '|' || rolconnlimit::text "
        f"FROM pg_roles WHERE rolname = {role_literal};",
        operation="Inspecting tenant role",
    )
    database_owner = cluster.admin_scalar(
        "SELECT pg_get_userbyid(datdba) FROM pg_database "
        f"WHERE datname = {database_literal};",
        operation="Inspecting tenant database",
    )
    public_connect = False
    non_owner_connect_grantee_count = 0
    if database_owner:
        public_value = cluster.admin_scalar(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_database d, "
            "LATERAL aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) acl "
            f"WHERE d.datname = {database_literal} "
            "AND acl.grantee = 0 AND acl.privilege_type = 'CONNECT'"
            ")::int;",
            operation="Inspecting PUBLIC database access",
        )
        if public_value not in {"0", "1"}:
            raise ProvisionError("Unexpected PostgreSQL access-check response")
        public_connect = public_value == "1"
        non_owner_value = cluster.admin_scalar(
            "SELECT count(DISTINCT acl.grantee) FROM pg_database d, "
            "LATERAL aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) acl "
            f"WHERE d.datname = {database_literal} "
            "AND acl.privilege_type = 'CONNECT' "
            "AND acl.grantee <> 0 AND acl.grantee <> d.datdba;",
            operation="Inspecting explicit database CONNECT grantees",
        )
        if not non_owner_value.isdigit():
            raise ProvisionError("Unexpected PostgreSQL grantee-check response")
        non_owner_connect_grantee_count = int(non_owner_value)
    membership_count = "0"
    grantee_count = "0"
    other_database_connect_count = "0"
    if role_row:
        membership_count = cluster.admin_scalar(
            "SELECT count(*) FROM pg_auth_members membership "
            "JOIN pg_roles member_role ON member_role.oid = membership.member "
            f"WHERE member_role.rolname = {role_literal};",
            operation="Inspecting roles granted to tenant",
        )
        grantee_count = cluster.admin_scalar(
            "SELECT count(*) FROM pg_auth_members membership "
            "JOIN pg_roles granted_role ON granted_role.oid = membership.roleid "
            f"WHERE granted_role.rolname = {role_literal};",
            operation="Inspecting tenant role grantees",
        )
        other_database_connect_count = cluster.admin_scalar(
            "SELECT count(*) FROM pg_database d "
            "WHERE d.datallowconn "
            f"AND d.datname <> {database_literal} "
            f"AND has_database_privilege({role_literal}, d.oid, 'CONNECT');",
            operation="Inspecting tenant outbound database access",
        )
        if (
            not membership_count.isdigit()
            or not grantee_count.isdigit()
            or not other_database_connect_count.isdigit()
        ):
            raise ProvisionError("Unexpected PostgreSQL role-access-check response")
    role_parts = role_row.split("|") if role_row else []
    if role_row and (
        len(role_parts) != 8
        or any(part not in {"0", "1"} for part in role_parts[:7])
        or not re.fullmatch(r"-?\d+", role_parts[7])
    ):
        raise ProvisionError("Unexpected PostgreSQL role-check response")
    return DatabaseInspection(
        role_exists=bool(role_row),
        role_can_login=role_parts[0] == "1" if role_parts else False,
        role_is_superuser=role_parts[1] == "1" if role_parts else False,
        role_can_create_db=role_parts[2] == "1" if role_parts else False,
        role_can_create_role=role_parts[3] == "1" if role_parts else False,
        role_can_replicate=role_parts[4] == "1" if role_parts else False,
        role_bypasses_rls=role_parts[5] == "1" if role_parts else False,
        role_inherits=role_parts[6] == "1" if role_parts else False,
        role_membership_count=int(membership_count),
        role_grantee_count=int(grantee_count),
        role_connection_limit=int(role_parts[7]) if role_parts else -1,
        database_exists=bool(database_owner),
        database_owner=database_owner,
        public_can_connect=public_connect,
        non_owner_connect_grantee_count=non_owner_connect_grantee_count,
        other_database_connect_count=int(other_database_connect_count),
    )


def harden_cluster_database_access(cluster: PostgresCluster) -> None:
    """Keep tenant credentials out of the shared maintenance databases."""

    for database in (ADMIN_DATABASE, "template1"):
        cluster.admin_scalar(
            f'REVOKE CONNECT ON DATABASE "{database}" FROM PUBLIC;',
            operation="Restricting maintenance database access",
        )
    remaining = cluster.admin_scalar(
        "SELECT count(*) FROM pg_database d, "
        "LATERAL aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) acl "
        "WHERE d.datname IN ('postgres', 'template1') "
        "AND acl.grantee = 0 AND acl.privilege_type = 'CONNECT';",
        operation="Verifying maintenance database isolation",
    )
    if remaining != "0":
        raise ProvisionError("Maintenance databases still allow PUBLIC CONNECT")


def _scram_verifier_matches(password: str, verifier: str) -> bool:
    """Verify a PostgreSQL SCRAM-SHA-256 verifier without a tenant DB login."""

    if (
        not isinstance(password, str)
        or not isinstance(verifier, str)
        or len(verifier) > 1024
    ):
        return False
    try:
        mechanism, parameters, keys = verifier.split("$", 2)
        iterations_raw, salt_raw = parameters.split(":", 1)
        stored_raw, server_raw = keys.split(":", 1)
        if mechanism != "SCRAM-SHA-256" or not iterations_raw.isdigit():
            return False
        iterations = int(iterations_raw)
        if iterations < 4096 or iterations > 1_000_000:
            return False
        salt = base64.b64decode(salt_raw, validate=True)
        expected_stored = base64.b64decode(stored_raw, validate=True)
        expected_server = base64.b64decode(server_raw, validate=True)
        if (
            not 8 <= len(salt) <= 64
            or len(expected_stored) != 32
            or len(expected_server) != 32
        ):
            return False
        salted = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
        stored_key = hashlib.sha256(client_key).digest()
        server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
        return hmac.compare_digest(stored_key, expected_stored) and hmac.compare_digest(
            server_key, expected_server
        )
    except (ValueError, UnicodeEncodeError):
        return False


def _verify_saved_role_password(
    cluster: PostgresCluster, *, role: str, password: str
) -> None:
    _validate_identifier(role, "role")
    verifier = cluster.admin_scalar(
        "SELECT rolpassword FROM pg_authid "
        f"WHERE rolname = {_literal(role)};",
        operation="Verifying resumed tenant role credential",
    )
    if not _scram_verifier_matches(password, verifier):
        raise ProvisionError("Existing role does not match the saved provisioning state")


def _set_role_password(cluster: PostgresCluster, state: TenantState, password: str) -> None:
    inspection = inspect_database(cluster, role=state.role, database=state.database)
    role_ident = _ident(state.role)
    password_literal = _literal(password)
    if inspection.role_exists:
        if (
            inspection.role_is_superuser
            or inspection.role_can_create_db
            or inspection.role_can_create_role
            or inspection.role_can_replicate
            or inspection.role_bypasses_rls
            or inspection.role_membership_count
            or inspection.role_grantee_count
            or inspection.non_owner_connect_grantee_count
            or inspection.other_database_connect_count
        ):
            raise ProvisionError("Refusing to manage an elevated tenant role")
        if state.status == "provisioning":
            # A provisioning record may be resumed after CREATE ROLE succeeded.
            # Prove that the root-only saved password matches pg_authid before
            # changing any role attribute. This avoids granting tenants CONNECT
            # to the shared postgres maintenance database just for recovery.
            _verify_saved_role_password(
                cluster, role=state.role, password=state.password
            )
            cluster.admin_scalar(
                f"ALTER ROLE {role_ident} WITH LOGIN NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 10;",
                operation="Hardening resumed tenant role",
            )
            return
        statement = (
            f"ALTER ROLE {role_ident} WITH LOGIN PASSWORD {password_literal} "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION "
            "NOBYPASSRLS CONNECTION LIMIT 10;\n"
        )
    else:
        statement = (
            f"CREATE ROLE {role_ident} WITH LOGIN PASSWORD {password_literal} "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION "
            "NOBYPASSRLS CONNECTION LIMIT 10;\n"
        )
    cluster.psql(
        user=cluster.cluster.admin_user,
        password=cluster.cluster.admin_password,
        database=cluster.cluster.admin_database,
        sql=statement,
        secret_sql=True,
        operation="Configuring tenant role",
    )


def _ensure_database(cluster: PostgresCluster, state: TenantState) -> None:
    inspection = inspect_database(cluster, role=state.role, database=state.database)
    if inspection.database_exists:
        if inspection.database_owner != state.role:
            raise ProvisionError("Tenant database exists with a different owner")
    else:
        cluster.admin_scalar(
            f"CREATE DATABASE {_ident(state.database)} OWNER {_ident(state.role)};",
            operation="Creating tenant database",
        )
    cluster.admin_scalar(
        f"REVOKE CONNECT ON DATABASE {_ident(state.database)} FROM PUBLIC;",
        operation="Revoking PUBLIC database access",
    )


def _verify_tenant_login(cluster: PostgresCluster, state: TenantState, password: str) -> None:
    result = cluster.psql(
        user=state.role,
        password=password,
        database=state.database,
        sql="SELECT current_user || '|' || current_database();",
        operation="Verifying tenant login",
    )
    if result != f"{state.role}|{state.database}":
        raise ProvisionError("Tenant login verification returned an unexpected identity")


def ensure_tenant_database(cluster: PostgresCluster, state: TenantState) -> TenantState:
    inspection = inspect_database(cluster, role=state.role, database=state.database)
    if state.status == "ready":
        if not inspection.role_exists or not inspection.database_exists:
            raise ProvisionError("Tenant PostgreSQL objects are missing")
        if (
            not inspection.role_can_login
            or inspection.role_is_superuser
            or inspection.role_can_create_db
            or inspection.role_can_create_role
            or inspection.role_can_replicate
            or inspection.role_bypasses_rls
            or inspection.role_inherits
            or inspection.role_membership_count
            or inspection.role_grantee_count
            or inspection.role_connection_limit != 10
            or inspection.non_owner_connect_grantee_count
            or inspection.other_database_connect_count
        ):
            raise ProvisionError("Tenant role privileges do not match the safe contract")
        if inspection.database_owner != state.role:
            raise ProvisionError("Tenant database owner does not match the saved state")
        if inspection.public_can_connect:
            cluster.admin_scalar(
                f"REVOKE CONNECT ON DATABASE {_ident(state.database)} FROM PUBLIC;",
                operation="Repairing PUBLIC database access",
            )
        _verify_tenant_login(cluster, state, state.password)
        final = inspect_database(cluster, role=state.role, database=state.database)
        if (
            final.public_can_connect
            or final.non_owner_connect_grantee_count
            or final.other_database_connect_count
        ):
            raise ProvisionError("Tenant database CONNECT isolation changed during verification")
        return state

    if state.status == "provisioning":
        _set_role_password(cluster, state, state.password)
        _ensure_database(cluster, state)
        _verify_tenant_login(cluster, state, state.password)
        final = inspect_database(cluster, role=state.role, database=state.database)
        if (
            not final.role_can_login
            or final.role_is_superuser
            or final.role_can_create_db
            or final.role_can_create_role
            or final.role_can_replicate
            or final.role_bypasses_rls
            or final.role_inherits
            or final.role_membership_count
            or final.role_grantee_count
            or final.role_connection_limit != 10
            or final.database_owner != state.role
            or final.public_can_connect
            or final.non_owner_connect_grantee_count
            or final.other_database_connect_count
        ):
            raise ProvisionError("Tenant database did not reach the required security state")
        return replace(state, status="ready")

    if state.status == "rotating":
        if not state.pending_password or not state.pending_source_ref_key:
            raise ProvisionError("Tenant rotation state is incomplete")
        if not inspection.role_exists or not inspection.database_exists:
            raise ProvisionError("Cannot rotate missing tenant PostgreSQL objects")
        if (
            inspection.database_owner != state.role
            or inspection.non_owner_connect_grantee_count
            or inspection.other_database_connect_count
            or inspection.role_is_superuser
            or inspection.role_can_create_db
            or inspection.role_can_create_role
            or inspection.role_can_replicate
            or inspection.role_bypasses_rls
            or inspection.role_membership_count
            or inspection.role_grantee_count
        ):
            raise ProvisionError("Cannot rotate a tenant with unexpected ownership or privileges")
        _set_role_password(cluster, state, state.pending_password)
        _ensure_database(cluster, state)
        _verify_tenant_login(cluster, state, state.pending_password)
        final = inspect_database(cluster, role=state.role, database=state.database)
        if (
            not final.role_can_login
            or final.role_is_superuser
            or final.role_can_create_db
            or final.role_can_create_role
            or final.role_can_replicate
            or final.role_bypasses_rls
            or final.role_inherits
            or final.role_membership_count
            or final.role_grantee_count
            or final.role_connection_limit != 10
            or final.database_owner != state.role
            or final.public_can_connect
            or final.non_owner_connect_grantee_count
            or final.other_database_connect_count
        ):
            raise ProvisionError("Rotated tenant did not reach the required security state")
        return replace(
            state,
            status="ready",
            password=state.pending_password,
            source_ref_key=state.pending_source_ref_key,
            pending_password="",
            pending_source_ref_key="",
        )

    raise ProvisionError("Unknown tenant provisioning state")


def _tenant_state_path(state_dir: Path, client_id: str) -> Path:
    clients_dir = state_dir / "clients"
    _ensure_private_dir(clients_dir, uid=0, gid=0)
    return clients_dir / f"{client_id}.json"


def _validate_tenant_state(state: TenantState) -> None:
    if type(state.version) is not int or state.version != STATE_VERSION:
        raise ProvisionError("Unsupported tenant state version")
    text_fields = (
        state.status,
        state.client_id,
        state.tenant_id,
        state.owner,
        state.hermes_env,
        state.database,
        state.role,
        state.password,
        state.source_ref_key,
        state.pending_password,
        state.pending_source_ref_key,
    )
    if any(not isinstance(value, str) for value in text_fields):
        raise ProvisionError("Tenant state contains a non-text field")
    _validate_client_id(state.client_id)
    _validate_client_id(state.tenant_id)
    if not OWNER_RE.fullmatch(state.owner):
        raise ProvisionError("Tenant state contains an invalid owner")
    if not Path(state.hermes_env).is_absolute() or Path(state.hermes_env).name != ".env":
        raise ProvisionError("Tenant state contains an invalid Hermes .env path")
    _validate_identifier(state.database, "database")
    _validate_identifier(state.role, "role")
    if state.status not in {"provisioning", "ready", "rotating"}:
        raise ProvisionError("Unknown tenant state status")
    for value in (state.password, state.source_ref_key):
        if len(value) < 32 or any(ch.isspace() for ch in value):
            raise ProvisionError("Tenant state contains malformed secret material")
    if state.status == "rotating" and (
        len(state.pending_password) < 32 or len(state.pending_source_ref_key) < 32
    ):
        raise ProvisionError("Tenant rotation state is incomplete")
    if state.status != "rotating" and (
        state.pending_password or state.pending_source_ref_key
    ):
        raise ProvisionError("Tenant state contains unexpected pending secrets")


def load_tenant_state(path: Path) -> TenantState | None:
    if not path.exists():
        return None
    _assert_private_root_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != set(TenantState.__dataclass_fields__):
            raise ValueError
        state = TenantState(**payload)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ProvisionError("Tenant state file is malformed") from exc
    _validate_tenant_state(state)
    return state


def save_tenant_state(path: Path, state: TenantState) -> None:
    _validate_tenant_state(state)
    _atomic_write(
        path,
        json.dumps(asdict(state), sort_keys=True, separators=(",", ":")) + "\n",
        mode=0o600,
        uid=0,
        gid=0,
    )


def _parse_env_assignment(raw: str) -> tuple[str, str] | None:
    candidate = raw.strip()
    if not candidate or candidate.startswith("#"):
        return None
    if candidate.startswith("export "):
        candidate = candidate[7:].lstrip()
    if "=" not in candidate:
        return None
    key, value = candidate.split("=", 1)
    return key.strip(), value.strip()


def _require_owner_identity(owner_entry: pwd.struct_passwd) -> None:
    if os.geteuid() != owner_entry.pw_uid or os.getegid() != owner_entry.pw_gid:
        raise ProvisionError("Hermes filesystem access must run as its Linux owner")


def _validate_hermes_env_path(path: Path, owner_entry: pwd.struct_passwd) -> None:
    expected = Path(owner_entry.pw_dir).absolute() / ".hermes" / ".env"
    if path != expected:
        raise ProvisionError("hermes-env must be the selected owner's ~/.hermes/.env")


def _validate_hermes_env(path: Path, owner_entry: pwd.struct_passwd) -> None:
    _validate_hermes_env_path(path, owner_entry)
    _require_owner_identity(owner_entry)
    _reject_symlink_components(path, label="Hermes .env")
    parent = path.parent
    try:
        parent_info = parent.lstat()
    except OSError as exc:
        raise ProvisionError("Hermes home is missing or inaccessible") from exc
    if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
        raise ProvisionError("Hermes home is missing or symlinked")
    if parent_info.st_uid != owner_entry.pw_uid or parent_info.st_gid != owner_entry.pw_gid:
        raise ProvisionError("Hermes home has an unexpected owner")
    if parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ProvisionError("Hermes home must not be group/world writable")
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ProvisionError("Hermes .env is not a regular file")
        if info.st_uid != owner_entry.pw_uid or info.st_gid != owner_entry.pw_gid:
            raise ProvisionError("Hermes .env has an unexpected owner")
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ProvisionError("Hermes .env must be private")


def _env_values(path: Path, owner_entry: pwd.struct_passwd) -> dict[str, str]:
    _validate_hermes_env(path, owner_entry)
    if not path.exists():
        return {}
    if path.stat().st_size > 1024 * 1024:
        raise ProvisionError("Hermes .env is unexpectedly large")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_assignment(raw)
        if parsed is None:
            continue
        key, value = parsed
        if key in {DATABASE_URL_ENV, SOURCE_REF_KEY_ENV}:
            # Hermes loads this file with python-dotenv, whose duplicate-key
            # semantics are last assignment wins. The preflight must inspect
            # the same effective value before any database mutation.
            values[key] = value
    return values


def _ensure_owner_private_dir(path: Path, owner_entry: pwd.struct_passwd) -> None:
    _require_owner_identity(owner_entry)
    _reject_symlink_components(path, label="owner directory")
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != owner_entry.pw_uid
            or info.st_gid != owner_entry.pw_gid
        ):
            raise ProvisionError("New private owner directory failed validation")
        os.chmod(directory, 0o700)
        _fsync_directory(directory)
        _fsync_directory(directory.parent)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ProvisionError("Expected private owner directory")
    if info.st_uid != owner_entry.pw_uid or info.st_gid != owner_entry.pw_gid:
        raise ProvisionError("Private owner directory has an unexpected owner")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ProvisionError("Private owner directory is group/world writable")
    os.chmod(path, 0o700)


def _backup_env(path: Path, owner_entry: pwd.struct_passwd) -> Path | None:
    _require_owner_identity(owner_entry)
    if not path.exists():
        return None
    backup_root = path.parent / "backups" / "passive-secretary-postgres"
    _ensure_owner_private_dir(backup_root, owner_entry)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{time.time_ns()}"
    backup_dir = backup_root / stamp
    _ensure_owner_private_dir(backup_dir, owner_entry)
    target = backup_dir / ".env"
    _atomic_write(
        target,
        path.read_text(encoding="utf-8"),
        mode=0o600,
        uid=owner_entry.pw_uid,
        gid=owner_entry.pw_gid,
    )
    return target


def merge_hermes_env(
    path: Path,
    *,
    updates: Mapping[str, str],
    owner_entry: pwd.struct_passwd,
    allow_replace: bool,
) -> Path | None:
    _validate_hermes_env(path, owner_entry)
    for key, value in updates.items():
        if key not in {DATABASE_URL_ENV, SOURCE_REF_KEY_ENV}:
            raise ProvisionError("Unexpected Hermes environment key")
        if not value or "\n" in value or "\r" in value:
            raise ProvisionError("Refusing malformed Hermes environment value")

    original = path.read_text(encoding="utf-8") if path.exists() else ""
    output: list[str] = []
    seen: set[str] = set()
    for raw in original.splitlines():
        parsed = _parse_env_assignment(raw)
        key, old_value = parsed if parsed is not None else ("", "")
        if key not in updates:
            output.append(raw)
            continue
        if key in seen:
            if old_value and old_value != updates[key] and not allow_replace:
                raise ProvisionError(
                    "Hermes .env contains conflicting duplicate Passive Secretary secrets"
                )
            continue
        if old_value and old_value != updates[key] and not allow_replace:
            raise ProvisionError(
                "Hermes .env contains different Passive Secretary secrets; use --rotate explicitly"
            )
        output.append(f"{key}={updates[key]}")
        seen.add(key)
    for key, value in updates.items():
        if key not in seen:
            if output and output[-1] != "":
                output.append("")
            output.append(f"{key}={value}")
    rendered = "\n".join(output).rstrip("\n") + "\n"
    if rendered == original:
        info = path.lstat()
        if info.st_uid != owner_entry.pw_uid or info.st_gid != owner_entry.pw_gid:
            raise ProvisionError("Hermes .env owner changed during update")
        os.chmod(path, 0o600)
        return None
    backup = _backup_env(path, owner_entry)
    _atomic_write(
        path,
        rendered,
        mode=0o600,
        uid=owner_entry.pw_uid,
        gid=owner_entry.pw_gid,
    )
    return backup


def _drop_to_owner(owner_entry: pwd.struct_passwd) -> None:
    """Permanently discard root privileges before touching owner paths."""

    os.setgroups([])
    os.setgid(owner_entry.pw_gid)
    os.setuid(owner_entry.pw_uid)
    if (
        os.getuid() != owner_entry.pw_uid
        or os.geteuid() != owner_entry.pw_uid
        or os.getgid() != owner_entry.pw_gid
        or os.getegid() != owner_entry.pw_gid
        or os.getgroups()
    ):
        raise ProvisionError("Could not drop privileges for Hermes filesystem access")
    os.umask(0o077)
    owner_name = getattr(owner_entry, "pw_name", str(owner_entry.pw_uid))
    os.environ.clear()
    os.environ.update(
        {
            "HOME": owner_entry.pw_dir,
            "USER": owner_name,
            "LOGNAME": owner_name,
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
        }
    )


def _write_pipe_payload(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short pipe write")
        view = view[written:]


def _owner_env_action(
    action: str,
    path: Path,
    owner_entry: pwd.struct_passwd,
    *,
    updates: Mapping[str, str] | None = None,
    allow_replace: bool = False,
):
    """Execute all owner-controlled path I/O in a permanently demoted child."""

    if action not in {"read", "merge"}:
        raise ProvisionError("Unknown Hermes environment operation")
    _validate_hermes_env_path(path, owner_entry)
    try:
        read_fd, write_fd = os.pipe()
    except OSError as exc:
        raise ProvisionError("Could not isolate Hermes filesystem operation") from exc
    try:
        pid = os.fork()
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        raise ProvisionError("Could not isolate Hermes filesystem operation") from exc

    if pid == 0:  # pragma: no cover - behavior is observed by the parent tests
        os.close(read_fd)
        exit_code = 1
        try:
            _drop_to_owner(owner_entry)
            if action == "read":
                result = _env_values(path, owner_entry)
            else:
                backup = merge_hermes_env(
                    path,
                    updates=updates or {},
                    owner_entry=owner_entry,
                    allow_replace=allow_replace,
                )
                result = str(backup) if backup is not None else None
            payload = json.dumps(
                {"ok": True, "result": result}, separators=(",", ":")
            ).encode("utf-8")
            exit_code = 0
        except BaseException:
            # Never reflect exception strings: they may contain a secret value
            # parsed from the owner-controlled .env.
            payload = b'{"ok":false}'
        try:
            _write_pipe_payload(write_fd, payload)
        except OSError:
            exit_code = 1
        finally:
            os.close(write_fd)
            os._exit(exit_code)

    os.close(write_fd)
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > 128 * 1024:
                raise ProvisionError("Hermes filesystem response exceeded the safe limit")
            chunks.append(chunk)
    finally:
        os.close(read_fd)
        _, status = os.waitpid(pid, 0)
    try:
        response = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvisionError("Hermes filesystem operation returned an invalid response") from exc
    if (
        not os.WIFEXITED(status)
        or os.WEXITSTATUS(status) != 0
        or not isinstance(response, dict)
        or response.get("ok") is not True
    ):
        raise ProvisionError("Hermes filesystem operation failed safely")
    return response.get("result")


def _env_values_as_owner(
    path: Path, owner_entry: pwd.struct_passwd
) -> dict[str, str]:
    result = _owner_env_action("read", path, owner_entry)
    if not isinstance(result, dict):
        raise ProvisionError("Hermes filesystem read returned an invalid response")
    allowed = {DATABASE_URL_ENV, SOURCE_REF_KEY_ENV}
    if any(
        key not in allowed or not isinstance(value, str)
        for key, value in result.items()
    ):
        raise ProvisionError("Hermes filesystem read returned unexpected data")
    return result


def _merge_hermes_env_as_owner(
    path: Path,
    *,
    updates: Mapping[str, str],
    owner_entry: pwd.struct_passwd,
    allow_replace: bool,
) -> Path | None:
    result = _owner_env_action(
        "merge",
        path,
        owner_entry,
        updates=updates,
        allow_replace=allow_replace,
    )
    if result is None:
        return None
    if not isinstance(result, str):
        raise ProvisionError("Hermes filesystem merge returned an invalid response")
    backup = Path(result)
    expected_root = path.parent / "backups" / "passive-secretary-postgres"
    if expected_root not in backup.parents:
        raise ProvisionError("Hermes filesystem merge returned an unsafe backup path")
    return backup


def tenant_dsn(state: TenantState, *, port: int) -> str:
    return (
        "postgresql://"
        f"{quote(state.role, safe='')}:{quote(state.password, safe='')}"
        f"@127.0.0.1:{port}/{quote(state.database, safe='')}?sslmode=disable"
    )


def _new_tenant_state(
    *,
    client_id: str,
    tenant_id: str,
    owner: str,
    hermes_env: Path,
    database: str,
    role: str,
) -> TenantState:
    return TenantState(
        version=STATE_VERSION,
        status="provisioning",
        client_id=client_id,
        tenant_id=tenant_id,
        owner=owner,
        hermes_env=str(hermes_env),
        database=database,
        role=role,
        password=secrets.token_urlsafe(48),
        source_ref_key=secrets.token_urlsafe(48),
    )


def provision(args: argparse.Namespace, *, runner: Callable[..., str] | None = None) -> None:
    if os.geteuid() != 0:
        raise ProvisionError("Provisioning must run as root")
    _assert_root_owned_runtime()
    client_id = _validate_client_id(args.client_id)
    state_dir = _validate_state_dir_argument(Path(args.state_dir))
    # The lock covers every state read/write, credential generation, database
    # mutation, .env merge, and explicit rotation commit below.
    with _provision_lock(state_dir):
        _provision_locked(
            args,
            runner=runner,
            client_id=client_id,
            state_dir=state_dir,
        )


def _provision_locked(
    args: argparse.Namespace,
    *,
    runner: Callable[..., str] | None,
    client_id: str,
    state_dir: Path,
) -> None:
    try:
        owner_entry = pwd.getpwnam(args.owner)
    except KeyError as exc:
        raise ProvisionError("Unknown Hermes owner") from exc
    if owner_entry.pw_uid == 0 or owner_entry.pw_gid == 0:
        raise ProvisionError("Hermes owner must be an unprivileged Linux account")
    hermes_env = Path(args.hermes_env)
    if not hermes_env.is_absolute():
        raise ProvisionError("hermes-env must be an absolute path")
    _validate_hermes_env_path(hermes_env, owner_entry)
    docker_bin = _validate_docker_binary(Path(args.docker_bin))

    cluster_config, cluster_env, _ = load_or_create_cluster_config(
        state_dir,
        requested_port=args.port,
    )
    state_path = _tenant_state_path(state_dir, client_id)
    existing = load_tenant_state(state_path)

    if existing:
        if args.owner != existing.owner or str(Path(args.hermes_env)) != existing.hermes_env:
            raise ProvisionError("Requested Hermes owner or .env differs from saved tenant state")
        tenant_id = args.tenant_id or existing.tenant_id
        database = args.database or existing.database
        role = args.role or existing.role
        if (
            tenant_id != existing.tenant_id
            or database != existing.database
            or role != existing.role
        ):
            raise ProvisionError("Requested tenant identity differs from saved state")
        state = existing
    else:
        tenant_id = args.tenant_id or client_id
        database = args.database or f"hermes_{client_id.replace('-', '_')}"
        role = args.role or f"hermes_{client_id.replace('-', '_')}"
        _validate_client_id(tenant_id)
        _validate_identifier(database, "database")
        _validate_identifier(role, "role")
        current = _env_values_as_owner(hermes_env, owner_entry)
        if any(current.get(key) for key in (DATABASE_URL_ENV, SOURCE_REF_KEY_ENV)):
            raise ProvisionError(
                "Hermes .env already contains Passive Secretary secrets but no tenant state"
            )
        state = _new_tenant_state(
            client_id=client_id,
            tenant_id=tenant_id,
            owner=args.owner,
            hermes_env=hermes_env,
            database=database,
            role=role,
        )

    _validate_client_id(tenant_id)
    _validate_identifier(database, "database")
    _validate_identifier(role, "role")
    if state.status == "ready" and args.rotate:
        state = replace(
            state,
            status="rotating",
            pending_password=secrets.token_urlsafe(48),
            pending_source_ref_key=secrets.token_urlsafe(48),
        )
        save_tenant_state(state_path, state)
    elif state.status == "rotating":
        # Resume a previously explicit rotation after a crash. Do not generate a
        # second set of credentials.
        pass
    elif args.rotate and state.status == "provisioning":
        raise ProvisionError("Cannot start rotation while initial provisioning is incomplete")

    command_runner = runner or CommandRunner()
    postgres = PostgresCluster(
        cluster=cluster_config,
        cluster_env=cluster_env,
        docker_bin=docker_bin,
        runner=command_runner,
    )
    postgres.up()
    postgres.wait_until_healthy(timeout=args.health_timeout)
    harden_cluster_database_access(postgres)
    if existing is None:
        collision = inspect_database(postgres, role=state.role, database=state.database)
        if collision.role_exists or collision.database_exists:
            raise ProvisionError(
                "Requested role or database already exists without matching tenant state"
            )
        # Persist generated credentials before the first DB mutation so an
        # interrupted initial run can resume without silently rotating them.
        save_tenant_state(state_path, state)
    ready_state = ensure_tenant_database(postgres, state)

    updates = {
        DATABASE_URL_ENV: tenant_dsn(ready_state, port=cluster_config.port),
        SOURCE_REF_KEY_ENV: ready_state.source_ref_key,
    }
    current = _env_values_as_owner(hermes_env, owner_entry)
    different = any(
        current.get(key) and current.get(key) != value for key, value in updates.items()
    )
    if different and not (args.rotate or state.status == "rotating"):
        raise ProvisionError(
            "Hermes .env differs from root-only tenant state; use --rotate explicitly"
        )
    _merge_hermes_env_as_owner(
        hermes_env,
        updates=updates,
        owner_entry=owner_entry,
        allow_replace=args.rotate or state.status == "rotating",
    )
    if ready_state != state:
        # Commit ready only after the atomic Hermes .env update. Until then the
        # provisioning/rotating record remains resumable with the same secrets.
        save_tenant_state(state_path, ready_state)

    # Deliberately print only non-secret identifiers and status.
    print("provisioned=true")
    print(f"client={client_id}")
    print(f"database={ready_state.database}")
    print(f"role={ready_state.role}")
    print(f"hermes_env={hermes_env}")
    print(f"rotated={'true' if state.status == 'rotating' else 'false'}")
    print("secrets_printed=false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provision", choices=("provision",))
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--tenant-id")
    parser.add_argument("--database")
    parser.add_argument("--role")
    parser.add_argument("--owner", required=True, help="Linux owner of the Hermes profile")
    parser.add_argument("--hermes-env", required=True, help="Exact owner ~/.hermes/.env path")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--port", type=_validate_port, default=None)
    parser.add_argument("--health-timeout", type=float, default=120.0)
    parser.add_argument("--docker-bin", default=str(DEFAULT_DOCKER_BIN))
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="Explicitly rotate this existing tenant's DB password and source-ref key",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        system_python = SYSTEM_PYTHON.resolve(strict=True)
        active_python = Path(sys.executable).resolve(strict=True)
    except OSError:
        system_python = Path()
        active_python = Path("untrusted")
    if not sys.flags.isolated or active_python != system_python:
        print(
            "ERROR: run this root-owned provisioner with /usr/bin/python3 -I",
            file=sys.stderr,
        )
        return 1
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.health_timeout < 5 or args.health_timeout > 900:
        parser.error("--health-timeout must be between 5 and 900 seconds")
    try:
        provision(args)
    except ProvisionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
