"""In-process job queue for transcription, LLM, enrollment, journal, and live jobs.

- Jobs live in memory (dict keyed by UUID) and never resume after a restart;
  ``job_history`` records every state change in the ``jobs`` table.
- One asyncio task drains a FIFO queue and runs each job in a thread via
  asyncio.to_thread(), so the event loop stays responsive.
- Exactly one job runs at a time: the transcriber/diarizer/embedding model
  globals are not thread-safe.
- Progress: tqdm.write is patched per job to capture status lines into
  job.log_lines for SSE. LLM jobs redirect sys.stderr the same way (safe only
  because a single job runs at a time).
- Enroll jobs report progress through a plain callback into job.log_lines.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import subprocess
import sys as _sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import tqdm as _tqdm_module

from wisper_transcribe import file_registry
from wisper_transcribe.pipeline import process_file
from wisper_transcribe.transcript_store import atomic_write_text, save_summary, save_transcript

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
# Open-ended job that holds the single worker slot for a whole local capture
# session, transcribing chunks from LocalCaptureManager's live sink.
JOB_LIVE = "live"
JOB_CAMPAIGN_JOURNAL = "campaign_journal"
JOB_SPEAKER_RELABEL = "speaker_relabel"

_MAX_LIVE_LINES = 2000  # mirrors _MAX_LOG_LINES below -- bound a very long session's memory

_EXCERPT_SECONDS = 12  # length of each speaker audio clip

# job.error renders into HTML and SSE, and exception text often contains
# filesystem paths, so every failure maps to one of these generic messages
# and the real exception is logged (see _set_job_error).
_GENERIC_JOB_ERRORS = {
    JOB_TRANSCRIPTION: "Transcription failed — see server logs",
    JOB_REFINE: "Post-processing failed — see server logs",
    JOB_SUMMARIZE: "Post-processing failed — see server logs",
    JOB_ENROLL: "Enrollment failed",
    JOB_SPEAKER_RELABEL: "Speaker re-match failed — see server logs",
}


def _record_history(job: "Job") -> None:
    """Write the job's current state to the ``jobs`` table (never raises)."""
    from wisper_transcribe import job_history
    job_history.record(job)


class TranscriptMissingError(RuntimeError):
    """The pipeline reported a transcript path, but no file is there."""


def _set_job_error(job: "Job", exc: BaseException) -> None:
    """Set a generic, path-free error on *job* and log the real exception.

    ``"Cancelled"`` is kept verbatim; the job-detail template checks for it.
    """
    if isinstance(exc, InterruptedError):
        job.error = "Cancelled"
        return
    log.error("Job %s (%s) failed", job.id, job.job_type, exc_info=exc)
    from wisper_transcribe.transcript_store import TranscriptExistsError
    if isinstance(exc, TranscriptExistsError):
        job.error = "Transcript already exists"
        return
    if isinstance(exc, TranscriptMissingError):
        job.error = "Transcript file missing after write"
        return
    if isinstance(exc, FileNotFoundError):
        # Known input error — short, safe text with no path reflected.
        job.error = "Input file not found"
        return
    job.error = _GENERIC_JOB_ERRORS.get(job.job_type, "Job failed — see server logs")

# In-memory growth caps. Internal resource limits, not user config.
_MAX_RETAINED_JOBS = 50   # cap on retained COMPLETED/FAILED jobs (never PENDING/RUNNING)
_MAX_LOG_LINES = 1000     # cap on Job.log_lines, oldest lines dropped first


def _append_capped(items: list, item: Any, cap: int) -> int:
    """Append ``item``, trimming the oldest entries past ``cap``.

    Returns how many entries were dropped.
    """
    items.append(item)
    overflow = len(items) - cap
    if overflow > 0:
        del items[:overflow]
        return overflow
    return 0


def resume_slice(items: list, dropped: int, last_idx: int) -> tuple[list, int]:
    """Map an absolute produced-so-far index onto the entries still retained.

    Returns the retained slice from ``last_idx`` on and the new absolute index.
    Used by the SSE streams that resume against capped job lists; a client that
    fell more than ``cap`` behind resumes from the oldest retained entry.
    """
    retained_start = dropped
    new_items = items[max(last_idx, retained_start) - retained_start:]
    return new_items, retained_start + len(items)


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


def _write_enrollment_sidecar(job: "Job", output_path: "Path") -> bool:  # type: ignore[name-defined]
    """Store the job's diarization data with ``transcript_store.write_sidecar``:
    speakers and the audio path in the database, segments in ``<stem>_diar.json``.

    Lets the enrollment wizard work after a restart. Failures are swallowed:
    the transcript is already written. Returns True only after the write
    succeeded.

    ``input_path`` is included only for a job that owns its audio copy: a
    recording's audio belongs to the recording, never to the transcript.
    """
    from pathlib import Path as _Path

    if not job.diarization_segments:
        return False

    try:
        out = _Path(output_path)
        sidecar = {
            "diarization_segments": [
                {"start": s.start, "end": s.end, "speaker": s.speaker}
                for s in job.diarization_segments
            ],
            # Authoritative raw label -> display name map; absent in old sidecars.
            "speaker_map": dict(job.speaker_map) if job.speaker_map else {},
        }
        if job.input_path and not job.recording_id:
            sidecar["input_path"] = str(_Path(job.input_path))
        if job.speaker_map:
            from wisper_transcribe.speaker_registry import SOURCE_AUTO
            sidecar["speaker_map_source"] = {label: SOURCE_AUTO for label in job.speaker_map}
        if job.speaker_embeddings:
            from wisper_transcribe.speaker_registry import embeddings_to_sidecar
            sidecar.update(embeddings_to_sidecar(job.speaker_embeddings))
        # Speakers + audio path to the DB, segments to <stem>_diar.json.
        from wisper_transcribe.transcript_store import write_sidecar
        write_sidecar(out, sidecar)
        return True
    except Exception:
        log.warning("Could not store speaker data for %s", _Path(output_path).name, exc_info=True)
        return False


