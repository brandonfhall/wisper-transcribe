from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

import numpy as np

# Must be in place before pyannote.audio (and Lightning) are imported so that
# checkpoint-upgrade, migration-shim, and TF32 warnings are suppressed.
from ._noise_suppress import suppress_third_party_noise as _suppress
_suppress()

from .config import get_data_dir
from .models import DiarizationSegment, SpeakerProfile

# Embedding-model cache, keyed by device so a different device reloads it.
_embedding_model = None
_embedding_device: Optional[str] = None

# Guards every load-modify-save of speakers.json against lost updates from
# concurrent requests. Never taken inside load_profiles/save_profiles
# themselves: callers already hold it and would deadlock.
_profiles_lock = threading.Lock()


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


def remove_profile_files(key: str, data_dir: Optional[Path] = None) -> None:
    """Delete a profile's embedding (``.npy``) and reference clip (``.mp3``).

    Both are optional: older profiles or failed clip extraction leave no clip.
    """
    emb_dir = _get_embeddings_dir(data_dir)
    (emb_dir / f"{key}.npy").unlink(missing_ok=True)
    (emb_dir / f"{key}.mp3").unlink(missing_ok=True)


def remove_profile(key: str, data_dir: Optional[Path] = None) -> None:
    """Remove a profile's ``speakers.json`` entry and its embedding + clip files.

    Shared by the CLI and web remove paths. Raises ``KeyError`` if ``key`` is
    not enrolled.
    """
    with _profiles_lock:
        profiles = load_profiles(data_dir)
        if key not in profiles:
            raise KeyError(f"Speaker profile {key!r} not found")
        profiles.pop(key)
        remove_profile_files(key, data_dir)
        save_profiles(profiles, data_dir)


def rename_profile_files(old_key: str, new_key: str, data_dir: Optional[Path] = None) -> Path:
    """Rename a profile's embedding and reference clip to a new key.

    Returns the new embedding path. The clip may not exist.
    """
    emb_dir = _get_embeddings_dir(data_dir)
    old_npy = emb_dir / f"{old_key}.npy"
    new_npy = emb_dir / f"{new_key}.npy"
    if old_npy.exists():
        old_npy.rename(new_npy)

    old_mp3 = emb_dir / f"{old_key}.mp3"
    new_mp3 = emb_dir / f"{new_key}.mp3"
    if old_mp3.exists():
        old_mp3.rename(new_mp3)

    return new_npy


def rename_profile(old_key: str, new_name: str, data_dir: Optional[Path] = None) -> SpeakerProfile:
    """Rename a speaker: rekey the profile, move its files, update campaigns.

    Shared by ``wisper speakers rename`` and the web rename route.

    1. Derive ``new_key`` (``name.lower().replace(" ", "_")``) and validate it
       with ``validate_path_component``; it becomes a filename and URL slug,
       and validation also breaks the CodeQL taint chain for form input.
       Invalid keys raise ``ValueError``.
    2. Renaming onto a different existing key raises ``ValueError``.
    3. Rekey the entry, update ``name``/``display_name``/``embedding_path``,
       and move the ``.npy``/``.mp3`` files.
    4. Rekey campaign membership (roles, characters, Discord bindings).

    Raises ``KeyError`` if ``old_key`` isn't enrolled. A same-key rename (case
    change) only updates ``display_name``.
    """
    from .path_utils import validate_path_component

    with _profiles_lock:
        profiles = load_profiles(data_dir)
        if old_key not in profiles:
            raise KeyError(f"Speaker profile {old_key!r} not found")

        new_key = new_name.lower().replace(" ", "_")
        safe_new_key = validate_path_component(new_key, "_rename_profile_guard")
        if safe_new_key is None:
            raise ValueError("invalid profile name")
        if safe_new_key != old_key and safe_new_key in profiles:
            raise ValueError("profile already exists")

        profile = profiles.pop(old_key)
        profile.name = safe_new_key
        profile.display_name = new_name
        if safe_new_key != old_key:
            profile.embedding_path = rename_profile_files(old_key, safe_new_key, data_dir)
        profiles[safe_new_key] = profile
        save_profiles(profiles, data_dir)

        if safe_new_key != old_key:
            # Lock order is always profiles then campaigns, so this nested
            # acquire can't deadlock.
            from .campaign_manager import rekey_member
            rekey_member(old_key, safe_new_key, data_dir)

        return profile


def reset_profiles(data_dir: Optional[Path] = None) -> int:
    """Delete all speaker profiles and embeddings. Returns number of speakers removed."""
    with _profiles_lock:
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
            # Clear reference clips too.
            for mp3 in emb_dir.glob("*.mp3"):
                mp3.unlink()

        return count


