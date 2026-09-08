"""Bounded GRSAI submit/poll/download with a durable at-most-once ledger."""
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
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx


BASE = "https://grsaiapi.com"
MODELS = {"gpt-image-2": 600, "nano-banana-2": 1200}
MODEL_NAMES = {"gpt-image-2": "GPT Image 2", "nano-banana-2": "Nano Banana 2"}
MODEL_CONTRACTS = {"gpt-image-2": "unified", "nano-banana-2": "nano_legacy"}
CONTRACT_ROUTES = {
    "unified": {"submit_path": "/v1/api/generate", "poll_method": "GET", "poll_path": "/v1/api/result"},
    "nano_legacy": {"submit_path": "/v1/draw/nano-banana", "poll_method": "POST", "poll_path": "/v1/draw/result"},
}
ASPECTS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "4:5", "5:4", "21:9"}
CANONICAL_SHA = "166479dabe63bc5485ccac0040e1686ab202358944593b2ad10cbba75cc318ed"
MAX_IMAGE = 32 * 1024 * 1024
CDN_DOMAINS = ("aitohumanize.com", "grsai-resource.com", "grsai.com", "grsai.ai")
ACTIVE = ("submitting", "running", "submit_uncertain")


def now():
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
    return {"mime": kind, "suffix": suffix, "width": width, "height": height,
            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def validate_result_url(url: str, *, resolve_dns=True):
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not any(hostname == host or hostname.endswith("." + host) for host in CDN_DOMAINS)):
        raise ValueError("result_host_not_allowed")
    if resolve_dns:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError("result_host_not_public")


def build_payload(args: dict, reference: Path, *, contract: str | None = None) -> tuple[dict, str]:
    model = args.get("model", "gpt-image-2")
    if model not in MODELS:
        raise ValueError("model_not_allowed")
    contract = contract or MODEL_CONTRACTS[model]
    if contract not in CONTRACT_ROUTES or (contract == "nano_legacy" and model != "nano-banana-2"):
        raise ValueError("model_contract_mismatch")
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not 3 <= len(prompt.strip()) <= 6000:
        raise ValueError("invalid_prompt")
    request_id = args.get("request_id", "")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{3,95}", request_id):
        raise ValueError("invalid_request_id")
    aspect = args.get("aspect_ratio", "1:1")
    if aspect not in ASPECTS:
        raise ValueError("invalid_aspect_ratio")
    size = args.get("image_size", "1K")
    if size not in ("1K", "2K", "4K") or (model == "gpt-image-2" and size != "1K"):
        raise ValueError("unsupported_model_resolution")
    use_reference = args.get("use_pavel_reference", True)
    if type(use_reference) is not bool:
        raise ValueError("invalid_reference_flag")
    images = []
    reference_sha = None
    if use_reference:
        raw = reference.read_bytes()
        reference_sha = hashlib.sha256(raw).hexdigest()
        if reference_sha != CANONICAL_SHA:
            raise ValueError("canonical_reference_mismatch")
        images = ["data:image/png;base64," + base64.b64encode(raw).decode()]
    if contract == "nano_legacy":
        reference_field = "urls"
        payload = {"model": model, "prompt": prompt.strip(), "urls": images,
                   "aspectRatio": aspect, "imageSize": size, "webHook": "-1", "shutProgress": True}
    else:
        reference_field = "images"
        payload = {"model": model, "prompt": prompt.strip(), "images": images,
                   "aspectRatio": "1024x1024" if model == "gpt-image-2" and aspect == "1:1" else aspect,
                   "replyType": "async"}
        if model == "nano-banana-2":
            payload["imageSize"] = size
    # Preserve the original unified fingerprint for already recorded requests.
    signature = {**payload, reference_field: [reference_sha] if reference_sha else []}
    digest = hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return payload, digest


