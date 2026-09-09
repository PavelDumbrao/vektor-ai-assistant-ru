#!/usr/bin/env python3
"""Check the published server boundary and frozen component digests."""
from __future__ import annotations

import copy
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



def canonical_modern_client(config, owner):
    """Normalize identity and intentionally client-specific tool surfaces."""
    value=copy.deepcopy(config)

    def normalize_paths(item):
        if isinstance(item,dict):
            return {key:normalize_paths(val) for key,val in item.items()}
        if isinstance(item,list):
            return [normalize_paths(val) for val in item]
        if isinstance(item,str):
            return item.replace(f'/home/{owner}', '/home/OWNER')
        return item

    value=normalize_paths(value)
    value.get('agent',{}).pop('disabled_toolsets',None)
    value.pop('plugins',None)
    telegram=value.get('platforms',{}).get('telegram',{})
    home=telegram.get('home_channel',{})
    if isinstance(home,dict):
        home['name']='OWNER'
    extra=telegram.get('extra',{})
    if isinstance(extra,dict):
        admins=extra.get('allow_admin_from',[])
        extra['allow_admin_from']=['OWNER_TELEGRAM_USER_ID'] if 'OWNER_TELEGRAM_USER_ID' in admins else []
        extra.pop('group_passive_enabled',None)
        extra.pop('group_passive_chat_ids',None)
        # Enrollment helpers are deliberately profile-specific; they can only
        # start owner consent and therefore do not define the modern runtime baseline.
        extra.pop('group_passive_trusted_inviter_ids',None)
    maton=value.get('mcp_servers',{}).get('maton')
    if isinstance(maton,dict):
        maton.pop('enabled',None)
    return value

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

    baseline_owner='vyacheslav'
    baseline_profile=next((item for item in fleet if item['owner_label']==baseline_owner),None)
    if baseline_profile is None:
        errors.append('server/fleet.json: modern client baseline is missing')
    else:
        baseline_config=yaml.safe_load((ROOT/'server'/baseline_profile['config_template']).read_text())
        baseline=canonical_modern_client(baseline_config,baseline_owner)
        for profile in fleet:
            owner=profile['owner_label']
            if profile.get('variant')!='modern' or owner in {'pavel',baseline_owner}:
                continue
            config=yaml.safe_load((ROOT/'server'/profile['config_template']).read_text())
            if canonical_modern_client(config,owner)!=baseline:
                errors.append(owner+': modern client base config drifted from vyacheslav baseline')
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'Public bundle: {count} files checked; no credentials/private state; checksums match')


if __name__=='__main__':
    main()
