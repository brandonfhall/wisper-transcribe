from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import torch
from transformers.models.qwen3_asr.processing_qwen3_asr import Qwen3ASRProcessor

import wisper_transcribe.word_alignment as wa
from wisper_transcribe.models import TranscriptionSegment, Word


def _split(text, language=None):
    # The real splitter (it doesn't touch self), so mapping tests exercise the
    # actual punctuation/hyphen/digit/CJK rules.
    return Qwen3ASRProcessor.split_words_for_alignment(None, text, language)


class FakeProcessor:
    """Stands in for Qwen3ASRProcessor. Each item j in a crop is placed at
    0.1 + 0.5*j .. 0.4 + 0.5*j seconds, relative to the crop."""

    def __init__(self, fail=None, split_override=None, times=None):
        self.fail = fail                  # callable(batch_len) -> exception or None
        self.split_override = split_override
        self.times = times                # callable(crop_idx_in_batch, j) -> (start, end)
        self.batches = []
        self.languages = []

    def split_words_for_alignment(self, text, language=None):
        return _split(text, language)

    def prepare_forced_aligner_inputs(self, audio, transcript, language=None):
        self.batches.append(len(audio))
        self.languages.append(language)
        if self.fail is not None:
            exc = self.fail(len(audio))
            if exc is not None:
                raise exc
        split = self.split_override or _split
        word_lists = [split(t, language) for t in transcript]
        n = len(audio)
        inputs = {"input_ids": torch.zeros(n, 1, dtype=torch.long), "input_features": torch.zeros(n, 1)}
        return inputs, word_lists

    def decode_forced_alignment(self, logits, input_ids, word_lists, timestamp_token_id):
        out = []
        for b, wl in enumerate(word_lists):
            items = []
            for j, w in enumerate(wl):
                s, e = self.times(b, j) if self.times else (0.1 + 0.5 * j, 0.4 + 0.5 * j)
                items.append({"text": w, "start_time": s, "end_time": e})
            out.append(items)
        return out


class FakeModel:
    dtype = torch.float32
    config = SimpleNamespace(timestamp_token_id=1)

    def __call__(self, **kwargs):
        return SimpleNamespace(logits=torch.zeros(1))


@pytest.fixture
def audio():
    """60 s of 16 kHz silence, as load_wav_as_tensor returns it."""
    with patch("wisper_transcribe.audio_utils.load_wav_as_tensor") as m:
        m.return_value = {"waveform": torch.zeros(1, 16000 * 60), "sample_rate": 16000}
        yield m


@pytest.fixture
def aligner():
    """Install a fake cached model; reset the module globals afterwards."""
    proc = FakeProcessor()

    def install(p=None):
        wa._fa_processor = p or proc
        wa._fa_model = FakeModel()
        wa._fa_device = "cpu"
        return wa._fa_processor

    install()
    yield install
    wa._fa_model = None
    wa._fa_processor = None
    wa._fa_device = None


def _seg(start, end, *words):
    """words: (text, start, end) tuples."""
    ws = [Word(s, e, t) for t, s, e in words]
    return TranscriptionSegment(start, end, " ".join(w.text for w in ws), ws)


def _times(seg):
    return [(round(w.start, 3), round(w.end, 3)) for w in seg.words]


# ---------------------------------------------------------------------------
# Mapping items back to Whisper words
# ---------------------------------------------------------------------------


def test_words_retimed_and_offset_by_crop_start(audio, aligner):
    seg = _seg(10.0, 12.0, ("Hello,", 10.5, 10.9), ("world.", 11.0, 11.8))
    out, stats = wa.align_words(Path("x.wav"), [seg], "cpu", "en")
    assert _times(out[0]) == [(10.1, 10.4), (10.6, 10.9)]
    assert [w.text for w in out[0].words] == ["Hello,", "world."]
    assert stats.words_retimed == 2 and stats.words_kept == 0


def test_hyphen_digits_contraction_each_map_to_one_item(audio, aligner):
    seg = _seg(0.0, 5.0, ("well-known", 0, 1), ("3.5", 1, 2), ("don't", 2, 3), ("stop", 3, 4))
    out, _ = wa.align_words(Path("x.wav"), [seg], "cpu", "en")
    assert _times(out[0]) == [(0.1, 0.4), (0.6, 0.9), (1.1, 1.4), (1.6, 1.9)]


def test_multi_token_word_spans_first_to_last_item(audio, aligner):
    # CJK characters split individually: "你好" is two items.
    seg = _seg(0.0, 5.0, ("你好", 0, 1), ("world", 1, 2))
    out, stats = wa.align_words(Path("x.wav"), [seg], "cpu", None)
    assert _times(out[0]) == [(0.1, 0.9), (1.1, 1.4)]
    assert stats.words_retimed == 2


