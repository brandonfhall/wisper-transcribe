"""Mock ffmpeg/ffprobe for combined-track FLAC encoding in tests.

No real ffmpeg runs. ``install(monkeypatch)`` replaces the three audio_utils
entry points finalise and recovery use: ``encode_flac`` writes a placeholder
FLAC and remembers the source WAV's frame count, ``probe_format`` reports
16 kHz mono, and ``probe_frames`` returns that remembered count, so
verification passes exactly as it would for a real lossless encode. A test
that needs a failure overrides these with its own patch (inner patches win).
"""
from __future__ import annotations

import wave
from pathlib import Path


def install(monkeypatch) -> dict:
    counts: dict[str, int] = {}

    def _encode(src, dst):
        with wave.open(str(src), "rb") as wf:
            counts[str(dst)] = wf.getnframes()
        Path(dst).write_bytes(b"fLaC")

    monkeypatch.setattr("wisper_transcribe.audio_utils.encode_flac", _encode)
    monkeypatch.setattr("wisper_transcribe.audio_utils.probe_format", lambda _p: (16000, 1))
    monkeypatch.setattr("wisper_transcribe.audio_utils.probe_frames",
                        lambda p: counts.get(str(p), 0))
    return counts


def frames(path) -> int:
    """Frame count of a combined track for assertions: WAV header or mocked ffprobe."""
    from wisper_transcribe.audio_utils import probe_frames

    path = Path(path)
    if path.suffix.lower() == ".flac":
        return probe_frames(path)
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes()
