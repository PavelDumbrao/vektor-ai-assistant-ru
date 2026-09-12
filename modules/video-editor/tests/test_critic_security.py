from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
critic_client = importlib.import_module("plugin.critic_client")


def load_broker():
    spec = importlib.util.spec_from_file_location("video_critic_broker_test", MODULE / "critic_broker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
def test_secret_file_rejects_weak_permissions_and_symlink(tmp_path):
    broker = load_broker()
    secret = tmp_path / "lingsuan.env"
    secret.write_text("LINGSUAN_API_KEY=" + "k" * 40 + "\n")
    secret.chmod(0o600)
    assert broker._secure_regular(secret, expected_uid=os.getuid()).st_size > 40
    secret.chmod(0o644)
    with pytest.raises(RuntimeError, match="permissions_unsafe"):
        broker._secure_regular(secret, expected_uid=os.getuid())
    secret.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(secret)
    with pytest.raises(RuntimeError, match="unsafe"):
        broker._secure_regular(link, expected_uid=os.getuid())


def test_json_extractor_ignores_reasoning_prefix():
    broker = load_broker()
    text = 'Thinking first...\n```json\n{"verdict":"pass","summary":"ok","issues":[]}\n```'
    assert broker._extract_json(text)["verdict"] == "pass"
def test_model_fallback_moves_to_next_3_8_tier_on_524(tmp_path, monkeypatch):
    broker = load_broker()
    proxy = tmp_path / "proxy.mp4"
    proxy.write_bytes(b"video")
    monkeypatch.setattr(broker, "_lingsuan_key", lambda: "k" * 40)
    monkeypatch.setattr(broker.time, "sleep", lambda _value: None)
    calls = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit=-1):
            body = {"choices": [{"message": {"content": '{"verdict":"pass","summary":"clean","issues":[]}'}}]}
            return json.dumps(body).encode()

    def fake_urlopen(req, timeout):
        body = json.loads(req.data)
        calls.append(body["model"])
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 524, "timeout", {}, None)
        return Response()

    monkeypatch.setattr(broker.urllib.request, "urlopen", fake_urlopen)
    report, model = broker._post_lingsuan(proxy, "master")
    assert calls[:2] == ["gemini-3.8-flash-medium", "gemini-3.8-flash-low"]
    assert model == "gemini-3.8-flash-low"
    assert report["verdict"] == "pass"
def test_video_path_is_scoped_to_authenticated_profile_jobs(tmp_path, monkeypatch):
    broker = load_broker()
    uid = os.getuid()
    home = tmp_path / "alice"
    own = home / ".hermes" / "video_editor" / "jobs" / "012345abcdef" / "studio" / "out" / "master.mp4"
    own.parent.mkdir(parents=True)
    own.write_bytes(b"video")
    other = tmp_path / "bob" / ".hermes" / "video_editor" / "jobs" / "abcdef012345" / "studio" / "out" / "master.mp4"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"other")
    monkeypatch.setattr(broker.pwd, "getpwnam", lambda name: SimpleNamespace(pw_dir=str(home), pw_uid=uid))
    assert broker._validate_video_path("alice", str(own)) == own.resolve()
    with pytest.raises(RuntimeError, match="outside_profile_jobs"):
        broker._validate_video_path("alice", str(other))


def test_profile_token_rejects_weak_permissions(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    d = hermes / "video_editor"
    d.mkdir(parents=True)
    token = d / "critic_token"
    token.write_text("t" * 44 + "\n")
    token.chmod(0o600)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    assert critic_client._token() == "t" * 44
    token.chmod(0o644)
    assert critic_client._token() == ""
