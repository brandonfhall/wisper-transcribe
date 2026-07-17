"""Tests for BotManager — Phase 3 bot core."""
from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import pytest

from wisper_transcribe.campaign_manager import add_member, bind_discord_id, create_campaign
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
    """PCM frames from user U1 produce .wav segment files in per-user/U1/."""
    frames = [("U1", make_pcm_frame())] * 5
    factory = scripted_source(frames)

    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory)
    bm.start()
    rec = await bm.start_session(None, "VC1", "G1")

    # Wait for the session task to exhaust the scripted source
    await asyncio.wait_for(bm._task, timeout=5)

    per_user_dir = tmp_path / "recordings" / rec.id / "per-user" / "U1"
    assert per_user_dir.exists(), "per-user/U1/ directory should be created"
    wav_files = sorted(per_user_dir.glob("*.wav"))
    assert len(wav_files) >= 1, f"expected .wav files, got: {wav_files}"
    assert all(f.stat().st_size > 0 for f in wav_files)
    with wave.open(str(wav_files[0]), "rb") as wf:
        assert wf.getframerate() == 16000
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2

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


# ---------------------------------------------------------------------------
# 8. Phase 4 — Discord ID auto-tagging
# ---------------------------------------------------------------------------

async def test_known_discord_id_tagged_automatically_in_manifest(tmp_path):
    """When a speaker's Discord ID is bound in the campaign roster, their first
    audio frame tags recording.discord_speakers with their profile key."""
    create_campaign("dnd-mondays", data_dir=tmp_path)
    add_member("dnd-mondays", "alice", data_dir=tmp_path)
    bind_discord_id("dnd-mondays", "alice", "123456789012345678", data_dir=tmp_path)

    frames = [("123456789012345678", make_pcm_frame())] * 3
    factory = scripted_source(frames)

    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory)
    bm.start()
    rec = await bm.start_session("dnd-mondays", "VC1", "G1")
    await asyncio.wait_for(bm._task, timeout=5)

    loaded = load_recordings(tmp_path)[rec.id]
    assert loaded.discord_speakers.get("123456789012345678") == "alice"


async def test_unknown_discord_id_not_tagged(tmp_path):
    """A Discord ID with no roster binding gets an empty string in discord_speakers."""
    create_campaign("dnd-mondays", data_dir=tmp_path)
    add_member("dnd-mondays", "alice", data_dir=tmp_path)
    # alice has no discord_user_id bound

    frames = [("999999999999999999", make_pcm_frame())] * 3
    factory = scripted_source(frames)

    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory)
    bm.start()
    rec = await bm.start_session("dnd-mondays", "VC1", "G1")
    await asyncio.wait_for(bm._task, timeout=5)

    loaded = load_recordings(tmp_path)[rec.id]
    assert loaded.discord_speakers.get("999999999999999999") == ""


async def test_unknown_speaker_added_to_unbound_list(tmp_path):
    """A Discord ID with no roster binding is appended to recording.unbound_speakers."""
    create_campaign("dnd-mondays", data_dir=tmp_path)
    add_member("dnd-mondays", "alice", data_dir=tmp_path)
    # alice has no discord_user_id bound

    frames = [("999999999999999999", make_pcm_frame())] * 3
    factory = scripted_source(frames)

    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory)
    bm.start()
    rec = await bm.start_session("dnd-mondays", "VC1", "G1")
    await asyncio.wait_for(bm._task, timeout=5)

    loaded = load_recordings(tmp_path)[rec.id]
    assert "999999999999999999" in loaded.unbound_speakers


async def test_known_speaker_not_added_to_unbound_list(tmp_path):
    """A Discord ID bound in the campaign roster is NOT added to unbound_speakers."""
    create_campaign("dnd-mondays", data_dir=tmp_path)
    add_member("dnd-mondays", "alice", data_dir=tmp_path)
    bind_discord_id("dnd-mondays", "alice", "123456789012345678", data_dir=tmp_path)

    frames = [("123456789012345678", make_pcm_frame())] * 3
    factory = scripted_source(frames)

    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory)
    bm.start()
    rec = await bm.start_session("dnd-mondays", "VC1", "G1")
    await asyncio.wait_for(bm._task, timeout=5)

    loaded = load_recordings(tmp_path)[rec.id]
    assert "123456789012345678" not in loaded.unbound_speakers


