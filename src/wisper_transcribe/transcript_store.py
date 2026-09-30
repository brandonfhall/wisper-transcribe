"""Transcript registry and file lifecycle.

A transcript is ``<output root>/<stem>.md`` plus a row in ``transcripts``
(``wisper.db``). The ``.md`` is the product (edited in Obsidian, synced); the
row is the identity that campaigns, journals, recordings, and jobs link to.
A write that touches both a file and a row can't be atomic, so one ordering
rule applies everywhere:

    the ``.md`` is the existence marker, the row follows it,
    and companion files follow the row.

- Creating: write the ``.md`` (atomically), then :func:`register` the row.
- Deleting (:func:`delete_transcript`, the only delete path): read the
  companion paths, unlink the ``.md``, delete the row (cascades), then unlink
  the companions best-effort.

Every transcript, summary, sidecar, and journal write goes through
:func:`atomic_write_text`, so a crash leaves the old file or the new one,
never a truncated one. Transcript and summary rewrites use
:func:`save_transcript`/:func:`save_summary`, which also update the search
index (a test enforces it).
"""
from __future__ import annotations

import glob
import json
import logging
import os
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from typing import Literal, Optional

from . import db
from .search_index import check_freshness, mark_stale, request_backfill

log = logging.getLogger(__name__)

# Prefix of atomic-write temp files, so reconcile can sweep crash leftovers.
TEMP_PREFIX = ".wisper-tmp-"
# Diarization segments for the enrollment wizard; speakers live in the DB.
SIDECAR_SUFFIX = "_diar.json"

# os.replace() onto a file another process holds open without
# FILE_SHARE_DELETE (Obsidian, antivirus, the search indexer) fails on
# Windows with these codes. Retried with backoff, then written in place.
_WINDOWS_SHARING_ERRORS = {5, 32, 33}
_REPLACE_ATTEMPTS = 8
_REPLACE_FIRST_DELAY_S = 0.01
_IS_WINDOWS = os.name == "nt"


class TranscriptExistsError(FileExistsError):
    """A transcript with this name already exists and overwrite wasn't chosen."""

    def __init__(self, stem: str) -> None:
        super().__init__(f"A transcript named {stem!r} already exists")
        self.stem = stem


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------

