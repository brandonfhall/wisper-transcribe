import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from wisper_transcribe.models import AlignedSegment, TranscriptionSegment


FAKE_SEGMENTS = [
    TranscriptionSegment(start=0.0, end=5.0, text="Welcome to the game"),
    TranscriptionSegment(start=5.0, end=10.0, text="Let us begin"),
]


def test_seconds_to_hhmmss():
    from wisper_transcribe.pipeline import _seconds_to_hhmmss
    assert _seconds_to_hhmmss(0) == "0:00:00"
    assert _seconds_to_hhmmss(61) == "0:01:01"
    assert _seconds_to_hhmmss(3725) == "1:02:05"
    assert _seconds_to_hhmmss(3600) == "1:00:00"


@patch("wisper_transcribe.pipeline.subprocess.run")
def test_play_excerpt_calls_ffplay(mock_run, tmp_path):
    """_play_excerpt invokes ffplay with the correct -ss / -t arguments."""
    from wisper_transcribe.pipeline import _play_excerpt

    wav = tmp_path / "test.wav"
    wav.write_bytes(b"")
    _play_excerpt(wav, 10.0, 15.0)

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "ffplay"
    assert "-ss" in cmd and "10.0" in cmd
    assert "-t" in cmd and "5.0" in cmd


@patch("wisper_transcribe.pipeline.subprocess.run", side_effect=FileNotFoundError)
def test_play_excerpt_ffplay_not_found(mock_run, tmp_path, capsys):
    """_play_excerpt warns (does not raise) when ffplay is missing."""
    from wisper_transcribe.pipeline import _play_excerpt

    _play_excerpt(tmp_path / "test.wav", 0.0, 5.0)  # must not raise
    out = capsys.readouterr().out
    assert "ffplay not found" in out


