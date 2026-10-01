"""SQLite storage: connections, transactions, migrations, and guards.

Every connection to ``<data dir>/wisper.db`` goes through :func:`connect` (a
test enforces that no other module calls ``sqlite3.connect``), so every
connection gets the same pragmas:

- ``foreign_keys=ON``: SQLite ships with it off, per connection.
- ``busy_timeout``: wait for another writer instead of failing.
- Rollback journal (``journal_mode=DELETE``): works on any filesystem,
  including Docker Desktop bind mounts, where WAL's shared memory misbehaves.

Connections are ``autocommit=True``. Writes go through :func:`transaction`,
which issues ``BEGIN IMMEDIATE`` so a load-modify-save takes the write lock up
front (a deferred read-then-write gets ``SQLITE_BUSY`` on lock upgrade without
waiting). Nothing is cached across calls except per-process facts: whether the
SQLite library is capable, and which data dirs this process holds a runtime
lease on.

Migrations are versioned by ``PRAGMA user_version``. Each one is DDL plus an
optional import step and runs, with the ``user_version`` bump, in one
``BEGIN IMMEDIATE`` transaction that ``PRAGMA foreign_key_check`` must pass.
Shipped migrations are frozen: a schema change is a new version. A branch
that must reshape unreleased migrations in place sets :data:`SCHEMA_FROZEN`
to ``False``, which makes that build refuse the default data dir.
"""
from __future__ import annotations

import logging
import os
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePath, PurePosixPath
from typing import Optional

log = logging.getLogger(__name__)

DB_FILENAME = "wisper.db"
BUSY_TIMEOUT_MS = 5000
MIN_SQLITE = (3, 43, 0)

# False only on a branch that edits unreleased migrations in place; such a
# build refuses the default data dir. test_db fails on main unless True.
SCHEMA_FROZEN = True

# Runtime lease: a lease older than this is treated as abandoned.
LEASE_TTL_S = 120
HEARTBEAT_INTERVAL_S = 60
# connect() refreshes this process's lease at most this often.
_LEASE_REFRESH_S = 30


class DatabaseError(RuntimeError):
    """The database can't be used. The message is safe to show to the user."""


class UnsupportedSQLite(DatabaseError):
    pass


class SchemaTooNew(DatabaseError):
    pass


class DevDataDirRefused(DatabaseError):
    pass


class RuntimeConflict(DatabaseError):
    pass


class MigrationFailed(DatabaseError):
    pass


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _data_dir(data_dir: Optional[Path]) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    from .config import get_data_dir
    return get_data_dir()


def db_path(data_dir: Optional[Path] = None) -> Path:
    return _data_dir(data_dir) / DB_FILENAME


