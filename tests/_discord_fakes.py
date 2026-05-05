"""Fake Discord/JDA infrastructure for BotManager tests.

Provides scripted audio sources that simulate the JDA sidecar's wire protocol
without any real subprocess, socket, or network connection.

Wire protocol recap (from discord_bot.py):
  Each frame: (user_id: str, pcm: bytes)
  Control frame: user_id == CTRL_USER_ID, pcm == struct.pack("<I", close_code)
"""
from __future__ import annotations

import struct
from typing import AsyncIterator

from wisper_transcribe.web.discord_bot import CTRL_USER_ID

# ---------------------------------------------------------------------------
# Frame constructors
# ---------------------------------------------------------------------------

def make_pcm_frame(n_samples: int = 960, value: int = 1000) -> bytes:
    """One fake 20 ms 48 kHz stereo 16-bit PCM frame (3840 bytes)."""
    return struct.pack(f"<{n_samples * 2}h", *(value,) * (n_samples * 2))


def make_disconnect_frame(close_code: int) -> tuple[str, bytes]:
    """A control frame signalling a voice disconnect with the given close code."""
    return (CTRL_USER_ID, struct.pack("<I", close_code))


# ---------------------------------------------------------------------------
# Scripted audio source factory
# ---------------------------------------------------------------------------

def scripted_source(frames: list[tuple[str, bytes]]):
    """Return an audio_source_factory that yields the given frames once."""
    async def _source(recording_id, voice_channel_id, guild_id, token):
        for user_id, pcm in frames:
            yield user_id, pcm

    return lambda *_a, **_kw: _source(None, None, None, None)


def multi_attempt_source(frame_sequences: list[list[tuple[str, bytes]]]):
    """Return an audio_source_factory that yields different frame sets per call.

    Each call to the factory yields the next sequence.  When all sequences are
    exhausted, subsequent calls yield nothing.  Used for testing auto-rejoin:

        factory = multi_attempt_source([
            [("U1", pcm), make_disconnect_frame(4015)],  # attempt 0
            [("U1", pcm)],                                # attempt 1 — clean exit
        ])
    """
    call_index = [0]

    def _factory(*_a, **_kw):
        idx = call_index[0]
        call_index[0] += 1
        frames = frame_sequences[idx] if idx < len(frame_sequences) else []

        async def _gen():
            for user_id, pcm in frames:
                yield user_id, pcm

        return _gen()

    return _factory


def infinite_disconnect_source(close_code: int):
    """Factory that always yields one disconnect frame — exhausts retries."""
    def _factory(*_a, **_kw):
        async def _gen():
            yield make_disconnect_frame(close_code)

        return _gen()

    return _factory


def blocking_source():
    """Factory whose source never yields — simulates waiting for audio.

    Useful for stop_session tests: start the session, then stop it while
    the source is blocked, and confirm the recording finalises correctly.
    """
    def _factory(*_a, **_kw):
        async def _gen():
            import asyncio
            await asyncio.sleep(999)
            # unreachable, but makes this an async generator:
            yield  # pragma: no cover

        return _gen()

    return _factory
