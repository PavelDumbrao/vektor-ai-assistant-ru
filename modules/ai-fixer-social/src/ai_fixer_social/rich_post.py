"""Restricted native Telegram longreads with local photo slideshows."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit


MAX_ARTICLE_CHARS = 32768
MAX_HTML_BYTES = 128 * 1024
MAX_TOTAL_PHOTO_BYTES = 9 * 1024 * 1024
TAGS = {"h1", "h2", "h3", "p", "br", "b", "strong", "i", "em", "u", "s",
        "a", "blockquote", "code", "pre", "ul", "ol", "li", "details", "summary",
        "tg-slideshow", "figure", "img", "figcaption", "hr"}
VOID = {"img", "br", "hr"}


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []
        self.image_ids = []
        self.slideshows = []
        self.slide_start = None
        self.elements = 0

    def handle_starttag(self, tag, attrs):
        if tag not in TAGS or self.elements >= 100 or len(self.stack) >= 10:
            raise ValueError("unsupported_article_tag_or_structure_limit")
        self.elements += 1
        allowed = {"img": {"src"}, "a": {"href"}, "details": {"open"}}.get(tag, set())
        values = dict(attrs)
        if len(values) != len(attrs) or set(values) - allowed:
            raise ValueError("unsupported_article_attribute")
        if tag == "a":
            url = urlsplit(values.get("href") or "")
            if url.scheme != "https" or not url.netloc or url.username or url.password:
                raise ValueError("article_link_requires_https")
        if tag == "img":
            match = re.fullmatch(r"tg://photo\?id=([A-Za-z0-9_-]{1,64})", values.get("src") or "")
            if not match:
                raise ValueError("article_media_must_use_local_photo_id")
            self.image_ids.append(match.group(1))
        if tag == "tg-slideshow":
            if self.slide_start is not None:
                raise ValueError("nested_slideshow_forbidden")
            self.slide_start = len(self.image_ids)
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID or not self.stack or self.stack.pop() != tag:
            raise ValueError("unbalanced_article_html")
        if tag == "tg-slideshow":
            count = len(self.image_ids) - self.slide_start
            if not 2 <= count <= 10:
                raise ValueError("slideshow_requires_2_to_10_photos")
            self.slideshows.append(count)
            self.slide_start = None

    def handle_data(self, data):
        self.text.append(data)

    def handle_comment(self, data):
        raise ValueError("article_comments_forbidden")

    def handle_decl(self, decl):
        raise ValueError("article_declarations_forbidden")

    def handle_pi(self, data):
        raise ValueError("article_processing_instructions_forbidden")


@dataclass
class RichPost:
    html: str
    photos: list
    payload_hash: str
    visible_chars: int
    slideshows: list

    def wire(self):
        return {"html": self.html, "media": [
            {"id": photo["id"], "media": {"type": "photo", "media": f"attach://photo_{index}"}}
            for index, photo in enumerate(self.photos)
        ]}

    def files(self):
        return {f"photo_{index}": (f"photo_{index}{photo['suffix']}", photo["raw"], photo["mime"])
                for index, photo in enumerate(self.photos)}

    def summary(self):
        return {"format": "native_rich_message", "visible_chars": self.visible_chars,
                "photo_count": len(self.photos), "slideshow_sizes": self.slideshows,
                "payload_hash": self.payload_hash}


def prepare_article(data) -> RichPost:
    if not isinstance(data, dict) or set(data) != {"html", "photos"}:
        raise ValueError("article_requires_html_and_photos")
    content = data["html"]
    if not isinstance(content, str) or not content.strip() or len(content.encode()) > MAX_HTML_BYTES:
        raise ValueError("article_html_exceeds_128_kib_or_empty")
    if not isinstance(data["photos"], list) or len(data["photos"]) > 10:
        raise ValueError("article_max_10_photos")
    photos = []
    total = 0
    for item in data["photos"]:
        if not isinstance(item, dict) or set(item) != {"id", "data"}:
            raise ValueError("invalid_article_photo_fields")
        if not isinstance(item["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["id"]):
            raise ValueError("invalid_article_photo_id")
        if not isinstance(item["data"], str) or len(item["data"]) > MAX_TOTAL_PHOTO_BYTES * 4 // 3 + 8:
            raise ValueError("article_photos_too_large")
        try:
            raw = base64.b64decode(item["data"], validate=True)
        except Exception:
            raise ValueError("invalid_article_photo_base64") from None
        total += len(raw)
        if total > MAX_TOTAL_PHOTO_BYTES:
            raise ValueError("article_photos_total_exceeds_9_mib")
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix, mime = ".png", "image/png"
        elif raw.startswith(b"\xff\xd8\xff"):
            suffix, mime = ".jpg", "image/jpeg"
        else:
            raise ValueError("article_photo_requires_png_or_jpeg")
        photos.append({"id": item["id"], "raw": raw, "suffix": suffix, "mime": mime})
    ids = [item["id"] for item in photos]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate_article_photo_id")
    parser = ArticleParser()
    parser.feed(content)
    parser.close()
    if parser.stack:
        raise ValueError("unbalanced_article_html")
    text = "".join(parser.text)
    if len(text) > MAX_ARTICLE_CHARS:
        raise ValueError("article_text_exceeds_32768_characters")
    if not text.strip() or any(char in text for char in ("—", "–")):
        raise ValueError("empty_article_or_long_dash")
    if set(parser.image_ids) != set(ids):
        raise ValueError("article_photo_reference_mismatch")
    canonical = {"format": "rich_message_v1", "html": content,
                 "photos": [{"id": p["id"], "sha256": hashlib.sha256(p["raw"]).hexdigest()} for p in photos]}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return RichPost(content, photos, digest, len(text), parser.slideshows)
