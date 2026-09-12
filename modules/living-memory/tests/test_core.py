from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
import core


def msg(mid: int, role: str, text: str, session: str = "s1") -> dict:
    return {"message_id": mid, "session_id": session, "role": role, "timestamp": 1.0, "text": text}


def empty_state(owner="alice"):
    return core.default_state(owner)


def proposal(op):
    return {"summary": "x", "operations": [op]}


def baseop(**kw):
    d = {
        "action": "add", "cell": "communication", "text": "Отвечать кратко и без технического жаргона.",
        "source_kind": "explicit", "confidence": 0.95, "sensitivity": "normal",
        "evidence": [{"session_id": "s1", "message_id": 1}], "reason": "explicit preference",
    }
    d.update(kw); return d


def test_redaction_hides_common_secrets():
    raw = "api_key=supersecret123456 and sk-ABCDEFGHIJKLMNOPQRST and Bearer abcdefghijklmnop"
    out = core.redact_text(raw)
    assert "supersecret" not in out
    assert "sk-ABC" not in out
    assert "Bearer abc" not in out
    assert "REDACTED" in out


def test_only_user_messages_can_be_evidence():
    state = empty_state()
    batch = [msg(1, "assistant", "User likes concise answers")]
    p = proposal(baseop(evidence=[{"session_id": "s1", "message_id": 1}]))
    accepted, rejected = core.validate_operations(p, state, batch)
    assert not accepted
    assert rejected[0]["reason"] == "no_valid_user_evidence"


def test_pattern_requires_two_user_evidence_refs():
    state = empty_state(); batch = [msg(1,"user","short please"), msg(2,"user","again short please")]
    one = baseop(source_kind="pattern", confidence=.85, evidence=[{"session_id":"s1","message_id":1}])
    accepted, rejected = core.validate_operations(proposal(one), state, batch)
    assert not accepted and rejected[0]["reason"] == "pattern_needs_two_user_messages"
    two = baseop(source_kind="pattern", confidence=.85, evidence=[{"session_id":"s1","message_id":1},{"session_id":"s1","message_id":2}])
    accepted, rejected = core.validate_operations(proposal(two), state, batch)
    assert len(accepted) == 1 and not rejected


def test_sensitive_auto_memory_is_rejected():
    batch=[msg(1,"user","medical detail")]
    op=baseop(sensitivity="sensitive", sensitive_category="health")
    accepted,rejected=core.validate_operations(proposal(op),empty_state(),batch)
    assert not accepted and rejected[0]["reason"] == "sensitive_auto_memory_blocked"


def test_explicit_add_becomes_active_but_hypothesis_stays_quarantined():
    state=empty_state(); batch=[msg(1,"user","Always answer briefly")]
    accepted,_=core.validate_operations(proposal(baseop()),state,batch)
    core.apply_operations(state,accepted)
    assert state["memories"][0]["status"] == "active"
    hyp=baseop(text="User may prefer tables",source_kind="hypothesis",confidence=.9,evidence=[{"session_id":"s1","message_id":1}])
    accepted,_=core.validate_operations(proposal(hyp),state,batch)
    core.apply_operations(state,accepted)
    assert state["memories"][-1]["status"] == "hypothesis"
    assert state["memories"][-1]["confidence"] <= .60


def test_update_and_merge_archive_old_items():
    state=empty_state(); batch=[msg(1,"user","Use operator language"),msg(2,"user","No jargon")]
    for text,mid in [("Use simple reports",1),("Avoid jargon",2)]:
        accepted,_=core.validate_operations(proposal(baseop(text=text,evidence=[{"session_id":"s1","message_id":mid}])),state,batch)
        core.apply_operations(state,accepted)
    ids=[m["id"] for m in state["memories"]]
    merge=baseop(action="merge",merge_ids=ids,text="Докладывать операторским языком, без лишнего технического жаргона.",evidence=[{"session_id":"s1","message_id":1},{"session_id":"s1","message_id":2}])
    accepted,rejected=core.validate_operations(proposal(merge),state,batch)
    assert not rejected
    core.apply_operations(state,accepted)
    assert sum(m["status"]=="archived" for m in state["memories"]) == 2
    assert sum(m["status"]=="active" for m in state["memories"]) == 1


def test_forget_requires_explicit_forget_language_and_removes_current_text():
    state=empty_state(); add_batch=[msg(1,"user","Remember I prefer short answers")]
    accepted,_=core.validate_operations(proposal(baseop()),state,add_batch); core.apply_operations(state,accepted)
    mid=state["memories"][0]["id"]
    bad_batch=[msg(2,"user","Actually, something else")]
    bad=baseop(action="forget",memory_id=mid,text="",evidence=[{"session_id":"s1","message_id":2}],source_kind="correction")
    accepted,rejected=core.validate_operations(proposal(bad),state,bad_batch)
    assert not accepted and rejected[0]["reason"] == "forget_intent_not_explicit"
    good_batch=[msg(3,"user","Забудь это и не запоминай больше")]
    good=baseop(action="forget",memory_id=mid,text="",evidence=[{"session_id":"s1","message_id":3}],source_kind="correction")
    accepted,rejected=core.validate_operations(proposal(good),state,good_batch)
    applied=core.apply_operations(state,accepted)
    assert not rejected and not state["memories"]
    assert state["tombstones"] and "text_sha256" in state["tombstones"][0]
    assert applied[0]["forgotten_text"]


