"""In-process job queue for background transcription and LLM post-processing.

Design:
- Jobs are stored in-memory (dict keyed by UUID).  Single-user tool — no
  persistence needed between restarts.
- One background asyncio task drains a FIFO queue.  Each job runs
  process_file() (transcription) or the LLM pipeline (refine/summarize) in a
  thread via asyncio.to_thread() so the event loop stays responsive.
- Thread safety: the module-level _model / _pipeline globals in transcriber /
  diarizer are NOT thread-safe.  We run exactly ONE job at a time (max_workers=1
  thread pool semantics).  Future: use ProcessPoolExecutor for CPU multi-worker
  web deployments, same guard logic as Phase 10.
- Progress: process_file() uses tqdm.write() for status messages.  We
  monkey-patch tqdm.write per-job so messages are captured into job.log_lines
  and streamed to the browser via Server-Sent Events.
- LLM jobs: sys.stderr is redirected per-job so Ollama's streaming status
  messages ("Connecting…", "Generating: ·····") are captured the same way.
  Safe because the queue is single-worker — only one job runs at a time.
- Enroll jobs (JOB_ENROLL): the speaker-enrollment wizard's slow half (WAV
  conversion + pyannote embedding extraction, formerly synchronous in the
  HTTP request) runs here too. The wizard route applies renames to the
  transcript synchronously, then enqueues a JOB_ENROLL job carrying only the
  transcript path and the validated rename groups; the runner re-reads the
  transcript's _diar.json sidecar for segments/input_path/campaign. Progress
  is pushed via a plain callback straight into job.log_lines (no tqdm/stderr
  capture needed — enroll_profiles() calls back directly).
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys as _sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import tqdm as _tqdm_module

from wisper_transcribe.pipeline import process_file

log = logging.getLogger(__name__)

# Job status literals
PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"

# Job type literals
JOB_TRANSCRIPTION = "transcription"
JOB_REFINE = "refine"
JOB_SUMMARIZE = "summarize"
JOB_ENROLL = "enroll"

_EXCERPT_SECONDS = 12  # length of each speaker audio clip

# R13: user-facing error strings per job type. job.error is rendered directly
# into the job-detail page and SSE stream, and raw exception text routinely
# carries filesystem paths (WAV-conversion failures, ffmpeg command lines,
# HF cache paths) — so EVERY failure path maps to one of these generic
# messages and the real exception is logged server-side with its traceback
# (see _set_job_error). _run_enroll_job already followed this policy; this
# extends it to transcription and LLM jobs.
_GENERIC_JOB_ERRORS = {
    JOB_TRANSCRIPTION: "Transcription failed — see server logs",
    JOB_REFINE: "Post-processing failed — see server logs",
    JOB_SUMMARIZE: "Post-processing failed — see server logs",
    JOB_ENROLL: "Enrollment failed",
}


def _set_job_error(job: "Job", exc: BaseException) -> None:
    """Record a generic, path-free error message on *job* and log the real
    exception (with traceback) server-side (R13).

    The literal ``"Cancelled"`` string is preserved for cancellation — other
    code and the job-detail template check for it.
    """
    if isinstance(exc, InterruptedError):
        job.error = "Cancelled"
        return
    log.error("Job %s (%s) failed", job.id, job.job_type, exc_info=exc)
    if isinstance(exc, FileNotFoundError):
        # Known input error — short, safe text with no path reflected.
        job.error = "Input file not found"
        return
    job.error = _GENERIC_JOB_ERRORS.get(job.job_type, "Job failed — see server logs")

# R14: unbounded memory growth guards. _jobs is an in-memory dict that is
# never otherwise pruned, and a single verbose job (e.g. an LLM job whose
# stderr capture runs long) can append log lines forever -- both caps are
# constants rather than config keys since they're internal resource limits,
# not something a user needs to tune.
_MAX_RETAINED_JOBS = 50   # cap on retained COMPLETED/FAILED jobs (never PENDING/RUNNING)
_MAX_LOG_LINES = 1000     # cap on Job.log_lines, oldest lines dropped first


# ---------------------------------------------------------------------------
# Stderr capture — funnels LLM client status messages into job.log_lines
# ---------------------------------------------------------------------------

class _StderrCapture:
    """Redirect sys.stderr into job.log_lines for real-time LLM status.

    Accumulates partial writes into a line buffer and appends complete lines
    to job.log_lines immediately so the SSE stream picks them up within ~1 s.
    list.append() is atomic under the GIL, so concurrent reads from the async
    event loop are safe without a lock.
    """

    def __init__(self, job: "Job") -> None:
        self._job = job
        self._buf = ""

    def write(self, s: str) -> None:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            stripped = line.strip()
            if stripped:
                self._job.append_log(stripped)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def _write_enrollment_sidecar(job: "Job", output_path: "Path") -> None:  # type: ignore[name-defined]
    """Persist enrollment data alongside the transcript as <stem>_diar.json.

    Stores the diarization segments and source audio path so the enrollment
    wizard can function after a server restart without requiring the in-memory
    job to still exist.  Failures are silently swallowed — the transcript is
    already written and enrollment can still fall back to the in-memory job.
    """
    import json as _json
    from pathlib import Path as _Path

    if not job.diarization_segments:
        return

    try:
        out = _Path(output_path)
        sidecar = {
            "input_path": str(_Path(job.input_path)),
            "campaign": job.kwargs.get("campaign"),
            "diarization_segments": [
                {"start": s.start, "end": s.end, "speaker": s.speaker}
                for s in job.diarization_segments
            ],
            # F7: authoritative raw_label -> display_name map. Present on
            # every new transcript; absent on sidecars written before this
            # key existed, which is exactly the legacy fallback
            # resolve_current_names() handles.
            "speaker_map": dict(job.speaker_map) if job.speaker_map else {},
        }
        sidecar_path = out.with_name(out.stem + "_diar.json")
        sidecar_path.write_text(_json.dumps(sidecar, indent=2), encoding="utf-8")
    except Exception:
        pass


def _move_upload_to_output(input_path: str, output_path: "Path") -> str:  # type: ignore[name-defined]
    """Move a temp web-upload file next to its finished transcript.

    F5 fix: web uploads land in a ``wisper_upload_*`` NamedTemporaryFile in the
    OS tempdir (renamed to a friendly ``<original_stem><suffix>`` name by
    ``JobQueue.submit`` before the job even starts — see ``Job.is_web_upload``
    for why the *original* prefix is captured at submit time rather than
    re-derived from the current basename here).  That file is never cleaned
    up when a job completes, and the startup orphan sweep only recognises the
    unrenamed ``wisper_upload_*`` prefix, so it leaks until a lucky restart
    happens to catch it mid-flight.  Moving it next to the transcript makes it
    durable (the enrollment sidecar's ``input_path`` then survives restarts)
    and removes it from the tempdir, closing the leak.

    Returns the new durable path as a string, or the original ``input_path``
    unchanged if the source is missing or the move fails for any reason —
    callers should treat that as "enrollment audio may be unavailable" rather
    than a hard failure, since the transcript itself is already written.
    """
    import shutil
    from pathlib import Path as _Path

    src = _Path(input_path)
    if not src.exists():
        return input_path

    out = _Path(output_path)
    out_dir = out.parent
    stem = out.stem
    suffix = src.suffix

    dest = out_dir / f"{stem}{suffix}"
    counter = 1
    while dest.exists():
        dest = out_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    try:
        shutil.move(str(src), str(dest))
    except OSError:
        return input_path
    return str(dest)


def _delete_temp_upload(job: "Job") -> None:  # type: ignore[name-defined]
    """Delete the job's temp web-upload file on failure/cancel.

    Only ever acts on files ``Job.is_web_upload`` marks as originating from a
    ``wisper_upload_*`` temp file — recording-sourced or other durable inputs
    are never touched.  A failed/cancelled job never reaches
    ``_move_upload_to_output``, so the temp file would otherwise sit in the
    tempdir leaking disk until the next server restart.
    """
    if not job.is_web_upload:
        return
    from pathlib import Path as _Path

    try:
        p = _Path(job.input_path)
        if p.exists():
            p.unlink()
    except OSError:
        pass


def _longest_aligned_segment(aligned_segments: list, label: str) -> Optional[tuple]:
    """Return (start, duration, text) of the LONGEST aligned segment for a raw
    label (by ``end - start``), or None if the label has no segments.

    Used as the fallback excerpt window when no diarization turn is available
    for a label -- the pre-F12 behavior. A short interjection ("mm-hmm", a
    cross-talk aside) is often misattributed and plays mostly someone else's
    voice, while the longest block for a label is far more likely to actually
    be that speaker talking.
    """
    best = None
    best_duration = -1.0
    for seg in aligned_segments:
        if getattr(seg, "speaker", None) != label:
            continue
        duration = float(seg.end) - float(seg.start)
        if duration > best_duration:
            best_duration = duration
            best = seg
    if best is None:
        return None
    return float(best.start), best_duration, (getattr(best, "text", "") or "").strip()


def _extract_speaker_excerpts(job: "Job", output_path: "Path",  # type: ignore[name-defined]
                              aligned_segments: list | None = None,
                              diarization_segments: list | None = None) -> None:
    """Extract a short audio clip per speaker from the transcribed file.

    F12: for each *raw* speaker label (e.g. ``SPEAKER_00``), the clip window
    is chosen from the speaker's longest **solo diarization turn** --
    ``speaker_manager._select_embedding_segments(diarization_segments, label,
    max_count=1)``, the same solo-preferred / 2-20s-band / graceful-fallback
    policy F10b uses to pick embedding source audio, so the clip is (as much
    as diarization allows) audio of ONLY that speaker. The clip duration is
    strictly clamped to ``min(_EXCERPT_SECONDS, turn length)`` -- no padding
    floor when the turn is shorter than 12s: a short clip of only the target
    speaker beats 12s that runs into someone else's turn (decision
    2026-07-13). The persisted ``.txt`` snippet is built from ALL of that
    label's aligned word-runs (post-F8, a whisper segment can split into
    several word-run AlignedSegments) that overlap the clip window, joined in
    time order -- so the displayed text matches exactly what the listener
    hears, instead of one word-run that can be a mid-sentence fragment.

    A label with no diarization segments (or a `diarization_segments` list
    without any turn for that label -- `_select_embedding_segments` raises
    `ValueError`) falls back to the pre-F12 behavior: the clip is cut at the
    label's longest ALIGNED segment, with the full fixed `_EXCERPT_SECONDS`
    window and that single segment's text. This keeps the function robust for
    legacy callers/tests that only pass `aligned_segments`, and means one
    label's lookup failure never affects any other label.

    Clips are saved alongside the transcript as
    ``<stem>_excerpt_<raw_label>.mp3`` so the enrollment wizard -- which keys
    off the raw labels stored in ``_diar.json`` -- can find them.

    Earlier versions parsed the rendered markdown and so keyed files by the
    *display* name ("Unknown Speaker 1", etc.), which never matched the
    wizard lookup. The wizard route has a backfill for those legacy files.

    Failures are silently swallowed — playback is a nice-to-have, not critical.
    """
    import re
    from pathlib import Path as _Path

    from wisper_transcribe.speaker_manager import _select_embedding_segments

    if not aligned_segments:
        return

    labels = sorted({
        getattr(seg, "speaker", None)
        for seg in aligned_segments
        if getattr(seg, "speaker", None) and getattr(seg, "speaker", None) != "UNKNOWN"
    })
    if not labels:
        return

    out_dir = _Path(output_path).parent
    stem = _Path(output_path).stem
    input_path = _Path(job.input_path)

    for label in labels:
        turn = None
        if diarization_segments:
            try:
                turn = _select_embedding_segments(diarization_segments, label, max_count=1)[0]
            except ValueError:
                turn = None

        if turn is not None:
            start = float(turn.start)
            duration = min(_EXCERPT_SECONDS, float(turn.end) - float(turn.start))
            window_end = start + duration
            overlapping = sorted(
                (
                    seg for seg in aligned_segments
                    if getattr(seg, "speaker", None) == label
                    and float(seg.start) < window_end
                    and float(seg.end) > start
                ),
                key=lambda seg: seg.start,
            )
            text = " ".join(
                stripped for stripped in (
                    (getattr(seg, "text", "") or "").strip() for seg in overlapping
                ) if stripped
            )
        else:
            fallback = _longest_aligned_segment(aligned_segments, label)
            if fallback is None:
                continue
            start, _longest_duration, text = fallback
            duration = _EXCERPT_SECONDS

        safe_name = re.sub(r"[^\w\-]", "_", label)
        clip_path = out_dir / f"{stem}_excerpt_{safe_name}.mp3"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", str(start),
                    "-t", str(duration),
                    "-i", str(input_path),
                    "-ac", "1",
                    "-ar", "22050",
                    "-b:a", "64k",
                    str(clip_path),
                ],
                check=True,
                capture_output=True,
            )
            job.speaker_excerpts[label] = str(clip_path)
        except Exception:
            pass

        # Persist the transcript snippet to disk so it survives server restarts.
        text_path = out_dir / f"{stem}_excerpt_{safe_name}.txt"
        try:
            text_path.write_text(text, encoding="utf-8")
        except Exception:
            pass


@dataclass
class Job:
    id: str
    status: str
    created_at: datetime
    input_path: str
    kwargs: dict[str, Any]
    # Human-readable name shown in the UI (defaults to input filename stem)
    name: str = ""
    # "transcription" | "refine" | "summarize"
    job_type: str = JOB_TRANSCRIPTION
    output_path: Optional[str] = None
    error: Optional[str] = None
    log_lines: list[str] = field(default_factory=list)
    # R14: total count of log lines ever dropped from the front of
    # log_lines once _MAX_LOG_LINES was exceeded. The SSE stream
    # (job_stream in routes/transcribe.py) uses this to keep its
    # absolute line-index bookkeeping correct even after older lines
    # have been trimmed away -- see append_log().
    log_lines_dropped: int = 0
    progress: Optional[str] = None
    # Parallel mode: per-channel progress strings keyed by channel name
    progress_channels: dict[str, str] = field(default_factory=dict)
    finished_at: Optional[datetime] = None
    # Set after transcription completes when enroll flow is needed
    diarization_labels: list[str] = field(default_factory=list)
    # Full diarization segments retained for post-job enrollment (enroll_submit uses these)
    diarization_segments: list = field(default_factory=list)
    # F7: authoritative raw_label -> display_name map the formatter used when
    # writing the transcript (pipeline.process_file()'s speaker_map local).
    # Persisted into the _diar.json sidecar so the enrollment wizard can
    # resolve current names without reconstructing them from rendered
    # markdown timestamps -- see enroll_shared.resolve_current_names.
    speaker_map: dict[str, str] = field(default_factory=dict)
    # speaker_label -> path to a short audio excerpt (for enrollment wizard)
    speaker_excerpts: dict[str, str] = field(default_factory=dict)
    # Threading event set by cancel() to signal the worker to abort
    _cancel_event: threading.Event = field(
        default_factory=threading.Event, repr=False, compare=False
    )
    # Post-processing flags: run refine/summarize after transcription
    post_refine: bool = False
    post_summarize: bool = False
    # For LLM jobs: path to the transcript being processed
    llm_transcript_path: Optional[str] = None
    # For summarize jobs: path to the generated .summary.md file
    summary_path: Optional[str] = None
    # True when input_path originated from a wisper_upload_* temp file (set by
    # JobQueue.submit from the *original* basename before the friendly-name
    # rename below strips that prefix).  Drives the F5 move-to-output and
    # failure-path cleanup — recording-sourced or other durable inputs must
    # never be moved or deleted.
    is_web_upload: bool = False
    # For JOB_ENROLL jobs: path to the transcript markdown.  The runner
    # re-reads the transcript's <stem>_diar.json sidecar for segments,
    # input_path, and campaign at run time (restart-irrelevant since the
    # queue is in-memory anyway, but it keeps this payload small and avoids
    # serialising DiarizationSegment objects onto the job).
    enroll_md_path: Optional[str] = None
    # For JOB_ENROLL jobs: the validated rename groups from apply_renames()
    # -- display_name -> [raw_label, ...] -- carried on the job because they
    # came from the form and can't be reconstructed from the sidecar alone.
    enroll_groups: dict[str, list[str]] = field(default_factory=dict)
    # For JOB_ENROLL jobs: device to run embedding extraction on.
    enroll_device: str = "cpu"
    # R6: JOB_ENROLL flavor. "wizard" is the original post-transcription
    # wizard job (sidecar-driven); "standalone" is the /speakers/enroll
    # upload flow; "recording" enrolls from a Discord recording's per-user
    # audio dir. The latter two used to run minutes of ML work synchronously
    # inside their route handlers, blocking the event loop and mutating the
    # module-level ML caches concurrently with the worker thread.
    enroll_mode: str = "wizard"
    # R6: mode-specific parameters for standalone/recording enroll jobs
    # (profile key, display name, etc. — all plain strings).
    enroll_params: dict[str, Any] = field(default_factory=dict)

    def append_log(self, line: str) -> None:
        """Append a log line, trimming the oldest lines once _MAX_LOG_LINES
        is exceeded (R14).

        Every call site that used to do ``job.log_lines.append(...)``
        directly now goes through this method so a single verbose job (e.g.
        a large LLM stderr capture) can't grow log_lines without bound.
        Trimmed lines are counted in ``log_lines_dropped`` rather than just
        discarded silently, so the SSE stream in routes/transcribe.py can
        still translate its absolute line index into a valid slice of
        whatever remains.
        """
        self.log_lines.append(line)
        overflow = len(self.log_lines) - _MAX_LOG_LINES
        if overflow > 0:
            del self.log_lines[:overflow]
            self.log_lines_dropped += overflow

    @property
    def needs_extraction(self) -> bool:
        """True when the input must be streamed through ffmpeg before transcription.

        Anything that isn't a `.wav` (mp3, m4a, m4b, flac, ogg, mp4, mkv, …)
        is converted to 16 kHz mono WAV via `_extract_first_audio_track`.
        WAVs are passthrough-checked and may also re-encode silently if their
        rate/channels are wrong — but the common case is no extraction.
        """
        from pathlib import Path as _Path
        return _Path(self.input_path or "").suffix.lower() != ".wav"


class JobQueue:
    """In-memory job queue backed by a single asyncio background worker."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._on_complete_callbacks: dict[str, Callable[["Job"], None]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background worker.  Call from FastAPI lifespan startup."""
        self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        """Stop the background worker.  Call from FastAPI lifespan shutdown."""
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(
        self,
        input_path: str,
        *,
        on_complete: Optional[Callable[["Job"], None]] = None,
        **kwargs: Any,
    ) -> Job:
        """Enqueue a transcription job.  Returns the Job immediately.

        If ``original_stem`` is provided in kwargs it is used as the job's
        human-readable name and to rename the temp upload file so the output
        .md inherits the original filename.  It is stripped from kwargs before
        being forwarded to process_file.

        ``post_refine`` and ``post_summarize`` booleans trigger LLM
        post-processing after transcription completes; they are also stripped
        from kwargs before forwarding to process_file.

        ``on_complete`` is an optional callback invoked after the job
        transitions to COMPLETED. It runs in the worker thread.
        """
        from pathlib import Path
        import shutil

        original_stem: str = kwargs.pop("original_stem", "")
        post_refine: bool = bool(kwargs.pop("post_refine", False))
        post_summarize: bool = bool(kwargs.pop("post_summarize", False))

        if not original_stem:
            original_stem = Path(input_path).stem

        # Capture the web-upload marker from the *original* basename before
        # the friendly-name rename below strips the "wisper_upload_" prefix
        # (F5: the renamed file still lives in the tempdir and must still be
        # recognised as a temp upload at job-completion/failure time).
        is_web_upload = Path(input_path).name.startswith("wisper_upload_")

        # Rename temp file so process_file writes <stem>.md instead of a UUID
        tmp_path = Path(input_path)
        if tmp_path.exists() and tmp_path.stem != original_stem:
            renamed = tmp_path.with_name(original_stem + tmp_path.suffix)
            shutil.move(str(tmp_path), str(renamed))
            input_path = str(renamed)

        job = Job(
            id=str(uuid.uuid4()),
            status=PENDING,
            created_at=datetime.now(),
            input_path=input_path,
            kwargs=kwargs,
            name=original_stem,
            job_type=JOB_TRANSCRIPTION,
            post_refine=post_refine,
            post_summarize=post_summarize,
            is_web_upload=is_web_upload,
        )
        self._jobs[job.id] = job
        if on_complete is not None:
            self._on_complete_callbacks[job.id] = on_complete
        self._queue.put_nowait(job.id)
        return job

    def submit_llm(
        self,
        transcript_path: str,
        job_type: str,
        name: str = "",
    ) -> Job:
        """Enqueue a standalone refine or summarize LLM job."""
        from pathlib import Path

        display_name = name or Path(transcript_path).stem
        label = "Refine" if job_type == JOB_REFINE else "Summarize"
        job = Job(
            id=str(uuid.uuid4()),
            status=PENDING,
            created_at=datetime.now(),
            input_path=transcript_path,
            kwargs={},
            name=f"{label}: {display_name}",
            job_type=job_type,
            llm_transcript_path=transcript_path,
        )
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        return job

    def submit_enroll(
        self,
        md_path: str,
        transcript_name: str,
        groups: dict[str, list[str]],
        device: str = "cpu",
    ) -> Job:
        """Enqueue the slow half of a speaker-enrollment wizard submission.

        The fast half (renaming the transcript body) has already happened
        synchronously in the route via ``enroll_shared.apply_renames()`` --
        this job only runs WAV conversion + embedding extraction
        (``enroll_shared.enroll_profiles()``), which is what used to block
        the browser tab for 30-120s.

        ``output_path`` is set to ``md_path`` immediately (not just on
        completion, unlike the LLM jobs) so the job detail page's "View
        transcript" link works even while the job is still running -- the
        rename already happened, only enrollment is pending.
        """
        job = Job(
            id=str(uuid.uuid4()),
            status=PENDING,
            created_at=datetime.now(),
            input_path=md_path,
            kwargs={},
            name=f"Enroll: {transcript_name}",
            job_type=JOB_ENROLL,
            output_path=md_path,
            enroll_md_path=md_path,
            enroll_groups=groups,
            enroll_device=device,
        )
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        return job

    def submit_standalone_enroll(
        self,
        upload_path: str,
        *,
        profile_key: str,
        display_name: str,
        role: str = "",
        notes: str = "",
        update: bool = False,
    ) -> Job:
        """Enqueue a standalone speaker enrollment from an uploaded file (R6).

        The /speakers/enroll route used to run WAV conversion, diarization,
        and embedding extraction synchronously inside the request — blocking
        the event loop for minutes and touching the module-level ML caches
        concurrently with any running job's worker thread. It now saves the
        upload and hands off here.

        Temp-file ownership moves to the job with it (R9-1 interplay): the
        ``wisper_enroll_*`` upload is renamed to ``wisper_enrollsrc_<id>``
        at submit time — the same immediate-rename pattern as ``submit()``
        (F5) — so the startup sweep's ``wisper_enroll_*`` glob can never
        match a file a pending job needs. The sweep also clears
        ``wisper_enrollsrc_*`` orphans, which is safe because it only runs
        at startup, when the (in-memory) queue is necessarily empty.
        ``_run_standalone_enroll`` deletes the file in a ``finally``.
        """
        from pathlib import Path
        import shutil

        job_id = str(uuid.uuid4())
        src = Path(upload_path)
        if src.exists() and src.name.startswith("wisper_enroll_"):
            renamed = src.with_name(f"wisper_enrollsrc_{job_id}{src.suffix}")
            shutil.move(str(src), str(renamed))
            src = renamed

        job = Job(
            id=job_id,
            status=PENDING,
            created_at=datetime.now(),
            input_path=str(src),
            kwargs={},
            name=f"Enroll: {display_name}",
            job_type=JOB_ENROLL,
            enroll_mode="standalone",
            enroll_params={
                "profile_key": profile_key,
                "display_name": display_name,
                "role": role,
                "notes": notes,
                "update": bool(update),
            },
        )
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        return job

    def submit_recording_enroll(
        self,
        *,
        recording_id: str,
        discord_uid: str,
        per_user_dir: str,
        profile_key: str,
        display_name: str,
    ) -> Job:
        """Enqueue enrollment of an unbound speaker from a Discord recording's
        per-user audio directory (R6 — was synchronous pydub decode +
        embedding extraction inside the /recordings/{id}/enroll route).

        The per-user audio dir is durable recording storage — never deleted
        by the job. On success the runner also updates the recording's
        speaker bindings and campaign membership (the state changes the
        route used to apply inline after the synchronous enroll).
        """
        job = Job(
            id=str(uuid.uuid4()),
            status=PENDING,
            created_at=datetime.now(),
            input_path=per_user_dir,
            kwargs={},
            name=f"Enroll: {display_name}",
            job_type=JOB_ENROLL,
            enroll_mode="recording",
            enroll_params={
                "recording_id": recording_id,
                "discord_uid": discord_uid,
                "per_user_dir": per_user_dir,
                "profile_key": profile_key,
                "display_name": display_name,
            },
        )
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list_all(self) -> list[Job]:
        # R32-9: sort newest-first by created_at; ties (two jobs submitted
        # within the same clock tick share a `datetime.now()` value) break by
        # insertion order, most-recently-submitted first. `self._jobs` is a
        # plain dict (insertion-ordered since Python 3.7), so its enumerate
        # index is used as an explicit tiebreaker in the sort key instead of
        # the previous "reverse the list, then stable-sort with reverse=True"
        # trick, which relied on sort-stability semantics to get the same
        # result less legibly.
        indexed = enumerate(self._jobs.values())
        return [
            job
            for _, job in sorted(indexed, key=lambda pair: (pair[1].created_at, pair[0]), reverse=True)
        ]

    def list_recent(self, limit: int = 20) -> list[Job]:
        return self.list_all()[:limit]

    def active_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status in (PENDING, RUNNING))

    def cancel(self, job_id: str) -> bool:
        """Request cancellation of a pending or running job."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.status == PENDING:
            job.status = FAILED
            job.error = "Cancelled"
            job.finished_at = datetime.now()
            self._prune_finished_jobs()
            return True
        if job.status == RUNNING:
            job._cancel_event.set()
            return True
        return False

    def _prune_finished_jobs(self) -> None:
        """Cap retained terminal (COMPLETED/FAILED) jobs at
        _MAX_RETAINED_JOBS (R14), dropping the oldest first.

        PENDING/RUNNING jobs are never candidates -- only jobs that have
        already reached a terminal state count against the cap. A pruned
        job simply disappears from the dashboard/jobs list, which is
        acceptable per the review finding (this is a single-user, in-memory
        tool with no persistence expectation between restarts anyway).
        """
        terminal = [j for j in self._jobs.values() if j.status in (COMPLETED, FAILED)]
        if len(terminal) <= _MAX_RETAINED_JOBS:
            return
        terminal.sort(key=lambda j: j.created_at)
        excess = len(terminal) - _MAX_RETAINED_JOBS
        for job in terminal[:excess]:
            self._jobs.pop(job.id, None)
            self._on_complete_callbacks.pop(job.id, None)

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            if job is None:
                self._queue.task_done()
                continue
            if job.status != PENDING:
                # Job was cancelled (or otherwise moved on) while it sat in
                # the asyncio queue -- do not revive it (R3).
                self._queue.task_done()
                continue
            job.status = RUNNING
            try:
                await asyncio.to_thread(self._run_job, job)
            except Exception as exc:
                job.status = FAILED
                # R13: never put raw exception text (paths, ffmpeg command
                # lines) into job.error — it renders into HTML/SSE. The job
                # runner usually already set a generic message before
                # re-raising; keep it if so.
                if not job.error:
                    _set_job_error(job, exc)
                job.finished_at = datetime.now()
            finally:
                # R14: runs after every job that leaves RUNNING, however it
                # got there (success, failure inside _run_job, or an
                # uncaught exception here) -- exactly once per processed job.
                self._prune_finished_jobs()
                self._queue.task_done()

    def _run_job(self, job: Job) -> None:
        """Dispatch to the appropriate worker based on job_type."""
        if job.job_type in (JOB_REFINE, JOB_SUMMARIZE):
            self._run_llm_job(job)
        elif job.job_type == JOB_ENROLL:
            self._run_enroll_job(job)
        else:
            self._run_transcription_job(job)

    def _run_transcription_job(self, job: Job) -> None:
        """Runs in a thread.  Patches tqdm.write to capture progress logs."""
        from pathlib import Path

        # Disable TMonitor — tqdm's background thread that watches bars for stalls.
        original_monitor_interval = _tqdm_module.tqdm.monitor_interval
        _tqdm_module.tqdm.monitor_interval = 0

        # Patch tqdm.write to capture messages
        original_write = _tqdm_module.tqdm.write

        def capturing_write(msg: str, *args: Any, **kw: Any) -> None:
            if job._cancel_event.is_set():
                raise InterruptedError("Job cancelled by user")
            original_write(msg, *args, **kw)
            stripped = msg.strip()
            if not stripped:
                return
            import re as _re
            m = _re.match(r'^\[progress:(\w+)\]\s*(.*)', stripped)
            if m:
                job.progress_channels[m.group(1)] = m.group(2)
                return
            job.append_log(stripped)

        original_init = _tqdm_module.tqdm.__init__

        import re as _re
        _ansi_escape = _re.compile(r'\x1b\[[0-9;]*[A-Za-z]')

        class ProgressCatcher:
            def write(self, s: str) -> None:
                if job._cancel_event.is_set():
                    raise InterruptedError("Job cancelled by user")
                clean = _ansi_escape.sub('', s)
                for part in clean.split('\r'):
                    stripped = part.strip()
                    if stripped:
                        job.progress = stripped
            def flush(self) -> None:
                pass

        def capturing_init(self, *args: Any, **kwargs: Any) -> None:
            kwargs["file"] = ProgressCatcher()
            kwargs["dynamic_ncols"] = False
            kwargs["ncols"] = 100
            original_init(self, *args, **kwargs)

        _tqdm_module.tqdm.write = capturing_write  # type: ignore[method-assign]
        _tqdm_module.tqdm.__init__ = capturing_init  # type: ignore[method-assign]
        try:
            _result_store: dict = {}
            output_path = process_file(Path(job.input_path), _result_store=_result_store, job_id=job.id, **job.kwargs)
            job.diarization_segments = _result_store.get("diarization_segments", [])
            job.speaker_map = _result_store.get("speaker_map", {})
            job.output_path = str(output_path)

            # F5: move the temp web upload next to the transcript so the
            # enrollment sidecar's input_path is durable across restarts and
            # the tempdir copy doesn't leak. Must happen before excerpt
            # extraction and the sidecar write so both use the durable path.
            #
            # Only move it when there's diarization data: no segments means
            # _write_enrollment_sidecar (below) never writes a _diar.json, so
            # a moved copy would sit in the output dir with nothing recording
            # its path -- an unreclaimable leak in exactly the spot F5 is
            # fixing. With no enrollment wizard possible, just delete the temp
            # file, same as the failure path.
            if job.is_web_upload:
                if job.diarization_segments:
                    job.input_path = _move_upload_to_output(job.input_path, output_path)
                else:
                    _delete_temp_upload(job)
                # Either way the temp-upload obligation is discharged: never
                # let a later failure (e.g. in post-processing) fall into the
                # except-block cleanup and delete what is now either the
                # user's durable transcript-adjacent audio or already gone.
                job.is_web_upload = False

            _extract_speaker_excerpts(job, output_path,
                                      aligned_segments=_result_store.get("aligned_segments", []),
                                      diarization_segments=job.diarization_segments)
            _write_enrollment_sidecar(job, output_path)

            # Chain LLM post-processing if requested; defer COMPLETED until done
            if job.post_refine or job.post_summarize:
                self._run_post_process(job, Path(output_path))

            job.status = COMPLETED
            _cb = self._on_complete_callbacks.pop(job.id, None)
            if _cb is not None:
                try:
                    _cb(job)
                except Exception:
                    pass  # callback failure must not fail the job

        except InterruptedError:
            job.status = FAILED
            job.error = "Cancelled"
            _delete_temp_upload(job)
        except Exception as exc:
            job.status = FAILED
            _set_job_error(job, exc)  # R13: generic message, real exc logged
            _delete_temp_upload(job)
            raise
        finally:
            _tqdm_module.tqdm.write = original_write  # type: ignore[method-assign]
            _tqdm_module.tqdm.__init__ = original_init  # type: ignore[method-assign]
            _tqdm_module.tqdm.monitor_interval = original_monitor_interval
            job.finished_at = datetime.now()

    def _run_post_process(self, job: Job, transcript_path: "Path") -> None:  # type: ignore[name-defined]
        """Chain refine and/or summarize after a completed transcription job.

        Called from within _run_transcription_job, still in the job thread.
        sys.stderr is redirected to capture Ollama status messages.
        """
        from pathlib import Path

        old_stderr = _sys.stderr
        _sys.stderr = _StderrCapture(job)
        try:
            self._do_llm_work(
                job=job,
                transcript_path=transcript_path,
                do_refine=job.post_refine,
                do_summarize=job.post_summarize,
            )
        except Exception as exc:
            # R13: log_lines render into the job page too — keep the line
            # generic and put the real exception in the server log.
            log.error("Post-processing for job %s failed", job.id, exc_info=exc)
            job.append_log("Post-processing failed — see server logs")
        finally:
            _sys.stderr = old_stderr

    def _run_llm_job(self, job: Job) -> None:
        """Run a standalone refine or summarize LLM job in a thread."""
        from pathlib import Path

        transcript_path = Path(job.llm_transcript_path or job.input_path)

        old_stderr = _sys.stderr
        _sys.stderr = _StderrCapture(job)
        try:
            self._do_llm_work(
                job=job,
                transcript_path=transcript_path,
                do_refine=(job.job_type == JOB_REFINE),
                do_summarize=(job.job_type == JOB_SUMMARIZE),
            )
            job.status = COMPLETED
        except Exception as exc:
            job.status = FAILED
            _set_job_error(job, exc)  # R13: generic message, real exc logged
            raise
        finally:
            _sys.stderr = old_stderr
            job.finished_at = datetime.now()

    def _run_enroll_job(self, job: Job) -> None:
        """Dispatch a JOB_ENROLL job to its mode-specific runner (R6).

        All three runners share the enroll-job error policy: exceptions are
        never re-raised after being recorded on ``job.error``. ``_worker()``
        would otherwise handle the exception again, and exception text for
        this job type can contain a filesystem path (e.g. a WAV-conversion
        failure message) that the job detail page renders directly into
        HTML. Per the security rules (never reflect paths/exception text
        into a response), every failure path sets a generic message instead
        and swallows the exception locally (logging it server-side).
        """
        if job.enroll_mode == "standalone":
            self._run_standalone_enroll(job)
            return
        if job.enroll_mode == "recording":
            self._run_recording_enroll(job)
            return
        self._run_wizard_enroll(job)

    def _run_standalone_enroll(self, job: Job) -> None:
        """Run a standalone /speakers/enroll upload job in a thread (R6).

        Owns the renamed temp upload (``wisper_enrollsrc_*``): it is deleted
        in the ``finally`` — success or failure — along with the converted
        WAV, the cleanup that lived in the route's ``finally`` before the
        hand-off (R9-1).
        """
        from collections import defaultdict
        from pathlib import Path

        p = job.enroll_params
        tmp_path = Path(job.input_path)
        wav_path = tmp_path
        try:
            if not tmp_path.exists():
                job.status = FAILED
                job.error = "Source audio not available"
                return

            from wisper_transcribe.audio_utils import convert_to_wav
            from wisper_transcribe.config import get_device, get_hf_token, load_config
            from wisper_transcribe.diarizer import diarize
            from wisper_transcribe.speaker_manager import (
                enroll_speaker,
                extract_embedding,
                load_profiles,
                update_embedding,
            )

            config = load_config()
            device = get_device()
            hf_token = get_hf_token(config)

            job.append_log("Converting audio…")
            wav_path = convert_to_wav(tmp_path)

            job.append_log("Detecting speech…")
            diarization = diarize(wav_path, hf_token=hf_token, device=device)

            # Primary speaker = label with the most total speech time.
            speaker_time: dict[str, float] = defaultdict(float)
            for seg in diarization:
                speaker_time[seg.speaker] += seg.end - seg.start
            if not speaker_time:
                job.status = FAILED
                job.error = "No speech detected in the uploaded audio"
                return
            primary_label = max(speaker_time, key=lambda k: speaker_time[k])

            job.append_log(f"Extracting embedding for {p['display_name']}…")
            if p.get("update") and p["profile_key"] in load_profiles():
                new_emb = extract_embedding(wav_path, diarization, primary_label, device)
                update_embedding(p["profile_key"], new_emb)
            else:
                enroll_speaker(
                    name=p["profile_key"],
                    display_name=p["display_name"],
                    role=p.get("role", ""),
                    audio_path=wav_path,
                    segments=diarization,
                    speaker_label=primary_label,
                    device=device,
                    notes=p.get("notes", ""),
                )
            job.append_log("Speaker enrolled.")
            job.status = COMPLETED
        except Exception as exc:
            log.error("Standalone enroll job %s failed", job.id, exc_info=exc)
            job.status = FAILED
            job.error = "Enrollment failed"
        finally:
            tmp_path.unlink(missing_ok=True)
            if wav_path != tmp_path:
                wav_path.unlink(missing_ok=True)
            job.finished_at = datetime.now()

    def _run_recording_enroll(self, job: Job) -> None:
        """Enroll an unbound Discord speaker from a recording dir (R6).

        On success, applies the recording-state updates the route used to do
        inline: drop the uid from ``unbound_speakers``, bind it in
        ``discord_speakers``, and add/bind the profile in the recording's
        campaign. Those follow-up updates are best-effort — a failure there
        is logged but doesn't fail the (already successful) enrollment.
        """
        from pathlib import Path

        p = job.enroll_params
        try:
            from wisper_transcribe.config import get_data_dir
            from wisper_transcribe.speaker_manager import enroll_speaker_from_audio_dir

            data_dir = get_data_dir()

            job.append_log(f"Enrolling {p['display_name']} from recording audio…")
            enroll_speaker_from_audio_dir(
                name=p["profile_key"],
                display_name=p["display_name"],
                role="player",
                per_user_dir=Path(p["per_user_dir"]),
                data_dir=data_dir,
            )
        except Exception as exc:
            log.error("Recording enroll job %s failed", job.id, exc_info=exc)
            job.status = FAILED
            job.error = "Enrollment failed"
            job.finished_at = datetime.now()
            return

        try:
            from wisper_transcribe.campaign_manager import (
                add_member,
                bind_discord_id,
                load_campaigns,
            )
            from wisper_transcribe.recording_manager import load_recordings, save_recording

            recordings = load_recordings(data_dir)
            rec = recordings.get(p["recording_id"])
            if rec is not None:
                rec.unbound_speakers = [
                    uid for uid in rec.unbound_speakers if uid != p["discord_uid"]
                ]
                rec.discord_speakers[p["discord_uid"]] = p["profile_key"]
                save_recording(rec, data_dir)

                if rec.campaign_slug:
                    campaigns = load_campaigns(data_dir)
                    if rec.campaign_slug in campaigns:
                        if p["profile_key"] not in campaigns[rec.campaign_slug].members:
                            add_member(rec.campaign_slug, p["profile_key"], data_dir=data_dir)
                        bind_discord_id(
                            rec.campaign_slug, p["profile_key"], p["discord_uid"],
                            data_dir=data_dir,
                        )
        except Exception:
            log.warning(
                "Failed to update recording state after enrollment", exc_info=True
            )

        job.append_log("Speaker enrolled.")
        job.status = COMPLETED
        job.finished_at = datetime.now()

    def _run_wizard_enroll(self, job: Job) -> None:
        """Run the slow half of a wizard submission (WAV convert + embedding
        extraction) in a thread.

        Mirrors ``_run_llm_job``'s status-transition structure, minus the
        re-raise — see ``_run_enroll_job`` for the error policy.
        """
        from pathlib import Path

        from wisper_transcribe.web.enroll_shared import _load_diar_sidecar

        md_path = Path(job.enroll_md_path or job.output_path or "")
        diar = _load_diar_sidecar(md_path)
        if not diar:
            job.status = FAILED
            job.error = "Source audio not available"
            job.finished_at = datetime.now()
            return

        input_path = Path(diar.get("input_path", ""))
        if not input_path.exists():
            job.status = FAILED
            job.error = "Source audio not available"
            job.finished_at = datetime.now()
            return

        from wisper_transcribe.models import DiarizationSegment

        segments = [
            DiarizationSegment(start=s["start"], end=s["end"], speaker=s["speaker"])
            for s in diar.get("diarization_segments", [])
        ]
        campaign_slug = diar.get("campaign")

        def _progress(msg: str) -> None:
            job.append_log(msg)

        try:
            from wisper_transcribe.web.enroll_shared import enroll_profiles

            enroll_profiles(
                input_path=input_path,
                segments=segments,
                groups=job.enroll_groups,
                campaign_slug=campaign_slug,
                device=job.enroll_device,
                progress=_progress,
            )
            job.status = COMPLETED
        except Exception:
            job.status = FAILED
            job.error = "Enrollment failed"
        finally:
            job.finished_at = datetime.now()

    def _do_llm_work(
        self,
        job: Job,
        transcript_path: "Path",  # type: ignore[name-defined]
        do_refine: bool,
        do_summarize: bool,
    ) -> None:
        """Core LLM logic shared by post-processing and standalone LLM jobs."""
        from wisper_transcribe.config import load_config
        from wisper_transcribe.llm import get_client
        from wisper_transcribe.llm.errors import LLMUnavailableError, LLMResponseError
        from wisper_transcribe.speaker_manager import load_profiles

        cfg = load_config()
        client = get_client(cfg.get("llm_provider", "ollama"), config=cfg)
        provider = getattr(client, "provider", "")
        model = getattr(client, "model", "")
        job.append_log(f"LLM: {provider} / {model}")

        profiles = load_profiles()
        md = transcript_path.read_text(encoding="utf-8")

        if do_refine:
            from wisper_transcribe.refine import refine_transcript
            hotwords = list(cfg.get("hotwords", []) or [])
            character_names: list[str] = []
            for p in profiles.values():
                if p.notes:
                    for token in p.notes.replace(";", ",").split(","):
                        t = token.strip()
                        if t and not t.lower().startswith("voice_of:"):
                            character_names.append(t)
            job.append_log(f"Refining vocabulary in {transcript_path.name} ...")
            try:
                refined_md, edits, _unresolved = refine_transcript(
                    md,
                    client=client,
                    hotwords=hotwords,
                    character_names=character_names,
                    profiles=profiles,
                    tasks=["vocabulary"],
                )
            except (LLMUnavailableError, LLMResponseError) as exc:
                job.append_log(f"Refine failed: {exc}")
                refined_md, edits = md, []
            if edits and refined_md != md:
                backup = transcript_path.with_suffix(transcript_path.suffix + ".bak")
                backup.write_text(md, encoding="utf-8")
                transcript_path.write_text(refined_md, encoding="utf-8")
                job.append_log(
                    f"Applied {len(edits)} edit(s). Backup: {backup.name}"
                )
                md = refined_md
            else:
                job.append_log("No vocabulary changes needed.")
            job.output_path = str(transcript_path)

        if do_summarize:
            from wisper_transcribe.summarize import (
                summarize_transcript,
                default_summary_path,
                render_markdown,
            )
            job.append_log(
                f"Generating campaign summary for {transcript_path.name} ..."
            )
            try:
                note = summarize_transcript(
                    md,
                    profiles,
                    client,
                    source_transcript=transcript_path.name,
                )
                out_path = default_summary_path(transcript_path)
                body = render_markdown(note, profiles=profiles)
                out_path.write_text(body, encoding="utf-8")
                job.append_log(f"Summary written: {out_path.name}")
                job.summary_path = str(out_path)
            except (LLMUnavailableError, LLMResponseError) as exc:
                job.append_log(f"Summarize failed: {exc}")
            job.output_path = str(transcript_path)
