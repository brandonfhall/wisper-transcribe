"""Convert and delete audio that older transcripts and recordings no longer need.

``plan()`` only reads. ``apply()`` runs four actions in order:

1. reconcile the registry with the output folder, so renames are matched first;
2. for each transcript with an ``audio`` file: store the voice embeddings it
   lacks (while the original audio still exists), then shrink the audio to a
   16 kHz mono ``<stem>.flac`` (or drop it when the recording's
   ``combined.wav`` already covers it);
3. delete orphaned ``<recording-id>.wav`` hand-off copies in the output root;
4. trim each recording to ``combined.wav``.

Safety rule: a file is deleted only when it is tied to a registry row (the
replaced audio of a transcript), or is a ``<uuid>.wav`` whose uuid is a
recording id and which no row names. Nothing else in the output root is
touched, because the output root can be the user's own folder.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import db, file_registry
from .config import EMBEDDING_SPACE

log = logging.getLogger(__name__)

CONVERT = "convert"
DROP_COPY = "drop_copy"
ORPHAN = "orphan"
TRIM_RECORDING = "trim_recording"

# Order the actions run in (reconcile runs before all of them).
_ORDER = (CONVERT, DROP_COPY, ORPHAN, TRIM_RECORDING)
KIND_LABELS = {
    CONVERT: "convert to FLAC",
    DROP_COPY: "delete copy",
    ORPHAN: "delete orphan",
    TRIM_RECORDING: "trim recording",
}


@dataclass
class Action:
    kind: str
    path: Path
    size: int                       # bytes on disk now (for a trim: bytes it frees)
    stem: Optional[str] = None      # transcript actions
    recording_id: Optional[str] = None
    note: str = ""


@dataclass
class TrimPlan:
    output_dir: Path
    data_dir: Path
    actions: list[Action] = field(default_factory=list)
    attention: Optional[object] = None   # transcript_store.Attention

    @property
    def total_bytes(self) -> int:
        """Bytes the deletions free. A conversion's result size isn't known ahead."""
        return sum(a.size for a in self.actions if a.kind != CONVERT)

    @property
    def convert_bytes(self) -> int:
        """Current size of the files a conversion replaces."""
        return sum(a.size for a in self.actions if a.kind == CONVERT)


@dataclass
class TrimReport:
    converted: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    trimmed: dict[str, int] = field(default_factory=dict)
    embeddings: list[str] = field(default_factory=list)
    freed_bytes: int = 0            # net: negative when conversions grew the audio
    errors: list[str] = field(default_factory=list)
    attention: Optional[object] = None
    reconciled: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Server / runtime guard
# ---------------------------------------------------------------------------

def check_runtime(data_dir: Optional[Path] = None) -> None:
    """Refuse (``db.RuntimeConflict``) a container run beside a live host process.

    Only a container that crosses the Docker Desktop VM can corrupt the
    database; on the host, ``db.connect`` already refuses. A fresh lease of this
    process's own runtime (an earlier command) never blocks. Read-only: it
    never creates ``wisper.db``.
    """
    me = db.detect_runtime()
    if me.runtime != "container" or not me.crosses_vm:
        return
    for lease in db.status(data_dir).leases:
        if lease["runtime"] == "host" and lease["age_s"] < db.LEASE_TTL_S:
            raise db.RuntimeConflict(
                f"A host process ({lease['holder']}) is using this data directory. "
                "Stop it, or run this command on the host."
            )


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _is_kept_flac(row: file_registry.FileRow, stem: str) -> bool:
    """Already ``<stem>.flac`` at 16 kHz mono. An unprobeable file is not kept."""
    from .audio_utils import probe_format
    from .transcript_store import nfc

    if nfc(row.path.name) != nfc(stem) + ".flac":
        return False
    try:
        return tuple(probe_format(row.path)) == (16000, 1)
    except Exception:
        return False


def _find_orphans(output: Path, recording_ids: set[str], data_dir: Path) -> list[Path]:
    """``<recording-id>.wav`` files in the output root that no row names."""
    found: list[Path] = []
    try:
        entries = sorted(os.scandir(output), key=lambda e: e.name)
    except OSError:
        return found
    for entry in entries:
        name = entry.name
        if not name.endswith(".wav"):
            continue
        stem = name[:-4]
        if stem not in recording_ids:
            continue
        try:
            uuid.UUID(stem)
            if entry.is_symlink() or not entry.is_file():
                continue
        except (ValueError, OSError):
            continue
        path = Path(entry.path)
        if file_registry.is_registered(path, data_dir=data_dir, output_dir=output):
            continue
        found.append(path)
    return found


def plan(data_dir: Optional[Path] = None, output_dir: Optional[Path] = None) -> TrimPlan:
    """What :func:`apply` would do. Reads only: no reconcile, no registry writes."""
    from . import recording_manager, transcript_store

    data, output = file_registry._dirs(data_dir, output_dir)
    result = TrimPlan(output_dir=output, data_dir=data)

    rows: list[tuple[str, file_registry.FileRow, Optional[str]]] = []
    with db.connection(data) as conn:
        recordings = {r["id"]: r["capture_status"]
                      for r in conn.execute("SELECT id, capture_status FROM recordings")}
        linked = {r["transcript_id"]: r["id"] for r in conn.execute(
            "SELECT id, transcript_id FROM recordings WHERE transcript_id IS NOT NULL")}
        for t in conn.execute("SELECT id, stem FROM transcripts ORDER BY stem").fetchall():
            row = file_registry.file_for(
                file_registry.Owner("transcript", t["id"]), "audio",
                conn=conn, data_dir=data, output_dir=output)
            if row is not None and row.path.is_file():
                rows.append((t["stem"], row, linked.get(t["id"])))

    for stem, row, rec_id in rows:
        if rec_id is not None and recording_manager.combined_path_for(rec_id, data).is_file():
            result.actions.append(Action(
                DROP_COPY, row.path, _size(row.path), stem=stem, recording_id=rec_id,
                note="the recording's combined.wav is the audio"))
        elif not _is_kept_flac(row, stem):
            result.actions.append(Action(CONVERT, row.path, _size(row.path), stem=stem))

    for path in _find_orphans(output, set(recordings), data):
        result.actions.append(Action(ORPHAN, path, _size(path)))

    for rid, status in recordings.items():
        if status in file_registry._ACTIVE_CAPTURE:
            continue
        freed = recording_manager.trimmable_bytes(rid, data)
        if freed > 0:
            result.actions.append(Action(
                TRIM_RECORDING, recording_manager.get_recording_dir(rid, data), freed,
                recording_id=rid))

    result.actions.sort(key=lambda a: (_ORDER.index(a.kind), str(a.path)))
    report = file_registry.sync(output, data, scan_only=True)
    result.attention = transcript_store.needs_attention(output, data, report=report)
    return result


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _store_missing_embeddings(md_path: Path, audio: Path, data: Path, device: str,
                              report: TrimReport) -> None:
    """Extract and store the voice embeddings a transcript lacks, from ``audio``."""
    from . import speaker_registry, transcript_store
    from .models import DiarizationSegment

    diar = transcript_store.read_sidecar(md_path, data)
    if diar is None or not diar.get("diarization_segments"):
        return
    segments = [DiarizationSegment(start=s["start"], end=s["end"], speaker=s["speaker"])
                for s in diar["diarization_segments"]]
    with db.connection(data) as conn:
        tid = conn.execute("SELECT id FROM transcripts WHERE stem = ?",
                           (transcript_store.nfc(md_path.stem),)).fetchone()
        stored = {}
        named: list[str] = []
        if tid is not None:
            stored = {r["label"]: r["embedding_space"] for r in conn.execute(
                "SELECT label, embedding_space FROM transcript_speakers "
                "WHERE transcript_id = ? AND embedding IS NOT NULL", (tid["id"],))}
            named = [r["label"] for r in conn.execute(
                "SELECT label FROM transcript_speakers WHERE transcript_id = ?", (tid["id"],))]
    labels = set(named) or {s.speaker for s in segments}
    missing = {label for label in labels if stored.get(label) != EMBEDDING_SPACE}
    if not missing:
        return
    # read_sidecar may resolve a recording to combined.wav; this audio row is the source.
    diar["input_path"] = str(audio)
    try:
        found = speaker_registry._backfill_embeddings(diar, segments, device)
    except Exception as exc:
        report.errors.append(f"{md_path.stem}: voices not extracted ({type(exc).__name__})")
        log.warning("Embedding backfill failed for %s", md_path.stem, exc_info=True)
        return
    if not found:
        report.errors.append(f"{md_path.stem}: voices not extracted")
        return
    keep = {label: vec for label, vec in found.items() if label in missing}
    if keep:
        transcript_store.set_speaker_embeddings(md_path, keep, EMBEDDING_SPACE, data)
        report.embeddings.append(md_path.stem)


def _convert(action: Action, output: Path, data: Path, report: TrimReport) -> None:
    from . import transcript_store
    from .audio_utils import encode_flac

    md_path = output / f"{action.stem}.md"
    src = action.path
    before = _size(src)
    target = transcript_store.safe_path(action.stem, ".flac", output)
    if target is None:
        report.errors.append(f"{action.stem}: unsafe transcript name")
        return
    in_place = target.exists() and transcript_store._same_file(src, target)
    if target.exists() and not in_place:
        owner = ("belongs to another transcript"
                 if file_registry.is_registered(target, data_dir=data, output_dir=output)
                 else "already exists and is not this transcript's")
        report.errors.append(f"{action.stem}: {target.name} {owner}; audio left as it is")
        return

    created = not in_place
    try:
        if in_place:
            tmp = output / f"{transcript_store.TEMP_PREFIX}trim-{target.name}"
            try:
                encode_flac(src, tmp)
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
        else:
            encode_flac(src, target)
    except Exception as exc:
        report.errors.append(f"{action.stem}: could not convert ({type(exc).__name__})")
        log.warning("Could not convert audio for %s", action.stem, exc_info=True)
        return
    try:
        transcript_store.set_audio(md_path, target, data_dir=data, output_dir=output)
    except Exception as exc:
        report.errors.append(f"{action.stem}: could not record the new audio ({type(exc).__name__})")
        log.warning("Could not record audio for %s", action.stem, exc_info=True)
        if created:
            target.unlink(missing_ok=True)
        return
    report.converted.append(action.stem)
    report.freed_bytes += before - _size(target)


def _drop_copy(action: Action, output: Path, data: Path, report: TrimReport) -> None:
    from . import recording_manager, transcript_store

    if action.recording_id is None or not recording_manager.combined_path_for(
            action.recording_id, data).is_file():
        return
    before = _size(action.path)
    try:
        transcript_store.set_audio(output / f"{action.stem}.md", None,
                                   data_dir=data, output_dir=output)
    except Exception as exc:
        report.errors.append(f"{action.stem}: could not drop the copy ({type(exc).__name__})")
        return
    if action.path.exists():
        report.errors.append(f"{action.path.name}: could not be deleted")
        return
    report.dropped.append(action.stem or "")
    report.freed_bytes += before


def apply(plan_: Optional[TrimPlan] = None, device: str = "auto",
          progress: Optional[Callable[[str], None]] = None, *,
          data_dir: Optional[Path] = None, output_dir: Optional[Path] = None) -> TrimReport:
    """Run the four actions. The plan is recomputed after reconcile."""
    from . import recording_manager, transcript_store

    def say(msg: str) -> None:
        if progress is not None:
            progress(msg)

    if plan_ is not None:
        data_dir, output_dir = plan_.data_dir, plan_.output_dir
    data, output = file_registry._dirs(data_dir, output_dir)
    report = TrimReport()

    say("Matching renamed transcripts")
    report.reconciled = transcript_store.reconcile(output, data, sweep=True)
    current = plan(data, output)
    report.attention = current.attention

    shrink = [a for a in current.actions if a.kind in (CONVERT, DROP_COPY)]
    for action in shrink:
        md_path = output / f"{action.stem}.md"
        say(f"{KIND_LABELS[action.kind]}: {action.path.name}")
        try:
            _store_missing_embeddings(md_path, action.path, data, device, report)
        except Exception as exc:
            report.errors.append(f"{action.stem}: voices not stored ({type(exc).__name__})")
            log.warning("Could not store voices for %s", action.stem, exc_info=True)
        if action.kind == DROP_COPY:
            _drop_copy(action, output, data, report)
        else:
            _convert(action, output, data, report)

    with db.connection(data) as conn:
        ids = {r["id"] for r in conn.execute("SELECT id FROM recordings")}
    for path in _find_orphans(output, ids, data):
        say(f"delete orphan: {path.name}")
        size = _size(path)
        if transcript_store.delete_unowned_file(path, output, data):
            report.orphans.append(path.name)
            report.freed_bytes += size
        else:
            report.errors.append(f"{path.name}: could not be deleted")

    for action in current.actions:
        if action.kind != TRIM_RECORDING or action.recording_id is None:
            continue
        say(f"trim recording: {action.recording_id}")
        try:
            freed = recording_manager.trim_recording_audio(action.recording_id, data)
        except Exception as exc:
            report.errors.append(
                f"recording {action.recording_id}: could not trim ({type(exc).__name__})")
            continue
        if freed:
            report.trimmed[action.recording_id] = freed
            report.freed_bytes += freed

    return report
