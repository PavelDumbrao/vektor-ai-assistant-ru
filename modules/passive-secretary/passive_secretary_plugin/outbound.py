"""Owner-confirmed Telegram Business replies for the passive secretary.

The archive collector remains capability-free.  This module is a separate,
explicitly enabled action edge.  It accepts only opaque archive selectors,
stores no outbound plaintext, requires a fresh one-time owner approval inside
the tool handler, and delegates the only send to the live Telegram adapter.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import inspect
import json
import re
import secrets
import unicodedata
from typing import Any, Callable

from .retrieval import RetrievalInputError, normalize_source_label, parse_source_ref
from .settings import Settings


ApprovalRequester = Callable[..., dict[str, Any]]
ReplySender = Callable[..., Any]
OwnerResolver = Callable[[], str | None]
IntentIdFactory = Callable[[], str]

_ALLOWED_ARGUMENTS = frozenset({"source_ref", "text", "reply_to_latest"})
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_APPROVAL_REASON = (
    "Отправка сообщения от имени владельца Telegram Business требует "
    "одноразового подтверждения владельца."
)


class OutboundReplyError(RuntimeError):
    """Stable, secret-free reply failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _default_approval_requester(**kwargs: Any) -> dict[str, Any]:
    from tools.approval import request_one_time_tool_approval

    return request_one_time_tool_approval(**kwargs)


def _default_reply_sender(**kwargs: Any) -> Any:
    from plugins.platforms.telegram.adapter import (
        send_business_reply_for_current_session,
    )

    return send_business_reply_for_current_session(**kwargs)


