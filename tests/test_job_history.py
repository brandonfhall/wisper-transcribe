"""job_history: the jobs table behind the in-memory queue."""
from __future__ import annotations

import json
import re
import sqlite3
import types
from datetime import datetime

import pytest

from wisper_transcribe import db, job_history

JID = "11111111-1111-4111-8111-111111111111"


def _job(**kw):
    base = dict(id=JID, job_type="transcription", status="pending", created_at=datetime.now(),
                started_at=None, finished_at=None, error=None, log_lines=[], output_path=None,
                recording_id=None, kwargs={"model_size": "small", "output_dir": "/tmp/x",
                                           "hotwords": ["Kyra"], "input_path": "/tmp/secret"})
    base.update(kw)
    return types.SimpleNamespace(**base)


def _row():
    with db.connection() as conn:
        return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (JID,)).fetchone())


def test_lifecycle_rows_satisfy_constraints():
    job = _job()
    job_history.record(job)
    assert _row()["status"] == "pending" and _row()["started_at"] is None
    job.status, job.started_at = "running", datetime.now()
    job_history.record(job)
    assert _row()["started_at"] is not None
    job.status, job.error, job.log_lines = "failed", "Transcription failed", [f"l{i}" for i in range(300)]
    job_history.record(job)
    row = _row()
    assert row["error_code"] == "Transcription failed" and row["finished_at"]
    assert row["log_tail"].splitlines()[0] == "l100"      # last 200 lines


def test_params_are_allowlisted_and_record_output_root():
    job_history.record(_job())
    params = json.loads(_row()["params_json"])
    assert params["model_size"] == "small"
    # The job's own output dir, so the evidence names a campaign folder too.
    assert params["output_root"] == "/tmp/x"
    assert "output_dir" not in params and "input_path" not in params and "hotwords" not in params


def test_links_only_existing_subjects():
    from wisper_transcribe.path_utils import get_output_dir
    from wisper_transcribe.transcript_store import register

    md = get_output_dir() / "s1.md"
    md.write_text("x", encoding="utf-8")
    register(md, origin="job")
    job_history.record(_job(status="completed", output_path=str(md),
                            recording_id="22222222-2222-4222-8222-222222222222"))
    row = _row()
    assert row["transcript_id"] is not None and row["recording_id"] is None


def test_record_never_raises():
    job_history.record(_job(id="not-a-uuid"))   # CHECK fails; logged, not raised


def test_mark_interrupted_and_lookup():
    job_history.record(_job(status="running", started_at=datetime.now()))
    assert job_history.mark_interrupted() == 1
    rec = job_history.get_job(JID)
    assert rec.status == "failed" and rec.error == job_history.INTERRUPTED
    records, total = job_history.list_jobs(status="failed")
    assert total == 1 and records[0].id == JID


def test_schema_rejects_inconsistent_rows():
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO jobs (id, type, status, created_at, error_code) "
                         "VALUES (?, 'transcription', 'completed', 'x', 'e')", (JID,))


def test_enum_checks_mirror_python_constants():
    """Adding a job type, status, or source without a migration fails here."""
    from wisper_transcribe import file_registry, recording_manager
    from wisper_transcribe.speaker_registry import SOURCE_AUTO, SOURCE_MANUAL
    from wisper_transcribe.web import jobs

    ddl = "\n".join(m.ddl for m in db.MIGRATIONS)
    # v14 rebuilds `jobs` and `files`; read the latest definition of each.
    v14 = next(m.ddl for m in db.MIGRATIONS if m.version == 14)

    def check_values(column: str, source: str = ddl) -> set[str]:
        m = re.search(rf"{column}\s+TEXT NOT NULL CHECK \({column} IN \(([^)]*)\)\)", source)
        assert m, column
        return set(re.findall(r"'([^']+)'", m.group(1)))

    job_types = {getattr(jobs, n) for n in dir(jobs) if n.startswith("JOB_") and isinstance(getattr(jobs, n), str)}
    assert check_values("type", v14) == job_types
    assert check_values("status") == {jobs.PENDING, jobs.RUNNING, jobs.COMPLETED, jobs.FAILED}
    assert check_values("capture_status") == set(recording_manager.CAPTURE_STATUSES)
    assert check_values("source") >= {SOURCE_AUTO, SOURCE_MANUAL}
    # Other tables also have a `kind` column; the v14 files rebuild has the latest.
    assert check_values("kind", v14) == set(file_registry.KINDS)