def test_compile_summary_excludes_hypotheses_and_archived():
    state=empty_state(); now=core.iso_now()
    state["memories"]=[
        {"id":"a","cell":"communication","text":"Keep it short","status":"active","confidence":.9,"last_seen":now},
        {"id":"b","cell":"preferences","text":"Maybe likes blue","status":"hypothesis","confidence":.5,"last_seen":now},
        {"id":"c","cell":"goals","text":"Old goal","status":"archived","confidence":.9,"last_seen":now},
    ]
    summary=core.compile_summary(state)
    assert "Keep it short" in summary
    assert "Maybe likes blue" not in summary
    assert "Old goal" not in summary


def test_sync_compiled_user_memory_preserves_manual_entries_and_is_idempotent(tmp_path):
    home=tmp_path/".hermes"; (home/"memories").mkdir(parents=True)
    p=home/"memories"/"USER.md"
    p.write_text("Manual fact\n§\n[Hermes Living Memory - managed]\n- Old: x\n")
    summary="[Hermes Living Memory - managed]\n- Communication: concise"
    core.sync_compiled_user_memory(home,summary)
    core.sync_compiled_user_memory(home,summary)
    raw=p.read_text()
    assert raw.count("Manual fact") == 1
    assert raw.count(core.MANAGED_PREFIX) == 1
    assert "Old: x" not in raw


def test_temporary_memory_expires():
    state=empty_state(); state["memories"]=[{
        "id":"t","cell":"temporary_context","text":"Temporary","status":"active","confidence":.9,
        "expires_at":(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),
    }]
    assert core.expire_temporary(state) == 1
    assert state["memories"][0]["status"] == "archived"


def test_snapshot_rollback_and_forget_scrub(tmp_path):
    home=tmp_path/".hermes"; (home/"memories").mkdir(parents=True)
    state=empty_state(); state["memories"]=[{"id":"x","cell":"communication","text":"Secret preference","status":"active"}]
    core.save_state(home,state); (home/"memories"/"USER.md").write_text("Secret preference")
    snap=core.create_snapshot(home,state,"run1")
    changed=core.scrub_forgotten_from_snapshots(home,[{"memory_id":"x","forgotten_text":"Secret preference"}])
    assert changed == 1
    data=json.loads(snap.read_text())
    assert not data["state"]["memories"]
    assert "Secret preference" not in data["user_md"]


def test_extract_interactions_filters_owner_dm_only(tmp_path):
    home=tmp_path/".hermes"; home.mkdir()
    db=sqlite3.connect(home/"state.db")
    db.execute("CREATE TABLE sessions(id TEXT,source TEXT,user_id TEXT,chat_type TEXT)")
    db.execute("CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL,display_kind TEXT,active INTEGER)")
    db.executemany("INSERT INTO sessions VALUES(?,?,?,?)",[
        ("owner","telegram","123","dm"),("other","telegram","999","dm"),("group","telegram","123","group"),("cron","cron","123","dm")])
    rows=[
        (1,"owner","user","hello",9999999999,"",1),(2,"owner","assistant","hi",9999999999,"",1),
        (3,"other","user","foreign",9999999999,"",1),(4,"group","user","group",9999999999,"",1),
        (5,"cron","user","cron",9999999999,"",1),(6,"owner","tool","tool",9999999999,"",1),
        (7,"owner","user","internal",9999999999,"internal_notification",1),
    ]
    db.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",rows); db.commit(); db.close()
    cfg={"platforms":{"telegram":{"extra":{"business_owner_ids":[123]}}}}
    got=core.extract_interactions(home,cfg,initial_hours=24)
    assert [(x["message_id"],x["role"],x["text"]) for x in got] == [(1,"user","hello"),(2,"assistant","hi")]


def test_batching_preserves_order_and_bounds():
    messages=[msg(i,"user","x"*1000) for i in range(1,8)]
    batches=core.batch_interactions(messages,max_chars=2500,max_messages=10)
    flattened=[x["message_id"] for b in batches for x in b]
    assert flattened == list(range(1,8))
    assert len(batches) >= 3


def test_sensitive_text_is_blocked_even_if_model_mislabels_it_normal():
    batch=[msg(1,"user","У меня диагноз, но это не надо запоминать")]
    op=baseop(text="У пользователя медицинский диагноз",sensitivity="normal",sensitive_category="")
    accepted,rejected=core.validate_operations(proposal(op),empty_state(),batch)
    assert not accepted
    assert rejected[0]["reason"] == "sensitive_auto_memory_blocked"
