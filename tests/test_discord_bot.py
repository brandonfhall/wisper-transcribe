"""Tests for BotManager — Phase 3 bot core."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from wisper_transcribe.recording_manager import load_recordings
from wisper_transcribe.web.discord_bot import BotManager
from tests._discord_fakes import (
    blocking_source,
    infinite_disconnect_source,
    make_disconnect_frame,
    make_pcm_frame,
    multi_attempt_source,
    scripted_source,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# 1. Lifecycle
# ---------------------------------------------------------------------------

async def test_bot_manager_start_stop_lifecycle(tmp_path):
    """BotManager starts and stops cleanly with no active session."""
    bm = BotManager(data_dir=tmp_path)
    bm.start()
    await bm.stop()
    assert bm.active_recording is None


# ---------------------------------------------------------------------------
# 2. Session creation
# ---------------------------------------------------------------------------

async def test_start_session_creates_recording_in_manager(tmp_path):
    """start_session() persists a Recording with status 'recording'."""
    bm = BotManager(data_dir=tmp_path, audio_source_factory=scripted_source([]))
    bm.start()

    rec = await bm.start_session(
        campaign_slug="dnd-mondays",
        voice_channel_id="VC1",
        guild_id="G1",
    )

    assert rec.id is not None
    assert len(rec.id) == 36  # uuid4

    recordings = load_recordings(tmp_path)
    assert rec.id in recordings
    assert recordings[rec.id].status == "recording"

    await bm.stop()


# ---------------------------------------------------------------------------
# 3. Audio routing — per-user files written
# ---------------------------------------------------------------------------

async def test_user_speaks_writes_packets_to_per_user_dir(tmp_path):
    """PCM frames from user U1 produce .opus segment files in per-user/U1/."""
    frames = [("U1", make_pcm_frame())] * 5
    factory = scripted_source(frames)

    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory)
    bm.start()
    rec = await bm.start_session(None, "VC1", "G1")

    # Wait for the session task to exhaust the scripted source
    await asyncio.wait_for(bm._task, timeout=5)

    per_user_dir = tmp_path / "recordings" / rec.id / "per-user" / "U1"
    assert per_user_dir.exists(), "per-user/U1/ directory should be created"
    opus_files = sorted(per_user_dir.glob("*.opus"))
    assert len(opus_files) >= 1, f"expected .opus files, got: {opus_files}"
    assert all(f.stat().st_size > 0 for f in opus_files)

    await bm.stop()


# ---------------------------------------------------------------------------
# 4. Auto-rejoin — transient close code retries
# ---------------------------------------------------------------------------

async def test_auto_rejoin_on_transient_close_code_4015(tmp_path):
    """A 4015 disconnect is logged and the session retries."""
    factory = multi_attempt_source([
        [("U1", make_pcm_frame()), make_disconnect_frame(4015)],  # attempt 0 → disconnect
        [("U1", make_pcm_frame())],                                # attempt 1 → clean exit
    ])

    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory, _backoff=[0] * 5)
    bm.start()
    rec = await bm.start_session(None, "VC1", "G1")
    await asyncio.wait_for(bm._task, timeout=5)

    loaded = load_recordings(tmp_path)[rec.id]
    assert len(loaded.rejoin_log) == 1
    assert loaded.rejoin_log[0].close_code == 4015
    assert loaded.rejoin_log[0].attempt_number == 1
    assert loaded.status == "completed"


# ---------------------------------------------------------------------------
# 5. Auto-rejoin — exhausted retries → degraded
# ---------------------------------------------------------------------------

async def test_auto_rejoin_exhausted_sets_degraded_status(tmp_path):
    """After DEFAULT_BACKOFF retries all fail, status becomes 'degraded'."""
    factory = infinite_disconnect_source(close_code=4015)

    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory, _backoff=[0] * 5)
    bm.start()
    rec = await bm.start_session(None, "VC1", "G1")
    await asyncio.wait_for(bm._task, timeout=5)

    loaded = load_recordings(tmp_path)[rec.id]
    assert loaded.status == "degraded"
    assert len(loaded.rejoin_log) == len(bm._backoff)


# ---------------------------------------------------------------------------
# 6. Permanent close code — aborts without retry
# ---------------------------------------------------------------------------

async def test_permanent_close_code_4014_aborts_without_retry(tmp_path):
    """Close code 4014 (kicked) is permanent — no retry, status = failed."""
    factory = multi_attempt_source([
        [make_disconnect_frame(4014)],  # single permanent disconnect
    ])

    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory, _backoff=[0] * 5)
    bm.start()
    rec = await bm.start_session(None, "VC1", "G1")
    await asyncio.wait_for(bm._task, timeout=5)

    loaded = load_recordings(tmp_path)[rec.id]
    assert loaded.status == "failed"
    assert len(loaded.rejoin_log) == 0  # no retry logged


# ---------------------------------------------------------------------------
# 7. stop_session sets completed
# ---------------------------------------------------------------------------

async def test_stop_session_sets_completed_status(tmp_path):
    """stop_session() cleanly finalises the recording as 'completed'."""
    bm = BotManager(data_dir=tmp_path, audio_source_factory=blocking_source())
    bm.start()
    rec = await bm.start_session(None, "VC1", "G1")

    # Give the task a moment to start
    await asyncio.sleep(0)

    await bm.stop_session()

    loaded = load_recordings(tmp_path)[rec.id]
    assert loaded.status == "completed"
    assert loaded.ended_at is not None
