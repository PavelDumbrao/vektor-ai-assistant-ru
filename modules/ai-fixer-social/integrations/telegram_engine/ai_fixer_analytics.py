"""Read-only, fixed-channel analytics on the engine's existing Telethon session."""
from __future__ import annotations

import asyncio
import math
import time
from datetime import UTC, datetime


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def post_metrics(message, username):
    reactions = getattr(message, "reactions", None)
    counts = []
    for item in getattr(reactions, "results", None) or []:
        reaction = getattr(item, "reaction", None)
        label = getattr(reaction, "emoticon", None)
        if not label:
            label = "custom:" + str(reaction.document_id) if getattr(reaction, "document_id", None) else "other"
        counts.append({"reaction": label, "count": number(getattr(item, "count", None))})
    replies = getattr(message, "replies", None)
    return {"message_id": message.id, "date": message.date.isoformat(),
            "text_preview": str(message.message or "")[:500],
            "views": number(getattr(message, "views", None)), "forwards": number(getattr(message, "forwards", None)),
            "reactions": counts, "reaction_count": sum(item["count"] or 0 for item in counts),
            "comments": number(getattr(replies, "replies", None)),
            "grouped_id": str(message.grouped_id) if getattr(message, "grouped_id", None) else None,
            "url": f"https://t.me/{username}/{message.id}"}


async def collect(client, *, channel_id, username, limit):
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.utils import get_peer_id

    entity = await client.get_entity(channel_id)
    if get_peer_id(entity) != channel_id or not getattr(entity, "broadcast", False):
        raise ValueError("analytics_channel_identity_mismatch")
    if str(getattr(entity, "username", "")).lower() != username.lower():
        raise ValueError("analytics_channel_username_mismatch")
    full = await client(GetFullChannelRequest(entity))
    posts = [post_metrics(message, username) for message in await client.get_messages(entity, limit=limit)
             if getattr(message, "date", None) and not getattr(message, "action", None)]
    detailed = {"available": bool(getattr(full.full_chat, "can_view_stats", False)), "fetched": False}
    if detailed["available"]:
        try:
            # Telethon handles StatsMigrateError and the statistics DC internally.
            stats = await asyncio.wait_for(client.get_stats(entity), timeout=20)
            for key in ("followers", "views_per_post", "shares_per_post", "reactions_per_post"):
                value = getattr(stats, key, None)
                detailed[key] = {"current": number(getattr(value, "current", None)),
                                 "previous": number(getattr(value, "previous", None))}
            period = getattr(stats, "period", None)
            detailed["period"] = {key: value.isoformat() if hasattr(value, "isoformat") else value
                                  for key in ("min_date", "max_date") if (value := getattr(period, key, None)) is not None}
            detailed["fetched"] = True
        except Exception as exc:
            detailed["error_type"] = type(exc).__name__
    return {"ok": True, "channel_id": channel_id, "channel": username,
            "observed_at": datetime.now(UTC).isoformat(), "sample_limit": limit,
            "subscriber_count": number(getattr(full.full_chat, "participants_count", None)),
            "posts": posts, "detailed_stats": detailed,
            "scope": "fixed_channel_public_metrics_no_individual_voters_or_reactors"}


def register(app, verify_token, get_client):
    """No auth/bind changes. Targets come from the existing protected social env."""
    from fastapi import Depends, HTTPException, Query
    from dotenv import dotenv_values

    config = dotenv_values("/etc/ai-fixer-social.env")
    channel_id = int(config["TELEGRAM_CHANNEL_ID"])
    username = config["TELEGRAM_CHANNEL_USERNAME"].lstrip("@")
    cache = {}
    lock = asyncio.Lock()

    @app.get("/api/ai-fixer/analytics")
    async def analytics(limit: int = Query(default=20, ge=1, le=20), token: str = Depends(verify_token)):
        async with lock:
            now = time.monotonic()
            if limit in cache and now - cache[limit][0] < 300:
                return {**cache[limit][1], "cached": True}
            try:
                result = await asyncio.wait_for(collect(await get_client(), channel_id=channel_id, username=username, limit=limit), timeout=45)
            except Exception as exc:
                raise HTTPException(status_code=503, detail={"error": "channel_analytics_unavailable", "error_type": type(exc).__name__}) from None
            cache[limit] = (now, result)
            return {**result, "cached": False}
