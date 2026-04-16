# Wisper-Transcribe: Backlog & Active Work

## Project Context

Podcast transcription tool for tabletop RPG actual-play recordings (D&D, Pathfinder, etc.) with 5–8 speakers (GM + players). Transcripts are fed into NotebookLM for querying game events and tracking stats.

**Hardware:** NVIDIA RTX 3090 (Windows), Apple M5 Mac. Both platforms supported.
**Processing:** Fully local — no cloud APIs. CLI + web UI.
**Stack:** faster-whisper + pyannote-audio. See [architecture.md](architecture.md) for full technical reference and [README.md](README.md) for user docs.

---

## In Progress

### Progress Bar Redesign (April 2026)

**Status:** Template rewritten. Tests pass (404). Needs: commit, architecture.md update, README update (if user-facing).

**What changed in `job_detail.html`:**
- Removed the dual T/D/F dot + separate progress bar layout
- Single unified progress bar spanning all steps; each step occupies an equal slice
- Steps shown: T → D → F for transcription; R for refine-only; S for summarize-only; T → D → F → R/S when post-processing is chained (only R/S shown if `job.post_refine`/`job.post_summarize`)
- Each step pill shows: pending (gray), active (indigo + pulse), done (green)
- ETA + speed/rate shown live from tqdm data; when no tqdm data for 5 s, a slow creep estimator advances the bar 1% per 5 s up to 90% of the current step slice
- Phase detection added for refine/summarize log keywords (`connecting to ollama`, `refining`, `summarizing`) to activate R/S steps

**Where to pick up:** Run `.venv/bin/pytest tests/ -v`, then commit `job_detail.html` + `tailwind.min.css` + `architecture.md` + `README.md` (no user-facing flag changes, but architecture.md needs the progress section updated). Then push.

**Known gap:** ETA for LLM steps is estimated only (no tqdm data from LLM providers). The 5 s creep estimator covers this — it will slowly advance the R/S bar until the `done` event fires.

---

## Backlog

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

### Multi-Show / Multi-Campaign Support

**Problem:** All speaker profiles live in a single flat global namespace. There is no concept of a "show," "campaign," or "podcast." The same person can be DM in one game and a player in another, but role metadata is stored once per person — not once per show. Every transcription matches against every enrolled profile with no scoping.

---

#### The Math: Does Separating Profiles Improve Matching Accuracy?

**No — but it's still worth doing for UX and role metadata.**

pyannote's 512-dim embeddings are person-specific voice fingerprints. Cosine similarity between two *different* people is typically **0.20–0.45**; the match threshold is **0.65**. This gap is wide enough that 30 profiles from multiple shows do **not** meaningfully increase false-positive risk — the threshold filters them out.

| Scenario | Global profiles | Full per-show isolation |
|---|---|---|
| 3 shows × 6 players = 18 profiles | No false positives (threshold handles it) | No accuracy improvement |
| Same person in 2 shows | ✅ One enrollment, recognized everywhere | ❌ Must re-enroll per show |
| Alice is DM in Game A, Player in Game B | Role metadata wrong (one global role) | ✅ Per-show role override |

**Verdict:** Full directory isolation wastes enrollment effort and breaks cross-show recognition. The right model is **shared voice embeddings + per-show rosters** with per-show role/character overrides.

---

#### Recommended Architecture: Shows with Rosters

**Data layout — additive, no migration required:**

```
$DATA_DIR/
├── config.toml
├── profiles/
│   ├── speakers.json          # unchanged — global, one entry per PERSON
│   └── embeddings/
│       └── alice.npy          # one .npy per person, forever
└── shows/
    └── shows.json             # new — all shows + rosters
```

**`shows.json` schema:**

```json
{
  "dnd-mondays": {
    "display_name": "D&D Mondays",
    "created": "2026-04-11",
    "members": {
      "alice": { "role": "DM",     "character": "" },
      "bob":   { "role": "Player", "character": "Thorin Oakenshield" }
    }
  },
  "pathfinder-fridays": {
    "display_name": "Pathfinder Fridays",
    "created": "2026-04-11",
    "members": {
      "alice": { "role": "Player", "character": "Kyra" },
      "carol": { "role": "DM",     "character": "" }
    }
  }
}
```

