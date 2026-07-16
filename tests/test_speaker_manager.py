from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from wisper_transcribe.models import DiarizationSegment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_profile(data_dir: Path, name: str, embedding: np.ndarray, role: str = "Player") -> None:
    """Write a speaker profile and embedding to data_dir for testing."""
    profiles_dir = data_dir / "profiles"
    emb_dir = profiles_dir / "embeddings"
    emb_dir.mkdir(parents=True, exist_ok=True)

    np.save(str(emb_dir / f"{name}.npy"), embedding)

    speakers_json = profiles_dir / "speakers.json"
    existing = json.loads(speakers_json.read_text()) if speakers_json.exists() else {}
    existing[name] = {
        "display_name": name.capitalize(),
        "role": role,
        "embedding_file": f"embeddings/{name}.npy",
        "enrolled_date": "2026-04-05",
        "enrollment_source": "session01.mp3",
        "notes": "",
    }
    speakers_json.write_text(json.dumps(existing, indent=2))


def _fake_diarization(labels: list[str]) -> list[DiarizationSegment]:
    segs = []
    for i, label in enumerate(labels):
        segs.append(DiarizationSegment(start=float(i * 10), end=float(i * 10 + 9), speaker=label))
    return segs


# ---------------------------------------------------------------------------
# load_profiles / save_profiles
# ---------------------------------------------------------------------------

def test_load_profiles_empty(tmp_path):
    from wisper_transcribe.speaker_manager import load_profiles
    result = load_profiles(data_dir=tmp_path)
    assert result == {}


def test_save_and_load_profiles(tmp_path):
    from wisper_transcribe.models import SpeakerProfile
    from wisper_transcribe.speaker_manager import load_profiles, save_profiles

    emb_dir = tmp_path / "profiles" / "embeddings"
    emb_dir.mkdir(parents=True)
    emb_path = emb_dir / "alice.npy"
    np.save(str(emb_path), np.ones(512))

    profiles = {
        "alice": SpeakerProfile(
            name="alice",
            display_name="Alice",
            role="DM",
            embedding_path=emb_path,
            enrolled_date="2026-04-05",
            enrollment_source="session01.mp3",
            notes="Game Master",
        )
    }
    save_profiles(profiles, data_dir=tmp_path)
    loaded = load_profiles(data_dir=tmp_path)

    assert "alice" in loaded
    assert loaded["alice"].display_name == "Alice"
    assert loaded["alice"].role == "DM"
    assert loaded["alice"].notes == "Game Master"


# ---------------------------------------------------------------------------
# cosine_similarity (internal, tested via match_speakers)
# ---------------------------------------------------------------------------

def test_cosine_similarity_identical():
    from wisper_transcribe.speaker_manager import _cosine_similarity
    v = np.array([1.0, 0.0, 0.0])
    assert _cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal():
    from wisper_transcribe.speaker_manager import _cosine_similarity
    a = np.array([1.0, 0.0])
    b = np.array([0.0, 1.0])
    assert _cosine_similarity(a, b) == pytest.approx(0.0)


def test_cosine_similarity_zero_vector():
    from wisper_transcribe.speaker_manager import _cosine_similarity
    a = np.zeros(3)
    b = np.array([1.0, 0.0, 0.0])
    assert _cosine_similarity(a, b) == 0.0


# ---------------------------------------------------------------------------
# match_speakers
# ---------------------------------------------------------------------------

def test_match_speakers_no_profiles(tmp_path):
    from wisper_transcribe.speaker_manager import match_speakers
    segs = _fake_diarization(["SPEAKER_00"])
    result = match_speakers(Path("fake.wav"), segs, data_dir=tmp_path, device="cpu")
    assert result == {}


def test_match_speakers_above_threshold(tmp_path):
    from wisper_transcribe.speaker_manager import match_speakers

    alice_emb = np.array([1.0, 0.0, 0.0])
    _write_profile(tmp_path, "alice", alice_emb)

    segs = _fake_diarization(["SPEAKER_00"])

    # Mock extract_embedding to return alice's embedding for SPEAKER_00
    with patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=alice_emb):
        result = match_speakers(Path("fake.wav"), segs, data_dir=tmp_path, threshold=0.65)

    assert result["SPEAKER_00"] == "Alice"


