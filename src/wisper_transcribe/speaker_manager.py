from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import numpy as np

# Must be in place before pyannote.audio (and Lightning) are imported so that
# checkpoint-upgrade, migration-shim, and TF32 warnings are suppressed.
from ._noise_suppress import suppress_third_party_noise as _suppress
_suppress()

from .config import (
    DEFAULT_SIMILARITY_THRESHOLD,
    DIARIZATION_MODEL,
    EMBEDDING_SPACE,
    EMBEDDING_SUBFOLDER,
    get_data_dir,
)
from . import db, file_registry
from .models import DiarizationSegment, SpeakerProfile

# Embedding-model cache, keyed by device so a different device reloads it.
_embedding_model = None
_embedding_device: Optional[str] = None


def _get_profiles_dir(data_dir: Optional[Path] = None) -> Path:
    base = Path(data_dir) if data_dir else get_data_dir()
    return base / "profiles"


def get_reference_clips_dir(data_dir: Optional[Path] = None) -> Path:
    """Directory of the ``<key>.mp3`` reference clips. It keeps the
    ``embeddings/`` name so existing data dirs need no move."""
    return _get_profiles_dir(data_dir) / "embeddings"


def reference_clip_path(key: str, data_dir: Optional[Path] = None) -> Path:
    """Where a profile's ~12 s reference clip lives (it may not exist)."""
    return get_reference_clips_dir(data_dir) / f"{key}.mp3"


# ---------------------------------------------------------------------------
# Profile CRUD (table ``profiles`` in wisper.db)
# ---------------------------------------------------------------------------

def _embedding_to_blob(embedding: Optional[np.ndarray]) -> Optional[bytes]:
    if embedding is None:
        return None
    return np.asarray(embedding, dtype=np.float32).reshape(-1).tobytes()


def _blob_to_embedding(blob: Optional[bytes]) -> Optional[np.ndarray]:
    if blob is None:
        return None
    return np.frombuffer(blob, dtype=np.float32).copy()


def _row_to_profile(row) -> SpeakerProfile:
    return SpeakerProfile(
        name=row["key"],
        display_name=row["display_name"],
        role=row["role"],
        embedding=_blob_to_embedding(row["embedding"]),
        enrolled_date=row["enrolled_date"],
        enrollment_source=row["enrollment_source"],
        notes=row["notes"],
        # NULL = no embedding; "" = untagged, from the pre-WeSpeaker model.
        embedding_space=row["embedding_space"] or "",
    )


def _upsert_profile(conn, key: str, p: SpeakerProfile) -> None:
    """Insert or update by key. Keeps the row id, so memberships survive."""
    blob = _embedding_to_blob(p.embedding)
    conn.execute(
        "INSERT INTO profiles (key, display_name, role, notes, enrolled_date, "
        "enrollment_source, embedding, embedding_space) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET display_name = excluded.display_name, "
        "role = excluded.role, notes = excluded.notes, "
        "enrolled_date = excluded.enrolled_date, "
        "enrollment_source = excluded.enrollment_source, "
        "embedding = excluded.embedding, embedding_space = excluded.embedding_space",
        (
            key, p.display_name or key, p.role or "", p.notes or "",
            p.enrolled_date or "", p.enrollment_source or "",
            blob, None if blob is None else (p.embedding_space or ""),
        ),
    )


def load_profiles(data_dir: Optional[Path] = None) -> dict[str, SpeakerProfile]:
    """All profiles by key, in enrollment order, embeddings included (~1 KB each)."""
    with db.connection(data_dir) as conn:
        rows = conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()
    return {row["key"]: _row_to_profile(row) for row in rows}


