import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wisper_transcribe.audio_utils import (
    AUDIO_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    VIDEO_EXTENSIONS,
    validate_audio,
)


def test_validate_audio_missing_file():
    with pytest.raises(ValueError, match="not found"):
        validate_audio(Path("/nonexistent/file.mp3"))


def test_validate_audio_unsupported_extension(tmp_path):
    bad_file = tmp_path / "audio.xyz"
    bad_file.write_text("fake")
    with pytest.raises(ValueError, match="Unsupported format"):
        validate_audio(bad_file)


def test_supported_extensions_includes_video(tmp_path):
    for ext in VIDEO_EXTENSIONS:
        f = tmp_path / f"clip{ext}"
        f.write_text("fake")
        validate_audio(f)  # should not raise


def test_audio_and_video_extensions_are_disjoint():
    assert AUDIO_EXTENSIONS.isdisjoint(VIDEO_EXTENSIONS)


def test_validate_audio_supported_extensions(tmp_path):
    for ext in SUPPORTED_EXTENSIONS:
        f = tmp_path / f"audio{ext}"
        f.write_text("fake")
        validate_audio(f)  # should not raise


def test_validate_audio_case_insensitive(tmp_path):
    f = tmp_path / "audio.MP3"
    f.write_text("fake")
    validate_audio(f)  # .MP3 should be accepted


@patch("wisper_transcribe.audio_utils.AudioSegment")
def test_convert_to_wav_already_wav(mock_audio_segment, tmp_path):
    wav_file = tmp_path / "audio.wav"
    wav_file.write_bytes(b"fake wav data")

    mock_audio = MagicMock()
    mock_audio.frame_rate = 16000
    mock_audio.channels = 1
    mock_audio_segment.from_file.return_value = mock_audio

    from wisper_transcribe.audio_utils import convert_to_wav

    result = convert_to_wav(wav_file)
    assert result == wav_file  # returned unchanged


@patch("wisper_transcribe.audio_utils.AudioSegment")
def test_convert_to_wav_converts_mp3(mock_audio_segment, tmp_path):
    mp3_file = tmp_path / "audio.mp3"
    mp3_file.write_bytes(b"fake mp3 data")

    mock_audio = MagicMock()
    mock_audio.frame_rate = 44100
    mock_audio.channels = 2
    converted = MagicMock()
    mock_audio.set_frame_rate.return_value.set_channels.return_value = converted
    mock_audio_segment.from_file.return_value = mock_audio

    from wisper_transcribe.audio_utils import convert_to_wav

    result = convert_to_wav(mp3_file)
    assert result.suffix == ".wav"
    converted.export.assert_called_once()


@patch("wisper_transcribe.audio_utils.AudioSegment")
def test_get_duration(mock_audio_segment):
    mock_audio = MagicMock()
    mock_audio.__len__ = MagicMock(return_value=90000)  # 90 seconds in ms
    mock_audio_segment.from_file.return_value = mock_audio

    from wisper_transcribe.audio_utils import get_duration

    duration = get_duration(Path("fake.mp3"))
    assert duration == 90.0


# ---------------------------------------------------------------------------
# Video extraction (_extract_first_audio_track / convert_to_wav for video)
# ---------------------------------------------------------------------------

def _fake_ffmpeg_success(out_path: str):
    """Return a mock subprocess.CompletedProcess that writes a minimal WAV stub."""
    from unittest.mock import MagicMock
    Path(out_path).write_bytes(b"RIFF" + b"\x00" * 36)  # stub WAV header
    m = MagicMock()
    m.returncode = 0
    return m


def test_convert_to_wav_video_calls_ffmpeg(tmp_path):
    """Video files go through _extract_first_audio_track, not pydub."""
    mp4_file = tmp_path / "session.mp4"
    mp4_file.write_bytes(b"fake mp4")

    from wisper_transcribe.audio_utils import convert_to_wav

    with patch("wisper_transcribe.audio_utils.subprocess.run") as mock_run:
        mock_run.side_effect = lambda cmd, **kw: _fake_ffmpeg_success(cmd[-1])
        result = convert_to_wav(mp4_file)

    assert result.suffix == ".wav"
    cmd = mock_run.call_args[0][0]
    assert "-map" in cmd and "0:a:0" in cmd
    assert "-ac" in cmd and "1" in cmd
    assert "-ar" in cmd and "16000" in cmd
    assert "-vn" in cmd


