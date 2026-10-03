"""Tests for db.py: connections, migrations, guards, and path conversion."""
from __future__ import annotations

import multiprocessing
import os
import re
import sqlite3
import threading
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from click.testing import CliRunner

from wisper_transcribe import db

SRC = Path(__file__).parent.parent / "src" / "wisper_transcribe"
_real_detect_runtime = db.detect_runtime


@pytest.fixture(autouse=True)
def _reset_process_state(monkeypatch):
    """Per-process caches must not leak between tests."""
    monkeypatch.setattr(db, "_capable", None)
    monkeypatch.setattr(db, "_lease_refreshed", {})
    monkeypatch.setattr(db, "_cleaned", set())
    monkeypatch.setattr(db, "detect_runtime", lambda: db.RuntimeInfo("host", False))


@pytest.fixture
def data_dir():
    return db._data_dir(None)


NEXT = db.LATEST_VERSION + 1  # version number for fake migrations


def _fake_migration(monkeypatch, *extra: db.Migration) -> None:
    migrations = db.MIGRATIONS + extra
    monkeypatch.setattr(db, "MIGRATIONS", migrations)
    monkeypatch.setattr(db, "LATEST_VERSION", migrations[-1].version)


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def test_fresh_install_migrates_to_latest(data_dir):
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.LATEST_VERSION
        versions = [r[0] for r in conn.execute("SELECT version FROM migrations")]
    assert versions == [m.version for m in db.MIGRATIONS]
    assert (data_dir / db.DB_FILENAME).exists()


def test_rerun_is_noop(data_dir):
    assert db.migrate() == [m.version for m in db.MIGRATIONS]
    assert db.migrate() == []
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM migrations").fetchone()[0] == len(db.MIGRATIONS)


def test_every_connection_has_standard_pragmas():
    conn = db.connect()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.autocommit is True
        assert not conn.in_transaction
    finally:
        conn.close()


def test_transaction_commits_and_rolls_back():
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO runtime_leases VALUES ('container', 'x:1', 0, ?)", (db.now_utc(),)
        )
    with pytest.raises(ZeroDivisionError):
        with db.transaction() as conn:
            conn.execute("DELETE FROM runtime_leases WHERE runtime = 'container'")
            1 / 0
    with db.connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM runtime_leases WHERE runtime = 'container'"
        ).fetchone()[0] == 1


def test_begin_immediate_second_writer_waits():
    db.migrate()
    order: list[str] = []
    holding = threading.Event()

    def first():
        with db.transaction() as conn:
            conn.execute("INSERT INTO migrations VALUES (100, 'a', NULL)")
            holding.set()
            time.sleep(0.3)
            order.append("first-commit")

    def second():
        holding.wait(5)
        with db.transaction() as conn:
            order.append("second-start")
            conn.execute("INSERT INTO migrations VALUES (101, 'b', NULL)")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()
    assert order == ["first-commit", "second-start"]


def _race_worker(data_dir: str, queue) -> None:
    from wisper_transcribe import db as child_db
    try:
        queue.put(child_db.migrate(Path(data_dir)))
    except Exception as exc:  # reported to the parent
        queue.put(repr(exc))