def test_history_page_and_detail_fallback(tmp_path):
    from fastapi.testclient import TestClient

    from wisper_transcribe.web.app import create_app

    job_history.record(_job(status="failed", started_at=datetime.now(), error="Transcription failed",
                            log_lines=["boom line"]))
    with TestClient(create_app()) as client:
        page = client.get("/jobs/history?status=failed&type=transcription")
        assert 'data-testid="job-history-row"' in page.text
        assert client.get("/jobs/history?type=<script>").status_code == 200
        detail = client.get(f"/transcribe/jobs/{JID}")
        assert 'data-testid="job-history-detail"' in detail.text and "boom line" in detail.text
        assert client.get("/transcribe/jobs/..%2F..%2Fetc").status_code == 404
        # After a restart the dashboard still lists it (memory + history).
        assert JID[:8] in client.get("/").text


def test_transcript_id_filter_lists_only_that_session(tmp_path):
    """`?transcript_id=` narrows job history to one session even when another
    campaign holds a session of the same name."""
    from fastapi.testclient import TestClient

    from wisper_transcribe import db, file_registry
    from wisper_transcribe.config import get_output_root
    from wisper_transcribe.web.app import create_app

    from . import _seed

    out = get_output_root()
    c1 = _seed.seed_campaign("Alpha", slug="alpha", claimed=True)
    c2 = _seed.seed_campaign("Beta", slug="beta", claimed=True)
    ids = {}
    for jid, cid, folder in (
        ("11111111-1111-4111-8111-111111111111", c1, "Alpha"),
        ("22222222-2222-4222-8222-222222222222", c2, "Beta"),
    ):
        md = out / folder / "Same.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("x", encoding="utf-8")
        with db.transaction() as conn:
            tid = conn.execute(
                "INSERT INTO transcripts (stem, campaign_id, position, created_at) "
                "VALUES (?, ?, 0, ?)",
                ("Same", cid, db.now_utc()),
            ).lastrowid
        file_registry.add(md, kind="transcript",
                          owner=file_registry.Owner("transcript", tid), output_dir=out)
        job_history.record(_job(id=jid, status="completed", output_path=str(md)))
        ids[cid] = tid

    with TestClient(create_app()) as client:
        filtered = client.get(f"/jobs/history?transcript_id={ids[c1]}")
    assert "11111111" in filtered.text and "22222222" not in filtered.text
    with db.connection() as conn:
        row = conn.execute("SELECT transcript_id FROM jobs WHERE id = ?",
                           ("11111111-1111-4111-8111-111111111111",)).fetchone()
    assert row["transcript_id"] == ids[c1]
    rec, total = job_history.list_jobs(transcript_id=ids[c2])
    assert total == 1 and rec[0].id == "22222222-2222-4222-8222-222222222222"


# ---------------------------------------------------------------------------
# A job's campaign is derived: subject, transcript, recording, submit params
# ---------------------------------------------------------------------------

def _transcript_job(job_id, stem, **kw):
    from wisper_transcribe.path_utils import get_output_dir
    from wisper_transcribe.transcript_store import register

    md = get_output_dir() / f"{stem}.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("x", encoding="utf-8")
    register(md, origin="job")
    job_history.record(_job(id=job_id, status="completed", output_path=str(md), **kw))


def test_campaign_comes_from_the_transcripts_current_campaign():
    from ._seed import assign_campaign
    from wisper_transcribe.campaign_manager import create_campaign

    create_campaign("Curse")
    create_campaign("Other")
    _transcript_job(JID, "s1")
    tid = _tid("s1")
    assign_campaign(tid, "curse")
    (rec,) = job_history.list_jobs(campaign="curse")[0]
    assert (rec.id, rec.campaign_slug, rec.campaign_name) == (JID, "curse", "Curse")
    assert job_history.get_job(JID).campaign_name == "Curse"

    assign_campaign(tid, "other")  # the job follows its transcript
    assert job_history.list_jobs(campaign="curse")[0] == []
    assert [r.id for r in job_history.list_jobs(campaign="other")[0]] == [JID]


def test_campaign_rename_drops_a_params_only_jobs_link():
    """A job whose only campaign link is the slug in params_json loses it after
    the campaign is renamed: the stored slug no longer resolves."""
    from wisper_transcribe.campaign_folders import rename_campaign
    from wisper_transcribe.campaign_manager import create_campaign

    create_campaign("Game")
    job_history.record(_job(status="completed", kwargs={"campaign": "game"}))
    assert job_history.get_job(JID).campaign_name == "Game"

    assert rename_campaign("game", "Renamed").status == "renamed"
    assert job_history.get_job(JID).campaign_name is None
    assert job_history.get_job(JID).campaign_slug is None