@patch("wisper_transcribe.pipeline.subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffplay"))
def test_play_excerpt_ffplay_error(mock_run, tmp_path, capsys):
    """_play_excerpt warns (does not raise) when ffplay exits non-zero."""
    from wisper_transcribe.pipeline import _play_excerpt

    _play_excerpt(tmp_path / "test.wav", 0.0, 5.0)  # must not raise
    out = capsys.readouterr().out
    assert "playback failed" in out


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS)
def test_process_file_creates_markdown(
    mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg, tmp_path
):
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake audio")
    mock_convert.return_value = audio

    from wisper_transcribe.pipeline import process_file

    out = process_file(audio, output_dir=tmp_path, device="cpu", model_size="tiny", no_diarize=True)

    assert out.exists()
    assert out.suffix == ".md"
    content = out.read_text(encoding="utf-8")
    assert "Welcome to the game" in content
    assert "Let us begin" in content


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS)
def test_process_file_skips_existing(
    mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg, tmp_path
):
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake audio")
    mock_convert.return_value = audio

    existing = tmp_path / "session01.md"
    existing.write_text("existing content")

    from wisper_transcribe.pipeline import process_file

    out = process_file(audio, output_dir=tmp_path, device="cpu", overwrite=False)

    # transcribe should NOT have been called
    mock_transcribe.assert_not_called()
    assert out.read_text() == "existing content"


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS)
def test_process_file_overwrites_when_forced(
    mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg, tmp_path
):
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake audio")
    mock_convert.return_value = audio

    existing = tmp_path / "session01.md"
    existing.write_text("old content")

    from wisper_transcribe.pipeline import process_file

    out = process_file(audio, output_dir=tmp_path, device="cpu", overwrite=True, no_diarize=True)

    assert "Welcome to the game" in out.read_text(encoding="utf-8")


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=3725.0)
@patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS)
def test_process_file_frontmatter_metadata(
    mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg, tmp_path
):
    audio = tmp_path / "session_01.mp3"
    audio.write_bytes(b"fake audio")
    mock_convert.return_value = audio

    from wisper_transcribe.pipeline import process_file

    out = process_file(audio, output_dir=tmp_path, device="cpu", no_diarize=True)
    content = out.read_text(encoding="utf-8")

    assert "source_file: session_01.mp3" in content
    assert "date_processed:" in content
    assert "1:02:05" in content  # 3725 seconds = 1h 2m 5s


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.transcribe")
@patch("wisper_transcribe.pipeline.get_hf_token", return_value="fake-token")
@patch("wisper_transcribe.diarizer.diarize", return_value=[])
@patch("wisper_transcribe.aligner.align")
@patch("wisper_transcribe.speaker_manager.enroll_speaker")
@patch("wisper_transcribe.speaker_manager.load_profiles", return_value={})
@patch("click.prompt", return_value="Test")
def test_enroll_speakers_chronological_order(
    mock_prompt, mock_load_profiles, mock_enroll, mock_align, mock_diarize, mock_hf_token,
    mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg,
    tmp_path,
):
    """Speakers are presented for enrollment in order of first appearance, not label order."""
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake audio")
    mock_convert.return_value = audio
    mock_transcribe.return_value = [
        TranscriptionSegment(start=0.0, end=5.0, text="Hello"),
        TranscriptionSegment(start=10.0, end=15.0, text="World"),
    ]
    # SPEAKER_01 appears first at t=0; SPEAKER_00 appears second at t=10.
    # Chronological order should be [SPEAKER_01, SPEAKER_00] not [SPEAKER_00, SPEAKER_01].
    mock_align.return_value = [
        AlignedSegment(start=0.0, end=5.0, text="Hello", speaker="SPEAKER_01"),
        AlignedSegment(start=10.0, end=15.0, text="World", speaker="SPEAKER_00"),
    ]

    from wisper_transcribe.pipeline import process_file

    process_file(audio, output_dir=tmp_path, device="cpu", enroll_speakers=True)

    enrolled_labels = [c.kwargs["speaker_label"] for c in mock_enroll.call_args_list]
    assert enrolled_labels == ["SPEAKER_01", "SPEAKER_00"]


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.transcribe")
@patch("wisper_transcribe.pipeline.get_hf_token", return_value="fake-token")
@patch("wisper_transcribe.diarizer.diarize", return_value=[])
@patch("wisper_transcribe.aligner.align")
@patch("wisper_transcribe.speaker_manager.enroll_speaker")
@patch("wisper_transcribe.speaker_manager.load_profiles", return_value={})
@patch("wisper_transcribe.pipeline._play_excerpt")
@patch("click.prompt", return_value="Alice")
def test_enroll_play_audio_calls_play_excerpt(
    mock_prompt, mock_play, mock_load_profiles, mock_enroll, mock_align, mock_diarize, mock_hf_token,
    mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg,
    tmp_path,
):
    """play_audio=True calls _play_excerpt for each speaker sample."""
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake audio")
    mock_convert.return_value = audio
    mock_transcribe.return_value = [
        TranscriptionSegment(start=0.0, end=5.0, text="Hello"),
    ]
    mock_align.return_value = [
        AlignedSegment(start=0.0, end=5.0, text="Hello", speaker="SPEAKER_00"),
    ]

    from wisper_transcribe.pipeline import process_file

    process_file(audio, output_dir=tmp_path, device="cpu", enroll_speakers=True, play_audio=True)

    mock_play.assert_called_once_with(audio, 0.0, 5.0)


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.transcribe")
@patch("wisper_transcribe.pipeline.get_hf_token", return_value="fake-token")
@patch("wisper_transcribe.diarizer.diarize", return_value=[])
@patch("wisper_transcribe.aligner.align")
@patch("wisper_transcribe.speaker_manager.enroll_speaker")
@patch("wisper_transcribe.speaker_manager.load_profiles", return_value={})
@patch("click.prompt", return_value="Alice")
def test_enroll_play_audio_false_does_not_play(
    mock_prompt, mock_load_profiles, mock_enroll, mock_align, mock_diarize, mock_hf_token,
    mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg,
    tmp_path,
):
    """play_audio=False (default) never calls _play_excerpt."""
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake audio")
    mock_convert.return_value = audio
    mock_transcribe.return_value = [
        TranscriptionSegment(start=0.0, end=5.0, text="Hello"),
    ]
    mock_align.return_value = [
        AlignedSegment(start=0.0, end=5.0, text="Hello", speaker="SPEAKER_00"),
    ]

    with patch("wisper_transcribe.pipeline._play_excerpt") as mock_play:
        from wisper_transcribe.pipeline import process_file
        process_file(audio, output_dir=tmp_path, device="cpu", enroll_speakers=True, play_audio=False)
        mock_play.assert_not_called()