class TelegramBusinessReplyService:
    """Prepare and execute at most one exact owner-approved Business reply."""

    def __init__(
        self,
        settings: Settings,
        archive: Any,
        reference_key: bytes,
        *,
        approval_requester: ApprovalRequester | None = None,
        reply_sender: ReplySender | None = None,
        intent_id_factory: IntentIdFactory | None = None,
    ) -> None:
        self.settings = settings
        self.archive = archive
        self.reference_key = reference_key
        self._approval_requester = approval_requester or _default_approval_requester
        self._reply_sender = reply_sender or _default_reply_sender
        self._intent_id_factory = intent_id_factory or (
            lambda: f"intent:{secrets.token_hex(16)}"
        )

    def available(self) -> bool:
        if not (
            self.settings.outbound_replies_enabled
            and len(self.reference_key) >= 32
            and self.settings.postgres_configured()
            and importlib.util.find_spec("psycopg") is not None
        ):
            return False
        if self._approval_requester is not _default_approval_requester:
            approval_available = True
        else:
            try:
                from tools.approval import request_one_time_tool_approval  # noqa: F401

                approval_available = True
            except Exception:
                approval_available = False
        if self._reply_sender is not _default_reply_sender:
            sender_available = True
        else:
            try:
                from plugins.platforms.telegram.adapter import (  # noqa: F401
                    send_business_reply_for_current_session,
                )

                sender_available = True
            except Exception:
                sender_available = False
        return approval_available and sender_available

    def normalize_args(self, args: Any) -> dict[str, Any]:
        if not isinstance(args, dict) or set(args) - _ALLOWED_ARGUMENTS:
            raise OutboundReplyError("invalid_arguments")
        try:
            source_ref = parse_source_ref(args.get("source_ref"))
        except RetrievalInputError as exc:
            raise OutboundReplyError("invalid_source_ref") from exc
        if not source_ref:
            raise OutboundReplyError("invalid_source_ref")
        raw_text = args.get("text")
        if not isinstance(raw_text, str):
            raise OutboundReplyError("invalid_message_text")
        text = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if any(
            character not in {"\n", "\t"}
            and unicodedata.category(character).startswith("C")
            for character in text
        ):
            raise OutboundReplyError("invalid_message_text")
        if not text or len(text) > self.settings.outbound_reply_max_chars:
            raise OutboundReplyError("invalid_message_text")
        raw_reply = args.get("reply_to_latest", True)
        if not isinstance(raw_reply, bool):
            raise OutboundReplyError("invalid_reply_mode")
        return {
            "source_ref": source_ref,
            "text": text,
            "reply_to_latest": raw_reply,
        }

    def _session_ref(self, owner_id: str, session_id: str) -> str:
        if not session_id:
            raise OutboundReplyError("owner_session_not_authorized")
        digest = hmac.new(
            self.reference_key,
            f"outbound-session-v1\0{owner_id}\0{session_id}".encode(
                "utf-8", "strict"
            ),
            hashlib.sha256,
        ).hexdigest()
        return f"session:{digest[:32]}"

    def _message_hmac(self, normalized: dict[str, Any]) -> str:
        encoded = json.dumps(
            {
                "source_ref": normalized["source_ref"],
                "text": normalized["text"],
                "reply_to_latest": normalized["reply_to_latest"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", "strict")
        digest = hmac.new(
            self.reference_key,
            b"outbound-message-v1\0" + encoded,
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"

    def _approval_key(
        self,
        *,
        intent_id: str,
        owner_id: str,
        session_ref: str,
        source_ref: str,
        message_hmac: str,
    ) -> str:
        material = "\0".join(
            (
                "outbound-approval-v1",
                intent_id,
                owner_id,
                session_ref,
                source_ref,
                message_hmac,
            )
        ).encode("utf-8", "strict")
        digest = hmac.new(self.reference_key, material, hashlib.sha256).hexdigest()
        return f"passive-secretary-reply:{intent_id}:{digest}"

    @staticmethod
    def _display_target(
        *,
        chat_label: Any,
        source_ref: str,
        text: str,
    ) -> str:
        safe_label = normalize_source_label(chat_label) or "Неизвестный чат"
        # JSON quoting keeps control characters and newlines visibly literal
        # without truncating the exact text the owner is being asked to approve.
        return (
            f"Получатель: {json.dumps(safe_label, ensure_ascii=False)}\n"
            f"Источник: {source_ref}\n"
            f"Точный текст: {json.dumps(text, ensure_ascii=False)}"
        )

    def prepare(
        self,
        args: Any,
        *,
        owner_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        if not self.settings.outbound_replies_enabled:
            raise OutboundReplyError("outbound_replies_disabled")
        if str(owner_id or "") not in self.settings.owner_ids:
            raise OutboundReplyError("owner_session_not_authorized")
        normalized = self.normalize_args(args)
        if len(self.reference_key) < 32:
            raise OutboundReplyError("reference_key_unavailable")
        session_ref = self._session_ref(owner_id, session_id)
        message_hmac = self._message_hmac(normalized)
        record = self.archive.prepare_reply_intent(
            tenant_owner_id=owner_id,
            intent_id=self._intent_id_factory(),
            session_ref=session_ref,
            source_ref=normalized["source_ref"],
            message_hmac=message_hmac,
            reply_to_latest=normalized["reply_to_latest"],
            ttl_seconds=self.settings.outbound_intent_ttl_seconds,
        )
        state = str(record.get("state") or "")
        if state != "prepared":
            return {"state": state or "intent_not_prepared"}
        intent_id = str(record.get("intent_id") or "")
        if not intent_id:
            raise OutboundReplyError("invalid_intent_record")
        approval_key = self._approval_key(
            intent_id=intent_id,
            owner_id=owner_id,
            session_ref=session_ref,
            source_ref=normalized["source_ref"],
            message_hmac=message_hmac,
        )
        return {
            "state": "prepared",
            "intent_id": intent_id,
            "session_ref": session_ref,
            "message_hmac": message_hmac,
            "approval_key": approval_key,
            "display_target": self._display_target(
                chat_label=record.get("chat_label"),
                source_ref=normalized["source_ref"],
                text=normalized["text"],
            ),
            **normalized,
        }

    async def send(
        self,
        args: Any,
        *,
        owner_id: str,
        session_id: str,
        owner_resolver: OwnerResolver,
    ) -> str:
        if not self.available():
            return self._result(False, "outbound_replies_unavailable")
        try:
            prepared = self.prepare(
                args,
                owner_id=owner_id,
                session_id=session_id,
            )
        except OutboundReplyError as exc:
            return self._result(False, exc.code)
        except Exception:
            return self._result(False, "archive_unavailable")

        state = str(prepared.get("state") or "")
        if state == "sent":
            return self._result(True, "already_sent")
        if state != "prepared":
            return self._result(False, state or "intent_not_sendable")
        intent_id = prepared["intent_id"]
        approval_key = prepared["approval_key"]

        # Mandatory in-handler approval.  The pre-tool hook only rejects an
        # unauthorized session; it is deliberately not the approval boundary
        # because generic hook failures may be fail-open in Hermes core.
        try:
            approval = self._approval_requester(
                tool_name="passive_secretary_reply",
                reason=_APPROVAL_REASON,
                approval_key=approval_key,
                display_target=prepared["display_target"],
            )
            if inspect.isawaitable(approval):
                approval = await approval
        except Exception:
            self._finish_no_send(
                owner_id=owner_id,
                intent_id=intent_id,
                status="denied",
                failure_code="approval_error",
            )
            return self._result(False, "approval_failed")
        if not (
            isinstance(approval, dict)
            and set(approval) == {"approved", "choice", "message", "grant"}
            and approval.get("approved") is True
            and approval.get("choice") == "once"
            and approval.get("message") is None
            and approval.get("grant") is not None
        ):
            self._finish_no_send(
                owner_id=owner_id,
                intent_id=intent_id,
                status="denied",
                failure_code="approval_denied",
            )
            return self._result(False, "approval_denied")
        grant = approval.get("grant")

        # Re-resolve after the human wait. A non-owner observation, TTL expiry,
        # or session reuse revokes the authorization before any state claim.
        if owner_resolver() != owner_id:
            self._finish_no_send(
                owner_id=owner_id,
                intent_id=intent_id,
                status="denied",
                failure_code="owner_session_expired",
            )
            return self._result(False, "owner_session_not_authorized")

        try:
            intent = self.archive.claim_reply_intent(
                tenant_owner_id=owner_id,
                intent_id=intent_id,
                session_ref=prepared["session_ref"],
                source_ref=prepared["source_ref"],
                message_hmac=prepared["message_hmac"],
            )
        except Exception:
            return self._result(False, "archive_unavailable")
        claim_state = str(intent.get("state") or "")
        if claim_state == "sent":
            return self._result(True, "already_sent")
        if claim_state != "claimed":
            return self._result(False, claim_state or "intent_not_sendable")

        try:
            send_result = self._reply_sender(
                grant=grant,
                approval_key=approval_key,
                owner_id=owner_id,
                business_connection_id=intent["business_connection_id"],
                chat_id=int(intent["chat_id"]),
                text=prepared["text"],
                reply_message_id=intent.get("reply_message_id"),
            )
            if inspect.isawaitable(send_result):
                send_result = await send_result
        except Exception:
            self._finish_after_claim(
                owner_id=owner_id,
                intent_id=intent_id,
                status="ambiguous",
                failure_code="send_bridge_exception",
            )
            return self._result(False, "send_outcome_ambiguous")

        raw_status = getattr(send_result, "status", "")
        result_status = raw_status if isinstance(raw_status, str) else ""
        raw_error_code = getattr(send_result, "error_code", "")
        error_code = (
            raw_error_code
            if isinstance(raw_error_code, str)
            and _ERROR_CODE_RE.fullmatch(raw_error_code)
            else ""
        )
        if result_status == "failed_known":
            self._finish_after_claim(
                owner_id=owner_id,
                intent_id=intent_id,
                status="failed_known",
                failure_code=error_code or "send_failed_known",
            )
            return self._result(False, error_code or "send_failed_known")
        if result_status == "ambiguous":
            self._finish_after_claim(
                owner_id=owner_id,
                intent_id=intent_id,
                status="ambiguous",
                failure_code=error_code or "send_outcome_ambiguous",
            )
            return self._result(False, "send_outcome_ambiguous")
        message_id = getattr(send_result, "message_id", None)
        if result_status != "sent" or not isinstance(message_id, int) or message_id <= 0:
            self._finish_after_claim(
                owner_id=owner_id,
                intent_id=intent_id,
                status="ambiguous",
                failure_code="invalid_send_receipt",
            )
            return self._result(False, "send_outcome_ambiguous")
        try:
            persisted = self.archive.finish_reply_intent(
                tenant_owner_id=owner_id,
                intent_id=intent_id,
                status="sent",
                telegram_message_id=message_id,
            )
        except Exception:
            persisted = False
        if not persisted:
            # The network send succeeded but its durable receipt did not. Never
            # report success or retry; the lingering ``sending`` row is treated
            # as ambiguous by the next prepare/claim.
            return self._result(False, "send_outcome_ambiguous")
        return self._result(True, "sent")

    def _finish_no_send(
        self,
        *,
        owner_id: str,
        intent_id: str,
        status: str,
        failure_code: str,
    ) -> None:
        try:
            self.archive.finish_reply_intent(
                tenant_owner_id=owner_id,
                intent_id=intent_id,
                status=status,
                failure_code=failure_code,
            )
        except Exception:
            pass

    def _finish_after_claim(
        self,
        *,
        owner_id: str,
        intent_id: str,
        status: str,
        failure_code: str,
    ) -> None:
        try:
            self.archive.finish_reply_intent(
                tenant_owner_id=owner_id,
                intent_id=intent_id,
                status=status,
                failure_code=failure_code,
            )
        except Exception:
            pass

    @staticmethod
    def _result(ok: bool, code: str) -> str:
        # Never expose raw Telegram chat/connection/message ids, approval
        # grants, server intent ids, or stored HMACs to the model.
        return json.dumps(
            {"ok": ok, "status": code},
            ensure_ascii=False,
            sort_keys=True,
        )