def _keep_audio(job: "Job", output_path: "Path") -> None:  # type: ignore[name-defined]
    """Encode the job's extracted WAV to ``<stem>.flac`` beside the transcript.

    Sets ``job.input_path`` to the audio the transcript should keep: the new
    FLAC, else the transcript's existing registered audio, else ``""``. A
    ``<stem>.flac`` that belongs to another transcript is never replaced, and
    one wisper doesn't own is replaced only on Overwrite. A failed encode
    never costs the transcript the audio it already had.
    """
    from pathlib import Path as _Path

    from wisper_transcribe import transcript_store
    from wisper_transcribe.audio_utils import encode_flac
    from wisper_transcribe.config import get_output_root

    md_path = _Path(output_path)
    out_dir = md_path.parent
    output = get_output_root()
    stem = md_path.stem
    wav = _Path(job.input_path)
    target = transcript_store.safe_path(stem, ".flac", out_dir) or (out_dir / f"{stem}.flac")

    owner = file_registry.Owner.for_path(md_path)
    previous = (file_registry.file_for(owner, "audio", output_dir=output)
                if owner is not None else None)
    previous_path = previous.path if previous is not None and previous.path.is_file() else None

    keep_new = True
    if target.exists():
        own = previous is not None and transcript_store._same_file(previous.path, target)
        if not own and file_registry.is_registered(target, output_dir=output):
            job.append_log(f"Kept no new audio: {target.name} belongs to another transcript")
            keep_new = False
        elif not own and not job.kwargs.get("overwrite"):
            job.append_log(
                f"Kept no new audio: {target.name} already exists and belongs to something else"
            )
            keep_new = False

    if keep_new:
        try:
            encode_flac(wav, target)
            job.input_path = str(target)
            return
        except Exception:
            log.warning("Could not encode the audio for %s", stem, exc_info=True)
            job.append_log("Warning: could not save the audio copy; the transcript is unaffected")

    if previous_path is not None:
        job.input_path = str(previous_path)
        job.append_log("Kept the previous audio")
    else:
        job.input_path = ""


def _delete_temp_upload(job: "Job") -> None:  # type: ignore[name-defined]
    """Delete the job's temp upload folder after failure or cancellation.

    Acts only on a ``wisper_upload_*`` folder the job created, so recording
    audio and every other path are never touched.
    """
    import shutil
    from pathlib import Path as _Path

    if not job.upload_dir:
        return
    folder = _Path(job.upload_dir)
    if folder.name.startswith("wisper_upload_"):
        shutil.rmtree(folder, ignore_errors=True)


def _longest_aligned_segment(aligned_segments: list, label: str) -> Optional[tuple]:
    """Return (start, duration, text) of the label's longest aligned segment, or None.

    Fallback excerpt window when no diarization turn is available. The longest
    block is far more likely to be the speaker than a short, often
    misattributed interjection.
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
    """Save a short clip + text per raw speaker label for the enrollment wizard.

    The clip comes from the label's longest solo diarization turn (same
    selection as embedding extraction) and is clamped to
    ``min(_EXCERPT_SECONDS, turn length)``, so it never runs into another
    speaker's turn. The ``.txt`` holds every aligned word run overlapping the
    clip, in time order, so the text matches what is audible.

    A label with no usable diarization turn falls back to its longest aligned
    segment with the full window; one label's fallback never affects another.

    Files are ``<stem>_excerpt_<raw_label>.mp3``/``.txt`` next to the
    transcript. Failures are swallowed; playback is optional.
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
    safe_names: list[str] = []

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
        safe_names.append(safe_name)
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
            atomic_write_text(text_path, text)
        except Exception:
            pass

    # Two labels that sanitise to one name wrote one file; the second add updates its row.
    from wisper_transcribe import db
    from wisper_transcribe.config import get_output_root
    from wisper_transcribe.transcript_store import locate_path as _locate_path
    try:
        with db.transaction() as conn:
            loc = _locate_path(_Path(output_path), conn=conn)
            owner = file_registry.Owner("transcript", loc.id) if loc is not None else None
            for name in safe_names:
                for kind, suffix in (("excerpt", ".mp3"), ("excerpt_text", ".txt")):
                    path = out_dir / f"{stem}_excerpt_{name}{suffix}"
                    if path.is_file():
                        file_registry.add_if_owned(path, kind=kind, owner=owner, label=name,
                                                   conn=conn, output_dir=get_output_root())
    except Exception:
        log.warning("Could not register speaker excerpts for %s", stem, exc_info=True)


