from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(MODULE))
import provider


def test_resolve_routes_prefers_distinct_sol_on_other_host(monkeypatch,tmp_path):
    def fake_imports(home):
        return (lambda **kw: None, lambda e: e.get("_resolved_key"), lambda **kw: None)
    monkeypatch.setattr(provider,"_hermes_imports",fake_imports)
    monkeypatch.delenv("LINGSUAN_OPUS_KEY",raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY",raising=False)
    cfg={
        "model":{"default":"gpt-6.1-sol","base_url":"https://primary.example","api_key":"primary-key-123"},
        "fallback_providers":[
            {"provider":"custom","model":"gpt-5.6-terra","base_url":"https://primary.example","_resolved_key":"terra-key-456"},
            {"provider":"custom","model":"gpt-6.1-sol","base_url":"https://fallback.example","_resolved_key":"fallback-key-789"},
        ]}
    p,f=provider.resolve_routes(tmp_path,cfg)
    assert p["model"] == "gpt-6.1-sol"
    assert f["model"] == "gpt-6.1-sol"
    assert f["base_url"] == "https://fallback.example"
    assert f["api_key"] != p["api_key"]


def test_resolve_routes_rejects_same_key_fallback(monkeypatch,tmp_path):
    monkeypatch.setattr(provider,"_hermes_imports",lambda home:(lambda **kw:None,lambda e:e.get("_resolved_key"),lambda **kw:None))
    cfg={"model":{"default":"gpt-5.6-sol","base_url":"https://a","api_key":"same"},
         "fallback_providers":[{"provider":"custom","model":"gpt-5.6-sol","base_url":"https://b","_resolved_key":"same"}]}
    with pytest.raises(RuntimeError,match="distinct_fallback"):
        provider.resolve_routes(tmp_path,cfg)


def test_parse_submission_requires_exactly_one_named_tool_call():
    args='{"summary":"ok","operations":[]}'
    good=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(name=provider.TOOL_NAME,arguments=args))]))])
    assert provider._parse_submission(good)["operations"] == []
    bad=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[]))])
    with pytest.raises(RuntimeError,match="tool_call_count"):
        provider._parse_submission(bad)


def test_curate_batch_falls_back_on_primary_failure(monkeypatch,tmp_path):
    routes=(
        {"provider":"custom","model":"gpt-5.6-sol","base_url":"https://primary","api_key":"p"},
        {"provider":"custom","model":"gpt-5.6-sol","base_url":"https://fallback","api_key":"f"},
    )
    monkeypatch.setattr(provider,"resolve_routes",lambda home,config:routes)
    calls=[]
    def fake_call(home,route,**kwargs):
        calls.append(route["base_url"])
        if "primary" in route["base_url"]: raise TimeoutError("boom")
        return {"summary":"ok","operations":[]}
    monkeypatch.setattr(provider,"_call_route",fake_call)
    out,meta,err=provider.curate_batch(tmp_path,{}, {"memories":[],"tombstones":[]}, [], "sys")
    assert out["operations"] == []
    assert meta["route"] == "fallback"
    assert err == "TimeoutError"
    assert calls == ["https://primary","https://fallback"]


def test_curate_batch_uses_primary_when_healthy(monkeypatch,tmp_path):
    routes=(
        {"provider":"custom","model":"gpt-5.6-sol","base_url":"https://primary","api_key":"p"},
        {"provider":"custom","model":"gpt-5.6-sol","base_url":"https://fallback","api_key":"f"},
    )
    monkeypatch.setattr(provider,"resolve_routes",lambda home,config:routes)
    monkeypatch.setattr(provider,"_call_route",lambda home,route,**kwargs:{"summary":"ok","operations":[]})
    _,meta,err=provider.curate_batch(tmp_path,{}, {"memories":[],"tombstones":[]}, [], "sys")
    assert meta["route"] == "primary" and err is None


def test_resolve_routes_prefers_lingsuan_sonnet_on_opus_key(monkeypatch,tmp_path):
    monkeypatch.setattr(provider,"_hermes_imports",lambda home:(lambda **kw:None,lambda e:e.get("_resolved_key"),lambda **kw:None))
    monkeypatch.delenv("LIVING_MEMORY_FALLBACK_BASE",raising=False)
    monkeypatch.setenv("LINGSUAN_OPUS_KEY","opus-separate-key")
    monkeypatch.setenv("OPENROUTER_API_KEY","openrouter-separate-key")  # STT-only key: must be ignored here
    cfg={"model":{"default":"gpt-6.1-sol","base_url":"https://primary.example","api_key":"primary-key"},"fallback_providers":[]}
    primary,fallback=provider.resolve_routes(tmp_path,cfg)
    assert primary["model"] == "gpt-6.1-sol"
    assert fallback["provider"] == "custom"
    assert fallback["model"] == "claude-sonnet-5-5"
    assert fallback["base_url"] == "https://api.lingsuan.si/v1"
    assert fallback["api_key"] == "opus-separate-key" and fallback["api_key"] != primary["api_key"]


