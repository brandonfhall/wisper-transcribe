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
