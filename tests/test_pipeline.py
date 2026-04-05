from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wisper_transcribe.models import TranscriptionSegment


FAKE_SEGMENTS = [
    TranscriptionSegment(start=0.0, end=5.0, text="Welcome to the game"),
    TranscriptionSegment(start=5.0, end=10.0, text="Let us begin"),
]


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
