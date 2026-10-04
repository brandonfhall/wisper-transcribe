"""Campaign-wide speaker relabelling from per-transcript voice embeddings.

Each web transcript stores one embedding per raw label and where each
display name came from (``auto`` or ``manual``) in ``transcript_speakers``,
read and written as the sidecar-shaped dict by ``transcript_store``. ``relabel_campaign()``
re-matches every auto-named label in a campaign against the roster, and gives
unknown voices that recur across sessions one shared ``Recurring Speaker N``
name. Naming a person once (and enrolling them) then propagates to every
session they appear in; names set by hand are never touched.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .config import EMBEDDING_SPACE, load_config

log = logging.getLogger(__name__)

SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"


def embeddings_to_sidecar(embeddings: dict[str, np.ndarray]) -> dict:
    """Sidecar fields for per-label embeddings, tagged with their space."""
    return {
        "embedding_space": EMBEDDING_SPACE,
        "speaker_embeddings": {
            label: [round(float(x), 6) for x in np.asarray(emb).reshape(-1)]
            for label, emb in embeddings.items()
        },
    }


def embeddings_from_sidecar(diar: dict) -> Optional[dict[str, np.ndarray]]:
    """Per-label embeddings from a sidecar, or ``None`` if absent or from another model."""
    stored = diar.get("speaker_embeddings")
    if diar.get("embedding_space") != EMBEDDING_SPACE or not isinstance(stored, dict) or not stored:
        return None
    return {label: np.asarray(v, dtype=np.float32) for label, v in stored.items()}


def is_relabelable(label: str, name: str, sources: dict[str, str]) -> bool:
    """Whether the relabel pass may change this label's name.

    Labels with recorded provenance follow it. Older sidecars have none, so
    only pipeline-shaped names (raw labels, Unknown/Recurring Speaker N) count
    as automatic; a real name there may have been typed by the user.
    """
    from .web.enroll_shared import AUTO_NAME_RE

    source = sources.get(label)
    if source is not None:
        return source == SOURCE_AUTO
    return bool(AUTO_NAME_RE.match(name))


@dataclass
class TranscriptRelabel:
    stem: str
    # raw label -> (old name, new name)
    renamed: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Why the transcript was left alone, if it was.
    skipped: Optional[str] = None


@dataclass
class RelabelReport:
    transcripts: list[TranscriptRelabel] = field(default_factory=list)
    recurring: int = 0  # unknown voices heard in two or more sessions


@dataclass
class _Entry:
    stem: str
    md_path: Path
    diar: dict
    segments: list
    embeddings: dict[str, np.ndarray]
    assigned: dict[str, str]  # label -> profile display name (matched labels only)


def _unit(v: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(v)
    return v / norm if norm > 0 else v


def _cluster_unknowns(
    unknowns: list[tuple[str, str, np.ndarray]], threshold: float,
) -> dict[tuple[str, str], int]:
    """Greedy centroid clustering of ``(stem, label, embedding)`` across transcripts.

    Returns ``(stem, label) -> cluster id`` for clusters spanning two or more
    transcripts, ids numbered by first appearance. Single-session voices are
    omitted; they keep per-transcript Unknown numbering.
    """
    centroids: list[np.ndarray] = []
    members: list[list[tuple[str, str]]] = []
    for stem, label, emb in unknowns:
        u = _unit(emb)
        best, best_sim = None, threshold
        for i, c in enumerate(centroids):
            sim = float(np.dot(u, _unit(c)))
            if sim >= best_sim:
                best, best_sim = i, sim
        if best is None:
            centroids.append(u.copy())
            members.append([(stem, label)])
        else:
            centroids[best] = centroids[best] + u
            members[best].append((stem, label))

    result: dict[tuple[str, str], int] = {}
    next_id = 1
    for group in members:
        if len({stem for stem, _ in group}) < 2:
            continue
        for key in group:
            result[key] = next_id
        next_id += 1
    return result


def _load_sidecar(md_path: Path) -> Optional[dict]:
    from .transcript_store import read_sidecar
    return read_sidecar(md_path)


def _backfill_embeddings(diar: dict, segments: list, device: str) -> Optional[dict[str, np.ndarray]]:
    """Extract per-label embeddings from the transcript's durable audio, if it still exists."""
    from .audio_utils import convert_to_wav
    from .speaker_manager import extract_embedding

    input_path = diar.get("input_path")
    if not input_path or not Path(input_path).exists():
        return None
    source = Path(input_path)
    wav_path = convert_to_wav(source)
    try:
        embeddings: dict[str, np.ndarray] = {}
        for label in sorted({s.speaker for s in segments}):
            try:
                embeddings[label] = extract_embedding(wav_path, segments, label, device)
            except Exception as exc:
                log.warning("embedding extraction failed for %s: %s", label, exc)
        return embeddings or None
    finally:
        if wav_path != source:
            wav_path.unlink(missing_ok=True)


