from __future__ import annotations
import argparse, hashlib, json, os, pwd, re, shutil, subprocess, sys, time
from pathlib import Path

SRC=Path("/opt/vektor-dev/vektor-ai-assistant-ru/modules/passive-secretary/passive_secretary_plugin")
FILES=(
 "__init__.py","archive.py","controller.py","normalizer.py","outbound.py",
 "owner_intent.py","plugin.yaml","recall.py","retrieval.py","schema.sql","settings.py",
)
OWNER_RE=re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

def digest(path: Path)->str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def run(*args, check=True):
    p=subprocess.run(list(map(str,args)),capture_output=True,text=True,timeout=120)
    if check and p.returncode:
        raise RuntimeError((p.stderr or p.stdout)[-5000:])
    return p

def service_active(owner:str)->bool:
    return run("systemctl","is-active",f"{owner}-hermes.service",check=False).stdout.strip()=="active"

def main(owner:str):
    if os.geteuid()!=0 or not OWNER_RE.fullmatch(owner):
        raise ValueError("administrator_and_valid_owner_required")
    entry=pwd.getpwnam(owner)
    uid,gid=entry.pw_uid,entry.pw_gid
    home=Path(entry.pw_dir)/".hermes"
    live=home/"plugins/passive-secretary"
    if not live.is_dir() or not (live/"settings.json").is_file():
        raise ValueError("managed_passive_secretary_required")
    agent=home/"hermes-agent"
    resolved_agent=agent.resolve()
    if not str(resolved_agent).startswith("/opt/vektor/releases/"):
        raise ValueError("managed_runtime_required")
    cfg=home/"config.yaml"
    settings=live/"settings.json"
    cfg_sha=digest(cfg); settings_sha=digest(settings)
    log=home/"logs/agent.log"
    start_lines=len(log.read_text(encoding="utf-8",errors="ignore").splitlines()) if log.exists() else 0

    stamp=time.strftime("%Y%m%d-%H%M%S")
    backup=home/"backups"/f"passive-identity-plugin-{stamp}"
    backup.mkdir(parents=True,mode=0o700)
    shutil.copytree(live,backup/"passive-secretary",symlinks=True)
    for root,dirs,files in os.walk(backup):
        os.chown(root,uid,gid)
        for name in dirs:
            os.chown(os.path.join(root,name),uid,gid)
        for name in files:
            os.chown(os.path.join(root,name),uid,gid)

    receipt={
      "owner":owner,"runtime":str(resolved_agent),"backup":str(backup),
      "config_sha_before":cfg_sha,"settings_sha_before":settings_sha,
    }

    def restore():
        run("systemctl","stop",f"{owner}-hermes.service",check=False)
        replacement=live.with_name(live.name+".rollback")
        if replacement.exists():
            shutil.rmtree(replacement)
        shutil.copytree(backup/"passive-secretary",replacement,symlinks=True)
        if live.exists():
            shutil.rmtree(live)
        os.replace(replacement,live)
        for root,dirs,files in os.walk(live):
            os.chown(root,uid,gid)
            for name in dirs:
                os.chown(os.path.join(root,name),uid,gid)
            for name in files:
                os.chown(os.path.join(root,name),uid,gid)
        run("systemctl","start",f"{owner}-hermes.service",check=False)

    try:
        run("systemctl","stop",f"{owner}-hermes.service")
        for name in FILES:
            src=SRC/name
            if not src.is_file():
                raise RuntimeError(f"source_missing:{name}")
            tmp=live/f".{name}.new"
            tmp.write_bytes(src.read_bytes())
            os.chown(tmp,uid,gid); os.chmod(tmp,0o600)
            os.replace(tmp,live/name)
        os.chmod(live,0o700); os.chown(live,uid,gid)
        pycache=live/"__pycache__"
        if pycache.exists():
            shutil.rmtree(pycache)
        if digest(cfg)!=cfg_sha or digest(settings)!=settings_sha:
            raise RuntimeError("private_config_changed")

        py=agent/"venv/bin/python"
        compile_files=[str(live/name) for name in FILES if name.endswith(".py")]
        run(py,"-m","py_compile",*compile_files)

        probe=r'''
import importlib, sys, types
from pathlib import Path
root=Path(sys.argv[1])
pkg=types.ModuleType("fleet_ps")
pkg.__path__=[str(root)]
sys.modules["fleet_ps"]=pkg
for name in ("settings","retrieval","archive","normalizer","recall","outbound","owner_intent","controller"):
    importlib.import_module("fleet_ps."+name)
import telegram_business_rights
from plugins.platforms.telegram import adapter
import gateway.run
assert hasattr(adapter,"resolve_telegram_identities_for_current_session")
assert hasattr(adapter.TelegramAdapter,"resolve_private_chat_identities")
print("plugin_import_ok")
'''
        env=os.environ.copy()
        env["PYTHONPATH"]=str(agent)
        p=subprocess.run([str(py),"-c",probe,str(live)],env=env,capture_output=True,text=True,timeout=60)
        if p.returncode or "plugin_import_ok" not in p.stdout:
            raise RuntimeError("plugin_import_failed:"+((p.stderr or p.stdout)[-3000:]))

        run("systemctl","start",f"{owner}-hermes.service")
        deadline=time.time()+70
        fresh=""
        while time.time()<deadline:
            if log.exists():
                lines=log.read_text(encoding="utf-8",errors="ignore").splitlines()
                fresh="\n".join(lines[start_lines:])
            if "Failed to load plugin 'passive-secretary'" in fresh:
                raise RuntimeError("fresh_plugin_load_failed")
            if service_active(owner) and "Passive secretary plugin registered" in fresh and "Gateway running with" in fresh:
                break
            time.sleep(1)
        else:
            raise RuntimeError("fresh_gateway_readiness_timeout")
        if digest(cfg)!=cfg_sha or digest(settings)!=settings_sha:
            raise RuntimeError("private_config_changed_after_restart")

        receipt.update({
          "state":"active",
          "config_sha_after":digest(cfg),
          "settings_sha_after":digest(settings),
          "plugin_import_verified":True,
          "plugin_registered":True,
          "gateway_running":True,
        })
        (backup/"rollout.json").write_text(json.dumps(receipt,indent=2,ensure_ascii=False))
        print(json.dumps(receipt,ensure_ascii=False))
    except Exception as exc:
        restore()
        receipt.update({"state":"rolled_back","error":type(exc).__name__,"detail":str(exc)[:1000]})
        (backup/"rollout.json").write_text(json.dumps(receipt,indent=2,ensure_ascii=False))
        print(json.dumps(receipt,ensure_ascii=False),file=sys.stderr)
        raise

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--owner",required=True)
    ns=ap.parse_args()
    main(ns.owner)
