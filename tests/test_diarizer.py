from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from wisper_transcribe.models import DiarizationSegment

_FAKE_AUDIO = (16000, np.zeros(16000, dtype=np.int16))  # (sample_rate, mono array)
_FAKE_STEREO = (16000, np.zeros((16000, 2), dtype=np.int16))  # stereo: (time, channels)
_FAKE_INT16_FULLSCALE = (16000, np.full(16000, 32767, dtype=np.int16))  # full-scale int16


def _make_turn(start, end):
    turn = MagicMock()
    turn.start = start
    turn.end = end
    return turn


@patch("scipy.io.wavfile.read", return_value=_FAKE_AUDIO)
def test_diarize_returns_segments(mock_read):
    mock_pipeline = MagicMock()
    mock_pipeline.return_value.speaker_diarization.itertracks.return_value = [
        (_make_turn(0.0, 5.0), "A", "SPEAKER_00"),
        (_make_turn(5.0, 10.0), "B", "SPEAKER_01"),
        (_make_turn(10.0, 15.0), "C", "SPEAKER_00"),
    ]

    import wisper_transcribe.diarizer as d
    d._pipeline = mock_pipeline

    result = d.diarize(Path("fake.wav"), hf_token="hf_fake", device="cpu")

    assert len(result) == 3
    assert isinstance(result[0], DiarizationSegment)
    assert result[0].speaker == "SPEAKER_00"
    assert result[0].start == 0.0
    assert result[1].speaker == "SPEAKER_01"
    assert result[2].speaker == "SPEAKER_00"

    # Pipeline receives a waveform dict, not a file path
    audio_arg = mock_pipeline.call_args.args[0]
    assert isinstance(audio_arg, dict)
    assert "waveform" in audio_arg
    assert "sample_rate" in audio_arg

    d._pipeline = None


@patch("scipy.io.wavfile.read", return_value=_FAKE_AUDIO)
def test_diarize_with_num_speakers(mock_read):
    mock_pipeline = MagicMock()
    mock_pipeline.return_value.speaker_diarization.itertracks.return_value = [
        (_make_turn(0.0, 5.0), "A", "SPEAKER_00"),
    ]

    import wisper_transcribe.diarizer as d
    d._pipeline = mock_pipeline

    d.diarize(Path("fake.wav"), hf_token="hf_fake", device="cpu", num_speakers=4)

    _, kwargs = mock_pipeline.call_args
    assert kwargs.get("num_speakers") == 4

    d._pipeline = None


@patch("scipy.io.wavfile.read", return_value=_FAKE_AUDIO)
def test_diarize_with_min_max_speakers(mock_read):
    mock_pipeline = MagicMock()
    mock_pipeline.return_value.speaker_diarization.itertracks.return_value = []

    import wisper_transcribe.diarizer as d
    d._pipeline = mock_pipeline

    d.diarize(Path("fake.wav"), hf_token="hf_fake", device="cpu", min_speakers=2, max_speakers=6)

    _, kwargs = mock_pipeline.call_args
    assert kwargs.get("min_speakers") == 2
    assert kwargs.get("max_speakers") == 6

    d._pipeline = None


@patch("scipy.io.wavfile.read", return_value=_FAKE_STEREO)
def test_diarize_stereo_audio_transposed(mock_read):
    """Stereo WAV (time, channels) is transposed to (channels, time) before passing to pipeline."""
    mock_pipeline = MagicMock()
    mock_pipeline.return_value.speaker_diarization.itertracks.return_value = []

    import wisper_transcribe.diarizer as d
    d._pipeline = mock_pipeline

    d.diarize(Path("fake.wav"), hf_token="hf_fake", device="cpu")

    waveform = mock_pipeline.call_args.args[0]["waveform"]
    assert waveform.shape == (2, 16000)  # (channels, time) after transpose

    d._pipeline = None


@patch("scipy.io.wavfile.read", return_value=_FAKE_INT16_FULLSCALE)
def test_diarize_int16_audio_normalized_to_float32(mock_read):
    """Integer audio is normalized to float32 in [-1.0, 1.0]."""
    import torch
    mock_pipeline = MagicMock()
    mock_pipeline.return_value.speaker_diarization.itertracks.return_value = []

    import wisper_transcribe.diarizer as d
    d._pipeline = mock_pipeline

    d.diarize(Path("fake.wav"), hf_token="hf_fake", device="cpu")

    waveform = mock_pipeline.call_args.args[0]["waveform"]
    assert waveform.dtype == torch.float32
    assert waveform.max().item() <= 1.0

    d._pipeline = None


@patch("scipy.io.wavfile.read", return_value=_FAKE_AUDIO)
def test_load_pipeline_called_when_none(mock_read):
    import wisper_transcribe.diarizer as d
    d._pipeline = None

    mock_pipeline_instance = MagicMock()
    mock_pipeline_instance.return_value.speaker_diarization.itertracks.return_value = []

    with patch("wisper_transcribe.diarizer.Pipeline") as mock_cls:
        mock_cls.from_pretrained.return_value = mock_pipeline_instance
        d.diarize(Path("fake.wav"), hf_token="hf_abc", device="cpu")
        mock_cls.from_pretrained.assert_called_once()

    d._pipeline = None
