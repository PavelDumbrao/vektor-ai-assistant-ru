from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
engine = importlib.import_module("plugin.engine")
asr_client = importlib.import_module("plugin.asr_client")


def load_broker():
    spec = importlib.util.spec_from_file_location("video_asr_broker_security_test", MODULE / "asr_broker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_broker_secret_file_rejects_weak_permissions_and_symlink(tmp_path):
    broker = load_broker()
    secret = tmp_path / "secret"
    secret.write_text("x" * 40)
    secret.chmod(0o600)
    assert broker._secure_regular(secret, expected_uid=os.getuid()).st_size == 40
    secret.chmod(0o644)
    with pytest.raises(RuntimeError, match="permissions_unsafe"):
        broker._secure_regular(secret, expected_uid=os.getuid())
    secret.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(secret)
    with pytest.raises(RuntimeError, match="unsafe"):
        broker._secure_regular(link, expected_uid=os.getuid())


def test_broker_retry_delay_is_bounded():
    broker = load_broker()
    assert broker._retry_delay(1, "0") == 1.0
    assert broker._retry_delay(2, "999") == 20.0
    assert broker._retry_delay(3, "invalid") == 8.0


def test_profile_token_rejects_weak_permissions_and_symlink(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    token_dir = hermes / "video_editor"
    token_dir.mkdir(parents=True)
    token = token_dir / "asr_token"
    token.write_text("a" * 44 + "\n")
    token.chmod(0o600)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    assert asr_client._token() == "a" * 44
    token.chmod(0o644)
    assert asr_client._token() == ""
    token.unlink()
    target = tmp_path / "target"
    target.write_text("b" * 44)
    target.chmod(0o600)
    token.symlink_to(target)
    assert asr_client._token() == ""


def test_authenticated_health_endpoint(monkeypatch):
    broker = load_broker()
    good = "t" * 44
    monkeypatch.setattr(broker, "_client_token", lambda profile: good if profile == "alice" else "")
    monkeypatch.setattr(broker, "_openrouter_key", lambda: "k" * 24)
    server = broker.BrokerServer(("127.0.0.1", 0), broker.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/v1/health"
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(base, timeout=2)
        assert denied.value.code == 403
        req = urllib.request.Request(base, headers={
            "X-Vektor-Profile": "alice",
            "X-Vektor-ASR-Token": good,
        })
        with urllib.request.urlopen(req, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["model"] == broker.MODEL
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_auto_asr_falls_back_and_cleans_partial(tmp_path, monkeypatch):
    studio = tmp_path / "studio"
    transcripts = studio / "transcripts"
    transcripts.mkdir(parents=True)
    first_output = transcripts / "first.verbatim.json"
    calls = {"broker": 0, "local": 0}

    def fake_broker(source, studio_path, language):
        calls["broker"] += 1
        if calls["broker"] == 1:
            first_output.write_text("{}")
            return {"output": str(first_output), "items": 3}
        raise asr_client.ASRError("broker_down")

    def fake_local(cmd, **kwargs):
        calls["local"] += 1
        return None

    monkeypatch.setattr(asr_client, "transcribe", fake_broker)
    monkeypatch.setattr(engine, "_run", fake_local)
    used, details, error = engine._transcribe_sources(
        ["/tmp/a.mp4", "/tmp/b.mp4"], studio, "ru", Path("/tmp/model.bin"), "auto", "python3"
    )
    assert used == "local"
    assert details == []
    assert "broker_down" in str(error)
    assert calls == {"broker": 2, "local": 1}
    assert not first_output.exists()


def test_openrouter_asr_is_strict(tmp_path, monkeypatch):
    studio = tmp_path / "studio"
    (studio / "transcripts").mkdir(parents=True)
    monkeypatch.setattr(asr_client, "transcribe", lambda *args, **kwargs: (_ for _ in ()).throw(asr_client.ASRError("no_broker")))
    local_calls = []
    monkeypatch.setattr(engine, "_run", lambda *args, **kwargs: local_calls.append(args))
    with pytest.raises(engine.VideoEditorError, match="video_openrouter_asr_failed:no_broker"):
        engine._transcribe_sources(["/tmp/a.mp4"], studio, "ru", Path("/tmp/model.bin"), "openrouter", "python3")
    assert local_calls == []


def test_local_asr_never_calls_broker(tmp_path, monkeypatch):
    studio = tmp_path / "studio"
    (studio / "transcripts").mkdir(parents=True)
    monkeypatch.setattr(asr_client, "transcribe", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("broker called")))
    local_calls = []
    monkeypatch.setattr(engine, "_run", lambda cmd, **kwargs: local_calls.append(cmd))
    used, details, error = engine._transcribe_sources(
        ["/tmp/a.mp4"], studio, "ru", Path("/tmp/model.bin"), "local", "python3"
    )
    assert used == "local"
    assert details == []
    assert error is None
    assert len(local_calls) == 1
    assert local_calls[0][1].endswith("scripts/transcribe.py")


def test_openrouter_success_skips_local(tmp_path, monkeypatch):
    studio = tmp_path / "studio"
    (studio / "transcripts").mkdir(parents=True)
    monkeypatch.setattr(asr_client, "transcribe", lambda *args, **kwargs: {"output": str(studio / "transcripts" / "ok.verbatim.json"), "items": 4})
    local_calls = []
    monkeypatch.setattr(engine, "_run", lambda *args, **kwargs: local_calls.append(args))
    used, details, error = engine._transcribe_sources(
        ["/tmp/a.mp4"], studio, "ru", Path("/tmp/model.bin"), "auto", "python3"
    )
    assert used == "openrouter"
    assert details[0]["items"] == 4
    assert error is None
    assert local_calls == []


def test_openrouter_429_retries_then_succeeds(monkeypatch):
    broker = load_broker()
    sleeps = []
    attempts = {"n": 0}
    monkeypatch.setattr(broker, "_openrouter_key", lambda: "k" * 24)
    monkeypatch.setattr(broker.time, "sleep", lambda value: sleeps.append(value))

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, limit=-1):
            payload = {"text": "привет", "words": [{"word": "привет", "start": 0.0, "end": 0.5}]}
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def fake_urlopen(req, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "rate", {"Retry-After": "1"}, None)
        return Response()

    monkeypatch.setattr(broker.urllib.request, "urlopen", fake_urlopen)
    result = broker._post_openrouter(b"audio", "mp3", "ru")
    assert result["text"] == "привет"
    assert attempts["n"] == 2
    assert sleeps == [1.0]


def load_installer():
    spec = importlib.util.spec_from_file_location("video_asr_installer_test", MODULE / "install_asr_broker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_installer_source_env_requires_root_private_file(tmp_path):
    installer = load_installer()
    env = tmp_path / "openrouter.env"
    env.write_text("OPENROUTER_API_KEY=" + "k" * 32 + "\n")
    env.chmod(0o600)
    assert installer._read_key(env) == "k" * 32
    env.chmod(0o640)
    with pytest.raises(RuntimeError, match="permissions_unsafe"):
        installer._read_key(env)
    env.chmod(0o600)
    link = tmp_path / "env-link"
    link.symlink_to(env)
    with pytest.raises(RuntimeError, match="unsafe"):
        installer._read_key(link)


def test_installer_health_wait_retries_startup_race(monkeypatch):
    installer = load_installer()
    attempts = {"n": 0}
    sleeps = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return b'{"ok":true}'

    def fake_urlopen(url, timeout):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise urllib.error.URLError("not ready")
        return Response()

    monkeypatch.setattr(installer.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(installer.time, "sleep", lambda value: sleeps.append(value))
    assert installer._wait_health(attempts=4, delay=0.1)["ok"] is True
    assert attempts["n"] == 3
    assert sleeps == [0.1, 0.1]