# ---------------------------------------------------------------------------
# Feature 1: replay 'r' during enrollment
# ---------------------------------------------------------------------------

@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.transcribe")
@patch("wisper_transcribe.pipeline.get_hf_token", return_value="fake-token")
@patch("wisper_transcribe.diarizer.diarize", return_value=[])
@patch("wisper_transcribe.aligner.align")
@patch("wisper_transcribe.speaker_manager.enroll_speaker")
@patch("wisper_transcribe.speaker_manager.load_profiles", return_value={})
@patch("wisper_transcribe.pipeline._play_excerpt")
def test_enroll_replay_r_triggers_second_play(
    mock_play, mock_load_profiles, mock_enroll, mock_align, mock_diarize, mock_hf_token,
    mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg,
    tmp_path,
):
    """Entering 'r' at the name prompt replays the excerpt and re-asks."""
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake audio")
    mock_convert.return_value = audio
    mock_transcribe.return_value = [TranscriptionSegment(start=0.0, end=5.0, text="Hello")]
    mock_align.return_value = [AlignedSegment(start=0.0, end=5.0, text="Hello", speaker="SPEAKER_00")]

    # First prompt call returns 'r' (replay), second returns the actual name
    prompt_responses = iter(["r", "Alice", "", ""])
    with patch("click.prompt", side_effect=lambda *a, **kw: next(prompt_responses)):
        from wisper_transcribe.pipeline import process_file
        process_file(audio, output_dir=tmp_path, device="cpu", enroll_speakers=True, play_audio=True)

    # _play_excerpt: once on initial display + once for replay = 2 calls
    assert mock_play.call_count == 2


# ---------------------------------------------------------------------------
# Feature 2: pick existing speaker by number
# ---------------------------------------------------------------------------

@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.transcribe")
@patch("wisper_transcribe.pipeline.get_hf_token", return_value="fake-token")
@patch("wisper_transcribe.diarizer.diarize", return_value=[])
@patch("wisper_transcribe.aligner.align")
@patch("wisper_transcribe.speaker_manager.enroll_speaker")
def test_enroll_pick_existing_speaker_skips_enroll(
    mock_enroll, mock_align, mock_diarize, mock_hf_token,
    mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg,
    tmp_path,
):
    """Selecting an existing speaker by number skips enroll_speaker; confirm=No skips embedding update."""
    import numpy as np
    from wisper_transcribe.models import SpeakerProfile

    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake audio")
    mock_convert.return_value = audio
    mock_transcribe.return_value = [TranscriptionSegment(start=0.0, end=5.0, text="Hello")]
    mock_align.return_value = [AlignedSegment(start=0.0, end=5.0, text="Hello", speaker="SPEAKER_00")]

    fake_emb = np.zeros(512, dtype=np.float32)
    npy_path = tmp_path / "alice.npy"
    np.save(str(npy_path), fake_emb)

    existing = {
        "alice": SpeakerProfile(
            name="alice", display_name="Alice", role="DM",
            embedding_path=npy_path,
            enrolled_date="2026-01-01", enrollment_source="ep1.mp3",
        )
    }

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value=existing):
        with patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=fake_emb):
            with patch("click.prompt", return_value="1"):
                with patch("click.confirm", return_value=False) as mock_confirm:
                    from wisper_transcribe.pipeline import process_file
                    out = process_file(audio, output_dir=tmp_path, device="cpu", enroll_speakers=True)

    mock_enroll.assert_not_called()
    mock_confirm.assert_called_once()
    content = out.read_text(encoding="utf-8")
    assert "Alice" in content


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.transcribe")
@patch("wisper_transcribe.pipeline.get_hf_token", return_value="fake-token")
@patch("wisper_transcribe.diarizer.diarize", return_value=[])
@patch("wisper_transcribe.aligner.align")
@patch("wisper_transcribe.speaker_manager.enroll_speaker")
def test_enroll_pick_existing_speaker_confirm_yes_updates_embedding(
    mock_enroll, mock_align, mock_diarize, mock_hf_token,
    mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg,
    tmp_path,
):
    """Confirming yes on an existing speaker extracts a new embedding and blends it via EMA."""
    from wisper_transcribe.models import SpeakerProfile

    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake audio")
    mock_convert.return_value = audio
    mock_transcribe.return_value = [TranscriptionSegment(start=0.0, end=5.0, text="Hello")]
    mock_align.return_value = [AlignedSegment(start=0.0, end=5.0, text="Hello", speaker="SPEAKER_00")]

    import numpy as np
    fake_emb = np.zeros(512, dtype=np.float32)
    npy_path = tmp_path / "alice.npy"
    np.save(str(npy_path), fake_emb)

    existing = {
        "alice": SpeakerProfile(
            name="alice", display_name="Alice", role="DM",
            embedding_path=npy_path,
            enrolled_date="2026-01-01", enrollment_source="ep1.mp3",
        )
    }

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value=existing):
        with patch("click.prompt", return_value="1"):
            with patch("click.confirm", return_value=True):
                with patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=fake_emb) as mock_extract:
                    with patch("wisper_transcribe.speaker_manager.update_embedding") as mock_update:
                        from wisper_transcribe.pipeline import process_file
                        process_file(audio, output_dir=tmp_path, device="cpu", enroll_speakers=True)

    # extract_embedding called twice: once for ranking display, once for EMA update
    assert mock_extract.call_count == 2
    mock_update.assert_called_once_with("alice", fake_emb)
    mock_enroll.assert_not_called()


