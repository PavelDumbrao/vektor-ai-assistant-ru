from __future__ import annotations

import re
from dataclasses import replace
from typing import Callable

from .models import Precheck, ReplyDecision, Route


PROMPT_INJECTION_PATTERNS = (
    r"ignore (all|any|the|previous)",
    r"игнорируй (все|всё|предыдущ)",
    r"system prompt",
    r"системн(ый|ое) промпт",
    r"покажи (токен|ключ|парол|инструкц)",
    r"выполни (команд|код|скрипт)",
    r"прочитай .*\.env",
    r"developer message",
)

HIGH_RISK_PATTERNS = (
    r"\b(цен[аыуе]|стоимост|сколько стоит|прайс)\b",
    r"\b(оплат|плат[её]ж|возврат|договор|сч[её]т|карт[аыу]|рубл|доллар|₽|\$)\b",
    r"\b(юрист|юрид|суд|налог|декларац|штраф)\b",
    r"\b(врач|медицин|лекарств|диагноз|здоровь)\b",
    r"\b(инвестиц|акци[ия]|крипт|доходност|финанс)\b",
    r"\b(войн|политик|выбор|президент|санкци)\b",
    r"\b(жалоб|претензи|обман|мошенн|верните|компенсац)\b",
    r"\b(партн[её]р|сотрудничеств|контракт|нанять|заказать)\b",
    r"\b(телефон|почт[ауы]|email|e-mail|адрес|паспорт)\b",
)

OWNER_DECISION_PATTERNS = (
    r"\bпавел[,:]?\s+(вы|ты|когда|почему|можешь|сделай|дайте|ответь)",
    r"\bпаша[,:]?\s+",
    r"\bкогда (будет|запустите|сделаете|провед[её]те)\b",
)

TOXIC_PATTERNS = (
    r"\b(идиот|дебил|тупой|тупая|мусор|говно|херня|самоотключись|заткнись)\b",
    r"\b(вр[её]шь|бред|лохотрон)\b",
)

PROHIBITED_REPLY_PATTERNS = (
    r"https?://",
    r"t\.me/",
    r"\b\d[\d\s]{5,}\b",
    r"[₽$€]",
    r"\b(гарантир|обещаю|точно заработ|я павел|от лица павла)\b",
)


def _text(message: dict) -> str:
    return str(message.get("text") or message.get("caption") or "").strip()


def _sender_chat_id(message: dict) -> int | None:
    sender = message.get("sender_chat") or {}
    value = sender.get("id")
    return int(value) if isinstance(value, int) else None


def is_channel_root(message: dict, channel_id: int) -> bool:
    return bool(
        message.get("is_automatic_forward")
        and _sender_chat_id(message) == channel_id
        and isinstance(message.get("message_id"), int)
    )


def route_message(
    message: dict,
    *,
    discussion_id: int,
    channel_id: int,
    bot_username: str,
    root_exists: Callable[[int], bool],
) -> Route:
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id != discussion_id:
        return Route("ignore", None, "outside_discussion")

    sender = message.get("from") or {}
    if sender.get("is_bot"):
        return Route("ignore", None, "bot_sender")

    if is_channel_root(message, channel_id):
        return Route("root", int(message["message_id"]), "automatic_channel_forward")

    text = _text(message).lower()
    mention = f"@{bot_username.lower()}"
    direct_mention = mention in text or "ai fixer" in text or "ии редактор" in text

    reply = message.get("reply_to_message") or {}
    candidate_ids: list[int] = []
    for candidate in (
        message.get("message_thread_id"),
        reply.get("message_thread_id"),
        reply.get("message_id"),
    ):
        if isinstance(candidate, int) and candidate not in candidate_ids:
            candidate_ids.append(candidate)

    if is_channel_root(reply, channel_id):
        return Route("comment", int(reply["message_id"]), "reply_to_channel_root")
    for candidate in candidate_ids:
        if root_exists(candidate):
            return Route("comment", candidate, "known_channel_thread")

    if direct_mention:
        return Route("mention", message.get("message_thread_id"), "direct_bot_mention")
    return Route("ignore", None, "general_chat")


def precheck_comment(text: str) -> Precheck:
    compact = " ".join(text.split())
    lowered = compact.lower()
    if not compact:
        return Precheck("ignore", "low", "empty")
    if len(compact) < 3 or not re.search(r"[A-Za-zА-Яа-яЁё]", compact):
        return Precheck("ignore", "low", "reaction_only")
    if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in PROMPT_INJECTION_PATTERNS):
        return Precheck("ignore", "high", "prompt_injection")
    if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in TOXIC_PATTERNS):
        return Precheck("ignore", "medium", "toxic_or_provocation")
    if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in HIGH_RISK_PATTERNS):
        return Precheck("escalate", "high", "sensitive_topic")
    if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in OWNER_DECISION_PATTERNS):
        return Precheck("escalate", "medium", "owner_decision")
    if re.search(r"https?://|t\.me/", lowered):
        return Precheck("escalate", "medium", "external_link")
    return Precheck("continue", "low", "eligible")


def sanitize_reply_text(text: str) -> str:
    cleaned = text.replace("—", "-").replace("–", "-")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip().strip("`")
    return cleaned


def enforce_reply_policy(
    decision: ReplyDecision,
    *,
    min_confidence: float,
    max_chars: int,
) -> ReplyDecision:
    if decision.action != "reply":
        return replace(decision, reply_text="")
    if decision.risk != "low":
        return replace(decision, action="escalate", reply_text="", reason="model_marked_non_low_risk")
    if decision.confidence < min_confidence:
        return replace(decision, action="escalate", reply_text="", reason="confidence_below_threshold")

    reply = sanitize_reply_text(decision.reply_text)
    if not reply:
        return replace(decision, action="ignore", reply_text="", reason="empty_reply")
    if len(reply) > max_chars:
        return replace(decision, action="escalate", reply_text="", reason="reply_too_long")
    lowered = reply.lower()
    if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in PROHIBITED_REPLY_PATTERNS):
        return replace(decision, action="escalate", reply_text="", reason="reply_contains_prohibited_content")
    if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in HIGH_RISK_PATTERNS):
        return replace(decision, action="escalate", reply_text="", reason="reply_contains_sensitive_topic")
    return replace(decision, reply_text=reply)

