# Wisper-Transcribe: Backlog & Active Work

## Project Context

Podcast transcription tool for tabletop RPG actual-play recordings (D&D, Pathfinder, etc.) with 5–8 speakers (GM + players). Transcripts are fed into NotebookLM for querying game events and tracking stats.

**Hardware:** NVIDIA RTX 3090 (Windows), Apple M5 Mac. Both platforms supported.
**Processing:** Fully local — no cloud APIs. CLI + web UI.
**Stack:** faster-whisper + pyannote-audio. See [architecture.md](architecture.md) for full technical reference and [README.md](README.md) for user docs.

---

## Active — Discord Recording Bot (v1: batch capture)

> **Status:** planning / pre-research. Architecture decisions in this section have been agreed with the user; items marked **(research)** below are open and will be filled in by the implementing engineer / architect after exploring the libraries and code base.

### Problem

Sessions are currently recorded externally with OBS — system audio (Discord players) on one track, host mic on another, mixed down to a single file the user feeds into wisper. The host wants to fold the recording step into wisper itself: a Discord bot that joins the voice channel, records the session to disk, and surfaces the resulting file in wisper's existing transcribe / campaign / enrollment flow with no manual file shuffling.

A later phase will make transcription stream live (~30–60 s lag) while recording is in progress. **v1 is batch-only** — research (resolved item 13 below) found the live-ticker gate fails on two hard criteria (`JobQueue` is one-job-at-a-time and would be monopolized for 4-hour sessions; realistic build estimate is 2–3 days, not <1). Live transcription stays in v2, but v1 must honour five file-format invariants so v2 can be added without a rewrite — see "v1 file-format invariants for future live transcription" below.

### V1 scope (proof of concept)

- Discord bot, joined per-session to one voice channel, controlled from wisper (no Discord-side commands).
- **Per-user audio capture** (each Discord speaker → its own track) plus a **mixed combined track** for archival / fallback transcription.
- **Crash-survivable on-disk format**: segmented files written continuously so that a crash mid-session loses at most one segment.
- New `Recording` data model + `recordings/` data dir; per-recording metadata indexes who spoke, when, and which Discord ID maps to which wisper profile.
- **Campaign integration**: a recording is created against a campaign; the bot only enrolls / matches against members of that campaign's roster.
- **Hand-off into existing pipeline**: when recording stops, the mixed track becomes the input to `process_file()` via the existing `JobQueue`. No parallel transcription path.
- **Auto-rejoin** on Discord disconnect (best-effort, with backoff).

### Out of scope for v1

- Live / streaming transcription with a running ticker — **deferred to v2** (gate failed; see resolved item 13). v1 must preserve the file-format invariants that let v2 add it without rewriting the recording layer.
- Bot replying to Discord (live captions back, slash commands, etc.).
- Video / screenshare capture.
- Multi-server, multi-channel, or multi-bot operation.
- Cloud storage / non-local data dir.
- Voice-print fallback enrollment (Option C below — deferred).

### Architecture decisions (locked)

