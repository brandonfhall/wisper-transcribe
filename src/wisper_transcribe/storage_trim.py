"""Move sessions into their campaign folders, and convert and delete audio that
older transcripts and recordings don't need.

``plan()`` only reads. ``apply()`` runs five actions in order:

1. reconcile the registry with the output folders, so renames are matched first;
2. move each misplaced session's files into the folder its campaign names, and
   adopt journals still kept in the data dir;
3. for each transcript with an ``audio`` file: store the voice embeddings it
   lacks (while the original audio still exists), then shrink the audio to a
   16 kHz mono ``<stem>.flac`` (or drop it when the recording's
   ``combined.wav`` already covers it);
4. delete orphaned ``<recording-id>.wav`` hand-off copies in the output root;
5. trim each recording to ``combined.wav``.

The plan is recomputed after the moves, so the conversion actions name the
files' new paths.

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

ORGANIZE = "organize"
CONVERT = "convert"
DROP_COPY = "drop_copy"
ORPHAN = "orphan"
TRIM_RECORDING = "trim_recording"

# Order the actions run in (reconcile runs before all of them).
_ORDER = (ORGANIZE, CONVERT, DROP_COPY, ORPHAN, TRIM_RECORDING)
KIND_LABELS = {
    ORGANIZE: "move into campaign folder",
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
    transcript_id: Optional[int] = None
    recording_id: Optional[str] = None
    slug: Optional[str] = None      # campaign actions (a legacy journal's campaign)
    note: str = ""


@dataclass
class TrimPlan:
    output_dir: Path
    data_dir: Path
    actions: list[Action] = field(default_factory=list)
    attention: Optional[object] = None   # transcript_store.Attention
    blocked: list[tuple[str, int, str]] = field(default_factory=list)  # (name, sessions, why)

    @property
    def total_bytes(self) -> int:
        """Bytes the deletions free. A conversion's result size isn't known ahead."""
        return sum(a.size for a in self.actions if a.kind not in (CONVERT, ORGANIZE))

    @property
    def convert_bytes(self) -> int:
        """Current size of the files a conversion replaces."""
        return sum(a.size for a in self.actions if a.kind == CONVERT)

    @property
    def move_bytes(self) -> int:
        """Total bytes of the files an organize action moves."""
        return sum(a.size for a in self.actions if a.kind == ORGANIZE)

    @property
    def moves(self) -> list[Action]:
        """The organize actions, transcripts before journals."""
        return [a for a in self.actions if a.kind == ORGANIZE]


@dataclass
class TrimReport:
    converted: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    trimmed: dict[str, int] = field(default_factory=dict)
    embeddings: list[str] = field(default_factory=list)
    organized: list[str] = field(default_factory=list)
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


def _transcript_bytes(loc) -> int:
    """Total size on disk of a session's registered files (its ``.md`` too)."""
    seen: set[str] = set()
    total = 0
    for path in [loc.md, *loc.companions.values()]:
        if path is None:
            continue
        key = file_registry._key(str(path), loc.fold)
        if key in seen:
            continue
        seen.add(key)
        total += _size(path)
    return total


def _legacy_journals(data: Path) -> list[tuple[str, str, Path]]:
    """``(slug, display_name, path)`` for every journal still in the data dir."""
    from .journal import legacy_journal_path

    with db.connection(data) as conn:
        rows = conn.execute(
            "SELECT slug, display_name FROM campaigns ORDER BY id").fetchall()
    found = []
    for slug, display_name in rows:
        path = legacy_journal_path(slug, data)
        if path.is_file():
            found.append((slug, display_name, path))
    return found


def _blocked_campaigns(attention, data: Path) -> list[tuple[str, int, str]]:
    """Campaigns whose folder is blocked, with a count of their sessions.

    A blocked folder (taken, mid-rename, or gone) makes every session assigned
    to it unorganizable, and a legacy journal can't be adopted either.
    """
    blocked: list[tuple[str, int, str]] = []
    counts: dict[int, int] = {}
    with db.connection(data) as conn:
        for _tid, cid in conn.execute(
                "SELECT id, campaign_id FROM transcripts WHERE campaign_id IS NOT NULL"):
            counts[cid] = counts.get(cid, 0) + 1
    for cid, name, _folder in attention.folder_taken:
        blocked.append((name, counts.get(cid, 0), "folder taken"))
    for cid, name, _folder in attention.missing_folders:
        blocked.append((name, counts.get(cid, 0), "folder missing"))
    for p in attention.pending_folders:
        blocked.append((p.campaign, counts.get(_campaign_id(p.slug, data), 0),
                        "folder rename pending"))
    return blocked


def _campaign_id(slug: str, data: Path) -> Optional[int]:
    with db.connection(data) as conn:
        row = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (slug,)).fetchone()
    return row[0] if row else None


