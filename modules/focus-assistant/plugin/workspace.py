"""Bound-account, read-only Google Workspace access through the existing Maton key."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from hermes_constants import get_hermes_home


class WorkspaceError(RuntimeError):
    pass


class WorkspaceRejected(WorkspaceError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"workspace_http_{code}")


def load_settings():
    path = get_hermes_home() / "focus/assistant-settings.json"
    if not path.is_file():
        raise WorkspaceError("assistant_connections_not_bound")
    value = json.loads(path.read_text())
    if value.get("version") != 1 or value.get("timezone") != "Europe/Moscow":
        raise WorkspaceError("invalid_assistant_settings")
    return value


class Workspace:
    def __init__(self, settings=None, client=None):
        self.settings = settings if settings is not None else load_settings()
        self.key = os.environ.get("MCP_MATON_API_KEY", "").strip()
        if not self.key:
            raise WorkspaceError("maton_key_not_configured")
        self.client = client or httpx.Client(timeout=httpx.Timeout(25, connect=7), follow_redirects=False)
        self.owns_client = client is None
        self.verified = set()

    def close(self):
        if self.owns_client:
            self.client.close()

    def _get(self, path, params=None, connection=None):
        return self._request("GET", path, params=params, connection=connection)

    def _request(self, method, path, *, params=None, connection=None, payload=None):
        if method not in {"GET", "POST"} or not path.startswith("/") or ".." in path:
            raise WorkspaceError("invalid_workspace_request")
        headers = {"Authorization": "Bearer " + self.key}
        if connection:
            headers["Maton-Connection"] = connection
        try:
            with self.client.stream(method, "https://api.maton.ai" + path, params=params, headers=headers, json=payload) as response:
                if response.status_code != 200 and not 400 <= response.status_code < 500:
                    raise WorkspaceError(f"workspace_http_{response.status_code}")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise WorkspaceError("workspace_response_too_large")
                try:
                    value = json.loads(raw)
                except ValueError:
                    if 400 <= response.status_code < 500:
                        raise WorkspaceRejected(response.status_code) from None
                    raise WorkspaceError("unexpected_workspace_response") from None
                if 400 <= response.status_code < 500:
                    if isinstance(value, dict) and (value.get("id") or value.get("result")):
                        raise WorkspaceError("workspace_ambiguous_error_response")
                    raise WorkspaceRejected(response.status_code)
                if not isinstance(value, dict):
                    raise WorkspaceError("unexpected_workspace_response")
                if value.get("error"):
                    raise WorkspaceError("workspace_error_response")
                return value
        except WorkspaceError:
            raise
        except Exception:
            raise WorkspaceError("workspace_request_failed") from None

    def binding(self, app):
        bound = self.settings.get("connections", {}).get(app, {})
        cid = bound.get("connection_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", cid):
            raise WorkspaceError("workspace_app_not_bound")
        if app not in self.verified:
            response = self._get("/connections/" + cid)
            actual = response.get("connection", response)
            if actual.get("app") != app or actual.get("status") != "ACTIVE":
                raise WorkspaceError("bound_connection_not_active")
            expected = bound.get("identity_sha256")
            if expected:
                identity = str((actual.get("metadata") or {}).get("email", "")).strip().lower()
                if not identity or hashlib.sha256(identity.encode()).hexdigest() != expected:
                    raise WorkspaceError("bound_account_identity_changed")
            self.verified.add(app)
        return cid

    def calendar(self, *, day=None, days=2):
        if type(days) is not int or not 1 <= days <= 8:
            raise ValueError("calendar_days_out_of_range")
        tz = ZoneInfo(self.settings["timezone"])
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz) if day else datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=days)
        calendar = self.settings.get("calendar_id", "primary")
        if not isinstance(calendar, str) or len(calendar) > 256:
            raise WorkspaceError("invalid_calendar_binding")
        response = self._get("/google-calendar/calendar/v3/calendars/" + quote(calendar, safe="") + "/events",
            {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
             "orderBy": "startTime", "timeZone": self.settings["timezone"], "maxResults": 50}, self.binding("google-calendar"))
        events = []
        for item in response.get("items", []):
            if item.get("status") == "cancelled":
                continue
            if any(a.get("self") and a.get("responseStatus") == "declined" for a in item.get("attendees", [])):
                continue
            events.append({"id": item.get("id"), "title": str(item.get("summary", "Без названия"))[:300],
                "start": item.get("start", {}), "end": item.get("end", {}),
                "all_day": "date" in item.get("start", {}), "transparent": item.get("transparency") == "transparent",
                "url": item.get("htmlLink"), "status": item.get("status")})
        return {"available": True, "events": events, "truncated": bool(response.get("nextPageToken")),
                "from": start.isoformat(), "to": end.isoformat(), "conflicts": calendar_conflicts(events, tz)}

    def mail_headers(self, *, max_results=5, days=7):
        if type(max_results) is not int or not 1 <= max_results <= 5:
            raise ValueError("mail_limit_out_of_range")
        if type(days) is not int or not 1 <= days <= 7:
            raise ValueError("mail_days_out_of_range")
        cid = self.binding("google-mail")
        response = self._get("/google-mail/gmail/v1/users/me/messages",
            {"q": f"is:unread in:inbox newer_than:{days}d", "maxResults": max_results}, cid)
        messages = []
        for item in response.get("messages", []):
            mid = item.get("id", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", mid):
                raise WorkspaceError("invalid_mail_id")
            data = self._get("/google-mail/gmail/v1/users/me/messages/" + mid,
                {"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]}, cid)
            headers = {h.get("name", "").lower(): str(h.get("value", "")) for h in data.get("payload", {}).get("headers", [])}
            messages.append({"id": mid, "thread_id": data.get("threadId"),
                             "from": headers.get("from", "")[:250], "subject": headers.get("subject", "")[:300],
                             "date": headers.get("date", "")[:100], "source_ref": "gmail:" + mid})
        return {"available": True, "messages": messages, "truncated": bool(response.get("nextPageToken")),
                "scope": f"unread_inbox_last_{days}_days_metadata_only", "body_read": False}

    def calendar_event(self, event_id):
        if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", event_id):
            raise ValueError("invalid_event_id")
        calendar = quote(self.settings.get("calendar_id", "primary"), safe="")
        data = self._get(f"/google-calendar/calendar/v3/calendars/{calendar}/events/{event_id}", connection=self.binding("google-calendar"))
        return {"id": data.get("id"), "title": str(data.get("summary", ""))[:300],
                "description": str(data.get("description", ""))[:3000], "start": data.get("start"), "end": data.get("end"),
                "status": data.get("status"), "url": data.get("htmlLink"),
                "attendee_names": [str(a.get("displayName") or "Участник без имени")[:100] for a in data.get("attendees", [])[:20]],
                "untrusted_data": True}

    def mail_profile(self):
        result = self._get("/google-mail/gmail/v1/users/me/profile", connection=self.binding("google-mail"))
        email = str(result.get("emailAddress", "")).strip().lower()
        expected = self.settings["connections"]["google-mail"].get("identity_sha256")
        if not email or expected and hashlib.sha256(email.encode()).hexdigest() != expected:
            raise WorkspaceError("mail_profile_identity_mismatch")
        return email

    def mail_message(self, message_id, *, metadata=False):
        if not isinstance(message_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", message_id):
            raise ValueError("invalid_mail_message_id")
        return self._get("/google-mail/gmail/v1/users/me/messages/" + message_id,
            {"format": "metadata" if metadata else "full"}, self.binding("google-mail"))

    def send_mail(self, payload):
        return self._request("POST", "/google-mail/gmail/v1/users/me/messages/send",
                             connection=self.binding("google-mail"), payload=payload)

    def find_sent_mail(self, message_id):
        if not re.fullmatch(r"<mail_[a-f0-9]{20}@[^<>\s]{3,254}>", message_id):
            raise ValueError("invalid_readback_message_id")
        data = self._get("/google-mail/gmail/v1/users/me/messages",
            {"q": "in:sent rfc822msgid:" + message_id, "maxResults": 2}, self.binding("google-mail"))
        return [str(item["id"]) for item in data.get("messages", [])]


def event_time(value, tz):
    if value.get("dateTime"):
        parsed = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(value.get("timeZone") or str(tz)))
        return parsed.astimezone(tz)
    if value.get("date"):
        return datetime.strptime(value["date"], "%Y-%m-%d").replace(tzinfo=tz)
    return None


def calendar_conflicts(events, tz):
    intervals = []
    for event in events:
        if event.get("transparent"):
            continue
        start, end = event_time(event["start"], tz), event_time(event["end"], tz)
        if start and end and end > start:
            intervals.append((start, end, event["id"]))
    conflicts = []
    for index, (start, end, eid) in enumerate(intervals):
        for other_start, other_end, other_id in intervals[index + 1:]:
            if start < other_end and other_start < end:
                conflicts.append({"event_ids": [eid, other_id], "from": max(start, other_start).isoformat(), "to": min(end, other_end).isoformat()})
    return conflicts