| Decision | Choice |
|---|---|
| Bot lifecycle | Per-session — invoke at start, drop at stop. Not always-on. |
| Bot hosting | **Inline** — asyncio task inside the `wisper server` process. No separate Docker service. |
| Recording storage | `data_dir/recordings/<recording_id>/…`, sibling of `profiles/` and `campaigns/` |
| Audio shape | Per-user tracks **and** a mixed combined track, both retained on disk |
| Discord library | **Pycord (py-cord)** — only library with documented stable per-user voice receive; MIT-licensed; active maintainer team. See resolved item 1 + Phase 0 DAVE risk. |
| Audio file format | **Segmented Opus-in-Ogg** (`.ogg` per segment), one complete self-contained Ogg bitstream per segment file. No mid-stream truncation hazard; ffmpeg / pydub handle natively. |
| Segment length | **60 seconds** per segment file. 240 segments per 4-hour session × 7 streams (6 users + mixed); `ffmpeg -f concat -c copy` finalizes in 2–4 s. |
| Per-user silence handling | **Continuous-with-DTX**: every user's track is wall-clock-aligned; `libopus` DTX (`OPUS_SET_DTX(1)`) drops silent periods to ~400 bit/s comfort-noise. No sidecar timestamp manifest needed. |
| Mixing strategy | **Real-time PCM mixing** during recording (~2 % of one CPU core for 6 users). Deferred `ffmpeg amix` was 2–48 minutes for a 4-hour session — too slow for "stop and transcribe". A `--remix` repair flag may be added later from per-user tracks without format changes. |
| Transcription hand-off | Existing `JobQueue.submit()` consumes a **copy** of the combined track at `output/<recording_id>.wav` (not the original `recordings/<id>/final/combined.wav`). The queue's internal `shutil.move()` rename ([jobs.py:241–244](src/wisper_transcribe/web/jobs.py#L241-L244)) would otherwise relocate the recording's source file. Pass `original_stem=recording_id`, `output_dir=_default_output_dir()`, `campaign=recording.campaign_slug`. |
| Speaker enrollment | Discord-ID binding on roster (Option A) **+** auto-enroll on first hear (Option B). Voice-print fallback (Option C) deferred to v2. |
| Reconnect | Auto-rejoin on Discord disconnect with backoff. Mark session degraded but keep recording. |
| Discord token | Same handling as `HF_TOKEN`: env var (`DISCORD_BOT_TOKEN`) wins, then config, then web-UI input. Masked in `wisper config show`. |
| CLI parity | Surface every server-side action as a `wisper record` subcommand where it makes sense; UI is the primary entry point. |
| CLI ↔ server IPC | **HTTP on localhost.** CLI is a thin client of the same FastAPI routes the web UI uses (`POST /api/record/start`, etc.). Server writes its bind address to `data_dir/server.json` on startup; CLI reads that for discovery, with `WISPER_SERVER_URL` env var as override. If `server.json` is absent or the server is unreachable, the CLI errors out with a clear "wisper server is not running — start it with `wisper server` and try again" message. No auto-launch, no standalone-CLI bot path. |
| Auth on record routes | **None for v1** — match the existing posture (`0.0.0.0:8080`, no auth on any route). Adding auth project-wide (token or basic username/password covering *all* routes, not just record) is tracked in Backlog → `Web UI auth (project-wide)`. Recording is destructive enough that an audit-then-ship pass is appropriate, but doing it piecemeal on record-only routes would create a confusing two-tier security model. |
| FastAPI integration | **`BotManager` mirroring `JobQueue`.** New `web/discord_bot.py` exposes a `BotManager` class with `start()` / `stop()` that internally uses `asyncio.create_task()`. Created in `create_app()`, started in `lifespan()` after `job_queue.start()`, stopped before `job_queue.stop()` on shutdown. Stored on `app.state.bot_manager`; routes obtain it via `get_bot_manager(request)` helper alongside the existing `get_queue()`. `data_dir/server.json` is written immediately before the lifespan `yield` and deleted in the post-`yield` cleanup arm. The `wisper server` CLI ([cli.py:169](src/wisper_transcribe/cli.py#L169)) passes host/port via env var (`WISPER_BIND`) so `create_app()` can read it without a signature change. |
| Auto-rejoin policy | **5 retries on transient failures, fatal abort on permanent ones.** Backoff schedule: `[2, 5, 15, 30, 60]` seconds (capped linear; exponential blew to 16 min by retry 4 — too long for a session). **Transient** = close codes 4009 (session timeout), 4015 (voice server crash), `TimeoutError` / socket reset. **Permanent** = 4014 (kicked), 4011 (server gone), 4022 (call ended), 4017 (DAVE required — library bug). On 4006, retry once with fresh connect, then give up. Each rejoin attempt logs to the segment manifest with timestamp + attempt number. After max retries, set `Recording.status = "degraded"` and stop accepting new segments while keeping existing ones. Surface in UI via SSE / status badge; `wisper record show` prints `[DEGRADED] reconnected Nx — last reason: <code>`. Call `await vc.disconnect(force=True)` before each retry to dodge [discord.py #10207](https://github.com/Rapptz/discord.py/issues/10207). |

### Enrollment options

**A — Discord ID binding on the campaign roster.** `CampaignMember` gains an optional `discord_user_id` field. Roster page lets you bind a wisper profile to a Discord user. When that user speaks, their per-user track is tagged with their wisper profile name automatically.

**B — Auto-enroll on first hear.** When the bot hears an unknown Discord ID speaking, it queues a "new speaker" event. After recording, the new-speaker UI prompts for a name and extracts a voice embedding from that user's per-track audio. No interactive prompts during play.

**C (deferred to v2) — voice-print fallback.** Use existing wisper voice embeddings to match an unknown Discord ID to an enrolled profile via cosine similarity. Means a returning player who's already enrolled gets recognized without binding their Discord ID. Pure UX polish — A + B fully covers v1.

### Data model additions (locked after research)

```python
@dataclass
class Recording:
    id: str                       # uuid4 / slug
    campaign_slug: Optional[str]
    started_at: datetime
    ended_at: Optional[datetime]
    status: Literal["recording", "degraded", "completed", "failed", "transcribing", "transcribed"]
    # Note: "recording" and "degraded" are distinct active states (degraded = auto-rejoin
    # exhausted but existing segments preserved). v2 live ticker watches only while status
    # is in {"recording", "degraded"} — invariant 5.
    voice_channel_id: str
    guild_id: str                 # added — needed for Pycord channel resolution
    discord_speakers: dict[str, str]   # discord_user_id → tagged wisper profile name (or "")
    segment_manifest: list[SegmentRecord]  # see below — append-only, atomic per invariant 2
    combined_path: Path           # data_dir/recordings/<id>/final/combined.wav (16 kHz mono, post-stop)
    per_user_dir: Path            # data_dir/recordings/<id>/per-user/<discord_id>/
    transcript_path: Optional[Path]
    rejoin_log: list[RejoinAttempt]   # timestamp + close_code + attempt_number per resolved item 9
    notes: Optional[str]


@dataclass
class SegmentRecord:
    index: int                    # monotonic, per stream
    stream: Literal["mixed"] | str   # "mixed" or a discord_user_id
    started_at: datetime
    duration_s: float
    path: Path                    # …/per-user/<discord_id>/NNNN.opus or …/combined/NNNN.opus
    finalized: bool               # False while writing; True after EOS page flushed


class CampaignMember:                # existing dataclass, new field
    discord_user_id: Optional[str] = None
```

Storage layout:

```
data_dir/recordings/
├── recordings.json                # index (analogous to campaigns.json)
└── <recording_id>/
    ├── metadata.json              # full Recording dataclass + segment manifest
    ├── combined/                  # segmented mixed track
    │   ├── 0000.opus
    │   ├── 0001.opus
    │   └── …
    ├── per-user/<discord_id>/     # one folder per speaking Discord user
    │   ├── 0000.opus
    │   └── …
    └── final/
        ├── combined.wav           # produced at stop, fed into JobQueue
        └── transcript.md          # written by the existing pipeline (symlinked / copied)
```

### CLI surface (sketch)

```
wisper record start --campaign <slug> --voice-channel <discord_channel_id> [--guild <id>]
wisper record stop                                 # stops the active session, queues transcription
wisper record list [--campaign <slug>]             # all recordings, grouped by campaign
wisper record show <recording_id>                  # metadata, speakers, file paths
wisper record transcribe <recording_id>            # re-queue transcription on the existing combined track
wisper record delete <recording_id>                # remove files + entry, with confirmation
```

`wisper config discord` wizard mirrors `wisper config llm`: bot token, default guild, default voice channel.

### Web surface (sketch)

- New top-nav tab: **Record**.
- `/record` — control panel: campaign select, voice channel picker, start/stop, current-session status (who's connected, mic activity dots, segment count, elapsed time, disconnect/reconnect events).
- `/recordings` — list page, grouped by campaign (mirrors the transcripts list pattern).
- `/recordings/<id>` — per-recording detail: metadata, speaker bindings (with "link to wisper profile" controls for each Discord user heard), segment list, **Transcribe** button that drops into the existing job queue, **Download combined** button.
- (Future) `/recordings/<id>/live` — live transcript ticker. Placeholder route in v1, fully implemented later.

### Implementation phases (proposed — refine after research)

**Phase 0 — Library spike (explicitly throwaway).** 1–2 day timebox. Standalone `scripts/spike_voice_receive.py`: hardcoded bot token (env var), hardcoded guild + channel, joins, captures ~60 s, dumps per-user `.opus` files, exits. **Library is locked to Pycord** (resolved item 1) — the spike's job is to verify Pycord's `start_recording(sink, …)` actually receives per-user audio under DAVE/E2EE.

**DAVE/E2EE risk (highest priority).** Discord mandated DAVE on 2026-03-02 (close code 4017 on connect). Pycord 2.8rc1 added DAVE for voice-*sending*; voice-*receive* is documented as "may not work as expected" and tracked as in-progress in [pycord #3135](https://github.com/Pycord-Development/pycord/issues/3135). The spike must verify on the latest stable + master:

- Acceptance gate A — **stable Pycord works**: `pip install py-cord[voice]==<latest stable>`, run script, per-user `.opus` files have audible audio. Proceed to phase 1.
- Acceptance gate B — **only master works**: stable fails but `pip install git+…@master` works. Proceed but pin `git+` URL in `pyproject.toml` and add a tracking comment to swap back to PyPI when the next release ships.
- Acceptance gate C — **nothing works**: no Pycord variant receives DAVE-encrypted audio. **Replan**. Options: (a) fall back to mixed-audio capture (single track via OBS-style virtual cable inside the bot's host) + diarization, drop auto-enroll, reshape phases 4 and 7; (b) wait on Pycord upstream; (c) re-research at item-1 scope including aiortc raw-receive.

Other acceptance criteria: per-user `.opus` files are well-formed Ogg/Opus (playable by `ffplay`); chosen install works on Windows + macOS hosts; deliverable is a one-page library-choice memo appended to this plan (DAVE outcome, alternatives considered, platform caveats). Spike code is **not retained** — the production bot is written from scratch in phase 3.

1. **Storage layer.** `Recording` dataclass, `recording_manager.py` (CRUD + index), segmented audio writer (format chosen during research), crash-recovery on startup (`recordings.json` reconciliation with on-disk segments). No Discord deps yet — this is pure local file management with tests.
2. **Server discovery + control plane.** `data_dir/server.json` written on `wisper server` startup; FastAPI route stubs at `/api/record/{start,stop,status,…}` returning 501 for now; CLI client (`wisper record …`) that reads `server.json`, hits the routes, and prints the "server not running" error cleanly. Lets us land the CLI↔server plumbing before there's anything to control.
3. **Bot core.** Chosen library integrated into `wisper server` FastAPI lifespan hook; start/stop primitives wired to the routes from phase 2; per-user + combined writer pipeline writing into the storage layer from phase 1; auto-rejoin with backoff.
4. **Campaign / Discord ID binding.** `CampaignMember.discord_user_id`, roster UI updates, auto-tagging during recording.
5. **Web UI.** Record control page, recordings list (campaign-grouped), recording detail page, integration with the existing transcripts page.
6. **Auto-enroll on first hear (Option B).** "Unknown speaker" queue surfaced on the recording detail page, embedding extraction from per-user tracks.
7. **Hand-off into JobQueue.** "Transcribe" button reuses `process_file()` via `submit()`. Recording → transcript association recorded so the existing transcripts page shows them.
8. **Tests + docs.** `test_recording_manager.py`, `test_record_cli.py`, `test_record_routes.py`, `test_discord_bot.py` (using mocked Pycord client + synthesised Opus stream — no live Discord in CI). Update `architecture.md` (new module, new pipeline branch, new config keys, new env var, file-format invariants) and `README.md` (Discord setup walkthrough per resolved item 10, new CLI/UI surface).
9. **Hardening.** Auto-rejoin behaviour walkthrough on a real Discord server, crash recovery walkthrough (kill `wisper server` mid-session and resume), secret handling audit (token storage / masking), dependency footprint check, DAVE re-test if Pycord shipped a new release during development.

(The conditional "live ticker" phase has been dropped — resolved item 13 deferred it to v2.)

### Phase commit cadence

Per CLAUDE.md / user workflow: commit at the end of each numbered phase, push the branch, and pause for user review before starting the next phase.

### v1 file-format invariants for future live transcription (locked)

Live transcription is deferred to v2 (resolved item 13), but v1 must honour these five invariants so v2 can plug in without rewriting the recording layer. **None of these are negotiable during implementation.**

1. **Each segment file must be a self-contained Ogg/Opus container** — not a raw packet stream. v2's live worker will decode any finalized segment with `ffmpeg -i NNNN.opus -f f32le -ar 16000 -ac 1 pipe:1` independently of preceding segments.
2. **Segment manifest must be append-only and atomic.** `metadata.json` (or equivalent sidecar) records each segment's finalization with a monotonic index and the owning `discord_user_id` at the moment it is closed. v2 watches this manifest for new entries.
3. **Segment length must stay within 30–60 s.** Whisper's native context window is 30 s; >60 s segments hit the long-form chunking path with timestamp drift. v1 ships at 60 s.
4. **Per-user track directory layout is a versioned contract.** `data_dir/recordings/<id>/per-user/<discord_id>/NNNN.opus` is fixed. Any rename in v1 breaks v2.
5. **`Recording.status` includes a distinct `"recording"` state.** v2 watches for new segments only while status is `"recording"`; transition to `"completed"` / `"failed"` / `"degraded"` is the stop signal.

### Resolved research items (Sonnet research, 2026-05-03)

Each item below was researched by a sub-agent and a decision was made. Where a recommendation was adopted into the locked decisions table or the file-format invariants above, the source is noted. Where the resolution affects an implementation phase, the relevant phase number is called out.

**1. Discord library.** Pycord (py-cord). Only library with documented stable `VoiceClient.start_recording(sink, …)` per-user receive (added v2.0). Latest release v2.7.2 (2026-04-14), v2.8 in RC. MIT, multi-maintainer. Discarded: `discord-ext-voice-recv` (permanent alpha, single maintainer, same DAVE gap), `discord-ext-listening` (no PyPI release), disnake / hikari / aiortc (no built-in receive or months of work). Caveat: DAVE/E2EE receive is the active risk — see Phase 0 acceptance gates.

**2. Crash-survivable audio format.** Segmented Opus-in-Ogg, one self-contained `.ogg` bitstream per segment file. Beats raw-packets + sidecar (custom muxer needed at concat), segmented WAV (~32× larger), and Matroska (repair tooling). Locked in decisions table; satisfies invariant 1.

**3. Segment length.** 60 seconds. 240 segments × 7 streams (6 users + mixed) for a 4-hour session; concat in 2–4 s; ≤60 s loss on crash. Locked; satisfies invariant 3.

**4. Mixing per-user → combined.** Real-time PCM mixing (~2 % of one CPU core for 6 users). Deferred `ffmpeg amix` was 2–48 minutes for a 4-hour session — unacceptable for "stop and transcribe." A `--remix` repair flag from per-user tracks may be added later without format changes. Locked.

**5. FastAPI lifespan integration.** New `web/discord_bot.py` with a `BotManager` class mirroring `JobQueue.start()/stop()`. Started in the lifespan async context manager after `job_queue.start()`; stopped before `job_queue.stop()` in the cleanup arm. `data_dir/server.json` written immediately before `yield`, deleted after. Stored on `app.state.bot_manager`; routes use `get_bot_manager(request)` helper alongside the existing `get_queue()` ([routes/__init__.py:19–21](src/wisper_transcribe/web/routes/__init__.py#L19-L21)). The `wisper server` CLI ([cli.py:169](src/wisper_transcribe/cli.py#L169)) sets `WISPER_BIND` env var (host:port) before `uvicorn.run()` so `create_app()` can read it without a signature change. Locked.

**6. CLI ↔ server IPC.** Locked previously — HTTP on localhost, `data_dir/server.json` for discovery, `WISPER_SERVER_URL` env var override, error out clearly when server is not running.

**7. Discord channel discovery.** `bot.guilds` and `guild.voice_channels` populate from the gateway cache on startup — no extra REST calls. Required intents: `guilds` + `voice_states` (both non-privileged, present in `discord.Intents.default()`). New endpoint `GET /api/record/channels` returns `[{guild_id, guild_name, voice_channels: [{id, name, members}]}]`; returns 503 if `bot_manager.client` is None or not ready. Code skeleton in research memo (not reproduced here).

**8. Voice activity vs continuous recording.** Continuous-with-DTX. Enable `OPUS_SET_DTX(1)` on each per-user encoder; silence drops to ~400 bit/s comfort-noise, eliminating sidecar-manifest complexity while keeping per-user `.ogg` files directly playable. Locked.

**9. Auto-rejoin policy.** Locked in decisions table (5 retries, backoff `[2,5,15,30,60]`, transient/permanent close-code split, `force=True` disconnect before retry).

**10. Token / app setup UX.** Step-by-step procedure documented for the user docs:
- Discord developer portal → New Application → Bot → Reset Token (copy once)
- Privileged intents: all OFF (we don't need members / presence / message content)
- Gateway intents in code: `guilds` + `voice_states`
- OAuth2 → URL Generator → scope `bot` only; permissions: View Channels, Connect, Speak (last is required by gateway even though we never transmit)
- Invite to server (requires Manage Server role on target guild)
- Wire token: `wisper config discord` interactive wizard or `DISCORD_BOT_TOKEN` env var

These steps must be in `README.md` first-time setup and in a `wisper config discord` wizard ([config.py](src/wisper_transcribe/config.py) extension).

**11. Per-user track size estimate.** ~295 MB per 4-hour 6-player session (per-user with DTX-on + mixed track). ~15 GB/year at 50 sessions/year — well within typical disk budgets. **No automated cleanup policy needed for v1.** Recordings detail page shows per-recording disk usage with a manual delete button. The 460 MB temporary `combined.wav` (16 kHz mono PCM produced for the Whisper pipeline) is auto-deleted post-transcription; re-generate from per-user tracks on demand if needed.

**12. JobQueue compatibility.** `JobQueue.submit(input_path, …)` already accepts an arbitrary string path ([jobs.py:217](src/wisper_transcribe/web/jobs.py#L217)) — no API change needed. **Critical gotcha:** `_run_job` calls `shutil.move()` to rename the file when `tmp_path.stem != original_stem` ([jobs.py:241–244](src/wisper_transcribe/web/jobs.py#L241-L244)). This would relocate the recording's source `combined.wav` out of the recordings directory. Mitigation locked in decisions table: **copy `combined.wav` to `output/<recording_id>.wav` before submitting**, then pass `original_stem=recording_id`, `output_dir=_default_output_dir()`, `campaign=recording.campaign_slug`. `_default_output_dir()` ([transcribe.py:51–63](src/wisper_transcribe/web/routes/transcribe.py#L51-L63)) should be lifted to a shared utility for reuse. Campaign association is not auto-applied by `process_file`; the new endpoint should call `move_transcript_to_campaign(recording_id, recording.campaign_slug)` after the job completes (post-completion hook on `Job` is the cleanest seam).

**13. Live transcription cost gate — DEFER to v2.** Two hard gate failures:
- **JobQueue blocking:** the existing single-worker queue cannot serve a 4-hour live-ticker session and remain available for batch jobs. Fixing requires either monopolizing the queue for the session (unacceptable UX) or introducing a second concurrent transcription path (architectural change excluded by the gate's "re-use existing JobQueue" criterion).
- **Time:** realistic estimate is 2–3 days (locking/coordination between `LiveTickerWorker` and `JobQueue`, second `WhisperModel` instance lifecycle for a CPU `tiny`/`base` live model, SSE wiring, tests). Gate cap is <1 day.

The five v1 file-format invariants above are the price of admission for a clean v2 implementation.

### Future / v2+ (NOT v1, unless promoted by research)

- **Live streaming transcription** with 30–60 s ticker. Per-user tracks (no diarization) → faster-whisper chunks → live ticker UI. **Conditionally promoted to v1** if research item 13 finds it cheap on top of the chosen file format. The on-disk format chosen for v1 must not preclude this — i.e. if the writer is segmented Opus, a chunked transcribe worker must be able to pick up finalized segments as they appear.
- **Voice-print fallback enrollment (Option C)** for already-known wisper profiles whose Discord ID isn't bound on the roster.
- **Video / screenshare recording** (probable extension of the same bot infra; agreed not for v1, evaluate if "easy" during research).
- **Live captions back to Discord.**
- **Per-user transcription with merged timeline view** that surfaces overlap (multiple players talking simultaneously) cleanly.
- **Discord-side slash commands** to start/stop recording from inside the channel.

---

## Backlog

### Web UI auth (project-wide)

**Status:** not started. Trigger: shipping the recording bot raises the destructive-action surface (someone on the LAN can stop a 4-hour D&D recording), so the existing "trust your LAN" posture deserves a second look. Treat this as a single project-wide pass, not piecemeal per-route auth.

**Two reasonable shapes:**
1. **Single shared token.** Generate at first server startup, write to `data_dir/cli-token` (mode 600). Server requires `X-Wisper-Token` header on all mutating routes; CLI reads the file; web UI prompts on first visit and stores in localStorage. Smallest footprint.
2. **Basic username/password.** A users table (or single-user config), session cookie for web UI, `--user`/`--password` or token for CLI. Bigger lift, supports multi-user setups.

**Why not now:** Recording bot is the priority; auth is a horizontal concern that should land as one PR after recording is working, not blocked on it.

---

### Distribution — Tier 3: PyPI + pipx (future)

**Goal:** `pipx install wisper-transcribe` — fully isolated, one command, no venv management.

**What's needed:**
1. **Publish to PyPI** — `pyproject.toml` is already correctly structured. Steps:
   - Create a PyPI account and API token
   - Add a GitHub Actions release workflow (`.github/workflows/publish.yml`) that runs `python -m build && twine upload` on a `v*` tag push
   - `pip install build twine` (dev deps, not in `pyproject.toml`)
2. **pipx install story** — once on PyPI:
   ```bash
   pipx install wisper-transcribe           # base install (Ollama LLM)
   pipx inject wisper-transcribe anthropic  # cloud LLM extras
   ```
3. **Entry-point completeness** — `wisper server` must download `htmx.min.js` on first run if missing (Docker build does this; local pip installs do not). Add a startup check in `app.py` that downloads it if the placeholder is detected.
4. **Version pinning strategy** — ML dependencies (torch, pyannote, faster-whisper) move fast. Consider using `>=` lower bounds (as now) but adding a tested upper bound for major ML versions to prevent surprise breakage on pip installs.

**Why not now:** Requires cutting releases, managing PyPI credentials, and the htmx download story. Good to do once the tool is stable enough to version properly.

---

### DM Character Voice Handling

**Problem:** When a DM does a character voice (dragon accent, goblin voice, NPC), pyannote assigns it a different SPEAKER_XX label than their regular speech. Typical similarity scores: DM normal vs. DM profile ~0.80–0.90, DM character voice vs. DM profile ~0.35–0.55 — often below the 0.65 match threshold, so character voices fall through to `Unknown Speaker N`.

Three approaches designed (April 2026); none implemented yet.

#### Approach 1 — Named Character Profiles *(recommended first step, low complexity)*

No data model changes. The `notes` field stores `voice_of:<key>` to mark a profile as a character voice. The user enrolls the character with the name they want in the transcript (e.g. `DM (as Aziel)`).

**Enrollment UX addition** (one extra prompt after naming a new speaker):
```
  Who is this? DM (as Aziel)
  Is this a character voice performed by an existing speaker? [y/N]: y
  Which speaker performs this voice?
    1. DM  (DM)  — 61%
  > 1
  Enrolled "DM (as Aziel)" as a character voice of DM.
```

**Output:**
```markdown
**DM** *(01:23)*: Let us begin our adventure.
**DM (as Aziel)** *(14:22)*: Come now, little ones. You dare enter my lair?
```

Character voice profiles suppressed from YAML frontmatter `speakers:` list (only real people listed). `wisper speakers list` shows `[voice of DM]` annotation.

**Files:** `pipeline.py` (~20 lines enrollment prompt + speaker_metadata suppression), `cli.py` (speakers list annotation). **Tests:** 3–4 in `test_pipeline.py`, 1 in `test_speaker_manager.py`.

**Limitation:** Format frozen at enrollment time. Change via `wisper speakers rename` + `wisper fix`.

#### Approach 2 — Structured Ownership + Runtime Format Control *(medium complexity, build after Approach 1)*

Add `attributed_to: str | None` and `character_name: str | None` to `SpeakerProfile`. Add `character_voice_format` config key. Formatter assembles the display string from the template at render time.

```toml
character_voice_format = "{speaker} (as {character})"  # default
# alternatives: "{character}" / "{speaker}"
```

**Files:** `models.py`, `speaker_manager.py` (load/save + match_speakers), `formatter.py` (`_merge_consecutive` key change, `_resolve_display()`), `config.py`, `cli.py` (optional `wisper speakers attribute` command). **Type change:** `speaker_map` from `dict[str, str]` → `dict[str, SpeakerRef]` — touches all speaker_map tests.

**Migration from Approach 1:** Non-breaking. Profiles with `notes = "voice_of:X"` can be auto-migrated by reading that field as `attributed_to` in `load_profiles()`.

#### Approach 3 — Automatic Heuristic Detection *(not recommended standalone)*

Re-score `Unknown Speaker N` labels at a looser secondary threshold (~0.40) after primary matching. Can attribute to real speaker but **cannot name the character** → output is `DM (as Unknown Character)`. Threshold has no good default across DMs/campaigns; silent false positives worse than honest unknowns. Best treated as future enhancement layered on Approach 2, not standalone.

---

### Long-Term — Intel GPU Support

**Status:** Research complete (April 2026). Not actionable yet — blocked by upstream dependencies.

**The problem:** Our two core inference engines don't support Intel GPUs:
- **CTranslate2** (powers faster-whisper): NVIDIA CUDA only. Open issue [#1715](https://github.com/OpenNMT/CTranslate2/issues/1715), no work planned.
- **pyannote-audio**: No Intel XPU backend. No upstream interest.

**Viable paths if this becomes a real need:**

1. **OpenVINO backend for transcription** — Intel's inference engine has official Whisper support. Would require an abstraction layer in `transcriber.py` dispatching to either faster-whisper (CUDA/CPU) or OpenVINO (Intel GPU/CPU). Model conversion step needed (Whisper → ONNX → OpenVINO IR).

2. **whisper.cpp with SYCL** — C++ Whisper implementation with full Intel GPU acceleration via SYCL/oneAPI. Python bindings exist (`pywhispercpp`).

3. **Diarization alternatives** — SpeechBrain ECAPA-TDNN for speaker embeddings (actually faster on CPU than pyannote on GPU — 6.7x speedup reported).

**Architecture note:** If a second backend is ever added, use an abstract `TranscriptionBackend` interface in `transcriber.py` and `DiarizationBackend` in `diarizer.py`. Keep pipeline module backend-agnostic.

**When to revisit:** When (a) CTranslate2 adds Intel GPU support, (b) a user actually needs this, or (c) OpenVINO's Whisper API stabilizes. Don't build speculatively.

---

## Codebase conventions memo — Discord recording bot builder

> This memo is for the builder agent implementing the Discord Recording Bot phases.
> Read every section before writing any new file. All patterns below are **mandatory** — code that diverges will fail CodeQL, tests, or style review.

---

### Pattern 1: JSON-backed manager module

**Source:** [`src/wisper_transcribe/campaign_manager.py`](src/wisper_transcribe/campaign_manager.py)

```python
# campaign_manager.py:27–33 — path helpers always via get_data_dir()
def get_campaigns_dir(data_dir: Optional[Path] = None) -> Path:
    base = Path(data_dir) if data_dir else get_data_dir()
    return base / "campaigns"

def get_campaigns_path(data_dir: Optional[Path] = None) -> Path:
    return get_campaigns_dir(data_dir) / "campaigns.json"

# campaign_manager.py:77–83 — load returns {} on missing file; never raises
def load_campaigns(data_dir: Optional[Path] = None) -> dict[str, Campaign]:
    path = get_campaigns_path(data_dir)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    ...

# campaign_manager.py:105–123 — save always uses mkdir parents=True
def save_campaigns(campaigns: dict[str, Campaign], data_dir: Optional[Path] = None) -> None:
    path = get_campaigns_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
```

`recording_manager.py` must follow this exact layout: a `get_recordings_dir()` / `get_recordings_path()` pair that delegates to `get_data_dir()`, a `load_recordings()` that returns `{}` on a missing file, and a `save_recordings()` that calls `mkdir(parents=True, exist_ok=True)` before writing. All CRUD functions (`create_recording`, `delete_recording`, `update_recording`) accept an optional `data_dir` argument for test isolation.

---

### Pattern 2: Slug / ID validation gatekeeper (security-critical)

**Source:** [`src/wisper_transcribe/campaign_manager.py:45–70`](src/wisper_transcribe/campaign_manager.py) and [`src/wisper_transcribe/web/routes/transcribe.py:23–48`](src/wisper_transcribe/web/routes/transcribe.py)

```python
# campaign_manager.py:45–70 — _validate_campaign_slug: two-layer guard
def _validate_campaign_slug(slug: str) -> Optional[str]:
    if not slug or "\x00" in slug:
        return None
    safe = os.path.basename(slug)
    if safe != slug or safe in {".", ".."}:
        return None
    if not re.match(r"^[\w\-]+$", safe):
        return None
    # os.path round-trip breaks the CodeQL taint chain
    _guard_base = os.path.abspath("_campaigns_guard")
    if not _guard_base.endswith(os.sep):
        _guard_base += os.sep
    _guard_path = os.path.abspath(os.path.join(_guard_base, safe))
    if not _guard_path.startswith(_guard_base):
        return None
    return os.path.basename(_guard_path)
```

`recording_manager.py` must expose a `_validate_recording_id(recording_id: str) -> Optional[str]` function using **exactly** this four-step pattern: null-byte check → `os.path.basename` strip → `re.match(r"^[\w\-]+$")` → `os.path.abspath` round-trip returning `os.path.basename(_guard_path)`. The validated value from the manager function — not the raw URL parameter — is what route handlers pass to file-system or redirect operations. CodeQL will flag any shortcut.

---

### Pattern 3: Async server-state object lifecycle (BotManager mirrors JobQueue)

**Source:** [`src/wisper_transcribe/web/jobs.py:188–212`](src/wisper_transcribe/web/jobs.py)

```python
# jobs.py:188–212 — JobQueue lifecycle: start() / stop() called from lifespan
class JobQueue:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Start the background worker. Call from FastAPI lifespan startup."""
        self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        """Stop the background worker. Call from FastAPI lifespan shutdown."""
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            ...
            await asyncio.to_thread(self._run_job, job)
```

`BotManager` must expose the same `start() -> None` / `async stop() -> None` interface. CPU-blocking bot work (Pycord `run()`, audio writes) must be dispatched via `asyncio.to_thread()` or `asyncio.create_task()` so the FastAPI event loop is never blocked. Internal state (current recording, speaker map) is protected the same way `_jobs` is: a plain dict accessed from one thread at a time under the single-worker guarantee.

---

### Pattern 4: FastAPI lifespan + `app.state.<X>` integration

**Source:** [`src/wisper_transcribe/web/app.py:96–142`](src/wisper_transcribe/web/app.py) and [`src/wisper_transcribe/web/routes/__init__.py:19–21`](src/wisper_transcribe/web/routes/__init__.py)

```python
# app.py:96–138 — create_app() wires lifespan + app.state
def create_app() -> FastAPI:
    job_queue = JobQueue()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _build_tailwind()
        job_queue.start()
        yield
        await job_queue.stop()

    app = FastAPI(..., lifespan=lifespan)
    app.add_middleware(_SecurityHeadersMiddleware)
    app.state.job_queue = job_queue
    ...

# routes/__init__.py:19–21 — dependency helper pattern
def get_queue(request: Request) -> JobQueue:
    return request.app.state.job_queue
```

`BotManager` is created inside `create_app()`, started **after** `job_queue.start()`, stopped **before** `await job_queue.stop()`, and stored on `app.state.bot_manager`. The companion `get_bot_manager(request: Request) -> BotManager` helper lives in `routes/__init__.py` alongside `get_queue`. `server.json` is written immediately before the lifespan `yield` and deleted in the post-`yield` arm.

---

### Pattern 5: Route module conventions (path validation, redirect taint, error codes)

**Source:** [`src/wisper_transcribe/web/routes/campaigns.py:56–95`](src/wisper_transcribe/web/routes/campaigns.py) and [`src/wisper_transcribe/web/routes/transcribe.py:163–177`](src/wisper_transcribe/web/routes/transcribe.py)

```python
# campaigns.py:56–95 — every slug URL param is validated before use
@router.get("/{slug}", response_class=HTMLResponse)
async def campaign_detail(request: Request, slug: str) -> HTMLResponse:
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return HTMLResponse(content="Invalid campaign slug", status_code=400)
    campaigns = load_campaigns()
    campaign = campaigns.get(safe)
    if campaign is None:
        return RedirectResponse(url="/campaigns?error=not_found", status_code=303)
    ...

# transcribe.py:163–177 — redirect via server-generated object.id, not validated input
@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str) -> Response:
    safe_id = _validate_job_id(job_id)
    if safe_id is None:
        return HTMLResponse(content="Invalid job ID", status_code=400)
    queue = _get_queue(request)
    queue.cancel(safe_id)
    job = queue.get(safe_id)           # look up server object
    if job is None:
        return RedirectResponse(url="/transcribe", status_code=303)
    return RedirectResponse(url=f"/transcribe/jobs/{job.id}", status_code=303)  # job.id is uuid4
```

Every route that accepts an ID or slug from the URL must call the appropriate `_validate_*` function first and return 400 on failure. Redirect `Location` values must use the server object's own `.id` field (set from `uuid.uuid4()` at creation), never the validated URL parameter. Error redirects use generic `?error=some_code` query params — never `str(exc)` or internal paths.

---

### Pattern 6: SSE streaming endpoint

**Source:** [`src/wisper_transcribe/web/routes/transcribe.py:194–256`](src/wisper_transcribe/web/routes/transcribe.py)

```python
# transcribe.py:194–256 — SSE generator with disconnect check + 1 s poll
@router.get("/jobs/{job_id}/stream")
async def job_stream(request: Request, job_id: str) -> StreamingResponse:
    async def event_generator():
        last_line_idx = 0
        while True:
            if await request.is_disconnected():
                break
            job = queue.get(job_id)
            if job is None:
                yield "event: error\ndata: Job not found\n\n"
                return
            new_lines = job.log_lines[last_line_idx:]
            for line in new_lines:
                data = json.dumps({"type": "log", "message": line})
                yield f"data: {data}\n\n"
            last_line_idx += len(new_lines)
            data = json.dumps({"type": "status", "status": job.status})
            yield f"data: {data}\n\n"
            if job.status in (COMPLETED, FAILED):
                yield f"data: {json.dumps({'type': 'done', ...})}\n\n"
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

The `/record/status/stream` endpoint (real-time session status: who is speaking, segment count, elapsed time, reconnect events) must follow this exact shape: `StreamingResponse` with `media_type="text/event-stream"`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`, a `while True` loop with `request.is_disconnected()` guard, JSON-encoded event payloads, and `await asyncio.sleep(1.0)` between polls.

---

### Pattern 7: Template style — list grouped by campaign

**Source:** [`src/wisper_transcribe/web/templates/transcripts.html:7–49`](src/wisper_transcribe/web/templates/transcripts.html)

```jinja2
{# transcripts.html:7–49 — card macro with absolute-inset overlay link + z-50 action buttons #}
{% macro transcript_card(t) %}
<div class="card relative hover:shadow-md hover:ring-1 hover:ring-green-300 transition-all flex flex-col">
  <a href="/transcripts/{{ t.stem | urlencode }}" class="absolute inset-0 rounded-lg z-10" aria-label="..."></a>
  <div class="flex items-start justify-between">
    <h3 class="text-sm font-semibold text-gray-900 truncate flex-1 mr-2">{{ t.title }}</h3>
    <div class="flex items-center gap-2 flex-shrink-0 relative z-50">
      {# action buttons (download, delete) at z-50 so they float above the overlay link #}
    </div>
  </div>
</div>
{% endmacro %}

{# transcripts.html:63–80 — campaign grouping via <details open> #}
{% for slug, c in campaigns.items() %}
  {% set campaign_items = transcripts | selectattr("campaign_slug", "equalto", slug) | list %}
  {% if campaign_items %}
  <details class="card" open>
    <summary class="text-base font-semibold text-gray-900 cursor-pointer select-none flex items-center gap-2">
      {{ c.display_name }}
      <span class="text-xs font-normal text-gray-400 ml-1">{{ campaign_items | length }} sessions</span>
    </summary>
    <div class="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {% for t in campaign_items %}{{ transcript_card(t) }}{% endfor %}
    </div>
  </details>
  {% endif %}
{% endfor %}
```

The `/recordings` list page must use the identical pattern: a Jinja2 `{% macro recording_card(r) %}` with an `absolute inset-0 z-10` overlay link for the whole-card click target and `relative z-50` action buttons; campaign grouping via `<details class="card" open>` driven by `campaigns` passed in the template context; an uncampaigned fallback section; and an empty-state card. All file name tokens in URLs must go through the `| urlencode` filter.

---

### Pattern 8: Template style — SSE-driven detail page with progress

**Source:** [`src/wisper_transcribe/web/templates/job_detail.html:47–99`](src/wisper_transcribe/web/templates/job_detail.html) and [`job_detail.html:129–412`](src/wisper_transcribe/web/templates/job_detail.html)

```jinja2
{# job_detail.html:47–63 — step pills rendered from Jinja data, mirrored in JS STEPS array #}
{% for step_id, step_abbr, step_label in steps %}
<div id="step_{{ step_id }}" class="flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs ...">
  <span id="step_icon_{{ step_id }}" ...>{{ step_abbr }}</span>
  {{ step_label }}
</div>
{% endfor %}

{# job_detail.html:268–270 — SSE wire-up using server-side job.id (never a URL param) #}
const es = new EventSource('/transcribe/jobs/{{ job.id }}/stream');
es.onmessage = function(e) { const evt = JSON.parse(e.data); ... };
es.onerror = function() { es.close(); };
```

The `/recordings/<id>` detail page must use the same structure: step pills defined in both Jinja and a mirrored JS `STEPS` array; a single unified progress bar; a `#log-terminal` div for streamed log lines with a blinking `#log-cursor`; `new EventSource('/record/status/stream')` wired to update badge, bar, and log; and a `done` event handler that swaps `#job-actions` innerHTML to show the "Transcribe" button. The `<script>` block is only rendered when the session is active (`status in ("recording", "degraded")`).

---

### Pattern 9: Template style — list + create + slug-detail trio

**Source:** [`src/wisper_transcribe/web/templates/campaigns.html`](src/wisper_transcribe/web/templates/campaigns.html)

```jinja2
{# campaigns.html:14–26 — error display via ?error= query param, not flash middleware #}
{% if request.query_params.get('error') == 'create_failed' %}
<div class="rounded-md bg-red-50 border border-red-200 p-3 text-sm text-red-700">...</div>
{% endif %}

{# campaigns.html:29–39 — inline create form in a card #}
<div class="card">
  <form method="post" action="/campaigns" class="flex gap-2 items-end">
    <input type="text" name="display_name" required .../>
    <button type="submit" class="btn-primary">Create</button>
  </form>
</div>

{# campaigns.html:41–115 — detail panel injected into the same page via active_campaign context var #}
{% if active_campaign %}
  <!-- roster table + add/remove member forms -->
{% elif campaigns %}
  <!-- grid of campaign cards -->
{% else %}
  <!-- empty state -->
{% endif %}
```

The recordings route uses the same single-template-for-list-and-detail trick: the route handler passes `active_recording=None` on the list view and `active_recording=recording` on the detail view; the template uses `{% if active_recording %}` to switch between the two panels. Error feedback always goes through `?error=<code>` query params read with `request.query_params.get('error')` — no server-side flash session.

---

### Pattern 10: CLI command group (mirror `wisper campaigns` and `wisper config llm`)

**Source:** [`src/wisper_transcribe/cli.py:750–885`](src/wisper_transcribe/cli.py) (campaigns group) and [`cli.py:460–551`](src/wisper_transcribe/cli.py) (config llm wizard)

```python
# cli.py:750–753 — group declaration on main
@main.group()
def campaigns():
    """Manage campaigns (per-show speaker rosters)."""

# cli.py:785–804 — every command validates slug via manager helper before use
@campaigns.command("delete")
@click.argument("slug")
@click.option("--yes", is_flag=True, default=False, help="Skip confirmation prompt")
def campaigns_delete(slug: str, yes: bool):
    from .campaign_manager import _validate_campaign_slug, delete_campaign
    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise click.ClickException(f"Invalid campaign slug: {slug!r}")
    if not yes:
        click.confirm(f"Delete campaign {safe!r}?", abort=True)
    ...

# cli.py:460–551 — config_llm wizard pattern: prompt → validate → save_config
@config.command("llm")
def config_llm():
    from .config import load_config, save_config
    cfg = load_config()
    provider = click.prompt("Provider [...]", default=...).strip().lower()
    ...
    save_config(cfg)
    click.echo("Saved.")
```

`wisper record` is a `@main.group()` with subcommands `start`, `stop`, `list`, `show`, `transcribe`, `delete` — each as a `@record.command("name")` function. Every subcommand that takes a recording ID calls `_validate_recording_id()` and raises `click.ClickException` on failure. The `wisper config discord` wizard follows the `config_llm` pattern: `load_config()` → `click.prompt()` for bot token (with `hide_input=True`) and guild/channel defaults → `save_config(cfg)`. The CLI is a thin HTTP client: commands call `POST /api/record/start` (etc.) after reading `data_dir/server.json`; if the server is unreachable they print a clear error and exit 1 — no in-process bot logic.

---

### Pattern 11: Test fixture conventions

**Source:** [`tests/conftest.py`](tests/conftest.py), [`tests/test_campaign_manager.py`](tests/test_campaign_manager.py), [`tests/test_web_routes.py`](tests/test_web_routes.py), [`tests/test_path_traversal.py`](tests/test_path_traversal.py)

```python
# conftest.py:31–38 — autouse fixture patches config at the module boundary
@pytest.fixture(autouse=True)
def _isolated_pipeline_config():
    with patch("wisper_transcribe.pipeline.load_config", return_value=dict(_BASE_CONFIG)):
        yield

# test_campaign_manager.py:30–31 — manager tests use tmp_path for isolation
def test_load_campaigns_missing_file_returns_empty(tmp_path):
    result = load_campaigns(tmp_path)
    assert result == {}

# test_web_routes.py:15–27 — TestClient fixture without lifespan
@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c

# test_path_traversal.py:20–30 — parametrize payloads by category
_MALICIOUS_PAYLOADS = ["\x00", "some\x00name"]
_REGEX_PAYLOADS = ["invalid*name", "invalid+name", "name!@#"]
_REDIRECT_PAYLOADS = ["\\\\evil.com", "javascript:alert(1)", "\r\nLocation: evil.com"]

@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS)
def test_transcripts_path_traversal_blocked(client, payload):
    resp = client.get(f"/transcripts/{quote(payload)}")
    assert resp.status_code == 400
```

`test_recording_manager.py` must mirror `test_campaign_manager.py`: every function accepts `tmp_path`, passes it as `data_dir`, covers missing-file → empty return, save/load roundtrip, duplicate rejection, and `_validate_recording_id` parametrize with valid and invalid sets. `test_record_routes.py` must use the `client` fixture (no lifespan) and mock `BotManager` at `wisper_transcribe.web.discord_bot.BotManager`. `test_path_traversal.py` gains a new section covering recording ID endpoints with `_MALICIOUS_PAYLOADS`, `_REGEX_PAYLOADS`, and `_REDIRECT_PAYLOADS` parametrize blocks — all three categories must be tested for every route that accepts a recording ID in the URL.

---

## Spec A: Test fixture design — mocked Discord voice receive

### A.1 `tests/_discord_fakes.py` design

The module lives in the test package (not `src/`) and is imported by conftest and individual test files. It has zero runtime dependencies on Pycord — it only imports `asyncio`, `dataclasses`, `struct`, and `typing`.

#### `FakeDiscordClient`

Drop-in for `discord.Bot`. Constructed once per test via the `fake_discord_client` fixture (see A.2). Every method that would normally perform I/O returns immediately or appends to an internal event log so tests can assert call order.

```python
@dataclass
class FakeDiscordClient:
    # Internal state observable by tests
    connected: bool = False
    recording: bool = False
    _events: list[dict] = field(default_factory=list)
    _sink: "FakeAudioSink | None" = None
    _stop_callback: "Callable | None" = None
    _ready: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self, token: str) -> None:
        self._events.append({"type": "start", "token": token[:4] + "…"})
        self.connected = True
        self._ready.set()

    async def connect(self) -> None:
        self.connected = True
        self._events.append({"type": "connect"})

    async def disconnect(self, *, force: bool = False) -> None:
        self.connected = False
        self._events.append({"type": "disconnect", "force": force})

    async def wait_until_ready(self) -> None:
        await self._ready.wait()

    def start_recording(self, sink, callback, channel) -> None:
        self._sink = sink
        self._stop_callback = callback
        self.recording = True
        self._events.append({"type": "start_recording", "channel_id": channel.id})

    async def stop_recording(self) -> None:
        self.recording = False
        self._events.append({"type": "stop_recording"})
        if self._stop_callback:
            await self._stop_callback(self._sink, None)

    @property
    def guilds(self) -> "list[FakeGuild]":
        return self._guilds

    # set by VoiceEventScript.run() to supply guild/channel context
    _guilds: "list[FakeGuild]" = field(default_factory=list)
```

`FakeDiscordClient` also exposes a `close_code: int | None` attribute that `VoiceEventScript` sets before injecting a disconnect event, so `BotManager`'s reconnect logic can be driven through the transient/permanent code table locked in the architecture decisions.

#### `VoiceEventScript`

Declarative script builder. Methods are chainable and return `self`. `.run(client, sink)` is a coroutine that drives the `FakeDiscordClient` asynchronously, feeding packets and firing disconnect events at the declared wall-clock offsets (simulated with `asyncio.sleep` scaled by a `time_scale` factor defaulting to `0.01` so a 30-second script finishes in 0.3 s in CI).

```python
class VoiceEventScript:
    def user_speaks(self, user_id: str, start: float, duration: float,
                    frequency_hz: float = 440.0) -> "VoiceEventScript": ...
    def disconnect(self, code: int, t: float) -> "VoiceEventScript": ...
    def reconnect(self, t: float) -> "VoiceEventScript": ...
    async def run(self, client: FakeDiscordClient,
                  sink: "FakeAudioSink",
                  time_scale: float = 0.01) -> None: ...
```

`.run()` fires events in chronological order. For `user_speaks` it calls `sink.write(user_id, packet)` at 20 ms intervals (simulating the Pycord 20 ms Opus frame boundary) for the declared duration. For `disconnect` it sets `client.close_code = code` and then calls `await client.disconnect(force=True)`. For `reconnect` it calls `await client.connect()` and clears `close_code`.

#### `synth_opus_packet(duration_s, frequency_hz) -> bytes`

Two implementations are provided; tests choose via a fixture parameter:

- **`FAKE_OPUS` mode (default):** returns a 4-byte little-endian header `b"\x7f\xfe"` + 2-byte sequence number + zero-padded payload of the correct size for a 20 ms Opus frame (80 bytes). Downstream Ogg writers that call `.write()` with raw bytes accept this without decoding. Fast; no libopus required.
- **`REAL_OPUS` mode:** calls `opuslib.Encoder(48000, 1, opuslib.APPLICATION_AUDIO).encode(pcm_sine(duration_s, frequency_hz), frame_size)`. Requires `opuslib` (wheels available on all three CI platforms via py-cord[voice]). Used only in `test_audio_writer.py::test_segment_eos_page_written_on_finalize` to confirm the finalised file is genuinely playable by ffprobe.

The helper is exposed at module level so individual test functions can import it directly without going through the fixture tree.

#### `FakeVoiceChannel` and `FakeGuild`

```python
@dataclass
class FakeVoiceChannel:
    id: str
    name: str
    guild: "FakeGuild"
    members: list["FakeMember"] = field(default_factory=list)

@dataclass
class FakeGuild:
    id: str
    name: str
    voice_channels: list[FakeVoiceChannel] = field(default_factory=list)

@dataclass
class FakeMember:
    id: str
    display_name: str
```

All three classes have a `.mention` property returning `f"<@{self.id}>"` to satisfy any Pycord-API surface that uses it.

### A.2 Autouse conftest.py additions

Two new autouse fixtures are added to `tests/conftest.py`, mirroring the existing `_isolated_pipeline_config` and `_block_real_llm_calls` pattern ([tests/conftest.py:31–72](tests/conftest.py)):

```python
@pytest.fixture(autouse=True)
def _patch_pycord(monkeypatch):
    """Replace the real discord module with a fake at the import boundary.

    Any module that does `import discord` or `from discord import ...`
    inside wisper_transcribe.web.discord_bot gets the fake instead.
    Tests that need to exercise real Pycord behaviour must skip this
    fixture with @pytest.mark.usefixtures (rare — only integration
    tests running against a real gateway, not part of CI).
    """
    import tests._discord_fakes as fakes
    import types

    fake_discord = types.ModuleType("discord")
    fake_discord.Bot = fakes.FakeDiscordClient
    fake_discord.Client = fakes.FakeDiscordClient
    fake_discord.Intents = MagicMock()
    fake_discord.Intents.default.return_value = MagicMock()
    fake_discord.sinks = MagicMock()
    fake_discord.sinks.OggSink = fakes.FakeAudioSink

    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    monkeypatch.setitem(sys.modules, "discord.sinks", fake_discord.sinks)
    yield


@pytest.fixture(autouse=True)
def _block_discord_network():
    """Block accidental aiohttp connections to *.discord.com.

    Pycord uses aiohttp internally. This fixture patches
    aiohttp.ClientSession._request so any real network call to
    discord.com raises RuntimeError, the same way _block_real_llm_calls
    patches httpx.stream for LLM clients ([tests/conftest.py:41–72]).
    Tests that genuinely need aiohttp (none in CI) must patch over this.
    """
    original = None
    try:
        import aiohttp
        original = aiohttp.ClientSession._request
    except ImportError:
        yield
        return

    async def _blocked(self, method, url, **kwargs):
        if "discord.com" in str(url) or "discord.gg" in str(url):
            raise RuntimeError(
                f"Real Discord network call blocked by conftest.py: {url}. "
                "Patch aiohttp.ClientSession._request explicitly in your test."
            )
        return await original(self, method, url, **kwargs)

    with patch.object(aiohttp.ClientSession, "_request", _blocked):
        yield
```

These fixtures compose safely: `_patch_pycord` runs first (autouse order is definition order within the file) so by the time `_block_discord_network` runs, `discord` is already the fake module and aiohttp is never imported by the bot code during tests.

### A.3 Synthesised Opus stream — tradeoffs and recommendation

| Layer | Mode | Rationale |
|---|---|---|
| Unit tests (recording_manager, audio_writer rotation, mixer) | **FAKE_OPUS** | No libopus binary required; tests are purely about byte counts, manifest entries, and file rotation logic — not audio fidelity. |
| Integration test "start → speak → stop → manifest correct" | **FAKE_OPUS** | The Ogg writer only calls `.write(user_id, bytes)` — it doesn't decode the payload. FAKE_OPUS bytes produce a valid Ogg container as long as the writer wraps them correctly. |
| `test_segment_eos_page_written_on_finalize` | **REAL_OPUS** | This test calls `ffprobe` to confirm the finalised `.opus` file is well-formed. ffprobe validates Ogg headers and the EOS page; it does not decode frames. A 20 ms real Opus frame is required so ffprobe doesn't reject the stream as corrupt. |
| `test_realtime_mixer_clips_correctly` | **FAKE_OPUS** | The mixer operates on decoded PCM (`bytes` of int16 samples). The test synthesises raw PCM directly — no Opus encode/decode needed. |
| Phase 9 hardening / smoke test | **REAL_OPUS** | The one test that runs `ffplay --no-display` on a completed segment to confirm audible audio. Marked `@pytest.mark.slow` and skipped in CI unless `WISPER_SLOW_TESTS=1`. |

**Recommendation: default all CI tests to FAKE_OPUS.** The real Opus path lives in `tests/_discord_fakes.py` behind a `try/except ImportError` guard on `opuslib`, so a CI runner without py-cord[voice] simply skips those tests rather than erroring. Add `py-cord[voice]` to `[project.optional-dependencies] dev` in `pyproject.toml` so local devs get real Opus automatically, but mark it `; sys_platform != "unsupported"` to avoid blocking CI on unusual runners.

### A.4 Cross-platform caveats

- **libopus binary:** shipped by py-cord[voice] as a compiled wheel on Windows x64, macOS arm64, and Linux x86_64/aarch64. The CI matrix (Python 3.10–3.13, ubuntu-latest + windows-latest + macos-latest) is fully covered. No separate `apt-get install libopus-dev` step needed.
- **PyNaCl:** required by Pycord for voice encryption; wheel available on all three platforms via the same `py-cord[voice]` extra. In tests it is never called (FakeDiscordClient bypasses the encryption path entirely), but it must be importable or Pycord's own `__init__` may raise. The `_patch_pycord` fixture prevents the real import by replacing the module before any bot code runs.
- **ffmpeg / ffprobe in CI:** already required by existing wisper tests (audio_utils, pipeline). The GitHub Actions matrix already installs ffmpeg via `apt-get install ffmpeg` (Linux) and `brew install ffmpeg` (macOS) and `choco install ffmpeg` (Windows). No new CI step needed.
- **Windows path separators:** `FakeVoiceChannel.id` and recording IDs use only ASCII digits and hyphens (matching `_validate_recording_id`'s regex), so no path separator ambiguity arises when these values appear in directory names.

### A.5 Reusable test utilities

Both helpers live in `tests/_discord_fakes.py` and are importable by any test file.

```python
async def assert_recording_status_eventually(
    recording_id: str,
    expected_status: str,
    data_dir: Path,
    timeout: float = 2.0,
    poll_interval: float = 0.05,
) -> None:
    """Poll recording_manager.load_recordings() until status matches or timeout."""
    from wisper_transcribe.recording_manager import load_recordings
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        recs = load_recordings(data_dir)
        rec = recs.get(recording_id)
        if rec and rec.status == expected_status:
            return
        await asyncio.sleep(poll_interval)
    raise AssertionError(
        f"recording {recording_id!r} status never reached {expected_status!r} "
        f"within {timeout}s (last seen: {rec.status if rec else 'not found'})"
    )


def tmp_recording_dir(tmp_path: Path, recording_id: str = "test-rec-0001") -> Path:
    """Create a minimal recording directory tree and return the root."""
    root = tmp_path / "recordings" / recording_id
    (root / "combined").mkdir(parents=True)
    (root / "per-user").mkdir()
    (root / "final").mkdir()
    return root
```

`assert_recording_status_eventually` is intentionally async so it works naturally inside `pytest-anyio` or `asyncio.run()` test bodies. `tmp_recording_dir` is synchronous and usable as a plain helper or wrapped in a `@pytest.fixture` that calls it with `tmp_path`.

### A.6 Three example tests

#### Example 1 — Unit test: SegmentedOggWriter rotation (FAKE_OPUS, no Discord at all)

```python
# tests/test_audio_writer.py
import asyncio
from pathlib import Path
from tests._discord_fakes import synth_opus_packet, tmp_recording_dir

def test_segmented_ogg_writer_rotates_at_60s(tmp_path):
    """Writer must close the current segment and open a new one at 60 s."""
    from wisper_transcribe.web.audio_writer import SegmentedOggWriter

    root = tmp_recording_dir(tmp_path)
    writer = SegmentedOggWriter(
        stream_dir=root / "combined",
        segment_duration_s=60,
    )
    # Feed 61 seconds of 20 ms packets (3050 packets) using fake Opus bytes
    for i in range(3050):
        pkt = synth_opus_packet(0.02)   # FAKE_OPUS mode; returns ~84 fake bytes
        writer.write(pkt)
    writer.finalize()

    segments = sorted((root / "combined").glob("*.opus"))
    assert len(segments) == 2, f"expected 2 segments, got {len(segments)}: {segments}"
    # Each segment must be non-empty (EOS page was written)
    for seg in segments:
        assert seg.stat().st_size > 0
```

#### Example 2 — Integration test: start → speak → stop → manifest correct (FAKE_OPUS, FakeDiscordClient)

```python
# tests/test_discord_bot.py
import asyncio
import pytest
from tests._discord_fakes import (
    FakeDiscordClient, FakeGuild, FakeVoiceChannel, FakeMember,
    VoiceEventScript, tmp_recording_dir,
    assert_recording_status_eventually,
)

@pytest.mark.asyncio
async def test_start_recording_user_speaks_stop_segment_manifest(tmp_path):
    """BotManager records a scripted session; manifest has one segment per stream."""
    from wisper_transcribe.web.discord_bot import BotManager

    guild = FakeGuild(id="111", name="TestGuild")
    channel = FakeVoiceChannel(id="222", name="general", guild=guild,
                               members=[FakeMember(id="U1", display_name="Alice")])
    guild.voice_channels.append(channel)

    client = FakeDiscordClient(_guilds=[guild])
    manager = BotManager(data_dir=tmp_path, client_factory=lambda: client)

    manager.start()
    recording = await manager.start_session(
        campaign_slug="dnd-mondays",
        voice_channel_id="222",
        guild_id="111",
    )

    script = (
        VoiceEventScript()
        .user_speaks("U1", start=0.0, duration=5.0, frequency_hz=440)
    )
    await script.run(client, client._sink, time_scale=0.01)
    await manager.stop_session()
    await manager.stop()

    await assert_recording_status_eventually(
        recording.id, "completed", tmp_path, timeout=2.0
    )

    from wisper_transcribe.recording_manager import load_recordings
    recs = load_recordings(tmp_path)
    rec = recs[recording.id]
    # At minimum: one finalized segment on the "mixed" stream
    finalized = [s for s in rec.segment_manifest if s.finalized]
    assert any(s.stream == "mixed" for s in finalized)
    assert any(s.stream == "U1" for s in finalized)
```

#### Example 3 — Security test: recording_id path traversal (mirrors test_path_traversal.py pattern)

```python
# tests/test_path_traversal.py  (new section appended to existing file)
import pytest
from urllib.parse import quote
from unittest.mock import patch, MagicMock

_RECORDING_MALICIOUS = ["\x00", "some\x00name"]
_RECORDING_REGEX = ["invalid*name", "invalid+name", "id/with/slashes"]
_RECORDING_REDIRECT = ["\\\\evil.com", "javascript:alert(1)", "\r\nLocation: evil.com"]

@pytest.mark.parametrize("payload", _RECORDING_MALICIOUS + _RECORDING_REGEX)
def test_recording_detail_path_traversal_blocked(client, payload):
    """GET /recordings/<id> must reject any ID that fails _validate_recording_id."""
    with patch("wisper_transcribe.web.routes.record.load_recordings", return_value={}):
        resp = client.get(f"/recordings/{quote(payload)}")
    assert resp.status_code == 400

@pytest.mark.parametrize("payload", _RECORDING_MALICIOUS + _RECORDING_REGEX)
def test_recording_delete_path_traversal_blocked(client, payload):
    with patch("wisper_transcribe.web.routes.record.load_recordings", return_value={}):
        resp = client.post(f"/recordings/{quote(payload)}/delete")
    assert resp.status_code == 400

@pytest.mark.parametrize("payload", _RECORDING_MALICIOUS + _RECORDING_REGEX)
def test_recording_transcribe_path_traversal_blocked(client, payload):
    """POST /recordings/<id>/transcribe must not pass tainted ID to JobQueue."""
    mock_bm = MagicMock()
    with patch("wisper_transcribe.web.routes.record.load_recordings", return_value={}), \
         patch("wisper_transcribe.web.routes.record.get_bot_manager", return_value=mock_bm):
        resp = client.post(f"/recordings/{quote(payload)}/transcribe")
    assert resp.status_code == 400
```

---

## Spec B: Per-phase acceptance criteria

### Phase 1 acceptance — Storage layer

**Files added:**
- `src/wisper_transcribe/recording_manager.py` (~250 lines)
- `src/wisper_transcribe/web/audio_writer.py` (~200 lines)
- `src/wisper_transcribe/models.py` — additions: `Recording`, `SegmentRecord`, `RejoinAttempt`

**Tests added:**
- `tests/test_recording_manager.py`:
  - `test_load_save_roundtrip`
  - `test_create_recording_generates_uuid`
  - `test_validate_recording_id_rejects_traversal_payloads` (parametrize)
  - `test_append_segment_atomic_under_concurrent_calls`
  - `test_reconcile_on_startup_clean_completion`
  - `test_reconcile_on_startup_orphaned_segment_marked_failed`
  - `test_reconcile_on_startup_corrupt_recordings_json_logs_and_returns`
- `tests/test_audio_writer.py`:
  - `test_segmented_ogg_writer_rotates_at_60s`
  - `test_segment_eos_page_written_on_finalize`
  - `test_realtime_mixer_clips_correctly`
  - `test_writer_recovers_from_crash_mid_segment`

**Behaviour:** A test simulates a 3-minute recording session by feeding synthesised Opus packets (FAKE_OPUS mode) into a `SegmentedOggWriter`; the manifest shows 3 segments per stream; `ffprobe` confirms each segment is a valid Ogg/Opus file with a proper EOS page. The `REAL_OPUS` variant of `test_segment_eos_page_written_on_finalize` is gated behind `@pytest.mark.slow`.

**Docs:** `architecture.md` gets a new "Recording layer" subsection covering the modules, file layout, and the five v1 file-format invariants. `README.md` unchanged this phase (no user-facing surface yet).

**Done when:**
- [ ] All test cases above pass (`pytest tests/test_recording_manager.py tests/test_audio_writer.py -v`)
- [ ] `_validate_recording_id` follows the four-step CodeQL pattern from Pattern 2
- [ ] `SegmentedOggWriter` writes a complete Ogg EOS page on `finalize()` (confirmed by ffprobe in slow test)
- [ ] `architecture.md` updated with Recording layer subsection
- [ ] No Pycord import anywhere in these modules

**PR scope:** ONE PR. ~600 lines including tests. No external Discord deps; reviewable in isolation.

**Commit stub:** `feat(record): add recording_manager + audio_writer storage layer`

---

### Phase 2 acceptance — Server discovery + control plane

**Files added/modified:**
- `src/wisper_transcribe/web/app.py` — write `server.json` before lifespan `yield`, delete after
- `src/wisper_transcribe/web/routes/record.py` (~120 lines) — route stubs returning 501
- `src/wisper_transcribe/cli.py` — new `wisper record` group with `start`, `stop`, `list`, `show`, `transcribe`, `delete` subcommands; `wisper config discord` wizard

**Tests added:**
- `tests/test_record_cli.py`:
  - `test_record_start_errors_when_server_not_running`
  - `test_record_start_reads_server_json_and_posts`
  - `test_record_list_formats_output_grouped_by_campaign`
  - `test_wisper_server_url_env_var_overrides_server_json`
  - `test_record_show_validates_recording_id`
- `tests/test_record_routes.py`:
  - `test_record_start_returns_501_before_bot_core`
  - `test_server_json_written_on_lifespan_startup`
  - `test_server_json_deleted_on_lifespan_shutdown`

**Behaviour:** Run `wisper server` in one terminal; in another, run `wisper record start --campaign dnd-mondays --voice-channel 12345`. The CLI reads `data_dir/server.json`, calls `POST /api/record/start`, and prints the 501 stub response. Stop the server; confirm `server.json` is gone. Run `wisper record start` again with server stopped; confirm "wisper server is not running" error message and exit code 1.

**Docs:** `README.md` gets a "Discord bot — CLI" section stub (placeholder text "Bot core lands in Phase 3"). `architecture.md` gains entries for `server.json` under Environment Variables and a new "CLI ↔ server IPC" subsection.

**Done when:**
- [ ] `server.json` is written before lifespan yield and deleted in cleanup arm
- [ ] `WISPER_SERVER_URL` env var overrides `server.json` lookup
- [ ] All 7 route stubs return 501 with a JSON body `{"detail": "not implemented"}`
- [ ] CLI prints clear "server not running" error (not a Python traceback) when `server.json` absent
- [ ] All test cases above pass
- [ ] `architecture.md` and `README.md` updated

**PR scope:** ONE PR. ~300 lines. Stub routes are trivial to review; CLI plumbing is the substance.

**Commit stub:** `feat(record): add server.json discovery + record route stubs + CLI client`

---

### Phase 3 acceptance — Bot core

**Files added/modified:**
- `src/wisper_transcribe/web/discord_bot.py` (~350 lines) — `BotManager` class, `start()`/`stop()`, Pycord lifespan integration, per-user + combined writer pipeline, auto-rejoin with backoff schedule `[2, 5, 15, 30, 60]`
- `src/wisper_transcribe/web/app.py` — wire `BotManager` into lifespan after `job_queue.start()`
- `src/wisper_transcribe/web/routes/__init__.py` — add `get_bot_manager(request)`
- `src/wisper_transcribe/web/routes/record.py` — implement `POST /api/record/start` and `POST /api/record/stop`; remaining stubs stay 501
- `tests/conftest.py` — add `_patch_pycord` and `_block_discord_network` autouse fixtures
- `tests/_discord_fakes.py` — `FakeDiscordClient`, `VoiceEventScript`, `synth_opus_packet`, `FakeVoiceChannel`, `FakeGuild`, helpers

**Tests added:**
- `tests/test_discord_bot.py`:
  - `test_bot_manager_start_stop_lifecycle`
  - `test_start_session_creates_recording_in_manager`
  - `test_user_speaks_writes_packets_to_per_user_dir`
  - `test_auto_rejoin_on_transient_close_code_4015`
  - `test_auto_rejoin_exhausted_sets_degraded_status`
  - `test_permanent_close_code_4014_aborts_without_retry`
  - `test_stop_session_sets_completed_status`

**Behaviour:** With a real Discord bot token in `DISCORD_BOT_TOKEN` and a test server (manual smoke test only — not in CI), run `wisper server` and `wisper record start --campaign dnd-mondays --voice-channel <id>`. Speak for 30 s; check `data_dir/recordings/<id>/combined/0000.opus` exists and is >0 bytes. Run `wisper record stop`; check status is "completed" and `final/combined.wav` was produced. In CI the entire flow runs against `FakeDiscordClient` with `VoiceEventScript`.

**Docs:** `architecture.md` gains a "Bot core" subsection covering `BotManager` lifecycle, the auto-rejoin policy table, and close code categorisation. `README.md` unchanged (no user-facing setup steps yet — Discord token setup lands in Phase 9 docs pass).

**Done when:**
- [ ] `BotManager.start()` / `stop()` mirror `JobQueue` interface (Pattern 3)
- [ ] `_patch_pycord` and `_block_discord_network` autouse fixtures live in `conftest.py`
- [ ] Per-user and combined `SegmentedOggWriter` instances are created per-session, not per-bot
- [ ] Rejoin backoff uses exactly `[2, 5, 15, 30, 60]` seconds (tested via `FakeDiscordClient`)
- [ ] All 7 test cases pass
- [ ] `architecture.md` updated

**PR scope:** ONE PR. This is the largest phase (~700 lines). Consider a "draft → review" cycle before merging; do not split, as `BotManager` + `_discord_fakes.py` + conftest changes are tightly coupled.

**Commit stub:** `feat(record): add BotManager + Pycord integration + auto-rejoin`

---

### Phase 4 acceptance — Campaign / Discord ID binding

**Files added/modified:**
- `src/wisper_transcribe/models.py` — `CampaignMember.discord_user_id: Optional[str] = None`
- `src/wisper_transcribe/campaign_manager.py` — `bind_discord_id(slug, profile_key, discord_user_id)`, `lookup_profile_by_discord_id(slug, discord_user_id)`
- `src/wisper_transcribe/web/routes/campaigns.py` — POST handler for binding form
- `src/wisper_transcribe/web/templates/campaign_detail.html` — "Link Discord ID" column in roster table
- `src/wisper_transcribe/web/discord_bot.py` — auto-tag logic: on `on_voice_state_update`, resolve `discord_user_id` → profile key via `lookup_profile_by_discord_id`

**Tests added:**
- `tests/test_campaign_manager.py` (new cases):
  - `test_bind_discord_id_persists`
  - `test_lookup_profile_by_discord_id_returns_profile_key`
  - `test_lookup_returns_none_for_unknown_id`
  - `test_bind_discord_id_overwrites_previous_binding`
- `tests/test_discord_bot.py` (new cases):
  - `test_known_discord_id_tagged_automatically_in_manifest`
  - `test_unknown_discord_id_not_tagged`

**Behaviour:** In the campaign roster UI, paste a Discord user ID next to "Alice" and click "Link". Start a recording session; when Alice speaks, her per-user track directory name should be `<discord_id>` and `Recording.discord_speakers[alice_discord_id]` should equal `"alice"`. Verify by running `wisper record show <id>` which prints the speaker binding table.

**Docs:** `README.md` gets a "Binding Discord IDs to campaign members" section (2–3 steps). `architecture.md` updates the `CampaignMember` data model entry and adds an "Auto-tagging" note under Bot core.

**Done when:**
- [ ] `CampaignMember.discord_user_id` serialises/deserialises through `campaigns.json`
- [ ] Roster UI shows a text input for Discord ID per member
- [ ] `lookup_profile_by_discord_id` is called in `BotManager` on every voice packet
- [ ] All 6 new test cases pass (existing campaign tests still pass)
- [ ] `architecture.md` and `README.md` updated

**PR scope:** ONE PR. Touches three layers (model, manager, UI, bot) but each change is small. Reviewable together.

**Commit stub:** `feat(record): add CampaignMember.discord_user_id + auto-tagging in BotManager`

---

### Phase 5 acceptance — Web UI

**Files added/modified:**
- `src/wisper_transcribe/web/routes/record.py` — implement remaining HTML routes: `GET /record`, `GET /recordings`, `GET /recordings/<id>`, `POST /recordings/<id>/delete`; placeholder `GET /recordings/<id>/live` returning 501
- `src/wisper_transcribe/web/templates/record.html` — control panel: campaign select, voice channel picker, start/stop button, session status (speaker dots, segment count, elapsed, disconnect events via SSE)
- `src/wisper_transcribe/web/templates/recordings.html` — list page, campaign-grouped (Pattern 7)
- `src/wisper_transcribe/web/templates/recording_detail.html` — metadata, speaker table, segment list, "Transcribe" button (disabled until Phase 7), "Download combined" button
- `src/wisper_transcribe/web/app.py` — register record router; add "Record" tab to nav
- `static/tailwind.min.css` — rebuilt after template changes
- `src/wisper_transcribe/web/routes/__init__.py` — add `get_bot_manager` helper if not already present

**Tests added:**
- `tests/test_record_routes.py` (new cases):
  - `test_record_page_returns_200`
  - `test_recordings_list_returns_200_empty`
  - `test_recordings_list_groups_by_campaign`
  - `test_recording_detail_returns_200`
  - `test_recording_detail_unknown_id_redirects`
  - `test_recording_delete_removes_entry`
  - `test_recording_live_returns_501`

**Behaviour:** Open `http://localhost:8080/record`. Select a campaign and voice channel from the dropdowns; click "Start Recording". The status panel shows a blinking mic indicator for each speaking user (SSE-driven). Click "Stop"; the page transitions to a "completed" state and shows a "Transcribe" button (greyed out until Phase 7). Navigate to `/recordings` and confirm the new entry appears under the correct campaign heading.

**Docs:** `README.md` gets a "Web UI — recording" section with screenshots (or ASCII art placeholders). `architecture.md` updates the module map to include the new route files and templates.

**Done when:**
- [ ] All 7 test cases pass
- [ ] `tailwind.min.css` is committed alongside template changes
- [ ] `/record` SSE endpoint follows Pattern 6 (StreamingResponse, `Cache-Control: no-cache`)
- [ ] Recording IDs in all route handlers pass through `_validate_recording_id` before file-system use (CodeQL pattern)
- [ ] `/recordings/<id>/live` returns 501 with a `{"detail": "not implemented in v1"}` body
- [ ] `architecture.md` and `README.md` updated

**PR scope:** ONE PR for the route + template shell. If Tailwind rebuild produces a large diff, commit `tailwind.min.css` in the same PR (do not split — CLAUDE.md requires it in the same commit as template changes).

**Commit stub:** `feat(record): add Record control panel + recordings list + detail UI`

---

### Phase 6 acceptance — Auto-enroll on first hear (Option B)

**Files added/modified:**
- `src/wisper_transcribe/web/discord_bot.py` — unknown-speaker detection: when `discord_user_id` is not bound in roster, append to `Recording.unbound_speakers` list (new field)
- `src/wisper_transcribe/models.py` — `Recording.unbound_speakers: list[str]` (list of discord_user_ids heard but not bound)
- `src/wisper_transcribe/web/routes/record.py` — `POST /recordings/<id>/enroll` handler: takes `discord_user_id` + `profile_name`, calls speaker_manager to extract embedding from per-user track, adds to roster
- `src/wisper_transcribe/web/templates/recording_detail.html` — "Unknown speakers" panel listing unbound IDs with a "Name & Enroll" form per entry

**Tests added:**
- `tests/test_discord_bot.py` (new cases):
  - `test_unknown_speaker_added_to_unbound_list`
  - `test_known_speaker_not_added_to_unbound_list`
- `tests/test_record_routes.py` (new cases):
  - `test_enroll_unknown_speaker_creates_profile`
  - `test_enroll_unknown_speaker_invalid_id_returns_400`
  - `test_enroll_already_bound_speaker_returns_409`

**Behaviour:** After a recording session with an unbound Discord user, navigate to the recording detail page. The "Unknown speakers" panel shows the Discord user ID and a text input. Enter a name, click "Enroll"; the page refreshes showing the user bound to a new wisper profile. The per-user `.opus` track is used to extract a speaker embedding (the same `extract_embedding()` path used by the enrollment CLI).

**Docs:** `README.md` gets an "Auto-enrollment from recordings" walkthrough. `architecture.md` updates the `Recording` data model table and adds a note under "Speaker enrollment" about Option B.

**Done when:**
- [ ] `Recording.unbound_speakers` is populated during recording (not post-hoc)
- [ ] Enrollment form validates Discord user ID format before calling `extract_embedding`
- [ ] `extract_embedding` is mocked in all tests (no real audio processing in CI)
- [ ] All 5 new test cases pass
- [ ] `architecture.md` and `README.md` updated

**PR scope:** ONE PR. All changes are contained to the recording subsystem plus one `speaker_manager.py` call.

**Commit stub:** `feat(record): add unknown-speaker detection + post-session enrollment UI`

---

### Phase 7 acceptance — Hand-off into JobQueue

**Files added/modified:**
- `src/wisper_transcribe/web/routes/record.py` — implement `POST /recordings/<id>/transcribe`: copy `combined.wav` to `output/<recording_id>.wav`, call `job_queue.submit()` with `original_stem=recording_id`, `output_dir=_default_output_dir()`, `campaign=recording.campaign_slug`; record `Job.id` on `Recording.job_id` (new field); add post-completion hook to call `move_transcript_to_campaign`
- `src/wisper_transcribe/models.py` — `Recording.job_id: Optional[str] = None`, `Recording.transcript_path: Optional[Path]` (already in model; confirm wired)
- `src/wisper_transcribe/web/jobs.py` — add optional `on_complete: Callable[[Job], None]` callback parameter to `submit()` (or use a post-run hook); called after `_run_job` sets status to `COMPLETED`
- `src/wisper_transcribe/web/routes/transcribe.py` — `_default_output_dir()` lifted to `wisper_transcribe.web.routes.__init__` as a shared utility (it was previously module-private; see resolved item 12)
- `src/wisper_transcribe/web/templates/recording_detail.html` — "Transcribe" button enabled; transitions to "transcribing" badge when clicked; links to existing job detail page once job is queued

**Tests added:**
- `tests/test_record_routes.py` (new cases):
  - `test_transcribe_button_copies_combined_wav_not_moves`
  - `test_transcribe_button_submits_to_job_queue`
  - `test_transcribe_button_sets_recording_job_id`
  - `test_post_completion_hook_calls_move_transcript_to_campaign`
  - `test_transcribe_on_already_transcribing_recording_returns_409`

**Behaviour:** On a completed recording's detail page, click "Transcribe". The page shows a "Queued" badge; the job appears in the existing `/transcribe` job list with the recording ID as the job name. After transcription completes, the recording detail page shows a "View transcript" link. The original `recordings/<id>/combined/` segments are untouched — only the copy at `output/<recording_id>.wav` was consumed.

**Docs:** `README.md` gets a short note under the recording detail page description explaining the "Transcribe" button. `architecture.md` updates the JobQueue compatibility note (resolved item 12) to say it is now implemented and points to the `on_complete` callback.

**Done when:**
- [ ] `combined.wav` copy is verified in tests (assert original path still exists after job completes)
- [ ] `_default_output_dir()` is imported from the shared location, not duplicated
- [ ] `Recording.job_id` is persisted to `metadata.json`
- [ ] Transcript appears on existing `/transcripts` page with correct campaign grouping
- [ ] All 5 new test cases pass
- [ ] `architecture.md` and `README.md` updated

**PR scope:** ONE PR. Core logic is small (~80 lines in routes + ~30 lines in jobs.py for the hook). The shared utility refactor of `_default_output_dir()` adds one more file but is non-breaking.

**Commit stub:** `feat(record): wire Transcribe button to JobQueue with post-completion campaign hook`

---

### Phase 8 acceptance — Tests + docs

**Files added/modified:**
- `tests/test_recording_manager.py` — gap-fill: any cases identified during phases 1–7 that were deferred
- `tests/test_record_cli.py` — full coverage of `wisper record` subcommands with mocked HTTP
- `tests/test_discord_bot.py` — full coverage including `VoiceEventScript` multi-user scenario
- `tests/test_path_traversal.py` — new section: all three payload categories for every recording ID endpoint (per A.6 example 3 above)
- `docs/architecture.md` — complete Discord bot section: module map entry, pipeline branch diagram, config keys (`DISCORD_BOT_TOKEN`), file-format invariants table, Known Constraints table entry for "one active recording at a time"
- `docs/README.md` — Discord setup walkthrough (resolved item 10): developer portal steps, bot permissions, `wisper config discord` wizard, `DISCORD_BOT_TOKEN` env var

**Tests added (gap-fill):**
- `tests/test_record_cli.py`:
  - `test_record_delete_requires_confirmation`
  - `test_record_delete_yes_flag_skips_confirmation`
  - `test_record_transcribe_errors_when_already_transcribing`
  - `test_config_discord_wizard_saves_token_masked`
- `tests/test_discord_bot.py`:
  - `test_multi_user_session_produces_per_user_dirs`
  - `test_reconnect_after_4015_resumes_recording`
  - `test_degraded_status_after_max_retries`

**Behaviour:** Run the full test suite (`pytest tests/ -v --cov --cov-report=term-missing`); all new and existing tests pass. Coverage for `recording_manager.py`, `audio_writer.py`, and `discord_bot.py` is ≥80 %. `architecture.md` module map lists all new modules; test count is updated to reflect the new total.

**Docs:** This phase IS the docs pass — both files get their final, complete Discord content.

**Done when:**
- [ ] `pytest tests/ -v` is entirely green with no skips except `@pytest.mark.slow`
- [ ] Path-traversal section in `test_path_traversal.py` covers all three payload categories for all recording ID endpoints
- [ ] `architecture.md` test count updated
- [ ] README Discord setup walkthrough matches resolved item 10 step by step
- [ ] No `# TODO` comments left in Discord bot modules

**PR scope:** ONE PR (docs + gap-fill tests only — no new behaviour). Small, fast to review.

**Commit stub:** `docs(record): complete architecture.md + README Discord setup walkthrough; fill test gaps`

---

### Phase 9 acceptance — Hardening

**Files added/modified:**
- `src/wisper_transcribe/web/discord_bot.py` — DAVE re-test after any Pycord release during development; update pinned version or swap back to PyPI if RC is now stable
- `src/wisper_transcribe/config.py` — confirm `DISCORD_BOT_TOKEN` is masked in `wisper config show` (same as `HF_TOKEN`)
- `scripts/smoke_test_recording.sh` (or `.ps1`) — **not committed to CI**; throwaway manual walkthrough script for the hardening checklist
- `pyproject.toml` — dependency footprint check: confirm `py-cord[voice]` is in `[project.dependencies]` (not just dev), pin minimum Pycord version after DAVE re-test outcome

**Tests added:**
- `tests/test_discord_bot.py` (new cases):
  - `test_discord_token_masked_in_config_show`
  - `test_crash_recovery_orphaned_recording_marked_failed_on_startup`
- `tests/test_record_routes.py` (new cases):
  - `test_concurrent_start_session_returns_409`
  - `test_start_session_with_no_bot_token_returns_503`

**Behaviour (manual walkthrough — not automated):**
1. Start `wisper server` with `DISCORD_BOT_TOKEN` set; join a voice channel; let it record for 5 minutes; `kill -9` the process mid-segment. Restart; confirm `reconcile_on_startup()` marks the orphaned segment `finalized=False` and the recording status transitions to "failed" rather than hanging at "recording".
2. Run `wisper config show`; confirm the token appears as `***` not its literal value.
3. (If Pycord shipped a new release) re-run Phase 0 acceptance gates A/B/C against the new version; update the pin if needed.

**Docs:** `README.md` adds a "Known limitations (v1)" callout box: one active recording at a time, no multi-guild, DAVE status note with link to pycord#3135.

**Done when:**
- [ ] All 4 new test cases pass
- [ ] `DISCORD_BOT_TOKEN` masked in `wisper config show` output
- [ ] Manual crash-recovery walkthrough completed and notes appended to plan.md resolved items
- [ ] Pycord version pin reviewed against latest release
- [ ] `README.md` Known limitations section added
- [ ] One PR; this is the merge-gate before the feature branch is declared done

**PR scope:** ONE PR. Hardening changes are intentionally small — the PR is a signal that the feature is production-ready, not a code dump. If the DAVE re-test requires a library swap, that is a single `pyproject.toml` line change plus a comment.

**Commit stub:** `fix(record): hardening pass — token masking, crash recovery, DAVE re-test, dep pin`

---

## Appendix — Implementation Specs: HTTP API, server.json, and Crash Recovery

> Generated 2026-05-03. All architecture decisions from "Active — Discord Recording Bot" are locked. These specs give the phase-1–3 builder precise, builder-ready detail. Where prior-art patterns exist in the codebase, references are given as `[filename:lines]`.

---

## Spec 1 — HTTP API Table

### Validation helpers (applies to every route below)

**Recording IDs** are uuid4 strings. Validate with a `_validate_recording_id()` helper mirroring `_validate_job_id` ([transcribe.py:23–48](src/wisper_transcribe/web/routes/transcribe.py#L23-L48)):
1. `re.match(r"^[\w\-]+$", recording_id)` — reject everything else.
2. `os.path.abspath` + `startswith` round-trip — breaks the CodeQL taint chain before any filesystem or redirect use.
3. Look up the server-side `Recording` object; use `recording.id` (set from `uuid.uuid4()` at creation, never from user input) in any redirect `Location` header.

**Campaign slugs** use `_validate_campaign_slug` from [campaign_manager.py:45–70](src/wisper_transcribe/campaign_manager.py#L45-L70).

**Discord snowflakes** (guild/channel/user IDs): `re.match(r"^\d{17,20}$", value)` — Discord IDs are 17–20 digit integers; reject anything else before use. These do not reach the filesystem so no `os.path` round-trip is required, but they must never appear raw in redirect URLs (use server-side object attributes instead).

**Profile keys** follow the existing `^[\w\-]+$` + `os.path.basename` + `abspath/startswith` guard used in [campaigns.py:139–152](src/wisper_transcribe/web/routes/campaigns.py#L139-L152).

All error responses use short snake_case error codes; messages are user-facing plain English. **Never** reflect `str(exc)` into a response body or `Location` header — use generic codes (e.g. `enroll_failed`, `not_found`).

---

### JSON API routes

```
### POST /api/record/start
Auth: none (matches existing posture — locked decision)
Request body (JSON):
{
  "campaign_slug": "string (required) — validated via _validate_campaign_slug",
  "guild_id":       "string (required) — snowflake, re.match(r'^\\d{17,20}$')",
  "voice_channel_id": "string (required) — snowflake, same validation"
}
Response 200 (JSON):
{
  "recording_id": "uuid4 string",
  "status": "recording",
  "started_at": "ISO 8601 UTC"
}
Response 400: {"error": "code", "message": "human-readable string"}
  error codes:
    bot_not_ready        — BotManager.client exists but not yet logged in
    channel_not_found    — voice_channel_id not in guild cache
    campaign_not_found   — campaign_slug not in campaigns.json
    already_recording    — a session is currently active
    invalid_campaign_slug — failed _validate_campaign_slug
    invalid_guild_id     — failed snowflake regex
    invalid_channel_id   — failed snowflake regex
Response 503: {"error": "bot_offline", "message": "Discord client not connected"}
Idempotency: NOT idempotent. Repeat call while active returns 400 already_recording.
Side effects:
  BotManager.start_recording() → asyncio.create_task(client.join_vc()) →
  AudioPipeline opens per-user + combined SegmentedOggWriter instances →
  recording_manager.create_recording() writes metadata.json →
  segment manifest begins accumulating (append-only, invariant 2)
Web UI usage: /record control panel "Start" button
CLI usage: wisper record start --campaign <slug> --voice-channel <id> [--guild <id>]

### POST /api/record/stop
Auth: none
Request body: {} (empty — at most one active recording exists)
Response 200 (JSON):
{
  "recording_id": "uuid4 string",
  "status": "completed",
  "ended_at": "ISO 8601 UTC",
  "segment_count": 42,
  "combined_path": "relative path under data_dir"
}
Response 400: {"error": "not_recording", "message": "No active recording session"}
Response 503: {"error": "bot_offline"}
Side effects:
  BotManager.stop_recording() → flush final segment (EOS page written, finalized=True) →
  ffmpeg -f concat -c copy finalizes combined.wav in recordings/<id>/final/ →
  recording_manager.update_status(id, "completed", ended_at=now()) →
  vc.disconnect(force=True)
  Does NOT auto-submit to JobQueue; caller must POST /api/recordings/{id}/transcribe.
Web UI usage: "Stop" button on /record control panel
CLI usage: wisper record stop

### GET /api/record/status
Auth: none
Response 200 when active (JSON):
{
  "active": true,
  "recording_id": "uuid4 string",
  "status": "recording" | "degraded",
  "campaign_slug": "string",
  "guild_id": "string",
  "voice_channel_id": "string",
  "started_at": "ISO 8601 UTC",
  "elapsed_seconds": 3724.1,
  "segment_count": 62,
  "speaker_count": 4,
  "rejoin_count": 0
}
Response 200 when idle (JSON):
{ "active": false }
Response 503: {"error": "bot_offline"}
Notes: reads from BotManager.active_recording in-memory; no disk I/O on hot path.

### GET /api/record/channels
Auth: none
Response 200 (JSON):
[
  {
    "guild_id": "string",
    "guild_name": "string",
    "voice_channels": [
      {
        "id": "string",
        "name": "string",
        "members": ["discord_user_id_string", ...]
      }
    ]
  }
]
Response 503: {"error": "bot_offline"} — BotManager.client is None or not ready
Notes: populates from bot.guilds / guild.voice_channels gateway cache (no extra REST
  calls); requires Intents.guilds + Intents.voice_states (both non-privileged —
  resolved item 7 confirms this).

### GET /api/record/bot-status
Auth: none
Response 200 (JSON):
{
  "connected": true | false,
  "latency_ms": 42.3 | null,
  "guilds": 1,
  "user_tag": "WisperBot#1234" | null,
  "degraded": false,
  "degraded_reason": null | "string"
}
Notes: latency_ms from client.latency * 1000; degraded = active recording has
  status "degraded" (auto-rejoin exhausted).

### GET /api/recordings
Auth: none
Query params: ?campaign=<slug> (optional) — validated via _validate_campaign_slug
Response 200 (JSON):
[
  {
    "id": "uuid4 string",
    "campaign_slug": "string | null",
    "status": "recording|degraded|completed|failed|transcribing|transcribed",
    "started_at": "ISO 8601 UTC",
    "ended_at": "ISO 8601 UTC | null",
    "guild_id": "string",
    "voice_channel_id": "string",
    "speaker_count": 3,
    "segment_count": 240,
    "has_transcript": false,
    "disk_bytes": 309715200
  }
]

### GET /api/recordings/{id}
Auth: none
Path validation: _validate_recording_id (400 on invalid format, 404 on missing)
Response 200 (JSON): full Recording dataclass serialized, including segment_manifest
Response 400: {"error": "invalid_id"}
Response 404: {"error": "not_found"}

### POST /api/recordings/{id}/transcribe
Auth: none
Path validation: _validate_recording_id → look up recording.id (uuid4) → use
  recording.id (not user-supplied id) in all subsequent references
Request body: {} (reserved for future override params)
Response 200 (JSON):
{
  "job_id": "uuid4 string",
  "status": "pending"
}
Response 400:
  {"error": "invalid_id"}
  {"error": "not_completed"} — recording.status not in {"completed", "failed"}
  {"error": "already_transcribing"} — recording.status == "transcribing"
Response 404: {"error": "not_found"}
Side effects:
  shutil.copy(combined.wav, output/<recording_id>.wav) →
  queue.submit(input_path=copy_path, original_stem=recording.id,
               output_dir=_default_output_dir(), campaign=recording.campaign_slug) →
  recording.status = "transcribing" persisted to metadata.json
  After job completes (post-completion hook on Job):
    recording.status = "transcribed"
    recording.transcript_path = output_path
    move_transcript_to_campaign(recording.id, recording.campaign_slug) called
Note: _default_output_dir() is defined at [transcribe.py:51–63](src/wisper_transcribe/web/routes/transcribe.py#L51-L63);
  lift to web/utils.py for reuse (resolved item 12 recommends this).
  shutil.copy avoids the shutil.move gotcha in [jobs.py:241–244](src/wisper_transcribe/web/jobs.py#L241-L244).

### POST /api/recordings/{id}/delete
Auth: none
Path validation: _validate_recording_id
Request body: {}
Response 200 (JSON): {"deleted": true}
Response 400:
  {"error": "invalid_id"}
  {"error": "recording_active"} — status in {"recording", "degraded"}; refuse deletion
Response 404: {"error": "not_found"}
Side effects:
  shutil.rmtree(data_dir/recordings/<id>/) →
  recording_manager.delete_recording(recording.id) removes entry from recordings.json

### POST /api/recordings/{id}/bind-speaker
Auth: none
Path validation: _validate_recording_id
Request body (JSON):
{
  "discord_user_id": "string (required) — snowflake validation",
  "profile_key":     "string (required) — ^[\\w\\-]+$ + os.path round-trip"
}
Response 200 (JSON):
{
  "discord_user_id": "string",
  "profile_key": "string",
  "display_name": "string"
}
Response 400:
  {"error": "invalid_id"}
  {"error": "invalid_discord_id"}  — failed snowflake regex
  {"error": "invalid_profile_key"} — failed key validation
  {"error": "unknown_discord_id"}  — discord_user_id not in recording.discord_speakers
  {"error": "unknown_profile"}     — profile_key not in load_profiles()
Response 404: {"error": "not_found"}
Side effects (Option A):
  recording.discord_speakers[discord_user_id] = profile_key →
  metadata.json persisted

### POST /api/recordings/{id}/enroll-new-speaker
Auth: none
Path validation: _validate_recording_id
Request body (JSON):
{
  "discord_user_id": "string (required) — snowflake validation",
  "display_name":    "string (required)"
}
Response 200 (JSON):
{
  "profile_key": "string",
  "display_name": "string",
  "enrolled": true
}
Response 400:
  {"error": "invalid_id"}
  {"error": "invalid_discord_id"}
  {"error": "invalid_display_name"} — empty or whitespace-only
  {"error": "unknown_discord_id"}
  {"error": "no_audio"} — per-user track dir empty or missing
  {"error": "enroll_failed"} — enroll_speaker() raised (generic code; never str(exc))
Response 404: {"error": "not_found"}
Side effects (Option B):
  profile_key = display_name.lower().replace(" ", "_") →
  enroll_speaker(name=profile_key, display_name=display_name,
                 audio_path=per_user_dir/<discord_id>/,
                 segments=per_track_segments) →
  recording.discord_speakers[discord_user_id] = profile_key persisted

### GET /api/recordings/{id}/stream
Auth: none
Path validation: _validate_recording_id (400 on invalid)
Response: text/event-stream (SSE), headers Cache-Control: no-cache + X-Accel-Buffering: no
  Mirrors job stream at [transcribe.py:194–256](src/wisper_transcribe/web/routes/transcribe.py#L194-L256).
  Polling interval: 1.0 s.
  Event payloads (JSON-encoded data field):
    {"type": "segment",  "index": N, "stream": "mixed"|"<discord_user_id>", "finalized": true}
    {"type": "status",   "status": "recording"|"degraded"|"completed"|"failed"}
    {"type": "rejoin",   "attempt": N, "close_code": 4009, "timestamp": "ISO 8601 UTC"}
    {"type": "speaker",  "discord_user_id": "...", "profile_key": "..."}
    {"type": "done",     "status": "completed"|"failed"}
  Stream terminates when status leaves {"recording", "degraded"}.

### POST /api/campaigns/{slug}/members/{profile_key}/discord-id
Auth: none
Path validation:
  slug via _validate_campaign_slug ([campaign_manager.py:45–70](src/wisper_transcribe/campaign_manager.py#L45-L70))
  profile_key via ^[\w\-]+$ + os.path round-trip ([campaigns.py:139–152](src/wisper_transcribe/web/routes/campaigns.py#L139-L152))
Request body (JSON):
{
  "discord_user_id": "string | null  (null clears the binding)"
}
  Validated: re.match(r'^\d{17,20}$', value) or value is null
Response 200 (JSON):
{
  "profile_key": "string",
  "discord_user_id": "string | null"
}
Response 400:
  {"error": "invalid_slug"}
  {"error": "invalid_profile_key"}
  {"error": "invalid_discord_id"} — non-null value that fails snowflake regex
Response 404:
  {"error": "campaign_not_found"}
  {"error": "member_not_found"}
Side effects:
  CampaignMember.discord_user_id updated → save_campaigns() persists campaigns.json
  (Roster-side binding for Option A; complements bind-speaker which tags after-the-fact)
```

---

### HTML routes (server-rendered Jinja templates)

```
### GET /record
Response: 200 HTML
Template: record.html
Context:
  bot_status    — from BotManager (connected bool, latency, guilds)
  active_recording — Recording | None
  campaigns     — dict[str, Campaign] from load_campaigns()
  guild_channels — embedded on page load (same payload as GET /api/record/channels)
Sections: bot status badge · campaign selector · voice channel picker ·
  Start/Stop button · live session panel with htmx SSE to
  GET /api/recordings/{id}/stream (mic-activity dots, segment count, elapsed, rejoin log)

### GET /recordings
Response: 200 HTML
Query params: ?campaign=<slug> (optional) — validated before use
Template: recordings.html
Layout: grouped by campaign, matching transcripts list structure
  at [transcripts.py:149–191](src/wisper_transcribe/web/routes/transcripts.py#L149-L191)
Columns: status badge · campaign · started_at · duration · speakers ·
  segments · disk size · Transcribe / Delete actions

### GET /recordings/{id}
Path validation: _validate_recording_id → 400 on invalid, 404 on missing
Response: 200 HTML
Template: recording_detail.html
Context:
  recording    — Recording dataclass
  speakers     — list of {discord_user_id, profile_key | None, display_name | "Unknown"}
  job          — Job | None (when transcribing or transcribed)
  profiles     — dict[str, SpeakerProfile]
  campaigns    — dict[str, Campaign]
Actions:
  Transcribe button → POST /api/recordings/{id}/transcribe (htmx, updates button state)
  Per-speaker bind controls → POST /api/recordings/{id}/bind-speaker
  Per-unknown enroll form → POST /api/recordings/{id}/enroll-new-speaker
  Download combined.wav
  Delete button → POST /api/recordings/{id}/delete
  All mutating POSTs return 303 See Other to GET /recordings/{id} (matching convention
  at [campaigns.py:53](src/wisper_transcribe/web/routes/campaigns.py#L53))
```

---

## Spec 2 — server.json Schema

### File location and permissions

```
data_dir/server.json
```

Same data directory as `campaigns/`, `profiles/`, `recordings/`. File mode: **644** (world-readable on POSIX). Rationale: the file contains only bind address and PID — no secrets. Making it 600 would block other local tools from reading the server URL without elevation. HF token and Discord token live in `config.toml` (mode 600) or env vars, never here.

### Example document

```json
{
  "host": "127.0.0.1",
  "port": 8080,
  "scheme": "http",
  "pid": 12345,
  "started_at": "2026-05-03T19:42:00.123456+00:00",
  "version": "0.12.0"
}
```

### Field documentation

| Field | Type | Notes |
|-------|------|-------|
| `host` | string | Bind host, e.g. `"127.0.0.1"` or `"0.0.0.0"` |
| `port` | integer | Bind port, e.g. `8080` |
| `scheme` | string | `"http"` in v1; reserved for future TLS |
| `pid` | integer | `os.getpid()` — used by stale-file detection |
| `started_at` | string | `datetime.now(timezone.utc).isoformat()` |
| `version` | string | `importlib.metadata.version("wisper-transcribe")` |

### Lifecycle

Written in the FastAPI lifespan context manager immediately before the `yield`, after `job_queue.start()` and `bot_manager.start()`. Deleted in the `finally` arm after the `yield`. This matches resolved item 5.

```python
# in lifespan() — pseudocode
import importlib.metadata, json, os
from datetime import datetime, timezone
from wisper_transcribe.config import get_data_dir

server_json_path = get_data_dir() / "server.json"
server_json_path.write_text(json.dumps({
    "host": host, "port": port, "scheme": "http",
    "pid": os.getpid(),
    "started_at": datetime.now(timezone.utc).isoformat(),
    "version": importlib.metadata.version("wisper-transcribe"),
}))
try:
    yield
finally:
    server_json_path.unlink(missing_ok=True)
    await bot_manager.stop()
    await job_queue.stop()
```

### Stale-file detection on startup

Before writing a fresh `server.json`, check if one already exists from a previous crash:

```python
if server_json_path.exists():
    try:
        old = json.loads(server_json_path.read_text())
        url = f"{old['scheme']}://{old['host']}:{old['port']}/health"
        import httpx
        httpx.get(url, timeout=2.0)
        # Reachable → another process is running; abort
        raise RuntimeError(
            f"wisper server already running at {url} (PID {old.get('pid')})"
        )
    except (httpx.RequestError, KeyError, json.JSONDecodeError):
        # Unreachable or corrupt → stale, safe to overwrite
        logger.warning(
            "Replacing stale server.json from previous crash (PID %s)",
            old.get("pid") if isinstance(old, dict) else "unknown"
        )
        server_json_path.unlink(missing_ok=True)
```

### WISPER_SERVER_URL env var semantics

When set, `WISPER_SERVER_URL` overrides `server.json` entirely. Must be a fully-qualified URL including scheme and port (e.g. `http://192.168.1.10:8080`). The CLI strips any trailing slash before appending route paths. The env var is checked first in the discovery loop so a remote server can be targeted without touching the local data dir.

### CLI server-discovery helper skeleton

```python
# src/wisper_transcribe/cli_http.py

import json
import os
import sys
from pathlib import Path


def _resolve_server_url() -> str:
    """Locate the running wisper server for CLI → HTTP IPC.

    Discovery order (first reachable wins):
      1. WISPER_SERVER_URL env var
      2. data_dir/server.json
      3. Hard default http://127.0.0.1:8080

    Exits with code 2 and a plain-English error message if no candidate
    is reachable.  Never auto-launches the server.
    """
    import httpx
    from wisper_transcribe.config import get_data_dir

    candidates: list[tuple[str, str]] = []

    # 1. Env var override
    env_url = os.environ.get("WISPER_SERVER_URL", "").rstrip("/")
    if env_url:
        candidates.append(("WISPER_SERVER_URL", env_url))

    # 2. server.json
    server_json = Path(get_data_dir()) / "server.json"
    if server_json.exists():
        try:
            data = json.loads(server_json.read_text(encoding="utf-8"))
            url = f"{data['scheme']}://{data['host']}:{data['port']}"
            candidates.append(("server.json", url.rstrip("/")))
        except (KeyError, json.JSONDecodeError, OSError):
            pass  # corrupt / unreadable — skip silently

    # 3. Hard default
    candidates.append(("default", "http://127.0.0.1:8080"))

    for _source, url in candidates:
        try:
            r = httpx.get(f"{url}/health", timeout=2.0)
            if r.status_code < 500:
                return url
        except httpx.RequestError:
            continue

    # All candidates exhausted
    print(
        "Error: wisper server is not running.\n"
        "Start it with:  wisper server\n"
        "Or set WISPER_SERVER_URL to point at a remote instance.",
        file=sys.stderr,
    )
    sys.exit(2)
```

Every `wisper record` subcommand calls `url = _resolve_server_url()` at the top of its handler, then uses `httpx.post(f"{url}/api/record/start", json=payload)` etc. No auto-launch, no standalone bot path — per the locked CLI ↔ server IPC decision.

---

## Spec 3 — Crash Recovery Algorithm

### Context

`recording_manager.reconcile_on_startup()` is called once during `wisper server` lifespan startup, before the lifespan `yield` and before any Discord client is created. It runs synchronously. Its job is to bring `recordings.json` into consistency with what is actually on disk, and to remove a stale `server.json` if present.

### Detecting an incomplete Ogg file

An Ogg file is complete when its final page has the EOS (end-of-stream) bit set in the page header flag byte (bit 2, value `0x04`) per RFC 3533 §6. The helper:

```python
def _ogg_has_eos(path: Path) -> bool:
    """Return True if the last Ogg page in *path* has the EOS flag set."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()
            chunk_size = min(65536, file_size)
            f.seek(file_size - chunk_size)
            tail = f.read(chunk_size)
            # Find last OggS capture pattern (4 bytes)
            idx = tail.rfind(b"OggS")
            if idx == -1:
                return False
            # Page header byte offset 5 is header_type_flag; bit 2 = EOS
            if idx + 5 >= len(tail):
                return False
            return bool(tail[idx + 5] & 0x04)
    except OSError:
        return False
```

If `ffprobe` is available and the Ogg parse is uncertain (multi-stream container edge case), fall back to a duration check: `ffprobe -v quiet -print_format json -show_format <path>` and check `data["format"]["duration"] > "0"`.

### Logging conventions

Uses `logging.getLogger("wisper_transcribe.recording_manager")`, which routes through the root Python logger and is captured by `_LoggingBridge` when `--debug` is active (per [architecture.md:195](architecture.md#L195)). The startup summary line is emitted at `INFO` so it appears in normal server output without `--debug`. Per-recording detail (which segment, which recording) is at `DEBUG`.

### Pseudocode for `reconcile_on_startup()`

```python
# recording_manager.py

import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def reconcile_on_startup(data_dir: Path | None = None) -> None:
    """Reconcile recordings.json against on-disk segment files.

    Called synchronously during wisper server lifespan startup, before yield.
    Also deletes a stale data_dir/server.json if present from a previous crash.
    """
    from wisper_transcribe.config import get_data_dir as _get_data_dir

    base = Path(data_dir) if data_dir else _get_data_dir()
    recordings_dir = base / "recordings"
    server_json = base / "server.json"

    # Step 0: remove stale server.json
    if server_json.exists():
        logger.debug("Removing stale server.json from previous crash")
        server_json.unlink(missing_ok=True)

    # Step 1: ensure recordings directory + index exist
    if not recordings_dir.exists():
        recordings_dir.mkdir(parents=True, exist_ok=True)
        _save_recordings_index({}, base)
        logger.info("Recovery: initialized empty recordings directory")
        return

    # Step 2: load recordings.json
    try:
        recordings = _load_recordings_index(base)
    except (json.JSONDecodeError, OSError) as exc:
        recordings_json_path = recordings_dir / "recordings.json"
        bak = recordings_json_path.with_suffix(".json.bak")
        logger.error(
            "Recovery: recordings.json corrupt (%s) — renaming to %s, starting fresh",
            exc, bak.name
        )
        shutil.copy2(recordings_json_path, bak)
        recordings_json_path.unlink(missing_ok=True)
        _save_recordings_index({}, base)
        return

    # Step 3: reconcile sessions whose status implies they were active at crash
    recovered_ok = 0
    recovered_failed = 0

    for recording_id, recording in list(recordings.items()):
        if recording.status not in {"recording", "degraded"}:
            continue  # already in a terminal state — nothing to do

        # Cross-check each finalized segment in the manifest against disk
        all_segments_ok = True
        for seg in recording.segment_manifest:
            seg_path = Path(seg.path)
            if not seg_path.exists():
                all_segments_ok = False
                logger.debug(
                    "Recovery: recording %s segment %s missing on disk",
                    recording_id, seg.index
                )
                break
            if seg.finalized and not _ogg_has_eos(seg_path):
                # Manifest says finalized but EOS page is absent — truncated write
                seg.finalized = False
                all_segments_ok = False
                logger.debug(
                    "Recovery: recording %s segment %s missing EOS page (truncated)",
                    recording_id, seg.index
                )
                break
            if not seg.finalized:
                # Last entry was still open when server crashed
                all_segments_ok = False
                break

        if all_segments_ok and recording.segment_manifest:
            # All segments intact → session ended cleanly without a status update
            ended_at = max(
                seg.started_at + timedelta(seconds=seg.duration_s)
                for seg in recording.segment_manifest
                if seg.finalized
            )
            recording.status = "completed"
            recording.ended_at = ended_at
            recordings[recording_id] = recording
            recovered_ok += 1
            logger.debug(
                "Recovery: recording %s → completed (all segments intact)", recording_id
            )
        else:
            # Orphaned or truncated segment — mark failed; existing segments are preserved
            # and can still be transcribed from the combined track if it was finalized.
            recording.status = "failed"
            recording.ended_at = datetime.now(timezone.utc)
            # notes field carries the reason for UI display
            recording.notes = (
                (recording.notes or "") +
                " [crash_recovery_orphaned_segment]"
            ).strip()
            recordings[recording_id] = recording
            recovered_failed += 1
            logger.debug(
                "Recovery: recording %s → failed (orphaned segment)", recording_id
            )

    _save_recordings_index(recordings, base)

    # Step 4: startup summary
    total = recovered_ok + recovered_failed
    if total == 0:
        logger.info("Recovery: 0 active sessions found — clean startup")
    else:
        logger.info(
            "Recovery: %d dangling session(s) — %d marked completed, %d marked failed",
            total, recovered_ok, recovered_failed
        )
```

### Migration on first run

- If `data_dir/recordings/` does not exist: `reconcile_on_startup()` creates it and writes an empty `recordings.json`.
- If `campaigns.json` members lack `discord_user_id`: `CampaignMember` gains `discord_user_id: Optional[str] = None` as a dataclass field default. `load_campaigns()` ([campaign_manager.py:77–102](src/wisper_transcribe/campaign_manager.py#L77-L102)) uses `.get()` on every field already — old JSON that omits the key silently defaults to `None`. No migration pass needed.

### Test plan for `test_recording_manager.py`

| Test name | Scenario | Expected outcome |
|-----------|----------|-----------------|
| `test_reconcile_clean_startup` | `recordings.json` contains only `"completed"` and `"transcribed"` entries | `reconcile_on_startup()` makes no changes; log says "0 active sessions found — clean startup" |
| `test_reconcile_dangling_completed` | One recording with `status="recording"`, all segment files on disk with valid EOS pages | Status transitions to `"completed"`, `ended_at` set to `max(started_at + duration_s)`, `recovered_ok == 1` in INFO log |
| `test_reconcile_dangling_failed_orphaned_segment` | One recording with `status="recording"`, last segment file exists but lacks EOS page (file opened and closed without writing the EOS marker) | Status transitions to `"failed"`, last segment `finalized=False`, `recording.notes` contains `"crash_recovery_orphaned_segment"` |
| `test_reconcile_missing_recordings_json` | `recordings/` directory absent entirely | Directory created, empty `recordings.json` written, function returns without error, log says "initialized empty recordings directory" |
| `test_reconcile_corrupt_recordings_json` | `recordings.json` contains `"{broken json"` | Original renamed to `.json.bak`, fresh empty `recordings.json` written, function returns without error, `logger.error` emitted with the exception detail |


---

## Discord Recording Bot — Module Skeletons (FOR-BUILDER Reference)

> Paste this appendix into `plan.md` under the "Active — Discord Recording Bot" section.

---

## File: `src/wisper_transcribe/models.py` (additions only)

Show existing import block for context, then the three new dataclasses to append at the end of the file. Do **not** replace existing content.

```python
# ── existing imports already present ────────────────────────────────────────
# from dataclasses import dataclass, field
# from pathlib import Path
# ── add to existing imports ──────────────────────────────────────────────────
from datetime import datetime
from typing import Literal, Optional

# ---------------------------------------------------------------------------
# Discord recording data model
# ---------------------------------------------------------------------------


@dataclass
class SegmentRecord:
    """One finalized audio segment in a per-user or mixed track.

    Invariant 2: once written this record must never be mutated — only appended
    to Recording.segment_manifest.  The builder must use append_segment() in
    recording_manager, never direct list mutation on a loaded Recording.
    """
    index: int                          # monotonic counter, resets per stream
    stream: str                         # "mixed" or a discord_user_id string
    started_at: datetime                # wall-clock time this segment opened
    duration_s: float                   # actual encoded duration (may be <60 s for last segment)
    path: Path                          # absolute path to the .opus file on disk
    finalized: bool                     # False while OGG EOS page not yet flushed; True after

    def dict(self) -> dict:
        """Serialize to a JSON-safe dict for metadata.json storage.

        Converts datetime to ISO-8601 string; Path to POSIX string.
        """
        ...

    @classmethod
    def from_dict(cls, data: dict) -> "SegmentRecord":
        """Deserialize from a metadata.json dict entry.

        Converts ISO-8601 string back to datetime; string back to Path.
        """
        ...


@dataclass
class RejoinAttempt:
    """One auto-rejoin attempt logged during a recording session.

    Appended to Recording.rejoin_log by the bot; never removed.
    Surfaced in the UI status badge and by `wisper record show`.
    """
    attempt_number: int                 # 1-based index within the session
    timestamp: datetime                 # UTC moment the attempt was initiated
    close_code: int                     # Discord close code that triggered this attempt (e.g. 4009)
    succeeded: bool                     # True if the rejoin handshake completed
    error_detail: str = ""              # human-readable error string on failure, "" on success

    def dict(self) -> dict:
        """Serialize to a JSON-safe dict."""
        ...

    @classmethod
    def from_dict(cls, data: dict) -> "RejoinAttempt":
        """Deserialize from a stored dict."""
        ...


@dataclass
class Recording:
    """Top-level recording session created for each bot invocation.

    Invariant 5: status must pass through "recording" before reaching any
    terminal state.  v2's live ticker watches for new segments only while
    status is in {"recording", "degraded"}.

    Storage: data_dir/recordings/<id>/metadata.json (full object) and
    data_dir/recordings/recordings.json (index with id + status + campaign_slug only).
    """
    id: str                             # uuid4 string — never user-supplied
    campaign_slug: Optional[str]        # campaign this recording belongs to (may be None)
    started_at: datetime                # UTC moment recording began
    ended_at: Optional[datetime]        # UTC moment recording stopped; None while active
    status: Literal[                    # see Invariant 5 and plan.md data model
        "recording", "degraded", "completed", "failed", "transcribing", "transcribed"
    ]
    voice_channel_id: str               # Discord snowflake string of the voice channel
    guild_id: str                       # Discord snowflake string of the guild (needed for Pycord resolution)
    discord_speakers: dict              # dict[discord_user_id: str, wisper_profile_key: str | ""]
    segment_manifest: list              # list[SegmentRecord] — append-only, see Invariant 2
    combined_path: Path                 # data_dir/recordings/<id>/final/combined.wav (written at stop)
    per_user_dir: Path                  # data_dir/recordings/<id>/per-user/  (parent dir)
    transcript_path: Optional[Path]     # set after JobQueue completes; None until then
    rejoin_log: list                    # list[RejoinAttempt] — append-only
    notes: Optional[str] = None        # free-text operator notes

    def dict(self) -> dict:
        """Serialize to a JSON-safe dict for metadata.json.

        Converts datetime fields to ISO-8601; Path fields to POSIX strings;
        nested SegmentRecord and RejoinAttempt objects via their own .dict().
        """
        ...

    @classmethod
    def from_dict(cls, data: dict) -> "Recording":
        """Deserialize from a metadata.json dict.

        Reconstructs nested SegmentRecord and RejoinAttempt lists.
        Raises ValueError if required fields are absent.
        """
        ...


# ── Existing CampaignMember gets one new optional field ─────────────────────
# In the EXISTING CampaignMember dataclass, add:
#
#   discord_user_id: Optional[str] = None   # Discord snowflake; bound via roster UI
#
# Also update CampaignMember.dict() / from_dict() if they exist in campaign_manager.py,
# or add JSON round-trip handling in load_campaigns / save_campaigns.
# TODO(builder): CampaignMember currently has no dict()/from_dict(); the serialization
# lives in campaign_manager.py — add discord_user_id there in save_campaigns / load_campaigns.
```

---

## File: `src/wisper_transcribe/recording_manager.py`

```python
"""Recording manager — CRUD for Discord recording sessions.

Mirrors campaign_manager.py exactly in structure and security posture.

Data lives at:
    $DATA_DIR/recordings/recordings.json     — lightweight index (id, status, campaign_slug)
    $DATA_DIR/recordings/<id>/metadata.json  — full Recording + segment manifest

Recording IDs are uuid4 strings.  _validate_recording_id() is the security
gatekeeper for any id arriving from a URL path parameter or form field.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import get_data_dir
from .models import Recording, SegmentRecord, RejoinAttempt


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_recordings_dir(data_dir: Optional[Path] = None) -> Path:
    """Return data_dir/recordings/, creating it if absent."""
    ...

def get_recordings_index_path(data_dir: Optional[Path] = None) -> Path:
    """Return data_dir/recordings/recordings.json."""
    ...

def get_recording_dir(recording_id: str, data_dir: Optional[Path] = None) -> Path:
    """Return data_dir/recordings/<recording_id>/, creating it and sub-dirs if absent.

    Creates the full tree:
        <id>/combined/
        <id>/per-user/
        <id>/final/
    on first call so the writer can open files immediately.

    # TODO(builder): decide whether to create per-user/<discord_id>/ here or
    # lazily in AudioPipeline.on_user_join().  Lazy creation is recommended to
    # avoid empty directories for users who never speak.
    """
    ...


# ---------------------------------------------------------------------------
# Security gatekeeper
# ---------------------------------------------------------------------------

def _validate_recording_id(recording_id: str) -> Optional[str]:
    """Two-layer security guard for recording IDs.  Mirror of _validate_campaign_slug.

    Recording IDs are uuid4 strings (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx).
    Returns the sanitised ID on success; None on rejection.

    Layer 1: strict regex — only alphanumeric + hyphen, rejects null bytes,
             slashes, CRLF, dots, and every other injection character.
    Layer 2: os.path round-trip — breaks CodeQL taint chain for redirect URLs.
             See CLAUDE.md security rules and _validate_campaign_slug for the
             exact pattern.
    """
    ...


# ---------------------------------------------------------------------------
# Load / save (index + full metadata)
# ---------------------------------------------------------------------------

def load_recordings(data_dir: Optional[Path] = None) -> dict[str, Recording]:
    """Load all recordings from their individual metadata.json files.

    The recordings.json index is used to enumerate IDs; each Recording is then
    read from <id>/metadata.json.  Returns {} when the index is absent.

    # TODO(builder): decide whether to load eagerly (all metadata at once) or
    # lazily (index only, full metadata on demand).  Eager is simpler and safe
    # for expected session counts (<1000).  Use eager for v1.
    """
    ...

def save_recording(recording: Recording, data_dir: Optional[Path] = None) -> None:
    """Persist a single Recording to <id>/metadata.json and update the index.

    Atomic write pattern: write to <id>/metadata.json.tmp, then os.replace().
    Also rewrites recordings.json with the current id/status/campaign_slug tuple.
    """
    ...

def _save_index(recordings: dict[str, Recording], data_dir: Optional[Path] = None) -> None:
    """Write the lightweight recordings.json index.  Called by save_recording.

    Index entry shape: {"id": ..., "status": ..., "campaign_slug": ...}
    """
    ...


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_recording(
    campaign_slug: Optional[str],
    voice_channel_id: str,
    guild_id: str,
    data_dir: Optional[Path] = None,
) -> Recording:
    """Create a new Recording entry, allocate the on-disk directory tree, and persist.

    Sets status="recording", started_at=datetime.utcnow(), generates uuid4 id.
    Raises ValueError if campaign_slug is provided but fails _validate_campaign_slug.

    # Depends on: get_recording_dir (creates directory tree)
    """
    ...

def get_recording(
    recording_id: str,
    data_dir: Optional[Path] = None,
) -> Optional[Recording]:
    """Return a Recording by id, or None if not found.

    Does NOT validate the id — callers coming from HTTP must pass the id
    through _validate_recording_id() first.
    """
    ...

def list_recordings(
    campaign_slug: Optional[str] = None,
    data_dir: Optional[Path] = None,
) -> list[Recording]:
    """Return all recordings, optionally filtered by campaign_slug.

    Sorted by started_at descending (newest first), mirroring JobQueue.list_all().
    """
    ...

def update_recording_status(
    recording_id: str,
    status: str,
    data_dir: Optional[Path] = None,
) -> Recording:
    """Set Recording.status and persist.  Returns the updated Recording.

    Raises KeyError if recording_id is not found.
    Raises ValueError if status is not one of the allowed literals.

    Invariant 5: callers must not skip "recording" state.  This function does
    NOT enforce the FSM; the caller (BotManager) is responsible for ordering.
    """
    ...

def append_segment(
    recording_id: str,
    segment: SegmentRecord,
    data_dir: Optional[Path] = None,
) -> None:
    """Atomically append a finalized SegmentRecord to Recording.segment_manifest.

    Invariant 2: append-only, never mutates existing entries.
    Uses read-modify-write with os.replace() on metadata.json.tmp to ensure
    that a crash mid-write does not corrupt the existing manifest.

    # TODO(builder): consider appending to a separate segments.jsonl sidecar
    # (one JSON line per segment) so the writer never rewrites the full
    # metadata.json on every segment rotation.  A .jsonl sidecar is
    # substantially cheaper for 240-segment sessions.  Either approach satisfies
    # Invariant 2 as long as the write is atomic.  Decide before Phase 1 commit.
    """
    ...

def log_rejoin(
    recording_id: str,
    attempt: RejoinAttempt,
    data_dir: Optional[Path] = None,
) -> None:
    """Append a RejoinAttempt to Recording.rejoin_log and persist.

    # Depends on: save_recording
    """
    ...

def delete_recording(
    recording_id: str,
    data_dir: Optional[Path] = None,
) -> None:
    """Remove all files for a recording and delete its index entry.

    Safety: only removes data_dir/recordings/<id>/ — uses os.path.abspath +
    startswith guard to ensure the target path is inside recordings_dir before
    calling shutil.rmtree().  Raises KeyError if recording_id not found.
    Raises RuntimeError if status is "recording" or "degraded" (active session).

    # TODO(builder): add a force=True kwarg to allow deletion of active sessions
    # if the operator explicitly requests it (e.g. after a crash where status
    # was never updated to "failed").
    """
    ...


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------

def reconcile_on_startup(data_dir: Optional[Path] = None) -> dict:
    """Scan disk for partially-written recordings left by a previous crash.

    Called once from BotManager.start() before the Discord client connects.
    Returns a summary dict: {"recovered": int, "abandoned": int, "errors": list[str]}.

    Recovery logic (implement in full during Phase 1):
    1. Load the recordings index.
    2. For any Recording with status in {"recording", "degraded"}:
       a. Count .opus files present in combined/ and per-user/<id>/ dirs.
       b. Cross-check against segment_manifest (finalized=True entries).
       c. Any .opus file not in the manifest that has size > 0 is a partial
          segment — add it with finalized=False and log a warning.
       d. Set status="failed" and ended_at=datetime.utcnow() (session cannot
          be resumed; partial segments are preserved for manual recovery).
    3. Return the summary dict for BotManager to log on startup.

    # TODO(builder): this is the full spec; only the signature is required here.
    # Full implementation belongs in Phase 1 (Storage layer).
    """
    ...
```

---

## File: `src/wisper_transcribe/web/discord_bot.py`

```python
"""Discord BotManager — lifecycle wrapper for the Pycord client.

Mirrors JobQueue (web/jobs.py) exactly:
- BotManager.start() called from FastAPI lifespan after job_queue.start()
- BotManager.stop() called from FastAPI lifespan before job_queue.stop()
- Stored on app.state.bot_manager; routes retrieve via get_bot_manager(request)
- Internal asyncio task (_connect_loop) owns the discord.Client connection

Per locked decision: bot is per-session (started at wisper server startup,
inactive until start_recording() is called) NOT always-on.

Per locked decision: Discord library is Pycord (py-cord).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# NOTE: discord import is deferred inside methods so that the module loads
# without py-cord installed (allows non-bot usage of wisper server).
# TODO(builder): guard all discord imports with try/except ImportError and
# raise a clear "py-cord is required for Discord recording" message.


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass
class VoiceChannelInfo:
    """A single voice channel within a guild, for the channel-picker UI."""
    id: str                 # Discord snowflake string
    name: str               # display name
    member_count: int       # current occupants (from gateway cache)


@dataclass
class GuildSummary:
    """A guild the bot belongs to, with its available voice channels."""
    guild_id: str           # Discord snowflake string
    guild_name: str         # display name
    voice_channels: list    # list[VoiceChannelInfo]


# ---------------------------------------------------------------------------
# BotManager
# ---------------------------------------------------------------------------

class BotManager:
    """Manages the Pycord Discord client lifecycle inside the FastAPI server process.

    One BotManager per server process.  Mirrors JobQueue: created in create_app(),
    started in lifespan(), stopped in lifespan() cleanup arm.

    The Discord client runs in a long-lived asyncio task (_connect_loop).
    Recording sessions are started/stopped via start_recording / stop_recording;
    these are safe to call from FastAPI route handlers on the same event loop.

    # Per locked decision (FastAPI integration): stored on app.state.bot_manager
    """

    def __init__(self) -> None:
        self._client: Optional[object] = None          # discord.Client, typed as object to avoid hard import
        self._connect_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._token: Optional[str] = None
        self._active_recording_id: Optional[str] = None
        self._voice_client: Optional[object] = None    # discord.VoiceClient

    # ------------------------------------------------------------------
    # Lifecycle (mirrors JobQueue.start / stop)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background _connect_loop task.

        Does NOT connect to Discord immediately — connection happens lazily
        when start_recording() is called with a valid token.  Called from
        FastAPI lifespan startup.

        # Depends on: recording_manager.reconcile_on_startup (call here,
        # log result via tqdm.write or logging)
        """
        ...

    async def stop(self) -> None:
        """Disconnect from Discord and cancel the _connect_loop task.

        If a recording is active, marks it status="failed" before disconnecting.
        Called from FastAPI lifespan shutdown (before job_queue.stop()).

        # Depends on: recording_manager.update_recording_status
        """
        ...

    def is_ready(self) -> bool:
        """True when the Discord client is connected and the guild cache is populated."""
        ...

    @property
    def status(self) -> str:
        """Human-readable status string for the UI status badge.

        Returns one of: "disconnected", "connecting", "ready", "recording", "degraded".
        """
        ...

    # ------------------------------------------------------------------
    # Token resolution
    # ------------------------------------------------------------------

    def _load_token(self) -> Optional[str]:
        """Resolve the Discord bot token.

        Priority: DISCORD_BOT_TOKEN env var → config.toml discord_bot_token key.
        Returns None (not raises) when not configured so callers can return 503.

        # Per locked decision (Discord token): same resolution pattern as HF_TOKEN.
        # See config.get_hf_token() for the exact pattern to mirror.
        """
        ...

    # ------------------------------------------------------------------
    # Recording control
    # ------------------------------------------------------------------

    async def start_recording(
        self,
        recording_id: str,
        guild_id: str,
        channel_id: str,
    ) -> bool:
        """Join the specified voice channel and begin writing audio to disk.

        Returns True on success, False if already recording or connection fails.
        Creates and starts an AudioPipeline for the session.

        Steps:
        1. Resolve token via _load_token(); return False if None.
        2. Ensure client is connected (may trigger _connect_loop wake-up).
        3. Fetch guild + channel from gateway cache (no REST call needed).
        4. await guild.voice_channels[channel_id].connect()
        5. Attach a DiscordSink to the VoiceClient.start_recording().
        6. Set self._active_recording_id = recording_id.

        # Per locked decision: gateway intents = guilds + voice_states (non-privileged)
        # Depends on: recording_manager.update_recording_status (set "recording")
        # Depends on: AudioPipeline (web/audio_writer.py)
        """
        ...

    async def stop_recording(self, recording_id: str) -> Optional[object]:
        """Stop recording, finalize all segments, and return the updated Recording.

        Steps:
        1. Call VoiceClient.stop_recording() — flushes any partial segment.
        2. Await AudioPipeline.finalize() — closes all SegmentedOggWriters.
        3. Produce final/combined.wav via ffmpeg concat of combined/*.opus.
        4. Update Recording.status = "completed", ended_at = now.
        5. Disconnect VoiceClient.
        6. Return the updated Recording.

        Returns None if recording_id does not match the active session.

        # Depends on: recording_manager.update_recording_status
        # Depends on: AudioPipeline.finalize()
        # TODO(builder): ffmpeg concat command is:
        #   ffmpeg -f concat -safe 0 -i filelist.txt -c copy combined.wav
        #   where filelist.txt lists each segment in order.  Produce 16kHz mono WAV.
        """
        ...

    # ------------------------------------------------------------------
    # Channel discovery
    # ------------------------------------------------------------------

    def list_channels(self) -> list[GuildSummary]:
        """Return guild + voice-channel summaries from the gateway cache.

        Returns [] (not raises) when not ready.
        Called by GET /api/record/channels.

        # Per resolved item 7: bot.guilds and guild.voice_channels are populated
        # from the gateway cache — no REST calls needed.
        # Required intents: guilds + voice_states (both non-privileged).
        """
        ...

    # ------------------------------------------------------------------
    # Discord event handlers (registered on the discord.Client)
    # ------------------------------------------------------------------

    async def on_voice_state_update(
        self,
        member: object,
        before: object,
        after: object,
    ) -> None:
        """Handle user join/leave events during recording.

        On user join: notify AudioPipeline.on_user_join(discord_user_id).
        On user leave: notify AudioPipeline.on_user_leave(discord_user_id).
        Ignore if no active recording.

        # Depends on: AudioPipeline.on_user_join / on_user_leave
        """
        ...

    async def on_disconnect(self) -> None:
        """Handle unexpected Discord disconnect during a recording session.

        Triggers _auto_rejoin_loop() if _active_recording_id is set.
        Sets Recording.status = "degraded" immediately so the UI updates.

        # Per locked decision (auto-rejoin): 5 retries, backoff [2,5,15,30,60]s
        # Depends on: recording_manager.update_recording_status
        """
        ...

    # ------------------------------------------------------------------
    # Internal asyncio tasks
    # ------------------------------------------------------------------

    async def _connect_loop(self) -> None:
        """Long-lived task that owns the discord.Client connection.

        Runs until BotManager.stop() cancels it.
        Starts the client only when a token is available.
        On unexpected disconnect (not triggered by stop()), logs and waits
        before allowing reconnect via the auto-rejoin path.

        # TODO(builder): use asyncio.Event to wake _connect_loop when
        # start_recording() is called and a token becomes available for
        # the first time.  Do not poll in a busy loop.
        """
        ...

    async def _auto_rejoin_loop(self, recording_id: str) -> None:
        """Attempt to rejoin the voice channel after a disconnect.

        Per locked decision (auto-rejoin policy):
        - Backoff schedule: [2, 5, 15, 30, 60] seconds (5 attempts max).
        - Transient close codes: 4009, 4015, TimeoutError, socket reset.
        - Permanent close codes: 4014, 4011, 4022, 4017.
        - On 4006: retry once with fresh connect, then give up.
        - Call await vc.disconnect(force=True) before each retry to avoid
          discord.py #10207 (ghost connection left open).
        - Log each attempt via recording_manager.log_rejoin().
        - On max retries exhausted: set Recording.status = "degraded".

        # Depends on: recording_manager.log_rejoin
        # Depends on: recording_manager.update_recording_status
        """
        ...


# ---------------------------------------------------------------------------
# Pycord audio sink
# ---------------------------------------------------------------------------

class DiscordSink:
    """Pycord AudioSink subclass that fans out per-user Opus packets to AudioPipeline.

    Pycord calls write(user, data) for each Opus packet received from the gateway.
    This sink delegates immediately to AudioPipeline.write_packet() — no buffering
    here so back-pressure stays in the pipeline.

    # TODO(builder): subclass discord.sinks.Sink (Pycord's base class).
    # discord.sinks.Sink.write(user, data) receives an OpusDecoder output;
    # check whether Pycord gives raw Opus packets or decoded PCM — this affects
    # whether AudioPipeline.write_packet() expects Opus or PCM.
    # As of Pycord 2.7, sinks receive decoded PCM (48 kHz stereo s16le).
    """

    def __init__(self, pipeline: "AudioPipeline") -> None:
        """
        Args:
            pipeline: the AudioPipeline orchestrator for this session.
        """
        ...

    def write(self, user: object, data: object) -> None:
        """Called by Pycord for each decoded audio frame.

        Passes (discord_user_id, pcm_bytes) to pipeline.write_packet().
        Must not block — runs on the event loop.

        # Depends on: AudioPipeline.write_packet
        """
        ...

    def cleanup(self) -> None:
        """Called by Pycord when stop_recording() is invoked.

        Triggers AudioPipeline.on_sink_closed() to flush partial segments.

        # Depends on: AudioPipeline.on_sink_closed
        """
        ...


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

def get_bot_manager(request: object) -> BotManager:
    """FastAPI dependency — retrieve BotManager from app.state.

    Usage in route handlers:
        manager: BotManager = Depends(get_bot_manager)

    Mirrors get_queue() in web/routes/__init__.py.
    """
    ...
```

---

## File: `src/wisper_transcribe/web/audio_writer.py`

```python
"""Per-user and mixed segmented Opus-in-Ogg audio writers.

Three classes with a strict ownership hierarchy:
    AudioPipeline (orchestrator)
        owns one SegmentedOggWriter per active Discord user
        owns one SegmentedOggWriter for the mixed track
        owns one RealtimeMixer

File-format invariants (ALL non-negotiable — see plan.md):
    1. Each segment is a self-contained Ogg/Opus bitstream (not a raw packet dump).
    2. Segment manifest entries are append-only and atomic.
    3. Segment length: 60 s nominal (last segment may be shorter).
    4. Per-user track layout: data_dir/recordings/<id>/per-user/<discord_id>/NNNN.opus
    5. Recording.status includes "recording" as a distinct active state.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

# TODO(builder): choose the Ogg muxer library.
# Recommended: pyogg or opuslib for Opus encoding + manual Ogg page assembly.
# Alternative: subprocess ffmpeg with -f ogg -c:a copy piped per segment.
# The ffmpeg pipe approach is simpler but adds one process per user per segment.
# pyogg is the lowest-dependency pure-Python path; verify it handles arbitrary
# packet boundaries and emits the EOS page correctly on close().


class SegmentedOggWriter:
    """Opens a sequence of self-contained Ogg/Opus segment files.

    Each file is a complete Ogg bitstream (BOS page → audio pages → EOS page).
    On rotation (wall-clock 60 s elapsed), the current file is finalized (EOS
    page written) and a new file is opened.  The caller appends a SegmentRecord
    via the provided callback after finalization.

    Thread-safe: write() and rotate() may be called from the audio thread;
    finalize() is called from the main thread at session stop.

    # Per locked decision (segment length): 60 s — OPUS_SET_DTX(1) keeps
    # silent periods at ~400 bit/s so the 60 s files stay small.
    # Per invariant 1: each file must be independently decodable.
    """

    SEGMENT_DURATION_S: int = 60  # Per locked decision: 60 s per segment

    def __init__(
        self,
        output_dir: Path,
        stream_id: str,
        on_segment_finalized: "callable",  # Callable[[SegmentRecord], None]
        sample_rate: int = 48000,
        channels: int = 2,
    ) -> None:
        """
        Args:
            output_dir: directory where NNNN.opus files are written.
            stream_id: "mixed" or a discord_user_id (used in SegmentRecord.stream).
            on_segment_finalized: callback invoked with a SegmentRecord after each
                                  segment file is fully closed.
            sample_rate: Opus sample rate (Discord delivers 48 kHz).
            channels: 2 for per-user (stereo Discord output); 1 for mixed track.
        """
        ...

    def write(self, pcm_bytes: bytes) -> None:
        """Append a decoded PCM frame to the current segment.

        Encodes via libopus, writes the resulting Ogg page, checks wall-clock
        elapsed time and calls _rotate() if >= SEGMENT_DURATION_S.

        Thread-safe: called from the audio receive thread.

        # TODO(builder): DTX (OPUS_SET_DTX=1) must be enabled on the encoder
        # instance — silence drops to ~400 bit/s comfort noise per resolved item 8.
        """
        ...

    def _rotate(self) -> None:
        """Close the current segment file and open the next one.

        1. Flush encoder, write EOS Ogg page to current file, close file.
        2. Invoke on_segment_finalized callback with a SegmentRecord(finalized=True).
        3. Increment segment index, open new file, write BOS Ogg page.
        """
        ...

    def finalize(self) -> Optional[object]:
        """Close the current (possibly partial) segment and return its SegmentRecord.

        Called once at session stop.  Returns None if no data was written.
        Marks the returned SegmentRecord finalized=True.
        """
        ...


class RealtimeMixer:
    """Accepts per-user PCM frames, mixes them, and drives a SegmentedOggWriter.

    Mixing strategy: per locked decision (resolved item 4), real-time PCM mixing
    (~2% of one CPU core for 6 users).  Mixing is sample-wise addition with
    int16 clipping (min/max at -32768/32767) — no floating-point intermediate
    needed for this use case.

    # Per locked decision: mixed track is the input to JobQueue at transcription time.
    """

    def __init__(self, writer: SegmentedOggWriter) -> None:
        """
        Args:
            writer: the SegmentedOggWriter for the mixed combined track.
        """
        ...

    def mix_frame(self, user_frames: dict[str, bytes]) -> None:
        """Accept a mapping of discord_user_id -> PCM frame and write a mixed frame.

        All frames must be the same length (standard Opus frame = 20 ms @ 48 kHz
        = 1920 samples × 2 channels × 2 bytes = 7680 bytes).  Missing users
        contribute silence.  Result is written to self._writer.

        # TODO(builder): decide on frame alignment strategy.  Discord does not
        # guarantee that all users' packets arrive in lock-step.  A small jitter
        # buffer (2–3 frame look-ahead) may be needed to avoid mixing stale frames.
        # For v1, mixing whatever arrived in the last 20 ms window is acceptable.
        """
        ...

    def finalize(self) -> None:
        """Flush the mixer and finalize the underlying SegmentedOggWriter."""
        ...


class AudioPipeline:
    """Orchestrator: owns one SegmentedOggWriter per active Discord user + one for mixed.

    Created by BotManager.start_recording(); destroyed after BotManager.stop_recording()
    calls finalize().

    Handles users joining mid-session (new SegmentedOggWriter opened on demand)
    and users leaving (writer kept open — they may return; segment manifest
    records the gap implicitly via DTX silence).

    # Per locked decision: directory layout is a versioned contract (Invariant 4).
    # Per-user dirs: data_dir/recordings/<id>/per-user/<discord_id>/
    # Mixed dir:     data_dir/recordings/<id>/combined/
    """

    def __init__(
        self,
        recording_id: str,
        recording_dir: Path,
        on_segment_finalized: "callable",  # Callable[[SegmentRecord], None]
    ) -> None:
        """
        Args:
            recording_id: used for logging and SegmentRecord attribution.
            recording_dir: data_dir/recordings/<recording_id>/ (already created).
            on_segment_finalized: forwarded to each SegmentedOggWriter; should call
                                  recording_manager.append_segment() atomically.
        """
        ...

    def write_packet(self, discord_user_id: str, pcm_bytes: bytes) -> None:
        """Route an incoming PCM frame to the correct per-user writer and the mixer.

        Called from DiscordSink.write() on the event loop — must not block.
        Creates a new SegmentedOggWriter for discord_user_id on first call.

        # Depends on: SegmentedOggWriter (per-user)
        # Depends on: RealtimeMixer.mix_frame
        """
        ...

    def on_user_join(self, discord_user_id: str) -> None:
        """Called when a user joins the voice channel mid-session.

        Creates per-user/<discord_id>/ directory and opens a SegmentedOggWriter.
        No-op if writer already exists (handles duplicate events gracefully).
        """
        ...

    def on_user_leave(self, discord_user_id: str) -> None:
        """Called when a user leaves the voice channel mid-session.

        Keeps the writer open — user may return; DTX silence fills the gap.
        Logs the leave event for the UI but does NOT close or rotate the writer.

        # TODO(builder): decide whether to rotate on leave so the departure is
        # cleanly segment-bounded.  This simplifies v2 live-ticker logic.
        """
        ...

    def on_sink_closed(self) -> None:
        """Called by DiscordSink.cleanup() when Pycord stops recording.

        Signals finalize() to run if not already called.
        """
        ...

    async def finalize(self) -> None:
        """Close all writers, flush all buffers, and wait for all segments to land.

        Called from BotManager.stop_recording().  After this returns, all
        SegmentRecord callbacks have fired and metadata.json is up to date.

        # Depends on: SegmentedOggWriter.finalize (per writer)
        # Depends on: RealtimeMixer.finalize
        """
        ...
```

---

## File: `src/wisper_transcribe/web/routes/record.py`

```python
"""Record routes — Discord bot control and recording management API.

Route table (agreed with Agent C; do not rename without updating BotManager and CLI):

    GET  /api/record/channels          → list guilds + voice channels
    GET  /api/record/recordings        → list recordings (grouped by campaign)
    GET  /api/record/recordings/{id}   → recording detail
    POST /api/record/start             → start a new recording session
    POST /api/record/stop              → stop the active recording session
    GET  /api/record/status            → current bot + active session status
    DELETE /api/record/recordings/{id} → delete recording files + entry
    POST /api/record/recordings/{id}/transcribe → submit to JobQueue
    POST /api/record/recordings/{id}/bind       → bind Discord user to wisper profile
    GET  /api/record/recordings/{id}/stream     → SSE status stream

Security: all {id} path parameters pass through _validate_recording_id() before
any filesystem or database access.  Per CLAUDE.md: never reflect user input into
error messages or redirect URLs.

Auth: None for v1 — matches existing wisper server posture.
Per locked decision (auth): project-wide auth is a separate backlog item.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Annotated, AsyncGenerator, Optional

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..discord_bot import BotManager, get_bot_manager
from ..jobs import JobQueue
from . import get_queue, templates
from wisper_transcribe import recording_manager
from wisper_transcribe.recording_manager import _validate_recording_id
from wisper_transcribe.models import Recording

router = APIRouter(prefix="/api/record")


# ---------------------------------------------------------------------------
# Channel discovery
# ---------------------------------------------------------------------------

@router.get("/channels")
async def list_channels(
    manager: Annotated[BotManager, Depends(get_bot_manager)],
) -> JSONResponse:
    """Return guild + voice-channel summaries from the bot's gateway cache.

    Returns 503 when the bot is not connected.
    Response shape: [{"guild_id": ..., "guild_name": ...,
                      "voice_channels": [{"id": ..., "name": ..., "member_count": ...}]}]

    # Depends on: BotManager.list_channels
    """
    ...


# ---------------------------------------------------------------------------
# Recording list and detail
# ---------------------------------------------------------------------------

@router.get("/recordings")
async def list_recordings(
    campaign: Optional[str] = None,
) -> JSONResponse:
    """Return all recordings, optionally filtered by campaign slug.

    Response shape: [Recording.dict(), ...]  sorted by started_at descending.

    # Depends on: recording_manager.list_recordings
    """
    ...


@router.get("/recordings/{recording_id}")
async def get_recording(recording_id: str) -> JSONResponse:
    """Return a single Recording by id.

    Returns 404 if not found; 400 if recording_id fails validation.

    # Security: _validate_recording_id() before any lookup.
    # Depends on: recording_manager.get_recording
    """
    ...


# ---------------------------------------------------------------------------
# Session control
# ---------------------------------------------------------------------------

@router.post("/start")
async def start_recording(
    request: Request,
    manager: Annotated[BotManager, Depends(get_bot_manager)],
    campaign_slug: Annotated[Optional[str], Form()] = None,
    guild_id: Annotated[str, Form()] = ...,
    channel_id: Annotated[str, Form()] = ...,
) -> JSONResponse:
    """Create a new Recording entry and direct the bot to join the voice channel.

    Returns {"recording_id": "<uuid>", "status": "recording"} on success.
    Returns 409 if a recording is already active.
    Returns 503 if the bot is not connected.

    # Depends on: recording_manager.create_recording
    # Depends on: BotManager.start_recording
    # Per locked decision: transcription hand-off uses a copy of combined.wav
    """
    ...


@router.post("/stop")
async def stop_recording(
    manager: Annotated[BotManager, Depends(get_bot_manager)],
    queue: Annotated[JobQueue, Depends(get_queue)],
) -> JSONResponse:
    """Stop the active recording session and return the finalized Recording.

    Returns 404 if no active session.
    Auto-queues transcription when auto_transcribe=true in the request body
    (optional — default false for v1).

    # Depends on: BotManager.stop_recording
    # Depends on: recording_manager.update_recording_status
    """
    ...


@router.get("/status")
async def get_status(
    manager: Annotated[BotManager, Depends(get_bot_manager)],
) -> JSONResponse:
    """Return current bot status and active recording summary.

    Response shape:
        {
          "bot_status": "disconnected"|"connecting"|"ready"|"recording"|"degraded",
          "active_recording_id": "<uuid>" | null,
          "active_recording_status": "<status>" | null,
          "segment_count": <int> | null
        }

    # Depends on: BotManager.status, BotManager.is_ready
    # Depends on: recording_manager.get_recording
    """
    ...


# ---------------------------------------------------------------------------
# Recording lifecycle
# ---------------------------------------------------------------------------

@router.delete("/recordings/{recording_id}")
async def delete_recording(
    recording_id: str,
    manager: Annotated[BotManager, Depends(get_bot_manager)],
) -> JSONResponse:
    """Delete all files and the index entry for a recording.

    Returns 400 if recording_id is invalid; 404 if not found;
    409 if the recording is the active session (must stop first).

    # Security: _validate_recording_id() before any filesystem access.
    # Depends on: recording_manager.delete_recording
    """
    ...


@router.post("/recordings/{recording_id}/transcribe")
async def transcribe_recording(
    recording_id: str,
    queue: Annotated[JobQueue, Depends(get_queue)],
) -> JSONResponse:
    """Submit the recording's combined.wav to the existing JobQueue.

    Per locked decision (transcription hand-off):
    1. Copy combined.wav to output/<recording_id>.wav.
    2. queue.submit(copy_path, original_stem=recording_id,
                    output_dir=_default_output_dir(),
                    campaign=recording.campaign_slug).
    3. Set Recording.status = "transcribing".
    4. Return {"job_id": job.id}.

    Returns 400 if invalid id; 404 if not found; 409 if status != "completed".

    # Depends on: recording_manager.get_recording, update_recording_status
    # Depends on: JobQueue.submit
    # Per locked decision (JobQueue compat / resolved item 12): use copy, not move
    """
    ...


@router.post("/recordings/{recording_id}/bind")
async def bind_discord_user(
    recording_id: str,
    discord_user_id: Annotated[str, Form()],
    profile_key: Annotated[str, Form()],
) -> JSONResponse:
    """Bind a Discord user ID to a wisper speaker profile key for this recording.

    Updates Recording.discord_speakers[discord_user_id] = profile_key.
    Also persists to CampaignMember.discord_user_id if the recording has a campaign.

    Returns 400 if recording_id or profile_key fail validation; 404 if not found.

    # Security: validate both recording_id and profile_key before use.
    # Depends on: recording_manager.get_recording, save_recording
    # Depends on: campaign_manager.add_member (to persist discord_user_id on roster)
    # Per locked decision (enrollment Option A): Discord-ID binding on roster
    """
    ...


# ---------------------------------------------------------------------------
# SSE status stream
# ---------------------------------------------------------------------------

@router.get("/recordings/{recording_id}/stream")
async def sse_recording_status(
    recording_id: str,
    request: Request,
) -> StreamingResponse:
    """Server-Sent Events stream for real-time recording status updates.

    Emits a JSON event every ~2 s with the current Recording dict.
    Client disconnects when the browser closes the EventSource or the
    recording reaches a terminal state (completed/failed/transcribed).

    # TODO(builder): implement using asyncio.sleep(2) in a generator loop.
    # Mirror the SSE pattern in web/routes/transcribe.py (job log stream).
    # Depends on: recording_manager.get_recording
    """
    ...
```

---

## File: `src/wisper_transcribe/cli.py` (additions only)

```python
# ---------------------------------------------------------------------------
# Server discovery helpers (add near top of file, after imports)
# ---------------------------------------------------------------------------

import json as _json
import os as _os


def _server_url() -> str:
    """Return the base URL of the running wisper server.

    Resolution order:
    1. WISPER_SERVER_URL env var (override for non-standard bind addresses).
    2. data_dir/server.json written by the server on startup.

    Raises click.ClickException with a clear "server not running" message
    if neither source is available or the file's URL is unreachable.

    Per locked decision (CLI ↔ server IPC): HTTP on localhost;
    server.json is the discovery mechanism; WISPER_SERVER_URL overrides it.
    """
    import httpx

    if url := _os.environ.get("WISPER_SERVER_URL"):
        return url.rstrip("/")

    from .config import get_data_dir
    server_json = get_data_dir() / "server.json"
    if not server_json.exists():
        raise click.ClickException(
            "wisper server is not running — start it with `wisper server` and try again."
        )
    data = _json.loads(server_json.read_text(encoding="utf-8"))
    return data["url"].rstrip("/")


def _http_get(path: str) -> dict:
    """GET <server_url>/<path> and return the parsed JSON body.

    Raises click.ClickException on connection error or non-2xx response.
    """
    ...


def _http_post(path: str, data: Optional[dict] = None) -> dict:
    """POST <server_url>/<path> with optional form data and return the parsed JSON body.

    Raises click.ClickException on connection error or non-2xx response.
    """
    ...


def _http_delete(path: str) -> dict:
    """DELETE <server_url>/<path> and return the parsed JSON body.

    Raises click.ClickException on connection error or non-2xx response.
    """
    ...


# ---------------------------------------------------------------------------
# `wisper record` command group
# ---------------------------------------------------------------------------

@main.group()
def record():
    """Control Discord voice recording sessions."""
    ...


@record.command("start")
@click.option("--campaign", default=None, help="Campaign slug to associate this recording with")
@click.option("--voice-channel", "channel_id", required=True, help="Discord voice channel snowflake ID")
@click.option("--guild", "guild_id", default=None, help="Discord guild (server) snowflake ID")
def record_start(campaign: Optional[str], channel_id: str, guild_id: Optional[str]) -> None:
    """Start a new Discord recording session.

    Joins the specified voice channel and begins writing per-user and mixed audio.
    The recording is associated with <campaign> if provided.

    Guild ID is optional when the bot is in only one guild (discovered automatically
    via GET /api/record/channels).  Required when the bot is in multiple guilds.

    # Depends on: _http_post("/api/record/start")
    # Depends on: _http_get("/api/record/channels") if guild_id is None
    """
    ...


@record.command("stop")
def record_stop() -> None:
    """Stop the active recording session.

    Finalizes all audio segments and marks the recording as completed.
    The combined track is ready for transcription via `wisper record transcribe`.

    # Depends on: _http_post("/api/record/stop")
    """
    ...


@record.command("list")
@click.option("--campaign", default=None, help="Filter by campaign slug")
def record_list(campaign: Optional[str]) -> None:
    """List recordings, optionally filtered by campaign.

    Output format (one recording per line):
        <id>  <status>  <started_at>  [<campaign_slug>]

    # Depends on: _http_get("/api/record/recordings")
    """
    ...


@record.command("show")
@click.argument("recording_id")
def record_show(recording_id: str) -> None:
    """Show metadata, speakers, and file paths for a recording.

    Prints degraded/rejoin information when status is "degraded":
        [DEGRADED] reconnected 3x — last reason: close code 4015

    # Depends on: _http_get(f"/api/record/recordings/{recording_id}")
    """
    ...


@record.command("transcribe")
@click.argument("recording_id")
def record_transcribe(recording_id: str) -> None:
    """Submit a completed recording to the transcription queue.

    Equivalent to clicking the "Transcribe" button on the recording detail page.
    Prints the resulting job ID and a hint to watch progress in the web UI.

    # Depends on: _http_post(f"/api/record/recordings/{recording_id}/transcribe")
    """
    ...


@record.command("delete")
@click.argument("recording_id")
@click.confirmation_option(prompt="Delete all files for this recording?")
def record_delete(recording_id: str) -> None:
    """Permanently delete a recording's files and index entry.

    Prompts for confirmation before deleting.
    Refuses if the recording is currently active (must stop first).

    # Depends on: _http_delete(f"/api/record/recordings/{recording_id}")
    """
    ...


# ---------------------------------------------------------------------------
# `wisper config discord` wizard
# ---------------------------------------------------------------------------

@main.group("config")
def config_group():
    """Manage wisper configuration."""
    ...
# NOTE: the existing @main.command("config") must be converted to a group
# or the discord subcommand must be added under the existing group.
# TODO(builder): check cli.py for existing `config` command vs group pattern.
# If `config` is currently a @main.command, refactor to @main.group("config")
# and add `discord` as a subcommand.  Keep existing `config show` etc. working.


@config_group.command("discord")
def config_discord() -> None:
    """Interactive wizard: configure the Discord bot token and defaults.

    Mirrors `config llm` in style (see cli.py around line 245).

    Steps:
    1. Prompt for bot token (hide_input=True); validate non-empty.
    2. Prompt for default guild ID (optional — can leave blank).
    3. Prompt for default voice channel ID (optional).
    4. Save to config.toml keys: discord_bot_token, discord_guild_id,
       discord_channel_id.
    5. Print a reminder that the token is masked in `wisper config show`.

    Per resolved item 10: full Discord app setup instructions belong in README.md,
    not in this wizard.  The wizard only handles token storage.

    # Depends on: config.load_config, config.save_config
    # Per locked decision (Discord token): same storage pattern as hf_token.
    """
    ...
```

---

### Cross-cutting notes for the builder

**Import guards.** `discord_bot.py` and `audio_writer.py` must defer all `import discord` and Opus-encoder imports inside methods or use `TYPE_CHECKING` guards. This keeps `wisper server` startable without py-cord installed (graceful "bot disabled" mode).

**`server.json` write timing.** Per locked decision (resolved item 5): `server.json` is written immediately before the lifespan `yield` in `app.py`, after both `job_queue.start()` and `bot_manager.start()` complete. Deleted in the post-`yield` cleanup arm. The `wisper server` CLI sets `WISPER_BIND=<host>:<port>` before `uvicorn.run()` so `create_app()` can read it without a signature change.

**`_default_output_dir()` reuse.** Per resolved item 12, the function in `transcribe.py` must be lifted to a shared location (e.g., `wisper_transcribe.web.routes._default_output_dir` re-exported, or moved to `config.py`) before `record.py` can call it. Do this as part of Phase 2.

**`CampaignMember.discord_user_id` serialization.** The existing `load_campaigns` / `save_campaigns` in `campaign_manager.py` handles all CampaignMember fields explicitly. Add `"discord_user_id": m.discord_user_id` to the save dict and `discord_user_id=mdata.get("discord_user_id")` to the load path. No migration needed — `json.get()` returns `None` for absent keys.

**Test files required** (Phase 8 per plan.md): `tests/test_recording_manager.py`, `tests/test_record_cli.py`, `tests/test_record_routes.py`, `tests/test_discord_bot.py` (mocked Pycord client + synthesized Opus stream — no live Discord in CI).