def _replace(src: Path, dst: Path) -> bool:
    """``os.replace`` with the Windows sharing-violation retry.

    Returns False when every attempt hit a sharing violation (the caller
    falls back to an in-place write).
    """
    if not _IS_WINDOWS:
        os.replace(src, dst)
        return True
    delay = _REPLACE_FIRST_DELAY_S
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return True
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in _WINDOWS_SHARING_ERRORS:
                raise
            if attempt < _REPLACE_ATTEMPTS - 1:
                time.sleep(delay)
                delay *= 2
    return False


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` via a temp file in the same dir + ``os.replace``.

    A crash mid-write leaves either the old file or the new one. On Windows,
    if another process keeps the target locked through every retry (~1.3 s),
    the text is written in place instead and a warning is logged.
    """
    path = Path(path)
    tmp = path.with_name(f"{TEMP_PREFIX}{path.name}.{os.getpid()}-{threading.get_ident()}")
    try:
        with open(tmp, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if not _replace(tmp, path):
            log.warning("%s is locked by another program; writing it in place", path.name)
            with open(path, "w", encoding=encoding) as fh:
                fh.write(text)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def save_transcript(md_path: Path, text: str) -> None:
    """Rewrite an existing transcript ``.md`` and reindex it for search.

    For edits (renames, refine). A new transcript is written by the pipeline
    and indexed by :func:`register`. Files outside the output root are
    written but not indexed.
    """
    from .search_index import reindex_path
    atomic_write_text(md_path, text)
    reindex_path(md_path)


def save_summary(summary_path: Path, text: str) -> None:
    """Write a ``<stem>.summary.md`` and reindex its transcript for search."""
    from .search_index import SUMMARY_SUFFIX, reindex_path
    atomic_write_text(summary_path, text)
    if Path(summary_path).name.endswith(SUMMARY_SUFFIX):  # not a custom --output name
        reindex_path(summary_path)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def nfc(stem: str) -> str:
    return unicodedata.normalize("NFC", stem)


def existing_form(target: str) -> str:
    """``target``, or its NFD spelling when only that exists on disk.

    Stems are stored NFC. On a normalization-sensitive filesystem (ext4, e.g.
    Docker) a file synced from a Mac can carry the NFD bytes, so the NFC path
    misses it. New files are still written NFC.
    """
    if target.isascii() or os.path.exists(target):
        return target
    head, tail = os.path.split(target)
    alt = os.path.join(head, unicodedata.normalize("NFD", tail))
    return alt if alt != target and os.path.exists(alt) else target


def safe_path(stem: str, suffix: str, output_dir: Optional[Path] = None) -> Optional[Path]:
    """``<output root>/<stem><suffix>``, or None if ``stem`` could escape it.

    Same two-layer guard as the routes (basename, then abspath/startswith):
    CodeQL recognises only ``os.path`` as a sanitiser.
    """
    if not stem or "\x00" in stem:
        return None
    safe = os.path.basename(stem)
    if safe != stem or safe in {".", ".."}:
        return None
    if output_dir is None:
        from .path_utils import get_output_dir
        output_dir = get_output_dir()
    base = os.path.abspath(str(output_dir))
    if not base.endswith(os.sep):
        base += os.sep
    target = existing_form(os.path.abspath(os.path.join(base, f"{safe}{suffix}")))
    if not target.startswith(base):
        return None
    return Path(target)


def _file_timestamp(path: Path) -> str:
    from datetime import UTC, datetime
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def ensure_row(conn: sqlite3.Connection, stem: str, output_dir: Optional[Path] = None) -> int:
    """The registry row for ``stem``, created if absent (inside ``conn``'s txn).

    A new row whose ``.md`` doesn't exist is flagged ``missing_since`` (e.g.
    a campaign association made before the transcript was written).
    """
    stem = nfc(stem)
    row = conn.execute("SELECT id FROM transcripts WHERE stem = ?", (stem,)).fetchone()
    if row is not None:
        return row[0]
    md = safe_path(stem, ".md", output_dir)
    exists = md is not None and md.is_file()
    now = db.now_utc()
    return conn.execute(
        "INSERT INTO transcripts (stem, created_at, missing_since) VALUES (?, ?, ?) RETURNING id",
        (stem, _file_timestamp(md) if exists else now, None if exists else now),
    ).fetchone()[0]


def register(stem: str, *, origin: Literal["job", "reconcile"],
             data_dir: Optional[Path] = None) -> int:
    """Record that ``<stem>.md`` exists under the output root. Returns the row id.

    Call after the ``.md`` is written. An existing row keeps its id, campaign
    position, and journal entries, so an overwrite or re-transcribe keeps its
    links; a row flagged missing is un-flagged. ``origin`` is ``"job"`` when
    the app just wrote the file (an existing, journaled transcript then marks
    that campaign's journal stale), ``"reconcile"`` when it was found on disk.
    """
    if origin not in ("job", "reconcile"):
        raise ValueError(f"invalid origin: {origin!r}")
    stem = nfc(stem)
    stale_sidecar = None
    with db.transaction(data_dir) as conn:
        row = conn.execute(
            "SELECT id, missing_since FROM transcripts WHERE stem = ?", (stem,)
        ).fetchone()
        if row is None:
            tid = ensure_row(conn, stem)
        else:
            tid = row["id"]
            if row["missing_since"] is not None:
                conn.execute("UPDATE transcripts SET missing_since = NULL WHERE id = ?", (tid,))
                if origin == "job":
                    log.info("Reused the name of a previously missing transcript: %s", stem)
            if origin == "job":
                # Overwritten or re-transcribed: a journal that folded the old
                # text now describes something else. Flag it; never un-fold
                # automatically.
                conn.execute(
                    "UPDATE campaigns SET journal_stale_since = coalesce(journal_stale_since, ?) "
                    "WHERE id IN (SELECT campaign_id FROM journal_entries WHERE transcript_id = ?)",
                    (db.now_utc(), tid),
                )
                # The old run's speakers describe the old text. A web job writes
                # fresh ones (write_sidecar) right after; a CLI overwrite has none.
                conn.execute("DELETE FROM transcript_speakers WHERE transcript_id = ?", (tid,))
                stale_sidecar = safe_path(stem, SIDECAR_SUFFIX)
    if stale_sidecar is not None:
        stale_sidecar.unlink(missing_ok=True)
    # The app just wrote this file: index it now. Files found on disk
    # (reconcile) are left to the backfill, so no request parses them.
    if origin == "job":
        _reindex(stem, data_dir)
    return tid


def _reindex(stem: str, data_dir: Optional[Path]) -> None:
    """Index a transcript the app just wrote or relinked; never fails the write."""
    from .search_index import reindex
    try:
        reindex(stem, data_dir=data_dir)
    except Exception:
        log.warning("Search reindex failed for %s", stem, exc_info=True)


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def _companion_paths(stem: str, output_dir: Path,
                     audio_rel_path: Optional[str] = None) -> list[Path]:
    """Every file that belongs to a transcript except its ``.md``.

    The summary, the enrollment sidecar, the source-audio copy (from the
    row's ``audio_rel_path``, or an old sidecar's ``input_path``; only inside
    the output root, since old sidecars point at temp dirs or user files), and
    excerpt clips. Read before anything is deleted, because the audio copy's
    name can't be derived from the stem (collision suffixes).
    """
    paths: list[Path] = []
    summary = safe_path(stem, ".summary.md", output_dir)
    if summary is not None:
        paths.append(summary)
    sidecar = safe_path(stem, SIDECAR_SUFFIX, output_dir)
    if sidecar is not None:
        paths.append(sidecar)
        stored = None
        if audio_rel_path:
            try:
                stored = str(db.from_rel(audio_rel_path, output_dir))
            except ValueError:
                stored = None
        else:
            try:
                stored = json.loads(sidecar.read_text(encoding="utf-8")).get("input_path")
            except (OSError, ValueError, AttributeError):
                stored = None
        if stored:
            base = os.path.abspath(str(output_dir))
            if not base.endswith(os.sep):
                base += os.sep
            candidate = os.path.abspath(stored)
            if candidate.startswith(base) and not candidate.endswith(".md"):
                paths.append(Path(candidate))
    md = safe_path(stem, ".md", output_dir)
    if md is not None:
        # glob.escape: a stem like "mix*" must not match other transcripts' clips.
        paths.extend(output_dir.glob(f"{glob.escape(md.stem)}_excerpt_*"))
    return paths


def delete_transcript(stem: str, data_dir: Optional[Path] = None,
                      output_dir: Optional[Path] = None) -> bool:
    """Delete a transcript, its links, and its companion files.

    The only way a transcript is deleted. Follows the ordering rule: read the
    companion paths, unlink the ``.md``, delete the row in one transaction
    (cascading to campaign and journal rows), then unlink the companions. A
    crash between the last two steps leaves companions with no ``.md`` and
    no row, which reconcile sweeps.

    ``output_dir`` defaults to the output root; routes pass the directory they
    already resolved. Returns False if ``stem`` isn't a safe name; True
    otherwise (deleting a transcript that doesn't exist is not an error).
    """
    if output_dir is None:
        from .path_utils import get_output_dir
        output_dir = get_output_dir()
    md = safe_path(stem, ".md", output_dir)
    if md is None:
        return False
    stem = nfc(md.stem)

    with db.connection(data_dir) as conn:
        row = conn.execute("SELECT audio_rel_path FROM transcripts WHERE stem = ?", (stem,)).fetchone()
    companions = _companion_paths(stem, output_dir, row[0] if row else None)
    try:
        md.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not delete %s: %s", md.name, exc)
        return True  # the file is still there, so the row stays too

    with db.transaction(data_dir) as conn:
        # Cascades to campaign/journal/speaker rows; a recording that produced
        # it goes back to "completed" (recordings.transcript_id SET NULL).
        conn.execute("DELETE FROM transcripts WHERE stem = ?", (stem,))

    for path in companions:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return True


# ---------------------------------------------------------------------------
# Reconcile: the registry vs. what's on disk
# ---------------------------------------------------------------------------

# Leftover atomic-write temp files older than this are crash debris.
_TEMP_MAX_AGE_S = 600
_case_insensitive: dict[str, bool] = {}


def _is_case_insensitive(directory: Path) -> bool:
    """Probe (once per directory) whether names differing only in case collide."""
    key = os.path.realpath(directory)
    if key not in _case_insensitive:
        import tempfile

        fd, probe = tempfile.mkstemp(prefix=f"{TEMP_PREFIX}case-probe-", dir=directory)
        os.close(fd)
        try:
            head, tail = os.path.split(probe)
            _case_insensitive[key] = os.path.exists(os.path.join(head, tail.upper()))
        finally:
            os.unlink(probe)
    return _case_insensitive[key]


def _transcript_files(output_dir: Path) -> dict[str, Path]:
    """NFC stem → path for every transcript ``.md`` in the output root."""
    found: dict[str, Path] = {}
    for md in output_dir.glob("*.md"):
        if md.name.endswith(".summary.md") or md.name.startswith(TEMP_PREFIX):
            continue
        found[nfc(md.stem)] = md
    return found


def _companion_stem(name: str) -> Optional[str]:
    """The transcript stem a pattern-identifiable companion file belongs to."""
    if name.endswith(".summary.md"):
        return name[: -len(".summary.md")]
    if name.endswith("_diar.json"):
        return name[: -len("_diar.json")]
    if "_excerpt_" in name and name.endswith((".mp3", ".txt")):
        return name.rsplit("_excerpt_", 1)[0]
    return None


def reconcile(output_dir: Optional[Path] = None, data_dir: Optional[Path] = None,
              *, sweep: bool = False) -> dict[str, int]:
    """Bring the registry in line with the ``.md`` files on disk.

    - An unregistered ``.md`` gets a row.
    - A row whose ``.md`` is gone is flagged ``missing_since`` but kept, with
      its campaign position and companions: the file may come back (a sync,
      an unmounted drive). A reappearing file clears the flag.
    - On a case-insensitive filesystem, a ``.md`` whose name differs from a
      missing row's only in case renames that row, keeping its links.

    Rows are never deleted here. With ``sweep`` (server startup), also delete
    stale atomic-write temp files and pattern-identifiable companions
    (summary, sidecar, excerpt clips) that have neither a ``.md`` nor a row.
    Only cheap work: no file is parsed. Returns counts for logging.
    """
    if output_dir is None:
        from .path_utils import get_output_dir
        output_dir = get_output_dir()
    files = _transcript_files(output_dir)
    fold = _is_case_insensitive(output_dir)
    counts = {"added": 0, "missing": 0, "restored": 0, "renamed": 0, "swept": 0}
    now = db.now_utc()

    with db.transaction(data_dir) as conn:
        rows = {r["stem"]: r for r in conn.execute("SELECT id, stem, missing_since FROM transcripts")}
        missing_by_fold = {
            stem.casefold(): r for stem, r in rows.items() if stem not in files
        } if fold else {}
        for stem, md in files.items():
            row = rows.get(stem)
            if row is None:
                twin = missing_by_fold.pop(stem.casefold(), None)
                if twin is not None:
                    conn.execute("UPDATE transcripts SET stem = ?, missing_since = NULL WHERE id = ?",
                                 (stem, twin["id"]))
                    mark_stale(conn, [twin["id"]])  # a different file may carry the name now
                    rows.pop(twin["stem"], None)
                    counts["renamed"] += 1
                else:
                    ensure_row(conn, stem, output_dir)
                    counts["added"] += 1
            elif row["missing_since"] is not None:
                conn.execute("UPDATE transcripts SET missing_since = NULL WHERE id = ?", (row["id"],))
                counts["restored"] += 1
        for stem, row in rows.items():
            if stem not in files and row["missing_since"] is None:
                conn.execute("UPDATE transcripts SET missing_since = ? WHERE id = ?", (now, row["id"]))
                counts["missing"] += 1
        registered = {r[0] for r in conn.execute("SELECT stem FROM transcripts")}

    if sweep:
        cutoff = time.time() - _TEMP_MAX_AGE_S
        for entry in output_dir.iterdir():
            try:
                if entry.name.startswith(TEMP_PREFIX):
                    if entry.stat().st_mtime < cutoff:
                        entry.unlink()
                        counts["swept"] += 1
                    continue
                owner = _companion_stem(entry.name)
                if owner is None or not entry.is_file():
                    continue
                owner = nfc(owner)
                if owner not in files and owner not in registered:
                    entry.unlink()
                    counts["swept"] += 1
            except OSError:
                pass
    if any(counts.values()):
        log.info("Transcript reconcile: %s", counts)
    # Files edited outside wisper get reindexed; new ones get their first index.
    try:
        check_freshness(output_dir, data_dir)
    except Exception:
        log.warning("Search freshness check failed", exc_info=True)
    if counts["added"] or counts["renamed"] or counts["restored"]:
        request_backfill()
    return counts


def relink(old_stem: str, new_stem: str, data_dir: Optional[Path] = None,
           output_dir: Optional[Path] = None) -> None:
    """Give a missing transcript's identity to a file under a new name.

    ``old_stem`` must be flagged missing; ``<new_stem>.md`` must exist and
    must not be linked to anything yet (no campaign, no journal entry). The
    old row takes the new name, so its campaign position, journal entry, and
    speakers are kept; the new name's own row (reconcile may have created one)
    is removed.

    Raises ValueError (unsafe name, not missing, or target already linked)
    or KeyError (no such transcript).
    """
    if output_dir is None:
        from .path_utils import get_output_dir
        output_dir = get_output_dir()
    old_md = safe_path(old_stem, ".md", output_dir)
    new_md = safe_path(new_stem, ".md", output_dir)
    if old_md is None or new_md is None:
        raise ValueError("invalid transcript name")
    old_stem, new_stem = nfc(old_md.stem), nfc(new_md.stem)
    if not new_md.is_file():
        raise KeyError(f"No transcript file named {new_stem!r}")
    with db.transaction(data_dir) as conn:
        old = conn.execute(
            "SELECT id, missing_since FROM transcripts WHERE stem = ?", (old_stem,)
        ).fetchone()
        if old is None:
            raise KeyError(f"No transcript named {old_stem!r}")
        if old["missing_since"] is None:
            raise ValueError(f"{old_stem!r} is not missing")
        new = conn.execute("SELECT id FROM transcripts WHERE stem = ?", (new_stem,)).fetchone()
        if new is not None:
            linked = conn.execute(
                "SELECT 1 FROM campaign_transcripts WHERE transcript_id = ? "
                "UNION SELECT 1 FROM journal_entries WHERE transcript_id = ?",
                (new["id"], new["id"]),
            ).fetchone()
            if linked:
                raise ValueError(f"{new_stem!r} already belongs to a campaign")
            conn.execute("DELETE FROM transcripts WHERE id = ?", (new["id"],))
        conn.execute("UPDATE transcripts SET stem = ?, missing_since = NULL WHERE id = ?",
                     (new_stem, old["id"]))
    _reindex(new_stem, data_dir)  # the old identity's index described the old file


def relink_candidates(data_dir: Optional[Path] = None) -> list[str]:
    """Present transcripts not linked to any campaign, newest first — the
    files a missing campaign entry can be relinked to."""
    with db.connection(data_dir) as conn:
        return [r[0] for r in conn.execute(
            "SELECT t.stem FROM transcripts t WHERE t.missing_since IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM campaign_transcripts ct WHERE ct.transcript_id = t.id) "
            "ORDER BY t.created_at DESC, t.stem"
        )]


# ---------------------------------------------------------------------------
# Diarization sidecar: speakers in the DB, segments in <stem>_diar.json
# ---------------------------------------------------------------------------

_SPEAKER_FIELDS = ("speaker_map", "speaker_map_source", "speaker_embeddings",
                   "embedding_space", "input_path", "campaign")


def derived_source(name: str) -> str:
    """Provenance for a label with none recorded (sidecars before
    ``speaker_map_source``): pipeline-shaped names were automatic, anything
    else may have been typed by the user."""
    from .web.enroll_shared import AUTO_NAME_RE
    return "auto" if AUTO_NAME_RE.match(str(name)) else "manual"


def read_sidecar(md_path: Path, data_dir: Optional[Path] = None) -> Optional[dict]:
    """The transcript's diarization data in the JSON-era sidecar shape, or None.

    ``diarization_segments`` comes from ``<stem>_diar.json`` (its only content
    now); ``speaker_map``, ``speaker_map_source``, ``speaker_embeddings`` +
    ``embedding_space``, ``input_path`` (absolute, from the stored relative
    path), and ``campaign`` come from the database. A sidecar still carrying
    the old fields (e.g. synced from an older install) is used as a fallback
    only when the database has nothing for the transcript.
    """
    import numpy as np

    path = Path(md_path).with_name(Path(md_path).stem + SIDECAR_SUFFIX)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    diar: dict = {"diarization_segments": raw.get("diarization_segments") or [], "input_path": ""}
    for key in _SPEAKER_FIELDS:
        if key in raw:
            diar[key] = raw[key]

    with db.connection(data_dir) as conn:
        row = conn.execute(
            "SELECT t.id, t.audio_rel_path, c.slug FROM transcripts t "
            "LEFT JOIN campaign_transcripts ct ON ct.transcript_id = t.id "
            "LEFT JOIN campaigns c ON c.id = ct.campaign_id WHERE t.stem = ?",
            (nfc(Path(md_path).stem),),
        ).fetchone()
        speakers = conn.execute(
            "SELECT label, display_name, source, embedding, embedding_space "
            "FROM transcript_speakers WHERE transcript_id = ? ORDER BY label",
            (row["id"],),
        ).fetchall() if row else []
    if row is None:
        return diar
    diar["campaign"] = row["slug"]
    if not speakers and row["audio_rel_path"] is None:
        return diar  # nothing stored yet: keep any legacy file fields
    diar["speaker_map"] = {s["label"]: s["display_name"] for s in speakers}
    diar["speaker_map_source"] = {s["label"]: s["source"] for s in speakers}
    embedded = [s for s in speakers if s["embedding"] is not None]
    diar.pop("speaker_embeddings", None)
    diar.pop("embedding_space", None)
    if embedded:
        diar["embedding_space"] = embedded[0]["embedding_space"]
        diar["speaker_embeddings"] = {
            s["label"]: np.frombuffer(s["embedding"], dtype=np.float32).tolist()
            for s in embedded if s["embedding_space"] == diar["embedding_space"]
        }
    diar["input_path"] = (
        str(db.from_rel(row["audio_rel_path"], Path(md_path).parent))
        if row["audio_rel_path"] else ""
    )
    return diar


def write_sidecar(md_path: Path, diar: dict, data_dir: Optional[Path] = None) -> None:
    """Store a transcript's diarization data (the sidecar-shaped dict).

    Speakers (name, provenance, embedding) and the source-audio path go to the
    database in one transaction; the segments go to ``<stem>_diar.json``
    after it (companion files follow the row). The ``campaign`` key is
    ignored: the campaign is the transcript's ``campaign_transcripts`` row.
    An audio copy replaced by a different one (re-transcribe) is deleted.
    """
    import numpy as np

    md_path = Path(md_path)
    output_dir = md_path.parent
    stem = nfc(md_path.stem)
    speaker_map = {str(k): str(v) for k, v in (diar.get("speaker_map") or {}).items()}
    sources = dict(diar.get("speaker_map_source") or {})
    space = diar.get("embedding_space")
    embeddings = {}
    if space and isinstance(diar.get("speaker_embeddings"), dict):
        for label, vec in diar["speaker_embeddings"].items():
            arr = np.asarray(vec, dtype=np.float32).reshape(-1)
            if arr.size:
                embeddings[str(label)] = arr
    audio_rel = None
    if diar.get("input_path"):
        try:
            audio_rel = db.to_rel(Path(diar["input_path"]), output_dir)
        except ValueError:
            audio_rel = None  # outside the output root: not ours to track

    with db.transaction(data_dir) as conn:
        tid = ensure_row(conn, stem, output_dir)
        previous = conn.execute(
            "SELECT audio_rel_path FROM transcripts WHERE id = ?", (tid,)
        ).fetchone()[0]
        conn.execute("UPDATE transcripts SET audio_rel_path = ? WHERE id = ?", (audio_rel, tid))
        conn.execute("DELETE FROM transcript_speakers WHERE transcript_id = ?", (tid,))
        for label in sorted(set(speaker_map) | set(embeddings)):
            name = speaker_map.get(label, label)
            source = sources.get(label)
            if source not in ("auto", "manual"):
                source = derived_source(name)
            emb = embeddings.get(label)
            conn.execute(
                "INSERT INTO transcript_speakers (transcript_id, label, display_name, source, "
                "embedding, embedding_space) VALUES (?, ?, ?, ?, ?, ?)",
                (tid, label, name, source,
                 None if emb is None else emb.tobytes(), None if emb is None else space),
            )

    atomic_write_text(
        md_path.with_name(md_path.stem + SIDECAR_SUFFIX),
        json.dumps({"diarization_segments": diar.get("diarization_segments") or []}, indent=2),
    )
    if previous and previous != audio_rel:
        try:
            old = db.from_rel(previous, output_dir)
            if old.is_file() and not old.name.endswith(".md"):
                old.unlink()
        except (OSError, ValueError):
            pass


def set_speaker_names(md_path: Path, names: dict[str, str], sources: dict[str, str],
                      data_dir: Optional[Path] = None) -> None:
    """Record each label's current display name and provenance (wizard renames).

    Only the given labels' names and sources change; embeddings, the audio
    path, and the segments file are left alone. A label with no row yet gets
    one without an embedding. A missing or invalid source is derived from the
    name (see :func:`derived_source`).
    """
    md_path = Path(md_path)
    with db.transaction(data_dir) as conn:
        tid = ensure_row(conn, nfc(md_path.stem), md_path.parent)
        for label, name in names.items():
            source = sources.get(label)
            if source not in ("auto", "manual"):
                source = derived_source(name)
            conn.execute(
                "INSERT INTO transcript_speakers (transcript_id, label, display_name, source) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (transcript_id, label) DO UPDATE SET "
                "display_name = excluded.display_name, source = excluded.source",
                (tid, str(label), str(name), source),
            )


def set_speaker_embeddings(md_path: Path, embeddings: dict, space: str,
                           data_dir: Optional[Path] = None) -> None:
    """Store per-label voice embeddings (the campaign relabel backfill).

    Names are kept; a label with no row yet is named after itself.
    """
    import numpy as np

    md_path = Path(md_path)
    with db.transaction(data_dir) as conn:
        tid = ensure_row(conn, nfc(md_path.stem), md_path.parent)
        for label, vec in embeddings.items():
            blob = np.asarray(vec, dtype=np.float32).reshape(-1).tobytes()
            conn.execute(
                "INSERT INTO transcript_speakers (transcript_id, label, display_name, source, "
                "embedding, embedding_space) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (transcript_id, label) DO UPDATE SET "
                "embedding = excluded.embedding, embedding_space = excluded.embedding_space",
                (tid, str(label), str(label), derived_source(str(label)), blob, space),
            )
