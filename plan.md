# Wisper-Transcribe: Podcast Transcription with Speaker Diarization

## Context

The user runs tabletop RPG actual-play podcasts (D&D-style) with 5-8 speakers (GM + players). They want to transcribe sessions into markdown transcripts with consistent speaker labeling across files. The transcripts will be fed into a NotebookLM-style system for querying game events and tracking stats.

**Hardware**: NVIDIA RTX 3090 (24GB VRAM) on Windows, Apple M5 Mac. Both platforms must be supported.
**Processing**: All local, no cloud APIs. CLI-driven.

## Technical Stack

**Custom pipeline: faster-whisper + pyannote-audio**

- faster-whisper: 4× faster than OpenAI whisper via CTranslate2, lower VRAM usage
- pyannote-audio: speaker diarization + voice embedding extraction
- Chose this over WhisperX due to chronic dependency pinning issues in WhisperX
- Direct embedding access is critical for cross-file speaker ID

**Key dependency: HuggingFace token** (free) required for pyannote models. One-time setup.
**System requirement: ffmpeg** for audio format conversion.

## Project Structure

```
wisper-transcribe/
├── pyproject.toml
├── README.md
├── CLAUDE.md
├── plan.md
├── src/
│   └── wisper_transcribe/
│       ├── __init__.py
│       ├── __main__.py            # python -m wisper_transcribe
│       ├── cli.py                 # Click CLI commands
│       ├── config.py              # Config loading, platform paths, ffmpeg check
│       ├── pipeline.py            # Main orchestrator
│       ├── transcriber.py         # faster-whisper wrapper
│       ├── diarizer.py            # pyannote diarization wrapper
│       ├── speaker_manager.py     # Speaker profiles, enrollment, matching
│       ├── aligner.py             # Merge transcription + diarization segments
│       ├── formatter.py           # Markdown output generation
│       ├── audio_utils.py         # Audio validation, conversion
│       └── models.py              # Data classes
└── tests/
    ├── test_models.py
    ├── test_config.py
    ├── test_audio_utils.py
    ├── test_transcriber.py
    ├── test_formatter.py
    ├── test_aligner.py
    ├── test_diarizer.py
    ├── test_pipeline.py
    ├── test_pipeline_folder.py
    └── test_speaker_manager.py
```

User data stored via `platformdirs` (outside the repo, never committed):
- Windows: `%APPDATA%\wisper-transcribe\`
- Mac: `~/Library/Application Support/wisper-transcribe/`

```
wisper-transcribe/          # user data dir
├── config.toml
└── profiles/
    ├── speakers.json       # name -> metadata mapping
    └── embeddings/         # .npy voice fingerprint files (gitignored)
```

## Processing Pipeline

```
1. VALIDATE     → audio_utils: check file exists, supported format
2. PREPROCESS   → audio_utils: convert to 16kHz mono WAV (if needed)
3. TRANSCRIBE   → transcriber: faster-whisper → text segments with timestamps
4. DIARIZE      → diarizer: pyannote → speaker-labeled time regions
5. ALIGN        → aligner: merge text segments with speaker labels
6. IDENTIFY     → speaker_manager: match anonymous labels to enrolled profiles
7. FORMAT       → formatter: produce markdown output
8. WRITE        → save .md file (one per input file)
```

## Speaker Labeling Lifecycle

### First run — enroll speakers interactively
```
$ wisper transcribe session01.mp3 --enroll-speakers --num-speakers 6
```
After transcription + diarization, prompts for each speaker's name/role. Saves voice embeddings to profiles directory for future matching.

### Subsequent runs — automatic matching
```
$ wisper transcribe session02.mp3 --num-speakers 6
```
Extracts embeddings for each detected speaker, compares via cosine similarity against enrolled profiles (threshold: 0.65 default), assigns names. Unknown speakers labeled "Unknown Speaker N".

### Edge cases
- **New player:** appears as "Unknown Speaker N" → `wisper fix` + `wisper enroll`
- **Absent player:** their profile is simply ignored
- **Voice drift:** `wisper enroll --update` blends new sample via EMA (alpha=0.3)
- **Wrong match:** `wisper fix session.md --speaker "Alice" --name "Diana"`

## CLI Reference

```
wisper transcribe <path>          # file or folder
  -o, --output DIR
  -m, --model SIZE                # tiny/base/small/medium/large-v3 (default: medium)
  -l, --language LANG             # language code or 'auto'
  -n, --num-speakers INT
  --min-speakers / --max-speakers INT
  --enroll-speakers               # interactive first-run naming
  --play-audio                    # play each speaker's excerpt during enrollment
  --no-diarize
  --timestamps / --no-timestamps
  --device cpu|cuda|auto
  --compute-type auto|float16|int8_float16|int8|float32
  --vad / --no-vad                # voice activity detection to skip silence (default: on)
  --vocab-file FILE               # newline-separated hotwords list (overrides config)
  --initial-prompt TEXT           # prepended context to guide transcription style
  --overwrite
  --verbose

