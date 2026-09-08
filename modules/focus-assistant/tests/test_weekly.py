from types import SimpleNamespace

pytest_plugins = ["test_focus_assistant"]


def test_weekly_separates_candidates_and_does_not_mutate_states(plugin):
    plugin.ledger.save_item(
        {
            "title": "Подтверждено",
            "owner_confirmed": True,
            "state": "active",
            "priority": 10,
        }
    )
    plugin.ledger.save_item({"title": "Кандидат из встречи", "state": "candidate"})
    client = SimpleNamespace(
        calendar=lambda **kwargs: {
            "available": True,
            "events": [],
            "conflicts": [],
            "truncated": False,
        }
    )
    before = plugin.ledger.list_items({})
    report = plugin.weekly.review({}, client)
    after = plugin.ledger.list_items({})
    assert before == after
    assert len(report["active"]) == 1 and len(report["candidates"]) == 1
    assert report["task_states_changed"] is False
    assert report["external_writes_performed"] is False
    assert report["done"] == []


def test_weekly_missing_calendar_is_not_zero_events(plugin):
    def fail(**kwargs):
        raise plugin.workspace.WorkspaceError("workspace_http_403")

    report = plugin.weekly.review({}, SimpleNamespace(calendar=fail))
    assert report["previous_calendar"]["available"] is False
    assert "данные не удалось проверить" in report["text"]


def test_weekly_proposes_at_most_three_confirmed_items(plugin):
    for index in range(6):
        plugin.ledger.save_item(
            {"title": f"Задача {index}", "state": "active", "owner_confirmed": True}
        )
    client = SimpleNamespace(
        calendar=lambda **kwargs: {
            "available": True,
            "events": [],
            "conflicts": [],
            "truncated": False,
        }
    )
    report = plugin.weekly.review({}, client)
    assert len(report["proposed_next_week"]) == 3


def test_historical_week_does_not_include_future_completion(plugin):
    item = plugin.ledger.save_item({"title": "Later", "state": "active", "owner_confirmed": True})["item"]
    plugin.ledger.update_item({"item_id": item["id"], "state": "done", "owner_confirmed": True})
    client = SimpleNamespace(calendar=lambda **kwargs: {"available": True, "events": [], "conflicts": []})
    report = plugin.weekly.review({"day": "2020-01-01"}, client)
    assert report["done"] == []
