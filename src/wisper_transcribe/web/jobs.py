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


@dataclass
class Job:
    id: str
    status: str
    created_at: datetime
    input_path: str
    kwargs: dict[str, Any]
    output_path: Optional[str] = None
    error: Optional[str] = None
    log_lines: list[str] = field(default_factory=list)
    finished_at: Optional[datetime] = None
    # Set after transcription completes when enroll flow is needed
    diarization_labels: list[str] = field(default_factory=list)


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
        """Enqueue a transcription job.  Returns the Job immediately."""
        job = Job(
            id=str(uuid.uuid4()),
            status=PENDING,
            created_at=datetime.now(),
            input_path=input_path,
            kwargs=kwargs,
        )
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list_all(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def list_recent(self, limit: int = 20) -> list[Job]:
        return self.list_all()[:limit]

    def active_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status in (PENDING, RUNNING))

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

        # Patch tqdm.write to capture messages
        original_write = _tqdm_module.tqdm.write

        def capturing_write(msg: str, *args: Any, **kw: Any) -> None:
            original_write(msg, *args, **kw)
            if msg.strip():
                job.log_lines.append(msg.strip())

        _tqdm_module.tqdm.write = capturing_write  # type: ignore[method-assign]
        try:
            output_path = process_file(Path(job.input_path), **job.kwargs)
            job.output_path = str(output_path)
            job.status = COMPLETED
        except Exception as exc:
            job.status = FAILED
            job.error = str(exc)
            raise
        finally:
            _tqdm_module.tqdm.write = original_write  # type: ignore[method-assign]
            job.finished_at = datetime.now()
