#!/usr/bin/env python3
"""Forge profile hook (post_tool_call): clean generated office metadata, incl. PDFs.

Env-driven (set by the provisioner in ~/.hermes/.env):
  HERMES_DOC_WORKSPACE — owner workspace, e.g. /home/<owner>/workspace
  HERMES_DOC_AUTHOR    — author to stamp instead of python-docx/pypdf defaults
Runs /usr/local/bin/hermes-clean-docmeta for .docx/.xlsx/.pptx, then fixes
fresh PDFs (<=15 min) whose /Author still names a generator library.
Prints nothing to stdout (hook protocol).
"""

import os
import runpy
import stat
import sys
import tempfile
import time
from pathlib import Path

from pypdf import PdfReader, PdfWriter

GENERATED_AUTHORS = {"python-docx", "python-pptx", "openpyxl", "pypdf", "reportlab"}


def clean_pdf(path: Path, author: str) -> bool:
    file_stat = path.lstat()
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
        return False
    if file_stat.st_size > 20 * 1024 * 1024:
        return False
    with path.open("rb") as source:
        reader = PdfReader(source)
        if reader.is_encrypted:
            return False
        metadata = reader.metadata or {}
        if str(metadata.get("/Author", "")).lower() not in GENERATED_AUTHORS:
            return False
        # Never rewrite a signed, permission-restricted or XMP-bearing original.
        if "/Perms" in reader.trailer["/Root"] or reader.xmp_metadata is not None:
            return False
        if any(field.get("/FT") == "/Sig" for field in (reader.get_fields() or {}).values()):
            return False
        writer = PdfWriter(clone_from=reader)
        writer.pdf_header = reader.pdf_header
        writer.add_metadata({"/Author": author})
        fd, temp_name = tempfile.mkstemp(prefix=".docmeta-", suffix=".pdf", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as target:
                writer.write(target)
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
    workspace = Path(os.environ.get("HERMES_DOC_WORKSPACE", ""))
    author = os.environ.get("HERMES_DOC_AUTHOR", "").strip()
    home = Path.home()
    if not workspace.is_absolute() or workspace != home / "workspace" or not author:
        raise RuntimeError("Document workspace/author environment is not configured")
    runpy.run_path("/usr/local/bin/hermes-clean-docmeta", run_name="__main__")
    cutoff = time.time() - 15 * 60
    for root, directories, files in os.walk(workspace, followlinks=False):
        directories[:] = [name for name in directories if not Path(root, name).is_symlink()]
        for name in files:
            path = Path(root, name)
            if path.suffix.lower() != ".pdf" or path.is_symlink():
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    clean_pdf(path, author)
            except Exception as exc:
                print(f"Document metadata check failed: {type(exc).__name__}", file=sys.stderr)


if __name__ == "__main__":
    main()