@pytest.mark.parametrize("ext", [".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".ts", ".mts", ".m2ts"])
def test_convert_to_wav_all_video_extensions(tmp_path, ext):
    """All VIDEO_EXTENSIONS trigger the ffmpeg path."""
    video_file = tmp_path / f"clip{ext}"
    video_file.write_bytes(b"fake video")

    from wisper_transcribe.audio_utils import convert_to_wav

    with patch("wisper_transcribe.audio_utils.subprocess.run") as mock_run:
        mock_run.side_effect = lambda cmd, **kw: _fake_ffmpeg_success(cmd[-1])
        result = convert_to_wav(video_file)

    assert result.suffix == ".wav"


def test_extract_first_audio_track_ffmpeg_failure(tmp_path):
    """Non-zero ffmpeg exit raises ValueError with helpful message."""
    mp4_file = tmp_path / "bad.mp4"
    mp4_file.write_bytes(b"fake")

    from wisper_transcribe.audio_utils import convert_to_wav

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = b"Invalid data found when processing input"

    with patch("wisper_transcribe.audio_utils.subprocess.run", return_value=mock_result):
        with pytest.raises(ValueError, match="audio track"):
            convert_to_wav(mp4_file)


def test_extract_first_audio_track_ffmpeg_not_found(tmp_path):
    """Missing ffmpeg binary raises RuntimeError."""
    mp4_file = tmp_path / "clip.mp4"
    mp4_file.write_bytes(b"fake")

    from wisper_transcribe.audio_utils import convert_to_wav

    with patch("wisper_transcribe.audio_utils.subprocess.run",
               side_effect=FileNotFoundError("ffmpeg not found")):
        with pytest.raises(RuntimeError, match="ffmpeg not found"):
            convert_to_wav(mp4_file)


# ---------------------------------------------------------------------------
# load_wav_as_tensor
# ---------------------------------------------------------------------------

def test_load_wav_as_tensor_mono_int16(tmp_path):
    """Mono int16 WAV is normalised to float32 with shape (1, time)."""
    import numpy as np
    import scipy.io.wavfile as wavfile
    import torch

    from wisper_transcribe.audio_utils import load_wav_as_tensor

    wav = tmp_path / "mono.wav"
    data = np.array([0, 16383, 32767, -32768], dtype=np.int16)
    wavfile.write(str(wav), 16000, data)

    result = load_wav_as_tensor(wav)
    assert "waveform" in result
    assert "sample_rate" in result
    assert result["sample_rate"] == 16000
    assert result["waveform"].dtype == torch.float32
    assert result["waveform"].shape == (1, 4)
    assert result["waveform"].max().item() <= 1.0


def test_load_wav_as_tensor_stereo(tmp_path):
    """Stereo WAV is transposed to (channels, time)."""
    import numpy as np
    import scipy.io.wavfile as wavfile

    from wisper_transcribe.audio_utils import load_wav_as_tensor

    wav = tmp_path / "stereo.wav"
    data = np.zeros((100, 2), dtype=np.int16)
    wavfile.write(str(wav), 16000, data)

    result = load_wav_as_tensor(wav)
    assert result["waveform"].shape == (2, 100)


def test_load_wav_as_tensor_float32_passthrough(tmp_path):
    """Float32 WAV data is not re-normalised."""
    import numpy as np
    import scipy.io.wavfile as wavfile

    from wisper_transcribe.audio_utils import load_wav_as_tensor

    wav = tmp_path / "float.wav"
    data = np.array([0.0, 0.5, -0.5, 1.0], dtype=np.float32)
    wavfile.write(str(wav), 16000, data)

    result = load_wav_as_tensor(wav)
    assert result["waveform"].shape == (1, 4)
    np.testing.assert_array_almost_equal(
        result["waveform"].numpy().flatten(), data, decimal=5
    )
