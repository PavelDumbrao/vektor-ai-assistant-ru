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
          'modules/maton-legacy', 'modules/profile-tools']
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
            if path.name=='.env' or (path.name.endswith('.env') and not path.name.endswith('.example')):
                errors.append(relative+': environment secret file')
            text=path.read_text(errors='replace')
            if TOKEN.search(text) or PROVIDER_KEY.search(text) or PEM.search(text):
                errors.append(relative+': credential pattern (value withheld)')
    components=json.loads((ROOT/'server/components.json').read_text())
    for relative,expected in components['files'].items():
        path=(ROOT/relative).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file() or digest(path)!=expected:
            errors.append(relative+': component digest mismatch')
    release=ROOT/'modules/shared-runtime/releases/v0.21.0'
    spec=json.loads((release/'vektor3.json').read_text())
    for name,expected in spec['patch_sha256'].items():
        if digest(release/name)!=expected:
            errors.append(name+': patch digest mismatch')
    if digest(release/'server-requirements.lock')!=spec['dependencies_sha256']:
        errors.append('server-requirements.lock: digest mismatch')
    for profile in json.loads((ROOT/'server/fleet.json').read_text())['profiles']:
        config=yaml.safe_load((ROOT/'server'/profile['config_template']).read_text())
        extra=config['platforms']['telegram']['extra']
        if extra['business_owner_ids']!=['OWNER_TELEGRAM_USER_ID'] or extra['business_reply_enabled'] is not False:
            errors.append(profile['owner_label']+': unsafe public example')
        if config['model']['api_key']!='${LLM_API_KEY}':
            errors.append(profile['owner_label']+': provider secret is not an env reference')
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'Public bundle: {count} files checked; no credentials/private state; checksums match')


if __name__=='__main__':
    main()
