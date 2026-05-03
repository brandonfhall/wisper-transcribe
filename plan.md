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

A later phase will make transcription stream live (~30–60 s lag) while recording is in progress. **v1 is batch-only by default** (record → stop → transcribe via the existing job queue), but live transcription is **conditionally promoted to v1** if the research phase finds it cheap to add on top of the per-user-track architecture (see research item 13). If the cost is non-trivial, it stays deferred.

### V1 scope (proof of concept)

- Discord bot, joined per-session to one voice channel, controlled from wisper (no Discord-side commands).
- **Per-user audio capture** (each Discord speaker → its own track) plus a **mixed combined track** for archival / fallback transcription.
- **Crash-survivable on-disk format**: segmented files written continuously so that a crash mid-session loses at most one segment.
- New `Recording` data model + `recordings/` data dir; per-recording metadata indexes who spoke, when, and which Discord ID maps to which wisper profile.
- **Campaign integration**: a recording is created against a campaign; the bot only enrolls / matches against members of that campaign's roster.
- **Hand-off into existing pipeline**: when recording stops, the mixed track becomes the input to `process_file()` via the existing `JobQueue`. No parallel transcription path.
- **Auto-rejoin** on Discord disconnect (best-effort, with backoff).

### Out of scope for v1

- Live / streaming transcription with a running ticker — **conditional**: deferred *unless* research item 13 finds it inexpensive on top of per-user tracks, in which case promoted into v1.
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
| File format | Segmented Opus (length **(research)**), concatenated at session end |
| Transcription path | Existing `JobQueue.submit()` consumes the combined track; no parallel pipeline |
| Speaker enrollment | Discord-ID binding on roster (Option A) **+** auto-enroll on first hear (Option B). Voice-print fallback (Option C) deferred to v2. |
| Reconnect | Auto-rejoin on Discord disconnect with backoff. Mark session degraded but keep recording. |
| Discord token | Same handling as `HF_TOKEN`: env var (`DISCORD_BOT_TOKEN`) wins, then config, then web-UI input. Masked in `wisper config show`. |
| CLI parity | Surface every server-side action as a `wisper record` subcommand where it makes sense; UI is the primary entry point. |
| CLI ↔ server IPC | **HTTP on localhost.** CLI is a thin client of the same FastAPI routes the web UI uses (`POST /api/record/start`, etc.). Server writes its bind address to `data_dir/server.json` on startup; CLI reads that for discovery, with `WISPER_SERVER_URL` env var as override. If `server.json` is absent or the server is unreachable, the CLI errors out with a clear "wisper server is not running — start it with `wisper server` and try again" message. No auto-launch, no standalone-CLI bot path. |
| Auth on record routes | **None for v1** — match the existing posture (`0.0.0.0:8080`, no auth on any route). Adding auth project-wide (token or basic username/password covering *all* routes, not just record) is tracked in Backlog → `Web UI auth (project-wide)`. Recording is destructive enough that an audit-then-ship pass is appropriate, but doing it piecemeal on record-only routes would create a confusing two-tier security model. |

### Enrollment options

**A — Discord ID binding on the campaign roster.** `CampaignMember` gains an optional `discord_user_id` field. Roster page lets you bind a wisper profile to a Discord user. When that user speaks, their per-user track is tagged with their wisper profile name automatically.

**B — Auto-enroll on first hear.** When the bot hears an unknown Discord ID speaking, it queues a "new speaker" event. After recording, the new-speaker UI prompts for a name and extracts a voice embedding from that user's per-track audio. No interactive prompts during play.

**C (deferred to v2) — voice-print fallback.** Use existing wisper voice embeddings to match an unknown Discord ID to an enrolled profile via cosine similarity. Means a returning player who's already enrolled gets recognized without binding their Discord ID. Pure UX polish — A + B fully covers v1.

### Data model additions (sketch — refine in research)

