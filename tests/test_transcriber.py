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


def test_load_model_passes_compute_type():
    """load_model resolves 'auto' and forwards the concrete value to WhisperModel."""
    with patch("faster_whisper.WhisperModel") as mock_cls:
        mock_cls.return_value = MagicMock()
        import wisper_transcribe.transcriber as t
        t._model = None
        t.load_model("tiny", "cpu", compute_type="int8")
        _, kwargs = mock_cls.call_args
        assert kwargs.get("compute_type") == "int8"
        t._model = None


def test_load_model_resolves_auto_cpu():
    """compute_type='auto' on CPU resolves to 'int8'."""
    with patch("faster_whisper.WhisperModel") as mock_cls:
        mock_cls.return_value = MagicMock()
        import wisper_transcribe.transcriber as t
        t._model = None
        t.load_model("tiny", "cpu", compute_type="auto")
        _, kwargs = mock_cls.call_args
        assert kwargs.get("compute_type") == "int8"
        t._model = None


def test_transcribe_passes_vad_filter_true():
    """vad_filter=True is forwarded to model.transcribe()."""
    mock_model = MagicMock()
    mock_model.transcribe.return_value = (iter([]), _make_mock_info(1.0))

    import wisper_transcribe.transcriber as t
    t._model = mock_model

    t.transcribe(Path("fake.wav"), device="cpu", vad_filter=True)
    _, kwargs = mock_model.transcribe.call_args
    assert kwargs.get("vad_filter") is True


def test_transcribe_passes_vad_filter_false():
    """vad_filter=False is forwarded to model.transcribe()."""
    mock_model = MagicMock()
    mock_model.transcribe.return_value = (iter([]), _make_mock_info(1.0))

    import wisper_transcribe.transcriber as t
    t._model = mock_model

    t.transcribe(Path("fake.wav"), device="cpu", vad_filter=False)
    _, kwargs = mock_model.transcribe.call_args
    assert kwargs.get("vad_filter") is False


# ---------------------------------------------------------------------------
# MLX-Whisper backend tests
# ---------------------------------------------------------------------------

def test_is_mlx_available_false_on_non_darwin():
    """_is_mlx_available() always returns False on non-macOS platforms."""
    import wisper_transcribe.transcriber as t
    with patch("wisper_transcribe.transcriber.platform.system", return_value="Linux"):
        assert t._is_mlx_available() is False


def test_is_mlx_available_false_when_package_not_found():
    """_is_mlx_available() returns False when mlx_whisper is not installed."""
    import wisper_transcribe.transcriber as t
    with patch("wisper_transcribe.transcriber.platform.system", return_value="Darwin"):
        with patch("importlib.util.find_spec", return_value=None):
            assert t._is_mlx_available() is False


def test_transcribe_dispatches_to_mlx_on_mps():
    """transcribe() routes to _transcribe_mlx when device=mps and mlx is available."""
    import wisper_transcribe.transcriber as t

    fake_result = [TranscriptionSegment(0.0, 3.0, "Hello")]
    with patch.object(t, "_is_mlx_available", return_value=True):
        with patch.object(t, "_transcribe_mlx", return_value=fake_result) as mock_mlx:
            result = t.transcribe(Path("fake.wav"), device="mps", use_mlx="auto")

    mock_mlx.assert_called_once()
    assert result == fake_result


def test_transcribe_skips_mlx_when_use_mlx_false():
    """use_mlx='false' bypasses MLX even on MPS — uses faster-whisper CPU path."""
    import wisper_transcribe.transcriber as t

    mock_model = MagicMock()
    mock_model.transcribe.return_value = (iter([]), _make_mock_info(1.0))
    t._model = mock_model

    with patch.object(t, "_is_mlx_available", return_value=True):
        with patch.object(t, "_transcribe_mlx") as mock_mlx:
            t.transcribe(Path("fake.wav"), device="mps", use_mlx="false")

    mock_mlx.assert_not_called()
    mock_model.transcribe.assert_called_once()


def test_transcribe_falls_back_to_cpu_when_mlx_unavailable():
    """When MLX is not installed, MPS device falls back to faster-whisper on CPU."""
    import wisper_transcribe.transcriber as t

    mock_model = MagicMock()
    mock_model.transcribe.return_value = (iter([]), _make_mock_info(1.0))
    t._model = mock_model

    with patch.object(t, "_is_mlx_available", return_value=False):
        with patch.object(t, "_transcribe_mlx") as mock_mlx:
            t.transcribe(Path("fake.wav"), device="mps", use_mlx="auto")

    mock_mlx.assert_not_called()
    mock_model.transcribe.assert_called_once()


def test_transcribe_mlx_required_raises_when_unavailable():
    """use_mlx='true' raises RuntimeError when mlx-whisper is not installed."""
    import wisper_transcribe.transcriber as t

    with patch.object(t, "_is_mlx_available", return_value=False):
        with pytest.raises(RuntimeError, match="mlx-whisper is not installed"):
            t.transcribe(Path("fake.wav"), device="mps", use_mlx="true")


def test_transcribe_mlx_segment_mapping():
    """_transcribe_mlx maps mlx_whisper output dict to TranscriptionSegment objects."""
    import wisper_transcribe.transcriber as t

    mlx_output = {
        "segments": [
            {"start": 0.0, "end": 3.5, "text": " Hello world "},
            {"start": 3.5, "end": 7.0, "text": "  "},          # whitespace — filtered out
            {"start": 7.0, "end": 10.0, "text": " Roll for initiative "},
        ]
    }

    mock_mlx = MagicMock()
    mock_mlx.transcribe.return_value = mlx_output

    with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
        with patch("wisper_transcribe.transcriber.platform.system", return_value="Darwin"):
            result = t._transcribe_mlx(Path("fake.wav"), model_size="medium")

    assert len(result) == 2
    assert result[0].start == 0.0
    assert result[0].end == 3.5
    assert result[0].text == "Hello world"
    assert result[1].text == "Roll for initiative"


def test_transcribe_mlx_injects_hotwords_into_prompt():
    """_transcribe_mlx injects hotwords as initial_prompt prefix when no prompt given."""
    import wisper_transcribe.transcriber as t

    mlx_output = {"segments": []}
    mock_mlx = MagicMock()
    mock_mlx.transcribe.return_value = mlx_output

    with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
        with patch("wisper_transcribe.transcriber.platform.system", return_value="Darwin"):
            t._transcribe_mlx(
                Path("fake.wav"),
                model_size="medium",
                hotwords=["Kyra", "Golarion"],
            )

    _, kwargs = mock_mlx.transcribe.call_args
    assert "Kyra" in kwargs.get("initial_prompt", "")
    assert "Golarion" in kwargs.get("initial_prompt", "")


def test_transcribe_mlx_not_dispatched_on_non_mps():
    """MLX is never used when device is cpu or cuda, regardless of use_mlx."""
    import wisper_transcribe.transcriber as t

    mock_model = MagicMock()
    mock_model.transcribe.return_value = (iter([]), _make_mock_info(1.0))
    t._model = mock_model

    with patch.object(t, "_is_mlx_available", return_value=True):
        with patch.object(t, "_transcribe_mlx") as mock_mlx:
            t.transcribe(Path("fake.wav"), device="cpu", use_mlx="auto")

    mock_mlx.assert_not_called()