- `members` keys are `profile_key` values from `profiles/speakers.json`
- `role` and `character` are per-show overrides of the global `SpeakerProfile.role`
- A profile can belong to zero, one, or many shows
- Deleting a show does not delete the underlying profile

---

#### New Dataclasses (`models.py`)

```python
@dataclass
class ShowMember:
    profile_key: str       # FK → speakers.json key
    role: str              # per-show role override ("DM", "Player", etc.)
    character: str = ""    # optional character name for this show

@dataclass
class Show:
    slug: str              # URL/filesystem-safe key (e.g. "dnd-mondays")
    display_name: str      # human name
    created: str           # ISO date
    members: dict[str, ShowMember]  # keyed by profile_key
```

---

#### New Module: `show_manager.py`

```python
def get_shows_path(data_dir=None) -> Path
def load_shows(data_dir=None) -> dict[str, Show]
def save_shows(shows, data_dir=None) -> None
def create_show(slug, display_name, data_dir=None) -> Show
def delete_show(slug, data_dir=None) -> None
def add_member(slug, profile_key, role, character, data_dir=None) -> None
def remove_member(slug, profile_key, data_dir=None) -> None
def get_show_profile_keys(slug, data_dir=None) -> set[str]  # for match filtering
```

Slug generation: `re.sub(r'[^\w]+', '-', name.lower()).strip('-')` — same convention as existing profile keys.

---

#### Changes to Existing Code

**`speaker_manager.py` — `match_speakers()`**

