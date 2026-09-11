from unittest.mock import patch

from tools import transcription_tools as stt


def _ok(model, text="hello"):
    return {
        "success": True,
        "transcript": text,
        "provider": "openrouter",
        "model": model,
        "allow_local_fallback": False,
    }


def _fail(model, *, status=None, error="boom"):
    result = {
        "success": False,
        "transcript": "",
        "provider": "openrouter",
        "model": model,
        "error": error,
    }
    if status is not None:
        result["status_code"] = status
    return result
def test_openrouter_stt_uses_configured_order_and_timeout():
    cfg = {
        "openrouter": {
            "model": "openai/whisper-large-v3-turbo",
            "fallback_models": ["microsoft/mai-transcribe-2", "openai/whisper-large-v3"],
            "timeout": 30,
        }
    }
    models, timeout = stt._openrouter_stt_models(cfg)
    assert models == [
        "openai/whisper-large-v3-turbo",
        "microsoft/mai-transcribe-2",
        "openai/whisper-large-v3",
    ]
    assert timeout == 30.0


def test_openrouter_stt_falls_forward_to_second_model():
    calls = []
    def fake_once(_path, model, **_kwargs):
        calls.append(model)
        if len(calls) == 1:
            return _fail(model, status=502)
        return _ok(model, "recovered")
    cfg = {
        "openrouter": {
            "model": "openai/whisper-large-v3-turbo",
            "fallback_models": ["microsoft/mai-transcribe-2", "openai/whisper-large-v3"],
            "timeout": 30,
        }
    }
    with patch.object(stt, "_resolve_provider_key", return_value="test-key"), \
         patch.object(stt, "_load_stt_config", return_value=cfg), \
         patch.object(stt, "_transcribe_openrouter_once", side_effect=fake_once):
        result = stt._transcribe_openrouter("/tmp/voice.ogg", "openai/whisper-large-v3-turbo")

    assert result["success"] is True
    assert result["transcript"] == "recovered"
    assert calls == ["openai/whisper-large-v3-turbo", "microsoft/mai-transcribe-2"]


def test_openrouter_stt_cloud_exhaustion_allows_local_fallback():
    cfg = {"openrouter": {"fallback_models": ["microsoft/mai-transcribe-2"]}}
    with patch.object(stt, "_resolve_provider_key", return_value="test-key"), \
         patch.object(stt, "_load_stt_config", return_value=cfg), \
         patch.object(stt, "_transcribe_openrouter_once", side_effect=lambda _p, m, **_k: _fail(m, status=503)):
        result = stt._transcribe_openrouter("/tmp/voice.ogg", stt.DEFAULT_OPENROUTER_STT_MODEL)

    assert result["success"] is False
    assert result["allow_local_fallback"] is True
def test_openrouter_auth_error_skips_cloud_siblings_and_allows_local():
    calls = []
    cfg = {"openrouter": {"fallback_models": ["microsoft/mai-transcribe-2", "openai/whisper-large-v3"]}}
    def fake_once(_path, model, **_kwargs):
        calls.append(model)
        return _fail(model, status=401, error="unauthorized")

    with patch.object(stt, "_resolve_provider_key", return_value="test-key"), \
         patch.object(stt, "_load_stt_config", return_value=cfg), \
         patch.object(stt, "_transcribe_openrouter_once", side_effect=fake_once):
        result = stt._transcribe_openrouter("/tmp/voice.ogg", stt.DEFAULT_OPENROUTER_STT_MODEL)

    assert result["success"] is False
    assert result["allow_local_fallback"] is True
    assert calls == [stt.DEFAULT_OPENROUTER_STT_MODEL]


def test_openrouter_model_list_deduplicates_primary():
    cfg = {"openrouter": {"model": "m1", "fallback_models": ["m1", "m2", "m2"]}}
    models, timeout = stt._openrouter_stt_models(cfg)
    assert models == ["m1", "m2"]
    assert timeout == stt.DEFAULT_OPENROUTER_STT_TIMEOUT
