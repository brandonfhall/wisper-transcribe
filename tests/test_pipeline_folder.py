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


# ---------------------------------------------------------------------------
# Phase 10 — parallel workers tests
# ---------------------------------------------------------------------------


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=300.0)
@patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS)
def test_process_folder_workers_1_unchanged(
    mock_t, mock_d, mock_c, mock_v, mock_f, tmp_path
):
    """workers=1 (default) uses the existing sequential path unchanged."""
    _make_audio_files(tmp_path, ["s01.mp3", "s02.mp3"])
    mock_c.side_effect = lambda p: p

    from wisper_transcribe.pipeline import process_folder

    with patch("wisper_transcribe.pipeline.ProcessPoolExecutor") as mock_ppe:
        successes, errors = process_folder(
            tmp_path, output_dir=tmp_path, no_diarize=True, device="cpu", workers=1
        )

    mock_ppe.assert_not_called()
    assert len(successes) == 2
    assert errors == []


@patch("wisper_transcribe.pipeline.check_ffmpeg")
@patch("wisper_transcribe.pipeline.validate_audio")
@patch("wisper_transcribe.pipeline.convert_to_wav")
@patch("wisper_transcribe.pipeline.get_duration", return_value=300.0)
@patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS)
def test_process_folder_workers_2_cpu_uses_process_pool(
    mock_t, mock_d, mock_c, mock_v, mock_f, tmp_path
):
    """workers=2 on CPU invokes ProcessPoolExecutor with max_workers=2."""
    _make_audio_files(tmp_path, ["s01.mp3", "s02.mp3"])
    mock_c.side_effect = lambda p: p

    from unittest.mock import MagicMock
    from pathlib import Path

    expected_paths = [tmp_path / "s01.md", tmp_path / "s02.md"]
    for p in expected_paths:
        p.write_text("# output")

    mock_future_1 = MagicMock()
    mock_future_1.result.return_value = expected_paths[0]
    mock_future_2 = MagicMock()
    mock_future_2.result.return_value = expected_paths[1]

    mock_executor = MagicMock()
    mock_executor.__enter__ = MagicMock(return_value=mock_executor)
    mock_executor.__exit__ = MagicMock(return_value=False)
    mock_executor.submit.side_effect = [mock_future_1, mock_future_2]

    from wisper_transcribe.pipeline import process_folder

    with patch("wisper_transcribe.pipeline.ProcessPoolExecutor", return_value=mock_executor) as mock_ppe_cls:
        with patch("wisper_transcribe.pipeline.as_completed", return_value=iter([mock_future_1, mock_future_2])):
            successes, errors = process_folder(
                tmp_path, output_dir=tmp_path, no_diarize=True, device="cpu", workers=2
            )

    mock_ppe_cls.assert_called_once_with(max_workers=2)
    assert len(successes) == 2
    assert errors == []


@patch("wisper_transcribe.pipeline.get_device", return_value="cuda")
def test_process_folder_workers_gpu_clamped(mock_gd, tmp_path, capsys):
    """workers > 1 with device=cuda clamps to 1 and emits a warning."""
    _make_audio_files(tmp_path, ["s01.mp3"])

    from wisper_transcribe.pipeline import process_folder

    with patch("wisper_transcribe.pipeline.ProcessPoolExecutor") as mock_ppe:
        with patch("wisper_transcribe.pipeline.check_ffmpeg"):
            with patch("wisper_transcribe.pipeline.validate_audio"):
                with patch("wisper_transcribe.pipeline.convert_to_wav", side_effect=lambda p: p):
                    with patch("wisper_transcribe.pipeline.get_duration", return_value=10.0):
                        with patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS):
                            successes, errors = process_folder(
                                tmp_path,
                                output_dir=tmp_path,
                                no_diarize=True,
                                device="cuda",
                                workers=2,
                            )

    # ProcessPoolExecutor must NOT be used — clamped to 1
    mock_ppe.assert_not_called()
    assert len(successes) == 1


@patch("wisper_transcribe.pipeline.get_device", return_value="cuda")
def test_process_folder_workers_auto_device_gpu_clamped(mock_gd, tmp_path):
    """workers > 1 with device=auto that resolves to cuda is also clamped."""
    _make_audio_files(tmp_path, ["s01.mp3"])

    from wisper_transcribe.pipeline import process_folder

    with patch("wisper_transcribe.pipeline.ProcessPoolExecutor") as mock_ppe:
        with patch("wisper_transcribe.pipeline.check_ffmpeg"):
            with patch("wisper_transcribe.pipeline.validate_audio"):
                with patch("wisper_transcribe.pipeline.convert_to_wav", side_effect=lambda p: p):
                    with patch("wisper_transcribe.pipeline.get_duration", return_value=10.0):
                        with patch("wisper_transcribe.pipeline.transcribe", return_value=FAKE_SEGMENTS):
                            successes, errors = process_folder(
                                tmp_path,
                                output_dir=tmp_path,
                                no_diarize=True,
                                device="auto",  # auto → resolves to cuda
                                workers=3,
                            )

    mock_ppe.assert_not_called()
    assert len(successes) == 1
