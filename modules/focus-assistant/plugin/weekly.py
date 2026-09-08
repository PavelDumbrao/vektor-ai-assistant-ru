"""Weekly evidence review, without marking tasks done or moving calendar events."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import ledger, projects, workspace


def review(args, client=None):
    if set(args) - {"day"}:
        raise ValueError("invalid_weekly_fields")
    tz = ZoneInfo("Europe/Moscow")
    end = (
        datetime.strptime(args["day"], "%Y-%m-%d").replace(tzinfo=tz)
        + timedelta(days=1)
        if args.get("day")
        else datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    start = end - timedelta(days=7)
    conn = ledger._connect()
    try:
        rows = conn.execute(
            "SELECT * FROM focus_items WHERE state NOT IN ('done','cancelled') OR completed_at>=? OR updated_at>=? ORDER BY priority DESC,updated_at DESC LIMIT 201",
            (ledger._iso(start), ledger._iso(start)),
        ).fetchall()
        truncated = len(rows) > 200
        items = [ledger._row_to_item(row) for row in rows[:200]]
    finally:
        conn.close()
    done = [
        item
        for item in items
        if item["state"] == "done"
        and item["completed_at"]
        and start <= ledger._parse_stored(item["completed_at"]) < end
    ]
    active = [
        item
        for item in items
        if item["owner_confirmed"]
        and item["state"] in {"active", "waiting", "scheduled"}
    ]
    candidates = [item for item in items if item["state"] == "candidate"]
    waiting = [
        item
        for item in active
        if item["kind"] == "waiting" or item["state"] == "waiting"
    ]
    goals = ledger.goals({"action": "list"})
    goal_ids = {goal["id"] for goal in goals["goals"] if goal.get("status") == "active"}
    owned = client is None
    try:
        client = client or workspace.Workspace()
        previous_calendar = client.calendar(day=start.date().isoformat(), days=7)
        next_calendar = client.calendar(day=end.date().isoformat(), days=7)
    except Exception as exc:
        previous_calendar = next_calendar = {
            "available": False,
            "error": str(exc)
            if isinstance(exc, workspace.WorkspaceError)
            else type(exc).__name__,
        }
    finally:
        if owned and client:
            client.close()
    result = {
        "ok": True,
        "period_from": start.date().isoformat(),
        "period_through": (end - timedelta(days=1)).date().isoformat(),
        "done": done,
        "active": active,
        "waiting": waiting,
        "candidates": candidates,
        "goals": goals,
        "unlinked_to_goal": sum(item.get("goal_id") not in goal_ids for item in active),
        "projects": [
            {k: p.get(k) for k in ("id", "name", "version", "status", "next_action")}
            for p in projects.run({"action": "list"})["projects"]
        ],
        "previous_calendar": previous_calendar,
        "next_calendar": next_calendar,
        "proposed_next_week": active[:3],
        "truncated": truncated,
        "untrusted_data": True,
        "external_writes_performed": False,
        "task_states_changed": False,
    }
    result["text"] = render(result)
    return result


def render(data):
    def title(item):
        return " ".join(item["title"].split())[:150]

    lines = [
        f"📌 Недельный разбор · {data['period_from']} — {data['period_through']}",
        f"\nПодтверждённо закрыто: {len(data['done'])}.",
    ]
    lines.extend("• " + title(item) for item in data["done"][:5])
    lines += [
        f"\nАктивных подтверждённых дел: {len(data['active'])}.",
        f"Ожиданий от других: {len(data['waiting'])}.",
        f"Кандидатов на решение: {len(data['candidates'])}.",
    ]
    if not data["goals"]["goals"]:
        lines.append(
            "Подтверждённые цели ещё не заданы, сравнение с целями пока невозможно."
        )
    elif data["unlinked_to_goal"]:
        lines.append(f"Не связаны с действующей целью: {data['unlinked_to_goal']} дел.")
    lines.append("\nКалендарь:")
    for label, calendar in (
        ("За проверенную неделю", data["previous_calendar"]),
        ("На следующую неделю", data["next_calendar"]),
    ):
        if calendar["available"]:
            lines.append(
                f"• {label}: {len(calendar['events'])} событий, пересечений {len(calendar['conflicts'])}."
                + (" Выборка неполная." if calendar.get("truncated") else "")
            )
        else:
            lines.append(f"• {label}: данные не удалось проверить.")
    lines.append(
        "Событие в календаре само по себе не подтверждает, что встреча состоялась."
    )
    lines.append("\nКандидаты в план следующей недели:")
    lines.extend("• " + title(item) for item in data["proposed_next_week"])
    if not data["proposed_next_week"]:
        lines.append("Нужен выбор владельца; новые задачи не придумываю.")
    if data["truncated"]:
        lines.append("Реестр показан ограниченно, это не полный аудит всех записей.")
    lines.append("\nКакие три результата оставляем на следующую неделю?")
    return "\n".join(lines)


SCHEMA = {
    "name": "assistant_weekly",
    "description": "Недельный разбор фактов: закрытые/открытые дела, ожидания, кандидаты, цели, проекты и Calendar на две недели. Предлагает не больше трёх результатов. Не отмечает выполнение и не меняет встречи.",
    "parameters": {
        "type": "object",
        "properties": {
            "day": {
                "type": "string",
                "description": "Последний день обзорной недели YYYY-MM-DD; по умолчанию сегодня МСК.",
            }
        },
        "additionalProperties": False,
    },
}
