from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np

# Must be in place before pyannote.audio (and Lightning) are imported so that
# checkpoint-upgrade, migration-shim, and TF32 warnings are suppressed.
from ._noise_suppress import suppress_third_party_noise as _suppress
_suppress()

from .config import get_data_dir
from .models import DiarizationSegment, SpeakerProfile

_embedding_model = None


def _get_profiles_dir(data_dir: Optional[Path] = None) -> Path:
    base = Path(data_dir) if data_dir else get_data_dir()
    return base / "profiles"


def _get_speakers_json(data_dir: Optional[Path] = None) -> Path:
    return _get_profiles_dir(data_dir) / "speakers.json"


def _get_embeddings_dir(data_dir: Optional[Path] = None) -> Path:
    return _get_profiles_dir(data_dir) / "embeddings"


# ---------------------------------------------------------------------------
# Profile CRUD
# ---------------------------------------------------------------------------

def load_profiles(data_dir: Optional[Path] = None) -> dict[str, SpeakerProfile]:
    path = _get_speakers_json(data_dir)
    if not path.exists():
        return {}

    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    profiles: dict[str, SpeakerProfile] = {}
    for name, data in raw.items():
        profiles[name] = SpeakerProfile(
            name=name,
            display_name=data.get("display_name", name),
            role=data.get("role", ""),
            embedding_path=_get_profiles_dir(data_dir) / data["embedding_file"],
            enrolled_date=data.get("enrolled_date", ""),
            enrollment_source=data.get("enrollment_source", ""),
            notes=data.get("notes", ""),
        )
    return profiles


def save_profiles(profiles: dict[str, SpeakerProfile], data_dir: Optional[Path] = None) -> None:
    path = _get_speakers_json(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    raw: dict = {}
    for name, p in profiles.items():
        raw[name] = {
            "display_name": p.display_name,
            "role": p.role,
            "embedding_file": f"embeddings/{name}.npy",
            "enrolled_date": p.enrolled_date,
            "enrollment_source": p.enrollment_source,
            "notes": p.notes,
        }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)


def reset_profiles(data_dir: Optional[Path] = None) -> int:
    """Delete all speaker profiles and embeddings. Returns number of speakers removed."""
    speakers_json = _get_speakers_json(data_dir)
    emb_dir = _get_embeddings_dir(data_dir)

    count = 0
    if speakers_json.exists():
        import json as _json
        with open(speakers_json, encoding="utf-8") as f:
            count = len(_json.load(f))
        speakers_json.unlink()

    if emb_dir.exists():
        for npy in emb_dir.glob("*.npy"):
            npy.unlink()

    return count


# ---------------------------------------------------------------------------
# Embedding extraction
# ---------------------------------------------------------------------------

def _load_embedding_model(device: str):
    global _embedding_model
    if _embedding_model is None:
        from pyannote.audio import Model, Inference
        try:
            model = Model.from_pretrained(
                "pyannote/embedding",
                token=_get_hf_token(),
            )
        except Exception as e:
            if "locate the file on the Hub" in str(e) or "connection" in str(e).lower():
                raise RuntimeError(
                    "Failed to download the embedding model from Hugging Face. "
                    "Please ensure you have an active internet connection for the first run."
                ) from e
            raise
        _embedding_model = Inference(model, window="whole")
        if device in ("cuda", "mps"):
            import torch
            _embedding_model.to(torch.device(device))
    return _embedding_model


def _get_hf_token() -> str:
    from .config import get_hf_token
    try:
        return get_hf_token()
    except (RuntimeError, Exception):
        return ""


def extract_embedding(
    audio_path: Path,
    segments: list[DiarizationSegment],
    speaker_label: str,
    device: str = "cpu",
) -> np.ndarray:
    """Extract a voice embedding for a speaker by averaging their longest segments."""
    from pyannote.core import Segment as PyannoteSegment

    inference = _load_embedding_model(device)

    speaker_segs = [s for s in segments if s.speaker == speaker_label]
    if not speaker_segs:
        raise ValueError(f"No segments found for speaker {speaker_label!r}")

    # Use up to 5 longest segments
    longest = sorted(speaker_segs, key=lambda s: s.end - s.start, reverse=True)[:5]

    # Pre-load audio via scipy so pyannote never calls torchaudio (removed in 2.x).
    from .audio_utils import load_wav_as_tensor

    audio_dict = load_wav_as_tensor(audio_path)

    embeddings = []
    for seg in longest:
        excerpt = PyannoteSegment(seg.start, seg.end)
        emb = inference.crop(audio_dict, excerpt)
        embeddings.append(emb)

    return np.mean(embeddings, axis=0)


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------