# ---------------------------------------------------------------------------
@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.transcribe")
@patch("wisper_transcribe.pipeline.get_hf_token", return_value="fake-token")
@patch("wisper_transcribe.diarizer.diarize", return_value=[])
@patch("wisper_transcribe.aligner.align")
@patch("wisper_transcribe.speaker_manager.enroll_speaker")
def test_enroll_existing_speakers_ranked_by_similarity(
    mock_enroll, mock_align, mock_diarize, mock_hf_token,
    mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg,
    tmp_path, capsys,
):
    """Existing speakers are listed in descending similarity order with scores."""
    import numpy as np
    from wisper_transcribe.models import SpeakerProfile

    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake audio")
    mock_convert.return_value = audio
    mock_transcribe.return_value = [TranscriptionSegment(start=0.0, end=5.0, text="Hello")]
    mock_align.return_value = [AlignedSegment(start=0.0, end=5.0, text="Hello", speaker="SPEAKER_00")]

    # Alice embedding is close to the query; Bob is orthogonal (score ~0)
    alice_emb = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    bob_emb   = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    query_emb = np.array([0.9, 0.1, 0.0], dtype=np.float32)  # closer to Alice

    for name, emb in [("alice", alice_emb), ("bob", bob_emb)]:
        np.save(str(tmp_path / f"{name}.npy"), emb)

    existing = {
        "alice": SpeakerProfile("alice", "Alice", "DM", tmp_path / "alice.npy", "2026-01-01", "ep1.mp3"),
        "bob":   SpeakerProfile("bob",   "Bob",   "Player", tmp_path / "bob.npy", "2026-01-01", "ep1.mp3"),
    }

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value=existing):
        with patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=query_emb):
            with patch("click.prompt", return_value="Alice"):
                with patch("click.confirm", return_value=False):
                    from wisper_transcribe.pipeline import process_file
                    process_file(audio, output_dir=tmp_path, device="cpu", enroll_speakers=True)

    out = capsys.readouterr().out
    alice_pos = out.index("Alice")
    bob_pos   = out.index("Bob")
    assert alice_pos < bob_pos, "Alice (higher similarity) should appear before Bob"


