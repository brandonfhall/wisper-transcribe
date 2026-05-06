# Wisper-Transcribe: Discord Recording Bot — Completion Summary

## Status: Complete (2026-05-05)

All planned Discord Recording Bot v1 work is finished. 663 tests pass. The Java sidecar compiles and the Docker multi-stage build includes the JDA+JDAVE fat JAR.

---

## Completed Phases

| Phase | Description | Status |
|-------|-------------|--------|
| Phase 0 | Library spike — JDA+JDAVE confirmed working DAVE receive | Done |
| Phase 1 | Storage layer — `recording_manager.py`, `SegmentedOggWriter`, `RealtimePCMMixer`, data model | Done |
| Phase 2 | Server discovery + control plane — `server.json`, CLI `wisper record` group | Done |
| Phase 3 | Bot core — `BotManager`, `_unix_socket_source`, session loop, auto-rejoin, injectable audio source | Done |
| Phase 4 | Campaign / Discord ID binding — `bind_discord_id()`, `lookup_profile_by_discord_id()` | Done |
| Phase 5 | Web UI — Record control page, recordings list, recording detail, delete, SSE status stream | Done |
| Phase 6 | Auto-enroll on first hear (Option B) — `unbound_speakers`, enrollment from recordings | Done |
| Phase 7 | Hand-off into JobQueue — copy + submit, `on_complete` callback, `"transcribing"`/`"transcribed"` statuses | Done |
| Phase 8 | Tests + docs — expanded test coverage, README, architecture.md, Dockerfile, docker-compose, launchers | Done |
| Phase 9 | Hardening — token masking, crash recovery validation, config resolution, path traversal tests | Done |
| Phase 10 | JDA sidecar source — `discord-bot/` Java project (Main.java, SocketWriter.java, build.gradle, settings.gradle) | Done |

---

## Future: Replace JDA with Pycord When DAVE Ships

The Java sidecar (`discord-bot/`) is a **modular stop-gap**. The Unix-socket wire protocol is the stable interface. When [Pycord PR #3159](https://github.com/Pycord-Development/pycord/pull/3159) merges with working DAVE receive:

1. Delete `discord-bot/` (the Gradle/Java project)
2. Write a ~100-line Python replacement that emits the same length-prefixed PCM frames over the Unix socket
3. Update `BotManager` to launch the Python script instead of the JAR
4. Remove the Java stages from `Dockerfile`
5. Remove the Java 25 requirement from launchers and README

Nothing else changes — `SegmentedOggWriter`, the web UI, campaigns, CLI, and all tests remain unchanged.

---

## Key Architecture Decisions (reference)

| Decision | Choice |
|----------|--------|
| Bot lifecycle | Per-session — invoke at start, drop at stop. Not always-on |
| Bot hosting | JDA sidecar subprocess launched by `BotManager` |
| Discord library | JDA 6.3.0 + JDAVE 0.1.8 (Java 25 required) |
| Java build | Gradle wrapper + shadowJar (`com.gradleup.shadow:9.0.0`) |
| Bot↔Python IPC | Length-prefixed binary frames over Unix socket |
| Recording storage | `data_dir/recordings/<recording_id>/…` |
| Audio format | Segmented Opus-in-Ogg, 60 s per segment |
| Mixing | Real-time PCM mixing during recording |
| Transcription hand-off | Copy combined.wav → `JobQueue.submit()` |
| Speaker enrollment | Discord-ID binding (Option A) + auto-enroll on first hear (Option B) |
| Reconnect | Auto-rejoin with backoff [2, 5, 15, 30, 60] s |
| CLI ↔ server | HTTP on localhost, `server.json` for discovery |

### Data Model

```python
@dataclass
class Recording:
    id: str
    campaign_slug: Optional[str]
    started_at: datetime
    ended_at: Optional[datetime]
    status: Literal["recording", "degraded", "completed", "failed", "transcribing", "transcribed"]
    voice_channel_id: str
    guild_id: str
    discord_speakers: dict[str, str]    # discord_user_id → profile key (or "")
    segment_manifest: list[SegmentRecord]
    unbound_speakers: list[str]          # Discord IDs not yet bound to a profile
    combined_path: Path
    per_user_dir: Path
    transcript_path: Optional[Path]
    job_id: Optional[str]
    rejoin_log: list[RejoinAttempt]
    notes: Optional[str]
```

### Storage Layout

```
data_dir/recordings/
├── recordings.json
└── <recording_id>/
    ├── metadata.json
    ├── combined/           # segmented mixed track
    ├── per-user/<discord_id>/  # per-user segmented tracks
    └── final/
        ├── combined.wav    # produced at stop, fed into JobQueue
        └── transcript.md
```

### Five v1 File-Format Invariants (for v2 live transcription)

1. Each segment is a self-contained Ogg/Opus container with EOS page
2. Segment manifest is append-only and atomic
3. Segment length ≤ 60 s
4. Per-user directory layout is fixed: `recordings/<id>/per-user/<discord_id>/NNNN.opus`
5. `Recording.status` has distinct `"recording"` and `"degraded"` states

---

## Deferred to v2

- Live streaming transcription (ticker)
- Voice-print fallback enrollment (Option C)
- Video / screenshare recording
- Discord-side slash commands
- Multi-guild / multi-channel operation
- Web UI auth (project-wide)

