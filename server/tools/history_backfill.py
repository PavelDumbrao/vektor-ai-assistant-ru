#!/usr/bin/env python3
"""Import Telegram Desktop JSON history into one Passive Secretary profile."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERMES: Path
IMPORT_ROOT: Path
SETTINGS_PATH: Path
ENV_PATH: Path
OWNER_ID: str

REDACTED_BODY = '[REDACTED: message contained credentials or secret access]'
SECRET_PATTERNS = (
    re.compile(r'\b\d{7,12}:[A-Za-z0-9_-]{30,}\b'),
    re.compile(r'\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b'),
    re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'),
    re.compile(r'(?i)(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|secret|парол[ья]|password|логин|login)\s*[:=]?\s*\S+'),
    re.compile(r'(?i)https?://\S+[?&](?:pwd|token|key|secret|auth)=[^&\s]+'),
)
SENSITIVE_HINT_RE = re.compile(
    r'(?i)\b(?:парол[ья]|password|api[_ -]?key|секрет|secret|токен|token|логин|login|доступы?|access(?:\s+key)?)\b'
)
EMAIL_WITH_SECRET_RE = re.compile(
    r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b\s+\S{8,}'
)
PAYMENT_REQUISITE_RE = re.compile(r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)')


def explicit_secret(text: str) -> bool:
    return bool(
        SENSITIVE_HINT_RE.search(text)
        or EMAIL_WITH_SECRET_RE.search(text)
        or any(pattern.search(text) for pattern in SECRET_PATTERNS)
    )


def looks_like_credential_payload(text: str) -> bool:
    value = text.strip()
    if EMAIL_WITH_SECRET_RE.search(value):
        return True
    if len(value) > 180:
        return False
    parts = value.split()
    if len(parts) not in (1, 2, 3):
        return False
    if not any('@' in part and '.' in part for part in parts):
        return False
    return any(
        len(part) >= 8
        and re.search(r'[A-Za-z]', part)
        and re.search(r'[^A-Za-z0-9@._+-]', part)
        for part in parts
    )


def mask_payment_requisites(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        digits = re.sub(r'\D', '', match.group(0))
        return '[PAYMENT_REQUISITE_REDACTED]' if len(digits) >= 13 else match.group(0)

    return PAYMENT_REQUISITE_RE.sub(replace, text)


def sanitized_message_bodies(messages: list[Any]) -> dict[int, str]:
    bodies: dict[int, str] = {}
    protect_until = -1
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        raw = flatten_text(message.get('text')).strip()
        if SENSITIVE_HINT_RE.search(raw):
            protect_until = max(protect_until, index + 3)
        if explicit_secret(raw) or (index <= protect_until and looks_like_credential_payload(raw)):
            bodies[index] = REDACTED_BODY
        else:
            bodies[index] = mask_payment_requisites(raw)
    return bodies


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get('text') or ''))
        return ''.join(parts)
    return ''


def parse_date(message: dict[str, Any]) -> datetime | None:
    raw_unix = message.get('date_unixtime')
    if str(raw_unix or '').isdigit():
        return datetime.fromtimestamp(int(raw_unix), tz=timezone.utc)
    raw = message.get('date')
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def iter_chats(data: dict[str, Any], source: Path):
    chats = data.get('chats')
    if isinstance(chats, dict) and isinstance(chats.get('list'), list):
        for chat in chats['list']:
            if not isinstance(chat, dict):
                continue
            messages = chat.get('messages')
            if isinstance(messages, list):
                label = str(chat.get('name') or chat.get('title') or 'Telegram chat')
                raw_id = str(chat.get('id') or label)
                yield label, raw_id, messages
        return
    messages = data.get('messages')
    if isinstance(messages, list):
        label = str(data.get('name') or data.get('title') or source.stem)
        raw_id = str(data.get('id') or label)
        yield label, raw_id, messages
        return
    raise ValueError('unsupported_telegram_export_shape')


def opaque_ref(key: bytes, settings: dict[str, Any], namespace: str, raw: str) -> str:
    payload = (
        f"{settings['tenant_id']}\0{OWNER_ID}\0{settings['source_id']}\0"
        f"{namespace}\0{raw}"
    ).encode('utf-8', 'strict')
    digest = hmac.new(key, payload, hashlib.sha256).hexdigest()[:20]
    return f'{namespace}:{digest}'


def synthetic_chat_id(key: bytes, raw: str) -> int:
    digest = hmac.new(key, ('chat-id\0' + raw).encode(), hashlib.sha256).digest()
    return -(2**61 + int.from_bytes(digest[:8], 'big') % (2**60))


def sender_id(raw: Any) -> int | None:
    match = re.fullmatch(r'user(\d+)', str(raw or ''))
    return int(match.group(1)) if match else None


def safe_label(value: Any, fallback: str) -> str:
    text = ' '.join(str(value or fallback).split())
    return text[:160]


def normalize_export(path: Path, data: dict[str, Any], key: bytes, settings: dict[str, Any]):
    rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for label, raw_chat_id, messages in iter_chats(data, path):
        chat_key = f'history:{raw_chat_id}:{label}'
        chat_id = synthetic_chat_id(key, chat_key)
        source_ref = opaque_ref(key, settings, 'chat', chat_key)
        chat_rows: list[dict[str, Any]] = []
        sanitized_bodies = sanitized_message_bodies(messages)
        for message_index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get('type') not in (None, 'message'):
                continue
            mid = message.get('id')
            if isinstance(mid, bool) or not isinstance(mid, int) or mid < 0:
                continue
            raw_sender = message.get('from_id') or message.get('from') or 'unknown'
            sid = sender_id(raw_sender)
            body = sanitized_bodies.get(message_index, '').strip() or None
            sent_at = parse_date(message)
            reply_id = message.get('reply_to_message_id')
            if isinstance(reply_id, bool) or not isinstance(reply_id, int) or reply_id < 0:
                reply_id = None
            row = {
                'chat_id': chat_id,
                'message_id': mid,
                'source_ref': source_ref,
                'chat_label': safe_label(label, 'Telegram chat'),
                'message_ref': opaque_ref(key, settings, 'message', f'{chat_key}:{mid}'),
                'reply_to_message_id': reply_id,
                'reply_to_message_ref': (
                    opaque_ref(key, settings, 'message', f'{chat_key}:{reply_id}')
                    if reply_id is not None else None
                ),
                'sender_telegram_user_id': sid,
                'sender_ref': opaque_ref(key, settings, 'sender', str(raw_sender)),
                'sender_label': safe_label(message.get('from'), 'Telegram contact'),
                'direction': 'outgoing' if str(sid or '') == OWNER_ID else 'incoming',
                'body': body,
                'content_kind': 'text' if body else 'other',
                'sent_at': sent_at,
            }
            chat_rows.append(row)
            rows.append(row)
        dates = [row['sent_at'] for row in chat_rows if row['sent_at'] is not None]
        summaries.append({
            'label': safe_label(label, 'Telegram chat'),
            'source_ref': source_ref,
            'messages': len(chat_rows),
            'date_from': min(dates).isoformat() if dates else None,
            'date_to': max(dates).isoformat() if dates else None,
        })
    return rows, summaries


INSERT_SQL = """
INSERT INTO passive_secretary.messages (
 tenant_id, tenant_owner_id, source_id, test_run_id,
 chat_id, message_id, business_connection_id, source_ref, chat_label,
 message_ref, reply_to_message_id, reply_to_message_ref,
 sender_telegram_user_id, sender_ref, sender_label, direction,
 body, caption, content_kind, attachment, sent_at, edited_at,
 is_deleted, ingest_origin, last_update_id, updated_at
) VALUES (
 %s,%s,%s,%s,%s,%s,'history-backfill-v1',%s,%s,%s,%s,%s,
 %s,%s,%s,%s,%s,NULL,%s,'{}'::jsonb,%s,NULL,FALSE,'history_backfill',0,NOW()
)
ON CONFLICT (tenant_id, tenant_owner_id, source_id, test_run_id, chat_id, message_id)
DO UPDATE SET source_ref=excluded.source_ref, chat_label=excluded.chat_label,
 message_ref=excluded.message_ref, reply_to_message_id=excluded.reply_to_message_id,
 reply_to_message_ref=excluded.reply_to_message_ref,
 sender_telegram_user_id=excluded.sender_telegram_user_id,
 sender_ref=excluded.sender_ref, sender_label=excluded.sender_label,
 direction=excluded.direction, body=excluded.body,
 content_kind=excluded.content_kind, sent_at=excluded.sent_at,
 ingest_origin='history_backfill', updated_at=NOW()
