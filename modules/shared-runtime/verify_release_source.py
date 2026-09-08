#!/usr/bin/env python3
"""Rebuild a pinned source tree without credentials, installations or service changes."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

from prepare_runtime import digest, manifest_digest, source_manifest


def verify(archive: Path, release_file: Path, output: Path, variant: str) -> dict:
    manifest = json.loads(release_file.read_text())
    spec = manifest['variants'][variant]
    output = output.resolve()
    if output.exists():
        raise ValueError('Output must be a new directory')
    opener = gzip.open if archive.suffix == '.gz' else open
    with opener(archive, 'rb') as stream:
        archive_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    if archive_hash != manifest['archive_sha256']:
        raise ValueError('Source archive checksum mismatch')
    for name in spec['patches']:
        if digest(release_file.parent / name) != manifest['patch_sha256'][name]:
            raise ValueError('Patch checksum mismatch: '+name)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as probe_dir:
        probe = Path(probe_dir) / 'Case'
        probe.touch()
        if probe.with_name('case').exists():
            raise ValueError('Linux/case-sensitive filesystem required by upstream filenames')
    output.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(output, filter='data')
    # The temporary source must not inherit an enclosing repository's path prefix.
    subprocess.run(['git', 'init', '-q', str(output)], check=True)
    for name in spec['patches']:
        patch = (release_file.parent / name).resolve()
        subprocess.run(['git', 'apply', '--check', str(patch)], cwd=output, check=True)
        subprocess.run(['git', 'apply', str(patch)], cwd=output, check=True)
    (output / '.hermes_build_sha').write_text(manifest['upstream_commit']+'\n')
    files = source_manifest(output)
    actual = manifest_digest(files)
    if actual != spec['code_sha256']:
        raise ValueError('Reconstructed source differs from verified VPS source: '+actual)
    return {'variant':variant, 'source_files':len(files), 'code_sha256':actual, 'match':True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--release', type=Path, default=Path(__file__).parent/'releases/v0.21.0/vektor3.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--variant', choices=['modern','legacy'], required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.archive.resolve(),args.release.resolve(),args.output,args.variant)))