def plan(data_dir: Optional[Path] = None, output_dir: Optional[Path] = None) -> TrimPlan:
    """What :func:`apply` would do. Reads only: no reconcile, no registry writes."""
    from . import recording_manager, transcript_store

    data, output = file_registry._dirs(data_dir, output_dir)
    result = TrimPlan(output_dir=output, data_dir=data)

    rows: list[tuple[str, int, file_registry.FileRow, Optional[str]]] = []
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
                rows.append((t["stem"], t["id"], row, linked.get(t["id"])))

    for stem, tid, row, rec_id in rows:
        if rec_id is not None and recording_manager.existing_combined_path(rec_id, data) is not None:
            result.actions.append(Action(
                DROP_COPY, row.path, _size(row.path), stem=stem, transcript_id=tid,
                recording_id=rec_id, note="the recording's combined FLAC/WAV is the audio"))
        elif not _is_kept_flac(row, stem):
            result.actions.append(Action(CONVERT, row.path, _size(row.path), stem=stem,
                                         transcript_id=tid))

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

    # Organize: one action per misplaced session, plus each legacy journal. The
    # Needs-attention pass already resolved every session's location and left
    # out the ones in a blocked folder (they're reported instead).
    for loc in result.attention.misplaced:
        result.actions.append(Action(
            ORGANIZE, loc.md, _transcript_bytes(loc), stem=loc.stem, transcript_id=loc.id,
            note=f"→ {loc.expected_dir.name}/"))
    for slug, _name, path in _legacy_journals(data):
        result.actions.append(Action(ORGANIZE, path, _size(path), slug=slug, note="journal"))
    result.blocked = _blocked_campaigns(result.attention, data)

    result.actions.sort(key=lambda a: (_ORDER.index(a.kind), str(a.path)))
    return result


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _store_missing_embeddings(loc, audio: Path, data: Path, device: str,
                              report: TrimReport) -> None:
    """Extract and store the voice embeddings a transcript lacks, from ``audio``."""
    from . import speaker_registry, transcript_store
    from .models import DiarizationSegment

    md_path = loc.md
    if md_path is None or not md_path.exists():
        return
    diar = transcript_store.read_sidecar(md_path, data)
    if diar is None or not diar.get("diarization_segments"):
        return
    segments = [DiarizationSegment(start=s["start"], end=s["end"], speaker=s["speaker"])
                for s in diar["diarization_segments"]]
    with db.connection(data) as conn:
        stored = {r["label"]: r["embedding_space"] for r in conn.execute(
            "SELECT label, embedding_space FROM transcript_speakers "
            "WHERE transcript_id = ? AND embedding IS NOT NULL", (loc.id,))}
        named = [r["label"] for r in conn.execute(
            "SELECT label FROM transcript_speakers WHERE transcript_id = ?", (loc.id,))]
    labels = set(named) or {s.speaker for s in segments}
    missing = {label for label in labels if stored.get(label) != EMBEDDING_SPACE}
    if not missing:
        return
    # read_sidecar may resolve a recording to combined.wav; this audio row is the source.
    diar["input_path"] = str(audio)
    try:
        found = speaker_registry._backfill_embeddings(diar, segments, device)
    except Exception as exc:
        report.errors.append(f"{loc.stem}: voices not extracted ({type(exc).__name__})")
        log.warning("Embedding backfill failed for %s", loc.stem, exc_info=True)
        return
    if not found:
        report.errors.append(f"{loc.stem}: voices not extracted")
        return
    keep = {label: vec for label, vec in found.items() if label in missing}
    if keep:
        transcript_store.set_speaker_embeddings(md_path, keep, EMBEDDING_SPACE, data)
        report.embeddings.append(loc.stem)


def _convert(loc, action: Action, output: Path, data: Path, report: TrimReport) -> None:
    from . import transcript_store
    from .audio_utils import encode_flac

    md_path = loc.md
    src = action.path
    before = _size(src)
    target = transcript_store.safe_path(loc.stem, ".flac", loc.dir)
    if target is None:
        report.errors.append(f"{loc.stem}: unsafe transcript name")
        return
    in_place = target.exists() and transcript_store._same_file(src, target)
    if target.exists() and not in_place:
        owner = ("belongs to another transcript"
                 if file_registry.is_registered(target, data_dir=data, output_dir=output)
                 else "already exists and is not this transcript's")
        report.errors.append(f"{loc.stem}: {target.name} {owner}; audio left as it is")
        return

    created = not in_place
    try:
        if in_place:
            tmp = target.parent / f"{transcript_store.TEMP_PREFIX}trim-{target.name}"
            try:
                encode_flac(src, tmp)
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
        else:
            encode_flac(src, target)
    except Exception as exc:
        report.errors.append(f"{loc.stem}: could not convert ({type(exc).__name__})")
        log.warning("Could not convert audio for %s", loc.stem, exc_info=True)
        return
    try:
        transcript_store.set_audio(md_path, target, data_dir=data, output_dir=output)
    except Exception as exc:
        report.errors.append(f"{loc.stem}: could not record the new audio ({type(exc).__name__})")
        log.warning("Could not record audio for %s", loc.stem, exc_info=True)
        if created:
            target.unlink(missing_ok=True)
        return
    report.converted.append(loc.stem)
    report.freed_bytes += before - _size(target)


