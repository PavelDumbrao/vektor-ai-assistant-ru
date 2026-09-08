#!/usr/bin/env python3
"""Check the published server boundary and frozen component digests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ['server', 'modules/focus-assistant', 'modules/ai-fixer-social',
          'modules/maton-legacy', 'modules/profile-tools', 'modules/vektor-live']
PRIVATE_FILES = {'settings.json','auth.json','credentials.json','secrets.json',
                 'USER.md','MEMORY.md','SOUL.md','SOURCE_NOTES.md','PROFILE.md'}
TOKEN = re.compile(r'\b\d{7,12}:[A-Za-z0-9_-]{30,}\b')
PROVIDER_KEY = re.compile(r'\b(?:sk-proj-|sk-or-v1-|ghp_|github_pat_)[A-Za-z0-9_-]{25,}\b')
PEM = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    errors=[]
    count=0
    for scope in SCOPES:
        for path in (ROOT/scope).rglob('*'):
            if not path.is_file() or any(x in path.parts for x in ['__pycache__','.pytest_cache']):
                continue
            count+=1
            relative=str(path.relative_to(ROOT))
            if path.is_symlink() or path.name in PRIVATE_FILES or path.suffix in {'.db','.sqlite','.sqlite3','.session','.pem','.key','.log','.pyc'}:
                errors.append(relative+': private/runtime file')
            if (path.name.startswith('.env') or path.name.endswith('.env')) and not path.name.endswith('.example'):
                errors.append(relative+': environment secret file')
            text=path.read_text(errors='replace')
            if TOKEN.search(text) or PROVIDER_KEY.search(text) or PEM.search(text):
                errors.append(relative+': credential pattern (value withheld)')
    components=json.loads((ROOT/'server/components.json').read_text())
    for relative,expected in components['files'].items():
        path=(ROOT/relative).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file() or digest(path)!=expected:
            errors.append(relative+': component digest mismatch')
    vektor_root=ROOT/'modules/vektor-live'
    vektor_version=(vektor_root/'VERSION').read_text().strip()
    vektor_spec=json.loads((vektor_root/'releases'/vektor_version/'manifest.json').read_text())
    for relative,expected in vektor_spec['files_sha256'].items():
        path=vektor_root/relative
        if not path.is_file() or digest(path)!=expected:
            errors.append('modules/vektor-live/'+relative+': release digest mismatch')
    release=ROOT/'modules/shared-runtime/releases/v0.21.0'
    spec=json.loads((release/'vektor3.json').read_text())
    for name,expected in spec['patch_sha256'].items():
        if digest(release/name)!=expected:
            errors.append(name+': patch digest mismatch')
    if digest(release/'server-requirements.lock')!=spec['dependencies_sha256']:
        errors.append('server-requirements.lock: digest mismatch')
    fleet=json.loads((ROOT/'server/fleet.json').read_text())['profiles']
    owners=[profile['owner_label'] for profile in fleet]
    homes=[profile['hermes_home'] for profile in fleet]
    services=[profile['service'] for profile in fleet]
    if len(owners)!=len(set(owners)) or len(homes)!=len(set(homes)) or len(services)!=len(set(services)):
        errors.append('server/fleet.json: duplicate owner, home or service')
    for profile in fleet:
        owner=profile['owner_label']
        config=yaml.safe_load((ROOT/'server'/profile['config_template']).read_text())
        extra=config['platforms']['telegram']['extra']
        if profile['hermes_home']!=f'/home/{owner}/.hermes' or config['terminal']['cwd']!=f'/home/{owner}/workspace':
            errors.append(owner+': cross-profile home/workspace path')
        if extra['business_owner_ids']!=['OWNER_TELEGRAM_USER_ID'] or extra['business_reply_enabled'] is not False:
            errors.append(owner+': unsafe public example')
        admins=extra.get('allow_admin_from',[])
        if any(item not in {'OWNER_TELEGRAM_USER_ID','TECH_ADMIN_TELEGRAM_USER_ID'} for item in admins):
            errors.append(owner+': real Telegram admin id in public example')
        if config['model']['api_key']!='${LLM_API_KEY}':
            errors.append(owner+': provider secret is not an env reference')
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'Public bundle: {count} files checked; no credentials/private state; checksums match')


if __name__=='__main__':
    main()
