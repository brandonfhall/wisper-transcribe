"""Tests for web/live_transcribe.py — Phase 2 (live local transcription).

WhisperModel is mocked throughout (CLAUDE.md: no GPU/network/real audio in
tests). faster_whisper's Silero VAD ONNX model ships bundled with the
package (no network, cheap CPU inference) so `find_commit_boundary`'s
force-cut/min-duration paths and the pure-silence case use the real VAD
unmocked (unambiguous on all-zero input); the "speech then trailing
silence" cut-point arithmetic is tested with `get_speech_timestamps`
mocked so the test doesn't depend on synthetic audio actually crossing a
real speech-detection confidence threshold.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from wisper_transcribe.web.live_transcribe import (
    NOISE_FLOOR_RMS,
    RATE,
    LiveLine,
    LiveRingBuffer,
    attribute_speaker,
    commit_and_transcribe,
    find_commit_boundary,
    run_live_loop,
    transcribe_array,
)


def _tone_i16(seconds: float, value: int = 1000) -> bytes:
    n = int(seconds * RATE)
    return np.full(n, value, dtype="<i2").tobytes()


def _silence_i16(seconds: float) -> bytes:
    return _tone_i16(seconds, value=0)


# ---------------------------------------------------------------------------
# LiveRingBuffer
# ---------------------------------------------------------------------------

def test_ring_buffer_push_and_snapshot():
    buf = LiveRingBuffer()
    buf.push(_tone_i16(0.02, 1), _tone_i16(0.02, 2), _tone_i16(0.02, 3))
    mic, system, mixed, elapsed = buf.snapshot()
    assert len(mixed) == 320 * 2  # 320 samples * 2 bytes
    assert elapsed == pytest.approx(0.02)


def test_ring_buffer_push_empty_mixed_is_noop():
    buf = LiveRingBuffer()
    buf.push(b"", b"", b"")
    assert len(buf) == 0


def test_ring_buffer_drop_prefix_keeps_tracks_aligned():
    buf = LiveRingBuffer()
    buf.push(_tone_i16(0.02, 1), _tone_i16(0.02, 2), _tone_i16(0.02, 3))
    buf.push(_tone_i16(0.02, 4), _tone_i16(0.02, 5), _tone_i16(0.02, 6))
    buf.drop_prefix(320 * 2)  # drop the first tick's worth
    mic, system, mixed, _ = buf.snapshot()
    assert len(mic) == len(system) == len(mixed) == 320 * 2
    assert np.frombuffer(mic, dtype="<i2")[0] == 4
    assert np.frombuffer(system, dtype="<i2")[0] == 5
    assert np.frombuffer(mixed, dtype="<i2")[0] == 6


def test_ring_buffer_len():
    buf = LiveRingBuffer()
    assert len(buf) == 0
    buf.push(_tone_i16(0.02), _tone_i16(0.02), _tone_i16(0.02))
    assert len(buf) == 320 * 2


# ---------------------------------------------------------------------------
# find_commit_boundary
# ---------------------------------------------------------------------------

def test_find_commit_boundary_below_min_duration_returns_none():
    mixed = np.frombuffer(_silence_i16(0.1), dtype="<i2")
    assert find_commit_boundary(mixed) is None


def test_find_commit_boundary_pure_silence_not_ready():
    """Real (unmocked) VAD: all-zero input is unambiguously non-speech."""
    mixed = np.frombuffer(_silence_i16(2.0), dtype="<i2")
    assert find_commit_boundary(mixed) is None


def test_find_commit_boundary_force_cuts_at_15s():
    mixed = np.frombuffer(_silence_i16(15.0), dtype="<i2")
    cut = find_commit_boundary(mixed)
    assert cut == len(mixed)


def test_find_commit_boundary_cuts_after_trailing_silence_gap():
    """Only the last _VAD_WINDOW_S seconds of the buffer are scanned (see
    find_commit_boundary's docstring) -- the mocked VAD result is expressed
    in tail-relative coordinates, and the returned cut is absolute."""
    from wisper_transcribe.web.live_transcribe import _VAD_WINDOW_S

    n = int(2.0 * RATE)
    mixed = np.zeros(n, dtype="<i2")
    window_samples = int(_VAD_WINDOW_S * RATE)
    offset = n - window_samples
    tail_relative_end = int(1.0 * RATE)  # 0.6s of trailing silence within the tail
    fake_timestamps = [{"start": 0, "end": tail_relative_end}]
    with patch("faster_whisper.vad.get_speech_timestamps", return_value=fake_timestamps):
        cut = find_commit_boundary(mixed)
    assert cut == offset + tail_relative_end


def test_find_commit_boundary_only_scans_tail_window_not_whole_buffer():
    """The actual point of the windowing: a 10s buffer should hand VAD only
    ~_VAD_WINDOW_S seconds, not all 10 -- this is what keeps repeated
    polling from being O(n^2) across a session."""
    from wisper_transcribe.web.live_transcribe import _VAD_WINDOW_S

    n = int(10.0 * RATE)  # under FORCE_CUT_S=15s, so VAD does get called
    mixed = np.zeros(n, dtype="<i2")
    with patch("faster_whisper.vad.get_speech_timestamps", return_value=[]) as mock_vad:
        find_commit_boundary(mixed)

    scanned_audio = mock_vad.call_args.args[0]
    assert len(scanned_audio) == int(_VAD_WINDOW_S * RATE)
    assert len(scanned_audio) < n


def test_find_commit_boundary_waits_when_trailing_silence_too_short():
    n = int(1.2 * RATE)
    mixed = np.zeros(n, dtype="<i2")
    # speech ends 0.2s before buffer end -- below SILENCE_GAP_S (0.6s)
    fake_timestamps = [{"start": 0, "end": int(1.0 * RATE)}]
    with patch("faster_whisper.vad.get_speech_timestamps", return_value=fake_timestamps):
        cut = find_commit_boundary(mixed)
    assert cut is None


# ---------------------------------------------------------------------------
# attribute_speaker
# ---------------------------------------------------------------------------

def test_attribute_speaker_mic_dominant_is_you():
    mic = np.full(100, 5000, dtype="<i2")
    system = np.full(100, 100, dtype="<i2")
    assert attribute_speaker(mic, system) == "You"


def test_attribute_speaker_system_dominant_is_other():
    mic = np.full(100, 100, dtype="<i2")
    system = np.full(100, 5000, dtype="<i2")
    assert attribute_speaker(mic, system) == "Other"


def test_attribute_speaker_both_silent_returns_none():
    """Regression test: both tracks below the noise floor must NOT default
    to mic_label -- that used to mislabel background noise / hallucinated
    text as the mic owner's voice."""
    mic = np.zeros(100, dtype="<i2")
    system = np.zeros(100, dtype="<i2")
    assert attribute_speaker(mic, system) is None


def test_attribute_speaker_empty_spans_returns_none():
    assert attribute_speaker(np.array([], dtype="<i2"), np.array([], dtype="<i2")) is None


def test_attribute_speaker_both_below_noise_floor_returns_none():
    """Quiet room tone / mic self-noise on both tracks (nonzero, but below
    the floor) is dropped rather than attributed to either side."""
    mic = np.full(100, 50, dtype="<i2")
    system = np.full(100, 30, dtype="<i2")
    assert attribute_speaker(mic, system, noise_floor=150.0) is None


def test_attribute_speaker_quiet_but_above_floor_still_attributed():
    """A quiet but real voice (above the floor) is still attributed
    normally, not dropped -- the floor only catches noise below it."""
    mic = np.full(100, 200, dtype="<i2")
    system = np.full(100, 30, dtype="<i2")
    assert attribute_speaker(mic, system, noise_floor=150.0) == "You"


def test_attribute_speaker_custom_mic_label():
    """Phase 3 'this is me': mic-dominant lines use the given label instead
    of the generic 'You'."""
    mic = np.full(100, 5000, dtype="<i2")
    system = np.full(100, 100, dtype="<i2")
    assert attribute_speaker(mic, system, mic_label="Brandon") == "Brandon"
    assert attribute_speaker(system, mic, mic_label="Brandon") == "Other"


# ---------------------------------------------------------------------------
# transcribe_array / commit_and_transcribe (mocked WhisperModel)
# ---------------------------------------------------------------------------

def _install_fake_model(monkeypatch, segments):
    from wisper_transcribe import transcriber as _transcriber

    fake_model = MagicMock()
    fake_model.transcribe.return_value = (segments, MagicMock())
    monkeypatch.setattr(_transcriber, "_model", fake_model)
    monkeypatch.setattr(_transcriber, "_model_key", ("base", "cpu", "auto"))
    monkeypatch.setattr("wisper_transcribe.config.get_device", lambda: "cpu")
    return fake_model


def _fake_whisper_segment(start, end, text):
    seg = MagicMock()
    seg.start = start
    seg.end = end
    seg.text = text
    return seg


def test_transcribe_array_returns_transcription_segments(monkeypatch):
    _install_fake_model(monkeypatch, [_fake_whisper_segment(0.0, 1.0, "hello there")])
    audio = np.zeros(RATE, dtype=np.float32)
    result = transcribe_array(audio, model_size="base", device="cpu")
    assert len(result) == 1
    assert result[0].text == "hello there"


def test_transcribe_array_skips_blank_segments(monkeypatch):
    _install_fake_model(monkeypatch, [_fake_whisper_segment(0.0, 1.0, "   ")])
    audio = np.zeros(RATE, dtype=np.float32)
    result = transcribe_array(audio, model_size="base", device="cpu")
    assert result == []


def test_commit_and_transcribe_attributes_by_dominant_track(monkeypatch):
    _install_fake_model(monkeypatch, [_fake_whisper_segment(0.0, 0.5, "hi")])
    n = int(0.5 * RATE)
    mic_bytes = np.full(n, 5000, dtype="<i2").tobytes()
    system_bytes = np.full(n, 0, dtype="<i2").tobytes()
    mixed_bytes = np.full(n, 2500, dtype="<i2").tobytes()

    lines = commit_and_transcribe(
        mic_bytes, system_bytes, mixed_bytes, chunk_start_s=10.0,
        model_size="base", device="cpu",
    )
    assert len(lines) == 1
    assert lines[0].speaker == "You"
    assert lines[0].text == "hi"
    assert lines[0].start_s == pytest.approx(10.0)
    assert lines[0].end_s == pytest.approx(10.5)


def test_commit_and_transcribe_uses_custom_mic_label(monkeypatch):
    _install_fake_model(monkeypatch, [_fake_whisper_segment(0.0, 0.5, "hi")])
    n = int(0.5 * RATE)
    mic_bytes = np.full(n, 5000, dtype="<i2").tobytes()
    system_bytes = np.full(n, 0, dtype="<i2").tobytes()
    mixed_bytes = np.full(n, 2500, dtype="<i2").tobytes()

    lines = commit_and_transcribe(
        mic_bytes, system_bytes, mixed_bytes, chunk_start_s=0.0,
        model_size="base", device="cpu", mic_label="Brandon",
    )
    assert lines[0].speaker == "Brandon"


def test_commit_and_transcribe_empty_mixed_returns_no_lines(monkeypatch):
    _install_fake_model(monkeypatch, [])
    lines = commit_and_transcribe(b"", b"", b"", chunk_start_s=0.0)
    assert lines == []


def test_commit_and_transcribe_no_segments_returns_no_lines(monkeypatch):
    _install_fake_model(monkeypatch, [])
    n = int(0.5 * RATE)
    silence = np.zeros(n, dtype="<i2").tobytes()
    lines = commit_and_transcribe(silence, silence, silence, chunk_start_s=0.0, device="cpu")
    assert lines == []


def test_commit_and_transcribe_drops_hallucination_on_noise_floor(monkeypatch):
    """Whisper can still return text for a chunk that's really just room
    tone / mic self-noise (a VAD false positive, or the mixed track has
    enough energy to look like "something" even though neither individual
    track does) -- that hallucinated line must be dropped, not mislabeled
    under mic_label."""
    _install_fake_model(monkeypatch, [_fake_whisper_segment(0.0, 0.5, "thanks for watching")])
    n = int(0.5 * RATE)
    mic_bytes = np.full(n, 50, dtype="<i2").tobytes()      # below NOISE_FLOOR_RMS
    system_bytes = np.full(n, 30, dtype="<i2").tobytes()   # below NOISE_FLOOR_RMS
    mixed_bytes = np.full(n, 80, dtype="<i2").tobytes()

    lines = commit_and_transcribe(
        mic_bytes, system_bytes, mixed_bytes, chunk_start_s=0.0,
        model_size="base", device="cpu",
    )
    assert lines == []


def test_commit_and_transcribe_custom_noise_floor_overrides_default(monkeypatch):
    """A caller-supplied noise_floor (the slider) is honored over the
    module default -- a lower floor lets quieter audio through that the
    default would have dropped."""
    _install_fake_model(monkeypatch, [_fake_whisper_segment(0.0, 0.5, "hi")])
    n = int(0.5 * RATE)
    mic_bytes = np.full(n, 50, dtype="<i2").tobytes()      # below default NOISE_FLOOR_RMS
    system_bytes = np.full(n, 0, dtype="<i2").tobytes()
    mixed_bytes = np.full(n, 50, dtype="<i2").tobytes()

    lines = commit_and_transcribe(
        mic_bytes, system_bytes, mixed_bytes, chunk_start_s=0.0,
        model_size="base", device="cpu", noise_floor=10.0,
    )
    assert len(lines) == 1
    assert lines[0].speaker == "You"


# ---------------------------------------------------------------------------
# run_live_loop
# ---------------------------------------------------------------------------

def test_run_live_loop_commits_and_calls_on_line(monkeypatch):
    _install_fake_model(monkeypatch, [_fake_whisper_segment(0.0, 1.0, "yo")])

    buf = LiveRingBuffer()
    # 1s of "speech" + trailing silence long enough to trigger a cut.
    buf.push(_tone_i16(1.0, 1000), _silence_i16(1.0), _tone_i16(1.0, 1000))
    buf.push(_silence_i16(1.0), _silence_i16(1.0), _silence_i16(1.0))

    stop_event = threading.Event()
    lines_received = []

    def on_line(line):
        lines_received.append(line)
        stop_event.set()  # end the loop after the first committed line

    fake_timestamps = [{"start": 0, "end": RATE}]  # 1s of speech at the start
    with patch("faster_whisper.vad.get_speech_timestamps", return_value=fake_timestamps):
        run_live_loop(
            buf, stop_event, on_line,
            model_size="base", device="cpu", poll_interval_s=0.0, sleep_fn=lambda s: None,
        )

    assert len(lines_received) == 1
    assert isinstance(lines_received[0], LiveLine)
    assert lines_received[0].text == "yo"


def test_run_live_loop_passes_mic_label_through(monkeypatch):
    _install_fake_model(monkeypatch, [_fake_whisper_segment(0.0, 1.0, "yo")])

    buf = LiveRingBuffer()
    buf.push(_tone_i16(1.0, 1000), _silence_i16(1.0), _tone_i16(1.0, 1000))
    buf.push(_silence_i16(1.0), _silence_i16(1.0), _silence_i16(1.0))

    stop_event = threading.Event()
    lines_received = []

    def on_line(line):
        lines_received.append(line)
        stop_event.set()

    fake_timestamps = [{"start": 0, "end": RATE}]
    with patch("faster_whisper.vad.get_speech_timestamps", return_value=fake_timestamps):
        run_live_loop(
            buf, stop_event, on_line, mic_label="Brandon",
            model_size="base", device="cpu", poll_interval_s=0.0, sleep_fn=lambda s: None,
        )

    assert lines_received[0].speaker == "Brandon"


def test_run_live_loop_stops_immediately_when_stop_event_already_set():
    buf = LiveRingBuffer()
    stop_event = threading.Event()
    stop_event.set()
    calls = []
    run_live_loop(buf, stop_event, lambda line: calls.append(line), sleep_fn=lambda s: None)
    assert calls == []


def test_run_live_loop_skips_flaky_chunk_without_ending_loop(monkeypatch):
    """A per-chunk transcription exception is logged and skipped, not fatal."""
    buf = LiveRingBuffer()
    buf.push(_tone_i16(15.0, 1000), _tone_i16(15.0, 1000), _tone_i16(15.0, 1000))

    stop_event = threading.Event()
    call_count = {"n": 0}

    def failing_commit(*a, **kw):
        call_count["n"] += 1
        stop_event.set()
        raise RuntimeError("boom")

    with patch("wisper_transcribe.web.live_transcribe.commit_and_transcribe", side_effect=failing_commit):
        run_live_loop(buf, stop_event, lambda line: None, sleep_fn=lambda s: None)

    assert call_count["n"] == 1  # ran once, didn't crash the loop


def test_run_live_loop_does_not_chain_initial_prompt():
    """Regression test: a committed chunk's text must NOT be chained into
    the next chunk's initial_prompt. Confirmed on real speech to send
    faster-whisper into repetition loops ("column column column...") that
    got worse chunk over chunk as each hallucinated repeat re-primed the
    next prompt -- see run_live_loop's docstring."""
    buf = LiveRingBuffer()
    # 15s+ force-cuts (find_commit_boundary short-circuits past real VAD
    # once total_s >= FORCE_CUT_S), so no VAD mocking is needed here.
    buf.push(_tone_i16(15.0, 1000), _tone_i16(15.0, 1000), _tone_i16(15.0, 1000))

    stop_event = threading.Event()
    prompts_seen = []
    call_count = {"n": 0}

    def fake_commit(*a, **kw):
        call_count["n"] += 1
        prompts_seen.append(kw.get("initial_prompt"))
        if call_count["n"] >= 2:
            stop_event.set()
        else:
            buf.push(_tone_i16(15.0, 1000), _tone_i16(15.0, 1000), _tone_i16(15.0, 1000))
        return [LiveLine(speaker="You", text="column column column", start_s=0.0, end_s=1.0)]

    with patch("wisper_transcribe.web.live_transcribe.commit_and_transcribe", side_effect=fake_commit):
        run_live_loop(buf, stop_event, lambda line: None, initial_prompt=None, sleep_fn=lambda s: None)

    assert call_count["n"] == 2
    assert prompts_seen == [None, None]


def test_run_live_loop_reads_noise_floor_live_each_chunk():
    """get_noise_floor is called fresh every iteration -- not once at loop
    start -- so a slider adjustment mid-session takes effect on the very
    next chunk without restarting the session."""
    buf = LiveRingBuffer()
    buf.push(_tone_i16(15.0, 1000), _tone_i16(15.0, 1000), _tone_i16(15.0, 1000))

    stop_event = threading.Event()
    floors_seen = []
    call_count = {"n": 0}
    current_floor = {"v": 150.0}

    def fake_commit(*a, **kw):
        call_count["n"] += 1
        floors_seen.append(kw.get("noise_floor"))
        if call_count["n"] >= 2:
            stop_event.set()
        else:
            current_floor["v"] = 400.0  # simulate the slider moving between chunks
            buf.push(_tone_i16(15.0, 1000), _tone_i16(15.0, 1000), _tone_i16(15.0, 1000))
        return []

    with patch("wisper_transcribe.web.live_transcribe.commit_and_transcribe", side_effect=fake_commit):
        run_live_loop(
            buf, stop_event, lambda line: None, sleep_fn=lambda s: None,
            get_noise_floor=lambda: current_floor["v"],
        )

    assert floors_seen == [150.0, 400.0]


def test_run_live_loop_defaults_noise_floor_when_not_given():
    """Without an explicit get_noise_floor, the module default is used."""
    buf = LiveRingBuffer()
    buf.push(_tone_i16(15.0, 1000), _tone_i16(15.0, 1000), _tone_i16(15.0, 1000))
    stop_event = threading.Event()
    floors_seen = []

    def fake_commit(*a, **kw):
        floors_seen.append(kw.get("noise_floor"))
        stop_event.set()
        return []

    with patch("wisper_transcribe.web.live_transcribe.commit_and_transcribe", side_effect=fake_commit):
        run_live_loop(buf, stop_event, lambda line: None, sleep_fn=lambda s: None)

    assert floors_seen == [NOISE_FLOOR_RMS]
