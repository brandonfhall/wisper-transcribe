from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import torch

from wisper_transcribe.models import DiarizationSegment


def _make_fake_audio_dict(*, mono: bool = True, dtype=np.int16, value: int = 0):
    """Build a {waveform, sample_rate} dict matching load_wav_as_tensor output."""
    if mono:
        waveform = torch.zeros(1, 16000, dtype=torch.float32)
    else:
        waveform = torch.zeros(2, 16000, dtype=torch.float32)
    return {"waveform": waveform, "sample_rate": 16000}


def _make_turn(start, end):
    turn = MagicMock()
    turn.start = start
    turn.end = end
    return turn


@patch("wisper_transcribe.audio_utils.load_wav_as_tensor")
def test_diarize_returns_segments(mock_load):
    mock_load.return_value = _make_fake_audio_dict()
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


@patch("wisper_transcribe.audio_utils.load_wav_as_tensor")
def test_diarize_with_num_speakers(mock_load):
    mock_load.return_value = _make_fake_audio_dict()
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


@patch("wisper_transcribe.audio_utils.load_wav_as_tensor")
def test_diarize_with_min_max_speakers(mock_load):
    mock_load.return_value = _make_fake_audio_dict()
    mock_pipeline = MagicMock()
    mock_pipeline.return_value.speaker_diarization.itertracks.return_value = []

    import wisper_transcribe.diarizer as d
    d._pipeline = mock_pipeline

    d.diarize(Path("fake.wav"), hf_token="hf_fake", device="cpu", min_speakers=2, max_speakers=6)

    _, kwargs = mock_pipeline.call_args
    assert kwargs.get("min_speakers") == 2
    assert kwargs.get("max_speakers") == 6

    d._pipeline = None


@patch("wisper_transcribe.audio_utils.load_wav_as_tensor")
def test_diarize_stereo_audio_passed_through(mock_load):
    """Stereo waveform from load_wav_as_tensor is passed through to pipeline."""
    audio_dict = _make_fake_audio_dict(mono=False)
    mock_load.return_value = audio_dict
    mock_pipeline = MagicMock()
    mock_pipeline.return_value.speaker_diarization.itertracks.return_value = []

    import wisper_transcribe.diarizer as d
    d._pipeline = mock_pipeline

    d.diarize(Path("fake.wav"), hf_token="hf_fake", device="cpu")

    waveform = mock_pipeline.call_args.args[0]["waveform"]
    assert waveform.shape == (2, 16000)  # (channels, time)

    d._pipeline = None


@patch("wisper_transcribe.audio_utils.load_wav_as_tensor")
def test_load_pipeline_called_when_none(mock_load):
    mock_load.return_value = _make_fake_audio_dict()

    import wisper_transcribe.diarizer as d
    d._pipeline = None

    mock_pipeline_instance = MagicMock()
    mock_pipeline_instance.return_value.speaker_diarization.itertracks.return_value = []

    with patch("wisper_transcribe.diarizer.Pipeline") as mock_cls:
        mock_cls.from_pretrained.return_value = mock_pipeline_instance
        d.diarize(Path("fake.wav"), hf_token="hf_abc", device="cpu")
        mock_cls.from_pretrained.assert_called_once()

    d._pipeline = None
