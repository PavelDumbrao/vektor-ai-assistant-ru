"""Bounded assistant briefing with explicit missing-source states."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from . import ledger, workspace


def snapshot(args, client=None):
    if set(args) - {"day", "mode"}:
        raise ValueError("invalid_brief_fields")
    mode = args.get("mode", "morning")
    if mode not in {"morning", "evening"}:
        raise ValueError("invalid_brief_mode")
    if "day" in args:
        datetime.strptime(args["day"], "%Y-%m-%d")
    owned = client is None
    try:
        client = client or workspace.Workspace()
        try:
            calendar = client.calendar(day=args.get("day"), days=2)
        except Exception as exc:
            calendar = {"available": False, "error": str(exc) if isinstance(exc, workspace.WorkspaceError) else type(exc).__name__}
        try:
            mail = client.mail_headers()
        except Exception as exc:
            mail = {"available": False, "error": str(exc) if isinstance(exc, workspace.WorkspaceError) else type(exc).__name__}
    except workspace.WorkspaceError as exc:
        calendar = mail = {"available": False, "error": str(exc)}
    finally:
        if owned and client:
            client.close()
    items = ledger.list_items({"limit": 100})
    active = [i for i in items["items"] if i["owner_confirmed"] and i["state"] in {"active", "scheduled", "waiting"}]
    candidates = [i for i in items["items"] if i["state"] == "candidate"]
    data = {"ok": True, "mode": mode, "date": args.get("day") or datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat(),
            "calendar": calendar, "mail": mail, "priorities": active[:3], "candidate_count": len(candidates),
            "attention": ledger.attention({"cadence": mode, "max_items": 5, "mark_prompted": False}),
            "goals": ledger.goals({"action": "list"}), "untrusted_data": True,
            "external_writes_performed": False, "delivery_acknowledged": False}
    data["text"] = render(data)
    return data


def render(data):
    def short(value, limit=160):
        return " ".join(str(value).split())[:limit]
    lines = [f"{'☀️ Утренний бриф' if data['mode']=='morning' else '🌙 Вечерний обзор'} · {data['date']}"]
    calendar = data["calendar"]
    if not calendar["available"]:
        lines += ["\nКалендарь: доступ сейчас не подтверждён. Свободные окна не определяю."]
    else:
        events = calendar["events"]
        lines.append("\nКалендарь:")
        if not events:
            lines.append("В проверенном календаре на сегодня и завтра событий нет.")
        for event in events[:8]:
            stamp = workspace.event_time(event["start"], ZoneInfo("Europe/Moscow"))
            label = stamp.strftime("%d.%m, весь день" if event["all_day"] else "%d.%m %H:%M") if stamp else "время не указано"
            lines.append(f"• {label}: {short(event['title'], 120)}")
        if len(events) > 8:
            lines.append(f"Ещё событий в проверенной выборке: {len(events) - 8}.")
        if calendar.get("truncated"):
            lines.append("Показана ограниченная выборка календаря; полный список не проверен.")
        if calendar["conflicts"]:
            lines.append(f"⚠️ Пересечений во времени: {len(calendar['conflicts'])}.")
    lines.append("\nДо трёх подтверждённых приоритетов:")
    lines.extend(f"• {short(item['title'])}" for item in data["priorities"])
    if not data["priorities"]:
        lines.append("Подтверждённых приоритетов пока нет.")
    if data["candidate_count"]:
        lines.append(f"Ещё {data['candidate_count']} пунктов ждут решения, это пока не обязательства.")
    mail = data["mail"]
    lines.append("\nПочта:")
    if not mail["available"]:
        lines.append("Не удалось проверить. Отсутствие доступа не означает пустой inbox.")
    elif not mail["messages"]:
        lines.append("Непрочитанных входящих за проверенные 7 дней не найдено.")
    else:
        for item in mail["messages"][:3]:
            lines.append(f"• {short(item['subject'], 120) or '(без темы)'}")
        lines.append("Это заголовки непрочитанных писем, не оценка важности их содержания.")
    lines.append("\nКакой один результат сегодня важнее всего?" if data["mode"] == "morning" else "Что сегодня действительно завершено, а что переносим?")
    return "\n".join(lines)


SCHEMA = {"name": "assistant_brief", "description": "Собирает утренний/вечерний бриф: bound Calendar, заголовки Gmail, подтверждённые задачи, кандидаты, пересечения. Только чтение, не помечает письма прочитанными и не меняет календарь. Возвращает готовый text и качество источников.",
    "parameters": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["morning", "evening"]}, "day": {"type": "string", "description": "YYYY-MM-DD, по умолчанию сегодня МСК."}}, "additionalProperties": False}}
