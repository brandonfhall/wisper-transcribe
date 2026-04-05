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
    """Two speakers should not both be assigned to the same profile."""
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

    names = list(result.values())
    assert "Alice" in names
    # One should be Alice, the other Unknown (profile already claimed)
    assert names.count("Alice") == 1
    assert "Unknown Speaker" in names[1] or "Unknown Speaker" in names[0]


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
