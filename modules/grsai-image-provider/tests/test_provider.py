from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import sys
import types

import pytest

requests = types.ModuleType("requests")
class RequestException(Exception): pass
class Timeout(RequestException): pass
class ConnectTimeout(Timeout): pass
class ReadTimeout(Timeout): pass
requests.RequestException = RequestException
requests.Timeout = Timeout
requests.ConnectTimeout = ConnectTimeout
requests.ReadTimeout = ReadTimeout
requests.post = None
sys.modules["requests"] = requests

MODULE = Path(__file__).resolve().parents[1] / "plugin" / "__init__.py"
spec = importlib.util.spec_from_file_location("grsai_image_provider_under_test", MODULE)
assert spec and spec.loader
provider_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider_module)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def provider(monkeypatch, tmp_path):
    monkeypatch.setenv("GRSAI_API_KEY", "grsai-test")
    monkeypatch.setenv("LLM_API_KEY", "lingsuan-a")
    monkeypatch.setenv("FALLBACK_LLM_API_KEY", "lingsuan-b")
    output = tmp_path / "image.png"
    monkeypatch.setattr(provider_module, "save_b64_image", lambda *a, **k: output)
    monkeypatch.setattr(provider_module, "save_url_image", lambda *a, **k: output)
    return provider_module.GrsaiImageProvider()


def test_primary_success_never_calls_lingsuan(monkeypatch, provider):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs.get("json", {})))
        return FakeResponse(200, {"data": {
            "id": "job-1", "status": "success",
            "results": [{"url": "https://cdn.grsai.com/a.png"}],
        }})

    monkeypatch.setattr(requests, "post", fake_post)
    result = provider.generate("portrait", aspect_ratio="square")
    assert result["success"] is True
    assert result["provider"] == "grsai"
    assert result["model"] == "gpt-image-2.5"
    assert len(calls) == 1
    assert calls[0][0].endswith("/v1/draw/completions")


def test_primary_http_error_uses_sunburst(monkeypatch, provider):
    calls = []
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 40).decode()
    def fake_post(url, **kwargs):
        calls.append((url, kwargs.get("json", {})))
        if url.endswith("/v1/draw/completions"):
            return FakeResponse(503, {})
        return FakeResponse(200, {"data": [{"b64_json": encoded}]})

    monkeypatch.setattr(requests, "post", fake_post)
    result = provider.generate("portrait", aspect_ratio="square")
    assert result["success"] is True
    assert result["provider"] == "lingsuan"
    assert result["model"] == "gpt-image-2.5-sunburst"
    assert result["fallback_stage"] == "fallback_1"
    assert [payload.get("model") for _, payload in calls] == [
        "gpt-image-2.5", "gpt-image-2.5-sunburst",
    ]


def test_sunburst_failure_uses_flare_firefly(monkeypatch, provider):
    calls = []
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 40).decode()

    def fake_post(url, **kwargs):
        payload = kwargs.get("json", {})
        calls.append((url, payload))
        if url.endswith("/v1/draw/completions"):
            return FakeResponse(503, {})
        if payload.get("model") == "gpt-image-2.5-sunburst":
            return FakeResponse(502, {})
        return FakeResponse(200, {"data": [{"b64_json": encoded}]})
    monkeypatch.setattr(requests, "post", fake_post)
    result = provider.generate("portrait", aspect_ratio="square")
    assert result["success"] is True
    assert result["provider"] == "lingsuan"
    assert result["model"] == "gpt-image-2.5-flare-firefly"
    assert result["fallback_stage"] == "fallback_2"


def test_reference_edit_does_not_auto_fallback(monkeypatch, provider):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return FakeResponse(503, {})

    monkeypatch.setattr(requests, "post", fake_post)
    result = provider.generate(
        "edit this", image_url="data:image/png;base64,AAAA", aspect_ratio="square"
    )
    assert result["success"] is False
    assert result["provider"] == "grsai"
    assert result["error_type"] == "api_error"
    assert len(calls) == 1


def test_read_timeout_does_not_start_paid_fallback(monkeypatch, provider):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        raise requests.ReadTimeout("ambiguous")
    monkeypatch.setattr(requests, "post", fake_post)
    result = provider.generate("portrait", aspect_ratio="square")
    assert result["success"] is False
    assert result["error_type"] == "submit_uncertain"
    assert len(calls) == 1


def test_missing_grsai_key_can_use_lingsuan(monkeypatch, provider):
    monkeypatch.delenv("GRSAI_API_KEY")
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 40).decode()
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs.get("json", {})))
        return FakeResponse(200, {"data": [{"b64_json": encoded}]})

    monkeypatch.setattr(requests, "post", fake_post)
    result = provider.generate("portrait", aspect_ratio="square")
    assert result["success"] is True
    assert result["provider"] == "lingsuan"
    assert result["model"] == "gpt-image-2.5-sunburst"
    assert len(calls) == 1
    assert calls[0][0].endswith("/v1/images/generations")