def profile_activity(data_dir: Optional[Path] = None) -> dict[str, tuple[int, Optional[str]]]:
    """Per profile key: (transcripts that name the speaker, latest such transcript's date).

    A transcript names a profile when one of its speaker rows carries the
    profile's display name; transcripts whose ``.md`` is missing don't count.
    """
    with db.connection(data_dir) as conn:
        rows = conn.execute(
            "SELECT p.key, COUNT(DISTINCT t.id), MAX(t.created_at) FROM profiles p "
            "LEFT JOIN transcript_speakers ts ON ts.display_name = p.display_name "
            "LEFT JOIN transcripts t ON t.id = ts.transcript_id AND t.missing_since IS NULL "
            "GROUP BY p.key"
        ).fetchall()
    return {key: (count, last) for key, count, last in rows}


def remove_profile_files(key: str, data_dir: Optional[Path] = None) -> None:
    """Delete a profile's reference clip (and a stray pre-SQLite ``.npy``).

    ``key`` can come from a URL, so each path is confined to the clips folder
    (basename + abspath/startswith, the form CodeQL accepts as a sanitiser).
    """
    base = os.path.abspath(str(get_reference_clips_dir(data_dir))) + os.sep
    for suffix in (".mp3", ".npy"):
        target = os.path.abspath(os.path.join(base, os.path.basename(f"{key}{suffix}")))
        if target.startswith(base):
            Path(target).unlink(missing_ok=True)


def remove_profile(key: str, data_dir: Optional[Path] = None) -> None:
    """Delete a profile (its memberships cascade), then its reference clip.

    Shared by the CLI and web remove paths. Raises ``KeyError`` if ``key`` is
    not enrolled.
    """
    with db.transaction(data_dir) as conn:
        owner = file_registry.Owner.for_profile_key(key, conn=conn)
        registered = file_registry.paths_for_delete(owner, conn, data_dir=data_dir) if owner else []
        if conn.execute("DELETE FROM profiles WHERE key = ?", (key,)).rowcount == 0:
            raise KeyError(f"Speaker profile {key!r} not found")
    file_registry.unlink_paths(registered)
    remove_profile_files(key, data_dir)  # also an unregistered clip or stray .npy


def rename_profile(old_key: str, new_name: str, data_dir: Optional[Path] = None) -> SpeakerProfile:
    """Rename a speaker: rekey the profile, then move its reference clip.

    Shared by ``wisper speakers rename`` and the web rename route.

    1. Derive ``new_key`` (``name.lower().replace(" ", "_")``) and validate it
       with ``validate_path_component``; it becomes a filename and URL slug,
       and validation also breaks the CodeQL taint chain for form input.
       Invalid keys raise ``ValueError``.
    2. Renaming onto a different existing key raises ``ValueError``.
    3. One ``UPDATE`` of the key and display name. Campaign memberships
       (roles, characters, Discord bindings) reference the profile's id, so
       they follow without being touched.
    4. After the commit, move the ``.mp3`` clip (file after row).

    Raises ``KeyError`` if ``old_key`` isn't enrolled. A same-key rename (case
    change) only updates ``display_name``.
    """
    from .path_utils import validate_path_component

    new_key = new_name.lower().replace(" ", "_")
    safe_new_key = validate_path_component(new_key, "_rename_profile_guard")
    with db.transaction(data_dir) as conn:
        if conn.execute("SELECT 1 FROM profiles WHERE key = ?", (old_key,)).fetchone() is None:
            raise KeyError(f"Speaker profile {old_key!r} not found")
        if safe_new_key is None:
            raise ValueError("invalid profile name")
        if safe_new_key != old_key and conn.execute(
            "SELECT 1 FROM profiles WHERE key = ?", (safe_new_key,)
        ).fetchone() is not None:
            raise ValueError("profile already exists")
        row = conn.execute(
            "UPDATE profiles SET key = ?, display_name = ? WHERE key = ? RETURNING *",
            (safe_new_key, new_name, old_key),
        ).fetchone()
        profile = _row_to_profile(row)
        profile_id = row["id"]

    if safe_new_key != old_key:
        old_clip = reference_clip_path(old_key, data_dir)
        if old_clip.exists():
            try:
                old_clip.rename(reference_clip_path(safe_new_key, data_dir))
            except OSError:
                # The clip is a convenience; the rename already committed. The
                # trigger moved its row, so point it back at the file on disk.
                row = file_registry.file_for(
                    file_registry.Owner("profile", profile_id), "reference_clip", data_dir=data_dir)
                if row is not None:
                    file_registry.repoint(row, old_clip, data_dir=data_dir)
    return profile


