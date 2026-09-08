from __future__ import annotations

import hashlib
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

pytest_plugins = ["test_focus_assistant"]


def api(plugin, monkeypatch, handler, identity="pavel@example.test"):
    monkeypatch.setenv("MCP_MATON_API_KEY", "test-only")
    settings = {"version": 1, "timezone": "Europe/Moscow", "calendar_id": "primary", "connections": {
        app: {"connection_id": "connection-" + app, "identity_sha256": hashlib.sha256(identity.encode()).hexdigest()}
        for app in ("google-calendar", "google-mail")}}
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return plugin.brief.workspace.Workspace(settings, client), client


def test_bound_calendar_uses_exact_account_and_finds_overlap(plugin, monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path.startswith("/connections/"):
            return httpx.Response(200, json={"connection": {"app": "google-calendar", "status": "ACTIVE", "metadata": {"email": "pavel@example.test"}}})
        return httpx.Response(200, json={"items": [
            {"id": "one", "summary": "Первая", "start": {"dateTime": "2026-09-04T10:00:00+03:00"}, "end": {"dateTime": "2026-09-04T11:00:00+03:00"}},
            {"id": "two", "summary": "Вторая", "start": {"dateTime": "2026-09-04T10:30:00+03:00"}, "end": {"dateTime": "2026-09-04T11:30:00+03:00"}},
            {"id": "cancelled", "status": "cancelled"}]})
    gateway, client = api(plugin, monkeypatch, handle)
    try:
        result = gateway.calendar(day="2026-09-04")
    finally:
        client.close()
    assert len(result["events"]) == 2
    assert result["conflicts"][0]["event_ids"] == ["one", "two"]
    assert requests[-1].headers["Maton-Connection"] == "connection-google-calendar"
    assert requests[-1].url.params["singleEvents"] == "true"
    assert all(r.method == "GET" for r in requests)


def test_account_switch_rejected_before_reading_calendar(plugin, monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"connection": {"app": "google-calendar", "status": "ACTIVE", "metadata": {"email": "other@example.test"}}})
    gateway, client = api(plugin, monkeypatch, handle)
    try:
        with pytest.raises(plugin.brief.workspace.WorkspaceError, match="identity_changed"):
            gateway.calendar(day="2026-09-04")
    finally:
        client.close()
    assert len(requests) == 1


def test_mail_metadata_only_and_no_read_or_label_mutations(plugin, monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path.startswith("/connections/"):
            return httpx.Response(200, json={"connection": {"app": "google-mail", "status": "ACTIVE", "metadata": {"email": "pavel@example.test"}}})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "abcdef"}], "nextPageToken": "more"})
        return httpx.Response(200, json={"id": "abcdef", "threadId": "thread1", "payload": {"headers": [{"name": "Subject", "value": "Важное письмо"}]}})
    gateway, client = api(plugin, monkeypatch, handle)
    try:
        result = gateway.mail_headers()
    finally:
        client.close()
    assert result["body_read"] is False and result["truncated"] is True
    assert requests[-1].url.params["format"] == "metadata"
    assert all(r.method == "GET" for r in requests)


def test_redirect_not_followed_and_no_token_leak(plugin, monkeypatch):
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://outside.example/"})
    gateway, client = api(plugin, monkeypatch, handle)
    try:
        with pytest.raises(plugin.brief.workspace.WorkspaceError, match="302"):
            gateway.calendar(day="2026-09-04")
    finally:
        client.close()
    assert len(seen) == 1


def test_missing_sources_are_not_reported_as_empty_or_free(plugin):
    def fail(**kwargs):
        raise plugin.brief.workspace.WorkspaceError("workspace_http_403")
    result = plugin.brief.snapshot({}, client=SimpleNamespace(calendar=fail, mail_headers=fail))
    assert result["calendar"]["available"] is False
    assert "не означает пустой inbox" in result["text"]
    assert "Свободные окна не определяю" in result["text"]


def test_adjacent_and_transparent_events_are_not_conflicts(plugin):
    def event(eid, start, end, **kwargs):
        return {"id": eid, "start": {"dateTime": start}, "end": {"dateTime": end}, **kwargs}
    events = [event("one", "2026-09-04T10:00:00+03:00", "2026-09-04T11:00:00+03:00"),
              event("two", "2026-09-04T11:00:00+03:00", "2026-09-04T12:00:00+03:00"),
              event("transparent", "2026-09-04T10:30:00+03:00", "2026-09-04T11:30:00+03:00", transparent=True)]
    assert plugin.brief.workspace.calendar_conflicts(events, ZoneInfo("Europe/Moscow")) == []


def test_brief_does_not_mark_nudges_delivered(plugin):
    plugin.ledger.save_item({"title": "Приоритет", "state": "active", "priority": 10, "owner_confirmed": True})
    empty = SimpleNamespace(calendar=lambda **kw: {"available": True, "events": [], "conflicts": [], "truncated": False},
                            mail_headers=lambda: {"available": True, "messages": [], "truncated": False})
    first = plugin.brief.snapshot({}, client=empty)
    second = plugin.brief.snapshot({}, client=empty)
    assert first["attention"]["count"] == second["attention"]["count"] == 1
    assert first["attention"]["marked_prompted"] is False
    assert first["external_writes_performed"] is False
