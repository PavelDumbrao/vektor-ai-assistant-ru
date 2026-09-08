"""Read-only Fathom client routed through the owner's Maton connection."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional


BASE_URL = "https://gateway.maton.ai/fathom/external/v1"
MAX_PAGES = 3
MAX_TRANSCRIPT_SEGMENTS = 200


class FathomError(RuntimeError):
    pass


def available() -> bool:
    return bool(os.environ.get("MCP_MATON_API_KEY", "").strip())


def _request(path: str, query: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    key = os.environ.get("MCP_MATON_API_KEY", "").strip()
    if not key:
        raise FathomError("Maton key is not configured")
    if not path.startswith("/") or ".." in path:
        raise FathomError("invalid Fathom path")
    from hermes_constants import get_hermes_home
    if (get_hermes_home() / "focus/assistant-settings.json").is_file():
        from .workspace import Workspace
        client = Workspace()
        try:
            return client._get("/fathom/external/v1" + path, query, client.binding("fathom"))
        finally:
            client.close()
    url = BASE_URL + path
    if query:
        clean_query = {k: v for k, v in query.items() if v not in (None, "")}
        url += "?" + urllib.parse.urlencode(clean_query, doseq=True)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "Vektor-Focus-Assistant/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise FathomError(f"Fathom returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FathomError("Fathom is temporarily unavailable") from exc
    if not isinstance(payload, dict):
        raise FathomError("unexpected Fathom response")
    return payload


def list_meetings(
    *,
    created_after: Optional[str] = None,
    created_before: Optional[str] = None,
    include_action_items: bool = True,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    cursor = None
    items: list[dict[str, Any]] = []
    for _ in range(max(1, min(MAX_PAGES, int(max_pages)))):
        payload = _request(
            "/meetings",
            {
                "created_after": created_after,
                "created_before": created_before,
                "include_action_items": "true" if include_action_items else "false",
                "cursor": cursor,
            },
        )
        page_items = payload.get("items") or []
        items.extend(item for item in page_items if isinstance(item, dict))
        cursor = payload.get("next_cursor")
        if not cursor:
            break
    return items


def get_summary(recording_id: int | str) -> str:
    rid = _recording_id(recording_id)
    payload = _request(f"/recordings/{rid}/summary")
    summary = payload.get("summary") or payload.get("default_summary") or payload
    if isinstance(summary, dict):
        return str(summary.get("markdown_formatted") or summary.get("text") or json.dumps(summary, ensure_ascii=False))
    return str(summary or "")


def get_transcript(recording_id: int | str) -> list[dict[str, Any]]:
    rid = _recording_id(recording_id)
    payload = _request(f"/recordings/{rid}/transcript")
    transcript = payload.get("transcript") or []
    return [segment for segment in transcript if isinstance(segment, dict)]


def _recording_id(value: int | str) -> int:
    text = str(value).strip()
    if not re.fullmatch(r"\d{1,20}", text):
        raise FathomError("recording_id must be an integer")
    return int(text)


def _safe_action_items(meeting: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for item in meeting.get("action_items") or []:
        if not isinstance(item, dict):
            continue
        assignee = item.get("assignee") or {}
        result.append(
            {
                "description": str(item.get("description") or "")[:1000],
                "completed": bool(item.get("completed")),
                "timestamp": item.get("recording_timestamp"),
                "assignee": str(assignee.get("name") or "")[:200] if isinstance(assignee, dict) else None,
            }
        )
    return result


def recent_meetings(args: dict[str, Any]) -> dict[str, Any]:
    meetings = list_meetings(
        created_after=args.get("created_after"),
        created_before=args.get("created_before"),
        include_action_items=bool(args.get("include_action_items", True)),
        max_pages=int(args.get("max_pages") or 2),
    )
    limit = max(1, min(50, int(args.get("limit") or 10)))
    output = []
    for meeting in meetings[:limit]:
        output.append(
            {
                "recording_id": meeting.get("recording_id"),
                "title": meeting.get("meeting_title") or meeting.get("title"),
                "created_at": meeting.get("created_at"),
                "scheduled_start_time": meeting.get("scheduled_start_time"),
                "scheduled_end_time": meeting.get("scheduled_end_time"),
                "recording_start_time": meeting.get("recording_start_time"),
                "recording_end_time": meeting.get("recording_end_time"),
                "action_items": _safe_action_items(meeting),
            }
        )
    return {"meetings": output, "count": len(output), "truncated": len(meetings) > limit}


def meeting_summary(args: dict[str, Any]) -> dict[str, Any]:
    summary = get_summary(args.get("recording_id"))
    max_chars = max(500, min(20000, int(args.get("max_chars") or 12000)))
    return {"recording_id": _recording_id(args.get("recording_id")), "summary": summary[:max_chars], "truncated": len(summary) > max_chars}


def transcript(args: dict[str, Any]) -> dict[str, Any]:
    rid = _recording_id(args.get("recording_id"))
    segments = get_transcript(rid)
    query = str(args.get("query") or "").strip().casefold()
    if query:
        segments = [segment for segment in segments if query in str(segment.get("text") or "").casefold()]
    limit = max(1, min(MAX_TRANSCRIPT_SEGMENTS, int(args.get("max_segments") or 80)))
    safe = []
    for segment in segments[:limit]:
        speaker = segment.get("speaker") or {}
        safe.append(
            {
                "timestamp": segment.get("timestamp"),
                "speaker": str(speaker.get("display_name") or "")[:200] if isinstance(speaker, dict) else None,
                "text": str(segment.get("text") or "")[:4000],
            }
        )
    return {"recording_id": rid, "segments": safe, "count": len(safe), "truncated": len(segments) > limit}


def json_result(callable_, args: dict[str, Any]) -> str:
    try:
        return json.dumps({"ok": True, **callable_(args)}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False)
