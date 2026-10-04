"""Job history: a durable projection of the web job queue into ``wisper.db``.

The queue itself stays in memory (events, closures, and cancel flags can't
persist, and only 50 finished jobs are kept). Every job is written through
here when it is submitted and at each status change, so history survives
restarts: the Job history page, a recording's job link, and "what happened
to that transcript" all read the ``jobs`` table.

Only generic error codes and an allowlisted set of parameters are stored —
never exception text, secrets, or temp paths.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from . import db

log = logging.getLogger(__name__)

JOB_STATUSES = ("pending", "running", "completed", "failed")
LOG_TAIL_LINES = 200
INTERRUPTED = "Interrupted by restart"

# Job parameters worth keeping for the record. Paths and free text are not.
_PARAM_ALLOWLIST = (
    "model_size", "language", "device", "compute_type", "num_speakers", "min_speakers",
    "max_speakers", "no_diarize", "vad_filter", "include_timestamps", "campaign",
    "overwrite", "forced_alignment", "slug", "session_stem", "fold_all", "rebuild",
    "resummarize", "backfill", "tasks", "provider", "model",
)


def _utc(dt: Optional[datetime]) -> Optional[str]:
    """A job timestamp (naive local time, as ``datetime.now()`` gives) in UTC."""
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _params(job: Any) -> str:
    kwargs = dict(getattr(job, "kwargs", {}) or {})
    out: dict[str, Any] = {}
    for key in _PARAM_ALLOWLIST:
        value = kwargs.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if value is not None:
                out[key] = value
        elif isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
            out[key] = list(value)
    for flag in ("post_refine", "post_summarize"):
        if getattr(job, flag, False):
            out[flag] = True
    if getattr(job, "job_type", "") == "transcription":
        from .path_utils import get_output_dir
        # Where it wrote: evidence for "the job said done but the file is missing".
        out["output_root"] = str(get_output_dir())
    return json.dumps(out, sort_keys=True)


def _subject_ids(conn, job: Any) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """(transcript_id, campaign_id, recording_id) — the job's direct subject.

    Links only to rows that exist; a job never creates them.
    """
    from .transcript_store import nfc

    transcript_id = campaign_id = recording_id = None
    job_type = getattr(job, "job_type", "")
    path = None
    if job_type == "transcription":
        path = getattr(job, "output_path", None)
    elif job_type in ("refine", "summarize"):
        path = getattr(job, "llm_transcript_path", None)
    elif job_type == "enroll":
        path = getattr(job, "enroll_md_path", None)
    if path:
        row = conn.execute("SELECT id FROM transcripts WHERE stem = ?",
                           (nfc(Path(path).stem),)).fetchone()
        transcript_id = row[0] if row else None
    if job_type in ("campaign_journal", "speaker_relabel"):
        slug = (getattr(job, "kwargs", {}) or {}).get("slug")
        if slug:
            row = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (slug,)).fetchone()
            campaign_id = row[0] if row else None
    rid = (getattr(job, "recording_id", None)
           or getattr(job, "live_recording_id", None)
           or ((getattr(job, "enroll_params", {}) or {}).get("recording_id") if job_type == "enroll" else None))
    if rid:
        row = conn.execute("SELECT id FROM recordings WHERE id = ?", (rid,)).fetchone()
        recording_id = row[0] if row else None
    return transcript_id, campaign_id, recording_id


def record(job: Any, data_dir: Optional[Path] = None) -> None:
    """Upsert one job's row from the in-memory ``Job``. Never raises: a
    history write must not break the job itself."""
    try:
        status = job.status if job.status in JOB_STATUSES else "failed"
        terminal = status in ("completed", "failed")
        started = getattr(job, "started_at", None)
        if status == "pending":
            started = None
        elif status == "running" and started is None:
            started = datetime.now()
        finished = (job.finished_at or datetime.now()) if terminal else None
        error = (job.error or "Job failed") if status == "failed" else None
        log_tail = "\n".join(list(getattr(job, "log_lines", []) or [])[-LOG_TAIL_LINES:]) if terminal else ""
        with db.transaction(data_dir) as conn:
            transcript_id, campaign_id, recording_id = _subject_ids(conn, job)
            conn.execute(
                "INSERT INTO jobs (id, type, status, created_at, started_at, finished_at, error_code, "
                "transcript_id, campaign_id, recording_id, params_json, log_tail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
                "status = excluded.status, started_at = excluded.started_at, "
                "finished_at = excluded.finished_at, error_code = excluded.error_code, "
                "transcript_id = coalesce(excluded.transcript_id, jobs.transcript_id), "
                "campaign_id = coalesce(excluded.campaign_id, jobs.campaign_id), "
                "recording_id = coalesce(excluded.recording_id, jobs.recording_id), "
                "params_json = excluded.params_json, log_tail = excluded.log_tail",
                (job.id, job.job_type, status, _utc(job.created_at), _utc(started), _utc(finished),
                 error, transcript_id, campaign_id, recording_id, _params(job), log_tail),
            )
    except Exception:
        log.warning("Could not record job %s in history", getattr(job, "id", "?"), exc_info=True)


def mark_interrupted(data_dir: Optional[Path] = None) -> int:
    """At startup: jobs left pending or running died with the last process.
    They are never resumed (uploads are gone; the work is hours of GPU)."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with db.transaction(data_dir) as conn:
        return conn.execute(
            "UPDATE jobs SET status = 'failed', finished_at = ?, error_code = ? "
            "WHERE status IN ('pending', 'running')",
            (now, INTERRUPTED),
        ).rowcount


