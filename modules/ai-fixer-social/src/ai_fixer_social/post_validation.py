from __future__ import annotations

import html
import re
from html.parser import HTMLParser


ALLOWED_TAGS = {
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ins",
    "s",
    "strike",
    "del",
    "tg-spoiler",
    "code",
    "pre",
    "a",
    "blockquote",
}


class _TelegramHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.visible: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"unsupported_tag:{tag}")
            return
        values = dict(attrs)
        if tag == "a":
            href = values.get("href") or ""
            if not re.match(r"^(https?|tg)://", href):
                self.errors.append("unsafe_link")
        elif tag == "blockquote":
            unexpected = {key for key in values if key != "expandable"}
            if unexpected:
                self.errors.append("unsupported_blockquote_attribute")
        elif attrs:
            self.errors.append(f"unsupported_attributes:{tag}")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in ALLOWED_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced_tag:{tag}")
            return
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.visible.append(data)

    def handle_entityref(self, name: str) -> None:
        self.visible.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.visible.append(f"&#{name};")

    def close(self) -> None:
        super().close()
        if self.stack:
            self.errors.append("unclosed_tags:" + ",".join(self.stack))


def visible_text(text: str) -> str:
    parser = _TelegramHtmlParser()
    parser.feed(text)
    parser.close()
    return html.unescape("".join(parser.visible))


def validate_post(text: str, *, has_photo: bool = False) -> list[str]:
    errors: list[str] = []
    if not text.strip():
        return ["empty_text"]
    if "—" in text or "–" in text:
        errors.append("long_dash_forbidden")
    if "<script" in text.lower() or "javascript:" in text.lower():
        errors.append("unsafe_html")

    parser = _TelegramHtmlParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        errors.append("html_parse_error")
    errors.extend(parser.errors)

    length = len(html.unescape("".join(parser.visible)))
    limit = 1024 if has_photo else 4096
    if length > limit:
        errors.append(f"visible_text_too_long:{length}>{limit}")
    return sorted(set(errors))