def test_concurrent_migrations_across_processes_apply_once(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_race_worker, args=(str(tmp_path), queue)) for _ in range(4)]
    for p in procs:
        p.start()
    results = [queue.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0
    assert sorted(map(str, results)) == sorted(
        [str([m.version for m in db.MIGRATIONS])] + ["[]"] * 3
    )
    with sqlite3.connect(tmp_path / db.DB_FILENAME) as conn:
        assert conn.execute("SELECT count(*) FROM migrations").fetchone()[0] == len(db.MIGRATIONS)


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------

def test_failed_migration_rolls_back_everything(monkeypatch, data_dir):
    db.migrate()

    def boom(conn, ctx):
        raise RuntimeError("import failed")

    _fake_migration(monkeypatch, db.Migration(NEXT, "boom", "CREATE TABLE t99 (x INTEGER) STRICT;", boom))
    with pytest.raises(RuntimeError, match="import failed"):
        db.migrate()
    with sqlite3.connect(data_dir / db.DB_FILENAME) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == NEXT - 1
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 't99'").fetchone()[0] == 0
        assert conn.execute("SELECT max(version) FROM migrations").fetchone()[0] == NEXT - 1


def test_foreign_key_violation_rolls_back(monkeypatch, data_dir):
    ddl = """
    CREATE TABLE fk_parent (id INTEGER PRIMARY KEY) STRICT;
    CREATE TABLE fk_child (pid INTEGER NOT NULL REFERENCES fk_parent(id)) STRICT;
    """

    def dangling(conn, ctx):
        conn.execute("INSERT INTO fk_child VALUES (7)")  # deferred: no parent 7

    _fake_migration(monkeypatch, db.Migration(NEXT, "dangling", ddl, dangling))
    with pytest.raises(db.MigrationFailed):
        db.migrate()
    with sqlite3.connect(data_dir / db.DB_FILENAME) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0


def test_deferred_foreign_keys_allow_any_import_order(monkeypatch):
    ddl = """
    CREATE TABLE fk_parent (id INTEGER PRIMARY KEY) STRICT;
    CREATE TABLE fk_child (pid INTEGER NOT NULL REFERENCES fk_parent(id)) STRICT;
    """

    def child_first(conn, ctx):
        conn.execute("INSERT INTO fk_child VALUES (1)")
        conn.execute("INSERT INTO fk_parent VALUES (1)")

    _fake_migration(monkeypatch, db.Migration(NEXT, "order", ddl, child_first))
    assert db.migrate() == list(range(1, NEXT + 1))


def test_upgrade_snapshots_existing_db(monkeypatch, data_dir):
    db.migrate()
    _fake_migration(monkeypatch, db.Migration(NEXT, "more", "CREATE TABLE t2 (x INTEGER) STRICT;"))
    assert db.migrate() == [NEXT]
    with db.connection() as conn:
        backup_dir = conn.execute("SELECT backup_dir FROM migrations WHERE version = ?", (NEXT,)).fetchone()[0]
    snap = data_dir / backup_dir
    assert snap.exists()
    with sqlite3.connect(snap) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == NEXT - 1


def test_import_report_and_after_commit(monkeypatch, data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    legacy = data_dir / "legacy.json"
    legacy.write_text("{}", encoding="utf-8")

    def imp(conn, ctx):
        ctx.backup_legacy([legacy])
        ctx.note("skipped one bad row")
        ctx.after_commit.append(lambda: legacy.unlink())

    _fake_migration(monkeypatch, db.Migration(NEXT, "imp", "", imp))
    db.migrate()
    assert not legacy.exists()
    backups = list((data_dir / "backups").glob(f"pre-sqlite-v{NEXT}-*"))
    assert len(backups) == 1
    assert (backups[0] / "legacy.json").exists()
    assert "skipped one bad row" in (backups[0] / "import-report.txt").read_text(encoding="utf-8")


def test_downgrade_guard_refuses_newer_db(data_dir):
    db.migrate()
    with sqlite3.connect(data_dir / db.DB_FILENAME) as conn:
        conn.execute(f"PRAGMA user_version={db.LATEST_VERSION + 1}")
    with pytest.raises(db.SchemaTooNew, match="Upgrade wisper"):
        db.connect()
    st = db.status()  # inspection still works
    assert st.version == db.LATEST_VERSION + 1


# ---------------------------------------------------------------------------
# Schema constraints (foundation tables)
# ---------------------------------------------------------------------------

def test_runtime_leases_constraints():
    with db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):  # host can't cross the VM
            conn.execute("INSERT INTO runtime_leases VALUES ('host', 'h:1', 1, 'x')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO runtime_leases VALUES ('laptop', 'h:1', 0, 'x')")
        with pytest.raises(sqlite3.IntegrityError):  # STRICT rejects the wrong type
            conn.execute("INSERT INTO runtime_leases VALUES ('container', 'h:1', 'yes', 'x')")


def test_every_table_is_strict():
    """Every ordinary table is STRICT. FTS5's virtual and shadow tables
    (``search_fts*``, ``transcript_titles*``) can't be, and PRAGMA
    table_list types them apart."""
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT name, type, strict FROM pragma_table_list WHERE schema = 'main' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    assert {r["name"] for r in rows if r["type"] == "virtual"} == {"search_fts", "transcript_titles"}
    for r in rows:
        if r["type"] == "table":
            assert r["strict"] == 1, r["name"]
        else:
            assert r["name"].startswith(("search_fts", "transcript_titles")), r["name"]


# ---------------------------------------------------------------------------
# Capability check
# ---------------------------------------------------------------------------

def test_capability_check_refuses_old_sqlite(monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 42, 0))
    with pytest.raises(db.UnsupportedSQLite, match="too old"):
        db.connect()


