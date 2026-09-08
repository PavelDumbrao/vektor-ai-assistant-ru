from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin"


def _load_plugin():
    for name in list(sys.modules):
        if name == "focus_assistant" or name.startswith("focus_assistant."):
            sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "focus_assistant",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["focus_assistant"] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    module = _load_plugin()
    module.ledger.initialize()
    return module


def test_candidate_dedup_and_owner_confirmation(plugin):
    first = plugin.ledger.save_item(
        {
            "title": "Отправить предложение клиенту",
            "kind": "commitment",
            "state": "candidate",
            "source_type": "telegram",
            "source_ref": "telegram:test:1",
            "idempotency_key": "telegram:test:1",
        }
    )
    second = plugin.ledger.save_item(
        {
            "title": "Дубликат",
            "kind": "commitment",
            "state": "candidate",
            "idempotency_key": "telegram:test:1",
        }
    )
    assert first["created"] is True
    assert second["deduplicated"] is True
    assert first["item"]["id"] == second["item"]["id"]

    with pytest.raises(ValueError, match="owner_confirmed"):
        plugin.ledger.update_item({"item_id": first["item"]["id"], "state": "active"})
    updated = plugin.ledger.update_item(
        {"item_id": first["item"]["id"], "state": "active", "owner_confirmed": True}
    )
    assert updated["item"]["state"] == "active"
    assert updated["item"]["owner_confirmed"] is True


def test_focus_ledger_does_not_create_kanban_board(plugin):
    home = Path(plugin.ledger.get_hermes_home())
    status = plugin.ledger.initialize()
    assert status["backend"] == "sqlite"
    assert (home / "focus/ledger.db").is_file()
    assert not (home / "kanban/boards/focus/kanban.db").exists()