def enroll_speaker(
    name: str,
    display_name: str,
    role: str,
    audio_path: Path,
    segments: list[DiarizationSegment],
    speaker_label: str,
    device: str = "cpu",
    data_dir: Optional[Path] = None,
    notes: str = "",
) -> SpeakerProfile:
    """Extract embedding and save a new speaker profile."""
    import datetime

    embedding = extract_embedding(audio_path, segments, speaker_label, device)

    emb_dir = _get_embeddings_dir(data_dir)
    emb_dir.mkdir(parents=True, exist_ok=True)
    emb_path = emb_dir / f"{name}.npy"
    np.save(str(emb_path), embedding)

    profile = SpeakerProfile(
        name=name,
        display_name=display_name,
        role=role,
        embedding_path=emb_path,
        enrolled_date=datetime.date.today().isoformat(),
        enrollment_source=Path(audio_path).name,
        notes=notes,
    )

    profiles = load_profiles(data_dir)
    profiles[name] = profile
    save_profiles(profiles, data_dir)

    # Save a short reference audio clip alongside the embedding for web playback.
    # Failures are silently swallowed — the clip is a convenience, not critical.
    _save_reference_clip(audio_path, segments, speaker_label, emb_dir / f"{name}.mp3")

    return profile


def _save_reference_clip(
    audio_path: Path,
    segments: list,
    speaker_label: str,
    out_path: Path,
    max_seconds: float = 12.0,
) -> None:
    """Extract a short clip of speaker_label from audio_path using ffmpeg."""
    import subprocess

    # Find the first segment for this speaker
    first = next((s for s in segments if s.speaker == speaker_label), None)
    if first is None:
        return
    start = first.start
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-t", str(max_seconds),
                "-i", str(audio_path),
                "-ac", "1", "-ar", "22050", "-b:a", "64k",
                str(out_path),
            ],
            check=True,
            capture_output=True,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    a_norm = np.linalg.norm(a)
    b_norm = np.linalg.norm(b)
    if a_norm == 0 or b_norm == 0:
        return 0.0
    return float(np.dot(a, b) / (a_norm * b_norm))


def match_speakers(
    audio_path: Path,
    diarization_segments: list[DiarizationSegment],
    data_dir: Optional[Path] = None,
    device: str = "cpu",
    threshold: float = 0.65,
    profile_filter: Optional[set] = None,
) -> dict[str, str]:
    """Match anonymous speaker labels to enrolled profiles via cosine similarity.

    Returns a mapping like {"SPEAKER_00": "Alice", "SPEAKER_01": "Unknown Speaker 1"}.
    Returns an empty dict if no profiles are enrolled.

    profile_filter: when provided, only profiles whose key is in this set are
    considered candidates.  None (default) uses all enrolled profiles.
    """
    profiles = load_profiles(data_dir)
    if profile_filter is not None:
        profiles = {k: v for k, v in profiles.items() if k in profile_filter}
        if not profiles:
            return {}
    if not profiles:
        return {}

    unique_labels = sorted({s.speaker for s in diarization_segments})

    # Extract query embeddings
    query_embeddings: dict[str, np.ndarray] = {}
    for label in unique_labels:
        try:
            query_embeddings[label] = extract_embedding(audio_path, diarization_segments, label, device)
        except Exception:
            query_embeddings[label] = None  # type: ignore[assignment]

    # Load enrolled embeddings
    enrolled: dict[str, np.ndarray] = {}
    for pname, profile in profiles.items():
        if profile.embedding_path.exists():
            enrolled[pname] = np.load(str(profile.embedding_path))

    if not enrolled:
        return {}

    # Greedy best-match assignment
    result: dict[str, str] = {}
    used_profiles: set[str] = set()
    unknown_counter = 1

    # Sort by best available similarity (descending) so highest-confidence matches go first
    scored = []
    for label, q_emb in query_embeddings.items():
        if q_emb is None:
            scored.append((label, None, -1.0))
            continue
        best_name = None
        best_sim = -1.0
        for pname, e_emb in enrolled.items():
            sim = _cosine_similarity(q_emb, e_emb)
            if sim > best_sim:
                best_sim = sim
                best_name = pname
        scored.append((label, best_name, best_sim))

    scored.sort(key=lambda x: x[2], reverse=True)

    for label, best_name, best_sim in scored:
        if best_name is not None and best_sim >= threshold and best_name not in used_profiles:
            result[label] = profiles[best_name].display_name
            used_profiles.add(best_name)
        else:
            result[label] = f"Unknown Speaker {unknown_counter}"
            unknown_counter += 1

    return result


# ---------------------------------------------------------------------------
# Embedding update (EMA)
# ---------------------------------------------------------------------------

def update_embedding(
    name: str,
    new_embedding: np.ndarray,
    data_dir: Optional[Path] = None,
    alpha: float = 0.3,
) -> None:
    """Update an existing embedding using exponential moving average."""
    emb_dir = _get_embeddings_dir(data_dir)
    emb_dir.mkdir(parents=True, exist_ok=True)
    emb_path = emb_dir / f"{name}.npy"
    if not emb_path.exists():
        np.save(str(emb_path), new_embedding)
        return
    existing = np.load(str(emb_path))
    updated = alpha * new_embedding + (1 - alpha) * existing
    np.save(str(emb_path), updated)
