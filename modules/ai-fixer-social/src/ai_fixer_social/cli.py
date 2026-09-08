from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .config import Settings
from .db import StateStore
from .llm import LlmClient
from .outbound import publish_post
from .policy import enforce_reply_policy, precheck_comment
from .post_validation import validate_post, visible_text
from .telegram import TelegramClient


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


async def _status(settings: Settings) -> int:
    telegram = TelegramClient(settings.telegram_bot_token)
    store = StateStore(settings.state_db)
    try:
        me = await telegram.get_me()
        member = await telegram.get_chat_member(settings.telegram_discussion_id, int(me["id"]))
        print(
            json.dumps(
                {
                    "ok": True,
                    "bot": {"id": me.get("id"), "username": me.get("username")},
                    "discussion_status": member.get("status"),
                    "comment_mode": settings.comment_mode,
                    "state": store.stats(),
                    "next_update_offset": store.get_int("telegram_update_offset"),
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        store.close()
        await telegram.close()


async def _publish(args: argparse.Namespace, settings: Settings) -> int:
    text = _read_text(args.text_file)
    photo = Path(args.photo) if args.photo else None
    store = StateStore(settings.state_db)
    telegram = TelegramClient(settings.telegram_bot_token)
    try:
        result = await publish_post(settings, store, telegram, request_id=args.request_id,
                                    text=text, photo=photo, dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 2
    finally:
        store.close()
        await telegram.close()


async def _evaluate_comment(args: argparse.Namespace, settings: Settings) -> int:
    text = _read_text(args.text_file).strip()
    precheck = precheck_comment(text)
    if precheck.action != "continue":
        print(
            json.dumps(
                {
                    "ok": True,
                    "stage": "precheck",
                    "action": precheck.action,
                    "risk": precheck.risk,
                    "reason": precheck.reason,
                },
                ensure_ascii=False,
            )
        )
        return 0
    policy = settings.editorial_policy.read_text(encoding="utf-8")
    llm = LlmClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_comment_model,
        editorial_policy=policy,
    )
    try:
        decision = await llm.decide_reply(
            comment=text,
            post_context=args.post_context or "",
            thread_context=[],
            direct_mention=args.direct_mention,
            runtime_state={
                "reads_new_channel_comments": True,
                "automatic_low_risk_replies_enabled": settings.comment_mode == "live",
                "general_group_chat_is_ignored": True,
                "sensitive_topics_are_escalated": True,
                "can_delete_or_ban_users": False,
            },
        )
        decision = enforce_reply_policy(
            decision,
            min_confidence=settings.min_reply_confidence,
            max_chars=settings.max_reply_chars,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "stage": "model_and_policy",
                    "action": decision.action,
                    "risk": decision.risk,
                    "confidence": decision.confidence,
                    "intent": decision.intent,
                    "reason": decision.reason,
                    "reply_text": decision.reply_text,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        await llm.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-fixer-social")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate-post")
    validate.add_argument("--text-file", required=True)
    validate.add_argument("--photo", action="store_true")

    publish = sub.add_parser("publish")
    publish.add_argument("--request-id", required=True)
    publish.add_argument("--text-file", required=True)
    publish.add_argument("--photo")
    publish.add_argument("--dry-run", action="store_true")

    evaluate = sub.add_parser("evaluate-comment")
    evaluate.add_argument("--text-file", required=True)
    evaluate.add_argument("--post-context")
    evaluate.add_argument("--direct-mention", action="store_true")

    sub.add_parser("status")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "validate-post":
        text = _read_text(args.text_file)
        errors = validate_post(text, has_photo=args.photo)
        print(
            json.dumps(
                {
                    "ok": not errors,
                    "errors": errors,
                    "visible_chars": len(visible_text(text)),
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(0 if not errors else 2)

    settings = Settings.from_env()
    if args.command == "status":
        raise SystemExit(asyncio.run(_status(settings)))
    if args.command == "publish":
        raise SystemExit(asyncio.run(_publish(args, settings)))
    if args.command == "evaluate-comment":
        raise SystemExit(asyncio.run(_evaluate_comment(args, settings)))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