# ---------------------------------------------------------------------------
# Embedding extraction
# ---------------------------------------------------------------------------

def _load_embedding_model(device: str):
    """Load (and cache) the pyannote embedding model for *device*.

    Published to the cache only after a successful load and move, so a
    failure never leaves a half-initialised model cached.
    """
    global _embedding_model, _embedding_device
    if _embedding_model is None or _embedding_device != device:
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
        inference = Inference(model, window="whole")
        if device in ("cuda", "mps"):
            import torch
            inference.to(torch.device(device))
        # Publish to the cache only after the device move succeeded; drop the
        # old reference first so peak memory doesn't hold two models.
        _embedding_model = None
        _embedding_device = None
        _embedding_model = inference
        _embedding_device = device
    return _embedding_model


def _get_hf_token() -> str:
    from .config import get_hf_token
    try:
        return get_hf_token()
    except Exception:
        return ""


def _segments_overlap(a: DiarizationSegment, b: DiarizationSegment) -> bool:
    """Strict time overlap between two segments: ``max(starts) < min(ends)``."""
    return max(a.start, b.start) < min(a.end, b.end)


def _select_embedding_segments(
    segments: list[DiarizationSegment],
    speaker_label: str,
    max_count: int = 5,
) -> list[DiarizationSegment]:
    """Pick up to ``max_count`` segments to build a speaker's embedding from.

    1. Segments with ``speaker_label`` (``ValueError`` if none).
    2. Prefer *solo* segments — no overlap with another speaker, where
       cross-talk or music would pollute the embedding — of 2–20 s,
       longest first.
    3. Else all solo segments, longest first.
    4. Else (constant cross-talk) the longest segments regardless of overlap.
    """
    speaker_segs = [s for s in segments if s.speaker == speaker_label]
    if not speaker_segs:
        raise ValueError(f"No segments found for speaker {speaker_label!r}")

    other_segs = [s for s in segments if s.speaker != speaker_label]
    solo = [
        s for s in speaker_segs
        if not any(_segments_overlap(s, o) for o in other_segs)
    ]

    if solo:
        banded = [s for s in solo if 2.0 <= (s.end - s.start) <= 20.0]
        pool = banded if banded else solo
        return sorted(pool, key=lambda s: s.end - s.start, reverse=True)[:max_count]

    return sorted(speaker_segs, key=lambda s: s.end - s.start, reverse=True)[:max_count]


def extract_embedding(
    audio_path: Path,
    segments: list[DiarizationSegment],
    speaker_label: str,
    device: str = "cpu",
) -> np.ndarray:
    """Extract a speaker's voice embedding, averaged over selected segments.

    See ``_select_embedding_segments()`` for which segments are used.
    """
    from pyannote.core import Segment as PyannoteSegment

    inference = _load_embedding_model(device)

    selected = _select_embedding_segments(segments, speaker_label)

    # Pre-load audio via scipy so pyannote never calls torchaudio (removed in 2.x).
    from .audio_utils import load_wav_as_tensor

    audio_dict = load_wav_as_tensor(audio_path)

    embeddings = []
    for seg in selected:
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
    embedding: Optional[np.ndarray] = None,
) -> SpeakerProfile:
    """Extract an embedding and save a new speaker profile.

    Pass ``embedding`` to skip extraction, e.g. when the caller averaged
    several raw labels assigned the same name.
    """
    import datetime

    if embedding is None:
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

    # Lock only the store update; extraction and the .npy write above stay
    # outside so enrollments don't serialize.
    with _profiles_lock:
        profiles = load_profiles(data_dir)
        profiles[name] = profile
        save_profiles(profiles, data_dir)

    # Save a short reference audio clip alongside the embedding for web playback.
    # Failures are silently swallowed — the clip is a convenience, not critical.
    _save_reference_clip(audio_path, segments, speaker_label, emb_dir / f"{name}.mp3")

    return profile