def test_capability_check_refuses_missing_fts5(monkeypatch):
    monkeypatch.setattr(db, "_FTS5_PROBE", "CREATE VIRTUAL TABLE temp.p USING no_such_module(x)")
    with pytest.raises(db.UnsupportedSQLite, match="FTS5"):
        db.connect()


def test_capability_check_passes_here():
    db.check_sqlite_capabilities()


# ---------------------------------------------------------------------------
# Dev-data guard
# ---------------------------------------------------------------------------

@pytest.fixture
def default_data_dir(tmp_path, monkeypatch):
    """Make a temp dir *be* the platform default, with no WISPER_DATA_DIR."""
    import platformdirs

    fake_default = tmp_path / "default-data"
    monkeypatch.delenv("WISPER_DATA_DIR", raising=False)
    monkeypatch.setattr(platformdirs, "user_data_dir", lambda *a, **k: str(fake_default))
    return fake_default


def test_dev_guard_refuses_default_dir_while_unfrozen(monkeypatch, default_data_dir):
    monkeypatch.setattr(db, "SCHEMA_FROZEN", False)
    with pytest.raises(db.DevDataDirRefused, match="WISPER_DATA_DIR"):
        db.connect()
    assert not (default_data_dir / db.DB_FILENAME).exists()


def test_dev_guard_catches_override_pointing_at_default(monkeypatch, default_data_dir):
    monkeypatch.setattr(db, "SCHEMA_FROZEN", False)
    monkeypatch.setenv("WISPER_DATA_DIR", str(default_data_dir))
    with pytest.raises(db.DevDataDirRefused):
        db.connect()


def test_dev_guard_allows_default_dir_when_frozen(monkeypatch, default_data_dir):
    monkeypatch.setattr(db, "SCHEMA_FROZEN", True)
    db.connect().close()
    assert (default_data_dir / db.DB_FILENAME).exists()


def test_dev_guard_allows_override(monkeypatch, tmp_path, default_data_dir):
    monkeypatch.setattr(db, "SCHEMA_FROZEN", False)
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path / "copy"))
    db.connect().close()


def test_schema_frozen_on_main():
    """main must never ship SCHEMA_FROZEN = False (a branch-only escape hatch)."""
    base = os.environ.get("GITHUB_BASE_REF", "")
    ref = os.environ.get("GITHUB_REF", "")
    if base != "main" and ref != "refs/heads/main":
        pytest.skip("only enforced for main and pull requests into main")
    assert db.SCHEMA_FROZEN is True


# ---------------------------------------------------------------------------
# Runtime guard
# ---------------------------------------------------------------------------