# ---------------------------------------------------------------------------
# 9. Multi-user simultaneous scenarios
# ---------------------------------------------------------------------------

async def test_multiple_users_speak_simultaneously(tmp_path):
    """Interleaved frames from 3 users all produce per-user .wav files."""
    frames = []
    for _ in range(10):
        frames.append(("U1", make_pcm_frame()))
        frames.append(("U2", make_pcm_frame()))
        frames.append(("U3", make_pcm_frame()))

    factory = scripted_source(frames)
    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory)
    bm.start()
    rec = await bm.start_session(None, "VC1", "G1")
    await asyncio.wait_for(bm._task, timeout=5)

    rec_dir = tmp_path / "recordings" / rec.id / "per-user"
    for uid in ("U1", "U2", "U3"):
        user_dir = rec_dir / uid
        assert user_dir.exists(), f"per-user/{uid}/ should exist"
        wav_files = sorted(user_dir.glob("*.wav"))
        assert len(wav_files) >= 1, f"{uid} should have .wav segments"


async def test_multiple_unknown_speakers_all_in_unbound(tmp_path):
    """Three unbound Discord IDs should all appear in unbound_speakers, no duplicates."""
    frames = [
        ("111111111111111111", make_pcm_frame()),
        ("222222222222222222", make_pcm_frame()),
        ("333333333333333333", make_pcm_frame()),
        ("111111111111111111", make_pcm_frame()),
        ("222222222222222222", make_pcm_frame()),
    ]

    factory = scripted_source(frames)
    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory)
    bm.start()
    rec = await bm.start_session(None, "VC1", "G1")
    await asyncio.wait_for(bm._task, timeout=5)

    loaded = load_recordings(tmp_path)[rec.id]
    assert len(loaded.unbound_speakers) == 3
    assert "111111111111111111" in loaded.unbound_speakers
    assert "222222222222222222" in loaded.unbound_speakers
    assert "333333333333333333" in loaded.unbound_speakers


async def test_simultaneous_known_and_unknown_speakers(tmp_path):
    """Known speakers tag immediately; unknown speakers land in unbound list."""
    create_campaign("dnd-mondays", data_dir=tmp_path)
    add_member("dnd-mondays", "alice", data_dir=tmp_path)
    add_member("dnd-mondays", "bob", data_dir=tmp_path)
    bind_discord_id("dnd-mondays", "alice", "111111111111111111", data_dir=tmp_path)
    bind_discord_id("dnd-mondays", "bob", "222222222222222222", data_dir=tmp_path)

    frames = [
        ("111111111111111111", make_pcm_frame()),  # known: alice
        ("999999999999999999", make_pcm_frame()),  # unknown
        ("222222222222222222", make_pcm_frame()),  # known: bob
        ("888888888888888888", make_pcm_frame()),  # unknown
        ("111111111111111111", make_pcm_frame()),  # known: alice
    ]

    factory = scripted_source(frames)
    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory)
    bm.start()
    rec = await bm.start_session("dnd-mondays", "VC1", "G1")
    await asyncio.wait_for(bm._task, timeout=5)

    loaded = load_recordings(tmp_path)[rec.id]
    assert loaded.discord_speakers.get("111111111111111111") == "alice"
    assert loaded.discord_speakers.get("222222222222222222") == "bob"
    assert "999999999999999999" in loaded.unbound_speakers
    assert "888888888888888888" in loaded.unbound_speakers
    assert "111111111111111111" not in loaded.unbound_speakers
    assert "222222222222222222" not in loaded.unbound_speakers


# ---------------------------------------------------------------------------
# 10. R12 — __mixed__ track handling + end-to-end WAV decode
# ---------------------------------------------------------------------------

async def test_mixed_track_frames_do_not_create_per_user_state(tmp_path):
    """`__mixed__` (JDA's pre-mixed track) must never be treated as a
    per-user speaker: no per-user/__mixed__/ dir, no discord_speakers entry,
    no unbound_speakers entry."""
    frames = [("__mixed__", make_pcm_frame())] * 5
    factory = scripted_source(frames)

    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory)
    bm.start()
    rec = await bm.start_session(None, "VC1", "G1")
    await asyncio.wait_for(bm._task, timeout=5)

    per_user_dir = tmp_path / "recordings" / rec.id / "per-user"
    assert not (per_user_dir / "__mixed__").exists()

    loaded = load_recordings(tmp_path)[rec.id]
    assert "__mixed__" not in loaded.discord_speakers
    assert "__mixed__" not in loaded.unbound_speakers


