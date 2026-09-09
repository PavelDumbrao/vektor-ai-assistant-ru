"""Behavioral coverage for file-tool checkpoint path resolution."""

from types import SimpleNamespace

from agent.tool_executor import _ensure_file_checkpoint
from tools.checkpoint_manager import CheckpointManager


def test_relative_file_checkpoint_uses_task_workspace(tmp_path, monkeypatch):
    """Checkpoint lookup must use the same cwd as a relative file mutation."""
    process_cwd = tmp_path / "opt" / "hermes"
    workspace_cwd = tmp_path / "opt" / "data" / "workspace"
    process_cwd.mkdir(parents=True)
    workspace_cwd.mkdir(parents=True)

    # Both directories contain content so checkpointing the wrong one would
    # still succeed and remain observable as the regression did in Docker.
    (process_cwd / "pyproject.toml").write_text("[project]\nname = 'hermes'\n")
    (workspace_cwd / "pyproject.toml").write_text("[project]\nname = 'workspace'\n")
    (workspace_cwd / "existing.txt").write_text("before\n")

    monkeypatch.chdir(process_cwd)
    monkeypatch.setenv("TERMINAL_CWD", str(workspace_cwd))
    monkeypatch.setattr(
        "tools.checkpoint_manager.CHECKPOINT_BASE",
        tmp_path / "checkpoints",
    )

    manager = CheckpointManager(enabled=True)
    agent = SimpleNamespace(_checkpoint_mgr=manager)

    _ensure_file_checkpoint(
        agent,
        "write_file",
        {"path": "test_permissions2.txt"},
        "gateway-session",
    )

    assert manager.list_checkpoints(str(workspace_cwd))
    assert manager.list_checkpoints(str(process_cwd)) == []


def test_concurrent_timeout_uses_registered_tool_override(monkeypatch):
    from agent import tool_executor
    from types import SimpleNamespace
    from tools.registry import registry

    monkeypatch.delenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", raising=False)
    monkeypatch.setattr(
        registry, "get_entry",
        lambda name: SimpleNamespace(timeout_seconds=1200) if name == "video_editor_master" else None,
    )
    assert tool_executor._resolve_concurrent_tool_timeout(["video_editor_master"]) == 1200.0
    assert tool_executor._resolve_concurrent_tool_timeout(["ordinary_tool"]) == 420.0


def test_concurrent_timeout_operator_env_wins(monkeypatch):
    from agent import tool_executor
    monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "300")
    assert tool_executor._resolve_concurrent_tool_timeout(["video_editor_master"]) == 300.0