def test_campaign_falls_back_to_recording_then_submit_params():
    from wisper_transcribe.campaign_manager import create_campaign

    from ._seed import seed_recording

    create_campaign("Curse")
    rec = seed_recording(status="completed")
    with db.transaction() as conn:
        conn.execute("UPDATE recordings SET campaign_id = (SELECT id FROM campaigns WHERE slug = 'curse') "
                     "WHERE id = ?", (rec.id,))
    rec_job = "33333333-3333-4333-8333-333333333333"
    job_history.record(_job(id=rec_job, recording_id=rec.id))
    job_history.record(_job(kwargs={"campaign": "curse"}))  # never wrote a transcript
    job_history.record(_job(id="44444444-4444-4444-8444-444444444444", kwargs={"campaign": "gone"}))
    names = {r.id: r.campaign_name for r in job_history.list_jobs()[0]}
    assert names[rec_job] == "Curse" and names[JID] == "Curse"
    assert names["44444444-4444-4444-8444-444444444444"] is None
    assert {r.id for r in job_history.list_jobs(campaign="curse")[0]} == {rec_job, JID}


def test_dashboard_campaign_column(tmp_path):
    from fastapi.testclient import TestClient

    from ._seed import assign_campaign
    from wisper_transcribe.campaign_manager import create_campaign
    from wisper_transcribe.web.app import create_app
    from wisper_transcribe.web.routes.dashboard import job_campaigns

    create_campaign("Curse")
    _transcript_job(JID, "s1")
    assign_campaign(_tid("s1"), "curse")
    with TestClient(create_app()) as client:
        queue = client.app.state.job_queue
        live = queue.submit(str(tmp_path / "wisper_upload_x.mp3"), campaign="curse")
        live.status = "failed"  # keep the worker off it
        rows = client.get("/jobs").text
    assert job_campaigns([live]) == {live.id: "Curse"}
    cells = re.findall(r'data-testid="job-campaign"[^>]*>([^<]*)<', rows)
    assert cells.count("Curse") == 2  # in-memory + history row


def _transcription(job_id, transcript_path, status="completed", job_type="transcription",
                   created=None, **kw):
    kwargs = {"model_size": "large-v3", "device": "cuda", "vad_filter": True,
              "overwrite": True, "campaign": "dnd", "language": "en",
              "num_speakers": 4, "no_diarize": False, "include_timestamps": True}
    kwargs.update(kw.pop("kwargs", {}))
    job = _job(id=job_id, status=status, job_type=job_type, kwargs=kwargs,
               output_path=str(transcript_path), created_at=created or datetime.now(),
               finished_at=datetime.now(), **kw)
    job.llm_transcript_path = str(transcript_path)
    job.post_refine = job.post_summarize = False
    return job


def _transcript_row(tmp_path, monkeypatch, stem="s1"):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(out))
    (out / f"{stem}.md").write_text("x", encoding="utf-8")
    from wisper_transcribe import transcript_store as ts
    ts.register(out / f"{stem}.md", origin="job")
    return out / f"{stem}.md"


def _tid(stem="s1"):
    with db.connection() as conn:
        return conn.execute("SELECT id FROM transcripts WHERE stem = ?", (stem,)).fetchone()[0]


def test_last_transcription_params_returns_latest_completed_session_settings(tmp_path, monkeypatch):
    md = _transcript_row(tmp_path, monkeypatch)
    old = "22222222-2222-4222-8222-222222222222"
    new = "33333333-3333-4333-8333-333333333333"
    job_history.record(_transcription(old, md, kwargs={"language": "fr", "num_speakers": 2}))
    newer = _transcription(new, md, created=datetime.now())
    newer.post_refine = True
    job_history.record(newer)
    assert job_history.last_transcription_params(_tid()) == {
        "language": "en", "num_speakers": 4, "no_diarize": False,
        "include_timestamps": True, "post_refine": True,
    }


def test_last_transcription_params_ignores_failed_and_other_jobs(tmp_path, monkeypatch):
    md = _transcript_row(tmp_path, monkeypatch)
    job_history.record(_transcription("22222222-2222-4222-8222-222222222222", md,
                                      kwargs={"language": "de"}))
    job_history.record(_transcription("33333333-3333-4333-8333-333333333333", md,
                                      status="failed", kwargs={"language": "fr"}))
    job_history.record(_transcription("44444444-4444-4444-8444-444444444444", md,
                                      job_type="refine", kwargs={"language": "es"}))
    assert job_history.last_transcription_params(_tid())["language"] == "de"


def test_last_transcription_params_drops_engine_and_submit_keys(tmp_path, monkeypatch):
    md = _transcript_row(tmp_path, monkeypatch)
    job_history.record(_transcription("22222222-2222-4222-8222-222222222222", md))
    got = job_history.last_transcription_params(_tid())
    for key in ("model_size", "device", "compute_type", "vad_filter", "forced_alignment",
                "overwrite", "campaign", "output_root"):
        assert key not in got


def test_last_transcription_params_empty_without_history(tmp_path, monkeypatch):
    _transcript_row(tmp_path, monkeypatch)
    assert job_history.last_transcription_params(_tid()) == {}
    assert job_history.last_transcription_params(99999) == {}


