#!/usr/bin/env python3
"""Clean generated office metadata, including PDFs converted in the same tool call."""

import os
from pathlib import Path
import runpy
import stat
import sys
import tempfile
import time

from pypdf import PdfReader, PdfWriter


WORKSPACE = Path(os.environ.get('HERMES_DOC_WORKSPACE', ''))
AUTHOR = os.environ.get('HERMES_DOC_AUTHOR', '').strip()
GENERATED_AUTHORS = {'python-docx', 'python-pptx', 'openpyxl'}


def clean_pdf(path: Path) -> bool:
    if not AUTHOR:
        raise RuntimeError('HERMES_DOC_AUTHOR is required')
    file_stat = path.lstat()
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
        return False
    if file_stat.st_size > 20 * 1024 * 1024:
        return False
    with path.open('rb') as source:
        reader = PdfReader(source)
        if reader.is_encrypted:
            return False
        metadata = reader.metadata or {}
        if str(metadata.get('/Author', '')).lower() not in GENERATED_AUTHORS:
            return False
        # Never rewrite a signed, permission-restricted or XMP-bearing original.
        if '/Perms' in reader.trailer['/Root'] or reader.xmp_metadata is not None:
            return False
        if any(field.get('/FT') == '/Sig' for field in (reader.get_fields() or {}).values()):
            return False
        writer = PdfWriter(clone_from=reader)
        writer.pdf_header = reader.pdf_header
        writer.add_metadata({'/Author': AUTHOR})
        fd, temp_name = tempfile.mkstemp(prefix='.docmeta-', suffix='.pdf', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as target:
                writer.write(target)
            # A concurrently changed file must not be replaced by an older copy.
            current = path.lstat()
            if (current.st_ino, current.st_size, current.st_mtime_ns) != (
                file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns
            ):
                return False
            os.chmod(temp_name, stat.S_IMODE(file_stat.st_mode))
            os.utime(temp_name, ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns))
            os.replace(temp_name, path)
            return True
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def main() -> None:
    if not WORKSPACE.is_absolute() or not WORKSPACE.is_dir() or not AUTHOR:
        raise RuntimeError('Absolute HERMES_DOC_WORKSPACE and HERMES_DOC_AUTHOR are required')
    runpy.run_path('/usr/local/bin/hermes-clean-docmeta', run_name='__main__')
    cutoff = time.time() - 15 * 60
    for root, directories, files in os.walk(WORKSPACE, followlinks=False):
        directories[:] = [name for name in directories if not Path(root, name).is_symlink()]
        for name in files:
            path = Path(root, name)
            if path.suffix.lower() != '.pdf' or path.is_symlink():
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    clean_pdf(path)
            except Exception as exc:
                print(f'Document metadata check failed: {type(exc).__name__}', file=sys.stderr)


if __name__ == '__main__':
    main()
