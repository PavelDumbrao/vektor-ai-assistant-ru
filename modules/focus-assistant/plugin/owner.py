"""Trusted gateway context and scoped approvals for personal-assistant tools."""
from __future__ import annotations

import hashlib
import json
import threading
import time


TELEGRAM_RESPONSE_STYLE = """Подача ответа в личном Telegram:
- Пиши по-русски, на «ты», как спокойный толковый товарищ. Без канцелярита,
  саморекламы, фальшивого восторга и фраз «рад помочь».
- Начинай с результата или прямого ответа. Не пересказывай запрос и ход мыслей.
- Обычный ответ держи компактным: короткие абзацы, одна мысль на абзац. Если
  пунктов больше двух, используй простой список. Один вопрос за раз.
- Используй обычный Markdown, который Hermes сам переведёт в Telegram MarkdownV2:
  **жирный** для коротких смысловых заголовков, списки через `-`, нумерацию для
  последовательности, `>` только для настоящей цитаты, `code` только для команд,
  [понятный текст](https://example.com) для ссылки. Не пиши HTML и не экранируй
  MarkdownV2 вручную. Не делай таблицу в коротком сообщении.
- Эмодзи необязательны; максимум 1-2 уместных на сообщение, не ставь значок у
  каждого пункта. Не дроби одну короткую мысль на множество секций.
- Не показывай JSON, имена tools, внутренние пути, хеши и служебные ID, если
  владелец этого не просил. Ошибку объясни простыми словами: что не получилось
  и какой один следующий шаг.
- Чётко различай: проверено, подготовлено, отправлено. Если нужно действие
  владельца, вынеси в последнюю короткую строку: **От тебя нужно:** ...
- Если владелец явно запросил иной формат, длину, дословный JSON или официальный
  стиль, следуй его запросу вместо этих предпочтений.
"""


