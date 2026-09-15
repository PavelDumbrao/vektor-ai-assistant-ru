from __future__ import annotations
import json, os, threading, time, urllib.parse, urllib.request
from pathlib import Path

STATE = Path(os.environ.get("HERMES_HOME", str(Path.home()/".hermes"))) / "workspace-members.json"
_lock=threading.RLock(); _actors={}
MEMORY_TOOL_PREFIXES=("memory","passive_secretary_")

def _load():
    try:
        d=json.loads(STATE.read_text(encoding="utf-8"))
        return d if d.get("schema")=="hermes.workspace-members/v1" else {}
    except Exception: return {}

def _member(d, uid): return next((m for m in d.get("members",[]) if str(m.get("user_id"))==str(uid)),None)
def _owner(d,uid): return str(d.get("owner_user_id") or "")==str(uid)
def _allowed(d,uid,tool):
    if tool.startswith(MEMORY_TOOL_PREFIXES): return True
    now=int(time.time())
    return any(str(g.get("member_user_id"))==str(uid) and g.get("tool_name")==tool and int(g.get("expires_at") or 0)>now for g in d.get("grants",[]))

def _write_pending(d, member, tool):
    with _lock:
        fresh=_load() or d; pending=fresh.setdefault("pending",[])
        if any(str(x.get("member_user_id"))==str(member["user_id"]) and x.get("tool_name")==tool for x in pending): return False
        pending.append({"member_user_id":int(member["user_id"]),"member_name":str(member.get("name") or "Участник")[:120],"tool_name":tool,"created_at":int(time.time())})
        tmp=STATE.with_suffix(".tmp"); tmp.write_text(json.dumps(fresh,ensure_ascii=False)+"\n",encoding="utf-8"); os.chmod(tmp,0o600); os.replace(tmp,STATE); return True

def _notify_owner(d,member,tool):
    token=(os.getenv("TELEGRAM_BOT_TOKEN") or "").strip(); owner=str(d.get("owner_user_id") or "")
    if not token or not owner: return
    text=f"🔐 {member.get('name') or 'Участник'} просит доступ к инструменту {tool}.\n\nОткрой Hermes Forge → Команда и разреши на 1, 7 или 30 дней."
    body=urllib.parse.urlencode({"chat_id":owner,"text":text}).encode()
    try: urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",data=body),timeout=8).read()
    except Exception: pass

def observe(**kw):
    sid=str(kw.get("session_id") or ""); uid=str(kw.get("sender_id") or ""); d=_load()
    if not sid or not uid or not d: return None
    with _lock: _actors[sid]=uid
    if _owner(d,uid):
        return {"context":f"[Hermes workspace identity] Current actor is the workspace owner (Telegram ID {uid}).", "persist":False}
    m=_member(d,uid)
    if not m: return None
    owner=d.get("owner_user_id")
    return {"context":f"[Hermes workspace identity] You are speaking with invited member {m.get('name') or uid} (Telegram ID {uid}). The owner of this Hermes is Telegram ID {owner}. The member has full workspace memory access. Never confuse the current actor with the owner. Tool actions are separately permission-gated by the owner.","persist":False}

def guard(**kw):
    sid=str(kw.get("session_id") or ""); tool=str(kw.get("tool_name") or ""); d=_load(); uid=_actors.get(sid,"")
    if not d or not uid or _owner(d,uid): return None
    m=_member(d,uid)
    if not m: return {"action":"block","message":"Этот пользователь не является участником данного Hermes workspace."}
    if _allowed(d,uid,tool): return None
    first=_write_pending(d,m,tool)
    if first: _notify_owner(d,m,tool)
    return {"action":"block","message":f"Для инструмента {tool} требуется разрешение владельца этого Hermes. Запрос владельцу уже отправлен; после разрешения повтори действие."}

def register(ctx):
    ctx.register_hook("pre_llm_call",observe); ctx.register_hook("pre_tool_call",guard)