# ---------------------------------------------------------------------------
# Feature 3: hotwords / initial_prompt pass-through
# ---------------------------------------------------------------------------

def test_transcribe_passes_hotwords():
    """hotwords list is forwarded to model.transcribe()."""
    from unittest.mock import MagicMock
    mock_model = MagicMock()
    mock_model.transcribe.return_value = (iter([]), MagicMock(duration=1.0))

    import wisper_transcribe.transcriber as t
    t._model = mock_model

    t.transcribe(Path("fake.wav"), device="cpu", hotwords=["Kyra", "Golarion"])
    _, kwargs = mock_model.transcribe.call_args
    assert kwargs.get("hotwords") == ["Kyra", "Golarion"]


def test_transcribe_passes_initial_prompt():
    """initial_prompt string is forwarded to model.transcribe()."""
    from unittest.mock import MagicMock
    mock_model = MagicMock()
    mock_model.transcribe.return_value = (iter([]), MagicMock(duration=1.0))

    import wisper_transcribe.transcriber as t
    t._model = mock_model

    t.transcribe(Path("fake.wav"), device="cpu", initial_prompt="Kyra Zeldris Golarion")
    _, kwargs = mock_model.transcribe.call_args
    assert kwargs.get("initial_prompt") == "Kyra Zeldris Golarion"


def test_transcribe_hotwords_none_by_default():
    """hotwords defaults to None (not passed as a non-None value)."""
    from unittest.mock import MagicMock
    mock_model = MagicMock()
    mock_model.transcribe.return_value = (iter([]), MagicMock(duration=1.0))

    import wisper_transcribe.transcriber as t
    t._model = mock_model

    t.transcribe(Path("fake.wav"), device="cpu")
    _, kwargs = mock_model.transcribe.call_args
    assert kwargs.get("hotwords") is None
    assert kwargs.get("initial_prompt") is None


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=300.0)
@patch("wisper_transcribe.pipeline.transcribe")
def test_process_file_uses_config_hotwords(
    mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg, tmp_path
):
    """When hotwords=None is passed, process_file falls back to config['hotwords']."""
    audio = tmp_path / "ep.mp3"
    audio.write_bytes(b"fake")
    mock_convert.return_value = audio
    mock_transcribe.return_value = FAKE_SEGMENTS

    with patch("wisper_transcribe.pipeline.load_config", return_value={
        "hotwords": ["Kyra", "Golarion"],
        "vad_filter": True,
        "model": "medium",
        "language": "en",
        "compute_type": "auto",
    }):
        from wisper_transcribe.pipeline import process_file
        process_file(audio, output_dir=tmp_path, device="cpu", no_diarize=True)

    _, kwargs = mock_transcribe.call_args
    assert kwargs.get("hotwords") == ["Kyra", "Golarion"]


# ---------------------------------------------------------------------------
# Feature 4: always show skip message for already-processed files
# ---------------------------------------------------------------------------

@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=300.0)
@patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS)
def test_skip_message_shown_without_verbose(
    mock_t, mock_d, mock_c, mock_v, mock_f, tmp_path, capsys
):
    """Already-processed skip message is shown regardless of verbose flag."""
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake")
    (tmp_path / "session01.md").write_text("existing")
    mock_c.side_effect = lambda p: p

    from wisper_transcribe.pipeline import process_folder

    process_folder(tmp_path, output_dir=tmp_path, no_diarize=True, device="cpu", verbose=False)

    captured = capsys.readouterr()
    assert "already processed" in captured.out


# ---------------------------------------------------------------------------
# Feature 5: use_mlx config key forwarded to transcribe()
# ---------------------------------------------------------------------------

