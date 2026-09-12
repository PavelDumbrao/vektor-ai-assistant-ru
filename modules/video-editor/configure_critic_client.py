#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import secrets
import stat
import tempfile
from pathlib import Path

RUNTIME = Path("/opt/vektor/video-editor")
PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
NOFOLLOW = getattr(os,"O_NOFOLLOW",0)
DIRECTORY = getattr(os,"O_DIRECTORY",0)


def _open_dir(path: Path) -> int:
    try: fd=os.open(str(path),os.O_RDONLY|DIRECTORY|NOFOLLOW)
    except OSError as exc: raise RuntimeError("unsafe_directory") from exc
    if not stat.S_ISDIR(os.fstat(fd).st_mode): os.close(fd); raise RuntimeError("unsafe_directory")
    return fd
def _read_token(dir_fd: int, uid: int) -> str:
    try: fd=os.open("critic_token",os.O_RDONLY|NOFOLLOW,dir_fd=dir_fd)
    except FileNotFoundError: return ""
    except OSError as exc: raise RuntimeError("critic_token_unsafe") from exc
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or (info.st_mode & 0o077) or info.st_size > 512:
            raise RuntimeError("critic_token_unsafe")
        value=os.read(fd,513).decode().strip()
    finally: os.close(fd)
    return value if 32 <= len(value) <= 256 and not any(ch.isspace() for ch in value) else ""


def _write_token(dir_fd: int, uid: int, gid: int, token: str) -> None:
    name=f".critic_token.{secrets.token_hex(10)}"
    fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|NOFOLLOW,0o600,dir_fd=dir_fd)
    try:
        os.fchown(fd,uid,gid); os.fchmod(fd,0o600); os.write(fd,(token+"\n").encode()); os.fsync(fd)
    finally: os.close(fd)
    try: os.replace(name,"critic_token",src_dir_fd=dir_fd,dst_dir_fd=dir_fd)
    finally:
        try: os.unlink(name,dir_fd=dir_fd)
        except FileNotFoundError: pass
def _root_dir(path: Path) -> None:
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise RuntimeError("shared_critic_dir_unsafe")
    path.chmod(0o700)


def _atomic(path: Path, data: bytes) -> None:
    fd,name=tempfile.mkstemp(prefix=".critic-",dir=path.parent); temp=Path(name)
    try:
        os.fchmod(fd,0o600); os.fchown(fd,0,0); os.write(fd,data); os.fsync(fd); os.close(fd); fd=-1
        os.replace(temp,path); path.chmod(0o600); os.chown(path,0,0)
    finally:
        if fd>=0: os.close(fd)
        temp.unlink(missing_ok=True)


def configure(owner: str, mode: str) -> None:
    if os.geteuid()!=0: raise RuntimeError("root_required")
    if not PROFILE_RE.fullmatch(owner) or mode not in {"off","final","all"}: raise RuntimeError("invalid_argument")
    entry=pwd.getpwnam(owner)
    if entry.pw_uid<=0 or Path(entry.pw_dir).name!=owner: raise RuntimeError("invalid_owner")
    hermes_fd=_open_dir(Path(entry.pw_dir)/".hermes")
    try:
        info=os.fstat(hermes_fd)
        if info.st_uid!=entry.pw_uid: raise RuntimeError("profile_home_unsafe")
        try:
            os.mkdir("video_editor",mode=0o700,dir_fd=hermes_fd)
            os.chown("video_editor",entry.pw_uid,entry.pw_gid,dir_fd=hermes_fd,follow_symlinks=False)
        except FileExistsError: pass
        local_fd=os.open("video_editor",os.O_RDONLY|DIRECTORY|NOFOLLOW,dir_fd=hermes_fd)
        try:
            local_info=os.fstat(local_fd)
            if not stat.S_ISDIR(local_info.st_mode) or local_info.st_uid!=entry.pw_uid: raise RuntimeError("profile_video_editor_dir_unsafe")
            os.fchmod(local_fd,0o700)
            token=_read_token(local_fd,entry.pw_uid)
            if not token:
                token=secrets.token_urlsafe(32); _write_token(local_fd,entry.pw_uid,entry.pw_gid,token)
        finally: os.close(local_fd)
    finally: os.close(hermes_fd)
    clients=RUNTIME/"private"/"critic-clients"; policies=RUNTIME/"private"/"critic-policies"
    _root_dir(clients); _root_dir(policies)
    _atomic(clients/f"{owner}.token",(token+"\n").encode())
    stages=[] if mode=="off" else (["master"] if mode=="final" else ["cut","master"])
    policy={"enabled":bool(stages),"stages":stages,"provider":"lingsuan.top","model":"gemini-3.8-flash-medium","fallback_models":["gemini-3.8-flash-low","gemini-3.8-flash-high"]}
    _atomic(policies/f"{owner}.json",(json.dumps(policy,separators=(",",":"))+"\n").encode())
    print("critic_client_configured=true"); print(f"owner={owner}"); print(f"mode={mode}"); print("token_printed=false")
def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--owner",required=True); parser.add_argument("--mode",choices=["off","final","all"],default="off")
    args=parser.parse_args()
    try: configure(args.owner,args.mode)
    except Exception as exc:
        print(f"error={type(exc).__name__}:{exc}",file=__import__("sys").stderr); return 1
    return 0


if __name__=="__main__": raise SystemExit(main())
