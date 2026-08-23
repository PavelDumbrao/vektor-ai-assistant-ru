"""Deterministic current-turn provenance for Telegram Business replies.

The model may describe or propose a reply, but it may not even enter the
human-approval flow unless the current raw owner DM starts with an explicit
command prefix.  This gate is deliberately independent from natural-language
classification: a fixed prefix is the only auditable way to prove that the
owner initiated the send flow.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


COMMAND_PREFIXES = ("ОТПРАВИТЬ:", "ОТВЕТИТЬ:")


def _is_explicit_owner_command(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    # Do not trim leading whitespace: the command must be visibly at the very
    # beginning of the owner's current Telegram message, not inside a quote or
    # pasted archive fragment.
    folded = value.casefold()
    for prefix in COMMAND_PREFIXES:
        marker = prefix.casefold()
        if folded.startswith(marker) and value[len(prefix) :].strip():
            return True
    return False


def _args_fingerprint(args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    try:
        encoded = json.dumps(
            args,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError):
        return None
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class _TurnPermit:
    owner_id: str
    turn_id: str
    expires_at: float


@dataclass(frozen=True)
class _HandlerPermit:
    owner_id: str
    args_fingerprint: str
    expires_at: float


class OwnerReplyIntentGate:
    """Two-stage, single-use gate bound to owner, session, turn and arguments.

    ``observe_turn`` sees the trusted raw user message in ``pre_llm_call``.
    ``stage_tool_call`` runs in ``pre_tool_call`` and consumes the turn permit.
    The actual tool handler must then consume the staged argument-bound permit.
    If any hook is skipped or fails open, the handler still fails closed.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 120,
        monotonic_fn: Callable[[], float] | None = None,
    ) -> None:
        if not 5 <= int(ttl_seconds) <= 600:
            raise ValueError("ttl_seconds must be between 5 and 600")
        self.ttl_seconds = int(ttl_seconds)
        self._monotonic_fn = monotonic_fn or time.monotonic
        self._turn_permits: dict[str, _TurnPermit] = {}
        self._handler_permits: dict[str, _HandlerPermit] = {}
        self._lock = threading.Lock()

    def observe_turn(
        self,
        *,
        owner_id: Any,
        session_id: Any,
        turn_id: Any,
        raw_user_message: Any,
        is_internal_event: Any,
    ) -> bool:
        session_key = str(session_id or "")
        owner_key = str(owner_id or "")
        turn_key = str(turn_id or "")
        now = self._monotonic_fn()
        with self._lock:
            self._prune_locked(now)
            if session_key:
                # Every new observed turn revokes unused provenance from the
                # previous one, including a previously staged handler permit.
                self._turn_permits.pop(session_key, None)
                self._handler_permits.pop(session_key, None)
            if not (
                session_key
                and owner_key
                and turn_key
                # Only a real external gateway event can mint provenance.
                # ``is False`` intentionally rejects None, truthy aliases,
                # integers, and every caller that lacks the new core contract.
                and is_internal_event is False
                and _is_explicit_owner_command(raw_user_message)
            ):
                return False
            self._turn_permits[session_key] = _TurnPermit(
                owner_id=owner_key,
                turn_id=turn_key,
                expires_at=now + self.ttl_seconds,
            )
            return True

    def stage_tool_call(
        self,
        *,
        owner_id: Any,
        session_id: Any,
        turn_id: Any,
        args: Any,
    ) -> bool:
        session_key = str(session_id or "")
        owner_key = str(owner_id or "")
        turn_key = str(turn_id or "")
        fingerprint = _args_fingerprint(args)
        now = self._monotonic_fn()
        with self._lock:
            self._prune_locked(now)
            permit = self._turn_permits.pop(session_key, None)
            if (
                permit is None
                or permit.owner_id != owner_key
                or permit.turn_id != turn_key
                or permit.expires_at <= now
                or fingerprint is None
            ):
                return False
            self._handler_permits[session_key] = _HandlerPermit(
                owner_id=owner_key,
                args_fingerprint=fingerprint,
                expires_at=now + self.ttl_seconds,
            )
            return True

    def consume_for_handler(
        self,
        *,
        owner_id: Any,
        session_id: Any,
        args: Any,
    ) -> bool:
        session_key = str(session_id or "")
        owner_key = str(owner_id or "")
        fingerprint = _args_fingerprint(args)
        now = self._monotonic_fn()
        with self._lock:
            self._prune_locked(now)
            permit = self._handler_permits.pop(session_key, None)
            return bool(
                permit is not None
                and permit.owner_id == owner_key
                and permit.args_fingerprint == fingerprint
                and permit.expires_at > now
            )

    def revoke(self, session_id: Any) -> None:
        session_key = str(session_id or "")
        if not session_key:
            return
        with self._lock:
            self._turn_permits.pop(session_key, None)
            self._handler_permits.pop(session_key, None)

    def _prune_locked(self, now: float) -> None:
        for mapping in (self._turn_permits, self._handler_permits):
            expired = [
                key for key, permit in mapping.items() if permit.expires_at <= now
            ]
            for key in expired:
                mapping.pop(key, None)