def now_utc() -> str:
    """Timestamp in the schema's format: ISO-8601 UTC, second precision."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_rel_pure(path: PurePath, root: PurePath) -> str:
    """``path`` relative to ``root`` as a POSIX string. No filesystem access.

    Raises ValueError when ``path`` isn't under ``root``. Pure-path flavour
    rules apply, so Windows paths compare case-insensitively.
    """
    rel = path.relative_to(root)  # ValueError when outside root
    if not rel.parts or ".." in rel.parts:
        raise ValueError(f"{path} is not a file under {root}")
    return PurePosixPath(*rel.parts).as_posix()


def to_rel(path: Path, root: Optional[Path] = None) -> str:
    """Convert a real path under the output root into a stored relative path.

    The only place stored paths are made (the schema's CHECKs reject
    absolute paths, backslashes, and ``..``). Both sides are resolved, so
    symlinks such as macOS ``/var`` → ``/private/var`` don't cause a
    spurious "outside the root".
    """
    if root is None:
        from .path_utils import get_output_dir
        root = get_output_dir()
    return to_rel_pure(
        Path(os.path.realpath(path)), Path(os.path.realpath(root))
    )


def from_rel(rel: str, root: Optional[Path] = None) -> Path:
    """Convert a stored relative path back into a real path under the root."""
    if not rel or "\\" in rel or "\x00" in rel:
        raise ValueError(f"invalid stored path: {rel!r}")
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"invalid stored path: {rel!r}")
    if root is None:
        from .path_utils import get_output_dir
        root = get_output_dir()
    return Path(root).joinpath(*pure.parts)


# ---------------------------------------------------------------------------
# Capability check (once per process)
# ---------------------------------------------------------------------------

_capable: Optional[bool] = None
_FTS5_PROBE = (
    "CREATE VIRTUAL TABLE temp.fts_probe USING fts5(x, content='', contentless_delete=1)"
)


def check_sqlite_capabilities() -> None:
    """Refuse to run on SQLite < 3.43 or without FTS5 (contentless delete)."""
    global _capable
    if _capable:
        return
    have = sqlite3.sqlite_version_info
    if have < MIN_SQLITE:
        raise UnsupportedSQLite(
            f"SQLite {sqlite3.sqlite_version} is too old; wisper needs "
            f"{'.'.join(map(str, MIN_SQLITE))} or newer. Use Python 3.13+ from "
            "python.org or Homebrew, or the Docker image."
        )
    try:
        with closing(sqlite3.connect(":memory:")) as probe:
            probe.execute(_FTS5_PROBE)
    except sqlite3.Error as exc:
        raise UnsupportedSQLite(
            f"This Python's SQLite ({sqlite3.sqlite_version}) was built without "
            "FTS5 full-text search, which wisper needs. Use Python from "
            "python.org or Homebrew, or the Docker image."
        ) from exc
    _capable = True


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

@dataclass
class MigrationContext:
    """What a migration's import step gets besides the connection."""

    data_dir: Path
    version: int
    report: list[str] = field(default_factory=list)
    backup_dir: Optional[Path] = None
    after_commit: list[Callable[[], None]] = field(default_factory=list)

    def note(self, message: str) -> None:
        """Record an import-report line (dirty data skipped or repaired)."""
        self.report.append(message)
        log.warning("migration v%d: %s", self.version, message)

    def backup_legacy(self, paths: list[Path], root: Optional[Path] = None,
                      into: str = "") -> Path:
        """Copy legacy files into a pre-import backup dir before importing.

        Paths are kept relative to ``root`` (default: the data dir) under the
        ``into`` subfolder, e.g. ``root=<output root>, into="output"``.
        """
        import shutil

        dest = self.backup_dir or self.data_dir / "backups" / (
            f"pre-sqlite-v{self.version}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        )
        dest.mkdir(parents=True, exist_ok=True)
        base = root if root is not None else self.data_dir
        for src in paths:
            if not src.exists():
                continue
            rel = src.relative_to(base)
            target = dest / into / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, target, dirs_exist_ok=True)
            else:
                shutil.copy2(src, target)
        self.backup_dir = dest
        return dest


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    ddl: str
    # Imports legacy files after the DDL, in the same transaction.
    import_legacy: Optional[Callable[[sqlite3.Connection, MigrationContext], None]] = None


# --- v1: foundation --------------------------------------------------------

_V1_DDL = """
CREATE TABLE migrations (                      -- append-only log; PRAGMA user_version is authoritative
  version     INTEGER PRIMARY KEY,
  applied_at  TEXT NOT NULL,
  backup_dir  TEXT                             -- relative to the data dir; NULL = nothing backed up
) STRICT;

CREATE TABLE runtime_leases (                  -- host + Docker Desktop container guard
  runtime      TEXT PRIMARY KEY CHECK (runtime IN ('host', 'container')),
  holder       TEXT NOT NULL,                  -- hostname:pid, for the error message
  crosses_vm   INTEGER NOT NULL CHECK (crosses_vm IN (0, 1)),  -- container inside Docker Desktop's VM
  heartbeat_at TEXT NOT NULL,
  CHECK (runtime = 'container' OR crosses_vm = 0)
) STRICT;
"""

# Legacy stores whose presence means "an existing install", not a fresh one.
_LEGACY_MARKERS = ("config.toml", "speakers.json", "campaigns.json", "recordings.json")