@dataclass
class Job:
    id: str
    status: str
    created_at: datetime
    input_path: str
    kwargs: dict[str, Any]
    # Human-readable name shown in the UI (defaults to input filename stem)
    name: str = ""
    # one of the JOB_* constants
    job_type: str = JOB_TRANSCRIPTION
    output_path: Optional[str] = None
    error: Optional[str] = None
    log_lines: list[str] = field(default_factory=list)
    # Lines trimmed from the front of log_lines; lets the SSE stream keep
    # absolute indices valid (see resume_slice).
    log_lines_dropped: int = 0
    progress: Optional[str] = None
    # Parallel mode: per-channel progress strings keyed by channel name
    progress_channels: dict[str, str] = field(default_factory=dict)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    # Set after transcription completes when enroll flow is needed
    diarization_labels: list[str] = field(default_factory=list)
    # Full diarization segments retained for post-job enrollment (enroll_submit uses these)
    diarization_segments: list = field(default_factory=list)
    # Raw label -> display name map the formatter used; persisted to _diar.json.
    speaker_map: dict[str, str] = field(default_factory=dict)
    # Raw label -> voice embedding from matching; persisted to _diar.json.
    speaker_embeddings: dict = field(default_factory=dict)
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
    # The transcript's row id, resolved from output_path or set at submit.
    transcript_id: Optional[int] = None
    # For summarize jobs: path to the generated .summary.md file
    summary_path: Optional[str] = None
    # True when input_path came from a wisper_upload_* temp file, judged from
    # the original basename at submit. Only these are ever extracted and deleted.
    is_web_upload: bool = False
    # The job's ``wisper_upload_<job-id>`` temp folder (the moved upload and
    # its extracted WAV); "" for any other input. In memory only.
    upload_dir: str = ""
    # Frozen at submit from the original input, so the Extract step doesn't
    # change when ``input_path`` does. None: derive from ``input_path``.
    needs_extraction_flag: Optional[bool] = None
    # Transcription of a recording's combined track: which recording. The
    # recording's "transcribing" status and job link are derived from this.
    recording_id: Optional[str] = None
    # JOB_ENROLL: transcript path. The runner re-reads its _diar.json sidecar
    # rather than carrying segments on the job.
    enroll_md_path: Optional[str] = None
    # JOB_ENROLL: validated rename groups (display_name -> [raw_label, ...]).
    enroll_groups: dict[str, list[str]] = field(default_factory=dict)
    # For JOB_ENROLL jobs: device to run embedding extraction on.
    enroll_device: str = "cpu"
    # JOB_ENROLL mode: "wizard" (post-transcription wizard), "standalone"
    # (/speakers/enroll upload), or "recording" (Discord per-user track).
    enroll_mode: str = "wizard"
    # Mode-specific string parameters for standalone/recording enroll jobs.
    enroll_params: dict[str, Any] = field(default_factory=dict)
    # JOB_LIVE: the Recording.id being transcribed. Looked up by scanning
    # jobs (find_live_job_for_recording) rather than stored on the Recording.
    live_recording_id: Optional[str] = None
    # Fed by LocalCaptureManager's tick thread via set_live_sink().
    live_ring_buffer: Any = None
    # Set by stop_live(). Ending a session is normal completion, distinct from
    # _cancel_event (which fails the job as "Cancelled").
    live_stop_event: threading.Event = field(
        default_factory=threading.Event, repr=False, compare=False
    )
    # recordings/<id>/live_transcript.md; SSE reads job.live_lines instead.
    live_output_path: Optional[str] = None
    # Committed lines so far, as plain dicts (LiveLine.to_dict()) so the SSE
    # route can json.dumps them directly -- see GET /recordings/{id}/live.
    live_lines: list[dict] = field(default_factory=list)
    live_lines_dropped: int = 0  # mirrors log_lines_dropped -- see append_live_line()

    def append_log(self, line: str) -> None:
        """Append a log line, trimming the oldest past _MAX_LOG_LINES.

        Drops are counted in ``log_lines_dropped`` so SSE indices stay valid.
        """
        self.log_lines_dropped += _append_capped(self.log_lines, line, _MAX_LOG_LINES)

    def append_live_line(self, line_dict: dict) -> None:
        """Append one committed live-transcript line (JOB_LIVE), trimming
        the oldest once _MAX_LIVE_LINES is exceeded -- same bounded-memory
        pattern as append_log()/_MAX_LOG_LINES, sized for a multi-hour
        session's line count rather than a transcription job's log chatter.
        """
        self.live_lines_dropped += _append_capped(self.live_lines, line_dict, _MAX_LIVE_LINES)

    @property
    def will_align(self) -> bool:
        """True when this transcription job runs forced word alignment.

        Mirrors process_file(): only with diarization (not ``no_diarize``, and
        a HuggingFace token available) and when ``forced_alignment`` resolves
        on for the device. Drives the job page's Align step.
        """
        if self.job_type != JOB_TRANSCRIPTION or self.kwargs.get("no_diarize"):
            return False
        import os
        from wisper_transcribe.config import forced_alignment_enabled, get_device, load_config

        config = load_config()
        if not (os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
                or config.get("hf_token")):
            return False
        device = self.kwargs.get("device") or "auto"
        if device == "auto":
            device = get_device()
        setting = self.kwargs.get("forced_alignment")
        if setting is None:
            setting = config.get("forced_alignment", "auto")
        return forced_alignment_enabled(setting, device)

    @property
    def needs_extraction(self) -> bool:
        """True when the input must be streamed through ffmpeg before transcription.

        Anything that isn't a `.wav` (mp3, m4a, m4b, flac, ogg, mp4, mkv, …)
        is converted to 16 kHz mono WAV via `_extract_first_audio_track`.
        WAVs are passthrough-checked and may also re-encode silently if their
        rate/channels are wrong — but the common case is no extraction.
        """
        if self.needs_extraction_flag is not None:
            return self.needs_extraction_flag
        from pathlib import Path as _Path
        return _Path(self.input_path or "").suffix.lower() != ".wav"