class Grsai:
    def __init__(self, *, key: str, root: Path, reference: Path, client=None):
        self.key, self.root, self.reference = key, root, reference
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(root / "jobs.sqlite3", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs(
            request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, model TEXT NOT NULL,
            state TEXT NOT NULL, task_id TEXT, result_url TEXT, path TEXT,
            expected_credits INTEGER NOT NULL, credits_before INTEGER, credits_after INTEGER,
            metadata_json TEXT, error_type TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        self.db.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "provider_error" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN provider_error TEXT")
        if "contract" not in columns:
            # Existing tasks were submitted through unified, including old Nano tasks.
            self.db.execute("ALTER TABLE jobs ADD COLUMN contract TEXT NOT NULL DEFAULT 'unified'")
        self.db.commit()
        self.client = client or httpx.AsyncClient(base_url=BASE, headers={"Authorization": f"Bearer {key}"},
                                                  timeout=httpx.Timeout(55, connect=10), follow_redirects=False)

    async def close(self):
        self.db.close()
        await self.client.aclose()

    def row(self, request_id):
        row = self.db.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise ValueError("job_not_found")
        return dict(row)

    def update(self, request_id, **values):
        allowed = {"state", "task_id", "result_url", "path", "credits_after", "metadata_json", "error_type", "provider_error"}
        if set(values) - allowed:
            raise ValueError("invalid_job_update")
        values["updated_at"] = now()
        self.db.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in values) + " WHERE request_id=?",
                        (*values.values(), request_id))
        self.db.commit()

    def public(self, request_id):
        row = self.row(request_id)
        result = {k: row[k] for k in ("request_id", "model", "contract", "state", "expected_credits", "error_type")}
        result["model_name"] = MODEL_NAMES[row["model"]]
        result.update(ok=row["state"] not in ("failed", "violation", "submit_uncertain", "rejected"),
                      provider_task_known=bool(row["task_id"]), retry_submit_allowed=False,
                      fingerprint=row["fingerprint"])
        if row["metadata_json"]:
            result["image"] = json.loads(row["metadata_json"])
        if row["provider_error"]:
            result["provider_error"] = row["provider_error"]
            result["provider_error_is_untrusted_data"] = True
        if row["credits_after"] is not None and row["credits_before"] is not None:
            result["account_credit_delta"] = row["credits_before"] - row["credits_after"]
            result["billing_note"] = "Account balance delta; other activity on the same account can affect it."
        return result

    async def credits(self):
        # GRSAI's legacy balance endpoint requires apikey in query, not Bearer.
        # Do not log response.request.url or raw exception text.
        response = await self.client.get("/client/common/getCredits", params={"apikey": self.key})
        data = response.json()
        credits = (data.get("data") or {}).get("credits") if isinstance(data, dict) else None
        if response.status_code != 200 or data.get("code") != 0 or type(credits) not in (int, float):
            raise ValueError("credits_unavailable")
        return int(credits)

    async def status(self):
        return {"ok": True, "provider": "grsai", "credential_configured": bool(self.key),
                "credits": await self.credits(), "models": MODELS,
                "default_model": "gpt-image-2",
                "model_choices": [
                    {"id": model, "name": MODEL_NAMES[model], "credits": credits,
                     "contract": MODEL_CONTRACTS[model], **CONTRACT_ROUTES[MODEL_CONTRACTS[model]]}
                    for model, credits in MODELS.items()
                ],
                "active_jobs": [dict(r) for r in self.db.execute(
                    "SELECT request_id,state,model,contract FROM jobs WHERE state IN ('submitting','running','submit_uncertain')")],
                "per_request_confirmation": True, "automatic_resubmit": False}

    async def submit(self, args):
        request_id = args.get("request_id", "")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{3,95}", request_id):
            raise ValueError("invalid_request_id")
        previous = self.db.execute("SELECT fingerprint,contract FROM jobs WHERE request_id=?", (request_id,)).fetchone()
        contract = previous["contract"] if previous else MODEL_CONTRACTS.get(args.get("model", "gpt-image-2"))
        payload, digest = build_payload(args, self.reference, contract=contract)
        if args.get("confirm_credits") != MODELS[payload["model"]] or type(args.get("confirm_credits")) is not int:
            raise ValueError("exact_credit_confirmation_required")
        if previous:
            if previous["fingerprint"] != digest:
                raise ValueError("request_id_payload_mismatch")
            return self.public(request_id)
        before = await self.credits()
        cost = MODELS[payload["model"]]
        if before < cost:
            raise ValueError("insufficient_credits")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute("SELECT 1 FROM jobs WHERE state IN ('submitting','running','submit_uncertain') LIMIT 1").fetchone():
                raise ValueError("another_job_requires_completion_or_reconciliation")
            self.db.execute("""INSERT INTO jobs(request_id,fingerprint,model,state,expected_credits,
                            credits_before,created_at,updated_at,contract) VALUES(?,?,?,?,?,?,?,?,?)""",
                            (request_id, digest, payload["model"], "submitting", cost, before, now(), now(), contract))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        try:
            response = await self.client.post(CONTRACT_ROUTES[contract]["submit_path"], json=payload)
            data = response.json()
            data = data.get("data", data) if isinstance(data, dict) else {}
            task_id = data.get("id")
            if task_id:
                self.update(request_id, task_id=str(task_id), state="running")
                await self._apply_result(request_id, data)
            elif response.status_code in (400, 401, 403, 422, 429):
                self.update(request_id, state="rejected", error_type=f"http_{response.status_code}")
            else:
                self.update(request_id, state="submit_uncertain", error_type="missing_task_id")
        except Exception as exc:
            if self.row(request_id)["task_id"]:
                self.update(request_id, error_type=type(exc).__name__)
            else:
                self.update(request_id, state="submit_uncertain", error_type=type(exc).__name__)
        return self.public(request_id)

    async def _apply_result(self, request_id, data):
        state = str(data.get("status", "")).lower()
        if state in ("succeeded", "success"):
            results = data.get("results") or []
            url = results[0].get("url") if results and isinstance(results[0], dict) else None
            if not isinstance(url, str):
                self.update(request_id, error_type="result_url_missing")
                return
            validate_result_url(url)
            self.update(request_id, state="succeeded", result_url=url, error_type=None)
        elif state in ("failed", "violation", "cancelled", "canceled", "error"):
            message = str(data.get("error") or "").replace(self.key, "<redacted>")
            message = re.sub(r"https?://\S+|sk-[A-Za-z0-9_-]{16,}", "<redacted>", message)[:500]
            self.update(request_id, state="violation" if state == "violation" else "failed",
                        error_type=state, provider_error=message)
        if state in ("succeeded", "success", "failed", "violation", "cancelled", "canceled", "error"):
            try:
                self.update(request_id, credits_after=await self.credits())
            except Exception:
                pass

    async def poll(self, request_id):
        row = self.row(request_id)
        if row["state"] != "running":
            return self.public(request_id)
        route = CONTRACT_ROUTES[row["contract"]]
        if row["contract"] == "nano_legacy":
            response = await self.client.post(route["poll_path"], json={"id": row["task_id"]})
        else:
            response = await self.client.get(route["poll_path"], params={"id": row["task_id"]})
        if response.status_code != 200:
            return {**self.public(request_id), "poll_error": f"http_{response.status_code}"}
        data = response.json()
        if row["contract"] == "nano_legacy" and (not isinstance(data, dict) or data.get("code") != 0):
            code = data.get("code") if isinstance(data, dict) else "invalid_response"
            return {**self.public(request_id), "poll_error": f"legacy_code_{code}"}
        data = data.get("data", data) if isinstance(data, dict) else {}
        if str(data.get("id", row["task_id"])) != row["task_id"]:
            raise ValueError("poll_task_id_mismatch")
        await self._apply_result(request_id, data)
        return self.public(request_id)

    async def fetch(self, request_id):
        row = self.row(request_id)
        if row["state"] not in ("succeeded", "downloaded"):
            return self.public(request_id)
        if row["path"]:
            raw = Path(row["path"]).read_bytes()
        else:
            url = row["result_url"]
            validate_result_url(url)
            # Never reuse the credentialed API client for the result CDN.
            async with httpx.AsyncClient(timeout=50, follow_redirects=False) as downloader:
                async with downloader.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise ValueError("result_download_failed")
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_IMAGE:
                            raise ValueError("result_too_large")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
            metadata = image_metadata(raw)
            output = self.root / (hashlib.sha256(request_id.encode()).hexdigest()[:24] + metadata["suffix"])
            output.write_bytes(raw)
            output.chmod(0o600)
            self.update(request_id, path=str(output), state="downloaded", metadata_json=json.dumps(metadata))
        metadata = image_metadata(raw)
        stored = json.loads(self.row(request_id)["metadata_json"])
        if metadata["sha256"] != stored["sha256"]:
            raise ValueError("downloaded_file_checksum_mismatch")
        return {**self.public(request_id), "image_b64": base64.b64encode(raw).decode()}


async def dispatch_media(data):
    key = os.environ.get("GRSAI_API_KEY", "")
    if not key:
        return {"ok": False, "error": "grsai_not_configured"}
    root = Path(os.environ["GRSAI_STATE_DIR"])
    reference = Path(os.environ["GRSAI_REFERENCE_PATH"])
    api = Grsai(key=key, root=root, reference=reference)
    try:
        op = data["op"]
        if op == "image_status":
            return await api.status()
        if op == "image_submit":
            return await api.submit(data)
        if op == "image_poll":
            return await api.poll(data["request_id"])
        if op == "image_fetch":
            return await api.fetch(data["request_id"])
        raise ValueError("unknown_media_operation")
    finally:
        await api.close()