def test_match_speakers_below_threshold_becomes_unknown(tmp_path):
    from wisper_transcribe.speaker_manager import match_speakers

    alice_emb = np.array([1.0, 0.0, 0.0])
    _write_profile(tmp_path, "alice", alice_emb)

    segs = _fake_diarization(["SPEAKER_00"])
    # Return an orthogonal embedding — similarity = 0.0, below any threshold
    query_emb = np.array([0.0, 1.0, 0.0])

    with patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=query_emb):
        result = match_speakers(Path("fake.wav"), segs, data_dir=tmp_path, threshold=0.65)

    assert result["SPEAKER_00"] == "Unknown Speaker 1"


def test_match_speakers_greedy_no_double_assign(tmp_path):
    """Two speakers should not both be assigned to the same profile.

    With the pair-scored algorithm, ties are broken deterministically by
    label order — SPEAKER_00 wins the shared profile, SPEAKER_01 falls back
    to Unknown since there's no other profile to claim (allow_many_to_one
    defaults to False).
    """
    from wisper_transcribe.speaker_manager import match_speakers

    alice_emb = np.array([1.0, 0.0, 0.0])
    _write_profile(tmp_path, "alice", alice_emb)

    segs = _fake_diarization(["SPEAKER_00", "SPEAKER_01"])

    call_count = [0]
    def fake_extract(audio_path, segments, label, device="cpu"):
        call_count[0] += 1
        return alice_emb  # Both speakers look like Alice

    with patch("wisper_transcribe.speaker_manager.extract_embedding", side_effect=fake_extract):
        result = match_speakers(Path("fake.wav"), segs, data_dir=tmp_path, threshold=0.65)

    assert result["SPEAKER_00"] == "Alice"
    assert result["SPEAKER_01"] == "Unknown Speaker 1"


def test_match_speakers_next_best_fallback(tmp_path):
    """Label B's best profile is claimed by label A (higher score); B should
    fall back to its own second-best profile above threshold rather than
    going to Unknown."""
    from wisper_transcribe.speaker_manager import match_speakers

    p1_emb = np.array([1.0, 0.0, 0.0])
    p2_emb = np.array([0.0, 1.0, 0.0])
    _write_profile(tmp_path, "p1", p1_emb)
    _write_profile(tmp_path, "p2", p2_emb)

    segs = _fake_diarization(["SPEAKER_00", "SPEAKER_01"])

    # Label A sits mostly along p1 (sim=0.9) — the strongest claim on p1.
    a_emb = np.array([0.9, 0.0, np.sqrt(1 - 0.9 ** 2)])
    # Label B scores 0.70 vs p1 (its nominal best, but loses to A's
    # stronger 0.9) and 0.68 vs p2 (its fallback, still above the 0.65
    # threshold). The z-component absorbs the remaining norm so both
    # similarities are simultaneously achievable on a unit vector.
    b_p1_sim, b_p2_sim = 0.70, 0.68
    b_emb = np.array([b_p1_sim, b_p2_sim, np.sqrt(1 - b_p1_sim ** 2 - b_p2_sim ** 2)])

    def fake_extract(audio_path, segments, label, device="cpu"):
        return {"SPEAKER_00": a_emb, "SPEAKER_01": b_emb}[label]

    with patch("wisper_transcribe.speaker_manager.extract_embedding", side_effect=fake_extract):
        result = match_speakers(Path("fake.wav"), segs, data_dir=tmp_path, threshold=0.65)

    assert result["SPEAKER_00"] == "P1"
    assert result["SPEAKER_01"] == "P2"