def reset_profiles(data_dir: Optional[Path] = None) -> int:
    """Delete all speaker profiles and reference clips. Returns the number removed."""
    with db.transaction(data_dir) as conn:
        registered = [
            path for (pid,) in conn.execute("SELECT id FROM profiles").fetchall()
            for path in file_registry.paths_for_delete(
                file_registry.Owner("profile", pid), conn, data_dir=data_dir)
        ]
        count = conn.execute("DELETE FROM profiles").rowcount
    file_registry.unlink_paths(registered)
    clips = get_reference_clips_dir(data_dir)
    if clips.exists():
        for pattern in ("*.mp3", "*.npy"):
            for f in clips.glob(pattern):
                f.unlink(missing_ok=True)
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
                DIARIZATION_MODEL,
                subfolder=EMBEDDING_SUBFOLDER,
                token=_get_hf_token(),
            )
        except Exception as e:
            from huggingface_hub.errors import GatedRepoError

            if isinstance(e, GatedRepoError):
                raise RuntimeError(
                    "Your Hugging Face token can't access the speaker model. "
                    f"Accept its terms at https://huggingface.co/{DIARIZATION_MODEL} "
                    "(free, one-time), then retry."
                ) from e
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


# Segments averaged per speaker embedding. Measured on real sessions, 30
# instead of 5 raised same-person similarity across sessions by 0.05-0.10.
EMBEDDING_SEGMENTS = 30


