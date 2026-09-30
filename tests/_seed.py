"""Seed wisper.db directly for tests (synthetic data only)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from wisper_transcribe import db
from wisper_transcribe.config import EMBEDDING_SPACE
from wisper_transcribe.models import SpeakerProfile


def unit(vec) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = np.linalg.norm(v)
    return v / n if n else v


def make_profile(key: str, display_name: Optional[str] = None, *, role: str = "",
                 embedding=None, embedding_space: str = EMBEDDING_SPACE,
                 notes: str = "", enrolled_date: str = "2026-01-01",
                 enrollment_source: str = "session.mp3") -> SpeakerProfile:
    """A SpeakerProfile; ``embedding`` defaults to a key-seeded random unit vector."""
    if embedding is None:
        rng = np.random.default_rng(abs(hash(key)) % (2**32))
        embedding = unit(rng.standard_normal(256))
    return SpeakerProfile(
        name=key,
        display_name=display_name or key.replace("_", " ").title(),
        role=role,
        embedding=unit(embedding),
        enrolled_date=enrolled_date,
        enrollment_source=enrollment_source,
        notes=notes,
        embedding_space=embedding_space,
    )


def seed_profile(key: str, display_name: Optional[str] = None, *,
                 data_dir: Optional[Path] = None, **kwargs) -> SpeakerProfile:
    """Insert (or replace) one profile in the data dir's wisper.db."""
    from wisper_transcribe.speaker_manager import _upsert_profile

    profile = make_profile(key, display_name, **kwargs)
    with db.transaction(data_dir) as conn:
        _upsert_profile(conn, key, profile)
    return profile


def seed_profiles(*keys: str, data_dir: Optional[Path] = None) -> None:
    for key in keys:
        seed_profile(key, data_dir=data_dir)


def sidecar_data(sidecar_path: Path) -> dict:
    """What a ``<stem>_diar.json`` means now: its segments plus the speaker
    fields stored in the database (``transcript_store.read_sidecar``)."""
    from wisper_transcribe.transcript_store import SIDECAR_SUFFIX, read_sidecar

    name = Path(sidecar_path).name
    assert name.endswith(SIDECAR_SUFFIX), name
    md = Path(sidecar_path).with_name(name[: -len(SIDECAR_SUFFIX)] + ".md")
    data = read_sidecar(md)
    assert data is not None, f"no sidecar for {md.name}"
    return data