@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=300.0)
@patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS)
def test_process_file_forwards_use_mlx_from_config(
    mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg, tmp_path
):
    """use_mlx from config is forwarded to transcribe() as a keyword argument."""
    audio = tmp_path / "ep.mp3"
    audio.write_bytes(b"fake")
    mock_convert.return_value = audio

    with patch("wisper_transcribe.pipeline.load_config", return_value={
        "model": "medium",
        "language": "en",
        "compute_type": "auto",
        "vad_filter": True,
        "hotwords": [],
        "use_mlx": "false",
        "parallel_stages": False,
    }):
        from wisper_transcribe.pipeline import process_file
        process_file(audio, output_dir=tmp_path, device="cpu", no_diarize=True)

    _, kwargs = mock_transcribe.call_args
    assert kwargs.get("use_mlx") == "false"


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=300.0)
@patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS)
def test_process_file_use_mlx_defaults_to_auto(
    mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg, tmp_path
):
    """use_mlx defaults to 'auto' when not present in config."""
    audio = tmp_path / "ep.mp3"
    audio.write_bytes(b"fake")
    mock_convert.return_value = audio

    # Config missing use_mlx key
    with patch("wisper_transcribe.pipeline.load_config", return_value={
        "model": "medium",
        "language": "en",
        "compute_type": "auto",
        "vad_filter": True,
        "hotwords": [],
        "parallel_stages": False,
    }):
        from wisper_transcribe.pipeline import process_file
        process_file(audio, output_dir=tmp_path, device="cpu", no_diarize=True)

    _, kwargs = mock_transcribe.call_args
    assert kwargs.get("use_mlx") == "auto"


# ---------------------------------------------------------------------------
# Feature 6: parallel_stages — concurrent transcription + diarization
# ---------------------------------------------------------------------------

