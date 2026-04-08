"""In-process job queue for background transcription tasks.

Design:
- Jobs are stored in-memory (dict keyed by UUID).  Single-user tool — no
  persistence needed between restarts.
- One background asyncio task drains a FIFO queue.  Each job runs
  process_file() in a thread via asyncio.to_thread() so the event loop
  stays responsive during long transcription runs.
- Thread safety: the module-level _model / _pipeline globals in transcriber /
  diarizer are NOT thread-safe.  We run exactly ONE job at a time (max_workers=1
  thread pool semantics).  Future: use ProcessPoolExecutor for CPU multi-worker
  web deployments, same guard logic as Phase 10.
- Progress: process_file() uses tqdm.write() for status messages.  We
  monkey-patch tqdm.write per-job so messages are captured into job.log_lines
  and streamed to the browser via Server-Sent Events.
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import tqdm as _tqdm_module

from wisper_transcribe.pipeline import process_file

# Job status literals
PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"

_EXCERPT_SECONDS = 12  # length of each speaker audio clip


def _extract_speaker_excerpts(job: "Job", output_path: "Path") -> None:  # type: ignore[name-defined]
    """Extract a short audio clip per speaker from the transcribed file.

    Parses the output markdown for the first timestamp of each speaker, then
    uses ffmpeg to cut a ~12s clip from the original input audio.  Clips are
    saved alongside the transcript as <stem>_excerpt_<speaker>.mp3 so they
    can be served to the browser during the enrollment wizard.

    Failures are silently swallowed — playback is a nice-to-have, not critical.
    """
    import re
    import subprocess
    from pathlib import Path as _Path

    try:
        content = _Path(output_path).read_text(encoding="utf-8")
    except Exception:
        return

    # Match lines like: **Alice** *(00:01:23)*: …  or  **Bob** *(1:23)*: …
    pattern = re.compile(r"\*\*(.+?)\*\*\s+\*\((\d+:\d{2}(?::\d{2})?)\)\*")
    first_ts: dict[str, float] = {}
    for m in pattern.finditer(content):
        speaker = m.group(1)
        ts_str = m.group(2)
        if speaker in first_ts:
            continue
        parts = ts_str.split(":")
        if len(parts) == 2:
            secs = int(parts[0]) * 60 + int(parts[1])
        else:
            secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        first_ts[speaker] = float(secs)

    if not first_ts:
        return

    out_dir = _Path(output_path).parent
    stem = _Path(output_path).stem
    input_path = _Path(job.input_path)

    for speaker, start in first_ts.items():
        safe_name = re.sub(r"[^\w\-]", "_", speaker)
        clip_path = out_dir / f"{stem}_excerpt_{safe_name}.mp3"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", str(start),
                    "-t", str(_EXCERPT_SECONDS),
                    "-i", str(input_path),
                    "-ac", "1",
                    "-ar", "22050",
                    "-b:a", "64k",
                    str(clip_path),
                ],
                check=True,
                capture_output=True,
            )
            job.speaker_excerpts[speaker] = str(clip_path)
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
    output_path: Optional[str] = None
    error: Optional[str] = None
    log_lines: list[str] = field(default_factory=list)
    progress: Optional[str] = None
    finished_at: Optional[datetime] = None
    # Set after transcription completes when enroll flow is needed
    diarization_labels: list[str] = field(default_factory=list)
    # speaker_label -> path to a short audio excerpt (for enrollment wizard playback)
    speaker_excerpts: dict[str, str] = field(default_factory=dict)
    # Threading event set by cancel() to signal the worker to abort
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)


class JobQueue:
    """In-memory job queue backed by a single asyncio background worker."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

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

    def submit(self, input_path: str, **kwargs: Any) -> Job:
        """Enqueue a transcription job.  Returns the Job immediately.

        If ``original_stem`` is provided in kwargs it is used as the job's
        human-readable name and to rename the temp upload file so the output
        .md inherits the original filename.  It is stripped from kwargs before
        being forwarded to process_file.
        """
        from pathlib import Path
        import shutil

        original_stem: str = kwargs.pop("original_stem", "")
        if not original_stem:
            original_stem = Path(input_path).stem

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
        )
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list_all(self) -> list[Job]:
        # Reverse insertion order first so that ties in created_at put newer jobs first
        return sorted(list(self._jobs.values())[::-1], key=lambda j: j.created_at, reverse=True)

    def list_recent(self, limit: int = 20) -> list[Job]:
        return self.list_all()[:limit]

    def active_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status in (PENDING, RUNNING))

    def cancel(self, job_id: str) -> bool:
        """Request cancellation of a pending or running job.

        Pending jobs are immediately marked failed.  Running jobs set their
        cancel event; the worker checks it and aborts the pipeline thread via
        a raised exception on the next tqdm heartbeat.

        Returns True if the job existed and was cancellable.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.status == PENDING:
            job.status = FAILED
            job.error = "Cancelled"
            job.finished_at = datetime.now()
            return True
        if job.status == RUNNING:
            job._cancel_event.set()
            return True
        return False

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            if job is None:
                continue
            job.status = RUNNING
            try:
                await asyncio.to_thread(self._run_job, job)
            except Exception as exc:
                job.status = FAILED
                job.error = str(exc)
                job.finished_at = datetime.now()
            finally:
                self._queue.task_done()

    def _run_job(self, job: Job) -> None:
        """Runs in a thread.  Patches tqdm.write to capture progress logs."""
        from pathlib import Path

        # Disable TMonitor — tqdm's background thread that watches bars for stalls.
        # It registers an atexit callback that join()s the thread, which hangs on
        # shutdown (especially on Python 3.14's stricter thread cleanup).
        original_monitor_interval = _tqdm_module.tqdm.monitor_interval
        _tqdm_module.tqdm.monitor_interval = 0

        # Patch tqdm.write to capture messages
        original_write = _tqdm_module.tqdm.write

        def capturing_write(msg: str, *args: Any, **kw: Any) -> None:
            if job._cancel_event.is_set():
                raise InterruptedError("Job cancelled by user")
            original_write(msg, *args, **kw)
            if msg.strip():
                job.log_lines.append(msg.strip())

        # Patch tqdm.__init__ to capture the progress bar itself
        original_init = _tqdm_module.tqdm.__init__

        import re as _re
        _ansi_escape = _re.compile(r'\x1b\[[0-9;]*[A-Za-z]')

        class ProgressCatcher:
            def write(self, s: str) -> None:
                # Check cancel on every progress tick (tqdm bar update fires this
                # frequently during transcription, so this is the reliable cancel point)
                if job._cancel_event.is_set():
                    raise InterruptedError("Job cancelled by user")
                # Strip ANSI control sequences (cursor-up, clear-line, colour codes, etc.)
                clean = _ansi_escape.sub('', s)
                # tqdm updates the same line using carriage returns (\r)
                for part in clean.split('\r'):
                    stripped = part.strip()
                    if stripped:
                        job.progress = stripped
            def flush(self) -> None:
                pass

        def capturing_init(self, *args: Any, **kwargs: Any) -> None:
            kwargs["file"] = ProgressCatcher()
            kwargs["dynamic_ncols"] = False  # Avoids console size errors in background threads
            kwargs["ncols"] = 100            # Keeps the text output clean and predictable
            original_init(self, *args, **kwargs)

        _tqdm_module.tqdm.write = capturing_write  # type: ignore[method-assign]
        _tqdm_module.tqdm.__init__ = capturing_init  # type: ignore[method-assign]
        try:
            output_path = process_file(Path(job.input_path), **job.kwargs)
            job.output_path = str(output_path)
            job.status = COMPLETED
            _extract_speaker_excerpts(job, output_path)
        except InterruptedError:
            job.status = FAILED
            job.error = "Cancelled"
            # Do not re-raise — cancellation is intentional, not an error
        except Exception as exc:
            job.status = FAILED
            job.error = str(exc)
            raise
        finally:
            _tqdm_module.tqdm.write = original_write  # type: ignore[method-assign]
            _tqdm_module.tqdm.__init__ = original_init  # type: ignore[method-assign]
            _tqdm_module.tqdm.monitor_interval = original_monitor_interval
            job.finished_at = datetime.now()