def _unit(v: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(v)
    return v / norm if norm > 0 else v


def extract_embedding(
    audio_path: Path,
    segments: list[DiarizationSegment],
    speaker_label: str,
    device: str = "cpu",
) -> np.ndarray:
    """Extract a speaker's unit-length voice embedding, averaged over selected segments.

    See ``_select_embedding_segments()`` for which segments are used.
    Each segment is normalized before averaging so long segments don't dominate.
    """
    from pyannote.core import Segment as PyannoteSegment

    inference = _load_embedding_model(device)

    selected = _select_embedding_segments(segments, speaker_label, max_count=EMBEDDING_SEGMENTS)

    # Pre-load audio via scipy so pyannote never calls torchaudio (removed in 2.x).
    from .audio_utils import load_wav_as_tensor

    audio_dict = load_wav_as_tensor(audio_path)

    embeddings = []
    for seg in selected:
        excerpt = PyannoteSegment(seg.start, seg.end)
        emb = inference.crop(audio_dict, excerpt)
        embeddings.append(_unit(np.asarray(emb, dtype=np.float32).reshape(-1)))

    return _unit(np.mean(embeddings, axis=0))


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------

def enroll_speaker(
    name: str,
    display_name: str,
    role: str,
    audio_path: Optional[Path] = None,
    segments: Optional[list[DiarizationSegment]] = None,
    speaker_label: Optional[str] = None,
    device: str = "cpu",
    data_dir: Optional[Path] = None,
    notes: str = "",
    embedding: Optional[np.ndarray] = None,
    clip_source: Optional[Path] = None,
    source_name: Optional[str] = None,
) -> SpeakerProfile:
    """Save a new speaker profile, extracting its embedding from audio.

    Pass ``embedding`` to skip extraction (a stored vector, or one averaged
    over several raw labels); then ``audio_path`` is only needed for the clip.
    ``clip_source`` is an existing clip copied as the reference clip, so no
    audio is read. ``source_name`` labels the enrollment source; it defaults
    to the audio file's name.
    """
    import datetime
    import shutil

    if embedding is None and audio_path is None:
        raise ValueError("enroll_speaker needs an embedding or an audio_path")
    if embedding is None:
        embedding = extract_embedding(audio_path, segments, speaker_label, device)

    profile = SpeakerProfile(
        name=name,
        display_name=display_name,
        role=role,
        embedding=_unit(np.asarray(embedding, dtype=np.float32).reshape(-1)),
        enrolled_date=datetime.date.today().isoformat(),
        enrollment_source=source_name or (Path(audio_path).name if audio_path else ""),
        notes=notes,
    )
    # Extraction above stays outside the transaction.
    with db.transaction(data_dir) as conn:
        _upsert_profile(conn, name, profile)

    # Save a short reference audio clip for web playback (file after row).
    # Failures are silently swallowed — the clip is a convenience, not critical.
    clip = reference_clip_path(name, data_dir)
    if clip_source is not None and Path(clip_source).is_file():
        clip.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(clip_source, clip)
    elif audio_path is not None and segments is not None and speaker_label is not None:
        clip.parent.mkdir(parents=True, exist_ok=True)
        _save_reference_clip(audio_path, segments, speaker_label, clip)
    if clip.is_file():
        file_registry.add_if_owned(
            clip, kind="reference_clip",
            owner=file_registry.Owner.for_profile_key(name, data_dir=data_dir), data_dir=data_dir)

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

def load_profile_embedding(profile: SpeakerProfile) -> Optional[np.ndarray]:
    """The profile's embedding, or ``None`` if it's missing or from another model."""
    if profile.embedding_space != EMBEDDING_SPACE or profile.embedding is None:
        return None
    return profile.embedding


def stale_profile_keys(profiles: dict[str, SpeakerProfile]) -> list[str]:
    """Keys of profiles enrolled with an older embedding model; they need re-enrolling."""
    return sorted(k for k, p in profiles.items() if p.embedding_space != EMBEDDING_SPACE)


# Profiles this similar are almost certainly one person enrolled twice.
DUPLICATE_SIMILARITY = 0.95


def find_duplicate_profiles(
    profiles: dict[str, SpeakerProfile], threshold: float = DUPLICATE_SIMILARITY,
) -> list[tuple[str, str, float]]:
    """Pairs of current-model profiles whose embeddings score above ``threshold``.

    Returns ``(key_a, key_b, similarity)`` sorted most similar first. Profiles
    from an older model or without an embedding are never compared.
    """
    usable = {k: e for k, p in profiles.items() if (e := load_profile_embedding(p)) is not None}
    keys = sorted(usable)
    pairs = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if usable[a].shape != usable[b].shape:
                continue
            sim = _cosine_similarity(usable[a], usable[b])
            if sim > threshold:
                pairs.append((a, b, sim))
    return sorted(pairs, key=lambda t: (-t[2], t[0], t[1]))


def placeholder_name_profiles(profiles: dict[str, SpeakerProfile]) -> list[str]:
    """Keys of profiles named like a pipeline placeholder (``SPEAKER_03``,
    ``Unknown Speaker 2``) — usually an accidental enrollment."""
    from .web.enroll_shared import AUTO_NAME_RE

    return sorted(k for k, p in profiles.items() if AUTO_NAME_RE.match(p.display_name))


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
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    profile_filter: Optional[set] = None,
    allow_many_to_one: bool = False,
    scores: Optional[dict[str, tuple[str, float]]] = None,
    embeddings: Optional[dict[str, np.ndarray]] = None,
) -> dict[str, str]:
    """Match diarization labels to enrolled profiles by cosine similarity.

    Returns e.g. ``{"SPEAKER_00": "Alice", "SPEAKER_01": "Unknown Speaker 1"}``,
    or ``{}`` if no profiles are enrolled.

    ``profile_filter`` restricts candidates to those keys (``None`` = all).
    See ``assign_labels()`` for the assignment rules and ``scores``.
    Pass a dict as ``embeddings`` to receive each label's extracted embedding;
    they are extracted even with no profiles, for the campaign relabel pass.
    """
    profiles = load_profiles(data_dir)
    if profile_filter is not None:
        profiles = {k: v for k, v in profiles.items() if k in profile_filter}
    if not profiles and embeddings is None:
        return {}

    unique_labels = sorted({s.speaker for s in diarization_segments})

    # Failed extractions stay out of query_embeddings and number as Unknown.
    query_embeddings: dict[str, np.ndarray] = {}
    for label in unique_labels:
        try:
            query_embeddings[label] = extract_embedding(audio_path, diarization_segments, label, device)
        except Exception:
            pass
    if embeddings is not None:
        embeddings.update(query_embeddings)
    if not profiles:
        return {}

    return assign_labels(
        unique_labels, query_embeddings, profiles,
        threshold=threshold, allow_many_to_one=allow_many_to_one, scores=scores,
    )


