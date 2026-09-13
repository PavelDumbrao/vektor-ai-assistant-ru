from __future__ import annotations

import base64
import mimetypes
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    save_url_image,
    success_response,
)

MODEL = "gpt-image-2.5"
BASE_DEFAULT = "https://grsaiapi.com"
POLL_SECONDS = 2.0
POLL_TIMEOUT = 180.0
MAX_REFERENCE_IMAGES = 4
MAX_REFERENCE_BYTES = 12 * 1024 * 1024
LINGSUAN_BASE_DEFAULT = "https://lingsuan.top"
LINGSUAN_FALLBACKS = (
    ("gpt-image-2.5-sunburst", "LLM_API_KEY", "fallback_1"),
    ("gpt-image-2.5-flare-firefly", "FALLBACK_LLM_API_KEY", "fallback_2"),
)
LINGSUAN_TIMEOUT = (15, 150)
ASPECT_PIXELS = {
    "square": "1024x1024",
    "landscape": "1536x1024",
    "portrait": "1024x1536",
}
TRUSTED_RESULT_DOMAINS = (
    "aitohumanize.com",
    "grsai-resource.com",
    "grsai.com",
    "grsai.ai",
)


def _unwrap(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data if isinstance(data, dict) else {}


def _trusted_result_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    return parsed.scheme == "https" and any(
        host == item or host.endswith("." + item) for item in TRUSTED_RESULT_DOMAINS
    )

def _lingsuan_text_fallback(prompt: str, aspect: str) -> Optional[Dict[str, Any]]:
    """Try bounded Lingsuan text-to-image fallbacks without exposing secrets."""
    import requests

    base = os.environ.get("LINGSUAN_IMAGE_BASE_URL", LINGSUAN_BASE_DEFAULT).strip().rstrip("/")
    attempted = False
    for model, key_env, stage in LINGSUAN_FALLBACKS:
        key = os.environ.get(key_env, "").strip()
        if not key:
            continue
        attempted = True
        payload = {
            "model": model,
            "prompt": str(prompt or "").strip(),
            "size": ASPECT_PIXELS.get(aspect, "1024x1024"),
            "quality": "auto",
            "output_format": "png",
            "moderation": "auto",
            "n": 1,
            "response_format": "b64_json",
        }
        try:
            response = requests.post(
                base + "/v1/images/generations",
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                json=payload,
                timeout=LINGSUAN_TIMEOUT,
            )
        except requests.ConnectTimeout:
            continue
        except requests.Timeout:
            return error_response(
                error="Lingsuan fallback submit timed out; not trying another paid route",
                error_type="submit_uncertain",
                provider="lingsuan",
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.RequestException:
            continue
        if response.status_code != 200:
            continue
        try:
            data = response.json()
            items = data.get("data") if isinstance(data, dict) else None
            item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
            encoded = item.get("b64_json")
        except (ValueError, TypeError, IndexError):
            encoded = None
        if not isinstance(encoded, str) or not encoded.strip():
            return error_response(
                error="Lingsuan fallback returned no inline image; not trying another paid route",
                error_type="invalid_response",
                provider="lingsuan",
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        try:
            saved = save_b64_image(encoded, prefix="lingsuan_image_fallback", extension="png")
        except Exception:
            return error_response(
                error="Lingsuan fallback image could not be cached safely",
                error_type="download_error",
                provider="lingsuan",
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        return success_response(
            image=str(saved),
            model=model,
            prompt=prompt,
            aspect_ratio=aspect,
            provider="lingsuan",
            modality="text",
            extra={
                "fallback_from": "grsai/gpt-image-2.5",
                "fallback_stage": stage,
                "quality": "auto",
            },
        )
    if attempted:
        return error_response(
            error="GRSAI failed and the Lingsuan image fallback chain was exhausted",
            error_type="fallback_exhausted",
            provider="lingsuan",
            model=MODEL,
            prompt=prompt,
            aspect_ratio=aspect,
        )
    return None


def _fallback_or_error(*, prompt: str, aspect: str, refs: List[str], primary_error: Dict[str, Any]) -> Dict[str, Any]:
    """Fail over only for text-to-image; reference edits stay single-submit safe."""
    if refs:
        return primary_error
    fallback = _lingsuan_text_fallback(prompt, aspect)
    return fallback if fallback is not None else primary_error


def _reference_value(ref: str) -> Optional[str]:
    value = str(ref or "").strip()
    if not value:
        return None
    if value.startswith(("https://", "data:image/")):
        return value
    from agent.file_safety import raise_if_read_blocked
    raise_if_read_blocked(value)
    path = Path(value)
    try:
        size = path.stat().st_size
        if size <= 0 or size > MAX_REFERENCE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    if not mime.startswith("image/"):
        return None
    return "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")


class GrsaiImageProvider(ImageGenProvider):
    @property
    def name(self) -> str:
        return "grsai"

    @property
    def display_name(self) -> str:
        return "GRSAI GPT Image 2.5"

    def is_available(self) -> bool:
        return any(os.environ.get(name, "").strip() for name in (
            "GRSAI_API_KEY", "LLM_API_KEY", "FALLBACK_LLM_API_KEY",
        ))

    def capabilities(self) -> Dict[str, Any]:
        return {"modalities": ["text", "image"], "max_reference_images": MAX_REFERENCE_IMAGES}

    def list_models(self) -> List[Dict[str, Any]]:
        return [{
            "id": MODEL,
            "display": "GPT Image 2.5",
            "strengths": "GRSAI primary with bounded Lingsuan text-to-image fallbacks",
            "price": "600 credits/request",
        }]

    def default_model(self) -> Optional[str]:
        return MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "GRSAI GPT Image 2.5",
            "badge": "paid",
            "tag": "600 credits/request; supports reference images",
            "env_vars": [{"key": "GRSAI_API_KEY", "prompt": "GRSAI API key", "url": "https://grsai.com/dashboard/api-keys"}],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        import requests
        key = os.environ.get("GRSAI_API_KEY", "").strip()
        base = os.environ.get("GRSAI_BASE_URL", BASE_DEFAULT).strip().rstrip("/")
        selected = str(kwargs.get("model") or MODEL).strip()
        if selected != MODEL:
            return error_response(error="Only gpt-image-2.5 is allowed for this profile", error_type="model_not_allowed", provider=self.name, model=selected, prompt=prompt, aspect_ratio=aspect_ratio)
        aspect = resolve_aspect_ratio(aspect_ratio)
        refs: List[str] = []
        if image_url:
            refs.append(str(image_url))
        refs.extend(str(item) for item in (reference_image_urls or []))
        refs.extend(str(item) for item in (kwargs.get("reference_images") or []))
        encoded_refs = []
        for ref in refs[:MAX_REFERENCE_IMAGES]:
            value = _reference_value(ref)
            if value:
                encoded_refs.append(value)
        if not key:
            primary_error = error_response(
                error="GRSAI key is not configured", error_type="missing_api_key",
                provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect,
            )
            return _fallback_or_error(
                prompt=prompt, aspect=aspect, refs=encoded_refs, primary_error=primary_error,
            )
        payload = {
            "model": MODEL,
            "prompt": str(prompt or "").strip(),
            "aspectRatio": ASPECT_PIXELS.get(aspect, "1024x1024"),
            "quality": "low",
            "urls": encoded_refs,
            "webHook": "-1",
            "shutProgress": True,
        }
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        try:
            response = requests.post(base + "/v1/draw/completions", headers=headers, json=payload, timeout=(15, 75))
        except requests.ConnectTimeout:
            primary_error = error_response(
                error="GRSAI connect timed out", error_type="connection_error",
                provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect,
            )
            return _fallback_or_error(
                prompt=prompt, aspect=aspect, refs=encoded_refs, primary_error=primary_error,
            )
        except requests.Timeout:
            return error_response(error="GRSAI submit timed out; not retrying automatically", error_type="submit_uncertain", provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect)
        except requests.RequestException:
            return error_response(error="GRSAI submit connection failed", error_type="connection_error", provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect)
        if response.status_code != 200:
            primary_error = error_response(
                error=f"GRSAI submit failed with HTTP {response.status_code}",
                error_type="api_error", provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect,
            )
            return _fallback_or_error(
                prompt=prompt, aspect=aspect, refs=encoded_refs, primary_error=primary_error,
            )
        try:
            submitted = _unwrap(response.json())
        except ValueError:
            return error_response(error="GRSAI submit returned invalid JSON", error_type="invalid_response", provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect)
        task_id = str(submitted.get("id") or "").strip()
        if not task_id:
            return error_response(error="GRSAI accepted no task id; not retrying automatically", error_type="submit_uncertain", provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect)
        result = submitted
        state = str(result.get("status") or "").lower()
        deadline = time.monotonic() + POLL_TIMEOUT
        terminal_states = {"succeeded", "success", "failed", "violation", "error", "cancelled", "canceled"}
        while state not in terminal_states:
            if time.monotonic() >= deadline:
                return error_response(error="GRSAI result polling timed out", error_type="poll_timeout", provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect)
            time.sleep(POLL_SECONDS)
            try:
                polled = requests.post(base + "/v1/draw/result", headers=headers, json={"id": task_id}, timeout=(15, 45))
            except requests.RequestException:
                continue
            if polled.status_code != 200:
                continue
            try:
                payload_result = polled.json()
            except ValueError:
                continue
            if isinstance(payload_result, dict) and payload_result.get("code") not in (None, 0):
                continue
            result = _unwrap(payload_result)
            state = str(result.get("status") or "").lower()
        if state not in {"succeeded", "success"}:
            primary_error = error_response(
                error="GRSAI image generation failed", error_type="provider_failed",
                provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect,
            )
            return _fallback_or_error(
                prompt=prompt, aspect=aspect, refs=encoded_refs, primary_error=primary_error,
            )
        results = result.get("results") or []
        result_url = results[0].get("url") if results and isinstance(results[0], dict) else None
        if not isinstance(result_url, str) or not _trusted_result_url(result_url):
            return error_response(error="GRSAI returned an invalid result URL", error_type="invalid_response", provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect)
        try:
            saved = save_url_image(result_url, prefix="grsai_gpt_image_2_5", timeout=60.0)
        except Exception:
            return error_response(error="GRSAI result could not be cached safely", error_type="download_error", provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect)
        return success_response(
            image=str(saved),
            model=MODEL,
            prompt=prompt,
            aspect_ratio=aspect,
            provider=self.name,
            modality="image" if encoded_refs else "text",
            extra={"quality": "low", "expected_credits": 600},
        )


def register(ctx: Any) -> None:
    ctx.register_image_gen_provider(GrsaiImageProvider())