def test_punctuation_only_word_keeps_whisper_times_clamped(audio, aligner):
    # "—" has no alignable characters; its Whisper times (0.0-3.0) straddle
    # both re-timed neighbours, so it's clamped between them.
    seg = _seg(0.0, 5.0, ("one", 0.0, 0.2), ("—", 0.0, 3.0), ("two", 3.0, 3.2))
    out, stats = wa.align_words(Path("x.wav"), [seg], "cpu", "en")
    one, dash, two = out[0].words
    assert (one.start, one.end) == (0.1, 0.4)
    assert (two.start, two.end) == (0.6, 0.9)
    assert one.end <= dash.start <= dash.end <= two.start
    assert stats.words_retimed == 2 and stats.words_kept == 1


def test_word_list_mismatch_keeps_whisper_times(audio, aligner):
    aligner(FakeProcessor(split_override=lambda t, lang: t.split() + ["extra"]))
    seg = _seg(0.0, 5.0, ("hello", 1.0, 1.5), ("there", 1.5, 2.0))
    out, stats = wa.align_words(Path("x.wav"), [seg], "cpu", "en")
    assert _times(out[0]) == [(1.0, 1.5), (1.5, 2.0)]
    assert stats.words_kept == 2


def test_input_segments_not_mutated(audio, aligner):
    seg = _seg(10.0, 12.0, ("hi", 10.5, 10.9))
    wa.align_words(Path("x.wav"), [seg], "cpu", "en")
    assert (seg.words[0].start, seg.words[0].end) == (10.5, 10.9)


def test_segment_without_words_passes_through(audio, aligner):
    seg = TranscriptionSegment(0.0, 1.0, "hm", None)
    out, _ = wa.align_words(Path("x.wav"), [seg, _seg(1.0, 2.0, ("ok", 1.2, 1.4))], "cpu", "en")
    assert out[0].words is None
    assert _times(out[1]) == [(1.1, 1.4)]


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_cross_segment_first_word_clamped_to_previous_end(audio, aligner):
    # Segment A's last word ends at 5.0 + 0.4 = 5.4; B starts at 5.2, so its
    # first item (5.3) is clamped to 5.4.
    a = _seg(5.0, 5.5, ("a", 5.0, 5.4))
    b = _seg(5.2, 6.0, ("b", 5.5, 5.9))
    out, _ = wa.align_words(Path("x.wav"), [a, b], "cpu", "en")
    assert out[1].words[0].start == pytest.approx(5.4)
    words = [w for s in out for w in s.words]
    assert all(words[i].start <= words[i + 1].start for i in range(len(words) - 1))


def test_results_follow_segment_order_despite_length_sorting(audio, aligner):
    long_first = _seg(0.0, 20.0, ("long", 1, 2))
    short_second = _seg(20.0, 21.0, ("short", 20.2, 20.5))
    out, _ = wa.align_words(Path("x.wav"), [long_first, short_second], "cpu", "en")
    assert _times(out[0]) == [(0.1, 0.4)]
    assert _times(out[1]) == [(20.1, 20.4)]


# ---------------------------------------------------------------------------
# Failure handling: alignment never fails the job
# ---------------------------------------------------------------------------


def test_batch_exception_keeps_whisper_times(audio, aligner):
    aligner(FakeProcessor(fail=lambda n: ValueError("boom")))
    seg = _seg(0.0, 5.0, ("hello", 1.0, 1.5))
    out, stats = wa.align_words(Path("x.wav"), [seg], "cpu", "en")
    assert _times(out[0]) == [(1.0, 1.5)]
    assert stats.words_kept == 1 and stats.words_retimed == 0


def test_oom_halves_batch_and_retries(audio, aligner):
    proc = aligner(FakeProcessor(
        fail=lambda n: RuntimeError("MPS backend out of memory") if n > 1 else None
    ))
    segs = [_seg(i, i + 1.0, (f"w{i}", i + 0.2, i + 0.5)) for i in range(4)]
    with patch("wisper_transcribe.word_alignment._free_cache"):
        out, stats = wa.align_words(Path("x.wav"), segs, "cpu", "en")
    assert stats.words_retimed == 4
    assert proc.batches[0] == 4 and proc.batches[-1] == 1


def test_oom_at_batch_of_one_keeps_whisper_times(audio, aligner):
    aligner(FakeProcessor(fail=lambda n: torch.OutOfMemoryError("oom")))
    seg = _seg(0.0, 5.0, ("hello", 1.0, 1.5))
    with patch("wisper_transcribe.word_alignment._free_cache"):
        out, stats = wa.align_words(Path("x.wav"), [seg], "cpu", "en")
    assert _times(out[0]) == [(1.0, 1.5)]