@dataclass
class JobRecord:
    """One row of job history, for pages that outlive the in-memory queue."""
    id: str
    job_type: str
    status: str
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    error: Optional[str]
    transcript_stem: Optional[str]
    campaign_slug: Optional[str]
    recording_id: Optional[str]
    campaign_name: Optional[str]
    params: dict
    log_tail: str

    @property
    def name(self) -> str:
        return self.transcript_stem or self.campaign_slug or self.recording_id or self.job_type

    # Duck-typed like the in-memory Job for the dashboard's job rows.
    input_path = ""

    @property
    def output_path(self) -> Optional[str]:
        if self.job_type == "transcription" and self.transcript_stem and self.status == "completed":
            return f"{self.transcript_stem}.md"
        return None


# A job's campaign: its own subject (journal, relabel), else its transcript's
# current campaign, else its recording's, else the campaign it was submitted
# with (params_json; a job that never wrote a transcript). Derived, so a moved
# transcript's jobs follow it. campaign_transcripts allows one row per
# transcript, so the joins never multiply job rows.
_FROM = (
    "FROM jobs j LEFT JOIN transcripts t ON t.id = j.transcript_id "
    "LEFT JOIN campaign_transcripts ct ON ct.transcript_id = j.transcript_id "
    "LEFT JOIN recordings rec ON rec.id = j.recording_id "
    "LEFT JOIN campaigns c ON c.id = coalesce(j.campaign_id, ct.campaign_id, rec.campaign_id, "
    "(SELECT id FROM campaigns WHERE slug = json_extract(j.params_json, '$.campaign'))) "
)
_COLUMNS = "j.*, t.stem AS transcript_stem, c.slug AS campaign_slug, c.display_name AS campaign_name"


def _record(r) -> "JobRecord":
    return JobRecord(
        id=r["id"], job_type=r["type"], status=r["status"], created_at=_dt(r["created_at"]),
        started_at=_dt(r["started_at"]), finished_at=_dt(r["finished_at"]),
        error=r["error_code"], transcript_stem=r["transcript_stem"],
        campaign_slug=r["campaign_slug"], recording_id=r["recording_id"],
        campaign_name=r["campaign_name"],
        params=json.loads(r["params_json"]), log_tail=r["log_tail"],
    )


def _dt(s: Optional[str]) -> Optional[datetime]:
    """Stored UTC → naive local time, comparable with in-memory ``Job`` times."""
    return datetime.fromisoformat(s).astimezone().replace(tzinfo=None) if s else None


def list_jobs(*, page: int = 1, per_page: int = 50, job_type: Optional[str] = None,
              status: Optional[str] = None, transcript: Optional[str] = None,
              campaign: Optional[str] = None, data_dir: Optional[Path] = None,
              ) -> tuple[list[JobRecord], int]:
    """A page of history, newest first, and the total matching count."""
    where, params = [], []
    if job_type:
        where.append("j.type = ?")
        params.append(job_type)
    if status:
        where.append("j.status = ?")
        params.append(status)
    if transcript:
        where.append("t.stem = ?")
        params.append(transcript)
    if campaign:
        where.append("c.slug = ?")
        params.append(campaign)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    base = _FROM + clause
    page = max(1, page)
    with db.connection(data_dir) as conn:
        total = conn.execute(f"SELECT count(*) {base}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {_COLUMNS} {base} "
            "ORDER BY j.created_at DESC, j.rowid DESC LIMIT ? OFFSET ?",
            [*params, per_page, (page - 1) * per_page],
        ).fetchall()
    return [_record(r) for r in rows], total


def get_job(job_id: str, data_dir: Optional[Path] = None) -> Optional[JobRecord]:
    with db.connection(data_dir) as conn:
        row = conn.execute(f"SELECT {_COLUMNS} {_FROM} WHERE j.id = ?", (job_id,)).fetchone()
    return None if row is None else _record(row)


# Per-session choices a rerun repeats. Engine settings (model, device, VAD,
# alignment) are left out so a rerun uses the current config.
_SESSION_PARAMS = (
    "language", "num_speakers", "min_speakers", "max_speakers", "no_diarize",
    "include_timestamps",
)


def last_transcription_params(transcript_id: int,
                              data_dir: Optional[Path] = None) -> dict:
    """The session settings of a transcript's latest completed transcription
    job, as keyword arguments for ``JobQueue.submit``; ``{}`` with no history.

    ``post_refine`` and ``post_summarize`` are stored only when on, so absence
    means off. ``overwrite`` and ``campaign`` are never returned: the caller
    supplies its own.
    """
    with db.connection(data_dir) as conn:
        row = conn.execute(
            "SELECT params_json FROM jobs WHERE transcript_id = ? "
            "AND type = 'transcription' AND status = 'completed' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (transcript_id,),
        ).fetchone()
    if row is None:
        return {}
    stored = json.loads(row["params_json"])
    out = {k: stored[k] for k in _SESSION_PARAMS if k in stored}
    for flag in ("post_refine", "post_summarize"):
        if stored.get(flag):
            out[flag] = True
    return out