def test_match_speakers_many_to_one_disabled_by_default(tmp_path):
    """Two labels both best-match the same profile; with allow_many_to_one
    False (default) the loser goes Unknown when no other profile clears
    threshold."""
    from wisper_transcribe.speaker_manager import match_speakers

    alice_emb = np.array([1.0, 0.0, 0.0])
    _write_profile(tmp_path, "alice", alice_emb)

    segs = _fake_diarization(["SPEAKER_00", "SPEAKER_01"])

    def fake_extract(audio_path, segments, label, device="cpu"):
        return alice_emb

    with patch("wisper_transcribe.speaker_manager.extract_embedding", side_effect=fake_extract):
        result = match_speakers(
            Path("fake.wav"), segs, data_dir=tmp_path, threshold=0.65,
            allow_many_to_one=False,
        )

    assert result["SPEAKER_00"] == "Alice"
    assert result["SPEAKER_01"] == "Unknown Speaker 1"


def test_match_speakers_many_to_one_enabled(tmp_path):
    """With allow_many_to_one True, both labels that best-match the same
    profile above threshold get its display name."""
    from wisper_transcribe.speaker_manager import match_speakers

    alice_emb = np.array([1.0, 0.0, 0.0])
    _write_profile(tmp_path, "alice", alice_emb)

    segs = _fake_diarization(["SPEAKER_00", "SPEAKER_01"])

    def fake_extract(audio_path, segments, label, device="cpu"):
        return alice_emb

    with patch("wisper_transcribe.speaker_manager.extract_embedding", side_effect=fake_extract):
        result = match_speakers(
            Path("fake.wav"), segs, data_dir=tmp_path, threshold=0.65,
            allow_many_to_one=True,
        )

    assert result["SPEAKER_00"] == "Alice"
    assert result["SPEAKER_01"] == "Alice"


def test_match_speakers_many_to_one_still_respects_threshold(tmp_path):
    """An unassigned label below threshold stays Unknown even with
    allow_many_to_one=True."""
    from wisper_transcribe.speaker_manager import match_speakers

    alice_emb = np.array([1.0, 0.0, 0.0])
    _write_profile(tmp_path, "alice", alice_emb)

    segs = _fake_diarization(["SPEAKER_00", "SPEAKER_01"])
    # SPEAKER_01 is orthogonal to Alice — similarity 0.0, well below threshold
    orthogonal_emb = np.array([0.0, 1.0, 0.0])

    def fake_extract(audio_path, segments, label, device="cpu"):
        return alice_emb if label == "SPEAKER_00" else orthogonal_emb

    with patch("wisper_transcribe.speaker_manager.extract_embedding", side_effect=fake_extract):
        result = match_speakers(
            Path("fake.wav"), segs, data_dir=tmp_path, threshold=0.65,
            allow_many_to_one=True,
        )

    assert result["SPEAKER_00"] == "Alice"
    assert result["SPEAKER_01"] == "Unknown Speaker 1"


def test_match_speakers_unknown_numbering_deterministic_by_label_order(tmp_path):
    """Unknown numbering follows sorted label order, not similarity order."""
    from wisper_transcribe.speaker_manager import match_speakers

    alice_emb = np.array([1.0, 0.0, 0.0])
    _write_profile(tmp_path, "alice", alice_emb)

    # Three labels, none matching Alice — all become Unknown. SPEAKER_02's
    # embedding extraction happens to be the "closest" of the unmatched ones
    # (still below threshold), which must NOT earn it "Unknown Speaker 1".
    segs = _fake_diarization(["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"])

    def fake_extract(audio_path, segments, label, device="cpu"):
        # All orthogonal to alice_emb but with varying (irrelevant) magnitude
        return {
            "SPEAKER_00": np.array([0.0, 1.0, 0.0]),
            "SPEAKER_01": np.array([0.0, 0.0, 1.0]),
            "SPEAKER_02": np.array([0.0, 2.0, 0.0]),  # still sim 0.0 vs alice
        }[label]

    with patch("wisper_transcribe.speaker_manager.extract_embedding", side_effect=fake_extract):
        result = match_speakers(Path("fake.wav"), segs, data_dir=tmp_path, threshold=0.65)

    assert result["SPEAKER_00"] == "Unknown Speaker 1"
    assert result["SPEAKER_01"] == "Unknown Speaker 2"
    assert result["SPEAKER_02"] == "Unknown Speaker 3"


