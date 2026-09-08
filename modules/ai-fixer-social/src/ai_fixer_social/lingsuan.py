"""Bounded Lingsuan image generation with a durable at-most-once ledger."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import struct
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx


ALLOWED_BASES = {
    "https://lingsuan.top",
    "https://lingsuan.org",
    "https://edge.lingsuan.org",
}
MODEL = "gpt-image-2"
MODEL_NAME = "GPT Image 2"
PRICES_MILLI_CNY = {"1K": 100, "2K": 150, "4K": 268}
SIZE_MAP = {
    "1K": {
        "1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536",
        "16:9": "1280x720", "9:16": "720x1280", "4:3": "1024x768",
        "3:4": "768x1024", "21:9": "1280x544",
    },
    "2K": {
        "1:1": "2048x2048", "3:2": "2160x1440", "2:3": "1440x2160",
        "16:9": "2560x1440", "9:16": "1440x2560", "4:3": "2048x1536",
        "3:4": "1536x2048", "21:9": "2560x1088",
    },
    "4K": {
        "1:1": "2880x2880", "3:2": "3456x2304", "2:3": "2304x3456",
        "16:9": "3840x2160", "9:16": "2160x3840", "4:3": "3200x2400",
        "3:4": "2400x3200", "21:9": "3840x1600",
    },
}
CANONICAL_SHA = "166479dabe63bc5485ccac0040e1686ab202358944593b2ad10cbba75cc318ed"
MAX_IMAGE = 32 * 1024 * 1024


def now() -> str:
    return datetime.now(UTC).isoformat()


def image_metadata(raw: bytes) -> dict:
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 33:
        width, height = struct.unpack(">II", raw[16:24])
        kind, suffix = "image/png", ".png"
    elif raw.startswith(b"\xff\xd8\xff"):
        width = height = 0
        cursor = 2
        while cursor + 4 < len(raw):
            if raw[cursor] != 255:
                cursor += 1
                continue
            marker = raw[cursor + 1]
            cursor += 2
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            length = int.from_bytes(raw[cursor:cursor + 2], "big")
            if length < 2:
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3) and cursor + 7 <= len(raw):
                height, width = struct.unpack(">HH", raw[cursor + 3:cursor + 7])
                break
            cursor += length
        kind, suffix = "image/jpeg", ".jpg"
    else:
        raise ValueError("unsupported_result_image")
    if not 64 <= width <= 8192 or not 64 <= height <= 8192:
        raise ValueError("invalid_result_dimensions")
    return {
        "mime": kind, "suffix": suffix, "width": width, "height": height,
        "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
    }


def validate_result_url(url: str, *, resolve_dns: bool = True) -> None:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("result_host_not_allowed")
    if resolve_dns:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError("result_host_not_public")


def _clean_provider_error(value: object, key: str) -> str:
    message = str(value or "").replace(key, "<redacted>")
    return re.sub(r"https?://\S+|sk-[A-Za-z0-9_-]{16,}", "<redacted>", message)[:500]


def build_request(args: dict, reference: Path) -> tuple[dict, str, bytes | None]:
    if args.get("model", MODEL) != MODEL:
        raise ValueError("model_not_allowed")
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not 3 <= len(prompt.strip()) <= 6000:
        raise ValueError("invalid_prompt")
    request_id = args.get("request_id", "")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{3,95}", request_id):
        raise ValueError("invalid_request_id")
    size_class = args.get("image_size", "1K")
    aspect = args.get("aspect_ratio", "1:1")
    if size_class not in SIZE_MAP or aspect not in SIZE_MAP[size_class]:
        raise ValueError("unsupported_size_or_aspect")
    use_reference = args.get("use_pavel_reference", True)
    if type(use_reference) is not bool:
        raise ValueError("invalid_reference_flag")
    reference_raw = None
    reference_sha = None
    if use_reference:
        reference_raw = reference.read_bytes()
        reference_sha = hashlib.sha256(reference_raw).hexdigest()
        if reference_sha != CANONICAL_SHA:
            raise ValueError("canonical_reference_mismatch")
    payload = {
        "model": MODEL,
        "prompt": prompt.strip(),
        "size": SIZE_MAP[size_class][aspect],
        "quality": "auto",
        "output_format": "png",
        "moderation": "auto",
        "n": 1,
        "response_format": "b64_json",
    }
    signature = {**payload, "reference_sha256": reference_sha}
    fingerprint = hashlib.sha256(
        json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return payload, fingerprint, reference_raw


class LingsuanImage:
    def __init__(self, *, key: str, base_url: str, root: Path, reference: Path, client=None):
        base_url = base_url.rstrip("/")
        if base_url not in ALLOWED_BASES:
            raise ValueError("base_url_not_allowed")
        self.key, self.base_url, self.root, self.reference = key, base_url, root, reference
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(root / "jobs.sqlite3", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS jobs(
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, model TEXT NOT NULL,
                state TEXT NOT NULL, endpoint TEXT NOT NULL, path TEXT,
                expected_milli_cny INTEGER NOT NULL, requested_size TEXT NOT NULL,
                metadata_json TEXT, error_type TEXT, provider_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        self.db.commit()
        self.client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(330, connect=10),
            follow_redirects=False,
        )

    async def close(self) -> None:
        self.db.close()
        await self.client.aclose()

    def row(self, request_id: str) -> dict:
        row = self.db.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise ValueError("job_not_found")
        return dict(row)

    def update(self, request_id: str, **values) -> None:
        allowed = {"state", "path", "metadata_json", "error_type", "provider_error"}
        if set(values) - allowed:
            raise ValueError("invalid_job_update")
        values["updated_at"] = now()
        self.db.execute(
            "UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE request_id=?",
            (*values.values(), request_id),
        )
        self.db.commit()

    def public(self, request_id: str) -> dict:
        row = self.row(request_id)
        result = {key: row[key] for key in (
            "request_id", "model", "state", "endpoint", "expected_milli_cny", "error_type"
        )}
        result.update(
            ok=row["state"] not in ("rejected", "failed", "submit_uncertain"),
            provider="lingsuan", model_name=MODEL_NAME,
            expected_price_cny=row["expected_milli_cny"] / 1000,
            retry_submit_allowed=False, fingerprint=row["fingerprint"],
        )
        if row["metadata_json"]:
            result["image"] = json.loads(row["metadata_json"])
        if row["provider_error"]:
            result["provider_error"] = row["provider_error"]
            result["provider_error_is_untrusted_data"] = True
        if row["state"] == "submit_uncertain":
            result["reconciliation_required"] = True
        return result

    async def status(self) -> dict:
        response = await self.client.get("/v1/models")
        visible = False
        if response.status_code == 200:
            data = response.json()
            models = data.get("data", []) if isinstance(data, dict) else []
            visible = any(isinstance(item, dict) and item.get("id") == MODEL for item in models)
        return {
            "ok": response.status_code == 200 and visible,
            "provider": "lingsuan", "base_url": self.base_url,
            "credential_configured": bool(self.key), "model_visible": visible,
            "model_list_status": response.status_code, "default_model": MODEL,
            "model_choices": [{
                "id": MODEL, "name": MODEL_NAME,
                "generation_path": "/v1/images/generations",
                "edit_path": "/v1/images/edits",
                "prices_milli_cny": PRICES_MILLI_CNY,
                "sizes": list(PRICES_MILLI_CNY),
            }],
            "active_jobs": [dict(row) for row in self.db.execute(
                "SELECT request_id,state,model,endpoint FROM jobs WHERE state IN ('submitting','submit_uncertain')"
            )],
            "per_request_confirmation": True, "automatic_resubmit": False,
            "balance_api": "not_available_for_generation_key",
        }

    async def _download(self, url: str) -> bytes:
        validate_result_url(url)
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as downloader:
            async with downloader.stream("GET", url) as response:
                if response.status_code != 200:
                    raise ValueError("result_download_failed")
                chunks, total = [], 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_IMAGE:
                        raise ValueError("result_too_large")
                    chunks.append(chunk)
        return b"".join(chunks)

    async def _result_bytes(self, data: object) -> tuple[bytes, dict]:
        if not isinstance(data, dict) or not isinstance(data.get("data"), list) or not data["data"]:
            raise ValueError("image_result_missing")
        item = data["data"][0]
        if not isinstance(item, dict):
            raise ValueError("image_result_missing")
        if isinstance(item.get("b64_json"), str):
            try:
                raw = base64.b64decode(item["b64_json"], validate=True)
            except Exception:
                raise ValueError("invalid_result_base64") from None
            source = "b64_json"
        elif isinstance(item.get("url"), str):
            raw = await self._download(item["url"])
            source = "url"
        else:
            raise ValueError("image_result_missing")
        if len(raw) > MAX_IMAGE:
            raise ValueError("result_too_large")
        usage = data.get("usage")
        safe_usage = {
            str(key): value for key, value in usage.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        } if isinstance(usage, dict) else {}
        return raw, {"response_source": source, "provider_usage": safe_usage}

    async def submit(self, args: dict) -> dict:
        payload, fingerprint, reference_raw = build_request(args, self.reference)
        request_id = args["request_id"]
        expected = PRICES_MILLI_CNY[args.get("image_size", "1K")]
        if type(args.get("confirm_milli_cny")) is not int or args["confirm_milli_cny"] != expected:
            raise ValueError("exact_price_confirmation_required")
        previous = self.db.execute("SELECT fingerprint FROM jobs WHERE request_id=?", (request_id,)).fetchone()
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise ValueError("request_id_payload_mismatch")
            return self.public(request_id)
        endpoint = "/v1/images/edits" if reference_raw is not None else "/v1/images/generations"
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute(
                "SELECT 1 FROM jobs WHERE state IN ('submitting','submit_uncertain') LIMIT 1"
            ).fetchone():
                raise ValueError("another_job_requires_completion_or_reconciliation")
            self.db.execute(
                """INSERT INTO jobs(request_id,fingerprint,model,state,endpoint,
                   expected_milli_cny,requested_size,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (request_id, fingerprint, MODEL, "submitting", endpoint, expected,
                 payload["size"], now(), now()),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        started = time.perf_counter()
        try:
            if reference_raw is None:
                response = await self.client.post(endpoint, json=payload)
            else:
                response = await self.client.post(
                    endpoint,
                    data={key: str(value).lower() if isinstance(value, bool) else str(value)
                          for key, value in payload.items()},
                    files=[("image[]", ("reference.png", reference_raw, "image/png"))],
                )
            latency_ms = round((time.perf_counter() - started) * 1000, 1)
            if response.status_code != 200:
                try:
                    body = response.json()
                    provider_error = body.get("error") if isinstance(body, dict) else None
                except Exception:
                    provider_error = None
                self.update(
                    request_id,
                    state="rejected" if response.status_code in (400, 401, 403, 422, 429) else "submit_uncertain",
                    error_type=f"http_{response.status_code}",
                    provider_error=_clean_provider_error(provider_error, self.key),
                )
                return self.public(request_id)
            data = response.json()
            raw, extra = await self._result_bytes(data)
            metadata = {
                **image_metadata(raw), **extra, "requested_size": payload["size"],
                "latency_ms": latency_ms,
            }
            output = self.root / (hashlib.sha256(request_id.encode()).hexdigest()[:24] + metadata["suffix"])
            output.write_bytes(raw)
            output.chmod(0o600)
            self.update(
                request_id, state="downloaded", path=str(output),
                metadata_json=json.dumps(metadata, ensure_ascii=False),
                error_type=None, provider_error=None,
            )
        except Exception as exc:
            self.update(request_id, state="submit_uncertain", error_type=type(exc).__name__)
        return self.public(request_id)

    async def poll(self, request_id: str) -> dict:
        return self.public(request_id)

    async def fetch(self, request_id: str) -> dict:
        row = self.row(request_id)
        if row["state"] != "downloaded" or not row["path"]:
            return self.public(request_id)
        raw = Path(row["path"]).read_bytes()
        metadata = image_metadata(raw)
        stored = json.loads(row["metadata_json"])
        if metadata["sha256"] != stored["sha256"]:
            raise ValueError("downloaded_file_checksum_mismatch")
        return {**self.public(request_id), "image_b64": base64.b64encode(raw).decode()}


async def dispatch_media(data: dict) -> dict:
    key = os.environ.get("LINGSUAN_IMAGE_API_KEY", "")
    if not key:
        return {"ok": False, "error": "lingsuan_not_configured"}
    api = LingsuanImage(
        key=key,
        base_url=os.environ.get("LINGSUAN_IMAGE_BASE_URL", "https://lingsuan.top"),
        root=Path(os.environ["LINGSUAN_IMAGE_STATE_DIR"]),
        reference=Path(os.environ["LINGSUAN_IMAGE_REFERENCE_PATH"]),
    )
    try:
        if data["op"] == "image_status":
            return await api.status()
        if data["op"] == "image_submit":
            return await api.submit(data)
        if data["op"] == "image_poll":
            return await api.poll(data["request_id"])
        if data["op"] == "image_fetch":
            return await api.fetch(data["request_id"])
        raise ValueError("unknown_media_operation")
    finally:
        await api.close()
