from __future__ import annotations

from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from wisper_transcribe.config import DIARIZATION_MODEL, EMBEDDING_SPACE, EMBEDDING_SUBFOLDER
from wisper_transcribe.models import DiarizationSegment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_profile(
    data_dir: Path,
    name: str,
    embedding: np.ndarray,
    role: str = "Player",
    embedding_space: Optional[str] = EMBEDDING_SPACE,
) -> None:
    """Insert a speaker profile (embedding stored as given) into data_dir's DB.

    ``embedding_space=None`` is an untagged profile from the old model.
    """
    from wisper_transcribe import db
    from wisper_transcribe.models import SpeakerProfile
    from wisper_transcribe.speaker_manager import _upsert_profile

    profile = SpeakerProfile(
        name=name, display_name=name.capitalize(), role=role,
        embedding=np.asarray(embedding, dtype=np.float32),
        enrolled_date="2026-04-05", enrollment_source="session01.mp3", notes="",
        embedding_space=embedding_space or "",
    )
    with db.transaction(data_dir) as conn:
        _upsert_profile(conn, name, profile)


def _stored_embedding(data_dir: Path, name: str) -> np.ndarray:
    from wisper_transcribe.speaker_manager import load_profiles
    return load_profiles(data_dir)[name].embedding


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
    from wisper_transcribe.speaker_manager import load_profiles
    from tests._seed import save_profiles

    vec = np.full(256, 1 / 16, dtype=np.float32)
    profiles = {
        "alice": SpeakerProfile(
            name="alice",
            display_name="Alice",
            role="DM",
            embedding=vec,
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
    np.testing.assert_array_equal(loaded["alice"].embedding, vec)
    assert loaded["alice"].embedding.dtype == np.float32


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

def test_update_embedding_unknown_profile_is_noop(tmp_path):
    from wisper_transcribe.speaker_manager import load_profiles, update_embedding

    update_embedding("alice", np.array([1.0, 0.0, 0.0]), data_dir=tmp_path)
    assert load_profiles(tmp_path) == {}


def test_update_embedding_fills_profile_without_embedding(tmp_path):
    from wisper_transcribe import db
    from wisper_transcribe.speaker_manager import update_embedding

    _write_profile(tmp_path, "alice", np.ones(3))
    with db.transaction(tmp_path) as conn:
        conn.execute("UPDATE profiles SET embedding = NULL, embedding_space = NULL")
    update_embedding("alice", np.array([1.0, 0.0, 0.0]), data_dir=tmp_path)
    np.testing.assert_array_equal(_stored_embedding(tmp_path, "alice"), [1.0, 0.0, 0.0])


def test_update_embedding_ema(tmp_path):
    from wisper_transcribe.speaker_manager import update_embedding

    existing = np.array([1.0, 0.0, 0.0])
    new_emb = np.array([0.0, 1.0, 0.0])

    _write_profile(tmp_path, "alice", existing)

    update_embedding("alice", new_emb, data_dir=tmp_path, alpha=0.3)

    saved = _stored_embedding(tmp_path, "alice")
    expected = 0.3 * new_emb + 0.7 * existing
    np.testing.assert_array_almost_equal(saved, expected / np.linalg.norm(expected))


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

    # The unit-length embedding is stored with the profile, no .npy file
    np.testing.assert_array_almost_equal(
        loaded["alice"].embedding, np.ones(512) / np.sqrt(512))
    assert loaded["alice"].embedding_space == EMBEDDING_SPACE
    assert not list((tmp_path / "profiles").rglob("*.npy"))


def test_enroll_speaker_uses_precomputed_embedding_when_given(tmp_path):
    """When `embedding` is passed, enroll_speaker must save it as-is and must
    NOT call extract_embedding -- this is what lets web callers average
    embeddings from two raw labels assigned the same display name before
    saving."""
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
    saved = _stored_embedding(tmp_path, "alice")
    np.testing.assert_array_almost_equal(saved, precomputed / np.linalg.norm(precomputed))
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


def test_reset_profiles_removes_reference_clips(tmp_path):
    """A full reset must also clear .mp3 reference clips, not just the
    profile rows -- otherwise every enrolled speaker's clip leaks."""
    from wisper_transcribe.speaker_manager import reset_profiles

    _write_profile(tmp_path, "alice", np.ones(3))
    emb_dir = tmp_path / "profiles" / "embeddings"
    emb_dir.mkdir(parents=True, exist_ok=True)
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
# _select_embedding_segments (pure function, no mocks needed)
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
    the longest speaker_segs regardless of overlap."""
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
# enroll_speaker_from_audio_dir
# ---------------------------------------------------------------------------

def test_enroll_speaker_from_audio_dir_rejects_dir_outside_recordings_tree(tmp_path):
    """The per_user_dir guard (os.path.abspath/os.sep) rejects a directory
    outside the recordings tree before any file is read."""
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
    """A per_user_dir inside the recordings tree but with no .wav or .opus
    segment files should raise a clear 'No audio files found' error rather
    than crashing or silently succeeding."""
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


def _write_fake_wav_segment(path: Path, seconds: float = 0.5) -> None:
    """Write a tiny real 16 kHz mono 16-bit WAV file (silence) for decode tests."""
    import wave as _wave

    n_samples = int(16000 * seconds)
    with _wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * n_samples)


def test_enroll_speaker_from_audio_dir_prefers_wav_over_legacy_opus(tmp_path):
    """Enrollment reads .wav segments and prefers them over stale .opus
    files in the same directory."""
    from wisper_transcribe.speaker_manager import enroll_speaker_from_audio_dir

    per_user_dir = tmp_path / "recordings" / "session01" / "someuser"
    per_user_dir.mkdir(parents=True)
    _write_fake_wav_segment(per_user_dir / "0000.wav", seconds=0.5)
    _write_fake_wav_segment(per_user_dir / "0001.wav", seconds=0.5)
    # A stale/garbage .opus alongside real .wav segments must be ignored.
    (per_user_dir / "0000.opus").write_bytes(b"not a real opus stream")

    fake_emb = np.ones(512)
    with patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=fake_emb):
        profile = enroll_speaker_from_audio_dir(
            name="alice",
            display_name="Alice",
            role="Player",
            per_user_dir=per_user_dir,
            data_dir=tmp_path,
        )

    assert profile.display_name == "Alice"
    assert _stored_embedding(tmp_path, "alice") is not None


def test_enroll_speaker_from_audio_dir_legacy_opus_fallback_fails_gracefully(tmp_path):
    """An old recording directory has only (invalid) .opus files.
    enroll_speaker_from_audio_dir attempts them as a
    best-effort fallback, but must raise a normal exception -- not crash --
    so the JOB_ENROLL runner's generic error handling still applies."""
    from wisper_transcribe.speaker_manager import enroll_speaker_from_audio_dir

    per_user_dir = tmp_path / "recordings" / "session01" / "someuser"
    per_user_dir.mkdir(parents=True)
    (per_user_dir / "0000.opus").write_bytes(b"not a real opus stream" * 10)

    with pytest.raises(Exception):
        enroll_speaker_from_audio_dir(
            name="alice",
            display_name="Alice",
            role="Player",
            per_user_dir=per_user_dir,
            data_dir=tmp_path,
        )


# ---------------------------------------------------------------------------
# Embedding-model cache keyed by device
# ---------------------------------------------------------------------------

def test_load_embedding_model_reuses_cache_on_same_device():
    import wisper_transcribe.speaker_manager as sm
    from unittest.mock import MagicMock, patch as _patch

    cached = MagicMock()
    sm._embedding_model = cached
    sm._embedding_device = "cpu"
    try:
        with _patch("pyannote.audio.Model") as mock_model_cls:
            result = sm._load_embedding_model("cpu")
            mock_model_cls.from_pretrained.assert_not_called()
        assert result is cached
    finally:
        sm._embedding_model = None
        sm._embedding_device = None


# ---------------------------------------------------------------------------
# Concurrent mutation must not lose writes
# ---------------------------------------------------------------------------

def test_enroll_speaker_atomic_under_concurrent_calls(tmp_path):
    """Concurrent enroll_speaker() calls must not lose writes to the
    profiles table. `embedding=` is passed explicitly so no ML model
    is invoked; `segments=[]` skips the ffmpeg reference-clip step."""
    import threading

    from wisper_transcribe.speaker_manager import enroll_speaker, load_profiles

    n = 20

    def _enroll(i):
        enroll_speaker(
            name=f"speaker_{i:02d}",
            display_name=f"Speaker {i:02d}",
            role="",
            audio_path=tmp_path / "fake.wav",
            segments=[],
            speaker_label="SPEAKER_00",
            data_dir=tmp_path,
            embedding=np.zeros(4),
        )

    threads = [threading.Thread(target=_enroll, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    loaded = load_profiles(data_dir=tmp_path)
    assert len(loaded) == n
    for i in range(n):
        assert f"speaker_{i:02d}" in loaded


def test_remove_profile_and_enroll_speaker_do_not_lose_writes_concurrently(tmp_path):
    """Concurrent remove_profile() (on pre-seeded distinct profiles) and
    enroll_speaker() (adding new distinct profiles) must not clobber each
    other's writes."""
    import threading

    from wisper_transcribe.speaker_manager import (
        enroll_speaker,
        load_profiles,
        remove_profile,
    )
    from tests._seed import save_profiles
    from wisper_transcribe.models import SpeakerProfile

    n = 10
    # Pre-seed n profiles to be removed concurrently with n new enrollments.
    existing = {
        f"old_{i:02d}": SpeakerProfile(
            name=f"old_{i:02d}",
            display_name=f"Old {i:02d}",
            role="",
            embedding=np.ones(4, dtype=np.float32) / 2,
            enrolled_date="2026-01-01",
            enrollment_source="seed",
        )
        for i in range(n)
    }
    save_profiles(existing, data_dir=tmp_path)

    def _remove(i):
        remove_profile(f"old_{i:02d}", data_dir=tmp_path)

    def _enroll(i):
        enroll_speaker(
            name=f"new_{i:02d}",
            display_name=f"New {i:02d}",
            role="",
            audio_path=tmp_path / "fake.wav",
            segments=[],
            speaker_label="SPEAKER_00",
            data_dir=tmp_path,
            embedding=np.zeros(4),
        )

    threads = (
        [threading.Thread(target=_remove, args=(i,)) for i in range(n)]
        + [threading.Thread(target=_enroll, args=(i,)) for i in range(n)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    loaded = load_profiles(data_dir=tmp_path)
    assert len(loaded) == n  # all n "old_*" removed, all n "new_*" added
    for i in range(n):
        assert f"old_{i:02d}" not in loaded
        assert f"new_{i:02d}" in loaded


def test_load_embedding_model_reloads_on_device_change():
    """A cached CPU embedding model is not reused when a later caller
    asks for a different device."""
    import wisper_transcribe.speaker_manager as sm
    from unittest.mock import MagicMock, patch as _patch

    stale = MagicMock()
    sm._embedding_model = stale
    sm._embedding_device = "cuda"

    fresh = MagicMock()
    try:
        with _patch("pyannote.audio.Model") as mock_model_cls, \
             _patch("pyannote.audio.Inference", return_value=fresh) as mock_inf:
            mock_model_cls.from_pretrained.return_value = MagicMock()
            result = sm._load_embedding_model("cpu")
            mock_model_cls.from_pretrained.assert_called_once()
            mock_inf.assert_called_once()
        assert result is fresh
        assert sm._embedding_model is fresh
        assert sm._embedding_device == "cpu"
    finally:
        sm._embedding_model = None
        sm._embedding_device = None


# ---------------------------------------------------------------------------
# Embedding space (profiles from an older embedding model)
# ---------------------------------------------------------------------------

def test_legacy_profile_skipped_without_dimension_crash(tmp_path):
    """A 512-d untagged profile is never compared with a 256-d query."""
    from wisper_transcribe.speaker_manager import match_speakers

    _write_profile(tmp_path, "old", np.ones(512), embedding_space=None)
    new = np.zeros(256)
    new[0] = 1.0
    _write_profile(tmp_path, "alice", new)

    with patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=new):
        result = match_speakers(Path("fake.wav"), _fake_diarization(["SPEAKER_00"]), data_dir=tmp_path)

    assert result == {"SPEAKER_00": "Alice"}


def test_only_legacy_profiles_returns_empty(tmp_path):
    from wisper_transcribe.speaker_manager import match_speakers

    _write_profile(tmp_path, "old", np.ones(512), embedding_space=None)
    with patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=np.ones(256)):
        assert match_speakers(Path("fake.wav"), _fake_diarization(["SPEAKER_00"]), data_dir=tmp_path) == {}


def test_stale_profile_keys(tmp_path):
    from wisper_transcribe.speaker_manager import load_profiles, stale_profile_keys

    _write_profile(tmp_path, "old", np.ones(512), embedding_space=None)
    _write_profile(tmp_path, "other", np.ones(512), embedding_space="something-else")
    _write_profile(tmp_path, "alice", np.ones(256))

    assert stale_profile_keys(load_profiles(tmp_path)) == ["old", "other"]


def test_update_embedding_replaces_legacy_profile_and_retags(tmp_path):
    from wisper_transcribe.speaker_manager import load_profiles, update_embedding

    _write_profile(tmp_path, "alice", np.ones(512), embedding_space=None)
    new = np.array([3.0, 4.0])

    update_embedding("alice", new, data_dir=tmp_path)

    saved = _stored_embedding(tmp_path, "alice")
    np.testing.assert_array_almost_equal(saved, [0.6, 0.8])
    assert load_profiles(tmp_path)["alice"].embedding_space == EMBEDDING_SPACE


def test_rename_profile_moves_clip_after_commit(tmp_path):
    from wisper_transcribe.speaker_manager import load_profiles, reference_clip_path, rename_profile

    _write_profile(tmp_path, "alice", np.ones(4))
    clip = reference_clip_path("alice", tmp_path)
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"mp3")
    profile = rename_profile("alice", "Alicia Smith", data_dir=tmp_path)
    assert profile.name == "alicia_smith"
    assert set(load_profiles(tmp_path)) == {"alicia_smith"}
    assert reference_clip_path("alicia_smith", tmp_path).read_bytes() == b"mp3"
    assert not clip.exists()


def test_rename_profile_rejects_existing_key_without_changes(tmp_path):
    from wisper_transcribe.speaker_manager import load_profiles, rename_profile

    _write_profile(tmp_path, "alice", np.ones(4))
    _write_profile(tmp_path, "bob", np.ones(4))
    with pytest.raises(ValueError):
        rename_profile("alice", "Bob", data_dir=tmp_path)
    assert set(load_profiles(tmp_path)) == {"alice", "bob"}


def test_profile_schema_constraints(tmp_path):
    import sqlite3

    from wisper_transcribe import db

    _write_profile(tmp_path, "alice", np.ones(4))
    bad_statements = [
        "INSERT INTO profiles (key, display_name, enrolled_date, enrollment_source) "
        "VALUES ('alice', 'Dup', '', '')",                                   # UNIQUE key
        "INSERT INTO profiles (key, display_name, enrolled_date, enrollment_source) "
        "VALUES ('x', '', '', '')",                                          # empty name
        "UPDATE profiles SET embedding_space = NULL",                        # paired nullables
        "UPDATE profiles SET embedding = NULL",
        "UPDATE profiles SET embedding = x'010203'",                         # not float32-sized
        "UPDATE profiles SET embedding = 'text'",                            # STRICT
    ]
    for sql in bad_statements:
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction(tmp_path) as conn:
                conn.execute(sql)


def test_extract_embedding_normalizes_and_uses_many_segments():
    import wisper_transcribe.speaker_manager as sm

    segs = [DiarizationSegment(start=i * 10.0, end=i * 10.0 + 5.0, speaker="SPEAKER_00") for i in range(40)]
    inference = MagicMock()
    inference.crop.side_effect = lambda audio, seg: np.array([seg.start + 1.0, 0.0])

    with patch.object(sm, "_load_embedding_model", return_value=inference),          patch("wisper_transcribe.audio_utils.load_wav_as_tensor", return_value={}):
        emb = sm.extract_embedding(Path("fake.wav"), segs, "SPEAKER_00")

    assert inference.crop.call_count == sm.EMBEDDING_SEGMENTS
    np.testing.assert_array_almost_equal(emb, [1.0, 0.0])


def test_load_embedding_model_uses_diarization_repo_subfolder():
    import wisper_transcribe.speaker_manager as sm

    sm._embedding_model = None
    sm._embedding_device = None
    with patch("pyannote.audio.Model") as mock_model_cls, patch("pyannote.audio.Inference"):
        sm._load_embedding_model("cpu")

    args, kwargs = mock_model_cls.from_pretrained.call_args
    assert args == (DIARIZATION_MODEL,)
    assert kwargs["subfolder"] == EMBEDDING_SUBFOLDER
    sm._embedding_model = None
    sm._embedding_device = None


def test_match_speakers_reports_closest_profile_scores(tmp_path):
    """``scores`` gets every label's closest profile, matched or not."""
    from wisper_transcribe.speaker_manager import match_speakers

    _write_profile(tmp_path, "alice", np.array([1.0, 0.0]))
    embs = {"SPEAKER_00": np.array([1.0, 0.0]), "SPEAKER_01": np.array([0.6, 0.8])}
    scores: dict = {}

    with patch("wisper_transcribe.speaker_manager.extract_embedding",
               side_effect=lambda _a, _s, label, _d: embs[label]):
        result = match_speakers(Path("fake.wav"), _fake_diarization(["SPEAKER_00", "SPEAKER_01"]),
                                data_dir=tmp_path, threshold=0.55, scores=scores)

    assert result == {"SPEAKER_00": "Alice", "SPEAKER_01": "Unknown Speaker 1"}
    assert scores["SPEAKER_00"] == ("Alice", pytest.approx(1.0))
    assert scores["SPEAKER_01"] == ("Alice", pytest.approx(0.6))


def test_default_threshold_is_calibrated_value():
    import inspect

    from wisper_transcribe.config import DEFAULT_SIMILARITY_THRESHOLD
    from wisper_transcribe.speaker_manager import match_speakers

    assert DEFAULT_SIMILARITY_THRESHOLD == 0.55
    assert inspect.signature(match_speakers).parameters["threshold"].default == DEFAULT_SIMILARITY_THRESHOLD


# ---------------------------------------------------------------------------
# profile_activity — the Sessions / Last heard figures on the Speakers page
# ---------------------------------------------------------------------------

def _named_transcript(stem: str, names: dict[str, str]) -> Path:
    from wisper_transcribe.path_utils import get_output_dir

    from ._seed import seed_sidecar

    md = get_output_dir() / f"{stem}.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("x", encoding="utf-8")
    seed_sidecar(md, {"input_path": "", "diarization_segments": [], "speaker_map": names})
    return md


def test_profile_activity_counts_transcripts_naming_the_speaker():
    from wisper_transcribe import transcript_store
    from wisper_transcribe.speaker_manager import profile_activity

    from ._seed import seed_profile

    seed_profile("alice", "Alice")
    seed_profile("bob", "Bob")
    seed_profile("carol", "Carol")
    _named_transcript("s1", {"SPEAKER_00": "Alice", "SPEAKER_01": "Alice", "SPEAKER_02": "Bob"})
    _named_transcript("s2", {"SPEAKER_00": "Alice"})
    gone = _named_transcript("s3", {"SPEAKER_00": "Bob"})
    gone.unlink()
    transcript_store.reconcile()  # s3's .md is missing: it no longer counts

    activity = profile_activity()
    assert activity["alice"][0] == 2  # two labels in s1 count once
    assert activity["bob"][0] == 1
    assert activity["carol"] == (0, None)
    assert activity["alice"][1] is not None


# ---------------------------------------------------------------------------
# enroll_speaker without audio: stored embedding, copied clip
# ---------------------------------------------------------------------------

def test_enroll_speaker_requires_embedding_or_audio():
    from wisper_transcribe.speaker_manager import enroll_speaker

    with pytest.raises(ValueError):
        enroll_speaker(name="alice", display_name="Alice", role="", embedding=None, audio_path=None)


def test_enroll_speaker_copies_clip_source_byte_for_byte(tmp_path):
    from wisper_transcribe.speaker_manager import enroll_speaker, reference_clip_path

    clip_src = tmp_path / "session01_excerpt_SPEAKER_00.mp3"
    clip_src.write_bytes(b"\x00excerpt-bytes\xff")

    with patch("wisper_transcribe.speaker_manager.extract_embedding") as mock_extract:
        profile = enroll_speaker(
            name="alice", display_name="Alice", role="",
            embedding=np.array([1.0, 0.0, 0.0]),
            clip_source=clip_src, source_name="session01",
        )

    mock_extract.assert_not_called()
    assert reference_clip_path("alice").read_bytes() == clip_src.read_bytes()
    assert profile.enrollment_source == "session01"


def test_enroll_speaker_without_clip_or_audio_creates_profile_without_clip(tmp_path):
    from wisper_transcribe.speaker_manager import enroll_speaker, load_profiles, reference_clip_path

    enroll_speaker(
        name="alice", display_name="Alice", role="",
        embedding=np.array([0.0, 1.0, 0.0]),
        clip_source=tmp_path / "missing.mp3",
    )

    assert not reference_clip_path("alice").exists()
    assert "alice" in load_profiles()