```python
@dataclass
class Recording:
    id: str                       # uuid4 / slug
    campaign_slug: Optional[str]
    started_at: datetime
    ended_at: Optional[datetime]
    status: Literal["recording", "completed", "failed", "transcribing", "transcribed"]
    voice_channel_id: str
    discord_speakers: dict[str, str]   # discord_user_id → tagged wisper profile name (or "")
    combined_path: Path           # data_dir/recordings/<id>/combined.opus (or .wav after concat)
    per_user_dir: Path            # data_dir/recordings/<id>/per-user/
    transcript_path: Optional[Path]
    notes: Optional[str]


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

**Phase 0 — Library spike (explicitly throwaway).** 1–2 day timebox. Standalone `scripts/spike_voice_receive.py`: hardcoded bot token (env var), hardcoded guild + channel, joins, captures ~60 s, dumps per-user `.opus` files, exits. Acceptance: per-user files contain audible audio; chosen library is on PyPI with non-trivial commit activity in the last 12 months; install footprint reasonable; works on Windows + macOS hosts. Deliverable: a one-page library-choice memo appended to this plan (rationale, alternatives considered, platform caveats). Spike code is **not retained** — the production bot is written from scratch in phase 3 with the lessons learned. **If the spike fails on every viable library, replan: fall back to mixed-audio capture + diarization, drop the auto-enroll story, reshape phases 4 and 7.**

1. **Storage layer.** `Recording` dataclass, `recording_manager.py` (CRUD + index), segmented audio writer (format chosen during research), crash-recovery on startup (`recordings.json` reconciliation with on-disk segments). No Discord deps yet — this is pure local file management with tests.
2. **Server discovery + control plane.** `data_dir/server.json` written on `wisper server` startup; FastAPI route stubs at `/api/record/{start,stop,status,…}` returning 501 for now; CLI client (`wisper record …`) that reads `server.json`, hits the routes, and prints the "server not running" error cleanly. Lets us land the CLI↔server plumbing before there's anything to control.
3. **Bot core.** Chosen library integrated into `wisper server` FastAPI lifespan hook; start/stop primitives wired to the routes from phase 2; per-user + combined writer pipeline writing into the storage layer from phase 1; auto-rejoin with backoff.
4. **Campaign / Discord ID binding.** `CampaignMember.discord_user_id`, roster UI updates, auto-tagging during recording.
5. **Web UI.** Record control page, recordings list (campaign-grouped), recording detail page, integration with the existing transcripts page.
6. **Auto-enroll on first hear (Option B).** "Unknown speaker" queue surfaced on the recording detail page, embedding extraction from per-user tracks.
7. **Hand-off into JobQueue.** "Transcribe" button reuses `process_file()` via `submit()`. Recording → transcript association recorded so the existing transcripts page shows them.
8. **(Conditional) live ticker** — only if research item 13 finds it cheap. New SSE endpoint, ticker template, chunked-segment transcribe worker. If gate fails, this phase is dropped from v1 and tracked in Future.
9. **Tests + docs.** `test_recording_manager.py`, `test_record_cli.py`, `test_record_routes.py`. Bot integration tests use a fake voice gateway / synthesised Opus stream — no live Discord. Update `architecture.md` (new module, new pipeline branch, new config keys, new env var) and `README.md` (Discord setup walkthrough, new CLI/UI surface).
10. **Hardening.** Auto-rejoin behaviour, crash recovery walkthrough, secret handling audit, dependency footprint check.

### Phase commit cadence

Per CLAUDE.md / user workflow: commit at the end of each numbered phase, push the branch, and pause for user review before starting the next phase.

### Open research items (to be resolved before/while writing)

1. **Discord library.** Pycord vs disnake vs discord.py main + receive cogs vs raw aiortc/`discord-ext-voice-recv`. Which has stable per-user voice receive, asyncio integration, and an active maintainer? Verify on the Python versions our CI matrix targets (3.10–3.13 blocking, 3.14 non-blocking).
2. **Crash-survivable audio format.** Native Discord packets are Opus 48 kHz stereo (per user). Options: append-only `.ogg` per segment, raw Opus packets + sidecar metadata, segmented WAV (PCM) for simplicity. Which gives the cleanest recovery story and the lowest CPU cost?
3. **Segment length.** Tune for: max acceptable loss on crash (≤ 1 segment), cost of concatenation at stop, FS overhead of many small files. Likely 30–120 s. Validate.
4. **Mixing per-user → combined.** Real-time PCM mixing in Python (numpy add+clip) vs deferred `ffmpeg amix` at stop. CPU budget on the host machine while a 6-player session is recording.
5. **FastAPI lifespan integration.** How to spin the Pycord client up at server startup, expose start/stop control to routes and CLI, and shut down cleanly on Ctrl+C without dropping the recording.
6. **CLI ↔ server IPC.** When the user runs `wisper record stop` from a terminal but the bot is running inside `wisper server`, how does the command reach the bot? HTTP localhost endpoint with auth? Named pipe? Always-on local socket?
7. **Discord channel discovery.** Listing guilds / voice channels the bot has access to so the UI can show a picker rather than asking for raw IDs.
8. **Voice activity vs continuous recording.** Discord only sends audio frames for speaking users. Do we record pure silence in inactive periods (simpler timeline alignment) or rely on per-user timestamps to reconstruct gaps at concat time (smaller files, more bookkeeping)?
9. **Auto-rejoin policy.** Backoff schedule, max retries, behaviour when the channel is gone, how to surface a degraded session in the UI.
10. **Token / app setup UX.** Step-by-step in `wisper setup` (interactive) and `wisper config discord` (later config). Required scopes / intents / permissions to document.
11. **Per-user track size estimate.** A 4-hour session × 6 players × Opus 48 kHz mono ≈ ? Sanity check disk usage and plan for cleanup / archive policy.
12. **Existing job queue compatibility.** Does the existing `JobQueue` accept a `Path` to a pre-existing audio file from a non-upload source? Confirm by reading `web/jobs.py` and `pipeline.process_file()`.
13. **Live transcription cost analysis (gate for v1 promotion).** Quantify the incremental work to stream finalized per-user Opus segments through `faster-whisper` as they land on disk and surface a ticker on `/recordings/<id>/live`. Estimate in additional modules / lines / new dependencies. Acceptance bar for promoting into v1: re-uses the existing `JobQueue` worker model, adds at most one new module + one new SSE endpoint + one new template, no new ML dependency, and adds < ~1 day to phase 8 hand-off. If the answer is bigger than that — concurrency redesign, separate worker pool, model warm-up cost on every chunk, etc. — keep it in v2 and ensure the v1 file format does not preclude it.

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