def _drop_copy(loc, action: Action, output: Path, data: Path, report: TrimReport) -> None:
    from . import recording_manager, transcript_store

    if action.recording_id is None or recording_manager.existing_combined_path(
            action.recording_id, data) is None:
        return
    before = _size(action.path)
    try:
        transcript_store.set_audio(loc.md, None, data_dir=data, output_dir=output)
    except Exception as exc:
        report.errors.append(f"{loc.stem}: could not drop the copy ({type(exc).__name__})")
        return
    if action.path.exists():
        report.errors.append(f"{action.path.name}: could not be deleted")
        return
    report.dropped.append(loc.stem or "")
    report.freed_bytes += before


def _organize(moves: list[Action], data: Path, output: Path, report: TrimReport,
              say: Callable[[str], None]) -> bool:
    """Move misplaced sessions home and adopt legacy journals.

    Returns False (and stops organizing) when the transcripts folder is
    unavailable: nothing else can move then. Every other failure is reported
    and organizing continues.
    """
    from . import journal, transcript_store

    for action in moves:
        say(f"{KIND_LABELS[ORGANIZE]}: {action.path.name}")
        if action.transcript_id is not None:
            outcome = transcript_store.move_files_home(
                action.transcript_id, data_dir=data, output_dir=output)
            if outcome.status in ("moved", "unchanged"):
                report.organized.append(action.stem or action.path.name)
            elif outcome.status == "unavailable":
                report.errors.append("the transcripts folder isn't available")
                return False
            elif outcome.status == "partial":
                kept = ", ".join(p.name for p in outcome.kept)
                report.errors.append(
                    f"{action.stem}: kept in place: {kept or 'some files'}")
            elif outcome.status == "clash":
                folder = action.note.removeprefix("→ ").rstrip("/")
                report.errors.append(
                    f"{action.stem}: a file with that name is already in {folder}")
            elif outcome.status == "reserved":
                report.errors.append(f"{action.stem}: named like the campaign's journal")
            elif outcome.status == "folder_taken":
                report.errors.append(f"{action.stem}: the campaign's folder isn't available")
            else:
                report.errors.append(f"{action.stem}: could not be moved ({outcome.status})")
        elif action.slug is not None:
            result = journal.adopt_legacy_journal(action.slug, data, output)
            if result == "adopted":
                report.organized.append(action.slug)
            elif result == "unavailable":
                report.errors.append("the transcripts folder isn't available")
                return False
            elif result != "none":
                report.errors.append(f"{action.slug}: journal not moved ({result})")
    return True


def apply(plan_: Optional[TrimPlan] = None, device: str = "auto",
          progress: Optional[Callable[[str], None]] = None, *,
          data_dir: Optional[Path] = None, output_dir: Optional[Path] = None) -> TrimReport:
    """Run the actions. The plan is recomputed after reconcile and after organize."""
    from . import campaign_folders, recording_manager, transcript_store

    if plan_ is not None:
        data_dir, output_dir = plan_.data_dir, plan_.output_dir
    data, output = file_registry._dirs(data_dir, output_dir)
    report = TrimReport()

    def say(msg: str) -> None:
        if progress is not None:
            progress(msg)

    say("Matching renamed transcripts")
    report.reconciled = transcript_store.reconcile(output, data, sweep=True)

    say("Finishing pending campaign folder renames")
    campaign_folders.finish_pending_renames(data)

    current = plan(data, output)
    if _organize(current.moves, data, output, report, say):
        # The conversions name the files' new paths, so plan again.
        current = plan(data, output)
    report.attention = current.attention

    shrink = [a for a in current.actions if a.kind in (CONVERT, DROP_COPY)]
    for action in shrink:
        loc = transcript_store.locate(action.transcript_id, data_dir=data, output_dir=output)
        if loc is None:
            report.errors.append(f"transcript {action.transcript_id}: not found")
            continue
        say(f"{KIND_LABELS[action.kind]}: {action.path.name}")
        try:
            _store_missing_embeddings(loc, action.path, data, device, report)
        except Exception as exc:
            report.errors.append(f"{loc.stem}: voices not stored ({type(exc).__name__})")
            log.warning("Could not store voices for %s", loc.stem, exc_info=True)
        if action.kind == DROP_COPY:
            _drop_copy(loc, action, output, data, report)
        else:
            _convert(loc, action, output, data, report)

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
