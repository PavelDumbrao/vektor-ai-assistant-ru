#!/usr/bin/env python3
"""Profile-local Edge synthesis with bounded input and wall-clock timeout."""

import asyncio
from pathlib import Path
import os
import sys
import tempfile

import edge_tts


async def synthesize(input_path: Path, output_path: Path) -> None:
    text = input_path.read_text(encoding='utf-8').strip()
    if not text or len(text) > 1000:
        raise ValueError('Expected 1-1000 characters per voice segment')
    home = Path(os.environ.get('HERMES_HOME', ''))
    workspace = Path(os.environ.get('HERMES_DOC_WORKSPACE', ''))
    if not home.is_absolute() or not workspace.is_absolute():
        raise ValueError('Absolute HERMES_HOME and HERMES_DOC_WORKSPACE are required')
    allowed = ((home / 'cache/audio').resolve(), workspace.resolve())
    output_path = output_path.resolve()
    if not any(output_path.is_relative_to(root) for root in allowed):
        raise ValueError('Unexpected audio output location')
    fd, partial_name = tempfile.mkstemp(prefix='.voice-', suffix='.mp3', dir=output_path.parent)
    os.close(fd)
    partial = Path(partial_name)
    try:
        speech = edge_tts.Communicate(
            text, voice='ru-RU-DmitryNeural', rate='+15%',
            connect_timeout=10, receive_timeout=15,
        )
        await asyncio.wait_for(speech.save(str(partial)), timeout=35)
        if partial.stat().st_size == 0:
            raise RuntimeError('Voice service returned empty audio')
        os.replace(partial, output_path)
    finally:
        partial.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        if len(sys.argv) != 3:
            raise ValueError('Expected input and output paths')
        asyncio.run(synthesize(Path(sys.argv[1]), Path(sys.argv[2])))
    except Exception as error:
        # No input text, request URLs or credentials in diagnostics.
        print(f'Voice synthesis failed: {type(error).__name__}', file=sys.stderr)
        raise SystemExit(1)
