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

## Implementation Status

### ✅ Phase 1 — Project Skeleton + Basic Transcription
All modules created, CLI entry point, single-file transcription to markdown, tests.

### ✅ Phase 2 — Speaker Diarization
pyannote pipeline wrapper, max-overlap aligner, HF token management, `--num-speakers` / `--no-diarize` flags.

### ✅ Phase 3 — Speaker Profiles & Cross-File Identification
`speaker_manager.py`: profile CRUD, embedding extraction, cosine-similarity matching with greedy assignment, EMA updates. `wisper enroll`, `wisper speakers`, `wisper fix` commands.

### ✅ Phase 4 — Batch Processing & Polish
`process_folder()` with tqdm progress bars, per-file error recovery, skip-existing, `--verbose` flag. Windows CUDA DLL path resolution. `wisper config` commands.

### ✅ Phase 5 — Tests & README
103 tests passing. All ML calls mocked. No GPU required for test suite. README with install, quick start, full CLI reference.

### ✅ pyannote-audio 4.x Upgrade (April 2026)
Upgraded from 3.4.0 → 4.0.4. Removed 5 compatibility shims (torchaudio stubs, hf_hub `use_auth_token`, torch.load default). speechbrain `LazyModule.ensure_module` patch retained — pyannote 4.x still uses speechbrain for ECAPA-TDNN embeddings and the Windows path bug is in speechbrain itself.

One additional fix required post-upgrade: pyannote 4.x wraps diarization output in a `DiarizeOutput` dataclass (`DiarizeOutput.speaker_diarization` is the `Annotation`), breaking the existing `diarization.itertracks()` call. Fixed in `diarizer.py` with a `hasattr` guard for backwards compatibility.

torchcodec still cannot find FFmpeg shared DLLs on this Windows install despite `Gyan.FFmpeg.Shared` being listed in `setup.ps1`. The scipy audio loading bypass (`scipy.io.wavfile` → waveform dict) remains as a workaround. Functionally equivalent; end-to-end test confirmed working (11 speakers enrolled, full `.md` output, CUDA device).

---

## Backlog

### Near-term (ready to build)

*(No remaining near-term items — see completed list below.)*

### ✅ Near-term completed