def test_cancellation_propagates(audio, aligner):
    aligner(FakeProcessor(fail=lambda n: InterruptedError("Job cancelled by user")))
    with pytest.raises(InterruptedError):
        wa.align_words(Path("x.wav"), [_seg(0.0, 5.0, ("hi", 1.0, 1.5))], "cpu", "en")


def test_loader_failure_keeps_whisper_times(audio):
    wa._fa_model = None
    with patch("wisper_transcribe.word_alignment.load_aligner", side_effect=OSError("offline")):
        out, stats = wa.align_words(Path("x.wav"), [_seg(0.0, 5.0, ("hi", 1.0, 1.5))], "cpu", "en")
    assert _times(out[0]) == [(1.0, 1.5)]
    assert stats.words_kept == 1


def test_overlong_crop_skipped(audio, aligner):
    seg = _seg(0.0, 181.0, ("hi", 1.0, 1.5))
    out, stats = wa.align_words(Path("x.wav"), [seg], "cpu", "en")
    assert _times(out[0]) == [(1.0, 1.5)]
    assert stats.words_kept == 1


def test_wrong_sample_rate_skips(aligner):
    with patch("wisper_transcribe.audio_utils.load_wav_as_tensor",
               return_value={"waveform": torch.zeros(1, 44100), "sample_rate": 44100}):
        out, stats = wa.align_words(Path("x.wav"), [_seg(0.0, 1.0, ("hi", 0.2, 0.5))], "cpu", "en")
    assert _times(out[0]) == [(0.2, 0.5)]


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("setting,expected", [
    (None, (True, None)), ("", (True, None)), ("auto", (True, None)),
    ("en", (True, "English")), ("English", (True, "English")), ("ES", (True, "Spanish")),
    ("yue", (True, "Cantonese")), ("nl", (False, None)),
])
def test_resolve_language(setting, expected):
    assert wa._resolve_language(setting) == expected


def test_unsupported_language_skips_without_loading(audio):
    wa._fa_model = None
    with patch("wisper_transcribe.word_alignment.load_aligner") as load:
        out, stats = wa.align_words(Path("x.wav"), [_seg(0.0, 5.0, ("hallo", 1.0, 1.5))], "cpu", "nl")
    load.assert_not_called()
    assert _times(out[0]) == [(1.0, 1.5)]


def test_auto_language_passes_none(audio, aligner):
    proc = aligner(FakeProcessor())
    wa.align_words(Path("x.wav"), [_seg(0.0, 5.0, ("hi", 1.0, 1.5))], "cpu", "auto")
    assert proc.languages == [None]


# ---------------------------------------------------------------------------
# Model cache
# ---------------------------------------------------------------------------


def test_load_aligner_caches_and_reloads_on_device_change(audio):
    wa._fa_model = None
    fake_model = MagicMock()
    fake_model.to.return_value.eval.return_value = FakeModel()
    with patch("transformers.AutoProcessor.from_pretrained", return_value=FakeProcessor()) as proc_load, \
         patch("transformers.AutoModelForTokenClassification.from_pretrained", return_value=fake_model) as model_load:
        wa.align_words(Path("x.wav"), [_seg(0.0, 5.0, ("hi", 1.0, 1.5))], "cpu", "en")
        wa.align_words(Path("x.wav"), [_seg(0.0, 5.0, ("hi", 1.0, 1.5))], "cpu", "en")
        assert model_load.call_count == 1
        wa._fa_device = "mps"  # pretend the cached model is elsewhere
        wa.align_words(Path("x.wav"), [_seg(0.0, 5.0, ("hi", 1.0, 1.5))], "cpu", "en")
        assert model_load.call_count == 2
    assert model_load.call_args.kwargs["dtype"] == torch.float32
    fake_model.to.assert_called_with("cpu")
    wa._fa_model = wa._fa_processor = wa._fa_device = None


def test_dtype_per_device():
    assert wa._dtype_for("cuda") == torch.bfloat16
    assert wa._dtype_for("mps") == torch.float16
    assert wa._dtype_for("cpu") == torch.float32


def test_missing_transformers_gives_install_hint(audio, capsys):
    wa._fa_model = None
    err = ModuleNotFoundError("No module named 'transformers'", name="transformers")
    with patch("wisper_transcribe.word_alignment.load_aligner", side_effect=err):
        out, stats = wa.align_words(Path("x.wav"), [_seg(0.0, 5.0, ("hi", 1.0, 1.5))], "cpu", "en")
    assert _times(out[0]) == [(1.0, 1.5)]
    assert "pip install -e ." in capsys.readouterr().out