wisper enroll <name> --audio <file>
  --segment START-END
  --notes TEXT
  --update                        # EMA blend with existing embedding

wisper speakers list|remove|rename|reset|test

wisper config show|set|path

wisper fix <transcript.md>
  --speaker NAME --name NEW_NAME [--re-enroll]
```

## Output Format

```markdown
---
title: Session 01 - The Dragon's Keep
source_file: session01.mp3
date_processed: '2026-04-05'
duration: 1:23:45
speakers:
- name: Alice
  role: DM
- name: Bob
  role: Player
---

# Session 01 - The Dragon's Keep

**Alice** *(00:00:12)*: Welcome back everyone. Last session you had just entered the ruins.

**Bob** *(00:00:18)*: Right, I want to check for traps before we go further in.
```

## HuggingFace Model Notes

Models are downloaded once on first use and cached to `~/.cache/huggingface/hub/`. Subsequent runs are fully offline.

| Model | Purpose | Size |
|-------|---------|------|
| `openai/whisper-*` (via faster-whisper) | Transcription | 75MB–1.5GB depending on size |
| `pyannote/speaker-diarization-3.1` | Speaker diarization | ~400MB |
| `pyannote/embedding` | Voice fingerprinting | ~200MB |
| `pyannote/segmentation-3.0` | Voice activity detection | ~100MB |

To check what's cached: `huggingface-cli scan-cache`

Required one-time license agreements (free, HuggingFace account):
- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/embedding
- https://huggingface.co/pyannote/segmentation-3.0

## Cross-Platform Notes

- Always use `pathlib.Path` for all file ops
- `platformdirs` for config/data directory resolution
- **Windows (RTX 3090)**: CUDA auto-detected. Use `large-v3` model. CUDA DLL path fix applied in `transcriber.py` for CTranslate2 compatibility.
- **Mac (M5)**: CPU-only (MPS unreliable for these models). Use `medium` model for speed.
- ffmpeg check on startup with platform-specific install instructions

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

### Phase 10 — Parallel Folder Processing (CPU-only) ✓

**Context:** GPU processing is always the bottleneck — faster-whisper and pyannote are not thread-safe when sharing a GPU, and loading duplicate model copies would exhaust VRAM. Parallelism only makes sense on CPU-only deployments (e.g. a Linux server processing a large queue of files overnight).

**What was built:** `--workers N` flag on `wisper transcribe <folder>`. Uses `concurrent.futures.ProcessPoolExecutor` (not ThreadPoolExecutor — the module-level `_model`/`_pipeline` globals are not thread-safe; each subprocess gets its own isolated module state). Guards: device != cpu → clamp to 1; `--enroll-speakers` → clamp to 1 (needs TTY). Default workers=1.

---

### Phase 11 — Browser-Based Web UI ✓

Full-featured web interface launched by `wisper server`. FastAPI + HTMX + Jinja2 + Tailwind CSS. All assets served locally — no CDN at runtime.

Pages: Dashboard (live job queue, system status), Transcribe (file upload + all options), Transcripts (browse/view/download), Speakers (profiles, enroll, rename, remove), Config (edit all settings).

Speaker enrollment replaced by a post-job naming wizard (the interactive TTY-based CLI flow is web-incompatible). Each speaker card includes a Play/Stop button that streams a ~12s audio excerpt so users can hear the voice before naming.

Docker: `wisper-web` (GPU) and `wisper-cpu-web` (CPU) services on port 8080.

Wisp logo: will-o'-the-wisp SVG orb with animated floating spark particles. Header banner is dark green (`bg-green-900`).

**Web UI improvements (April 2026):**
- Transcripts now always save to the configured output directory (was saving to temp dir for web uploads)
- Job name is the uploaded filename stem — shown in dashboard table and job detail heading
- Visual progress bar on job detail page, driven by tqdm SSE events
- Stop Job button cancels pending jobs immediately; signals running jobs via threading.Event checked on each tqdm heartbeat
- Tailwind CSS auto-rebuilt on server startup (mtime check); `pytailwindcss` promoted to main dependency; Dockerfile also builds CSS at image build time
- Speaker audio playback in enrollment wizard (Play/Stop toggle per speaker card)

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



## GUI Improvements
 - Put dividers or space out the menu items in the upper right. 
 - mobile layout adjustments
 - confirm where transcripts are stored. 
    - Decision - Show transcripts already in output folder?
  - input file on the transcribe page should show the file name, not the full path.
  - Allow playing of the sample file in the speakers section
  - for web progress bar remove the middle section which would map to the commmand line progress bar. 
 