def enroll_speaker_from_audio_dir(
    name: str,
    display_name: str,
    role: str,
    per_user_dir: Path,
    device: str = "cpu",
    data_dir: Optional[Path] = None,
    notes: str = "",
) -> SpeakerProfile:
    """Enroll a speaker from a per-user recording directory.

    Concatenates the directory's ``.wav`` segments (single-speaker audio) and
    enrolls from one full-length segment. Very old recordings only have
    ``.opus`` files that were never valid Opus; they are tried as a fallback
    and fail with the caller's generic error.
    """
    import tempfile

    from pydub import AudioSegment as PydubSegment

    # Validate per_user_dir lives under the expected recordings tree.
    # os.path.abspath (not Path.resolve()) breaks the CodeQL taint chain.
    from .config import get_data_dir as _get_data_dir
    _recordings_base = os.path.abspath(str(Path(_get_data_dir() if data_dir is None else data_dir) / "recordings"))
    if not _recordings_base.endswith(os.sep):
        _recordings_base += os.sep
    _resolved = os.path.abspath(str(per_user_dir))
    if not _resolved.startswith(_recordings_base):
        raise ValueError("per_user_dir outside expected recordings tree")
    _safe_dir = Path(_resolved)

    wav_files = sorted(_safe_dir.glob("*.wav"))
    if wav_files:
        combined = PydubSegment.empty()
        for f in wav_files:
            combined += PydubSegment.from_file(str(f), format="wav")
    else:
        # Old recordings: no .wav segments, try the .opus files.
        opus_files = sorted(_safe_dir.glob("*.opus"))
        if not opus_files:
            raise ValueError(f"No audio files found in {_safe_dir}")

        combined = PydubSegment.empty()
        for f in opus_files:
            combined += PydubSegment.from_file(str(f), format="opus")

    duration_s = combined.duration_seconds
    if duration_s <= 0:
        raise ValueError("No audio content found in per-user directory")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        combined.export(str(tmp_path), format="wav")
        return enroll_speaker(
            name=name,
            display_name=display_name,
            role=role,
            audio_path=tmp_path,
            segments=[DiarizationSegment(0.0, duration_s, "SPEAKER_00")],
            speaker_label="SPEAKER_00",
            device=device,
            data_dir=data_dir,
            notes=notes,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


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
    allow_many_to_one: bool = False,
) -> dict[str, str]:
    """Match diarization labels to enrolled profiles by cosine similarity.

    Returns e.g. ``{"SPEAKER_00": "Alice", "SPEAKER_01": "Unknown Speaker 1"}``,
    or ``{}`` if no profiles are enrolled.

    ``profile_filter`` restricts candidates to those keys (``None`` = all).

    Every (label, profile) pair is scored and consumed highest-first, so a
    label whose top choice is taken falls back to its next-best unused
    profile. With ``allow_many_to_one``, a still-unassigned label may then
    claim an already-used profile above threshold (one person split into two
    labels); only enable it when the speaker count wasn't pinned.
    """
    profiles = load_profiles(data_dir)
    if profile_filter is not None:
        profiles = {k: v for k, v in profiles.items() if k in profile_filter}
        if not profiles:
            return {}
    if not profiles:
        return {}

    unique_labels = sorted({s.speaker for s in diarization_segments})

    # Failed extractions go to `failed`, never into query_embeddings.
    query_embeddings: dict[str, np.ndarray] = {}
    failed: set[str] = set()
    for label in unique_labels:
        try:
            query_embeddings[label] = extract_embedding(audio_path, diarization_segments, label, device)
        except Exception:
            failed.add(label)

    # Load enrolled embeddings
    enrolled: dict[str, np.ndarray] = {}
    for pname, profile in profiles.items():
        if profile.embedding_path.exists():
            enrolled[pname] = np.load(str(profile.embedding_path))

    if not enrolled:
        return {}

    # Score every pair for labels with an embedding; failed labels are
    # numbered as Unknown below.
    pairs: list[tuple[float, str, str]] = []  # (sim, label, profile_name)
    for label, q_emb in query_embeddings.items():
        for pname, e_emb in enrolled.items():
            sim = _cosine_similarity(q_emb, e_emb)
            pairs.append((sim, label, pname))

    # Deterministic ordering: highest similarity first, ties broken by label
    # then profile name so results don't depend on dict/insertion order.
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))

    result: dict[str, str] = {}
    used_profiles: set[str] = set()

    # Exclusive pass: take the best remaining pair while both sides are free.
    for sim, label, pname in pairs:
        if sim < threshold:
            break  # pairs are sorted descending; nothing further clears threshold
        if label in result or pname in used_profiles:
            continue
        result[label] = profiles[pname].display_name
        used_profiles.add(pname)

    # Many-to-one pass: an unassigned label takes its best profile, used or
    # not, if it clears threshold.
    if allow_many_to_one:
        best_by_label: dict[str, tuple[float, str]] = {}
        for sim, label, pname in pairs:
            if label in result:
                continue
            if label not in best_by_label or sim > best_by_label[label][0]:
                best_by_label[label] = (sim, pname)
        for label, (sim, pname) in best_by_label.items():
            if sim >= threshold:
                result[label] = profiles[pname].display_name

    # Everything else becomes "Unknown Speaker N", numbered by sorted label.
    unknown_counter = 1
    for label in unique_labels:
        if label not in result:
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
