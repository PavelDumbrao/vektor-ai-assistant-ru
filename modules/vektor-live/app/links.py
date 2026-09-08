"""Одноразовые ссылки на голосовую сессию.

ВЕКТОР (Hermes) просит у сервиса ссылку и присылает её Павлу в Telegram.
Ссылка живёт ограниченное время, привязывается к первому открывшему её браузеру
и не даёт доступа ни к чему, кроме самой голосовой сессии.
"""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from . import config


@dataclass
class VoiceLink:
    token: str
    created_at: float
    expires_at: float
    note: str = ""
    used_at: float = 0.0
    client_hint: str = ""
    sessions: int = 0

    @property
    def alive(self) -> bool:
        return time.time() < self.expires_at


class LinkStore:
    """Хранилище ссылок с атомарной записью на диск."""

    def __init__(self) -> None:
        self.path = config.STATE_DIR / "voice_links.json"
        self.links: dict[str, VoiceLink] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for item in raw.get("links", []):
                    link = VoiceLink(**item)
                    if link.alive:
                        self.links[link.token] = link
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        try:
            config.STATE_DIR.mkdir(parents=True, exist_ok=True)
            alive = [asdict(l) for l in self.links.values() if l.alive][-50:]
            fd, tmp = tempfile.mkstemp(dir=str(config.STATE_DIR), prefix=".links", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"links": alive}, fh, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def purge(self) -> None:
        dead = [t for t, l in self.links.items() if not l.alive]
        for t in dead:
            self.links.pop(t, None)
        if dead:
            self._save()

    def create(self, minutes: Optional[int] = None, note: str = "") -> VoiceLink:
        self.purge()
        ttl = (minutes or config.LINK_TTL_MINUTES) * 60
        link = VoiceLink(
            token=secrets.token_urlsafe(18),
            created_at=time.time(),
            expires_at=time.time() + ttl,
            note=note.strip()[:200],
        )
        self.links[link.token] = link
        self._save()
        return link

    def check(self, token: str) -> Optional[VoiceLink]:
        link = self.links.get(token or "")
        if not link or not link.alive:
            return None
        return link

    def mark_open(self, token: str, client_hint: str = "") -> Optional[VoiceLink]:
        link = self.check(token)
        if not link:
            return None
        if not link.used_at:
            link.used_at = time.time()
            link.client_hint = client_hint[:120]
        link.sessions += 1
        self._save()
        return link

    def url_for(self, link: VoiceLink) -> str:
        base = config.PUBLIC_BASE_URL.rstrip("/")
        return f"{base}/?k={link.token}"
