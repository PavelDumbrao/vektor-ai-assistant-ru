#!/usr/bin/env python3
"""Create a one-time Maton activation link for the current Telegram DM."""

from __future__ import annotations

import json
import os
import pwd
import sys
from pathlib import Path

from portal_store import SessionError, create_session


def main() -> int:
    settings_path = Path(__file__).with_name("settings.json")
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        expected_uid = pwd.getpwnam(settings["owner"]).pw_uid
        if os.geteuid() != expected_uid:
            raise SessionError("Генератор ссылки запущен не от пользователя клиента")
        platform = os.environ.get("HERMES_SESSION_PLATFORM", "")
        chat_type = os.environ.get("HERMES_SESSION_CHAT_TYPE", "")
        chat_id = os.environ.get("HERMES_SESSION_CHAT_ID", "")
        user_id = os.environ.get("HERMES_SESSION_USER_ID", "")
        if platform != "telegram" or chat_type != "dm":
            raise SessionError("Откройте личный чат с ботом и повторите запрос")
        token = create_session(
            settings["db_path"],
            chat_id=chat_id,
            user_id=user_id,
            ttl_seconds=int(settings["ttl_seconds"]),
        )
        base = settings["public_base_url"].rstrip("/")
        minutes = max(1, int(settings["ttl_seconds"]) // 60)
        print(f"ACTIVATION_URL={base}/connect/{token}")
        print(f"EXPIRES_IN_MINUTES={minutes}")
        print("Send only this activation URL to the current Telegram user.")
        return 0
    except (KeyError, OSError, ValueError, json.JSONDecodeError, SessionError) as exc:
        print(f"error={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