def test_match_speakers_failed_embedding_stays_unknown(tmp_path):
    """A label whose embedding extraction raises stays Unknown and doesn't
    disturb assignment of the other labels."""
    from wisper_transcribe.speaker_manager import match_speakers

    alice_emb = np.array([1.0, 0.0, 0.0])
    _write_profile(tmp_path, "alice", alice_emb)

    segs = _fake_diarization(["SPEAKER_00", "SPEAKER_01"])

    def fake_extract(audio_path, segments, label, device="cpu"):
        if label == "SPEAKER_00":
            raise RuntimeError("embedding extraction failed")
        return alice_emb

    with patch("wisper_transcribe.speaker_manager.extract_embedding", side_effect=fake_extract):
        result = match_speakers(Path("fake.wav"), segs, data_dir=tmp_path, threshold=0.65)

    assert result["SPEAKER_00"] == "Unknown Speaker 1"
    assert result["SPEAKER_01"] == "Alice"


def test_match_speakers_multiple_profiles(tmp_path):
    from wisper_transcribe.speaker_manager import match_speakers

    alice_emb = np.array([1.0, 0.0, 0.0])
    bob_emb = np.array([0.0, 1.0, 0.0])
    _write_profile(tmp_path, "alice", alice_emb)
    _write_profile(tmp_path, "bob", bob_emb)

    segs = _fake_diarization(["SPEAKER_00", "SPEAKER_01"])

    def fake_extract(audio_path, segments, label, device="cpu"):
        return alice_emb if label == "SPEAKER_00" else bob_emb

    with patch("wisper_transcribe.speaker_manager.extract_embedding", side_effect=fake_extract):
        result = match_speakers(Path("fake.wav"), segs, data_dir=tmp_path, threshold=0.65)

    assert result["SPEAKER_00"] == "Alice"
    assert result["SPEAKER_01"] == "Bob"


# ---------------------------------------------------------------------------
# update_embedding
# ---------------------------------------------------------------------------

def test_update_embedding_creates_new(tmp_path):
    from wisper_transcribe.speaker_manager import update_embedding

    new_emb = np.array([1.0, 0.0, 0.0])
    update_embedding("alice", new_emb, data_dir=tmp_path)

    saved = np.load(str(tmp_path / "profiles" / "embeddings" / "alice.npy"))
    np.testing.assert_array_equal(saved, new_emb)


def test_update_embedding_ema(tmp_path):
    from wisper_transcribe.speaker_manager import update_embedding

    existing = np.array([1.0, 0.0, 0.0])
    new_emb = np.array([0.0, 1.0, 0.0])

    emb_dir = tmp_path / "profiles" / "embeddings"
    emb_dir.mkdir(parents=True)
    np.save(str(emb_dir / "alice.npy"), existing)

    update_embedding("alice", new_emb, data_dir=tmp_path, alpha=0.3)

    saved = np.load(str(emb_dir / "alice.npy"))
    expected = 0.3 * new_emb + 0.7 * existing
    np.testing.assert_array_almost_equal(saved, expected)


# ---------------------------------------------------------------------------
# enroll_speaker
# ---------------------------------------------------------------------------

def test_enroll_speaker(tmp_path):
    from wisper_transcribe.speaker_manager import enroll_speaker, load_profiles

    segs = _fake_diarization(["SPEAKER_00"])
    fake_emb = np.ones(512)

    with patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=fake_emb):
        profile = enroll_speaker(
            name="alice",
            display_name="Alice",
            role="DM",
            audio_path=Path("fake.wav"),
            segments=segs,
            speaker_label="SPEAKER_00",
            device="cpu",
            data_dir=tmp_path,
            notes="Game Master",
        )

    assert profile.display_name == "Alice"
    assert profile.role == "DM"

    # Profile should be persisted
    loaded = load_profiles(data_dir=tmp_path)
    assert "alice" in loaded
    assert loaded["alice"].display_name == "Alice"

    # Embedding file should exist
    emb_path = tmp_path / "profiles" / "embeddings" / "alice.npy"
    assert emb_path.exists()