def test_resolve_routes_empty_fallback_base_uses_default(monkeypatch,tmp_path):
    monkeypatch.setattr(provider,"_hermes_imports",lambda home:(lambda **kw:None,lambda e:e.get("_resolved_key"),lambda **kw:None))
    monkeypatch.setenv("LIVING_MEMORY_FALLBACK_BASE","")
    monkeypatch.setenv("LINGSUAN_OPUS_KEY","opus-separate-key")
    monkeypatch.setenv("OPENROUTER_API_KEY","openrouter-separate-key")
    cfg={"model":{"default":"gpt-6.1-sol","base_url":"https://primary.example","api_key":"primary-key"},"fallback_providers":[]}
    primary,fallback=provider.resolve_routes(tmp_path,cfg)
    assert fallback["base_url"] == "https://api.lingsuan.si/v1"


def test_resolve_routes_openrouter_key_alone_is_not_a_fallback(monkeypatch,tmp_path):
    monkeypatch.setattr(provider,"_hermes_imports",lambda home:(lambda **kw:None,lambda e:e.get("_resolved_key"),lambda **kw:None))
    monkeypatch.delenv("LINGSUAN_OPUS_KEY",raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY","openrouter-separate-key")
    cfg={"model":{"default":"gpt-6.1-sol","base_url":"https://primary.example","api_key":"primary-key"},"fallback_providers":[]}
    with pytest.raises(RuntimeError,match="distinct_fallback"):
        provider.resolve_routes(tmp_path,cfg)


def test_call_route_requests_high_reasoning(monkeypatch,tmp_path):
    captured={}
    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        args='{"summary":"ok","operations":[]}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(name=provider.TOOL_NAME,arguments=args))]))])
    monkeypatch.setattr(provider,"_hermes_imports",lambda home:(lambda **kw:None,lambda e:None,fake_call_llm))
    route={"provider":"openrouter","model":"openai/gpt-5.6-sol","base_url":"https://openrouter.ai/api/v1","api_key":"x"}
    provider._call_route(tmp_path,route,system_prompt="s",user_prompt="u")
    assert captured["reasoning_config"] == {"enabled":True,"effort":"high"}
    assert captured["tools"][0]["function"]["name"] == provider.TOOL_NAME


def test_curate_batch_fallback_uses_configured_fallback(monkeypatch,tmp_path):
    routes=(
        {"provider":"custom","model":"gpt-5.6-sol","base_url":"https://primary","api_key":"p"},
        {"provider":"openrouter","model":"openai/gpt-5.6-sol","base_url":"https://openrouter.ai/api/v1","api_key":"f"},
    )
    monkeypatch.setattr(provider,"resolve_routes",lambda home,config:routes)
    seen={}
    def fake_call(home,route,**kwargs):
        seen.update(route)
        return {"summary":"ok","operations":[]}
    monkeypatch.setattr(provider,"_call_route",fake_call)
    out,meta=provider.curate_batch_fallback(tmp_path,{}, {"memories":[],"tombstones":[]}, [], "sys")
    assert out["operations"] == []
    assert seen["provider"] == "openrouter"
    assert meta["route"] == "fallback_contract_retry"


def test_resolve_routes_fallback_base_follows_env_override(monkeypatch,tmp_path):
    # Key-broker profiles point the preferred Sonnet fallback at their lane; the env is read at call time
    # because Hermes loads .env after this module is imported.
    monkeypatch.setattr(provider,"_hermes_imports",lambda home:(lambda **kw:None,lambda e:e.get("_resolved_key"),lambda **kw:None))
    monkeypatch.setenv("LINGSUAN_OPUS_KEY","keybroker-lingsuan-opus")
    monkeypatch.setenv("LIVING_MEMORY_FALLBACK_BASE","http://127.0.0.1:21001/lingsuan-opus/v1/")
    cfg={"model":{"default":"gpt-6.1-sol","base_url":"http://127.0.0.1:21001/lingsuan/v1","api_key":"keybroker-lingsuan"},"fallback_providers":[]}
    primary,fallback=provider.resolve_routes(tmp_path,cfg)
    assert primary["base_url"] == "http://127.0.0.1:21001/lingsuan/v1"
    assert fallback["model"] == "claude-sonnet-5-5"
    assert fallback["base_url"] == "http://127.0.0.1:21001/lingsuan-opus/v1"
    assert fallback["api_key"] != primary["api_key"]
