from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Route:
    kind: str
    thread_id: int | None
    reason: str


@dataclass(frozen=True)
class Precheck:
    action: str
    risk: str
    reason: str


@dataclass(frozen=True)
class ReplyDecision:
    action: str
    risk: str
    confidence: float
    intent: str
    reason: str
    reply_text: str

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "ReplyDecision":
        action = str(data.get("action", "")).strip().lower()
        risk = str(data.get("risk", "")).strip().lower()
        if action not in {"reply", "ignore", "escalate"}:
            raise ValueError("Invalid decision action")
        if risk not in {"low", "medium", "high"}:
            raise ValueError("Invalid decision risk")
        try:
            confidence = float(data.get("confidence", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid confidence") from exc
        if not 0 <= confidence <= 1:
            raise ValueError("Confidence must be between 0 and 1")
        return cls(
            action=action,
            risk=risk,
            confidence=confidence,
            intent=str(data.get("intent", "")).strip()[:120],
            reason=str(data.get("reason", "")).strip()[:500],
            reply_text=str(data.get("reply_text", "")).strip(),
        )