def _seed_lease(runtime: str, crosses_vm: bool, age_s: float = 0) -> None:
    from datetime import UTC, datetime, timedelta

    stamp = (datetime.now(UTC) - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.migrate()
    with sqlite3.connect(db.db_path()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO runtime_leases VALUES (?, 'other-host:42', ?, ?)",
            (runtime, int(crosses_vm), stamp),
        )


def test_host_refused_while_docker_desktop_container_active():
    _seed_lease("container", crosses_vm=True)
    with pytest.raises(db.RuntimeConflict, match="other-host:42"):
        db.connect()


def test_docker_desktop_container_refused_while_host_active(monkeypatch):
    _seed_lease("host", crosses_vm=False)
    monkeypatch.setattr(db, "detect_runtime", lambda: db.RuntimeInfo("container", True))
    with pytest.raises(db.RuntimeConflict):
        db.connect()


def test_stale_lease_is_ignored():
    _seed_lease("container", crosses_vm=True, age_s=db.LEASE_TTL_S + 60)
    db.connect().close()


def test_native_linux_container_never_blocks():
    _seed_lease("container", crosses_vm=False)
    db.connect().close()


def test_native_linux_container_not_blocked_by_host(monkeypatch):
    _seed_lease("host", crosses_vm=False)
    monkeypatch.setattr(db, "detect_runtime", lambda: db.RuntimeInfo("container", False))
    db.connect().close()


def test_same_runtime_shares_lease():
    _seed_lease("host", crosses_vm=False)
    db.connect().close()
    with db.connection() as conn:
        holder = conn.execute("SELECT holder FROM runtime_leases WHERE runtime = 'host'").fetchone()[0]
    assert holder == db._holder()


def test_release_only_deletes_own_lease():
    db.connect().close()
    db.release_runtime()
    with db.connection(migrate_schema=False, claim_runtime=False) as conn:
        assert conn.execute("SELECT count(*) FROM runtime_leases").fetchone()[0] == 0

    _seed_lease("host", crosses_vm=False)  # another host process took the row
    db._lease_refreshed[os.path.realpath(db._data_dir(None))] = time.monotonic()
    db.release_runtime()
    with db.connection(migrate_schema=False, claim_runtime=False) as conn:
        assert conn.execute("SELECT holder FROM runtime_leases").fetchone()[0] == "other-host:42"


def test_heartbeat_refreshes_and_releases():
    db.connect().close()
    hb = db.Heartbeat(interval_s=0.05).start()
    time.sleep(0.2)
    hb.stop()
    assert hb._thread is not None and not hb._thread.is_alive()
    with db.connection(migrate_schema=False, claim_runtime=False) as conn:
        assert conn.execute("SELECT count(*) FROM runtime_leases").fetchone()[0] == 0


def test_detect_runtime(monkeypatch, tmp_path):
    real_exists = Path.exists
    proc_version = tmp_path / "version"

    # Compare as Paths: on Windows str(Path("/.dockerenv")) is "\\.dockerenv".
    def fake_exists(self):
        return True if self == Path("/.dockerenv") else real_exists(self)

    real_read = Path.read_text

    def fake_read(self, *a, **k):
        return proc_version.read_text() if self == Path("/proc/version") else real_read(self, *a, **k)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(Path, "read_text", fake_read)
    proc_version.write_text("Linux version 6.10.14-linuxkit (root@buildkitsandbox)")
    assert _real_detect_runtime() == db.RuntimeInfo("container", True)
    proc_version.write_text("Linux version 6.8.0-45-generic (buildd@lcy02-amd64)")
    assert _real_detect_runtime() == db.RuntimeInfo("container", False)


# ---------------------------------------------------------------------------
# Stored relative paths
# ---------------------------------------------------------------------------

def test_to_rel_from_rel_round_trip(tmp_path):
    root = tmp_path / "out"
    target = root / "sub" / "Session 1 — The Keep.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    rel = db.to_rel(target, root)
    assert rel == "sub/Session 1 — The Keep.wav"
    assert os.path.realpath(db.from_rel(rel, root)) == os.path.realpath(target)


def test_to_rel_resolves_symlinked_root(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:  # Windows without Developer Mode or admin
        pytest.skip("symlinks not permitted")
    (real / "a.md").write_text("x")
    assert db.to_rel(link / "a.md", real) == "a.md"
    assert db.to_rel(real / "a.md", link) == "a.md"


def test_to_rel_rejects_outside_root(tmp_path):
    with pytest.raises(ValueError):
        db.to_rel(tmp_path / "elsewhere.wav", tmp_path / "out")
    with pytest.raises(ValueError):
        db.to_rel(tmp_path / "out", tmp_path / "out")  # the root itself


@pytest.mark.parametrize("bad", ["", "/etc/passwd", "../x.wav", "a/../../x", "a\\b.wav", "a\x00b"])
def test_from_rel_rejects_unsafe(bad, tmp_path):
    with pytest.raises(ValueError):
        db.from_rel(bad, tmp_path)


def test_windows_paths_convert_case_insensitively():
    rel = db.to_rel_pure(PureWindowsPath(r"C:\Users\Me\Output\Sub\a.md"),
                         PureWindowsPath(r"c:\users\me\output"))
    assert rel == "Sub/a.md"
    with pytest.raises(ValueError):
        db.to_rel_pure(PureWindowsPath(r"D:\Output\a.md"), PureWindowsPath(r"C:\Output"))


def test_posix_paths_are_case_sensitive():
    with pytest.raises(ValueError):
        db.to_rel_pure(PurePosixPath("/Out/a.md"), PurePosixPath("/out"))


# ---------------------------------------------------------------------------
# Inspection and CLI
# ---------------------------------------------------------------------------

def test_status_detects_schema_drift(data_dir):
    db.migrate()
    assert db.status().schema_drift is False
    with sqlite3.connect(data_dir / db.DB_FILENAME) as conn:
        conn.execute("CREATE TABLE from_an_old_branch (x INTEGER) STRICT")
    assert db.status().schema_drift is True


def test_status_without_db_does_not_create_it(data_dir):
    st = db.status()
    assert st.exists is False
    assert not (data_dir / db.DB_FILENAME).exists()


def test_cli_db_status_backup_dump(tmp_path, data_dir):
    from wisper_transcribe.cli import main

    runner = CliRunner()
    db.connect().close()
    result = runner.invoke(main, ["db", "status"])
    assert result.exit_code == 0, result.output
    assert f"Schema version : {db.LATEST_VERSION}" in result.output
    assert "Integrity      : ok" in result.output

    dest = tmp_path / "copy.db"
    result = runner.invoke(main, ["db", "backup", str(dest)])
    assert result.exit_code == 0, result.output
    with sqlite3.connect(dest) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.LATEST_VERSION

    result = runner.invoke(main, ["db", "backup", str(dest)])
    assert result.exit_code != 0 and "already exists" in result.output

    result = runner.invoke(main, ["db", "dump"])
    assert result.exit_code == 0
    assert "CREATE TABLE migrations" in result.output


def test_cli_reports_database_refusal_cleanly(data_dir):
    from wisper_transcribe.cli import main

    db.migrate()
    with sqlite3.connect(data_dir / db.DB_FILENAME) as conn:
        conn.execute(f"PRAGMA user_version={db.LATEST_VERSION + 1}")
    result = CliRunner().invoke(main, ["server"])
    assert result.exit_code != 0
    assert "Upgrade wisper" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# Guards over the codebase
# ---------------------------------------------------------------------------

def test_only_db_module_opens_sqlite():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "db.py" and path.parent == SRC:
            continue
        if re.search(r"\bsqlite3\s*\.\s*connect\s*\(", path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == [], f"use wisper_transcribe.db.connect(): {offenders}"


# ---------------------------------------------------------------------------
# v1: pin an existing install's CWD ./output into output_dir
# ---------------------------------------------------------------------------

def _existing_install(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "speakers.json").write_text("{}", encoding="utf-8")


def test_upgrade_pins_cwd_output(tmp_path, monkeypatch, data_dir):
    from wisper_transcribe.config import load_config
    from wisper_transcribe.path_utils import get_output_dir

    _existing_install(data_dir)
    (tmp_path / "output").mkdir()
    monkeypatch.chdir(tmp_path)
    db.migrate()
    assert load_config()["output_dir"] == os.path.realpath(tmp_path / "output")
    monkeypatch.chdir(data_dir)  # launched from elsewhere: same root
    assert os.path.realpath(get_output_dir()) == os.path.realpath(tmp_path / "output")


def test_fresh_install_does_not_pin(tmp_path, monkeypatch):
    from wisper_transcribe.config import load_config

    (tmp_path / "output").mkdir()
    monkeypatch.chdir(tmp_path)
    db.migrate()
    assert load_config()["output_dir"] == ""


def test_pin_skipped_when_env_or_setting_present(tmp_path, monkeypatch, data_dir):
    from wisper_transcribe.config import load_config, save_config

    _existing_install(data_dir)
    (tmp_path / "output").mkdir()
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    cfg["output_dir"] = "/somewhere/else"
    save_config(cfg)
    db.migrate()
    assert load_config()["output_dir"] == "/somewhere/else"


def test_pin_skipped_under_env_override(tmp_path, monkeypatch, data_dir):
    from wisper_transcribe.config import load_config

    _existing_install(data_dir)
    (tmp_path / "output").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(tmp_path / "output"))
    db.migrate()
    assert load_config()["output_dir"] == ""
