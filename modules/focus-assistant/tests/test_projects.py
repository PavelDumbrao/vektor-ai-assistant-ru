import pytest

pytest_plugins = ["test_focus_assistant"]


def create(plugin):
    return plugin.projects.run(
        {
            "action": "upsert",
            "project_id": "demo-project",
            "expected_version": 0,
            "name": "Тестовый проект",
            "objective": "Проверка постоянной памяти",
        }
    )


def test_project_fact_survives_fresh_connection(plugin):
    create(plugin)
    plugin.projects.run(
        {
            "action": "fact",
            "project_id": "demo-project",
            "expected_version": 1,
            "key": "decision",
            "text": "Черновики согласовывает владелец",
            "source_ref": "owner:test:1",
        }
    )
    result = plugin.projects.run({"action": "get", "project_id": "demo-project"})
    assert result["project"]["version"] == 2
    assert result["project"]["facts"]["decision"]["source_ref"] == "owner:test:1"


def test_stale_project_update_does_not_overwrite(plugin):
    create(plugin)
    with pytest.raises(ValueError, match="version_changed"):
        plugin.projects.run(
            {
                "action": "upsert",
                "project_id": "demo-project",
                "expected_version": 0,
                "name": "Старое",
            }
        )
    assert (
        plugin.projects.run({"action": "get", "project_id": "demo-project"})["project"][
            "name"
        ]
        == "Тестовый проект"
    )


def test_project_revisions_and_explicit_link(plugin):
    create(plugin)
    item = plugin.ledger.save_item({"title": "Кандидат"})["item"]
    plugin.projects.run(
        {"action": "link_item", "project_id": "demo-project", "item_id": item["id"]}
    )
    assert (
        len(
            plugin.projects.run({"action": "get", "project_id": "demo-project"})[
                "items"
            ]
        )
        == 1
    )
    plugin.projects.run(
        {
            "action": "upsert",
            "project_id": "demo-project",
            "expected_version": 1,
            "next_action": "Обсудить",
        }
    )
    conn = plugin.projects.connect()
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM assistant_project_revisions").fetchone()[
                0
            ]
            == 1
        )
    finally:
        conn.close()


def test_project_mutations_require_owner_approval(plugin):
    gate = plugin.OwnerGate("1")
    gate.observe(session_id="c", turn_id="t", platform="cron")
    assert (
        gate.guard(
            tool_name="assistant_project",
            args={"action": "upsert"},
            session_id="c",
            turn_id="t",
        )["action"]
        == "block"
    )
    assert (
        gate.guard(
            tool_name="assistant_project", args={"action": "list"}, session_id="c"
        )
        is None
    )