Add `profile_filter: set[str] | None = None`. When set, only profiles in that set are candidates. `None` = global match (today's behavior, unchanged).

```python
def match_speakers(..., profile_filter: set[str] | None = None):
    profiles = load_profiles(data_dir)
    if profile_filter is not None:
        profiles = {k: v for k, v in profiles.items() if k in profile_filter}
    # rest unchanged
```

**`pipeline.py` — `process_file()`**

Add `show: str | None = None`. When set, resolves profile filter via `get_show_profile_keys(show)` before calling `match_speakers()`.

**`web/jobs.py`** — `show` flows through `queue.submit(show=show)` → `job.kwargs` → `process_file()`. No structural change to `JobQueue`.

---

#### CLI Changes (`cli.py`)

New command group:

```
wisper shows list
wisper shows create "D&D Mondays"           # slug auto-derived: dnd-mondays
wisper shows delete dnd-mondays
wisper shows add-member dnd-mondays alice --role DM --character ""
wisper shows remove-member dnd-mondays alice
wisper shows info dnd-mondays               # list members with roles
```

Modified commands (`--show` always optional — omitting preserves current behavior):

```
wisper transcribe session.mp3 --show dnd-mondays   # scope matching to roster
wisper speakers list --show dnd-mondays             # filter display by show
wisper speakers test audio.mp3 --show dnd-mondays   # scope test match
```

---

#### Web UI Changes

**New page `/shows`:** list shows, create/delete show, per-show detail with roster management (add/remove members, set role/character).

**Modified pages:**
- `/transcribe` form: optional "Show" dropdown (default: all speakers). Populated from `load_shows()`.
- `/speakers` list: optional filter by show (tab or dropdown).

**New routes (`web/routes/shows.py`):**

```
GET  /shows
POST /shows
GET  /shows/{slug}
POST /shows/{slug}/delete
POST /shows/{slug}/members
POST /shows/{slug}/members/{key}/remove
```

---

#### File Changelist

| File | Change |
|------|--------|
| `src/wisper_transcribe/models.py` | Add `ShowMember`, `Show` dataclasses |
| `src/wisper_transcribe/show_manager.py` | **New** — CRUD for shows/rosters |
| `src/wisper_transcribe/speaker_manager.py` | Add `profile_filter` param to `match_speakers()` |
| `src/wisper_transcribe/pipeline.py` | Add `show` param to `process_file()` |
| `src/wisper_transcribe/cli.py` | Add `shows` command group; add `--show` to `transcribe`, `speakers list`, `speakers test` |
| `src/wisper_transcribe/web/routes/shows.py` | **New** — show management routes |
| `src/wisper_transcribe/web/routes/transcribe.py` | Pass `show` from form to job kwargs |
| `src/wisper_transcribe/web/routes/speakers.py` | Optional show filter on list |
| `src/wisper_transcribe/web/templates/shows.html` | **New** — show list/detail UI |
| `src/wisper_transcribe/web/app.py` | Register shows router |
| `tests/test_show_manager.py` | **New** — unit tests for all show_manager functions |
| `tests/test_web_routes.py` | Add show route tests |
| `architecture.md` | Update module map, data layout, design decisions |
| `README.md` | Document `wisper shows` commands and `--show` flag |

---

#### Migration / Backward Compatibility

- `shows.json` absent on first run → `load_shows()` returns `{}`
- `--show` omitted everywhere → `profile_filter=None` → identical behavior to today
- Existing enrolled profiles untouched; no file moves, no schema migrations
- If a profile is deleted but still referenced in a show roster, `match_speakers()` silently skips it (existing behavior — missing `.npy` is already handled)

---

## Manual Test Plans

### LLM Post-processing CLI (T1–T5) — code complete; manual verification pending

**T1 — `wisper refine`**

T1.1 Dry-run: `wisper refine session.md` → diff printed, file unchanged.
T1.2 No terms: `wisper refine session.md` with no hotwords → skipping warning.
T1.3 Apply + backup: `wisper refine session.md --apply` → `.bak` created.
T1.4 No-color: `wisper refine session.md --no-color | cat` → no ANSI codes.
T1.5 Unknown task: `wisper refine session.md --tasks unknown` → suggestions only.
T1.6 Both tasks: `wisper refine session.md --tasks vocabulary,unknown`.
T1.7 YAML frontmatter unchanged after `--apply`.

**T2 — `wisper summarize`**

T2.1 Basic: `wisper summarize session.md` → sidecar with all sections.
T2.2 No overwrite: second run without `--overwrite` → error.
T2.3 Overwrite: `wisper summarize session.md --overwrite` → clean regeneration.
T2.4 Custom path: `wisper summarize session.md --output /tmp/recap.md`.
T2.5 Sections filter: `--sections summary,loot` → only those sections present.
T2.6 Wiki-links: enrolled speaker names become `[[Name]]` in body.
T2.7 Non-enrolled: unenrolled names stay plain text.
T2.8 Refine flag: `wisper summarize session.md --refine` → `refined: true` in frontmatter.

**T3 — Combined flow**

T3.1 Refine failure still produces summary (bad endpoint → WARN + summary written).
T3.2 `--refine-tasks vocabulary,unknown` → both passes + unresolved speakers in output.

**T4 — LLM config integration**

T4.1 Provider flag beats config.
T4.2 `wisper config llm` wizard round-trip → keys masked in `config show`.
T4.3 Env var beats config key for API access.

**T5 — Edge cases**

T5.1 No-frontmatter transcript handled gracefully.
T5.2 Empty transcript body — no crash.
T5.3 Read-only directory — clean error.

### Web UI LLM post-processing (W1–W5) — code complete; manual verification pending

W1 Post-process checkboxes on transcribe form → campaign notes appear after job.
W2 Standalone summarize from transcript detail → job progress page → notes.
W3 Campaign Notes page renders with metadata.
W4 Delete transcript removes summary sidecar.
W5 Summary badge on transcripts list page.

### Progress bar redesign (P1–P3) — code complete; manual verification pending

P1 Transcription-only job: T → D → F pills advance, bar fills across all three slices, ETA/rate shown during T and D.
P2 Transcription + post-summarize: T → D → F → S pills shown; S activates on "Summarizing…" log line; estimator creeps bar during S when no tqdm data.
P3 Standalone summarize job: only S pill shown; bar fills from 0 → 100 via estimator + done event.