def fingerprint(tool, args):
    return hashlib.sha256(json.dumps([tool, args], ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


class OwnerGate:
    def __init__(self, owner_id):
        self.owner_id = str(owner_id)
        self.contexts = {}
        self.sources = {}
        self.permits = {}
        self.lock = threading.RLock()

    def observe(self, *, session_id="", turn_id="", sender_id="", platform="", chat_type="",
                raw_user_message=None, user_message=None, is_internal_event=None, **kwargs):
        platform = str(getattr(platform, "value", platform)).lower()
        chat_type = str(getattr(chat_type, "value", chat_type)).lower()
        has_owner_input = any(isinstance(v, str) and bool(v.strip()) for v in (raw_user_message, user_message))
        owner = (str(sender_id) == self.owner_id and platform == "telegram" and chat_type == "dm"
                 and is_internal_event is False and has_owner_input)
        # Native cron supplies platform="cron"; raw-message provenance is None.
        # This grants reads/candidate collection only, never an owner approval.
        internal = is_internal_event is True or platform == "cron"
        with self.lock:
            old = self.contexts.get(str(session_id), {})
            if old.get("turn") != str(turn_id):
                self.sources.pop(str(session_id), None)
                self.permits.pop(str(session_id), None)
            if session_id and turn_id and (owner or internal):
                self.contexts[str(session_id)] = {"turn": str(turn_id), "owner": owner,
                    "text": raw_user_message if owner else "", "expires": time.monotonic() + 1200,
                    "transcribed_text": user_message if isinstance(user_message, str) else raw_user_message,
                    "source_ref": f"owner-dm:{session_id}:{turn_id}"}
            else:
                self.contexts.pop(str(session_id), None)
                self.permits.pop(str(session_id), None)
        if owner or internal:
            return {"context": TELEGRAM_RESPONSE_STYLE}
        return None

    def context(self, session_id):
        value = self.contexts.get(str(session_id), {})
        return value if value.get("expires", 0) > time.monotonic() else {}

    def remember(self, session_id, ref, text, kind):
        if self.context(session_id):
            self.sources.setdefault(str(session_id), {})[ref] = {"text": text[:100000], "kind": kind, "source_ref": ref}

    def source(self, session_id, ref):
        ctx = self.context(session_id)
        if ref == "current_owner" and ctx.get("owner"):
            if not isinstance(ctx["text"], str) or not ctx["text"].strip():
                raise ValueError("owner_text_unavailable_use_voice_intake_for_transcription")
            return {"text": ctx["text"], "kind": "owner", "source_ref": ctx["source_ref"]}
        if ref == "current_voice" and ctx.get("owner") and isinstance(ctx.get("transcribed_text"), str):
            return {"text": ctx["transcribed_text"], "kind": "owner", "source_ref": ctx["source_ref"]}
        result = self.sources.get(str(session_id), {}).get(ref)
        if not ctx or not result:
            raise ValueError("source_not_observed_in_current_turn_fetch_it_first")
        return result

    @staticmethod
    def is_write(tool, args):
        if tool == "assistant_project":
            return args.get("action") not in {"list", "get"}
        if tool == "assistant_mail":
            return args.get("action") == "send"
        if tool == "focus_goals":
            return args.get("action", "list") != "list"
        if tool == "focus_task_update" or tool == "focus_task_save" and args.get("item_id"):
            return True
        if tool == "focus_task_save":
            return args.get("owner_confirmed") is True or args.get("state") in {"active", "waiting", "scheduled", "done", "cancelled"}
        return False

    def guard(self, *, tool_name="", args=None, session_id="", turn_id="", tool_call_id="", **kwargs):
        if not tool_name.startswith(("focus_", "assistant_", "fathom_")) and tool_name != "integration_connection_status":
            return None
        ctx = self.context(session_id)
        if not ctx:
            return {"action": "block", "message": "Нужен текущий диалог владельца или штатный scheduled-контекст профиля."}
        if not isinstance(args, dict):
            return {"action": "block", "message": "Некорректные аргументы."}
        if tool_name == "assistant_mail" and args.get("action") == "draft" and not ctx.get("owner"):
            return {"action": "block", "message": "Почтовые черновики готовятся только по поручению владельца в личке."}
        if self.is_write(tool_name, args):
            if not ctx.get("owner") or ctx["turn"] != str(turn_id):
                return {"action": "block", "message": "Подтверждать задачи и менять цели можно только из текущей лички владельца."}
            digest = fingerprint(tool_name, args)
            with self.lock:
                self.permits.pop(str(session_id), None)
            details = json.dumps(args, ensure_ascii=False, sort_keys=True)
            if len(details.encode("utf-16-le")) // 2 > 3000:
                return {"action": "block", "message": "Раздели изменение на меньшие части для точного подтверждения."}
            description = "Подтвердить это изменение личного реестра?\n" + details
            if args.get("item_id") and tool_name in {"focus_task_save", "focus_task_update"}:
                from . import ledger
                conn = ledger._connect()
                try:
                    item = ledger._fetch_item(conn, args["item_id"])
                    if item:
                        description = "Пункт: " + item["title"][:300] + "\n" + description
                finally:
                    conn.close()
            if tool_name == "assistant_mail":
                try:
                    from . import mail
                    row = mail.prepare_send(args)
                    description = "Отправить одно письмо с этим точным текстом?\n\n" + mail.preview_text(row)
                except ValueError as exc:
                    return {"action": "block", "message": str(exc)}
            if tool_name == "assistant_project":
                try:
                    from . import projects
                    description = projects.preview_change(args)
                except ValueError as exc:
                    return {"action": "block", "message": str(exc)}
            with self.lock:
                self.permits[str(session_id)] = (digest, time.monotonic() + 300)
            return {"action": "approve", "rule_key": f"focus-write:{session_id}:{turn_id}:{tool_call_id}:{digest}",
                    "message": description}
        return None

    def authorize(self, tool, args, session_id):
        if not self.context(session_id):
            raise ValueError("owner_or_scheduled_context_required")
        if tool == "assistant_mail" and args.get("action") == "draft" and not self.context(session_id).get("owner"):
            raise ValueError("mail_draft_requires_owner_dm")
        if self.is_write(tool, args):
            with self.lock:
                permit = self.permits.pop(str(session_id), None)
            if not permit or permit[0] != fingerprint(tool, args) or permit[1] < time.monotonic():
                raise ValueError("single_use_approval_required")

    def wrap(self, name, function):
        def handle(args, *, session_id="", **kwargs):
            try:
                self.authorize(name, args, session_id)
                result = function(args)
                if isinstance(result, str):
                    parsed = json.loads(result)
                else:
                    parsed = {"ok": True, **result}
                if parsed.get("ok") and (name in {"fathom_meeting_summary", "fathom_meeting_transcript"} or name == "assistant_meeting" and parsed.get("mode") == "review"):
                    rid = parsed.get("recording_id")
                    ref = f"fathom:{rid}"
                    source = parsed.get("transcript_excerpt") or parsed.get("summary") or "\n".join(str(s.get("text", "")) for s in parsed.get("segments", []))
                    self.remember(session_id, ref, source, "fathom")
                    parsed["capture_source_ref"] = ref
                    parsed["untrusted_data"] = True
                if parsed.get("ok") and name == "assistant_mail":
                    messages = [parsed["message"]] if parsed.get("message") else parsed.get("messages", [])
                    for message in messages:
                        self.remember(session_id, message["source_ref"], str(message.get("from") or "") + "\n" + str(message.get("subject") or "") + "\n" + message.get("text", ""), "gmail")
                return json.dumps(parsed, ensure_ascii=False)
            except Exception as exc:
                return json.dumps({"ok": False, "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}, ensure_ascii=False)
        return handle