def _v1_pin_output_dir(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    """Keep an existing install's CWD ``./output`` as the output root.

    ``get_output_dir()`` used to prefer ``./output`` in the working directory.
    It no longer looks at the CWD, so on upgrade an install whose transcripts
    live in a CWD ``./output`` gets that path pinned into ``output_dir``.
    """
    from .config import load_config, save_config

    if os.environ.get("WISPER_OUTPUT_DIR"):
        return
    if not any((ctx.data_dir / name).exists() for name in _LEGACY_MARKERS):
        return  # fresh install: nothing to preserve
    cwd_output = Path("output")
    if not cwd_output.is_dir():
        return
    resolved = Path(os.path.realpath(cwd_output))
    default = Path(os.path.realpath(ctx.data_dir / "output"))
    if resolved == default:
        return
    cfg = load_config()
    if cfg.get("output_dir"):
        return
    cfg["output_dir"] = str(resolved)
    save_config(cfg)
    ctx.note(f"output_dir pinned to {resolved} (the old working-directory ./output rule)")


# --- v2: profiles, campaigns, transcript registry -------------------------

_V2_DDL = """
CREATE TABLE profiles (
  id                INTEGER PRIMARY KEY,
  key               TEXT NOT NULL UNIQUE,      -- alternate key: URL slug + clip filename; set only by rename_profile()
  display_name      TEXT NOT NULL CHECK (display_name <> ''),
  role              TEXT NOT NULL DEFAULT '',
  notes             TEXT NOT NULL DEFAULT '',
  enrolled_date     TEXT NOT NULL,
  enrollment_source TEXT NOT NULL,
  embedding         BLOB,
  embedding_space   TEXT,
  CHECK ((embedding IS NULL) = (embedding_space IS NULL)),
  CHECK (embedding IS NULL OR length(embedding) % 4 = 0)   -- float32 vector
) STRICT;

CREATE TABLE campaigns (
  id             INTEGER PRIMARY KEY,
  slug           TEXT NOT NULL UNIQUE,
  display_name   TEXT NOT NULL CHECK (display_name <> ''),
  created_at     TEXT NOT NULL,
  journal_sha256 TEXT CHECK (journal_sha256 IS NULL OR length(journal_sha256) = 64),
  journal_stale_since TEXT                   -- journal text mentions a session that left or changed; cleared by rebuild
) STRICT;

CREATE TABLE campaign_members (
  campaign_id     INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  profile_id      INTEGER NOT NULL REFERENCES profiles(id)  ON DELETE CASCADE,
  role            TEXT NOT NULL DEFAULT '',
  character       TEXT NOT NULL DEFAULT '',
  discord_user_id TEXT CHECK (discord_user_id IS NULL
                              OR (discord_user_id <> '' AND discord_user_id NOT GLOB '*[^0-9]*')),
  PRIMARY KEY (campaign_id, profile_id),
  UNIQUE (campaign_id, discord_user_id)       -- one member per Discord account per campaign (NULLs allowed)
) STRICT;
CREATE INDEX campaign_members_profile ON campaign_members(profile_id);

CREATE TABLE transcripts (
  id             INTEGER PRIMARY KEY,
  stem           TEXT NOT NULL UNIQUE CHECK (stem <> '' AND stem NOT GLOB '*[/\\]*'),  -- NFC, relative to the output root
  created_at     TEXT NOT NULL,
  missing_since  TEXT,
  audio_rel_path TEXT CHECK (audio_rel_path IS NULL OR (audio_rel_path NOT GLOB '/*'
                                                  AND audio_rel_path NOT GLOB '*\\*'
                                                  AND '/' || audio_rel_path || '/' NOT GLOB '*/../*'))
) STRICT;

CREATE TABLE campaign_transcripts (           -- 1:N kept as its own relation so "no campaign" needs no NULLs
  transcript_id INTEGER PRIMARY KEY REFERENCES transcripts(id) ON DELETE CASCADE,   -- one campaign per transcript
  campaign_id   INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  position      INTEGER NOT NULL CHECK (position >= 0),
  UNIQUE (campaign_id, position),
  UNIQUE (campaign_id, transcript_id)         -- target of journal_entries' composite FK
) STRICT;
"""


# Legacy files each migration imports, relative to the data dir. A crash
# between an import's commit and its file deletion leaves them behind; the
# next start deletes them (the committed version means they were imported,
# and the backup dir holds copies).
_IMPORTED_LEGACY: dict[int, tuple[str, ...]] = {
    2: ("profiles/speakers.json", "campaigns/campaigns.json", "profiles/embeddings/*.npy"),
    5: ("recordings/recordings.json", "recordings/*/metadata.json"),
}
_cleaned: set[str] = set()


def _finish_legacy_cleanup(data_dir: Path) -> None:
    key = os.path.realpath(data_dir)
    if key in _cleaned:
        return
    _cleaned.add(key)
    path = data_dir / DB_FILENAME
    if not path.exists():
        return
    with closing(_open(path)) as conn:
        version = _user_version(conn)
    for v, patterns in _IMPORTED_LEGACY.items():
        if version < v:
            continue
        for pattern in patterns:
            for leftover in data_dir.glob(pattern):
                try:
                    leftover.unlink()
                    log.info("Removed already-imported legacy file %s", leftover)
                except OSError as exc:
                    log.warning("Could not remove imported legacy file %s: %s", leftover, exc)


def _v2_import(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    from .legacy_import import import_profiles_and_campaigns
    import_profiles_and_campaigns(conn, ctx)


# --- v3: journal entries ---------------------------------------------------

_V3_DDL = """
CREATE TABLE journal_entries (
  transcript_id INTEGER PRIMARY KEY,
  campaign_id   INTEGER NOT NULL,
  folded_at     TEXT NOT NULL,
  FOREIGN KEY (campaign_id, transcript_id)
    REFERENCES campaign_transcripts(campaign_id, transcript_id) ON DELETE CASCADE
    -- ON UPDATE NO ACTION: moving a journaled transcript fails unless its entry is deleted first
) STRICT;
CREATE INDEX journal_entries_campaign ON journal_entries(campaign_id, transcript_id);
CREATE TRIGGER journal_entries_ad AFTER DELETE ON journal_entries BEGIN   -- also fires on FK cascades
  UPDATE campaigns SET journal_stale_since = coalesce(journal_stale_since, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
   WHERE id = old.campaign_id;
END;
"""


def _v3_import(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    from .legacy_import import import_journal_entries
    import_journal_entries(conn, ctx)


# --- v4: per-transcript speakers ------------------------------------------

_V4_DDL = """
CREATE TABLE transcript_speakers (
  transcript_id   INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  label           TEXT NOT NULL,              -- raw pyannote label
  display_name    TEXT NOT NULL,              -- the name as rendered in the .md (not a profile FK, by design)
  source          TEXT NOT NULL CHECK (source IN ('auto', 'manual')),
  embedding       BLOB,
  embedding_space TEXT,
  PRIMARY KEY (transcript_id, label),
  CHECK ((embedding IS NULL) = (embedding_space IS NULL)),
  CHECK (embedding IS NULL OR length(embedding) % 4 = 0)
) STRICT;
"""


def _v4_import(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    from .legacy_import import import_diarization_sidecars
    import_diarization_sidecars(conn, ctx)


# --- v5: recordings ---------------------------------------------------------

_V5_DDL = """
CREATE TABLE recordings (
  id             TEXT PRIMARY KEY CHECK (length(id) = 36),   -- uuid4
  source         TEXT NOT NULL CHECK (source IN ('discord', 'local')),
  name           TEXT,
  notes          TEXT,
  campaign_id    INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
  transcript_id  INTEGER UNIQUE REFERENCES transcripts(id) ON DELETE SET NULL,
  capture_status TEXT NOT NULL CHECK (capture_status IN ('recording', 'degraded', 'completed', 'failed')),
  started_at     TEXT NOT NULL,
  ended_at       TEXT,
  recovered_at   TEXT,
  UNIQUE (id, source),                        -- target of the subtype FKs below
  CHECK (capture_status NOT IN ('recording', 'degraded') OR ended_at IS NULL),
  CHECK (recovered_at IS NULL OR capture_status = 'completed')
) STRICT;
CREATE INDEX recordings_campaign ON recordings(campaign_id);

CREATE TABLE recording_discord (              -- subtype: Discord-only attributes
  recording_id     TEXT PRIMARY KEY,
  source           TEXT NOT NULL DEFAULT 'discord' CHECK (source = 'discord'),
  guild_id         TEXT NOT NULL,
  voice_channel_id TEXT NOT NULL,
  FOREIGN KEY (recording_id, source) REFERENCES recordings(id, source) ON DELETE CASCADE
) STRICT;

CREATE TABLE recording_devices (              -- subtype: local-capture devices (display only)
  recording_id TEXT NOT NULL,
  source       TEXT NOT NULL DEFAULT 'local' CHECK (source = 'local'),
  role         TEXT NOT NULL CHECK (role IN ('mic', 'system')),
  device_name  TEXT NOT NULL,
  PRIMARY KEY (recording_id, role),
  FOREIGN KEY (recording_id, source) REFERENCES recordings(id, source) ON DELETE CASCADE
) STRICT;

CREATE TABLE recording_speakers (             -- Discord users heard; NULL profile = unbound
  recording_id    TEXT NOT NULL REFERENCES recording_discord(recording_id) ON DELETE CASCADE,
  discord_user_id TEXT NOT NULL CHECK (discord_user_id <> '' AND discord_user_id NOT GLOB '*[^0-9]*'),
  profile_id      INTEGER REFERENCES profiles(id) ON DELETE SET NULL,
  PRIMARY KEY (recording_id, discord_user_id)
) STRICT;
CREATE INDEX recording_speakers_profile ON recording_speakers(profile_id);

CREATE TABLE recording_segments (             -- path derived: recordings/<id>/combined/<idx:04d>.wav
  recording_id TEXT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
  idx          INTEGER NOT NULL CHECK (idx >= 0),
  started_at   TEXT NOT NULL,
  duration_s   REAL NOT NULL CHECK (duration_s >= 0),
  finalized    INTEGER NOT NULL CHECK (finalized IN (0, 1)),
  PRIMARY KEY (recording_id, idx)
) STRICT;

CREATE TABLE recording_markers (              -- elapsed = marked_at - recordings.started_at (derived)
  recording_id TEXT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
  marked_at    TEXT NOT NULL,
  PRIMARY KEY (recording_id, marked_at)
) STRICT;

CREATE TABLE recording_rejoins (
  recording_id   TEXT NOT NULL REFERENCES recording_discord(recording_id) ON DELETE CASCADE,
  attempted_at   TEXT NOT NULL,
  close_code     INTEGER NOT NULL,
  attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
  PRIMARY KEY (recording_id, attempted_at)
) STRICT;
"""


def _v5_import(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    from .legacy_import import import_recordings
    import_recordings(conn, ctx)


# --- v6: job history ------------------------------------------------------

_V6_DDL = """
CREATE TABLE jobs (
  id            TEXT PRIMARY KEY CHECK (length(id) = 36),
  type          TEXT NOT NULL CHECK (type IN ('transcription', 'refine', 'summarize', 'enroll',
                                              'live', 'campaign_journal', 'speaker_relabel')),
  status        TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
  created_at    TEXT NOT NULL,
  started_at    TEXT,
  finished_at   TEXT,
  error_code    TEXT,
  transcript_id INTEGER REFERENCES transcripts(id) ON DELETE SET NULL,
  campaign_id   INTEGER REFERENCES campaigns(id)   ON DELETE SET NULL,
  recording_id  TEXT    REFERENCES recordings(id)  ON DELETE SET NULL,
  params_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(params_json) AND json_type(params_json) = 'object'),
  log_tail      TEXT NOT NULL DEFAULT '',
  CHECK (status <> 'pending' OR started_at IS NULL),
  CHECK (status <> 'running' OR started_at IS NOT NULL),
  CHECK ((status IN ('completed', 'failed')) = (finished_at IS NOT NULL)),
  CHECK ((status = 'failed') = (error_code IS NOT NULL))
) STRICT;
CREATE INDEX jobs_created    ON jobs(created_at);
CREATE INDEX jobs_transcript ON jobs(transcript_id);
CREATE INDEX jobs_campaign   ON jobs(campaign_id);
CREATE INDEX jobs_recording  ON jobs(recording_id);
"""


# --- v7: full-text search (derived; rebuildable) ---------------------------

_V7_DDL = """
CREATE TABLE search_index_state (              -- one row per indexed file; no transcript row = not indexed or stale
  transcript_id    INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  kind             TEXT NOT NULL CHECK (kind IN ('transcript', 'summary')),   -- <stem>.md / <stem>.summary.md
  indexed_mtime_ns INTEGER NOT NULL,
  indexed_size     INTEGER NOT NULL CHECK (indexed_size >= 0),
  PRIMARY KEY (transcript_id, kind)
) STRICT;

CREATE TABLE search_blocks (                   -- one speaker block or summary section
  id            INTEGER PRIMARY KEY,           -- = search_fts.rowid
  transcript_id INTEGER NOT NULL,
  kind          TEXT NOT NULL,
  block_idx     INTEGER NOT NULL CHECK (block_idx >= 0),
  speaker       TEXT CHECK (speaker IS NULL OR speaker <> ''),
  start_s       REAL CHECK (start_s IS NULL OR start_s >= 0),
  CHECK (kind = 'transcript' OR (speaker IS NULL AND start_s IS NULL)),
  UNIQUE (transcript_id, kind, block_idx),
  -- A block exists only while the file it came from is indexed: marking a
  -- transcript stale (deleting its state rows) removes its blocks too.
  FOREIGN KEY (transcript_id, kind) REFERENCES search_index_state(transcript_id, kind) ON DELETE CASCADE
) STRICT;
CREATE INDEX search_blocks_speaker ON search_blocks(speaker);

-- No prefix indexes: measured, they doubled the index for ~10 ms on a
-- two-letter prefix query over 21 MB of text.
CREATE VIRTUAL TABLE search_fts USING fts5(
  text, content='', contentless_delete=1,
  tokenize='porter unicode61 remove_diacritics 2');

-- FTS tables can't hold foreign keys; this carries cascades into the index.
CREATE TRIGGER search_blocks_ad AFTER DELETE ON search_blocks BEGIN
  DELETE FROM search_fts WHERE rowid = old.id;
END;
"""


# --- v8: transcript titles in search ---------------------------------------

# A title isn't a block of the transcript's text (block anchors are #b-<n>), so
# it gets its own index over transcripts.stem, kept in step by triggers and
# filled from the existing rows here, so no reindex is needed.
_V8_DDL = """
CREATE VIRTUAL TABLE transcript_titles USING fts5(
  stem, content='transcripts', content_rowid='id',
  tokenize='porter unicode61 remove_diacritics 2');

CREATE TRIGGER transcript_titles_ai AFTER INSERT ON transcripts BEGIN
  INSERT INTO transcript_titles (rowid, stem) VALUES (new.id, new.stem);
END;
CREATE TRIGGER transcript_titles_ad AFTER DELETE ON transcripts BEGIN
  INSERT INTO transcript_titles (transcript_titles, rowid, stem) VALUES ('delete', old.id, old.stem);
END;
CREATE TRIGGER transcript_titles_au AFTER UPDATE OF stem ON transcripts BEGIN
  INSERT INTO transcript_titles (transcript_titles, rowid, stem) VALUES ('delete', old.id, old.stem);
  INSERT INTO transcript_titles (rowid, stem) VALUES (new.id, new.stem);
END;

INSERT INTO transcript_titles (transcript_titles) VALUES ('rebuild');
"""


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "foundation", _V1_DDL, _v1_pin_output_dir),
    Migration(2, "profiles-campaigns", _V2_DDL, _v2_import),
    Migration(3, "journal-entries", _V3_DDL, _v3_import),
    Migration(4, "transcript-speakers", _V4_DDL, _v4_import),
    Migration(5, "recordings", _V5_DDL, _v5_import),
    Migration(6, "jobs", _V6_DDL),
    Migration(7, "search", _V7_DDL),
    Migration(8, "search-titles", _V8_DDL),
)
LATEST_VERSION = MIGRATIONS[-1].version


def _is_default_data_dir(data_dir: Path) -> bool:
    import platformdirs

    from .config import APP_NAME

    default = platformdirs.user_data_dir(APP_NAME)
    return os.path.realpath(data_dir) == os.path.realpath(default)


def _snapshot(path: Path, data_dir: Path, version: int) -> Path:
    """Copy ``wisper.db`` with the backup API. Call while another connection
    holds ``BEGIN IMMEDIATE``: the backup needs only a shared lock."""
    dest_dir = data_dir / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"wisper-v{version}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.db"
    with closing(sqlite3.connect(path)) as src, closing(sqlite3.connect(dest)) as dst:
        src.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        src.backup(dst)
    return dest


def migrate(data_dir: Optional[Path] = None) -> list[int]:
    """Bring the database up to :data:`LATEST_VERSION`. Returns versions applied.

    Safe to call from several processes at once: the loser of the write lock
    waits, then sees the new version and applies nothing.
    """
    data_dir = _data_dir(data_dir)
    check_sqlite_capabilities()
    path = data_dir / DB_FILENAME
    # Checked before anything is opened, so a refused default dir is left
    # exactly as it was (no empty wisper.db).
    if not SCHEMA_FROZEN and _is_default_data_dir(data_dir):
        if path.exists():
            with closing(_open(path)) as probe:
                version = _user_version(probe)
            _check_not_too_new(version)
            if version == LATEST_VERSION:
                return []
        raise DevDataDirRefused(
            "This is an unmerged development build; set WISPER_DATA_DIR to "
            f"a copy of your data. Refusing to migrate {data_dir}."
        )
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = _open(path)
    applied: list[int] = []
    contexts: list[MigrationContext] = []
    try:
        current = _user_version(conn)
        _check_not_too_new(current)
        if current == LATEST_VERSION:
            return applied

        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _user_version(conn)  # re-read under the write lock
            _check_not_too_new(current)
            snapshot = None
            if 0 < current < LATEST_VERSION:
                snapshot = _snapshot(path, data_dir, current)
            conn.execute("PRAGMA defer_foreign_keys=ON")
            for m in MIGRATIONS:
                if m.version <= current:
                    continue
                ctx = MigrationContext(data_dir=data_dir, version=m.version)
                _exec_ddl(conn, m.ddl)
                if m.import_legacy is not None:
                    m.import_legacy(conn, ctx)
                backup = ctx.backup_dir or snapshot
                conn.execute(
                    "INSERT INTO migrations (version, applied_at, backup_dir) VALUES (?, ?, ?)",
                    (m.version, now_utc(),
                     backup.relative_to(data_dir).as_posix() if backup else None),
                )
                contexts.append(ctx)
                applied.append(m.version)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise MigrationFailed(
                    f"Migration left {len(violations)} foreign-key violation(s); "
                    "nothing was changed."
                )
            conn.execute(f"PRAGMA user_version={LATEST_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    _write_report(data_dir, contexts)
    for ctx in contexts:
        for fn in ctx.after_commit:
            try:
                fn()
            except OSError as exc:  # the import committed; cleanup retries next start
                log.warning("migration v%d cleanup failed: %s", ctx.version, exc)
    if applied:
        log.info("Database migrated to version %d (%s)", LATEST_VERSION, path)
    return applied


def _exec_ddl(conn: sqlite3.Connection, ddl: str) -> None:
    """Run a DDL script statement by statement inside the open transaction.

    ``executescript()`` would COMMIT first, so it can't be used here.
    """
    buf = ""
    for line in ddl.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            conn.execute(buf)
            buf = ""
    if buf.strip():
        conn.execute(buf)


def _write_report(data_dir: Path, contexts: list[MigrationContext]) -> None:
    """One ``import-report.txt`` for everything a migration run repaired or
    dropped: in the run's legacy-backup dir, else in ``backups/``."""
    lines = [f"v{ctx.version}: {note}" for ctx in contexts for note in ctx.report]
    if not lines:
        return
    backup_dirs = [ctx.backup_dir for ctx in contexts if ctx.backup_dir is not None]
    if backup_dirs:
        path = backup_dirs[0] / "import-report.txt"
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = data_dir / "backups" / f"import-report-{stamp}.txt"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.warning("Import report: %s", path)
    except OSError as exc:
        log.warning("Could not write import report: %s", exc)


def _check_not_too_new(version: int) -> None:
    if version > LATEST_VERSION:
        raise SchemaTooNew(
            f"The database is schema version {version}, but this version of "
            f"wisper only knows up to {LATEST_VERSION}. Upgrade wisper; an "
            "older build must not write to a newer database."
        )


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def _open(path: Path, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    conn = sqlite3.connect(path, autocommit=True, timeout=busy_timeout_ms / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
        conn.execute("PRAGMA journal_mode=DELETE")
    return conn


def connect(data_dir: Optional[Path] = None, *, migrate_schema: bool = True,
            claim_runtime: bool = True,
            busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open ``wisper.db`` with the standard pragmas. The caller closes it.

    Migrates first when the schema is behind and records this process's
    runtime lease. ``wisper db status|backup|dump`` pass
    ``migrate_schema=False, claim_runtime=False`` so they can inspect a
    database that normal startup would refuse.
    """
    data_dir = _data_dir(data_dir)
    if migrate_schema:
        check_sqlite_capabilities()
        path = data_dir / DB_FILENAME
        needs = True
        if path.exists():
            with closing(_open(path)) as probe:
                version = _user_version(probe)
            _check_not_too_new(version)
            needs = version < LATEST_VERSION
        if needs:
            migrate(data_dir)
        _finish_legacy_cleanup(data_dir)
    else:
        data_dir.mkdir(parents=True, exist_ok=True)
    conn = _open(data_dir / DB_FILENAME, busy_timeout_ms)
    if claim_runtime and migrate_schema:
        try:
            _refresh_lease(conn, data_dir)
        except BaseException:
            conn.close()
            raise
    return conn


@contextmanager
def connection(data_dir: Optional[Path] = None, **kwargs) -> Iterator[sqlite3.Connection]:
    """A connection for reads, closed on exit. No transaction is opened."""
    conn = connect(data_dir, **kwargs)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(data_dir: Optional[Path] = None, *,
                busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` … ``COMMIT`` on a fresh connection, closed on exit.

    Rolls back on any exception. Never hold one across ML or LLM work:
    compute first, then write in a short transaction. The capture hot path
    passes a short ``busy_timeout_ms`` so a busy database can't stall audio.
    """
    conn = connect(data_dir, busy_timeout_ms=busy_timeout_ms)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Runtime guard (host process vs Docker Desktop container on one data dir)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuntimeInfo:
    runtime: str        # 'host' | 'container'
    crosses_vm: bool    # container running inside Docker Desktop's VM


def detect_runtime() -> RuntimeInfo:
    if not Path("/.dockerenv").exists():
        return RuntimeInfo("host", False)
    try:
        kernel = Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        kernel = ""
    return RuntimeInfo("container", "linuxkit" in kernel.lower())


def _holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


# Resolved data dir → monotonic time of this process's last lease write.
_lease_refreshed: dict[str, float] = {}
_lease_lock = threading.Lock()


def _age_s(stamp: str) -> float:
    try:
        then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return float("inf")
    return (datetime.now(UTC) - then).total_seconds()


def _refresh_lease(conn: sqlite3.Connection, data_dir: Path, *, force: bool = False) -> None:
    key = os.path.realpath(data_dir)
    with _lease_lock:
        last = _lease_refreshed.get(key)
        if not force and last is not None and time.monotonic() - last < _LEASE_REFRESH_S:
            return
    me = detect_runtime()
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = {r["runtime"]: r for r in conn.execute("SELECT * FROM runtime_leases")}
        other = rows.get("container" if me.runtime == "host" else "host")
        if other is not None and _age_s(other["heartbeat_at"]) < LEASE_TTL_S:
            container_crosses = (
                me.crosses_vm if me.runtime == "container"
                else bool(rows["container"]["crosses_vm"])
            )
            if container_crosses:
                raise RuntimeConflict(
                    f"The database in {data_dir} is in use by a "
                    f"{other['runtime']} process ({other['holder']}). A host process "
                    "and a Docker Desktop container writing one database at the same "
                    "time corrupts it. Use Docker for everything (e.g. "
                    "`docker compose run wisper …`), or stop the container and use "
                    "the local CLI and `wisper server`."
                )
        conn.execute(
            "INSERT INTO runtime_leases (runtime, holder, crosses_vm, heartbeat_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (runtime) DO UPDATE SET "
            "holder = excluded.holder, crosses_vm = excluded.crosses_vm, "
            "heartbeat_at = excluded.heartbeat_at",
            (me.runtime, _holder(), int(me.crosses_vm), now_utc()),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    with _lease_lock:
        _lease_refreshed[key] = time.monotonic()


def release_runtime(data_dir: Optional[Path] = None) -> None:
    """Drop this process's lease on clean exit (only if it's still ours)."""
    data_dir = _data_dir(data_dir)
    key = os.path.realpath(data_dir)
    with _lease_lock:
        held = _lease_refreshed.pop(key, None)
    if held is None or not (data_dir / DB_FILENAME).exists():
        return
    try:
        with closing(_open(data_dir / DB_FILENAME)) as conn:
            conn.execute(
                "DELETE FROM runtime_leases WHERE runtime = ? AND holder = ?",
                (detect_runtime().runtime, _holder()),
            )
    except sqlite3.Error as exc:
        log.debug("Could not release runtime lease: %s", exc)


class Heartbeat:
    """Refresh this process's runtime lease every minute (server, long CLI jobs).

    ``connect()`` never starts one: short commands refresh lazily as they
    connect, and a thread per connect would leak.
    """

    def __init__(self, data_dir: Optional[Path] = None,
                 interval_s: float = HEARTBEAT_INTERVAL_S) -> None:
        self._data_dir = _data_dir(data_dir)
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "Heartbeat":
        self._thread = threading.Thread(
            target=self._run, name="wisper-db-heartbeat", daemon=True
        )
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with closing(_open(self._data_dir / DB_FILENAME)) as conn:
                    _refresh_lease(conn, self._data_dir, force=True)
            except (sqlite3.Error, DatabaseError, OSError) as exc:
                log.warning("Runtime lease refresh failed: %s", exc)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        release_runtime(self._data_dir)

    def __enter__(self) -> "Heartbeat":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Inspection (wisper db status | backup | dump)
# ---------------------------------------------------------------------------

def _normalized_schema(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    return [(t, n, " ".join(s.split())) for t, n, s in rows]


def expected_schema(version: int) -> list[tuple[str, str, str]]:
    """The schema this build creates at ``version`` (DDL only, in memory)."""
    with closing(sqlite3.connect(":memory:", autocommit=True)) as mem:
        for m in MIGRATIONS:
            if m.version <= version:
                _exec_ddl(mem, m.ddl)
        return _normalized_schema(mem)


@dataclass
class Status:
    path: Path
    exists: bool
    size_bytes: int = 0
    version: int = 0
    latest: int = LATEST_VERSION
    sqlite_version: str = sqlite3.sqlite_version
    capability_error: Optional[str] = None
    integrity: list[str] = field(default_factory=list)
    fk_violations: int = 0
    schema_drift: bool = False
    leases: list[dict] = field(default_factory=list)
    migrations: list[dict] = field(default_factory=list)
    frozen: bool = SCHEMA_FROZEN


def status(data_dir: Optional[Path] = None) -> Status:
    """Read-only health report. Never migrates, never claims a lease."""
    data_dir = _data_dir(data_dir)
    path = data_dir / DB_FILENAME
    st = Status(path=path, exists=path.exists())
    try:
        check_sqlite_capabilities()
    except UnsupportedSQLite as exc:
        st.capability_error = str(exc)
    if not st.exists:
        return st
    st.size_bytes = path.stat().st_size
    with closing(_open(path)) as conn:
        st.version = _user_version(conn)
        st.integrity = [r[0] for r in conn.execute("PRAGMA integrity_check")]
        st.fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        if st.version <= LATEST_VERSION:
            st.schema_drift = _normalized_schema(conn) != expected_schema(st.version)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "runtime_leases" in tables:
            st.leases = [
                dict(r) | {"age_s": _age_s(r["heartbeat_at"])}
                for r in conn.execute("SELECT * FROM runtime_leases ORDER BY runtime")
            ]
        if "migrations" in tables:
            st.migrations = [dict(r) for r in conn.execute("SELECT * FROM migrations ORDER BY version")]
    return st


def backup(data_dir: Optional[Path] = None, dest: Optional[Path] = None) -> Path:
    """Consistent copy of ``wisper.db`` via the backup API (safe while running)."""
    data_dir = _data_dir(data_dir)
    path = data_dir / DB_FILENAME
    if not path.exists():
        raise DatabaseError(f"No database at {path}")
    if dest is None:
        dest = data_dir / "backups" / (
            f"wisper-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.db"
        )
    dest = Path(dest)
    if dest.exists():
        raise DatabaseError(f"{dest} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with closing(_open(path)) as src, closing(sqlite3.connect(dest)) as dst:
        src.backup(dst)
    return dest


def dump(data_dir: Optional[Path] = None) -> Iterator[str]:
    """The database as SQL text, one statement per item."""
    data_dir = _data_dir(data_dir)
    path = data_dir / DB_FILENAME
    if not path.exists():
        raise DatabaseError(f"No database at {path}")
    with closing(_open(path)) as conn:
        yield from conn.iterdump()