def relabel_campaign(
    slug: str,
    device: str = "cpu",
    backfill: bool = True,
    threshold: Optional[float] = None,
    dry_run: bool = False,
    progress: Optional[Callable[[str], None]] = None,
    output_dir: Optional[Path] = None,
    data_dir: Optional[Path] = None,
) -> RelabelReport:
    """Re-match auto-named speakers across every transcript in a campaign.

    ``backfill`` extracts embeddings for transcripts whose sidecar has none
    (older runs) from the transcript's kept audio, and stores them. ``dry_run``
    computes the report without writing anything.

    Raises ``KeyError`` for an unknown campaign.
    """
    from .campaign_manager import get_campaign_profile_keys, get_transcripts_for_campaign, load_campaigns
    from .models import DiarizationSegment
    from .path_utils import get_output_dir
    from .speaker_manager import assign_labels, load_profiles
    from .web.enroll_shared import apply_renames, resolve_current_names

    def _say(msg: str) -> None:
        if progress is not None:
            progress(msg)

    if slug not in load_campaigns(data_dir):
        raise KeyError(slug)
    if threshold is None:
        threshold = float(load_config()["similarity_threshold"])
    out_dir = Path(output_dir) if output_dir is not None else get_output_dir()

    roster = get_campaign_profile_keys(slug, data_dir)
    profiles = {k: p for k, p in load_profiles(data_dir).items() if k in roster}

    report = RelabelReport()
    entries: list[_Entry] = []
    for stem in get_transcripts_for_campaign(slug, data_dir):
        item = TranscriptRelabel(stem=stem)
        report.transcripts.append(item)
        # Stems come from the database; refuse anything path-like.
        if os.path.basename(stem) != stem or stem in ("", ".", ".."):
            item.skipped = "invalid transcript name"
            continue
        md_path = out_dir / f"{stem}.md"
        diar = _load_sidecar(md_path) if md_path.exists() else None
        if diar is None or not diar.get("diarization_segments"):
            item.skipped = "no speaker data"
            continue
        segments = [
            DiarizationSegment(start=s["start"], end=s["end"], speaker=s["speaker"])
            for s in diar["diarization_segments"]
        ]

        embeddings = embeddings_from_sidecar(diar)
        if embeddings is None and backfill:
            _say(f"Extracting voices: {stem}")
            embeddings = _backfill_embeddings(diar, segments, device)
            if embeddings is not None and not dry_run:
                diar.update(embeddings_to_sidecar(embeddings))
                from .transcript_store import set_speaker_embeddings
                set_speaker_embeddings(md_path, embeddings, EMBEDDING_SPACE)
        if embeddings is None:
            item.skipped = "no stored voice data and the source audio is gone"
            continue

        labels = sorted({s.speaker for s in segments})
        names = assign_labels(labels, embeddings, profiles, threshold=threshold, allow_many_to_one=True)
        assigned = {
            label: name for label, name in names.items()
            if not name.startswith("Unknown Speaker ")
        }
        entries.append(_Entry(stem, md_path, diar, segments, embeddings, assigned))

    unknowns = [
        (e.stem, label, e.embeddings[label])
        for e in entries
        for label in sorted(e.embeddings)
        if label not in e.assigned
    ]
    clusters = _cluster_unknowns(unknowns, threshold)
    report.recurring = len(set(clusters.values()))

    items = {t.stem: t for t in report.transcripts}
    for e in entries:
        item = items[e.stem]
        labels = sorted({s.speaker for s in e.segments})
        targets: dict[str, str] = dict(e.assigned)
        counter = 1
        for label in labels:
            if label in targets:
                continue
            cluster = clusters.get((e.stem, label))
            if cluster is not None:
                targets[label] = f"Recurring Speaker {cluster}"
            else:
                targets[label] = f"Unknown Speaker {counter}"
                counter += 1

        current = resolve_current_names(e.md_path, e.diar, e.segments)
        sources = e.diar.get("speaker_map_source") or {}
        renames = {
            label: target for label, target in targets.items()
            if is_relabelable(label, current.get(label, label), sources)
            and current.get(label, label) != target
        }
        item.renamed = {label: (current.get(label, label), new) for label, new in renames.items()}
        if renames and not dry_run:
            apply_renames(e.md_path, e.segments, renames, data_dir=data_dir, source=SOURCE_AUTO)
        for label, (old, new) in sorted(item.renamed.items()):
            _say(f"{e.stem}: {label} {old} → {new}")

    return report
