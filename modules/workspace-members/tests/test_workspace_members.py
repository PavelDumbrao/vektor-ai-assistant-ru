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