def assign_labels(
    labels: list[str],
    query_embeddings: dict[str, np.ndarray],
    profiles: dict[str, SpeakerProfile],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    allow_many_to_one: bool = False,
    scores: Optional[dict[str, tuple[str, float]]] = None,
) -> dict[str, str]:
    """Assign each label a profile display name or ``Unknown Speaker N``.

    Returns ``{}`` when no profile has a current-space embedding.

    Every (label, profile) pair is scored and consumed highest-first, so a
    label whose top choice is taken falls back to its next-best unused
    profile. With ``allow_many_to_one``, a still-unassigned label may then
    claim an already-used profile above threshold (one person split into two
    labels); only enable it when the speaker count wasn't pinned. Labels
    missing from ``query_embeddings`` are numbered as Unknown.

    Pass a dict as ``scores`` to receive each scored label's closest profile
    as ``label -> (display_name, similarity)``, whether or not it matched.
    """
    enrolled: dict[str, np.ndarray] = {}
    for pname, profile in profiles.items():
        emb = load_profile_embedding(profile)
        if emb is not None:
            enrolled[pname] = emb
    if not enrolled:
        return {}

    pairs: list[tuple[float, str, str]] = []  # (sim, label, profile_name)
    for label, q_emb in query_embeddings.items():
        for pname, e_emb in enrolled.items():
            pairs.append((_cosine_similarity(q_emb, e_emb), label, pname))

    # Deterministic ordering: highest similarity first, ties broken by label
    # then profile name so results don't depend on dict/insertion order.
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
    if scores is not None:
        for sim, label, pname in pairs:
            scores.setdefault(label, (profiles[pname].display_name, sim))

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
    for label in sorted(labels):
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
    """Blend ``new_embedding`` into a profile by exponential moving average.

    A profile from an older embedding model (or with no embedding) is
    replaced outright and retagged, since its vector can't be averaged with
    the new one. The read-blend-write is one transaction. An unknown ``name``
    is a no-op.
    """
    new_unit = _unit(np.asarray(new_embedding, dtype=np.float32).reshape(-1))

    with db.transaction(data_dir) as conn:
        row = conn.execute(
            "SELECT embedding, embedding_space FROM profiles WHERE key = ?", (name,)
        ).fetchone()
        if row is None:
            return
        existing = _blob_to_embedding(row["embedding"])
        if (row["embedding_space"] != EMBEDDING_SPACE or existing is None
                or existing.shape != new_unit.shape):
            blended = new_unit
        else:
            blended = _unit(alpha * new_unit + (1 - alpha) * _unit(existing))
        conn.execute(
            "UPDATE profiles SET embedding = ?, embedding_space = ? WHERE key = ?",
            (_embedding_to_blob(blended), EMBEDDING_SPACE, name),
        )
