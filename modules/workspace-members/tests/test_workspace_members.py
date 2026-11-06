import importlib.util, json, time
from pathlib import Path
MOD=Path(__file__).parents[1]/"plugin/__init__.py"
spec=importlib.util.spec_from_file_location("wm",MOD); wm=importlib.util.module_from_spec(spec); spec.loader.exec_module(wm)
def test_actor_context_and_gate(tmp_path,monkeypatch):
 p=tmp_path/"state.json"; wm.STATE=p; p.write_text(json.dumps({"schema":"hermes.workspace-members/v1","owner_user_id":1,"members":[{"user_id":2,"name":"Son"}],"grants":[],"pending":[]})); monkeypatch.setattr(wm,"_notify_owner",lambda *a:None)
 ctx=wm.observe(session_id="s",sender_id="2"); assert "owner" in ctx["context"] and "Son" in ctx["context"]
 v=wm.guard(session_id="s",tool_name="web_search"); assert v["action"]=="block"
 d=json.loads(p.read_text()); assert d["pending"][0]["tool_name"]=="web_search"
 d["grants"]=[{"member_user_id":2,"tool_name":"web_search","expires_at":int(time.time())+60}]; p.write_text(json.dumps(d)); assert wm.guard(session_id="s",tool_name="web_search") is None
 assert wm.guard(session_id="s",tool_name="memory_search") is None


def test_owner_notice_uses_profile_bot_api(tmp_path, monkeypatch):
    # Bots on the local Bot API server must never be called through the cloud API:
    # one cloud request logs the bot into the cloud and breaks update delivery.
    calls = []

    class Reply:
        def read(self):
            return b"{}"

    monkeypatch.setattr(wm.urllib.request, "urlopen", lambda req, timeout=0: calls.append(req.full_url) or Reply())
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test-only")
    monkeypatch.setattr(wm, "STATE", tmp_path / "workspace-members.json")
    member, state = {"user_id": 2, "name": "Son"}, {"owner_user_id": 1}
    wm._notify_owner(state, member, "web_search")
    (tmp_path / "config.yaml").write_text(
        "platforms:\n  telegram:\n    extra:\n      base_url: http://127.0.0.1:8082/bot/\n", encoding="utf-8")
    wm._notify_owner(state, member, "web_search")
    assert calls == ["https://api.telegram.org/bot123:test-only/sendMessage",
                     "http://127.0.0.1:8082/bot123:test-only/sendMessage"]