- **`wisper setup` command** ✅ — guided wizard: ffmpeg, HF token, model pre-download, device detection.
- **Progress header on each file** ✅ — Input/Output/Model line printed before each file processes.
- **Expose data paths in `wisper config show`** ✅ — config file, data dir, profiles dir, HF cache all shown.
- **`wisper config show` model clarity** ✅ — Models section: device, Whisper model, compute type (with auto-resolution), pyannote models.
- **Enrollment speaker order — chronological** ✅ — speakers sorted by first appearance timestamp in `pipeline.py`.
- **Audio playback during enrollment** ✅ — `--play-audio` flag; plays up to 10 s via ffplay subprocess (reliable cross-platform). (PR #3; Windows fix in feat/enrollment-ux)
- **`wisper speakers reset`** ✅ — deletes all profiles and embeddings with confirmation prompt.
- **Phase 7 — Docker containerization** ✅ — `Dockerfile` (gpu/cpu targets), `docker-compose.yml`, `WISPER_DATA_DIR` env override in `config.py`. 103 tests.
- **Third-party warning suppression** ✅ — speechbrain/pyannote/torch noise suppressed by default; `WISPER_DEBUG=1` restores raw output. absl "triton not found" log requires `absl.logging.set_verbosity(ERROR)` (not `logging.getLogger("absl")`). (PR #6, absl fix in PR #7)
- **Phase 8 — VAD filter** ✅ — `--vad/--no-vad` flag; faster-whisper built-in `vad_filter`; `None`-sentinel so unset falls through to config default (on). 103 tests.
- **Phase 9 — Compute type / quantization** ✅ — `--compute-type auto|float16|int8_float16|int8|float32`; configurable via `wisper config set compute_type`; shown in run header and `wisper config show`.

### pyannote 4.x upgrade

**Status: ✅ Complete (April 2026). Merged in PR #2.**

### ✅ Phase 7 — Docker Containerization

**Status: Complete.**

- `Dockerfile`: two targets — `gpu` (PyTorch cu126 wheels, `python:3.12-slim` base) and `cpu`. PyTorch CUDA wheels bundle the CUDA runtime; no NVIDIA base image required. pydub still needs system `ffmpeg`, installed via apt.
- `docker-compose.yml`: `wisper` (GPU, default) and `wisper-cpu` services. GPU passthrough via modern `deploy.resources.reservations.devices` syntax (NVIDIA Container Toolkit on host).
- `config.py`: `get_data_dir()` checks `WISPER_DATA_DIR` env var before `platformdirs`. Set to `/data` in the image; bind-mounted to `./data/` on host.
- Volume layout: `./cache` → HF model cache, `./data` → config + profiles, `./input` → audio, `./output` → transcripts.
- `.dockerignore` excludes `.venv`, tests, example-file, docs, and user data dirs.

**Verification:**
- [ ] `docker compose build` completes
- [ ] `docker compose run wisper wisper setup` — guided wizard works with TTY
- [ ] `docker compose run wisper wisper transcribe /app/input/test.mp3 --enroll-speakers` — enrollment works, profiles persist in `./data/`
- [ ] `docker compose run wisper wisper transcribe /app/input/test2.mp3` — speaker matching from persisted profiles
- [ ] `docker compose run wisper nvidia-smi` — GPU visible in container
- [ ] Container restart: no re-download of models

---

### ✅ Phase 8 — VAD Filter (from Whisper-WebUI review)

**Status: Complete.** Used faster-whisper's built-in `vad_filter=True` (Option A). Avoids timestamp remapping entirely — faster-whisper's Silero VAD integration keeps timestamps original-relative. `--vad/--no-vad` flag added to CLI; `vad_filter` in config.toml; `None`-sentinel in `process_file()` so unset flag falls through to config default.

---

### ✅ Phase 9 — Compute Type / Quantization Flag

**Status: Complete.** `--compute-type` flag added; `compute_type` in config.toml; `resolve_compute_type()` in `config.py`; shown in run header and `wisper config show`.

---

### ✅ Enrollment UX Improvements (feat/enrollment-ux branch)

**1. Re-play audio during enrollment** ✅
At the "Who is this?" prompt, entering `r` triggers a second `_play_excerpt` call and re-asks. Only active when `--play-audio` is set. Implemented as a prompt loop in `pipeline.py`.

**2. Select an existing speaker during enrollment** ✅
Before the name prompt, enrolled speaker profiles are ranked by cosine similarity to the current speaker's embedding and displayed with percentage scores (`★` for matches above threshold). User enters a number to reuse an existing profile (offered EMA update, default No) or types a new name to create one. Implemented in `pipeline.py` using `load_profiles()`, `extract_embedding()`, and `_cosine_similarity()`.

**3. Custom vocabulary / hot-words for transcription** ✅
`--vocab-file <path>` (newline-separated words → `hotwords`, `#`-comments ignored) and `--initial-prompt "<text>"` flags added to CLI. Both threaded through `process_file()` to `transcribe()` which passes them to `_model.transcribe()`. faster-whisper 1.2.1 supports `hotwords` natively. Hotwords also persist in `config.toml` via `wisper config set hotwords "word1, word2"` — `process_file()` falls back to config when no `--vocab-file` is passed.

**4. Show 'already processed' skip message** ✅ (also in this branch)
Folder-mode skip message now always shown (was `--verbose` only). Single-file message updated to match.

---

### DM Character Voice Handling (Future Feature)

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

### Local LLM Post-Processing (`wisper refine`) (Future Feature)

**Concept:** After the primary pipeline produces a `.md` transcript, run a local LLM agent pass to clean up errors that are mechanical for an LLM but hard for heuristic code: vocabulary misspellings, obviously wrong speaker assignments, and unknown speaker identification from context.

**Local LLM target:** Ollama (primary). Simple REST API, free, runs on Windows/Mac without extra setup. `llama3.2:3b` (fast, fits 4 GB VRAM) or `llama3.1:8b` (better quality, 8 GB) are the recommended starting models. LM Studio is a secondary target (same OpenAI-compatible API, same code path). Neither requires a new Python ML dependency — just `httpx` or the lightweight `ollama` package for REST calls.

New config keys:
```toml
llm_endpoint = "http://localhost:11434"   # Ollama default
llm_model = "llama3.2"
```

New CLI command: `wisper refine <transcript.md>`

---

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
- IMPORTANT: After `_merge_consecutive()` in formatter.py, the original segment-level timestamps are gone — only the start timestamp of the merged block remains. Split segments can only inherit the block's start time. Flag this as a limitation in the output.

**Task 3 — Speaker assignment from context** *(LOW-MEDIUM feasibility, HIGH risk)*

A segment labeled DM that says "I rolled a nat 20!" is obviously a player. Context-based reassignment using known speaker roles and character names.

Approach:
- Provide LLM with: enrolled speaker list + roles + character names from profile notes
- Send a window of 20 segments around the suspect segment
- Prompt: "Based on context and these speaker roles, does the assignment seem correct? Return JSON: `{correct: bool, likely_speaker: str, confidence: float, reason: str}`"
- **Only suggest, never auto-apply.** Speaker reassignment is the highest-risk change.
- Apply only if confidence > 0.85 AND user has `--apply-suggestions` flag

**Task 4 — Unknown speaker identification from context** *(MEDIUM feasibility, MEDIUM risk)*

`Unknown Speaker N` labels in the transcript can sometimes be resolved from surrounding dialogue: "Unknown Speaker 2 says 'As Lyra, the innkeeper says...' " suggests this is the DM doing a character voice.

Approach:
- Collect all "Unknown Speaker N" occurrences with their surrounding segments
- Provide full enrolled speaker list + known character names
- Prompt: "Based on this dialogue, which enrolled speaker is most likely speaking? Return JSON: `{label: 'Unknown Speaker 2', likely_speaker: 'DM', character: 'Lyra', confidence: 0.7}`"
- Threshold: confidence > 0.75 to suggest; never auto-apply

---

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

---

#### Context window management

A 3-hour session at ~150 wpm ≈ 27,000 words ≈ 35,000 tokens. Most local models have 128K context, but processing 35K tokens in one shot is slow and expensive on local hardware.

Strategy:
- **Vocabulary pass:** 25 lines per request, no overlap needed (task is stateless)
- **Speaker detection / unknown speaker:** 20-line sliding window, 5-line overlap for context continuity
- Tasks run independently; vocabulary first (cheap), speaker detection second (expensive)

---

#### Safety principles

1. `--dry-run` on by default — never silently modify a transcript
2. Backup (`.md.bak`) always created before `--apply`
3. Vocabulary changes only accepted if they are a known-term substitution (validated by edit distance against hotwords list); reject freeform rewrites
4. Speaker reassignment is **suggestion only** — never auto-applied regardless of confidence
5. YAML frontmatter is **never touched** by the LLM — only the markdown body lines
6. Ollama connectivity failure is a soft warning, not an error; the rest of the pipeline continues unaffected
7. All changes logged to `refine.log` alongside the transcript

---

#### What NOT to build

- **Grammar/style improvements:** The transcript is a verbatim record, not polished prose. LLM should not fix run-ons, filler words ("um", "like"), or informal language.
- **Content summarization:** NotebookLM handles this. Not our problem.
- **Automatic full-transcript rewrite:** Too high a risk of hallucination silently corrupting factual content.

---

#### Dependencies

- `ollama` Python package (optional; fallback to `httpx` raw REST if not installed)
- No new ML models required
- Feature is entirely opt-in; `llm_endpoint` and `llm_model` absent from config means `wisper refine` exits early with a setup message

---

### Phase 10 — Parallel Folder Processing (CPU-only)

**Context:** GPU processing is always the bottleneck — faster-whisper and pyannote are not thread-safe when sharing a GPU, and loading duplicate model copies would exhaust VRAM. Parallelism only makes sense on CPU-only deployments (e.g. a Linux server processing a large queue of files overnight).

**What to build:** `--workers N` flag on `wisper transcribe <folder>`. Uses `concurrent.futures.ThreadPoolExecutor`. Each worker gets its own model instance (no sharing). Guard: if `device != "cpu"`, emit a warning and clamp workers to 1. Default workers=1 (current behavior unchanged for all GPU users).

**When to build:** Only if there's an actual CPU-server use case. Not worth building for the primary RTX 3090 / M5 Mac workflow.

---

### Phase 11 — Optional GUI

- **Optional GUI** — Textual (terminal) or tkinter/PyQt. Wraps the same `pipeline.process_file()` and `speaker_manager` calls. Keep CLI/library separation clean.

---

### Long-Term — Intel GPU Support

**Status:** Research complete (April 2026). Not actionable yet — blocked by upstream dependencies.

**The problem:** Our two core inference engines don't support Intel GPUs:
- **CTranslate2** (powers faster-whisper): NVIDIA CUDA only. Open issue [#1715](https://github.com/OpenNMT/CTranslate2/issues/1715), no work planned.
- **pyannote-audio**: No Intel XPU backend. No upstream interest.

PyTorch itself supports Intel Arc/Data Center GPUs via `torch.xpu` (production-ready since PyTorch 2.5), but that doesn't help when our deps use CUDA-specific code paths.

**Viable paths if this becomes a real need:**

1. **OpenVINO backend for transcription** — Intel's inference engine has official Whisper support (1.4-5x faster than PyTorch). Would require an abstraction layer in `transcriber.py` that dispatches to either faster-whisper (CUDA/CPU) or OpenVINO (Intel GPU/CPU) based on detected hardware. Model conversion step needed (Whisper → ONNX → OpenVINO IR). Static-shape constraint on GPU execution.

2. **whisper.cpp with SYCL** — C++ Whisper implementation with full Intel GPU acceleration via SYCL/oneAPI. Python bindings exist (`pywhispercpp`). Different integration surface from faster-whisper but avoids the model conversion step.

3. **Diarization alternatives** — If transcription moves to OpenVINO, diarization could either:
   - Convert pyannote models to OpenVINO (manual, static-shape constraints, fragile)
   - Switch to SpeechBrain ECAPA-TDNN for speaker embeddings (actually faster on CPU than pyannote on GPU — 6.7x speedup reported)
   - Wait for pyannote to add XPU support upstream

**Architecture note:** If we ever add a second backend, the right design is an abstract `TranscriptionBackend` interface in `transcriber.py` with `FasterWhisperBackend` and `OpenVINOBackend` implementations. Same for `DiarizationBackend` in `diarizer.py`. Keep the pipeline module backend-agnostic.

**When to revisit:** Check back when either (a) CTranslate2 adds Intel GPU support, (b) a user actually needs this, or (c) OpenVINO's Whisper API stabilizes enough to be a drop-in. Don't build speculatively.

---

## Verification Checklist

- [x] `pip install -e .` succeeds
- [x] `wisper transcribe single_speaker.mp3` → readable .md without speaker labels
- [x] `wisper transcribe multi_speaker.mp3 --num-speakers 4` → .md with SPEAKER_XX labels
- [x] `wisper transcribe session01.mp3 --enroll-speakers --num-speakers 6` → interactive enrollment + .md with real names
- [x] `wisper transcribe session02.mp3 --num-speakers 6` → automatic speaker matching from profiles
- [x] `wisper speakers list` → shows enrolled profiles
- [x] `wisper fix session.md --speaker "Unknown Speaker 1" --name "Frank"` → updates transcript
- [x] `wisper transcribe ./recordings/` → batch processing with progress, skip existing, error recovery
- [x] `wisper setup` → guided first-run wizard
- [x] `wisper transcribe <file> --enroll-speakers --device cuda` on pyannote 4.0.4 → 11 speakers enrolled, full `.md` produced (4/6/2026)
- [ ] Parallel folder processing with `--workers N`