# ---------------------------------------------------------------------------
# record_required and active_jobs: the write the busy guard depends on
# ---------------------------------------------------------------------------

def test_record_required_reraises_and_record_swallows():
    """A job whose row can't be written fails submit; the ordinary update
    path still swallows (a status write must not break the job)."""
    bad = _job(id="not-a-uuid")   # CHECK fails
    job_history.record(bad)      # swallowed
    with pytest.raises(sqlite3.IntegrityError):
        job_history.record_required(bad)


def _campaign_with_folder(display_name: str) -> tuple[int, str, str]:
    """A campaign whose folder exists and is claimed: (id, slug, folder)."""
    from . import _seed

    cid = _seed.seed_campaign(display_name, claimed=True)
    with db.connection() as conn:
        slug, folder = conn.execute(
            "SELECT slug, folder FROM campaigns WHERE id = ?", (cid,)).fetchone()
    return cid, slug, folder


def _session(stem: str, *, campaign_id=None) -> int:
    with db.transaction() as conn:
        return conn.execute(
            "INSERT INTO transcripts (stem, campaign_id, position, created_at) VALUES (?, ?, ?, ?) "
            "RETURNING id",
            (stem, campaign_id, 0 if campaign_id is not None else None, db.now_utc()),
        ).fetchone()[0]


def _recording(campaign_id, *, status: str = "recording",
               transcript_id=None) -> str:
    import uuid

    rid = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO recordings (id, source, capture_status, started_at, campaign_id, transcript_id) "
            "VALUES (?, 'discord', ?, ?, ?, ?)",
            (rid, status, db.now_utc(), campaign_id, transcript_id),
        )
    return rid


def test_active_jobs_sees_the_transcript_campaign_and_slug():
    """A pending transcription job counts by its target transcript, the
    campaign it targets, its transcript's campaign, or the slug in params_json."""
    cid, slug, _folder = _campaign_with_folder("Game")
    tid = _session("S1", campaign_id=cid)

    queued = _job(status="pending", transcript_id=tid)
    job_history.record_required(queued)
    with db.connection() as conn:
        assert job_history.active_jobs(conn, transcript_id=tid) == [queued.id]
        assert job_history.active_jobs(conn, campaign_ids={cid}) == [queued.id]
        assert job_history.active_jobs(conn, campaign_slugs={slug}) == []
        assert job_history.active_jobs(conn, campaign_ids={cid + 100}) == []

    # A job that only names its campaign in params_json counts by slug.
    upload = _job(id="22222222-2222-4222-8222-222222222222",
                  kwargs={"campaign": slug})
    job_history.record_required(upload)
    with db.connection() as conn:
        assert job_history.active_jobs(conn, campaign_slugs={slug}) == [upload.id]


def test_active_jobs_ignores_terminal_jobs_and_other_subjects():
    cid, slug, _folder = _campaign_with_folder("Game")
    other, other_slug, _ = _campaign_with_folder("Other")
    tid = _session("S1", campaign_id=cid)

    done = _job(status="completed", transcript_id=tid, finished_at=datetime.now())
    job_history.record_required(done)
    job_history.record_required(_job(id="33333333-3333-4333-8333-333333333333",
                                     status="pending", transcript_id=None,
                                     kwargs={"campaign": other_slug}))
    with db.connection() as conn:
        assert job_history.active_jobs(conn, transcript_id=tid) == []
        assert job_history.active_jobs(conn, campaign_ids={cid}) == []
        assert job_history.active_jobs(conn, campaign_slugs={slug}) == []
        assert job_history.active_jobs(conn, campaign_slugs={other_slug}) == [
            "33333333-3333-4333-8333-333333333333"]


def test_active_jobs_sees_a_capturing_recording():
    cid, slug, _folder = _campaign_with_folder("Game")
    tid = _session("S1", campaign_id=cid)
    by_campaign = _recording(cid, status="recording")
    by_transcript = _recording(None, status="degraded", transcript_id=tid)
    _recording(cid, status="completed")  # finished: not busy

    with db.connection() as conn:
        assert set(job_history.active_jobs(conn, transcript_id=tid, campaign_ids={cid})) == {
            by_campaign, by_transcript}


def test_active_jobs_sql_uses_the_jobs_active_partial_index():
    """The busy guard writes ``status IN ('pending', 'running')`` exactly as the
    partial index does; SQLite uses a partial index only on an exact match."""
    with db.connection() as conn:
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            job_history.active_jobs(conn, transcript_id=1, campaign_ids={1}, campaign_slugs={"game"})
        finally:
            conn.set_trace_callback(None)
        sql = next(s for s in reversed(statements) if "FROM jobs" in s)
        plan = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    assert any(r[3].startswith("SEARCH j USING INDEX jobs_active") for r in plan), [tuple(r) for r in plan]
