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
"""
from __future__ import annotations

import asyncio
import sys as _sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import tqdm as _tqdm_module

from wisper_transcribe.pipeline import process_file

# Job status literals
PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"

# Job type literals
JOB_TRANSCRIPTION = "transcription"
JOB_REFINE = "refine"
JOB_SUMMARIZE = "summarize"

_EXCERPT_SECONDS = 12  # length of each speaker audio clip


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
                self._job.log_lines.append(stripped)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


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
    # Also capture the text on each speaker line so we can show it in the
    # enrollment wizard alongside the audio clip.
    line_pattern = re.compile(
        r"\*\*(.+?)\*\*\s+\*\((\d+:\d{2}(?::\d{2})?)\)\*[:\s]*(.*)"
    )
    first_ts: dict[str, float] = {}
    first_text: dict[str, str] = {}
    for m in line_pattern.finditer(content):
        speaker, ts_str, text = m.group(1), m.group(2), m.group(3).strip()
        if speaker in first_ts:
            continue
        parts = ts_str.split(":")
        if len(parts) == 2:
            secs = int(parts[0]) * 60 + int(parts[1])
        else:
            secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        first_ts[speaker] = float(secs)
        first_text[speaker] = text

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

        # Persist the transcript snippet to disk so it survives server restarts.
        text_path = out_dir / f"{stem}_excerpt_{safe_name}.txt"
        try:
            text_path.write_text(first_text.get(speaker, ""), encoding="utf-8")
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
    progress: Optional[str] = None
    # Parallel mode: per-channel progress strings keyed by channel name
    progress_channels: dict[str, str] = field(default_factory=dict)
    finished_at: Optional[datetime] = None
    # Set after transcription completes when enroll flow is needed
    diarization_labels: list[str] = field(default_factory=list)
    # Full diarization segments retained for post-job enrollment (enroll_submit uses these)
    diarization_segments: list = field(default_factory=list)
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

    @property
    def is_video(self) -> bool:
        """True when the input file is a video container (not a pure audio file)."""
        from pathlib import Path as _Path
        from wisper_transcribe.audio_utils import VIDEO_EXTENSIONS
        return _Path(self.input_path or "").suffix.lower() in VIDEO_EXTENSIONS


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
        """Request cancellation of a pending or running job."""
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
        """Dispatch to the appropriate worker based on job_type."""
        if job.job_type in (JOB_REFINE, JOB_SUMMARIZE):
            self._run_llm_job(job)
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
            job.log_lines.append(stripped)

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
            job.output_path = str(output_path)
            _extract_speaker_excerpts(job, output_path)

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
        except Exception as exc:
            job.status = FAILED
            job.error = str(exc)
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
            job.log_lines.append(f"Post-processing error: {exc}")
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
            job.error = str(exc)
            raise
        finally:
            _sys.stderr = old_stderr
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
        job.log_lines.append(f"LLM: {provider} / {model}")

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
            job.log_lines.append(f"Refining vocabulary in {transcript_path.name} ...")
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
                job.log_lines.append(f"Refine failed: {exc}")
                refined_md, edits = md, []
            if edits and refined_md != md:
                backup = transcript_path.with_suffix(transcript_path.suffix + ".bak")
                backup.write_text(md, encoding="utf-8")
                transcript_path.write_text(refined_md, encoding="utf-8")
                job.log_lines.append(
                    f"Applied {len(edits)} edit(s). Backup: {backup.name}"
                )
                md = refined_md
            else:
                job.log_lines.append("No vocabulary changes needed.")
            job.output_path = str(transcript_path)

        if do_summarize:
            from wisper_transcribe.summarize import (
                summarize_transcript,
                default_summary_path,
                render_markdown,
            )
            job.log_lines.append(
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
                job.log_lines.append(f"Summary written: {out_path.name}")
                job.summary_path = str(out_path)
            except (LLMUnavailableError, LLMResponseError) as exc:
                job.log_lines.append(f"Summarize failed: {exc}")
            job.output_path = str(transcript_path)
