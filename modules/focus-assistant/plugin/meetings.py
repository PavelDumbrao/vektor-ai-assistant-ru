"""Meeting preparation and bounded, evidence-bearing post-meeting material."""
from __future__ import annotations

from . import fathom, ledger, workspace


def meeting(args, client=None):
    if set(args) - {"mode", "event_id", "recording_id", "project", "query"}:
        raise ValueError("invalid_meeting_fields")
    mode = args.get("mode")
    if mode == "prepare":
        owned = client is None
        client = client or workspace.Workspace()
        try:
            event = client.calendar_event(args.get("event_id"))
        finally:
            if owned:
                client.close()
        if event.get("status") == "cancelled":
            return {"ok": True, "mode": mode, "event": event, "preparation_needed": False, "reason": "meeting_cancelled"}
        project = args.get("project")
        related = ledger.list_items({"project": project, "limit": 30})["items"] if project else []
        return {"ok": True, "mode": mode, "event": event, "preparation_needed": True,
                "related_items": related, "project_match": "explicit_selection" if project else "not_selected",
                "suggested_structure": ["Цель встречи", "Что уже согласовано со ссылками", "Открытые обязательства", "Вопросы для решения"],
                "untrusted_data": True, "external_writes_performed": False,
                "instruction": "Подготовь короткую справку. Не выдумывай прежние договорённости или принадлежность к проекту. Встречу и приглашения не меняй."}
    if mode == "review":
        rid = fathom._recording_id(args.get("recording_id"))
        summary = fathom.meeting_summary({"recording_id": rid, "max_chars": 6000})
        transcript = fathom.transcript({"recording_id": rid, "max_segments": 50, "query": args.get("query", "")})
        full = "\n".join(f"{row.get('timestamp') or ''} {row.get('speaker') or ''}: {row['text']}" for row in transcript["segments"])
        excerpt = full[:14000]
        return {"ok": True, "mode": mode, "recording_id": rid, "summary": summary["summary"],
                "transcript_excerpt": excerpt, "transcript_truncated": transcript["truncated"] or len(full) > len(excerpt),
                "capture_source_ref": f"fathom:{rid}", "untrusted_data": True, "external_writes_performed": False,
                "instruction": "Раздели решения, идеи, обязательства, ожидания и открытые вопросы. Для focus_capture бери буквальные цитаты из transcript_excerpt. Отсутствующие сроки/исполнители оставляй неизвестными. Не заявляй, что прочитана вся встреча, если transcript_truncated=true. Итоговое письмо только черновик."}
    raise ValueError("meeting_mode_must_be_prepare_or_review")


SCHEMA = {"name": "assistant_meeting", "description": "Секретарь встреч: prepare по event_id из Calendar, review по recording_id Fathom. Краткая подготовка или материал для решений/поручений с цитатами. Только чтение; неизвестные даты и исполнители не придумывать.",
    "parameters": {"type": "object", "properties": {
        "mode": {"type": "string", "enum": ["prepare", "review"]}, "event_id": {"type": "string"},
        "recording_id": {"type": ["integer", "string"]}, "project": {"type": "string"}, "query": {"type": "string"}},
        "required": ["mode"], "additionalProperties": False}}
