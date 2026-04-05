from pathlib import Path
from unittest.mock import patch

import pytest

from wisper_transcribe.models import TranscriptionSegment

FAKE_SEGMENTS = [TranscriptionSegment(start=0.0, end=5.0, text="Hello")]


def _make_audio_files(folder: Path, names: list[str]) -> list[Path]:
    files = []
    for name in names:
        f = folder / name
        f.write_bytes(b"fake audio")
        files.append(f)
    return files


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=300.0)
@patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS)
def test_process_folder_transcribes_all(
    mock_t, mock_d, mock_c, mock_v, mock_f, tmp_path
):
    _make_audio_files(tmp_path, ["s01.mp3", "s02.mp3", "s03.mp3"])
    mock_c.side_effect = lambda p: p

    from wisper_transcribe.pipeline import process_folder

    successes, errors = process_folder(
        tmp_path, output_dir=tmp_path, no_diarize=True, device="cpu"
    )

    assert len(successes) == 3
    assert errors == []
    for name in ["s01.md", "s02.md", "s03.md"]:
        assert (tmp_path / name).exists()


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=300.0)
@patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS)
def test_process_folder_skips_existing(
    mock_t, mock_d, mock_c, mock_v, mock_f, tmp_path
):
    _make_audio_files(tmp_path, ["s01.mp3"])
    (tmp_path / "s01.md").write_text("existing")
    mock_c.side_effect = lambda p: p

    from wisper_transcribe.pipeline import process_folder

    successes, errors = process_folder(
        tmp_path, output_dir=tmp_path, no_diarize=True, device="cpu", overwrite=False
    )

    # transcribe should NOT have been called (file was skipped)
    mock_t.assert_not_called()
    assert successes == []
    assert (tmp_path / "s01.md").read_text() == "existing"


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=300.0)
@patch("wisper_transcribe.pipeline.transcribe", side_effect=RuntimeError("boom"))
def test_process_folder_continues_on_error(
    mock_t, mock_d, mock_c, mock_v, mock_f, tmp_path
):
    _make_audio_files(tmp_path, ["s01.mp3", "s02.mp3"])
    mock_c.side_effect = lambda p: p

    from wisper_transcribe.pipeline import process_folder

    successes, errors = process_folder(
        tmp_path, output_dir=tmp_path, no_diarize=True, device="cpu"
    )

    # Both files errored but no exception was raised
    assert len(errors) == 2
    assert all("boom" in e for e in errors)


def test_process_folder_empty_dir(tmp_path):
    from wisper_transcribe.pipeline import process_folder

    successes, errors = process_folder(tmp_path)
    assert successes == []
    assert errors == []