def test_enroll_speaker_uses_precomputed_embedding_when_given(tmp_path):
    """When `embedding` is passed, enroll_speaker must save it as-is and must
    NOT call extract_embedding -- this is what lets web callers average
    embeddings from two raw labels assigned the same display name before
    saving (F3)."""
    from wisper_transcribe.speaker_manager import enroll_speaker, load_profiles

    segs = _fake_diarization(["SPEAKER_00"])
    precomputed = np.array([0.5, 0.25, 0.25])

    with patch("wisper_transcribe.speaker_manager.extract_embedding") as mock_extract:
        profile = enroll_speaker(
            name="alice",
            display_name="Alice",
            role="",
            audio_path=Path("fake.wav"),
            segments=segs,
            speaker_label="SPEAKER_00",
            device="cpu",
            data_dir=tmp_path,
            embedding=precomputed,
        )

    mock_extract.assert_not_called()
    assert profile.display_name == "Alice"
    saved = np.load(str(tmp_path / "profiles" / "embeddings" / "alice.npy"))
    np.testing.assert_array_equal(saved, precomputed)
    assert "alice" in load_profiles(data_dir=tmp_path)


# ---------------------------------------------------------------------------
# reset_profiles
# ---------------------------------------------------------------------------

def test_reset_profiles_removes_all(tmp_path):
    from wisper_transcribe.speaker_manager import load_profiles, reset_profiles

    _write_profile(tmp_path, "alice", np.ones(3))
    _write_profile(tmp_path, "bob", np.ones(3))

    count = reset_profiles(data_dir=tmp_path)

    assert count == 2
    assert load_profiles(data_dir=tmp_path) == {}
    assert not (tmp_path / "profiles" / "embeddings" / "alice.npy").exists()
    assert not (tmp_path / "profiles" / "embeddings" / "bob.npy").exists()


def test_reset_profiles_removes_reference_clips(tmp_path):
    """R9-5: a full reset must also clear .mp3 reference clips, not just
    .npy embeddings -- otherwise every enrolled speaker's clip leaks."""
    from wisper_transcribe.speaker_manager import reset_profiles

    _write_profile(tmp_path, "alice", np.ones(3))
    emb_dir = tmp_path / "profiles" / "embeddings"
    clip = emb_dir / "alice.mp3"
    clip.write_bytes(b"fake mp3")

    reset_profiles(data_dir=tmp_path)

    assert not clip.exists()


def test_reset_profiles_empty(tmp_path):
    from wisper_transcribe.speaker_manager import reset_profiles

    count = reset_profiles(data_dir=tmp_path)
    assert count == 0