def test_legacy_kanban_items_migrate_without_generated_children(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    board = home / "kanban/boards/focus"
    board.mkdir(parents=True)
    old = sqlite3.connect(board / "kanban.db")
    old.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, body TEXT, priority INTEGER,
            idempotency_key TEXT, created_by TEXT
        );
        CREATE TABLE focus_items (
            task_id TEXT PRIMARY KEY, kind TEXT, state TEXT, project TEXT,
            goal_id TEXT, next_action TEXT, due_at TEXT, follow_up_at TEXT,
            waiting_on TEXT, source_type TEXT, source_ref TEXT, source_date TEXT,
            confidence TEXT, owner_confirmed INTEGER, snoozed_until TEXT,
            created_at TEXT, updated_at TEXT, completed_at TEXT
        );
        """
    )
    old.execute(
        "INSERT INTO tasks VALUES(?,?,?,?,?,?)",
        ("t_original", "Original", None, 5, "source:1", "fathom_watch"),
    )
    old.execute(
        "INSERT INTO focus_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "t_original", "commitment", "candidate", None, None, None,
            None, None, None, "fathom", "fathom:1", "2026-09-02",
            "high", 0, None, "2026-09-02T10:00:00Z",
            "2026-09-02T10:00:00Z", None,
        ),
    )
    old.execute(
        "INSERT INTO tasks VALUES(?,?,?,?,?,?)",
        ("t_generated", "Generated child", None, 0, None, "auto-decomposer"),
    )
    old.commit()
    old.close()

    monkeypatch.setenv("HERMES_HOME", str(home))
    module = _load_plugin()
    status = module.ledger.initialize()
    listed = module.ledger.list_items({"include_done": True})

    assert status["migrated_from_kanban"] == 1
    assert [item["id"] for item in listed["items"]] == ["t_original"]
    assert listed["backend"] == "sqlite"


def test_attention_suppresses_unchanged_repeat(plugin):
    plugin.ledger.save_item(
        {
            "title": "Главный результат дня",
            "state": "active",
            "owner_confirmed": True,
            "priority": 10,
        }
    )
    first = plugin.ledger.attention({"cadence": "morning", "now": "2026-09-02T09:00:00+03:00"})
    second = plugin.ledger.attention({"cadence": "morning", "now": "2026-09-02T10:00:00+03:00"})
    assert first["count"] == 1
    assert second["count"] == 0
    assert second["suppressed_unchanged"] == 1


def test_goals_require_explicit_confirmation(plugin):
    with pytest.raises(ValueError, match="confirmed_by_owner"):
        plugin.ledger.goals({"action": "upsert", "title": "Рост продаж"})
    result = plugin.ledger.goals(
        {
            "action": "upsert",
            "title": "Рост продаж",
            "metric": "Подтверждённые сделки",
            "confirmed_by_owner": True,
        }
    )
    assert result["status"] == "active"
    assert len(result["goals"]) == 1


def test_maton_policy_allows_reads_and_guards_writes(plugin):
    assert plugin._maton_write_guard(
        tool_name="mcp__maton__run_action",
        args={"id": "google-mail.message.list", "args": {}},
    ) is None
    approval = plugin._maton_write_guard(
        tool_name="mcp__maton__run_action",
        args={"id": "google-mail.draft.create", "args": {"to": "a@example.com", "subject": "Hi"}},
        session_id="s1",
        tool_call_id="tc1",
    )
    assert approval["action"] == "block"
    assert "assistant_mail" in approval["message"]
    blocked = plugin._maton_write_guard(
        tool_name="mcp__maton__run_action",
        args={"id": "google-drive.file.delete", "args": {"fileId": "x"}},
    )
    assert blocked["action"] == "block"


def test_plugin_registers_focus_and_fathom_tools(plugin, monkeypatch):
    import hermes_cli.config
    monkeypatch.setattr(hermes_cli.config, "load_config", lambda: {"platforms": {"telegram": {"home_channel": {"chat_id": "1"}}}})
    class Context:
        def __init__(self):
            self.tools = []
            self.hooks = []

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

        def register_hook(self, name, callback):
            self.hooks.append((name, callback))

    context = Context()
    plugin.register(context)
    names = {entry["name"] for entry in context.tools}
    assert {
        "focus_task_list",
        "focus_task_save",
        "focus_task_update",
        "focus_attention",
        "focus_goals",
        "fathom_recent_meetings",
        "fathom_meeting_summary",
        "fathom_meeting_transcript",
        "integration_connection_status",
        "focus_capture",
        "focus_commitments",
        "assistant_brief",
        "assistant_meeting",
        "assistant_voice_intake",
        "assistant_mail",
        "assistant_project",
        "assistant_weekly",
    } == names
    assert [name for name, _callback in context.hooks] == [
        "pre_llm_call",
        "pre_tool_call",
        "pre_tool_call",
        "transform_tool_result",
    ]
    observe = context.hooks[0][1]
    maton = context.hooks[2][1]
    observe(session_id="scheduled", turn_id="1", platform="cron")
    for name, args in [
        ("mcp__maton__run_action", {"id": "google-calendar.event.create"}),
        ("mcp__maton__run_action", {"id": "google-mail.message.send"}),
        ("mcp__maton__create_connection", {}),
        ("mcp__maton__run_action", {"id": "unknown.write"}),
    ]:
        assert maton(tool_name=name, args=args, session_id="scheduled")["action"] == "block"
    assert maton(tool_name="mcp__maton__run_action", args={"id": "google-calendar.event.list"}, session_id="scheduled") is None
    assert maton(tool_name="mcp__maton__get_action", args={}, session_id="scheduled") is None


def test_fathom_transcript_is_bounded(plugin, monkeypatch):
    monkeypatch.setattr(
        plugin.fathom,
        "get_transcript",
        lambda _rid: [
            {"timestamp": f"00:00:{i:02d}", "speaker": {"display_name": "Pavel"}, "text": f"line {i}"}
            for i in range(5)
        ],
    )
    result = plugin.fathom.transcript({"recording_id": 123, "max_segments": 2})
    assert result["count"] == 2
    assert result["truncated"] is True
    assert all("matched_calendar_invitee_email" not in row for row in result["segments"])


def test_maton_connection_result_is_sanitized(plugin):
    raw = json.dumps(
        {
            "result": json.dumps(
                {
                    "connections": [
                        {
                            "connection_id": "id-1",
                            "app": "google-mail",
                            "account": "private@example.com",
                            "method": "OAUTH2",
                            "status": "ACTIVE",
                            "url": "https://connect.example/?session_token=secret",
                        }
                    ],
                    "cursor": None,
                    "total": 1,
                }
            )
        }
    )
    safe = plugin.integrations.sanitize_connection_tool_result(
        tool_name="mcp__maton__list_connections",
        result=raw,
    )
    assert "private@example.com" not in safe
    assert "session_token" not in safe
    assert "google-mail" in safe
    assert "ACTIVE" in safe


def test_installer_enables_plugin_and_stages_watcher(tmp_path):
    install_path = PLUGIN_DIR.parent / "install.py"
    spec = importlib.util.spec_from_file_location("focus_assistant_installer", install_path)
    installer = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(installer)

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - passive-secretary\n",
        encoding="utf-8",
    )
    result = installer.install(home)

    assert (home / "plugins/focus_assistant/plugin.yaml").is_file()
    assert (home / "scripts/fathom_watch.py").is_file()
    assert (home / "scripts/fathom_watch.py").stat().st_mode & 0o777 == 0o700
    assert "focus_assistant" in (home / "config.yaml").read_text(encoding="utf-8")
    assert Path(result["backup"]).is_dir()
