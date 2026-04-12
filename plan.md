# Wisper-Transcribe: Backlog & Future Work

## Project Context

Podcast transcription tool for tabletop RPG actual-play recordings (D&D, Pathfinder, etc.) with 5–8 speakers (GM + players). Transcripts are fed into NotebookLM for querying game events and tracking stats.

**Hardware:** NVIDIA RTX 3090 (Windows), Apple M5 Mac. Both platforms supported.
**Processing:** Fully local — no cloud APIs. CLI + web UI.
**Stack:** faster-whisper + pyannote-audio. See [architecture.md](architecture.md) for full technical reference and [README.md](README.md) for user docs.

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

### Local LLM Post-Processing (`wisper refine`)

**Concept:** After the primary pipeline produces a `.md` transcript, run a local LLM agent pass to clean up errors that are mechanical for an LLM but hard for heuristic code: vocabulary misspellings, obviously wrong speaker assignments, and unknown speaker identification from context.

**Local LLM target:** Ollama (primary). Simple REST API, free, runs on Windows/Mac without extra setup. `llama3.2:3b` (fast, fits 4 GB VRAM) or `llama3.1:8b` (better quality, 8 GB) are the recommended starting models. LM Studio is a secondary target (same OpenAI-compatible API, same code path). Neither requires a new Python ML dependency — just `httpx` or the lightweight `ollama` package for REST calls.

New config keys:
```toml
llm_endpoint = "http://localhost:11434"   # Ollama default
llm_model = "llama3.2"
```

New CLI command: `wisper refine <transcript.md>`

#### Tasks, ranked by feasibility

**Task 1 — Vocabulary / hotword spelling correction** *(HIGH feasibility, LOW risk)*

Whisper often transcribes unknown proper nouns phonetically: "Kyra" → "Kira", "Golarion" → "Golarian", "Zeldris" → "Zeldis". The hotwords list and speaker notes are available at post-processing time and can be fed directly to the LLM as ground truth.

Approach:
- Process 20–30 transcript lines per request
- Prompt: "These proper nouns must be spelled exactly as given: [list]. Correct any misspellings in the lines below. Return JSON: `{changes: [{original, corrected}]}`. Change nothing else."
- Validate output: accept only changes that are plausible substitutions of known terms (soundex or edit-distance check on the diff)

Context source: `config["hotwords"]` + `SpeakerProfile.notes` for all enrolled profiles (character names often end up in notes).

**Task 2 — Multi-speaker segment detection** *(MEDIUM feasibility, MEDIUM risk)*

When diarization misses a speaker switch mid-segment, the merged block contains two voices. Example:
```
**DM**: The door creaks open. Right! I attack the skeleton.
```
The `"Right! I attack"` is almost certainly a player response that got captured in the same diarization window.

Approach:
- Heuristic pre-filter: flag segments where a single block contains both narration-style text AND first-person game actions ("I roll", "I attack", "I cast", "I want to...") — these are the highest-probability candidates
- Send flagged segment + surrounding 5 segments for context
- Prompt: "Does this segment sound like one continuous speaker or two? If two, where is the split? Return JSON: `{single_speaker: bool, split_after: '<exact text>'}`"
- IMPORTANT: After `_merge_consecutive()` in formatter.py, the original segment-level timestamps are gone — only the start timestamp of the merged block remains. Split segments can only inherit the block's start time.

**Task 3 — Speaker assignment from context** *(LOW-MEDIUM feasibility, HIGH risk)*

A segment labeled DM that says "I rolled a nat 20!" is obviously a player. Context-based reassignment using known speaker roles and character names.

Approach:
- Provide LLM with: enrolled speaker list + roles + character names from profile notes
- Send a window of 20 segments around the suspect segment
- Prompt: "Based on context and these speaker roles, does the assignment seem correct? Return JSON: `{correct: bool, likely_speaker: str, confidence: float, reason: str}`"
- **Only suggest, never auto-apply.** Speaker reassignment is the highest-risk change.
- Apply only if confidence > 0.85 AND user has `--apply-suggestions` flag

**Task 4 — Unknown speaker identification from context** *(MEDIUM feasibility, MEDIUM risk)*

