import json
from types import SimpleNamespace

pytest_plugins = ["test_focus_assistant"]


def test_prepare_does_not_mix_unselected_projects(plugin):
    plugin.ledger.save_item({"title": "Чужой проект в личном реестре", "project": "Other"})
    client = SimpleNamespace(calendar_event=lambda eid: {"id": eid, "status": "confirmed", "title": "Встреча"})
    result = plugin.meetings.meeting({"mode": "prepare", "event_id": "event1"}, client)
    assert result["related_items"] == []
    assert result["project_match"] == "not_selected"
    assert result["external_writes_performed"] is False


def test_cancelled_meeting_is_not_prepared(plugin):
    client = SimpleNamespace(calendar_event=lambda eid: {"id": eid, "status": "cancelled"})
    result = plugin.meetings.meeting({"mode": "prepare", "event_id": "event1"}, client)
    assert result["preparation_needed"] is False


def test_review_preserves_transcript_evidence_and_does_not_create_tasks(plugin, monkeypatch):
    monkeypatch.setattr(plugin.fathom, "get_summary", lambda rid: "На встрече обсуждали подготовку предложения.")
    monkeypatch.setattr(plugin.fathom, "get_transcript", lambda rid: [
        {"timestamp": "00:03:10", "speaker": {"display_name": "Павел"}, "text": "Я подготовлю предложение."}])
    gate = plugin.OwnerGate("1")
    gate.observe(session_id="s", turn_id="t", sender_id="1", platform="telegram", chat_type="dm", raw_user_message="Разбери встречу", is_internal_event=False)
    result = json.loads(gate.wrap("assistant_meeting", plugin.meetings.meeting)({"mode": "review", "recording_id": 7}, session_id="s"))
    assert "00:03:10" in result["transcript_excerpt"]
    assert gate.source("s", "fathom:7")["text"] == result["transcript_excerpt"]
    assert plugin.ledger.list_items({})["count"] == 0


def test_review_exposes_truncation(plugin, monkeypatch):
    monkeypatch.setattr(plugin.fathom, "get_summary", lambda rid: "Кратко")
    monkeypatch.setattr(plugin.fathom, "get_transcript", lambda rid: [
        {"timestamp": "00:00", "speaker": {}, "text": "x" * 1000} for _ in range(70)])
    result = plugin.meetings.meeting({"mode": "review", "recording_id": 7})
    assert result["transcript_truncated"] is True
    assert len(result["transcript_excerpt"]) == 14000
