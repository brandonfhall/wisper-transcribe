"""BotManager — manages the JDA recording sidecar subprocess.

Architecture (modular stop-gap — see plan.md "Sidecar modularity"):
  - Python opens a Unix socket server; JDA sidecar connects as a client
    and writes length-prefixed PCM frames.
  - Wire format: [u32 user_id_len][user_id bytes][u32 pcm_len][pcm bytes]
  - Control frame: user_id == "__ctrl__", pcm == struct.pack("<I", close_code)
  - BotManager is injectable: pass audio_source_factory= in tests to avoid
    real JDA subprocess and socket overhead.

When Pycord ships working DAVE receive (PR #3159), swap the sidecar:
  1. Delete discord-bot/ (the Gradle/Java project)
  2. Write a ~100-line Python replacement that emits the same wire format
  3. Point sidecar_command config key at the Python script
  Nothing else changes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

from wisper_transcribe.campaign_manager import lookup_profile_by_discord_id
from wisper_transcribe.models import Recording, RejoinAttempt
from wisper_transcribe.recording_manager import (
    create_recording,
    load_recordings,
    save_recording,
)
from wisper_transcribe.web.audio_writer import RealtimePCMMixer, SegmentedOggWriter

log = logging.getLogger(__name__)

CTRL_USER_ID = "__ctrl__"

# Close codes: close codes that warrant a retry vs hard abort
_TRANSIENT = frozenset({4009, 4015})
_PERMANENT = frozenset({4014, 4011, 4022})


# ---------------------------------------------------------------------------
# Production audio source (placeholder — real JDA launch in Phase 9)
# ---------------------------------------------------------------------------

async def _unix_socket_source(
    recording_id: str,
    voice_channel_id: str,
    guild_id: str,
    token: str,
) -> AsyncIterator[tuple[str, bytes]]:
    """Production: launch JDA sidecar, open Unix socket, yield PCM frames.

    Phase 3 placeholder — JDA subprocess launch and socket server wired up
    in Phase 9 hardening when the discord-bot/ Gradle JAR is integrated.
    Until then, yields nothing; start_session creates the Recording and
    stop_session finalises it with zero audio segments.
    """
    return
    yield  # pragma: no cover — makes this an async generator


# ---------------------------------------------------------------------------
# BotManager
# ---------------------------------------------------------------------------

class BotManager:
    """Manages per-session JDA sidecar subprocess and audio routing.

    Mirrors JobQueue's start()/stop() interface for FastAPI lifespan integration.
    One session at a time. Thread-safe via asyncio (all public methods are async
    or called from the same event loop).
    """

    DEFAULT_BACKOFF = [2, 5, 15, 30, 60]

    def __init__(
        self,
        data_dir: Path,
        audio_source_factory: Optional[Callable] = None,
        _backoff: Optional[list] = None,
    ):
        """
        audio_source_factory(recording_id, voice_channel_id, guild_id, token)
            → AsyncIterator[tuple[str, bytes]]

        Each item: (user_id, pcm_bytes) or (CTRL_USER_ID, close_code_bytes).
        Factory is called once per connection attempt (including rejoin retries).
        Defaults to _unix_socket_source.

        _backoff: override DEFAULT_BACKOFF for tests (e.g. [0]*5 to skip delays).
        """
        self._data_dir = Path(data_dir)
        self._source_factory = audio_source_factory or _unix_socket_source
        self._backoff = _backoff if _backoff is not None else self.DEFAULT_BACKOFF

        self._active_recording: Optional[Recording] = None
        self._writers: dict[str, SegmentedOggWriter] = {}
        self._mixer: Optional[RealtimePCMMixer] = None
        self._mixed_writer: Optional[SegmentedOggWriter] = None
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle (mirrors JobQueue)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Called from FastAPI lifespan — initialises state."""
        log.info("BotManager started")

    async def stop(self) -> None:
        """Called from FastAPI lifespan — stops any active session."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        log.info("BotManager stopped")

    # ------------------------------------------------------------------
    # Session control
    # ------------------------------------------------------------------

    @property
    def active_recording(self) -> Optional[Recording]:
        return self._active_recording

    async def start_session(
        self,
        campaign_slug: Optional[str],
        voice_channel_id: str,
        guild_id: str,
    ) -> Recording:
        """Create a Recording and start the audio capture task."""
        if self._active_recording and self._active_recording.status in {
            "recording", "degraded"
        }:
            raise RuntimeError(
                f"Session {self._active_recording.id} is already active"
            )

        recording = create_recording(
            voice_channel_id=voice_channel_id,
            guild_id=guild_id,
            campaign_slug=campaign_slug,
            data_dir=self._data_dir,
        )
        self._active_recording = recording
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._session_loop(recording),
            name=f"bot-session-{recording.id[:8]}",
        )
        log.info("Recording session started: %s", recording.id)
        return recording

    async def stop_session(self) -> None:
        """Signal the session to stop and wait for it to finalise."""
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=30)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        log.info("Recording session stopped: %s",
                 self._active_recording.id if self._active_recording else "none")

    # ------------------------------------------------------------------
    # Internal session loop
    # ------------------------------------------------------------------

    async def _session_loop(self, recording: Recording) -> None:
        """Read audio frames, route to writers, handle reconnects."""
        token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if not token:
            from wisper_transcribe.config import load_config
            token = load_config().get("discord_bot_token", "")
        if not token:
            token = recording.voice_channel_id  # fallback for test introspection
        attempt = 0

        try:
            while not self._stop_event.is_set():
                self._ensure_writers(recording)
                source = self._source_factory(
                    recording.id, recording.voice_channel_id, recording.guild_id, token
                )
                reconnecting = False
                async for user_id, pcm in source:
                    if self._stop_event.is_set():
                        break

                    if user_id == CTRL_USER_ID:
                        close_code = struct.unpack("<I", pcm[:4])[0]
                        should_retry = await self._handle_disconnect(
                            recording, close_code, attempt
                        )
                        if should_retry:
                            attempt += 1
                            reconnecting = True
                            break
                        else:
                            return  # fatal; status already set
                    else:
                        self._route_frame(user_id, pcm, recording)
                else:
                    # Source exhausted cleanly — done
                    break

                if not reconnecting:
                    break

        except asyncio.CancelledError:
            pass  # clean stop via stop_session()
        finally:
            await self._finalise(recording)

    def _ensure_writers(self, recording: Recording) -> None:
        """Initialise combined writer and mixer (idempotent)."""
        if self._mixed_writer is None:
            combined_dir = (
                self._data_dir / "recordings" / recording.id / "combined"
            )
            self._mixed_writer = SegmentedOggWriter(stream_dir=combined_dir)
            self._mixer = RealtimePCMMixer()

    def _route_frame(
        self, user_id: str, pcm: bytes, recording: Recording
    ) -> None:
        """Route one 20 ms PCM frame to per-user and combined writers."""
        if user_id not in self._writers:
            per_user_dir = (
                self._data_dir
                / "recordings"
                / recording.id
                / "per-user"
                / user_id
            )
            self._writers[user_id] = SegmentedOggWriter(stream_dir=per_user_dir)
            if user_id not in recording.discord_speakers:
                profile_key = ""
                if recording.campaign_slug:
                    resolved = lookup_profile_by_discord_id(
                        recording.campaign_slug, user_id, data_dir=self._data_dir
                    )
                    if resolved:
                        profile_key = resolved
                recording.discord_speakers[user_id] = profile_key
                if not profile_key and user_id not in recording.unbound_speakers:
                    recording.unbound_speakers.append(user_id)
                save_recording(recording, self._data_dir)

        self._writers[user_id].write(pcm)
        self._mixer.add_frame(user_id, pcm)
        mixed = self._mixer.mix()
        self._mixed_writer.write(mixed)

    async def _handle_disconnect(
        self, recording: Recording, close_code: int, attempt: int
    ) -> bool:
        """Decide whether to retry. Returns True = retry, False = abort."""
        if close_code in _PERMANENT:
            log.warning(
                "Permanent disconnect (code %d) on recording %s — aborting",
                close_code, recording.id,
            )
            recording.status = "failed"
            recording.ended_at = datetime.now(timezone.utc)
            save_recording(recording, self._data_dir)
            return False

        if attempt >= len(self._backoff):
            log.warning(
                "Max retries (%d) exhausted on recording %s — marking degraded",
                len(self._backoff), recording.id,
            )
            recording.status = "degraded"
            save_recording(recording, self._data_dir)
            return False

        delay = self._backoff[attempt]
        log.info(
            "Transient disconnect (code %d), retrying in %ds (attempt %d/%d)",
            close_code, delay, attempt + 1, len(self._backoff),
        )
        rejoin = RejoinAttempt(
            timestamp=datetime.now(timezone.utc),
            close_code=close_code,
            attempt_number=attempt + 1,
        )
        recording.rejoin_log.append(rejoin)
        save_recording(recording, self._data_dir)

        if delay > 0:
            await asyncio.sleep(delay)
        return True

    async def _finalise(self, recording: Recording) -> None:
        """Close all writers and mark recording completed."""
        for writer in self._writers.values():
            try:
                writer.finalize()
            except Exception as exc:
                log.warning("Failed to finalise writer: %s", exc)
        self._writers.clear()

        if self._mixed_writer:
            try:
                self._mixed_writer.finalize()
            except Exception as exc:
                log.warning("Failed to finalise combined writer: %s", exc)
            self._mixed_writer = None
        self._mixer = None

        if recording.status == "recording":
            recording.status = "completed"
            recording.ended_at = datetime.now(timezone.utc)
            save_recording(recording, self._data_dir)
            log.info("Recording %s finalised as completed", recording.id)
