"""Tests for scripts/live_diarization_eval.py (no audio, models, or network).

Embeddings are deterministic vectors per ground-truth speaker plus noise;
``extract_embedding`` is patched, and diarization segments are synthetic.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from wisper_transcribe.models import DiarizationSegment, SpeakerProfile

_spec = importlib.util.spec_from_file_location(
    "live_diarization_eval",
    Path(__file__).parents[1] / "scripts" / "live_diarization_eval.py",
)
lde = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lde)


def _unit(*vec):
    v = np.asarray(vec, dtype=np.float32)
    return v / np.linalg.norm(v)


def _noisy(base, seed, scale=0.02):
    rng = np.random.default_rng(seed)
    v = (np.asarray(base, dtype=np.float32)
         + rng.standard_normal(len(base)).astype(np.float32) * scale)
    return v / np.linalg.norm(v)


A, B = _unit(1.0, 0.0, 0.0), _unit(0.0, 1.0, 0.0)
# A vector at cosine 0.5 to A, for threshold tests.
COS50 = _unit(0.5, float(np.sqrt(0.75)), 0.0)


def test_utterances_merge_same_speaker_and_split_long_turns():
    segs = [DiarizationSegment(0.0, 3.0, "A"), DiarizationSegment(3.0, 6.0, "A"),
            DiarizationSegment(6.0, 14.0, "B")]
    # Same-speaker turns merge; a long stretch is cut every max_s like the live
    # loop's force-cut.
    assert lde._utterances(segs, 5.0, "turns") == [(0.0, 5.0), (5.0, 6.0), (6.0, 11.0), (11.0, 14.0)]


def test_utterances_sort_out_of_order_turns():
    segs = [DiarizationSegment(6.0, 8.0, "B"), DiarizationSegment(0.0, 2.0, "A")]
    assert lde._utterances(segs, 5.0, "turns") == [(0.0, 2.0), (6.0, 8.0)]


def test_pool_same_speaker_joins_different_speaker_opens_new_voice():
    pool = lde.VoicePool(0.55)
    assert pool.assign(_noisy(A, 1)) == 0
    assert pool.assign(_noisy(A, 2)) == 0   # same speaker joins voice 0
    assert pool.assign(_noisy(B, 3)) == 1   # different speaker opens voice 1
    assert len(pool.centroids) == 2
    assert pool.counts == [2, 1]


def test_pool_threshold_respected():
    strict = lde.VoicePool(0.55)
    strict.assign(A)
    assert strict.assign(COS50) == 1        # 0.5 < 0.55: opens a new voice
    loose = lde.VoicePool(0.4)
    loose.assign(A)
    assert loose.assign(COS50) == 0         # 0.5 >= 0.4: joins


def test_best_mapping_is_one_to_one_and_maximises_seconds():
    # Voice 2 covers only B, so it must take B (7); voice 0 then takes A (9)
    # and voice 1 is left unassigned. Greedy-by-max would instead give voice 0
    # B (its second choice) and lose the total.
    pair = {0: {"A": 9.0, "B": 1.0}, 1: {"A": 8.0}, 2: {"B": 7.0}}
    mapping = lde._best_mapping(pair)
    assert mapping == {0: "A", 2: "B"}
    assert len(set(mapping.values())) == len(mapping)  # one-to-one


def test_evaluate_scores_labels_and_overlap():
    segs = [DiarizationSegment(0.0, 4.0, "A"), DiarizationSegment(4.0, 6.0, "B")]
    windows = lde._utterances(segs, 15.0, "turns")

    def embed(start, end):
        return A if start < 4.0 else B

    report, pool, mapping, labels, _windows = lde._evaluate(windows, segs, embed, 0.55)
    assert len(pool.centroids) == 2
    assert labels == [0, 1]
    assert mapping == {0: "A", 1: "B"}
    assert report["label_accuracy"] == pytest.approx(1.0)
    assert report["overlap_fraction"] == 0.0
    assert report["true_speakers"] == 2


def test_evaluate_counts_overlap_over_20_percent():
    # B is the dominant speaker (3 s) but A still covers 2 s of the 5 s window.
    segs = [DiarizationSegment(0.0, 5.0, "B"), DiarizationSegment(3.0, 5.0, "A")]
    windows = lde._utterances(segs, 15.0, "turns")
    report, *_ = lde._evaluate(windows, segs, lambda s, e: B, 0.55)
    assert report["overlap_fraction"] == pytest.approx(1.0)


def test_overlap_below_20_percent_does_not_count():
    # One 6 s utterance; a second speaker covers 1 s of it (16.7%), under the bar.
    coverage = {"B": [(0.0, 6.0)], "A": [(5.0, 6.0)]}
    assert lde._overlap_fraction([(0.0, 6.0)], coverage) == 0.0


def test_profile_match_maps_voices_to_enrolled_names():
    pool = lde.VoicePool(0.55)
    pool.assign(_unit(1.0, 0.0))
    pool.assign(_unit(0.0, 1.0))
    profiles = {
        "alice": SpeakerProfile("alice", "Alice", "", _unit(1.0, 0.0), "", ""),
        "bob": SpeakerProfile("bob", "Bob", "", _unit(0.0, 1.0), "", ""),
    }
    with patch.object(lde.speaker_manager, "load_profiles", return_value=profiles):
        names, has_profiles = lde._profile_match(pool, 0.55)
    assert has_profiles is True
    assert names == {0: "Alice", 1: "Bob"}


def test_profile_match_without_profiles_reports_none():
    pool = lde.VoicePool(0.55)
    pool.assign(_unit(1.0, 0.0))
    with patch.object(lde.speaker_manager, "load_profiles", return_value={}):
        assert lde._profile_match(pool, 0.55) == ({}, False)


def test_name_accuracy_against_final_speaker_map():
    windows = [(0.0, 4.0), (4.0, 6.0)]
    coverage = lde._coverage_by_speaker(
        [DiarizationSegment(0.0, 4.0, "SPEAKER_00"), DiarizationSegment(4.0, 6.0, "SPEAKER_01")])
    mapping = {0: "SPEAKER_00", 1: "SPEAKER_01"}
    labels = [0, 1]
    # Voice 0's shown name matches the final name; voice 1's does not.
    names = {0: "Alice", 1: "Unknown Speaker 1"}
    speaker_map = {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"}
    assert lde._name_accuracy(windows, coverage, mapping, labels, names, speaker_map) == \
        pytest.approx(4.0 / 6.0)


def test_score_prints_a_table(tmp_path, capsys):
    for i, threshold in enumerate((0.55, 0.65)):
        out = tmp_path / f"run{i}"
        out.mkdir()
        (out / "report.json").write_text(json.dumps({
            "meta": {"threshold": threshold}, "pool_voices": 2, "true_speakers": 2,
            "label_accuracy": 0.9, "overlap_fraction": 0.1,
            "latency_s": {"p95": 0.05},
        }), encoding="utf-8")
    lde.cmd_score(SimpleNamespace(outs=[str(tmp_path / "run0"), str(tmp_path / "run1")]))
    report = capsys.readouterr().out
    assert "0.55" in report and "0.65" in report and "acc" in report


def test_cmd_run_writes_report_json(tmp_path, capsys):
    from wisper_transcribe import transcript_store as ts
    from wisper_transcribe.path_utils import get_output_dir

    out_dir = get_output_dir()
    stem = "session-30m"
    md = out_dir / f"{stem}.md"
    md.write_text("# x\n", encoding="utf-8")
    audio = out_dir / f"{stem}.flac"
    audio.write_bytes(b"fLaC")
    segs = [{"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00"},
            {"start": 4.0, "end": 8.0, "speaker": "SPEAKER_01"}]
    ts.write_sidecar(md, {"diarization_segments": segs, "input_path": str(audio),
                          "speaker_map": {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"}})

    class FakeInference:
        def crop(self, audio_dict, excerpt):
            base = A if excerpt.start < 4.0 else B
            return _noisy(base, int(excerpt.start))

    args = SimpleNamespace(transcript=stem, threshold=0.55, max_utterance_s=None, boundaries="turns",
                           out=str(tmp_path / "run"), device="cpu")
    wav = out_dir / "converted.wav"
    wav.write_bytes(b"RIFF")
    with patch.object(lde.speaker_manager, "_load_embedding_model", return_value=FakeInference()), \
            patch("wisper_transcribe.audio_utils.load_wav_as_tensor", return_value={}), \
            patch("wisper_transcribe.audio_utils.convert_to_wav", return_value=wav), \
            patch.object(lde.speaker_manager, "load_profiles", return_value={}), \
            patch("wisper_transcribe.config.get_device", return_value="cpu"):
        lde.cmd_run(args)

    report = json.loads((tmp_path / "run" / "report.json").read_text(encoding="utf-8"))
    assert report["meta"]["transcript"] == stem
    assert report["meta"]["max_utterance_s"] == lde.DEFAULT_MAX_UTTERANCE_S
    assert report["true_speakers"] == 2
    assert report["pool_voices"] == 2
    assert report["label_accuracy"] == pytest.approx(1.0)
    assert set(report["latency_s"]) == {"mean", "p50", "p95", "max"}
    assert report["profiles_enrolled"] is False


def test_widespread_extraction_failure_stops_the_run():
    """If most utterances fail to embed, the report would describe the
    failures, not the voices (e.g. FLAC passed where WAV is required): stop."""
    import pytest

    segs = [DiarizationSegment(float(i), float(i) + 1.0, "A") for i in range(40)]
    windows = [(s.start, s.end) for s in segs]

    def broken(start, end):
        raise ValueError("File format b'fLaC' not understood")

    with pytest.raises(ValueError, match="fLaC"):
        lde._evaluate(windows, segs, broken, 0.55)


def test_an_occasional_failure_is_skipped_and_counted():
    segs = [DiarizationSegment(float(i), float(i) + 1.0, "A") for i in range(40)]
    windows = [(s.start, s.end) for s in segs]
    vec = np.ones(4, dtype=np.float32) / 2.0

    def flaky(start, end):
        if start == 7.0:
            raise ValueError("too short")
        return vec

    report, pool, mapping, labels, kept = lde._evaluate(windows, segs, flaky, 0.55)
    assert report["meta"]["failed_utterances"] == 1
    assert len(kept) == len(labels) == 39
    assert report["pool_voices"] == 1


def test_too_short_utterances_are_counted_not_failed():
    segs = [DiarizationSegment(float(i), float(i) + 1.0, "A") for i in range(40)]
    windows = [(s.start, s.end) for s in segs]
    vec = np.ones(4, dtype=np.float32) / 2.0

    def embed(start, end):
        if start < 20:
            raise lde.TooShort
        return vec

    report, pool, mapping, labels, kept = lde._evaluate(windows, segs, embed, 0.55)
    assert report["meta"]["too_short_utterances"] == 20
    assert report["meta"]["failed_utterances"] == 0
    assert abs(report["meta"]["too_short_seconds_fraction"] - 0.5) < 1e-6
    assert len(kept) == 20


def test_pause_boundaries_merge_back_to_back_speakers_like_the_live_loop():
    segs = [DiarizationSegment(0.0, 2.0, "A"), DiarizationSegment(2.2, 4.0, "B"),
            DiarizationSegment(5.0, 6.0, "A")]
    assert lde._utterances(segs, 15.0, "pauses") == [(0.0, 4.0), (5.0, 6.0)]
    assert lde._utterances(segs, 15.0, "turns") == [(0.0, 2.0), (2.2, 4.0), (5.0, 6.0)]


def test_utterances_force_cut_and_drop_sub_commit_scraps():
    segs = [DiarizationSegment(0.0, 31.0, "A"), DiarizationSegment(40.0, 40.1, "B")]
    windows = lde._utterances(segs, 15.0, "pauses")
    assert windows == [(0.0, 15.0), (15.0, 30.0), (30.0, 31.0)]


def test_name_accuracy_scores_a_two_speaker_utterance_by_who_speaks():
    """An utterance half A, half B showing A's name is half right, not all right."""
    segs = [DiarizationSegment(0.0, 2.0, "A"), DiarizationSegment(2.0, 4.0, "B")]
    coverage = lde._coverage_by_speaker(segs)
    acc = lde._name_accuracy([(0.0, 4.0)], coverage, {0: "A"}, [0], {0: "Alice"},
                             {"A": "Alice", "B": "Bob"})
    assert abs(acc - 0.5) < 1e-9