def test_extract_embedding_no_matching_segments_raises(tmp_path):
    """ValueError is raised when no segments match the requested speaker label."""
    from wisper_transcribe.speaker_manager import extract_embedding

    segments = [DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_01")]
    import torch
    fake_audio_dict = {"waveform": torch.zeros(1, 16000), "sample_rate": 16000}

    with patch("wisper_transcribe.audio_utils.load_wav_as_tensor", return_value=fake_audio_dict):
        with patch("wisper_transcribe.speaker_manager._load_embedding_model"):
            with pytest.raises(ValueError, match="No segments found for speaker"):
                extract_embedding(
                    tmp_path / "fake.wav",
                    segments,
                    speaker_label="SPEAKER_00",
                )


# ---------------------------------------------------------------------------
# F10b — _select_embedding_segments (pure function, no mocks needed)
# ---------------------------------------------------------------------------

def test_select_embedding_segments_no_segments_for_label_raises():
    from wisper_transcribe.speaker_manager import _select_embedding_segments

    segments = [DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_01")]
    with pytest.raises(ValueError, match="No segments found for speaker"):
        _select_embedding_segments(segments, "SPEAKER_00")


def test_select_embedding_segments_prefers_solo_medium_over_longer_overlapped():
    """A long SPEAKER_00 segment that overlaps another speaker's turn
    (cross-talk) must lose out to a shorter solo segment in the 2-20s band."""
    from wisper_transcribe.speaker_manager import _select_embedding_segments

    segments = [
        # Long but overlaps SPEAKER_01 for its whole span -- cross-talk risk.
        DiarizationSegment(start=0.0, end=30.0, speaker="SPEAKER_00"),
        DiarizationSegment(start=5.0, end=10.0, speaker="SPEAKER_01"),
        # Solo, in the 2-20s sweet spot -- should be preferred.
        DiarizationSegment(start=40.0, end=48.0, speaker="SPEAKER_00"),
    ]

    selected = _select_embedding_segments(segments, "SPEAKER_00")

    assert selected == [DiarizationSegment(start=40.0, end=48.0, speaker="SPEAKER_00")]


def test_select_embedding_segments_band_fallback_to_all_solo():
    """When no solo segment falls in the 2-20s band, fall back to all solo
    segments sorted longest-first."""
    from wisper_transcribe.speaker_manager import _select_embedding_segments

    segments = [
        # Solo but too short (under 2.0s).
        DiarizationSegment(start=0.0, end=1.0, speaker="SPEAKER_00"),
        # Solo but too long (over 20.0s).
        DiarizationSegment(start=10.0, end=35.0, speaker="SPEAKER_00"),
    ]

    selected = _select_embedding_segments(segments, "SPEAKER_00")

    # Both are solo (no other speaker present at all) -- longest-first.
    assert selected == [
        DiarizationSegment(start=10.0, end=35.0, speaker="SPEAKER_00"),
        DiarizationSegment(start=0.0, end=1.0, speaker="SPEAKER_00"),
    ]


def test_select_embedding_segments_no_solo_falls_back_to_longest_overall():
    """When every SPEAKER_00 segment overlaps another speaker, fall back to
    the longest speaker_segs regardless of overlap (today's old behavior)."""
    from wisper_transcribe.speaker_manager import _select_embedding_segments

    segments = [
        DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00"),
        DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_01"),
        DiarizationSegment(start=10.0, end=20.0, speaker="SPEAKER_00"),
        DiarizationSegment(start=10.0, end=20.0, speaker="SPEAKER_01"),
    ]

    selected = _select_embedding_segments(segments, "SPEAKER_00")

    assert selected == [
        DiarizationSegment(start=10.0, end=20.0, speaker="SPEAKER_00"),
        DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00"),
    ]


def test_select_embedding_segments_respects_max_count():
    from wisper_transcribe.speaker_manager import _select_embedding_segments

    segments = [
        DiarizationSegment(start=float(i * 30), end=float(i * 30 + 5 + i), speaker="SPEAKER_00")
        for i in range(8)
    ]

    selected = _select_embedding_segments(segments, "SPEAKER_00", max_count=3)

    assert len(selected) == 3
    # Longest-first within the 2-20s band.
    durations = [s.end - s.start for s in selected]
    assert durations == sorted(durations, reverse=True)


# ---------------------------------------------------------------------------
# match_speakers — profile_filter
# ---------------------------------------------------------------------------

def test_match_speakers_with_profile_filter(tmp_path):
    """Profiles outside the filter must never be returned as matches."""
    from wisper_transcribe.speaker_manager import match_speakers

    alice_emb = np.array([1.0, 0.0, 0.0])
    bob_emb   = np.array([0.0, 1.0, 0.0])
    charlie_emb = np.array([0.0, 0.0, 1.0])
    _write_profile(tmp_path, "alice", alice_emb)
    _write_profile(tmp_path, "bob", bob_emb)
    _write_profile(tmp_path, "charlie", charlie_emb)

    segs = _fake_diarization(["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"])

    def fake_extract(audio_path, segments, label, device="cpu"):
        mapping = {
            "SPEAKER_00": alice_emb,
            "SPEAKER_01": bob_emb,
            "SPEAKER_02": charlie_emb,
        }
        return mapping[label]

    with patch("wisper_transcribe.speaker_manager.extract_embedding", side_effect=fake_extract):
        result = match_speakers(
            Path("fake.wav"),
            segs,
            data_dir=tmp_path,
            threshold=0.65,
            profile_filter={"alice", "bob"},
        )

    assert result["SPEAKER_00"] == "Alice"
    assert result["SPEAKER_01"] == "Bob"
    # Charlie is filtered out — SPEAKER_02 must not map to Charlie
    assert "Charlie" not in result.values()


def test_match_speakers_empty_profile_filter_returns_empty(tmp_path):
    """An empty profile_filter set means zero candidate profiles — always returns {}."""
    from wisper_transcribe.speaker_manager import match_speakers

    alice_emb = np.array([1.0, 0.0, 0.0])
    _write_profile(tmp_path, "alice", alice_emb)
    segs = _fake_diarization(["SPEAKER_00"])

    with patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=alice_emb):
        result = match_speakers(
            Path("fake.wav"),
            segs,
            data_dir=tmp_path,
            threshold=0.65,
            profile_filter=set(),
        )

    assert result == {}


def test_match_speakers_none_filter_uses_all_profiles(tmp_path):
    """profile_filter=None (default) preserves existing global-match behaviour."""
    from wisper_transcribe.speaker_manager import match_speakers

    alice_emb = np.array([1.0, 0.0, 0.0])
    bob_emb   = np.array([0.0, 1.0, 0.0])
    _write_profile(tmp_path, "alice", alice_emb)
    _write_profile(tmp_path, "bob", bob_emb)

    segs = _fake_diarization(["SPEAKER_00", "SPEAKER_01"])

    def fake_extract(audio_path, segments, label, device="cpu"):
        return alice_emb if label == "SPEAKER_00" else bob_emb

    with patch("wisper_transcribe.speaker_manager.extract_embedding", side_effect=fake_extract):
        result = match_speakers(
            Path("fake.wav"),
            segs,
            data_dir=tmp_path,
            threshold=0.65,
            profile_filter=None,
        )

    assert result["SPEAKER_00"] == "Alice"
    assert result["SPEAKER_01"] == "Bob"


# ---------------------------------------------------------------------------
# enroll_speaker_from_audio_dir (R1: path-traversal guard needs `os` import)
# ---------------------------------------------------------------------------

def test_enroll_speaker_from_audio_dir_rejects_dir_outside_recordings_tree(tmp_path):
    """R1 regression: the per_user_dir guard uses os.path.abspath/os.sep,
    which previously raised NameError because `os` was never imported into
    speaker_manager.py. This exercises those exact lines -- the validation
    happens before any file is read, so no audio is needed."""
    from wisper_transcribe.speaker_manager import enroll_speaker_from_audio_dir

    outside_dir = tmp_path / "elsewhere" / "someuser"
    outside_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="per_user_dir outside expected recordings tree"):
        enroll_speaker_from_audio_dir(
            name="alice",
            display_name="Alice",
            role="Player",
            per_user_dir=outside_dir,
            data_dir=tmp_path,
        )


def test_enroll_speaker_from_audio_dir_no_audio_files_found(tmp_path):
    """A per_user_dir inside the recordings tree but with no .opus files
    should raise a clear 'No audio files found' error rather than crashing
    or silently succeeding."""
    from wisper_transcribe.speaker_manager import enroll_speaker_from_audio_dir

    per_user_dir = tmp_path / "recordings" / "session01" / "someuser"
    per_user_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="No audio files found"):
        enroll_speaker_from_audio_dir(
            name="alice",
            display_name="Alice",
            role="Player",
            per_user_dir=per_user_dir,
            data_dir=tmp_path,
        )