@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.get_hf_token", return_value="fake-token")
@patch("wisper_transcribe.pipeline._run_parallel_transcribe_diarize")
@patch("wisper_transcribe.aligner.align")
@patch("wisper_transcribe.speaker_manager.match_speakers", return_value={})
def test_parallel_stages_calls_parallel_helper(
    mock_match, mock_align, mock_parallel, mock_hf,
    mock_duration, mock_convert, mock_validate, mock_ffmpeg, tmp_path
):
    """When parallel_stages=True and diarize is enabled, _run_parallel_transcribe_diarize is called."""
    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    mock_convert.return_value = audio
    mock_parallel.return_value = (FAKE_SEGMENTS, [])
    mock_align.return_value = []

    with patch("wisper_transcribe.pipeline.load_config", return_value={
        "model": "medium",
        "language": "en",
        "compute_type": "auto",
        "vad_filter": True,
        "hotwords": [],
        "use_mlx": "auto",
        "parallel_stages": True,
        "similarity_threshold": 0.65,
    }):
        from wisper_transcribe.pipeline import process_file
        process_file(audio, output_dir=tmp_path, device="cpu", no_diarize=False)

    mock_parallel.assert_called_once()


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS)
@patch("wisper_transcribe.pipeline._run_parallel_transcribe_diarize")
def test_parallel_stages_disabled_uses_sequential(
    mock_parallel, mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg, tmp_path
):
    """When parallel_stages=False (default), _run_parallel_transcribe_diarize is NOT called."""
    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    mock_convert.return_value = audio

    with patch("wisper_transcribe.pipeline.load_config", return_value={
        "model": "medium",
        "language": "en",
        "compute_type": "auto",
        "vad_filter": True,
        "hotwords": [],
        "use_mlx": "auto",
        "parallel_stages": False,
    }):
        from wisper_transcribe.pipeline import process_file
        process_file(audio, output_dir=tmp_path, device="cpu", no_diarize=True)

    mock_parallel.assert_not_called()
    mock_transcribe.assert_called_once()


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS)
@patch("wisper_transcribe.pipeline._run_parallel_transcribe_diarize")
def test_parallel_stages_skipped_when_no_diarize(
    mock_parallel, mock_transcribe, mock_duration, mock_convert, mock_validate, mock_ffmpeg, tmp_path
):
    """parallel_stages=True is ignored when no_diarize=True — sequential transcription only."""
    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    mock_convert.return_value = audio

    with patch("wisper_transcribe.pipeline.load_config", return_value={
        "model": "medium",
        "language": "en",
        "compute_type": "auto",
        "vad_filter": True,
        "hotwords": [],
        "use_mlx": "auto",
        "parallel_stages": True,
    }):
        from wisper_transcribe.pipeline import process_file
        process_file(audio, output_dir=tmp_path, device="cpu", no_diarize=True)

    mock_parallel.assert_not_called()
    mock_transcribe.assert_called_once()


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS)
@patch("wisper_transcribe.pipeline.get_hf_token", return_value="")
@patch("wisper_transcribe.pipeline._run_parallel_transcribe_diarize")
def test_parallel_stages_skipped_without_hf_token(
    mock_parallel, mock_hf, mock_transcribe,
    mock_duration, mock_convert, mock_validate, mock_ffmpeg, tmp_path
):
    """parallel_stages=True is ignored when no HF token — sequential path used."""
    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    mock_convert.return_value = audio

    with patch("wisper_transcribe.pipeline.load_config", return_value={
        "model": "medium",
        "language": "en",
        "compute_type": "auto",
        "vad_filter": True,
        "hotwords": [],
        "use_mlx": "auto",
        "parallel_stages": True,
    }):
        from wisper_transcribe.pipeline import process_file
        process_file(audio, output_dir=tmp_path, device="cpu", no_diarize=False)

    mock_parallel.assert_not_called()
    mock_transcribe.assert_called_once()


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.get_hf_token", return_value="fake-token")
@patch("wisper_transcribe.pipeline._run_parallel_transcribe_diarize")
@patch("wisper_transcribe.aligner.align")
@patch("wisper_transcribe.speaker_manager.match_speakers", return_value={})
def test_parallel_stages_produces_correct_output(
    mock_match, mock_align, mock_parallel, mock_hf,
    mock_duration, mock_convert, mock_validate, mock_ffmpeg, tmp_path
):
    """parallel_stages=True produces the same markdown output as sequential."""
    from wisper_transcribe.models import AlignedSegment

    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    mock_convert.return_value = audio

    fake_aligned = [
        AlignedSegment(start=0.0, end=5.0, speaker="SPEAKER_00", text="Welcome to the game"),
        AlignedSegment(start=5.0, end=10.0, speaker="SPEAKER_00", text="Let us begin"),
    ]
    mock_parallel.return_value = (FAKE_SEGMENTS, [])
    mock_align.return_value = fake_aligned

    with patch("wisper_transcribe.pipeline.load_config", return_value={
        "model": "medium",
        "language": "en",
        "compute_type": "auto",
        "vad_filter": True,
        "hotwords": [],
        "use_mlx": "auto",
        "parallel_stages": True,
        "similarity_threshold": 0.65,
    }):
        from wisper_transcribe.pipeline import process_file
        out = process_file(audio, output_dir=tmp_path, device="cpu", no_diarize=False)

    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "Welcome to the game" in content
    assert "Let us begin" in content


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=600.0)
@patch("wisper_transcribe.pipeline.get_hf_token", return_value="fake-token")
@patch("wisper_transcribe.pipeline._run_parallel_transcribe_diarize")
@patch("wisper_transcribe.aligner.align")
@patch("wisper_transcribe.speaker_manager.match_speakers", return_value={})
def test_parallel_stages_passes_use_mlx_to_worker(
    mock_match, mock_align, mock_parallel, mock_hf,
    mock_duration, mock_convert, mock_validate, mock_ffmpeg, tmp_path
):
    """parallel_stages path forwards use_mlx to the transcription worker kwargs."""
    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    mock_convert.return_value = audio
    mock_parallel.return_value = (FAKE_SEGMENTS, [])
    mock_align.return_value = []

    with patch("wisper_transcribe.pipeline.load_config", return_value={
        "model": "medium",
        "language": "en",
        "compute_type": "auto",
        "vad_filter": True,
        "hotwords": [],
        "use_mlx": "false",
        "parallel_stages": True,
        "similarity_threshold": 0.65,
    }):
        from wisper_transcribe.pipeline import process_file
        process_file(audio, output_dir=tmp_path, device="cpu", no_diarize=False)

    _, call_args, call_kwargs = mock_parallel.mock_calls[0]
    transcribe_kwargs = call_args[1]  # second positional arg is transcribe_kwargs dict
    assert transcribe_kwargs.get("use_mlx") == "false"