async def test_combined_track_duration_matches_wall_clock_not_speaker_count(tmp_path):
    """R12 regression: the old RealtimePCMMixer advanced the combined track
    once per *any* incoming frame, so N concurrent speakers made the
    combined track N x 20ms per real 20ms. Now `__mixed__` (JDA's already-
    mixed track) is written 1:1 with real time, independent of how many
    per-user frames arrive alongside it."""
    frames = []
    n_ticks = 50
    for _ in range(n_ticks):
        frames.append(("__mixed__", make_pcm_frame(value=500)))
        frames.append(("U1", make_pcm_frame(value=500)))
        frames.append(("U2", make_pcm_frame(value=500)))
        frames.append(("U3", make_pcm_frame(value=500)))

    factory = scripted_source(frames)
    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory)
    bm.start()
    rec = await bm.start_session(None, "VC1", "G1")
    await asyncio.wait_for(bm._task, timeout=5)

    combined_dir = tmp_path / "recordings" / rec.id / "combined"
    segments = sorted(combined_dir.glob("*.wav"))
    assert len(segments) == 1

    with wave.open(str(segments[0]), "rb") as wf:
        assert wf.getframerate() == 16000
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        nframes = wf.getnframes()
        pcm = wf.readframes(nframes)

    expected_frames = n_ticks * 320  # 320 samples per 20 ms @ 16 kHz
    assert nframes == expected_frames, (
        f"expected {expected_frames} samples ({n_ticks * 0.02}s of real time), "
        f"got {nframes} ({nframes / 16000}s) — combined track duration must "
        "track wall-clock time, not the number of concurrent speakers"
    )
    # Content survives downsampling: constant non-zero input -> non-silent output.
    assert any(b != 0 for b in pcm)


async def test_finalise_concatenates_combined_segments_and_sets_combined_path(
    tmp_path, monkeypatch
):
    """R2 regression: `Recording.combined_path` used to be assigned exactly
    once, to None, and never populated — the transcribe hand-off always
    redirected with ?error=no_audio. This drives BotManager through
    multiple combined-track segment rotations and verifies `_finalise`
    merges them into recordings/<id>/combined.wav with `combined_path`
    pointing at the merged file, whose duration equals the sum of the
    individual segments'."""
    import functools

    import wisper_transcribe.web.discord_bot as discord_bot_module
    from wisper_transcribe.web.audio_writer import SegmentedWavWriter

    # Force short (0.1s = 5 frames) segments so a modest frame count rotates
    # across multiple combined-track segment files.
    monkeypatch.setattr(
        discord_bot_module,
        "SegmentedWavWriter",
        functools.partial(SegmentedWavWriter, segment_duration_s=0.1),
    )

    frames = [("__mixed__", make_pcm_frame())] * 13
    factory = scripted_source(frames)
    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory)
    bm.start()
    rec = await bm.start_session(None, "VC1", "G1")
    await asyncio.wait_for(bm._task, timeout=5)

    combined_dir = tmp_path / "recordings" / rec.id / "combined"
    segments = sorted(combined_dir.glob("*.wav"))
    assert len(segments) >= 2, "expected rotation across multiple segments"

    loaded = load_recordings(tmp_path)[rec.id]
    assert loaded.combined_path is not None
    assert loaded.combined_path.exists()
    assert loaded.combined_path == tmp_path / "recordings" / rec.id / "combined.wav"

    seg_total = 0
    for seg in segments:
        with wave.open(str(seg), "rb") as wf:
            seg_total += wf.getnframes()

    with wave.open(str(loaded.combined_path), "rb") as wf:
        assert wf.getframerate() == 16000
        assert wf.getnchannels() == 1
        merged_frames = wf.getnframes()

    assert merged_frames == seg_total
    assert merged_frames == 13 * 320


async def test_finalise_leaves_combined_path_none_when_no_audio(tmp_path):
    """A session with no frames at all (e.g. bot joined and immediately
    stopped) must leave combined_path unset so ?error=no_audio still works."""
    factory = scripted_source([])
    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory)
    bm.start()
    rec = await bm.start_session(None, "VC1", "G1")
    await asyncio.wait_for(bm._task, timeout=5)

    loaded = load_recordings(tmp_path)[rec.id]
    assert loaded.combined_path is None