`Unknown Speaker N` labels in the transcript can sometimes be resolved from surrounding dialogue. Collect all "Unknown Speaker N" occurrences with surrounding segments, provide enrolled speaker list + known character names, ask the LLM to identify. Threshold: confidence > 0.75 to suggest; never auto-apply.

#### Architecture

**New module:** `src/wisper_transcribe/llm_fixer.py`
- `OllamaClient` — thin REST wrapper, handles chat and generate endpoints
- `fix_vocabulary(lines, hotwords, character_names) → list[Edit]`
- `detect_multi_speaker(lines, context_window) → list[SplitSuggestion]`
- `suggest_speaker_fixes(lines, profiles) → list[SpeakerSuggestion]`
- `apply_edits(transcript_text, edits) → str` — surgical line-level substitution, never rewrites structure

**New CLI command:** `wisper refine <transcript.md>`
```
  --tasks vocabulary,speakers,unknown   # which fix types to run (default: vocabulary)
  --dry-run                             # show proposed changes without writing (DEFAULT ON)
  --apply                               # actually write changes to file (requires explicit flag)
  --model NAME                          # override llm_model from config
  --endpoint URL                        # override llm_endpoint from config
```

`--dry-run` is the default. Changes are printed as a colored diff; user must explicitly pass `--apply` to write. A `.md.bak` backup is always written before applying.

**Optional `--llm-fix` on `wisper transcribe`:** Runs vocabulary correction automatically after the pipeline (the lowest-risk task only). Skipped if Ollama is not reachable — emits a warning, does not abort.

#### Context window management

A 3-hour session at ~150 wpm ≈ 27,000 words ≈ 35,000 tokens. Most local models have 128K context, but processing 35K tokens in one shot is slow on local hardware.

- **Vocabulary pass:** 25 lines per request, no overlap needed (stateless)
- **Speaker detection / unknown speaker:** 20-line sliding window, 5-line overlap for context continuity
- Tasks run independently; vocabulary first (cheap), speaker detection second (expensive)

#### Safety principles

1. `--dry-run` on by default — never silently modify a transcript
2. Backup (`.md.bak`) always created before `--apply`
3. Vocabulary changes only accepted if they are a known-term substitution (validated by edit distance against hotwords list); reject freeform rewrites
4. Speaker reassignment is **suggestion only** — never auto-applied regardless of confidence
5. YAML frontmatter is **never touched** by the LLM — only the markdown body lines
6. Ollama connectivity failure is a soft warning, not an error
7. All changes logged to `refine.log` alongside the transcript

#### What NOT to build

- Grammar/style improvements — verbatim record, not polished prose
- Content summarization — NotebookLM handles this
- Automatic full-transcript rewrite — hallucination risk too high

#### Dependencies

- `ollama` Python package (optional; fallback to `httpx` raw REST)
- No new ML models required
- Feature is entirely opt-in; missing `llm_endpoint`/`llm_model` in config → early exit with setup message

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

## Recommendations

*Research completed April 2026. Findings grounded in full codebase review.*

---

### DM Character Voice Handling — Recommendation

**Viable path: Implement Approach 1 now; defer Approach 2.**

Approach 1 delivers the core use case (~20 lines, uses existing `notes` field, no schema migration) and provides a clean migration path to Approach 2 when needed.

**Approach 1 implementation plan:**

- `pipeline.py` — after `match_speakers()` returns `speaker_map`, add a post-processing pass: for each profile whose `notes` matches `"voice_of:<key>"`, the display label is the profile's `display_name` (e.g., `"DM (as Aziel)"`). Character voice profiles are excluded from the YAML frontmatter `speakers:` list.
- `cli.py` — in the enrollment interactive flow (the `wisper transcribe --enroll-speakers` dialog), add an optional prompt after naming a new speaker: *"Is this a character voice performed by an existing speaker? [y/N]"*. If yes, prompt for which speaker and write `notes = "voice_of:<key>"`. Also add `[voice of DM]` annotation to `wisper speakers list` output.
- Scope: ~20 lines `pipeline.py`, ~15 lines `cli.py`. Tests: 3–4 in `test_pipeline.py`, 1 in `test_speaker_manager.py`.

