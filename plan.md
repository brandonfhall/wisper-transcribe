# Wisper-Transcribe: Backlog & Future Work

## Project Context

Podcast transcription tool for tabletop RPG actual-play recordings (D&D, Pathfinder, etc.) with 5–8 speakers (GM + players). Transcripts are fed into NotebookLM for querying game events and tracking stats.

**Hardware:** NVIDIA RTX 3090 (Windows), Apple M5 Mac. Both platforms supported.
**Processing:** Fully local — no cloud APIs. CLI + web UI.
**Stack:** faster-whisper + pyannote-audio. See [architecture.md](architecture.md) for full technical reference and [README.md](README.md) for user docs.

---

## Notes for Claude (Recent Security & Bug Fixes)

*   **Path Traversal (CWE-22) Mitigations:** Resolved GitHub CI CodeQL warnings in `transcripts.py`, `speakers.py`, and `transcribe.py`. Replaced manual string checks with `os.path.basename()`. In `transcripts.py`, explicitly used `os.path.abspath().startswith()` and reconstructed the `Path` object to successfully clear CodeQL's taint tracking. In `speakers.py` (`enroll_submit`), added a strict Regex Match Guard (`re.match(r"^[\w\-]+$")`) before cross-module calls to definitively clear CodeQL's taint tracking.
*   **Security Tests:** Added `tests/test_path_traversal.py` to enforce the new path traversal guards against null-byte and directory escape payloads. Fixed a testing quirk with FastAPI's `TestClient` automatically following `303 Redirect` responses by explicitly setting `follow_redirects=False` for POST requests.
*   **Job Queue Flakiness:** Fixed `test_list_all_sorted_by_created_at` failure in `jobs.py`. `JobQueue.list_all()` now reverses the dictionary values before sorting to preserve stable reverse-insertion order when `created_at` timestamps tie (especially common on Windows due to clock resolution).
*   **Setup Crash:** Fixed an `IndentationError` and a mangled `try/except` block in `speakers.py` (`speaker_clip`) that was breaking the FastAPI app initialization and causing 25+ tests to error out during setup.

---

## Backlog

### Research — Apple Silicon Acceleration

**Status:** Not researched. M5 Mac is a primary development and use machine.

**Problem:** faster-whisper (CTranslate2) has no MPS backend — transcription always falls back to CPU on Apple Silicon. pyannote diarization and embedding extraction do use MPS when available. So on Mac, transcription is the bottleneck running purely on CPU even though the M5 has substantial GPU compute.

**Areas to investigate:**

1. **MLX-Whisper** — Apple's MLX framework has a first-party Whisper port (`mlx-examples/whisper`) that runs natively on the Apple Neural Engine / GPU. Could replace faster-whisper on Mac only, dispatched via a backend abstraction in `transcriber.py`. Need to evaluate: output format compatibility with our `TranscriptionSegment` model, word-level timestamp support (required for diarization alignment), and accuracy vs. faster-whisper medium/large-v3.

2. **WhisperKit** — Swift-native Whisper optimized for Apple Silicon via Core ML. Python bindings exist (`whisperkittools`). Higher integration complexity but potentially better ANE utilization than MLX.

3. **faster-whisper on MPS via OpenBLAS / Accelerate** — CTranslate2 CPU backend on Apple Silicon already benefits from Accelerate framework (BLAS). Worth benchmarking: is the CPU path already near-optimal on M-series, or is there a meaningful gap vs. MPS-capable alternatives?

**Decision point:** Only build a Mac-specific backend if benchmarks show >2× speedup over the current CPU path. The abstraction cost (maintaining two backends) must be justified by real-world session processing time.

---

### Research — Parallel Processing of a Single File

**Status:** Not researched. Current pipeline processes one file sequentially (validate → convert → transcribe → diarize → align → identify → format).

**Problem:** A 3-hour session takes significant wall time even on fast hardware. The transcription and diarization steps together are the bottleneck and currently run sequentially, but they are largely independent — transcription produces text segments, diarization produces speaker-labeled time regions; neither needs the other's output to start.

**Areas to investigate:**

1. **Concurrent transcription + diarization** — Both steps take the same WAV file as input. They could run in parallel using `asyncio.to_thread()` (already used in the web job runner) or `ProcessPoolExecutor` with two workers. The blocker: both `_model` (transcriber) and `_pipeline` (diarizer) are module-level globals — parallel threads/processes would each need their own copy. Memory cost on GPU: loading both models simultaneously requires ~5 GB (medium) + ~700 MB (pyannote) = ~6 GB, well within the RTX 3090's 24 GB. On CPU/Mac, both are loaded anyway.

2. **Audio chunking for transcription** — Split audio into N overlapping chunks, transcribe in parallel across CPU cores, merge results. faster-whisper's VAD already chunks internally; exposing this as a multi-process step is non-trivial. Risk: segment boundary artifacts at chunk join points, especially mid-sentence. Would need overlap + deduplication logic.

3. **Diarization chunking** — pyannote processes audio in overlapping windows internally. Not obviously parallelizable from outside the pipeline without forking pyannote internals.

**Likely best outcome:** Run transcription and diarization concurrently (approach 1) — this is architecturally clean and the two models are already independent. Estimate: could cut wall time by ~30–40% on GPU where diarization is the slower step.

**Guard:** Must not break the `--workers N` folder processing mode or the web job queue's one-job-at-a-time guarantee.

---

### Research — Faster / Better Transcription Models

**Status:** Not researched. Currently using faster-whisper with OpenAI Whisper weights (tiny → large-v3).

**Areas to investigate:**

1. **Whisper large-v3-turbo** — OpenAI released a distilled large-v3 model (~809M params vs. 1.5B) that is reportedly ~8× faster than large-v3 with minimal accuracy loss on English. Already supported by faster-whisper. Should be a near-drop-in upgrade for the `large-v3` config option — worth benchmarking accuracy on podcast audio specifically.

2. **Distil-Whisper** — Hugging Face distilled variants (`distil-large-v3`, `distil-medium.en`) are 5.8× faster than large-v3 with ~1% WER increase on clean speech. English-only. May be ideal for the Mac CPU path where speed matters most. Requires `transformers` backend, not CTranslate2 — would need a new backend shim or conversion to CTranslate2 format via `ct2-transformers-converter`.

3. **Parakeet (NVIDIA NeMo)** — NVIDIA's `parakeet-tdt-0.6b-v2` recently topped the OpenASR leaderboard, outperforming Whisper large-v3 on English benchmarks. Uses CTC+TDT decoding. No faster-whisper support yet; would require NeMo or ONNX integration. GPU-only (CUDA). Worth watching — if CTranslate2 support lands, this could be a significant accuracy upgrade.

4. **Model format: CTranslate2 conversion** — Any Hugging Face Whisper-compatible model can be converted to CTranslate2 format via `ct2-transformers-converter`, making it usable in the current faster-whisper path with no code changes beyond adding the model size option.

**Recommendation order:** large-v3-turbo first (zero integration cost, meaningful speedup), then distil-large-v3 for Mac/CPU users, then watch Parakeet for future GPU accuracy gains.

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