class JobQueue:
    """In-memory job queue backed by a single asyncio background worker."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._on_complete_callbacks: dict[str, Callable[["Job"], None]] = {}
        self._on_error_callbacks: dict[str, Callable[["Job"], None]] = {}

    def _enqueue(self, job: Job) -> None:
        """Track a new job, record it in history, and queue it."""
        self._jobs[job.id] = job
        _record_history(job)
        self._queue.put_nowait(job.id)

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
        on_error: Optional[Callable[["Job"], None]] = None,
        **kwargs: Any,
    ) -> Job:
        """Enqueue a transcription job and return it.

        ``original_stem`` names the job and the transcript (it reaches
        ``process_file`` as ``output_stem``); a temp upload is moved into a
        ``wisper_upload_<job-id>`` folder the job deletes when it ends.
        ``source_name`` is forwarded as the frontmatter ``source_file``.
        ``recording_id``, ``post_refine`` and ``post_summarize`` are stripped
        before forwarding; the last two chain LLM post-processing.

        ``on_complete`` / ``on_error`` run in the worker thread when the job
        completes or fails (including cancellation), so callers with external
        state (e.g. a Recording's status) can update it. ``on_complete`` runs
        before the job reads as completed.
        """
        from pathlib import Path
        import re
        import shutil

        job_id = str(uuid.uuid4())
        original_stem: str = re.split(r"[\\/]", kwargs.pop("original_stem", "") or "")[-1]
        recording_id: Optional[str] = kwargs.pop("recording_id", None)
        post_refine: bool = bool(kwargs.pop("post_refine", False))
        post_summarize: bool = bool(kwargs.pop("post_summarize", False))

        if not original_stem:
            original_stem = Path(input_path).stem
        if not kwargs.get("source_name"):
            kwargs.pop("source_name", None)

        # The transcript is named after the job, not after the input file.
        kwargs["output_stem"] = original_stem

        # Freeze the alignment setting at submit so the job page's Align step
        # and the run agree even if the config changes while queued.
        if kwargs.get("forced_alignment") is None:
            from wisper_transcribe.config import load_config
            kwargs["forced_alignment"] = load_config().get("forced_alignment", "auto")

        tmp_path = Path(input_path)
        is_web_upload = tmp_path.name.startswith("wisper_upload_")
        needs_extraction = tmp_path.suffix.lower() != ".wav"

        # An upload gets a folder of its own so the job can delete everything
        # it made, whatever the file ends up called.
        upload_dir = ""
        if is_web_upload and tmp_path.exists():
            folder = tmp_path.parent / f"wisper_upload_{job_id}"
            folder.mkdir()
            try:
                moved = folder / (original_stem + tmp_path.suffix)
                try:
                    shutil.move(str(tmp_path), str(moved))
                except OSError:
                    # A name the filesystem refuses (e.g. a Windows-reserved one).
                    moved = folder / ("upload" + tmp_path.suffix)
                    shutil.move(str(tmp_path), str(moved))
            except OSError:
                shutil.rmtree(folder, ignore_errors=True)
                raise
            upload_dir = str(folder)
            input_path = str(moved)

        job = Job(
            id=job_id,
            status=PENDING,
            created_at=datetime.now(),
            input_path=input_path,
            kwargs=kwargs,
            name=original_stem,
            job_type=JOB_TRANSCRIPTION,
            post_refine=post_refine,
            post_summarize=post_summarize,
            is_web_upload=is_web_upload,
            upload_dir=upload_dir,
            needs_extraction_flag=needs_extraction,
            recording_id=recording_id,
        )
        if on_complete is not None:
            self._on_complete_callbacks[job.id] = on_complete
        if on_error is not None:
            self._on_error_callbacks[job.id] = on_error
        # Record the transcript the job will write, resolved from the output
        # dir at submit, so a pending re-transcribe or overwrite is found by
        # the busy guard and job history even before the file exists.
        output_dir = kwargs.get("output_dir")
        if output_dir:
            from wisper_transcribe.transcript_store import locate_path
            loc = locate_path(Path(output_dir) / f"{original_stem}.md")
            if loc is not None:
                job.transcript_id = loc.id
        self._enqueue(job)
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
        self._enqueue(job)
        return job

    def submit_enroll(
        self,
        md_path: str,
        transcript_name: str,
        groups: dict[str, list[str]],
        device: str = "cpu",
    ) -> Job:
        """Enqueue embedding extraction for a wizard submission.

        Renames were already applied in the route (``apply_renames()``).
        ``output_path`` is set now so "View transcript" works while the job runs.
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
        self._enqueue(job)
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
        """Enqueue a standalone enrollment from an uploaded file.

        The ``wisper_enroll_*`` upload is renamed to ``wisper_enrollsrc_<id>``
        now so the startup sweep's ``wisper_enroll_*`` glob never matches a file
        a pending job needs. ``_run_standalone_enroll`` deletes it in a ``finally``.
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
        self._enqueue(job)
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
        """Enqueue enrollment of an unbound Discord speaker from their per-user track.

        The recording's audio is never deleted. On success the runner also binds
        the speaker in the recording and its campaign.
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
        self._enqueue(job)
        return job

    def submit_live(
        self,
        recording_id: str,
        output_path: str,
        model_size: str = "base",
        device: str = "auto",
        compute_type: str = "auto",
        language: Optional[str] = "en",
        mic_label: str = "You",
        noise_floor: Optional[float] = None,
    ) -> Job:
        """Enqueue a JOB_LIVE job for a just-started local capture session.

        Runs until ``stop_live()``, holding the only worker slot for the whole
        session.

        ``mic_label`` replaces "You" on mic-dominant lines ("This is me").
        ``noise_floor`` (default ``NOISE_FLOOR_RMS``) seeds
        ``job.kwargs["noise_floor"]``, which ``set_live_noise_floor()`` can change
        while the job runs; the loop re-reads it every chunk.

        The caller wires ``job.live_ring_buffer.push`` onto
        ``LocalCaptureManager.set_live_sink()``.
        """
        from wisper_transcribe.web.live_transcribe import NOISE_FLOOR_RMS, LiveRingBuffer

        # Model params ride on job.kwargs (already a plain dict field on
        # every Job) rather than adding five more dataclass fields.
        job = Job(
            id=str(uuid.uuid4()),
            status=PENDING,
            created_at=datetime.now(),
            input_path="",
            kwargs={
                "model_size": model_size, "device": device,
                "compute_type": compute_type, "language": language,
                "mic_label": mic_label,
                "noise_floor": noise_floor if noise_floor is not None else NOISE_FLOOR_RMS,
            },
            name=f"Live: {recording_id[:8]}",
            job_type=JOB_LIVE,
            live_recording_id=recording_id,
            live_ring_buffer=LiveRingBuffer(),
            live_output_path=output_path,
        )
        self._enqueue(job)
        return job

    def submit_journal(
        self,
        slug: str,
        name: str = "",
        session_stem: Optional[str] = None,
        fold_all: bool = False,
        rebuild: bool = False,
        resummarize: bool = False,
    ) -> Job:
        """Enqueue a rolling-journal job for a campaign.

        ``rebuild=True`` resets the journal and re-folds every session's
        existing summary (one LLM call each; ``journal.refold_campaign``).
        With ``resummarize=True`` too, every session is re-summarized first
        (two calls each; ``journal.rebuild_campaign``). The route must get the
        user's confirmation first.
        """
        job = Job(
            id=str(uuid.uuid4()),
            status=PENDING,
            created_at=datetime.now(),
            input_path="",
            kwargs={"slug": slug, "session_stem": session_stem,
                    "fold_all": fold_all, "rebuild": rebuild,
                    "resummarize": resummarize},
            name=name or (f"Rebuild journal: {slug}" if rebuild else f"Journal: {slug}"),
            job_type=JOB_CAMPAIGN_JOURNAL,
        )
        self._enqueue(job)
        return job

    def submit_relabel(self, slug: str, name: str = "") -> Job:
        """Enqueue a campaign-wide speaker re-match (``speaker_registry.relabel_campaign``)."""
        job = Job(
            id=str(uuid.uuid4()),
            status=PENDING,
            created_at=datetime.now(),
            input_path="",
            kwargs={"slug": slug},
            name=name or f"Re-match speakers: {slug}",
            job_type=JOB_SPEAKER_RELABEL,
        )
        self._enqueue(job)
        return job

    def set_live_noise_floor(self, job_id: str, noise_floor: float) -> bool:
        """Live-update a running JOB_LIVE job's noise floor. Returns False
        if the job doesn't exist (route layer turns that into a 404)."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.kwargs["noise_floor"] = noise_floor
        return True

    def find_live_job_for_recording(self, recording_id: str) -> Optional[Job]:
        """Return the (RUNNING or PENDING) JOB_LIVE job for a recording, if any."""
        for job in self._jobs.values():
            if (
                job.job_type == JOB_LIVE
                and job.live_recording_id == recording_id
                and job.status in (PENDING, RUNNING)
            ):
                return job
        return None

    def stop_live(self, job_id: str) -> None:
        """Signal a JOB_LIVE job's loop to end (normal termination, not a
        cancellation -- the job still completes as COMPLETED)."""
        job = self._jobs.get(job_id)
        if job is not None:
            job.live_stop_event.set()
    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list_all(self) -> list[Job]:
        # Newest first; ties (same created_at) break by insertion order.
        indexed = enumerate(self._jobs.values())
        return [
            job
            for _, job in sorted(indexed, key=lambda pair: (pair[1].created_at, pair[0]), reverse=True)
        ]

    def list_recent(self, limit: int = 20) -> list[Job]:
        return self.list_all()[:limit]

    def active_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status in (PENDING, RUNNING))

    def stop_all_live(self) -> None:
        """Signal every active JOB_LIVE job to end.

        Call before ``job_queue.stop()``: cancelling the task awaiting
        ``asyncio.to_thread`` doesn't stop a thread that is already running, so
        an unsignalled live loop would block interpreter exit and keep its
        Whisper model loaded.
        """
        for job in self._jobs.values():
            if job.job_type == JOB_LIVE and job.status in (PENDING, RUNNING):
                job.live_stop_event.set()

    def cancel(self, job_id: str) -> bool:
        """Request cancellation of a pending or running job."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.status == PENDING:
            job.status = FAILED
            job.error = "Cancelled"
            job.finished_at = datetime.now()
            _delete_temp_upload(job)
            _record_history(job)
            self._prune_finished_jobs()
            return True
        if job.status == RUNNING:
            job._cancel_event.set()
            return True
        return False

    def _prune_finished_jobs(self) -> None:
        """Keep at most _MAX_RETAINED_JOBS COMPLETED/FAILED jobs, dropping the oldest.

        PENDING/RUNNING jobs are never pruned.
        """
        terminal = [j for j in self._jobs.values() if j.status in (COMPLETED, FAILED)]
        if len(terminal) <= _MAX_RETAINED_JOBS:
            return
        terminal.sort(key=lambda j: j.created_at)
        excess = len(terminal) - _MAX_RETAINED_JOBS
        for job in terminal[:excess]:
            self._jobs.pop(job.id, None)
            self._on_complete_callbacks.pop(job.id, None)
            self._on_error_callbacks.pop(job.id, None)

    def _run_on_error_callback(self, job: "Job") -> None:
        """Invoke and discard the job's ``on_error`` callback, if any.

        Also discards ``on_complete``, which can no longer fire. A callback
        failure never masks the job's own error.
        """
        self._on_complete_callbacks.pop(job.id, None)
        cb = self._on_error_callbacks.pop(job.id, None)
        if cb is not None:
            try:
                cb(job)
            except Exception:
                pass

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
                # Cancelled while queued; don't revive it.
                self._queue.task_done()
                continue
            if job.job_type == JOB_LIVE and job.live_stop_event.is_set():
                # The session ended while this job waited behind another. The
                # live loop would exit immediately and report an empty
                # COMPLETED run, so fail it explicitly instead.
                job.status = FAILED
                job.error = "Live transcript never started -- the job queue was busy for the whole session"
                job.finished_at = datetime.now()
                _record_history(job)
                self._prune_finished_jobs()
                self._queue.task_done()
                continue
            job.status = RUNNING
            job.started_at = datetime.now()
            await asyncio.to_thread(_record_history, job)
            try:
                await asyncio.to_thread(self._run_job, job)
            except asyncio.CancelledError:
                # Server shutdown (stop()). The job thread can't be stopped and
                # dies with the process, so the job is interrupted, not done.
                from wisper_transcribe.job_history import INTERRUPTED
                job.status = FAILED
                job.error = INTERRUPTED
                job.finished_at = datetime.now()
                _delete_temp_upload(job)
                raise
            except Exception as exc:
                job.status = FAILED
                # Keep the runner's generic message; never use exception text.
                if not job.error:
                    _set_job_error(job, exc)
                job.finished_at = datetime.now()
            finally:
                # Runs exactly once per processed job, however it ended.
                if job.status in (PENDING, RUNNING):   # a runner that forgot its terminal state
                    job.status = COMPLETED if not job.error else FAILED
                if job.finished_at is None:
                    job.finished_at = datetime.now()
                await asyncio.to_thread(_record_history, job)
                self._prune_finished_jobs()
                self._queue.task_done()

    def _run_job(self, job: Job) -> None:
        """Dispatch to the appropriate worker based on job_type."""
        if job.job_type == JOB_CAMPAIGN_JOURNAL:
            self._run_journal_job(job)
        elif job.job_type == JOB_SPEAKER_RELABEL:
            self._run_relabel_job(job)
        elif job.job_type in (JOB_REFINE, JOB_SUMMARIZE):
            self._run_llm_job(job)
        elif job.job_type == JOB_ENROLL:
            self._run_enroll_job(job)
        elif job.job_type == JOB_LIVE:
            self._run_live_job(job)
        else:
            self._run_transcription_job(job)

    def _run_relabel_job(self, job: Job) -> None:
        """Re-match auto-named speakers across a campaign's transcripts."""
        from wisper_transcribe.config import get_device
        from wisper_transcribe.speaker_registry import relabel_campaign

        report = relabel_campaign(job.kwargs["slug"], device=get_device(),
                                  backfill=True, progress=job.append_log)
        changed = sum(len(t.renamed) for t in report.transcripts)
        job.append_log(f"Renamed {changed} speaker label(s) across {len(report.transcripts)} session(s)")
        if report.recurring:
            job.append_log(f"Unknown voices heard in more than one session: {report.recurring}")
        for t in report.transcripts:
            if t.skipped:
                job.append_log(f"  Skipped {t.stem}: {t.skipped}")
        job.status = COMPLETED
        job.finished_at = datetime.now()

    def _run_journal_job(self, job: Job) -> None:
        """Fold session summaries into a campaign's rolling journal.

        Runs in a thread; sys.stderr is redirected to capture the LLM client's
        streaming status messages into job.log_lines (same pattern as the
        refine/summarize LLM jobs — safe because the queue is single-worker).
        """
        from wisper_transcribe.config import load_config
        from wisper_transcribe.journal import (
            rebuild_campaign, refold_campaign, unjournalled_sessions, update_journal,
        )
        from wisper_transcribe.llm import get_client
        from wisper_transcribe.speaker_manager import load_profiles

        slug = job.kwargs["slug"]
        session_stem = job.kwargs.get("session_stem")
        fold_all = bool(job.kwargs.get("fold_all", False))
        rebuild = bool(job.kwargs.get("rebuild", False))
        resummarize = bool(job.kwargs.get("resummarize", False))

        old_stderr = _sys.stderr
        _sys.stderr = _StderrCapture(job)
        try:
            cfg = load_config()
            client = get_client(cfg.get("llm_provider", "ollama"), config=cfg)
            job.append_log(f"LLM: {client.provider} / {client.model}")
            profiles = load_profiles()

            if rebuild:
                rebuild_fn = rebuild_campaign if resummarize else refold_campaign
                result = rebuild_fn(slug, client, profiles, on_progress=job.append_log)
                job.append_log(f"Summarized: {len(result.resummarized)}")
                if result.skipped:
                    job.append_log(f"Skipped: {len(result.skipped)}")
                    for stem, reason in result.skipped:
                        job.append_log(f"  {stem}: {reason}")
                if result.journal is not None:
                    job.output_path = str(result.journal.path)
                    job.append_log(
                        f"Journal written: {result.journal.path.name} "
                        f"({len(result.journal.journaled_sessions)} session(s) folded)"
                    )
                job.status = COMPLETED
                return

            if session_stem:
                targets = [session_stem]
            else:
                targets = unjournalled_sessions(slug)
                if not targets:
                    job.append_log("Journal already up to date — nothing to fold.")
                elif not fold_all:
                    targets = targets[:1]

            result = None
            for stem in targets:
                job.append_log(f"Folding in: {stem} ...")
                result = update_journal(slug, client, profiles, session_stem=stem)

            if result is not None:
                job.output_path = str(result.path)
                job.append_log(
                    f"Journal written: {result.path.name} "
                    f"({len(result.journaled_sessions)} session(s) folded)"
                )
            job.status = COMPLETED
        except Exception as exc:
            job.status = FAILED
            job.error = str(exc)
            raise
        finally:
            _sys.stderr = old_stderr
            job.finished_at = datetime.now()

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
            if job.is_web_upload and job.upload_dir:
                # Only the WAV is read from here on, so the upload (a video can
                # be gigabytes) goes as soon as it has been extracted.
                from wisper_transcribe.audio_utils import convert_to_wav
                upload = Path(job.input_path)
                wav = convert_to_wav(
                    upload, out_path=Path(job.upload_dir) / "extracted" / "audio.wav")
                if Path(wav) != upload:
                    try:
                        upload.unlink()
                        job.append_log("Extracted audio; deleted the uploaded file")
                    except OSError:
                        job.append_log("Extracted audio; could not delete the uploaded file")
                job.input_path = str(wav)

            _result_store: dict = {}
            output_path = process_file(Path(job.input_path), _result_store=_result_store,
                                       job_id=job.id, skip_existing=False, **job.kwargs)
            if not Path(output_path).is_file():
                from wisper_transcribe.config import get_output_root
                job.append_log(f"Transcripts folder: {job.kwargs.get('output_dir') or get_output_root()}")
                raise TranscriptMissingError(Path(output_path).name)
            job.diarization_segments = _result_store.get("diarization_segments", [])
            job.speaker_map = _result_store.get("speaker_map", {})
            job.speaker_embeddings = _result_store.get("speaker_embeddings", {})
            job.output_path = str(output_path)

            # Excerpts are cut from the extracted WAV (or a recording's combined.wav).
            _extract_speaker_excerpts(job, output_path,
                                      aligned_segments=_result_store.get("aligned_segments", []),
                                      diarization_segments=job.diarization_segments)

            if job.upload_dir:
                _keep_audio(job, Path(output_path))
            sidecar_written = _write_enrollment_sidecar(job, output_path)
            if job.upload_dir:
                from wisper_transcribe import transcript_store
                if job.diarization_segments and not sidecar_written and job.input_path:
                    # The sidecar write failed after the encode: don't leave the
                    # new FLAC unregistered.
                    try:
                        transcript_store.set_audio(Path(output_path), Path(job.input_path))
                    except Exception:
                        log.warning("Could not record the audio for %s", Path(output_path).name,
                                    exc_info=True)
                elif not job.diarization_segments:
                    try:
                        transcript_store.set_audio(
                            Path(output_path), Path(job.input_path) if job.input_path else None)
                    except Exception:
                        log.warning("Could not record the audio for %s", Path(output_path).name,
                                    exc_info=True)
                _delete_temp_upload(job)
                job.is_web_upload = False

            # Chain LLM post-processing if requested; defer COMPLETED until done
            if job.post_refine or job.post_summarize:
                self._run_post_process(job, Path(output_path))

            # Before COMPLETED, so anyone waiting on the status sees the
            # callback's effects (the recording hand-off links its transcript).
            _cb = self._on_complete_callbacks.pop(job.id, None)
            if _cb is not None:
                try:
                    _cb(job)
                except Exception:
                    pass  # callback failure must not fail the job
            job.status = COMPLETED

        except InterruptedError:
            job.status = FAILED
            job.error = "Cancelled"
            _delete_temp_upload(job)
            self._run_on_error_callback(job)
        except Exception as exc:
            job.status = FAILED
            _set_job_error(job, exc)
            _delete_temp_upload(job)
            self._run_on_error_callback(job)
            raise
        finally:
            _tqdm_module.tqdm.write = original_write  # type: ignore[method-assign]
            _tqdm_module.tqdm.__init__ = original_init  # type: ignore[method-assign]
            _tqdm_module.tqdm.monitor_interval = original_monitor_interval
            job.finished_at = datetime.now()

    def _run_post_process(self, job: Job, transcript_path: "Path") -> None:  # type: ignore[name-defined]
        """Chain refine and/or summarize after a completed transcription job.

        Called from within _run_transcription_job, still in the job thread.
        sys.stderr is redirected to capture the LLM client's status messages.
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
            # log_lines render in the UI too: keep this line generic.
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
            _set_job_error(job, exc)
            raise
        finally:
            _sys.stderr = old_stderr
            job.finished_at = datetime.now()

    def _run_enroll_job(self, job: Job) -> None:
        """Dispatch a JOB_ENROLL job to its mode-specific runner.

        Runners set a generic ``job.error`` and never re-raise: exception text
        can contain paths, and the job page renders it into HTML.
        """
        if job.enroll_mode == "standalone":
            self._run_standalone_enroll(job)
            return
        if job.enroll_mode == "recording":
            self._run_recording_enroll(job)
            return
        self._run_wizard_enroll(job)

    def _run_standalone_enroll(self, job: Job) -> None:
        """Run a standalone /speakers/enroll job.

        Deletes the ``wisper_enrollsrc_*`` upload and converted WAV in ``finally``.
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
        """Enroll an unbound Discord speaker from a recording's per-user track.

        On success, removes the uid from ``unbound_speakers``, binds it in
        ``discord_speakers``, and adds/binds the profile in the recording's
        campaign. Those follow-ups are best-effort and never fail the job.
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
            from wisper_transcribe.recording_manager import bind_recording_speaker, load_recording

            rec = load_recording(p["recording_id"], data_dir)
            if rec is not None:
                bind_recording_speaker(rec.id, p["discord_uid"], p["profile_key"], data_dir)

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

        try:
            from wisper_transcribe.recording_manager import trim_recording_audio

            trim_recording_audio(p["recording_id"], data_dir)
        except Exception:
            log.warning("Could not trim recording %s after enrollment", p["recording_id"],
                        exc_info=True)

        job.append_log("Speaker enrolled.")
        job.status = COMPLETED
        job.finished_at = datetime.now()

    def _run_wizard_enroll(self, job: Job) -> None:
        """Run embedding extraction for a wizard submission.

        Same status transitions as ``_run_llm_job`` without the re-raise; see
        ``_run_enroll_job``.
        """
        from pathlib import Path

        from wisper_transcribe.web.enroll_shared import (
            _load_diar_sidecar,
            audio_available,
            enrollable_labels,
            stored_embeddings,
        )

        md_path = Path(job.enroll_md_path or job.output_path or "")
        diar = _load_diar_sidecar(md_path)
        all_labels = [lb for labels in job.enroll_groups.values() for lb in labels]
        if not diar or not enrollable_labels(diar, all_labels)[0]:
            job.status = FAILED
            job.error = "Source audio not available"
            job.finished_at = datetime.now()
            return

        input_path = Path(diar["input_path"]) if audio_available(diar) else None

        from wisper_transcribe.models import DiarizationSegment

        segments = [
            DiarizationSegment(start=s["start"], end=s["end"], speaker=s["speaker"])
            for s in diar.get("diarization_segments", [])
        ]
        # The transcript's current campaign, not the one it was transcribed for.
        from wisper_transcribe.campaign_manager import get_campaign_for_transcript
        from wisper_transcribe.transcript_store import locate_path
        _loc = locate_path(md_path)
        campaign_slug = get_campaign_for_transcript(_loc.id) if _loc is not None else None

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
                stored=stored_embeddings(diar),
                md_path=md_path,
            )
            if campaign_slug:
                # Propagate the new names to the campaign's other sessions,
                # from stored embeddings only so the job stays fast.
                try:
                    from wisper_transcribe.speaker_registry import relabel_campaign

                    _progress("Updating other sessions in the campaign…")
                    relabel_campaign(campaign_slug, device=job.enroll_device,
                                     backfill=False, progress=_progress)
                except Exception as exc:
                    log.warning("campaign relabel after enroll failed: %s", exc)
            job.status = COMPLETED
        except Exception:
            job.status = FAILED
            job.error = "Enrollment failed"
        finally:
            job.finished_at = datetime.now()

    def _run_live_job(self, job: Job) -> None:
        """Run a live-transcription session until ``live_stop_event`` is set.

        Stopping is normal completion (COMPLETED), not cancellation. Each line
        goes to ``job.live_lines`` (for SSE) and ``live_transcript.md``; the
        post-session Transcribe pass remains the authoritative transcript.

        Per-chunk failures are handled inside ``run_live_loop()``. A setup
        failure fails the job but is not re-raised, so the page shows "session
        ended" rather than a traceback.
        """
        from pathlib import Path

        from wisper_transcribe.web.live_transcribe import NOISE_FLOOR_RMS, run_live_loop

        def _on_line(line) -> None:
            line_dict = line.to_dict()
            job.append_live_line(line_dict)
            if job.live_output_path:
                try:
                    speaker = line_dict["speaker"]
                    ts = line_dict["start_s"]
                    with open(job.live_output_path, "a", encoding="utf-8") as f:
                        f.write(f"**{speaker}** *({ts:.1f}s)*: {line_dict['text']}\n\n")
                except OSError:
                    log.warning("Failed to append live transcript line to %s", job.live_output_path)

        try:
            if job.live_output_path:
                Path(job.live_output_path).parent.mkdir(parents=True, exist_ok=True)
                if not Path(job.live_output_path).exists():
                    Path(job.live_output_path).write_text(
                        "# Live transcript\n\n", encoding="utf-8"
                    )

            run_live_loop(
                job.live_ring_buffer,
                job.live_stop_event,
                _on_line,
                model_size=job.kwargs.get("model_size", "base"),
                device=job.kwargs.get("device", "auto"),
                compute_type=job.kwargs.get("compute_type", "auto"),
                language=job.kwargs.get("language", "en"),
                mic_label=job.kwargs.get("mic_label", "You"),
                get_noise_floor=lambda: job.kwargs.get("noise_floor", NOISE_FLOOR_RMS),
                on_warning=job.append_log,
            )
            job.status = COMPLETED
        except Exception:
            log.error("Live transcription job %s failed", job.id, exc_info=True)
            job.status = COMPLETED  # ended, not "failed" -- see docstring
        finally:
            job.finished_at = datetime.now()
            if job.live_output_path and Path(job.live_output_path).is_file():
                try:
                    file_registry.add_if_owned(
                        Path(job.live_output_path), kind="live_draft",
                        owner=file_registry.Owner.for_recording(job.live_recording_id))
                except sqlite3.Error:
                    log.warning("Could not register the live draft", exc_info=True)

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
                atomic_write_text(backup, md)
                file_registry.add_if_owned(
                    backup, kind="backup", owner=file_registry.Owner.for_path(transcript_path))
                save_transcript(transcript_path, refined_md)
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
                save_summary(out_path, body)
                job.append_log(f"Summary written: {out_path.name}")
                job.summary_path = str(out_path)
            except (LLMUnavailableError, LLMResponseError) as exc:
                job.append_log(f"Summarize failed: {exc}")
            job.output_path = str(transcript_path)