**Migration to Approach 2:** When Approach 2 ships (structured `attributed_to: Optional[str]` + `character_name: Optional[str]` fields on `SpeakerProfile`), `load_profiles()` reads existing profiles and auto-migrates any `notes = "voice_of:<key>"` to the new fields on first load. Non-breaking. The `character_voice_format` config key and runtime format control are Approach 2 additions.

**Do not implement Approach 1 and 2 simultaneously.** The scope increase (models.py + speaker_manager.py + formatter.py + config.py + type change to `speaker_map`) is not justified until Approach 1 is validated in use.

---

### Local LLM Post-Processing — Recommendation

**Viable path: Build Task A (vocabulary correction) + Task D (unknown speaker ID). Defer Task B and Task C.**

**Task B (multi-speaker detection) deferred:** `_merge_consecutive()` in `formatter.py` destroys per-segment timestamps before the LLM sees the transcript. The LLM can detect that a block contains two voices but cannot propose an accurate split point — only the block's start timestamp survives the merge. This requires refactoring the formatter pipeline to preserve segment-level timestamps through the merge step before Task B is feasible.

**Task C (speaker reassignment) deferred:** High risk of silent errors in actual-play content where player speaking styles overlap. Defer until vocabulary correction and unknown-speaker ID are validated in production.

**Implementation plan for Task A + Task D:**

New file `src/wisper_transcribe/llm_fixer.py`:
- `OllamaClient` — thin `httpx` wrapper around Ollama's REST API (no new dependency; `httpx` is already in dev deps). Soft-fails with a warning if Ollama is unreachable.
- `fix_vocabulary(lines: list[str], hotwords: list[str], character_names: list[str]) -> list[str]` — batches 25 lines per request; validates each proposed change against the hotwords list using edit-distance before accepting (rejects freeform LLM rewrites).
- `identify_unknown_speakers(lines: list[str], profiles: list[SpeakerProfile]) -> list[SpeakerSuggestion]` — 20-line sliding window with 5-line overlap; returns `SpeakerSuggestion(line_idx, current_label, suggested_name, confidence, reason)`; only surfaces suggestions with confidence ≥ 0.75.
- Scope: ~150–200 lines.

`cli.py` — new `wisper refine <transcript.md>` command:
- `--dry-run` (default on) — prints colored diff, writes nothing
- `--apply` — writes `.md.bak` backup then overwrites transcript
- `--tasks vocabulary,unknown` (default: `vocabulary`)
- `--model NAME` / `--endpoint URL` — override config
- YAML frontmatter is never modified
- Scope: ~80–100 lines.

`config.py` — add `llm_endpoint = "http://localhost:11434"` and `llm_model = "llama3"` to DEFAULTS. 2 lines.

`tests/test_llm_fixer.py` — mock `httpx` calls; test batch slicing; test edit-distance guard rejects freeform changes; test dry-run produces diff without writing; test `.md.bak` created on apply; test Ollama unreachable produces warning not error.

**Batch processing note:** A 3-hour session at ~1 line per 10 seconds ≈ 1,080 lines. Vocabulary at 25 lines/request ≈ 43 requests. At ~2 seconds per local Ollama request ≈ ~90 seconds total — acceptable. Document expected runtime in `--help` text.

**Safety principles (all required):**
1. `--dry-run` on by default — no silent transcript modification
2. `.md.bak` always written before `--apply`
3. Vocabulary changes accepted only if the substitution matches a known hotword/character name by edit-distance
4. Unknown speaker suggestions: warn prominently when confidence is below 0.85 even if above the 0.75 suggestion threshold
5. YAML frontmatter is never passed to the LLM or modified
6. Ollama unreachable → `warnings.warn(...)`, early return; does not abort pipeline

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

#### Verification

```bash
.venv/bin/pytest tests/test_show_manager.py -v
.venv/bin/pytest tests/ -v

wisper shows create "Test Campaign"
wisper shows add-member test-campaign alice --role DM
wisper shows list
wisper transcribe audio.mp3 --show test-campaign   # matches alice only
wisper transcribe audio.mp3                        # global match, unchanged
wisper server --reload   # verify /shows page + transcribe show dropdown

```

# Known Issues. 
- On the web interface while doing speaker enrollment the "play" button will change to "stop" but clicking on it just restarts the audio file. 
