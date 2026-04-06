from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wisper_transcribe.models import TranscriptionSegment


def _make_mock_segment(start, end, text):
    seg = MagicMock()
    seg.start = start
    seg.end = end
    seg.text = f" {text} "  # whisper typically pads with spaces
    return seg


def _make_mock_info(duration: float = 10.0):
    info = MagicMock()
    info.duration = duration
    return info


@patch("wisper_transcribe.transcriber._model", None)
@patch("wisper_transcribe.transcriber.WhisperModel", create=True)
def test_transcribe_returns_segments(mock_whisper_cls):
    mock_model = MagicMock()
    mock_whisper_cls.return_value = mock_model

    raw_segments = [
        _make_mock_segment(0.0, 3.0, "Hello world"),
        _make_mock_segment(3.0, 6.0, "This is a test"),
    ]
    mock_model.transcribe.return_value = (iter(raw_segments), _make_mock_info(6.0))

    with patch("wisper_transcribe.transcriber.WhisperModel", mock_whisper_cls):
        import wisper_transcribe.transcriber as t
        t._model = None

        with patch("faster_whisper.WhisperModel", mock_whisper_cls):
            t._model = mock_model
            result = t.transcribe(Path("fake.wav"), model_size="tiny", device="cpu")

    assert len(result) == 2
    assert isinstance(result[0], TranscriptionSegment)
    assert result[0].text == "Hello world"
    assert result[0].start == 0.0
    assert result[1].text == "This is a test"


@patch("wisper_transcribe.transcriber._model", None)
def test_transcribe_filters_empty_segments():
    mock_model = MagicMock()
    raw_segments = [
        _make_mock_segment(0.0, 1.0, "Real text"),
        _make_mock_segment(1.0, 2.0, "   "),  # whitespace only
        _make_mock_segment(2.0, 3.0, "More text"),
    ]
    mock_model.transcribe.return_value = (iter(raw_segments), _make_mock_info(3.0))

    import wisper_transcribe.transcriber as t
    t._model = mock_model

    result = t.transcribe(Path("fake.wav"), device="cpu")
    texts = [r.text for r in result]
    assert "Real text" in texts
    assert "More text" in texts
    assert "" not in texts
    assert len(result) == 2