"""


def apply_rows(rows: list[dict[str, Any]], settings: dict[str, Any], env: dict[str, str], test_run_id: str) -> int:
    import psycopg
    dsn = env[settings['postgres_dsn_env']]
    inserted = 0
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            for row in rows:
                cur.execute(INSERT_SQL, (
                    settings['tenant_id'], OWNER_ID, settings['source_id'], test_run_id,
                    row['chat_id'], row['message_id'], row['source_ref'], row['chat_label'],
                    row['message_ref'], row['reply_to_message_id'], row['reply_to_message_ref'],
                    row['sender_telegram_user_id'], row['sender_ref'], row['sender_label'],
                    row['direction'], row['body'], row['content_kind'], row['sent_at'],
                ))
                inserted += 1
        conn.commit()
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-home', required=True)
    parser.add_argument('--file', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--test-run-id', default='')
    args = parser.parse_args()

    global HERMES, IMPORT_ROOT, SETTINGS_PATH, ENV_PATH, OWNER_ID
    HERMES = Path(args.hermes_home).expanduser().resolve()
    if HERMES.name != '.hermes' or not HERMES.is_dir():
        raise SystemExit('invalid_hermes_home')
    IMPORT_ROOT = (HERMES.parent / 'workspace' / 'imports').resolve()
    SETTINGS_PATH = HERMES / 'plugins/passive-secretary/settings.json'
    ENV_PATH = HERMES / '.env'
    source = Path(args.file).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != '.json':
        raise SystemExit('invalid_export_file')
    if not source.is_relative_to(IMPORT_ROOT):
        raise SystemExit('export_must_be_inside_workspace_imports')
    settings = json.loads(SETTINGS_PATH.read_text(encoding='utf-8'))
    env = load_env(ENV_PATH)
    owners = settings.get('owner_telegram_user_ids')
    if not isinstance(owners, list) or len(owners) != 1 or not str(owners[0]).isdigit():
        raise SystemExit('profile_must_have_exactly_one_owner')
    OWNER_ID = str(owners[0])
    key_name = settings['source_ref_key_env']
    key_value = env.get(key_name, '')
    if len(key_value) < 32:
        raise SystemExit('source_reference_key_missing')
    key = key_value.encode('utf-8', 'strict')

    data = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise SystemExit('telegram_export_must_be_object')
    rows, summaries = normalize_export(source, data, key, settings)
    print(json.dumps({'dry_run': not args.apply, 'chats': summaries, 'messages': len(rows)}, ensure_ascii=False, indent=2))
    if not args.apply:
        return 0
    if not rows:
        raise SystemExit('nothing_to_import')
    count = apply_rows(rows, settings, env, args.test_run_id)
    print(json.dumps({'applied': True, 'rows': count, 'test_run_id': args.test_run_id}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
