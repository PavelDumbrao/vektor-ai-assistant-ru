import shutil
import subprocess
from pathlib import Path

import pytest

from tools.long_media_transcription import (
    format_timestamp,
    prepare_long_media,
    probe_media,
    should_prepare_long_media,
)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg/ffprobe required")
def test_prepare_long_video_extracts_bounded_audio_chunks(tmp_path):
    source = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=160x90:r=1:d=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=3",
            "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            str(source),
        ],
        check=True,
    )
    probe = probe_media(str(source))
    assert probe.has_video is True
    assert probe.has_audio is True

    needed, _ = should_prepare_long_media(
        str(source), provider_is_local=False, cloud_max_bytes=25 * 1024 * 1024
    )
    assert needed is True

    prepared = prepare_long_media(str(source), chunk_seconds=60, probe=probe)
    try:
        assert len(prepared.chunks) == 1
        chunk = Path(prepared.chunks[0].path)
        assert chunk.exists()
        assert chunk.suffix == ".wav"
        assert chunk.stat().st_size > 0
        chunk_probe = probe_media(str(chunk))
        assert chunk_probe.has_audio is True
        assert chunk_probe.has_video is False
    finally:
        prepared.cleanup()


def test_remote_oversized_audio_requires_chunking(tmp_path):
    source = tmp_path / "large.wav"
    with source.open("wb") as fh:
        fh.truncate(26 * 1024 * 1024)
    needed, probe = should_prepare_long_media(
        str(source), provider_is_local=False, cloud_max_bytes=25 * 1024 * 1024
    )
    assert needed is True
    assert probe.size_bytes == 26 * 1024 * 1024


def test_local_oversized_audio_keeps_existing_direct_path(tmp_path):
    source = tmp_path / "large.wav"
    with source.open("wb") as fh:
        fh.truncate(26 * 1024 * 1024)
    needed, _ = should_prepare_long_media(
        str(source), provider_is_local=True, cloud_max_bytes=25 * 1024 * 1024
    )
    assert needed is False


def test_timestamp_format():
    assert format_timestamp(0) == "00:00:00"
    assert format_timestamp(480) == "00:08:00"
    assert format_timestamp(3723) == "01:02:03"
