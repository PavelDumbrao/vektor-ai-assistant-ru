from __future__ import annotations

import json

import pytest

pytest_plugins = ["test_focus_assistant"]


def test_capture_is_evidenced_candidate_and_deduplicates(plugin):
    source = {"kind": "owner", "source_ref": "owner:test:1", "text": "Я отправлю предложение 2026-09-10. От Анны жду ответ."}
    args = {"items": [{"title": "Отправить предложение", "quote": "Я отправлю предложение 2026-09-10.", "kind": "commitment", "due_at": "2026-09-10", "due_quote": "2026-09-10"}]}
    first = plugin.intake.capture(args, source)
    second = plugin.intake.capture(args, source)
    assert first["results"][0]["item"]["state"] == "candidate"
    assert first["results"][0]["item"]["owner_confirmed"] is False
    assert second["results"][0]["deduplicated"] is True
    rows = plugin.intake.commitments({})["items"]
    assert len(rows) == 1
    assert rows[0]["evidence"]["quote"] in source["text"]


def test_capture_rejects_invented_quote_or_date_or_assignee(plugin):
    source = {"kind": "owner", "source_ref": "owner:test:1", "text": "Подготовить предложение."}
    item = {"title": "Подготовить предложение", "quote": source["text"]}
    for changed in ({"quote": "Павел обещал завтра."}, {"due_at": "2026-09-05"}, {"waiting_on": "Анна"}):
        with pytest.raises(ValueError):
            plugin.intake.capture({"items": [item | changed]}, source)
    assert plugin.ledger.list_items({})["count"] == 0


def test_capture_dry_run_and_unknown_fields_do_not_save(plugin):
    source = {"kind": "owner", "source_ref": "owner:test:1", "text": "Запиши идею для статьи."}
    result = plugin.intake.capture({"items": [{"title": "Идея статьи", "quote": source["text"], "kind": "idea"}], "dry_run": True}, source)
    assert result["stored"] is False
    assert plugin.ledger.list_items({})["count"] == 0
    with pytest.raises(ValueError):
        plugin.intake.capture({"items": [{"title": "x", "quote": source["text"], "owner_confirmed": True}]}, source)


def test_owner_gate_rejects_group_and_internal_activation(plugin):
    gate = plugin.OwnerGate("1")
    gate.observe(session_id="s", turn_id="t", sender_id="1", platform="telegram", chat_type="group", raw_user_message="Сохрани", is_internal_event=False)
    assert gate.guard(tool_name="focus_task_list", args={}, session_id="s")["action"] == "block"
    gate.observe(session_id="s", turn_id="t", is_internal_event=True)
    args = {"title": "Новая цель", "action": "upsert", "confirmed_by_owner": True}
    assert gate.guard(tool_name="focus_goals", args=args, session_id="s", turn_id="t")["action"] == "block"
    assert gate.guard(tool_name="focus_task_list", args={}, session_id="s") is None


def test_owner_and_cron_receive_human_telegram_style(plugin):
    gate = plugin.OwnerGate("1")
    owner = gate.observe(session_id="owner", turn_id="1", sender_id="1", platform="telegram", chat_type="dm",
                         raw_user_message="Что сегодня?", user_message="Что сегодня?", is_internal_event=False)
    cron = gate.observe(session_id="cron", turn_id="1", platform="cron")
    stranger = gate.observe(session_id="stranger", turn_id="1", sender_id="2", platform="telegram", chat_type="dm",
                            raw_user_message="Привет", is_internal_event=False)
    for value in (owner, cron):
        assert value and "MarkdownV2" in value["context"]
        assert "JSON" in value["context"] and "1-2" in value["context"]
    assert stranger is None


def test_owner_mutation_requires_matching_one_use_permit(plugin):
    gate = plugin.OwnerGate("1")
    gate.observe(session_id="s", turn_id="t", sender_id="1", platform="telegram", chat_type="dm", raw_user_message="Подтверждаю задачу", is_internal_event=False)
    args = {"item_id": "f_test", "state": "active", "owner_confirmed": True}
    with pytest.raises(ValueError, match="single_use"):
        gate.authorize("focus_task_update", args, "s")
    assert gate.guard(tool_name="focus_task_update", args=args, session_id="s", turn_id="t", tool_call_id="1")["action"] == "approve"
    gate.authorize("focus_task_update", args, "s")
    with pytest.raises(ValueError, match="single_use"):
        gate.authorize("focus_task_update", args, "s")


def test_source_must_be_seen_in_current_turn(plugin):
    gate = plugin.OwnerGate("1")
    gate.observe(session_id="s", turn_id="t", sender_id="1", platform="telegram", chat_type="dm", raw_user_message="Мой голосовой текст", is_internal_event=False)
    assert gate.source("s", "current_owner")["text"] == "Мой голосовой текст"
    with pytest.raises(ValueError):
        gate.source("s", "fathom:5")
    gate.remember("s", "fathom:5", "Текст встречи", "fathom")
    assert gate.source("s", "fathom:5")["kind"] == "fathom"
    gate.observe(session_id="s", turn_id="t2", sender_id="1", platform="telegram", chat_type="dm", raw_user_message="Новый запрос", is_internal_event=False)
    with pytest.raises(ValueError):
        gate.source("s", "fathom:5")


def test_owner_confirmation_cannot_be_string(plugin):
    with pytest.raises(ValueError, match="boolean"):
        plugin.ledger.save_item({"title": "x", "owner_confirmed": "false"})


def test_native_cron_context_can_read_but_never_activate(plugin):
    gate = plugin.OwnerGate("1")
    gate.observe(session_id="cron-run", turn_id="t", platform="cron")
    assert gate.guard(tool_name="focus_task_list", args={}, session_id="cron-run") is None
    assert gate.guard(tool_name="focus_task_update", args={"state": "done"}, session_id="cron-run", turn_id="t")["action"] == "block"


def test_batch_validation_happens_before_any_write(plugin):
    source = {"kind": "owner", "source_ref": "owner:test:batch", "text": "Первое дело. Второе дело."}
    items = [{"title": "Первое", "quote": "Первое дело."}, {"title": "Второе", "quote": "Второе дело.", "priority": "bad"}]
    with pytest.raises(ValueError):
        plugin.intake.capture({"items": items}, source)
    assert plugin.ledger.list_items({})["count"] == 0


def test_cron_cannot_edit_existing_confirmed_item_by_omitting_state(plugin):
    gate = plugin.OwnerGate("1")
    gate.observe(session_id="cron", turn_id="t", platform="cron")
    for tool in ("focus_task_save", "focus_task_update"):
        result = gate.guard(tool_name=tool, args={"item_id": "f_existing", "title": "Подмена"}, session_id="cron", turn_id="t")
        assert result["action"] == "block"


def test_fetched_fathom_result_exposes_capture_reference(plugin):
    gate = plugin.OwnerGate("1")
    gate.observe(session_id="s", turn_id="t", sender_id="1", platform="telegram", chat_type="dm", raw_user_message="Прочитай встречу", is_internal_event=False)
    handler = gate.wrap("fathom_meeting_summary", lambda args: {"recording_id": 9, "summary": "Павел подготовит предложение."})
    result = json.loads(handler({}, session_id="s"))
    assert result["capture_source_ref"] == "fathom:9"
    assert gate.source("s", "fathom:9")["text"] == result["summary"]
