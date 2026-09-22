#!/usr/bin/env python3
"""Build a new managed release from a verified source archive; never restart users."""
from __future__ import annotations

import argparse
import email.message
import json
import os
import re
import subprocess
import tarfile
import tomllib
from pathlib import Path

import prepare_runtime as common

ROOT = Path('/opt/vektor')
UV = ROOT / 'tools/uv-0.12.3/uv'
PYTHON = ROOT / 'python/cpython-3.11.15-linux-x86_64-gnu/bin/python3.11'
RELEASE_RE = re.compile(r'^hermes-[A-Za-z0-9_.-]{1,80}$')


def package_names(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        match = re.match(r'^([A-Za-z0-9][A-Za-z0-9_.-]*)[=\[<>!~; ]', line)
        if match:
            names.add(match[1].lower().replace('_', '-'))
    return names


def run(*args, cwd=None):
    result = subprocess.run(list(map(str, args)), cwd=cwd, env=common.safe_env(),
                            capture_output=True, text=True, timeout=600)
    if result.returncode:
        # These commands have no profile environment or credentials.
        raise RuntimeError(result.stderr[-6000:])
    return result.stdout


def metadata(project: dict) -> dict:
    message = email.message.Message()
    message['Metadata-Version'] = '2.3'
    message['Name'] = project['name']
    message['Version'] = project['version']
    message['Requires-Python'] = project['requires-python']
    for requirement in project['dependencies']:
        message['Requires-Dist'] = requirement
    entry_points = project.get('scripts', {})
    entries = '[console_scripts]\n' + ''.join(f'{name} = {value}\n' for name, value in entry_points.items())
    return {'packages': {'hermes-agent': project['version']}, 'entry_points': entry_points,
            'metadata': {'METADATA': message.as_string(), 'entry_points.txt': entries,
                         'WHEEL': 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}}


def prepare(archive: Path, checksum: str, overlay: Path | None, release_id: str, *, dependencies: Path | None, patches=()):
    if os.geteuid() != 0 or not RELEASE_RE.fullmatch(release_id):
        raise ValueError('administrator_and_valid_release_required')
    if common.digest(archive) != checksum:
        raise ValueError('upstream_archive_digest_mismatch')
    if (overlay is None) == (not patches):
        raise ValueError('provide_exactly_one_overlay_or_patch_series')
    release = ROOT / 'releases' / release_id
    if release.exists():
        raise ValueError('release_already_exists')
    release.mkdir(mode=0o755)
    code = release / 'hermes-agent'
    code.mkdir()
    with tarfile.open(archive) as source:
        source.extractall(code, filter='data')
    overlay_manifest = common.source_manifest(overlay) if overlay is not None else {}
    # Overlay is reviewed source, not executable installer code. Never mutate
    # the shared older release or any user's private home.
    for relative, item in overlay_manifest.items():
        if item['kind'] != 'file' or not (relative.endswith('.py') or relative == 'plugins/web/tavily/plugin.yaml'):
            raise ValueError('invalid_release_overlay')
        target = code / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((overlay / relative).read_bytes())
    for patch in patches:
        run('git', 'apply', '--check', patch.resolve(), cwd=code)
        run('git', 'apply', patch.resolve(), cwd=code)
    project = tomllib.loads((code / 'pyproject.toml').read_text())['project']
    if project['version'] != '0.21.0':
        raise ValueError('unexpected_upstream_version')
    (code / '.hermes_build_sha').write_text('29112bef099274229cadff79cdff7bf7b99c4b77\n')
    dependency_file = release / 'requirements.lock'
    if dependencies is None:
        exported = release / 'core-requirements.in'
        run(UV, '--no-config', 'export', '--frozen', '--no-dev', '--extra', 'mcp',
            '--no-emit-project', '--no-hashes', '--format', 'requirements-txt',
            '--output-file', exported, cwd=code)
        text = exported.read_text()
        names = package_names(text) | {'hermes-agent', 'python-telegram-bot'}
        old = (ROOT / 'admin/requirements.lock').read_text()
        preserved = [line for line in old.splitlines()
                     if line and line.split('==')[0].lower().replace('_', '-') not in names]
        combined = release / 'requirements.in'
        combined.write_text(text + '\npython-telegram-bot[webhooks]==22.8\n' + '\n'.join(preserved) + '\n')
        run(UV, '--no-config', 'pip', 'compile', '--python', PYTHON, '--no-build',
            '--generate-hashes', '--output-file', dependency_file, combined, cwd=release)
    else:
        if not dependencies.resolve().is_relative_to(ROOT / 'releases'):
            raise ValueError('dependency_lock_outside_managed_releases')
        dependency_file.write_bytes(dependencies.read_bytes())
    venv = release / 'venv'
    run(UV, '--no-config', 'venv', '--python', PYTHON, venv)
    run(UV, '--no-config', 'pip', 'install', '--python', venv / 'bin/python',
        '--no-build', '--require-hashes', '--link-mode', 'hardlink', '-r', dependency_file)
    common.bind_core_metadata(metadata(project), code, venv)
    (code / 'venv').symlink_to(venv)
    common.make_readable_program_tree(release)
    manifest = common.source_manifest(code)
    (release / 'code-manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2))
    receipt = {'schema_version': 1, 'state': 'staged', 'release_id': release_id,
               'version': project['version'], 'upstream_commit': '29112bef099274229cadff79cdff7bf7b99c4b77',
               'archive_sha256': checksum, 'code_sha256': common.manifest_digest(manifest),
               'dependencies_sha256': common.digest(dependency_file)}
    (release / 'runtime.json').write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--overlay', type=Path)
    parser.add_argument('--patch', action='append', type=Path, default=[])
    parser.add_argument('--release-id', required=True)
    parser.add_argument('--dependencies', type=Path)
    args = parser.parse_args()
    prepare(args.archive, args.sha256, args.overlay, args.release_id, dependencies=args.dependencies, patches=args.patch)
