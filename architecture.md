# wisper-transcribe — Architecture Reference

> **Keep this file current.** Update it whenever a new module is added, a key design decision changes, or the processing pipeline is modified.

---

## Tech Stack

| Component | Library | Purpose |
|-----------|---------|---------|
| Transcription | `faster-whisper` (CTranslate2) | 4× faster than openai/whisper, lower VRAM, lazy model caching; supports `hotwords` and `initial_prompt` for vocabulary guidance; default model: `large-v3-turbo` |
| Transcription (macOS) | `mlx-whisper` (optional) | Apple Silicon GPU/ANE backend; dispatched automatically when `use_mlx=auto` and `mlx-whisper` is installed on MPS devices |
| Diarization | `pyannote-audio 4.x` | Speaker segmentation + voice embeddings |
| Audio loading (diarizer) | `scipy.io.wavfile` via `load_wav_as_tensor()` | Bypasses `torchcodec` (see [Known Constraints](#known-constraints)) |
| Audio conversion | `pydub` + ffmpeg | Convert any format → 16kHz mono WAV |
| CLI | `click` | Command groups: `setup`, `transcribe`, `enroll`, `speakers`, `config`, `fix` |
| Config/storage | `platformdirs` + TOML | OS-native user data dirs, never hardcoded paths |
| Progress display | `tqdm` | Nested bars: folder-level (position=0), transcription (position=1) |
| GPU detection | `torch.cuda / torch.backends.mps` | Auto-selects CUDA → MPS → CPU at runtime |

---

## Module Map

```
src/wisper_transcribe/
├── cli.py              Click entry points — no business logic, delegates to pipeline/manager; includes setup wizard, server command; --debug and --verbose flags
├── pipeline.py         Main orchestrator: process_file(), process_folder(); enrollment logic extracted into _interactive_enroll() and _prompt_speaker_name()
├── transcriber.py      faster-whisper wrapper, lazy model cache (_model), CUDA DLL path fix, MLX dispatch
├── diarizer.py         pyannote pipeline wrapper, lazy cache (_pipeline), uses load_wav_as_tensor() from audio_utils
├── _noise_suppress.py  Centralised third-party warning/logging suppression (Lightning, pyannote, speechbrain); safe to call from subprocesses
├── debug_log.py        Centralized logging controller (Logger class); activated by --debug (file) and/or --verbose (console); tees tqdm.write() + Python logging to ./logs/wisper_<ts>.log
├── aligner.py          Merge transcription segments with diarization labels (max-overlap)
├── speaker_manager.py  Profile CRUD, embedding extraction, cosine-similarity matching, EMA updates
├── formatter.py        Markdown output, YAML frontmatter, dynamic version from __version__
├── audio_utils.py      validate_audio(), convert_to_wav(), get_duration(), load_wav_as_tensor()
├── time_utils.py       Shared time formatting: format_timestamp(), format_duration()
├── config.py           load_config(), save_config(), get_device(), get_hf_token(), get_llm_api_key(), resolve_llm_model(), check_ffmpeg()
├── models.py           Dataclasses: TranscriptionSegment, DiarizationSegment, AlignedSegment, SpeakerProfile, Edit, SpeakerSuggestion, LootChange, NPCMention, SummaryNote
├── refine.py           LLM-driven transcript refinement: vocabulary correction + unknown-speaker ID (edit-distance guarded, frontmatter-preserving)
├── summarize.py        Campaign-notes generation (session recap, loot, NPCs, follow-ups) → Obsidian-ready sidecar markdown
├── llm/                Provider-agnostic LLM client package (Ollama, Anthropic, OpenAI, Google)
│   ├── base.py         LLMClient ABC: complete() + complete_json(schema)
│   ├── errors.py       LLMUnavailableError (soft-fail) / LLMResponseError
│   ├── ollama.py       httpx REST wrapper for local Ollama
│   ├── anthropic.py    Anthropic SDK; JSON via forced tool_use
│   ├── openai.py       OpenAI SDK; JSON via response_format json_schema strict mode
│   └── google.py       google-genai SDK; JSON via response_schema
├── static/             Vendored web assets: htmx.min.js, tailwind.min.css (pre-built), wisp.svg, app.js
└── web/                Phase 11: FastAPI web UI
    ├── app.py          FastAPI application factory (create_app()), module-level app instance for uvicorn
    ├── jobs.py         In-memory job queue, JobQueue class, asyncio background worker, SSE progress via tqdm.write patch; LLM job types (refine/summarize) with stderr capture
    └── routes/
        ├── __init__.py     Jinja2 templates setup, shared get_queue() helper, urlencode filter
        ├── dashboard.py    GET /, GET /jobs (HTMX partial)
        ├── transcribe.py   GET/POST /transcribe (+ post_refine/post_summarize flags), GET /transcribe/jobs/{id}, SSE /jobs/{id}/stream, enrollment wizard
        ├── transcripts.py  GET/POST /transcripts, transcript detail, download, delete, fix-speaker; POST /transcripts/{name}/refine, POST /transcripts/{name}/summarize; GET /transcripts/{name}/summary, GET /transcripts/{name}/summary/download
        ├── speakers.py     GET/POST /speakers, enroll, rename, remove
        └── config.py       GET/POST /config
```

---

## Processing Pipeline

```
Audio file
    │
    ▼
1. VALIDATE         audio_utils.validate_audio()
   • Check file exists and extension is supported
   • Raises ValueError on unsupported format
    │
    ▼
2. CONVERT          audio_utils.convert_to_wav()
   • pydub exports to 16kHz mono WAV (temp file)
   • Input file is never modified
   • WAV files skip conversion (returned as-is)
    │
    ├──────────────────────────────────────────────────────┐
    ▼                                                      ▼
3. TRANSCRIBE       transcriber.transcribe()       4. DIARIZE   diarizer.diarize()
   • faster-whisper (CTranslate2) by default          • pyannote Pipeline (lazy-loaded, cached)
   • MLX Whisper on Apple Silicon if available        • Audio loaded via scipy.io.wavfile
     (use_mlx=auto + macOS + MPS + mlx-whisper)       • Passed as tensor dict to pipeline
   • Returns List[TranscriptionSegment]               • Returns List[DiarizationSegment]
   • Each segment: start, end, text                   • Each segment: start, end, speaker label
   Steps 3+4 run concurrently when parallel_stages=True (ProcessPoolExecutor; each subprocess isolates model globals)
    │                                                      │
    └──────────────────┬───────────────────────────────────┘
                       ▼
               5. ALIGN          aligner.align()
                  • Max-overlap strategy: each transcription segment
                    gets the speaker label with most time overlap
                  • Unmatched segments labeled "UNKNOWN"
                  • Returns List[AlignedSegment]
                       │
                       ▼
               6. IDENTIFY       speaker_manager.match_speakers()
                  • Extract per-speaker voice embeddings from WAV
                  • Cosine similarity vs enrolled profiles
                  • Greedy assignment (each profile used once)
                  • Below threshold → "Unknown Speaker N"
                  • Returns Dict[label → display_name]
                       │
                       ▼
               7. FORMAT         formatter.to_markdown()
                  • YAML frontmatter (title, date, duration, speakers)
                  • Consecutive same-speaker lines merged
                  • Optional timestamps per paragraph
                       │
                       ▼
               8. WRITE          Path.write_text()
                  • Output: <stem>.md alongside input file (or --output dir)
```

### Optional post-processing (LLM)

After the core pipeline has written `<stem>.md`, two opt-in commands operate on the transcript file:

```
<stem>.md ──► wisper refine    ──► (dry-run diff) or (<stem>.md.bak + updated <stem>.md)
           ──► wisper summarize ──► <stem>.summary.md  (Obsidian-ready sidecar)
           ──► wisper summarize --refine  ──► refine in-place, then summarize (atomic)
```

Both commands are provider-agnostic (Ollama / Anthropic / OpenAI / Google) via the `llm/` package. Neither modifies the input silently: `refine` is dry-run by default; `summarize` refuses to overwrite an existing sidecar without `--overwrite`.

---

## Speaker Identification

### Enrollment flow
1. After diarization, user names each `SPEAKER_XX` label interactively (`--enroll-speakers`)
2. For each speaker, `pipeline.py` shows a sample quote and (with `--play-audio`) plays a clip via `ffplay` subprocess
3. If profiles already exist, `extract_embedding()` is called for the current speaker label and scored against all enrolled profile embeddings via cosine similarity; profiles are displayed ranked by score (descending) with a percentage and `★` for matches above the threshold
4. The user can enter a number to reuse an existing profile (skipping re-enrollment) or type a new name to create one; if reusing, they are offered the option to blend this episode's audio into the profile via EMA (default: No)
5. Entering `r` at the name prompt replays the audio clip (only when `--play-audio` is set)
6. For new speakers: `speaker_manager.extract_embedding()` slices the WAV to that speaker's segments and runs pyannote's embedding model; 512-dim numpy vector saved to `profiles/embeddings/<name>.npy`; metadata saved to `profiles/speakers.json`

### Matching flow (subsequent runs)
1. Extract embedding for each detected speaker label
2. Build cosine similarity matrix: `(n_detected × n_enrolled)`
3. Greedy assignment: highest-similarity pair first, each profile assigned at most once
4. Similarity below threshold (default 0.65) → label kept as `"Unknown Speaker N"`

### EMA updates (`wisper enroll --update`)
New embedding blended with existing: `stored = 0.7 * stored + 0.3 * new`

---

## Key Design Decisions

### scipy audio pre-loading (torchcodec bypass)
pyannote-audio 4.x uses `torchcodec` for audio I/O by default. On Windows, torchcodec requires FFmpeg's "full-shared" build (`winget install Gyan.FFmpeg.Shared`) to load its native DLLs. Rather than make the full-shared build a hard requirement, `diarize()` and `extract_embedding()` call `audio_utils.load_wav_as_tensor()` — a shared helper that reads a WAV file via `scipy.io.wavfile`, normalises the data to float32, and returns a `{'waveform': tensor, 'sample_rate': int}` dict. When the dict contains `"waveform"`, pyannote's `Audio.__call__()` and `Audio.crop()` skip torchcodec entirely and operate on the tensor directly. The input is always a 16kHz mono WAV produced by `convert_to_wav()`. This loading logic was previously duplicated inline in both `diarizer.py` and `speaker_manager.py`; it is now centralised in `audio_utils.load_wav_as_tensor()`.

### speechbrain LazyModule shim (Windows path bug)
speechbrain 1.0 lazy-loads optional integrations (k2, transformers, spacy, numba) via `LazyModule.ensure_module()`. The guard that suppresses lazy loads triggered by `inspect.getmembers()` checks for `"/inspect.py"` — a forward-slash check that never matches on Windows (which uses backslash). As a result, every missing optional integration raises `ImportError` instead of silently no-oping. `diarizer.py` patches `LazyModule.ensure_module` at import time to catch these `ImportError`s and return empty stub modules. This is the only compatibility shim remaining; it is in speechbrain itself, not pyannote.

### Module-level imports for mock patching
`pyannote.audio.Pipeline` is imported at the top of `diarizer.py` (not inside the function). `pydub.AudioSegment` is imported at the top of `audio_utils.py`. This is required so `unittest.mock.patch("wisper_transcribe.diarizer.Pipeline", ...)` resolves correctly in tests. Lazy imports inside functions cannot be patched at the module path.

### CUDA DLL path resolution (Windows)
`transcriber.load_model()` searches for `cublas64_12.dll` in PyTorch's `nvidia-cublas` site-packages directory and the system CUDA Toolkit before loading `WhisperModel`. CTranslate2 on Windows requires this DLL to be on `PATH` or added via `os.add_dll_directory()`.

### Third-party warning suppression (`WISPER_DEBUG` / `_noise_suppress.py`)
All suppression logic is centralised in `_noise_suppress.py` as a single `suppress_third_party_noise()` function. This is intentional: suppression must run as the **very first thing** in any process (or subprocess) that will load pyannote/Lightning, before those packages are imported. Inline suppression in `diarizer.py` alone was insufficient because subprocess workers (spawned by `ProcessPoolExecutor` in parallel mode) start fresh Python interpreters where `diarizer.py` is imported after `pipeline.py` — leaving a gap where Lightning redirect warnings fired before any filter was in place.

The function is called:
- At the top of `diarizer.py` (main process, before `from pyannote.audio import Pipeline`)
- At the top of `speaker_manager.py` (main process, before `pyannote.audio` is imported via `_load_embedding_model()`; required because `wisper enroll` never loads `diarizer.py`)
- As the **first line** of `_diarize_worker()` in `pipeline.py` (before any ML import in each subprocess)

The function handles two categories:
1. **`warnings.filterwarnings("ignore", ...)`** for `warnings.warn()`-based messages (speechbrain redirects, pyannote TF32/std() warnings, Lightning migration shim, checkpoint auto-upgrade, ModelCheckpoint states, task-dependent loss, missing state-dict keys). Category restrictions are intentionally omitted so filters catch all warning categories.
2. **`_silence_logger(name)`** for messages routed through Python `logging`. `setLevel(ERROR)` alone is unreliable because Lightning resets its own loggers to INFO during import. `_silence_logger` instead attaches a `_SilenceFilter` (a `logging.Filter` that always returns `False`) and sets `propagate=False`. A `Filter` is independent of `setLevel` — it persists even after downstream package init code resets the level. `propagate=False` prevents records from reaching root-logger handlers via propagation.

All suppressions are gated on `not os.environ.get("WISPER_DEBUG")`. Loggers silenced:
- `lightning`, `lightning.pytorch`, `lightning.pytorch.utilities`, `lightning.pytorch.utilities.migration`, `pytorch_lightning` — checkpoint upgrade and migration shim messages
- `torch` — `torch.utils.flop_counter` "triton not found" message
- `HF_HUB_DISABLE_SYMLINKS_WARNING=1` env var silences the HuggingFace Hub symlink advisory on Windows (informational; cache still works)
- `absl.logging.set_verbosity(ERROR)` covers triton messages routed through absl-py's logging system (separate from Python's hierarchy; `logging.getLogger("absl")` has no effect on it)

### Logging (`--debug` / `--verbose`)
Both flags are handled by `debug_log.Logger`, a class that owns both output modes independently. `setup_logging(debug=, verbose=)` creates the module-level singleton and is called once at CLI startup.

**`debug=True`** (`--debug` on `transcribe` and `server`):
1. Sets `WISPER_DEBUG=1` so warning suppression is disabled.
2. Creates `./logs/wisper_<YYYYMMDD_HHmmss>.log` in the CWD.
3. Patches `tqdm.write()` to tee every call to the file (captures full pipeline status for sequential and parallel modes).
4. Attaches a `_LoggingBridge` handler (not `logging.FileHandler`) to the root Python logger. `_LoggingBridge` routes records through `Logger._write_to_file()` — the same single fd used by the tqdm tee — eliminating the interleaved-write bug that occurred when two independent fds wrote to the same file concurrently (e.g. a long pydub ffmpeg command line split across a tqdm.write call).

**`verbose=True`** (`--verbose` on `transcribe` only):
1. Attaches a console `logging.StreamHandler` at DEBUG level to the root logger so ML library output (pyannote, faster-whisper, etc.) is surfaced in the terminal alongside normal `tqdm.write()` output.
2. Does **not** create a log file; does **not** set `WISPER_DEBUG`.

Both flags may be combined: `wisper transcribe --debug --verbose` writes the file and shows ML library logs on the console simultaneously. The log path is printed to stdout when `--debug` is active.

### VAD filter via faster-whisper built-in
`transcribe()` passes `vad_filter=True/False` directly to `_model.transcribe()`. faster-whisper bundles Silero VAD internally; when enabled it skips silence/non-speech frames before feeding audio to Whisper. This is "Option A" from the plan — no separate audio stripping step, no timestamp remapping required. Timestamps in the output remain original-audio-relative. Controlled via `--vad/--no-vad` CLI flag (default: on, from config). `process_file()` uses `vad_filter: Optional[bool] = None` as a sentinel so an unset flag falls through to the config value rather than hard-coding True.

### Custom vocabulary (hotwords / initial_prompt)
`transcribe()` accepts two optional vocabulary guidance parameters forwarded to `_model.transcribe()`:
- `hotwords: list[str]` — explicitly boosted tokens (faster-whisper ≥ 1.1). Ideal for proper nouns, character names, and location names that Whisper under-weights (e.g. `["Kyra", "Golarion", "Zeldris"]`).
- `initial_prompt: str` — text prepended as fake prior context; nudges Whisper toward certain vocabulary and style.

Exposed via `--vocab-file <path>` (newline-separated word list → `hotwords`, lines starting with `#` ignored) and `--initial-prompt "<text>"` CLI flags. The `cli.py` layer reads the file and parses the list before passing to `process_file()`.

Hotwords can also be persisted in `config.toml` as a TOML array via `wisper config set hotwords "word1, word2, ..."`. `process_file()` falls back to `config["hotwords"]` when no `--vocab-file` is passed. `--vocab-file` always takes precedence over config. Config key: `hotwords` (default: `[]`).

### Audio playback during enrollment (`--play-audio`)
`_play_excerpt()` in `pipeline.py` calls `ffplay` via `subprocess.run()` with `-nodisp -autoexit -loglevel quiet -ss <start> -t <duration>`. ffplay ships with ffmpeg, which is already a hard dependency, making this reliable cross-platform without additional Python audio packages. Replaces an earlier `pydub.playback.play()` implementation that silently failed on Windows due to missing `simpleaudio`/`pyaudio` backends.

### CTranslate2 compute type
`load_model()` calls `resolve_compute_type(compute_type, device)` to convert `"auto"` to a concrete CTranslate2 dtype: `"float16"` on CUDA (fast, GPU-native), `"int8"` on CPU (lower memory, minimal accuracy loss). Non-auto values (`float32`, `int8_float16`, etc.) are passed through unchanged. This is configurable via `--compute-type` flag and `wisper config set compute_type`.

### MLX Whisper backend (Apple Silicon)
On macOS with an MPS device, `transcribe()` can dispatch to `mlx_whisper.transcribe()` instead of faster-whisper. The dispatch logic lives in `transcriber.py` and is controlled by the `use_mlx` config key (`"auto"` | `"true"` | `"false"`). `"auto"` (default) enables MLX only when `mlx-whisper` is installed and importable — it falls back to faster-whisper CPU silently if not. `"true"` errors if the package is missing. `"false"` always uses faster-whisper CPU.

MLX models are downloaded from HuggingFace (`mlx-community/whisper-*-mlx`) on first use and cached in `~/.cache/huggingface/hub/`. The model-name mapping from standard size names to MLX repo IDs lives in `_MLX_MODEL_MAP`. hotwords are injected into `initial_prompt` as a comma-separated prefix (mlx-whisper has no native hotwords param). `vad_filter` is silently skipped (not supported). Install: `pip install 'wisper-transcribe[macos]'`.

### Parallel stage processing (`parallel_stages`)
When `parallel_stages=True` in config (default `False`), `process_file()` runs transcription and diarization concurrently via `ProcessPoolExecutor(max_workers=2)`. The two stages are independent: both take the same converted WAV file as input and produce outputs combined in the `align()` step. Each subprocess gets its own copy of the module-level `_model`/`_pipeline` globals, so there are no thread-safety concerns.

**Progress IPC for the web UI.** Subprocess workers write tqdm output to their own stderr by default — the web job's `ProgressCatcher` lives in the parent process and can't capture it. `_run_parallel_transcribe_diarize()` solves this by:
1. Creating a `multiprocessing.Manager().Queue()` (not a plain `multiprocessing.Queue`) passed to each worker as `_progress_queue`. A managed queue is required on macOS because Python's "spawn" start method pickles arguments; plain `multiprocessing.Queue` objects cannot be pickled across spawn boundaries.
2. Each worker calls `_patch_tqdm_for_queue(queue, channel)` before any ML import. This patches `tqdm.write` and `tqdm.__init__` in the subprocess. Queue tuple format: `(channel, msg_type, message)` where `msg_type` is `"log"` (tqdm.write status messages) or `"bar"` (last non-empty tqdm bar render frame per update, with ANSI codes stripped).
3. A background drain thread in the parent reads tuples: `"log"` messages go through `tqdm.write()` so they reach the debug log tee if active; `"bar"` renders go directly to `sys.stderr` with per-channel deduplication so they display in the terminal without appearing in the log file.
4. `tqdm.write()` in the parent goes through the `capturing_write` patch in `jobs._run_job`, routing messages to `job.log_lines` for the SSE stream.
5. The SSE route streams log lines to the browser; the job detail page shows the standard progress indicators.

Interaction with `--workers N` folder mode: when both `parallel_stages=True` and `workers>1` are active, the total process count is N×2. Users with high `--workers` values can set `parallel_stages=False` to avoid contention. The web job queue's one-job-at-a-time guarantee is unaffected because the inner `ProcessPoolExecutor` runs inside the `asyncio.to_thread()` call.

`_run_parallel_transcribe_diarize()` is a module-level function (target for test mocking). `_transcribe_worker`, `_diarize_worker`, and `_patch_tqdm_for_queue` are all module-level (not closures) so they are picklable by the executor.

### Module-level model caches
`_model` (transcriber) and `_pipeline` (diarizer) are module-level globals. This avoids reloading multi-GB models between files when processing a folder. The caches are intentionally reset to `None` in tests.

### Parallel folder processing (`--workers N`)
`process_folder()` accepts a `workers` parameter (default 1). When `workers > 1`, it uses `concurrent.futures.ProcessPoolExecutor` — **not** `ThreadPoolExecutor` — because `_model` and `_pipeline` are module-level globals that are not thread-safe. Each subprocess gets its own copy of the module, so globals are isolated. Guard: if the effective device resolves to anything other than `"cpu"` (after resolving `"auto"`), `workers` is clamped to 1 with a warning, because GPU memory cannot be shared across processes. CPU-only deployments (e.g. a batch server) can safely use multiple workers. `ProcessPoolExecutor` is imported at module level in `pipeline.py` so tests can patch it at `wisper_transcribe.pipeline.ProcessPoolExecutor`.

### pyproject.toml torch version
`torch>=2.8.0` is required because `pyannote-audio 4.x` declares this minimum. The CUDA build must be installed from `https://download.pytorch.org/whl/cu126` — PyPI only ships the CPU-only build. The `setup.ps1` script handles this automatically on Windows.

### LLM post-processing (`refine.py`, `summarize.py`, `llm/`)
Post-processing of an already-written transcript is split into two shapes:

- **`refine.py` — surgical.** `fix_vocabulary()` asks the LLM for `{original, corrected}` pairs in batches of ~25 lines, then validates each proposed substitution against the known hotwords + enrolled character names via `difflib.get_close_matches(..., cutoff=0.7)`. Freeform rewrites ("The party stepped in" → "The heroes proceeded") are rejected with a `UserWarning`. `identify_unknown_speakers()` runs a 20-line sliding window with 5-line overlap and only keeps suggestions with confidence ≥ 0.75 **and** a `suggested_name` that matches an enrolled `SpeakerProfile.display_name` — so the LLM cannot hallucinate new identities. Unknown-speaker suggestions are **never auto-applied**; they surface as rendered output only.
- **`summarize.py` — generative.** One structured-JSON call produces a `SummaryNote` (summary paragraph, loot list, NPC list, follow-ups). `render_markdown()` emits an Obsidian-compatible sidecar: YAML frontmatter (`type: session-summary`, `provider`, `model`, `refined`), then `## Summary / ## Loot & Inventory / ## NPCs / ## Follow-ups`. Names are wrapped in `[[wiki-links]]` only when they match an enrolled speaker's `display_name` or a name listed in their `notes` — unknown names stay plain to avoid creating orphan vault pages.

The `llm/` package wraps each provider behind a single `LLMClient` ABC with `complete(system, user)` and `complete_json(system, user, schema)`. Provider differences (Anthropic's forced `tool_use`, OpenAI's `response_format={"type": "json_schema", "strict": true}`, Google's `response_schema`, Ollama's `format="json"`) are entirely internal. SDKs are **lazy-imported inside each client class** so a user with only Ollama installed never hits an `anthropic`/`openai`/`google-genai` import error — missing package raises `LLMUnavailableError` with an install hint.

**Ollama streaming.** `OllamaClient._post_chat()` uses `httpx.stream()` with `read=None` (no per-chunk read timeout) so long transcripts never hit a read deadline mid-generation. A connect/write timeout (`self.timeout`, default 30 s) still guards against Ollama not being reachable. While streaming, a live dot-progress line is written to stderr (one `·` per 50 tokens) so the user can see the model is working. `wisper config llm` and `wisper setup` call `ollama list` via subprocess and display a numbered model picker when Ollama is running — falls back to a plain text prompt if the command is unavailable.

Safety invariants the implementation enforces:
1. **YAML frontmatter is never sent to the LLM and is preserved byte-for-byte** — `parse_transcript()` splits the document into `(frontmatter_dict, body, raw_frontmatter_str)` and only the body is passed downstream. Reassembly uses the original raw string, not a re-serialised copy.
2. **Dry-run default on refine**; `--apply` writes `<stem>.md.bak` before overwriting.
3. **Cloud providers are opt-in**: default config is `llm_provider = "ollama"`. Cloud usage requires an explicit config change + API key (via env var preferred).
4. **Soft-fail network model**: unreachable Ollama, 429/500 from cloud, or missing package → `warnings.warn()` + early return. In the `summarize --refine` flow, a refine failure still produces a summary with `refined: false` recorded in frontmatter.
5. **API key lookup is env-var first** (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`), then config — matching the existing `get_hf_token()` pattern. Keys are masked as `***` in `wisper config show`.

---

## Data Storage

All user data lives **outside the repo** in the OS-native user data directory, unless overridden by `WISPER_DATA_DIR`:

| Context | Path |
|---------|------|
| Windows | `%APPDATA%\wisper-transcribe\` |
| Mac/Linux | `~/.local/share/wisper-transcribe/` (or XDG equivalent) |
| Docker | `/data` (via `WISPER_DATA_DIR=/data` env var set in the image) |

`get_data_dir()` in `config.py` checks `os.environ.get("WISPER_DATA_DIR")` first; if set, that path is used instead of `platformdirs.user_data_dir()`. This is the only source-code change needed for Docker support — everything else (config loading, profile storage, embedding paths) calls through `get_data_dir()` already.

```
wisper-transcribe/       ← get_data_dir()
├── config.toml
└── profiles/
    ├── speakers.json    name → SpeakerProfile metadata
    └── embeddings/
        └── <name>.npy   512-dim float32 voice embeddings (gitignored)
```

Config keys: `model`, `language`, `device`, `compute_type`, `vad_filter`, `timestamps`, `similarity_threshold`, `min_speakers`, `max_speakers`, `hf_token`, `hotwords`, `use_mlx`, `parallel_stages`, `llm_provider`, `llm_model`, `llm_endpoint`, `llm_temperature`, `anthropic_api_key`, `openai_api_key`, `google_api_key`.

> **`omegaconf` dependency note:** `omegaconf` is an undeclared transitive requirement of `pyannote-audio` — it is required at import time but not listed in pyannote's package metadata. `wisper-transcribe` declares it explicitly in `pyproject.toml` to ensure it is always installed.

---

## Test Strategy

- All tests in `tests/`, mirroring `src/wisper_transcribe/`
- **No GPU, no network, no real audio required.** All ML calls (WhisperModel, pyannote Pipeline, embedding extraction) are mocked with `unittest.mock.MagicMock`
- `audio_utils.load_wav_as_tensor` patched in diarizer and speaker_manager tests to return a fake `{'waveform': tensor, 'sample_rate': 16000}` dict
- `tqdm.write` used throughout production code so test output is not polluted by progress bars
- Enrollment tests patch `wisper_transcribe.speaker_manager.load_profiles` to return `{}` (no existing profiles) to prevent tests from seeing real profiles on the developer's machine
- Coverage: run `pytest tests/ -v --cov --cov-report=term-missing`
- Web tests use `fastapi.testclient.TestClient`; routes are tested via HTTP with all ML calls mocked — no GPU/network needed
- Security tests in `tests/test_path_traversal.py` cover path traversal (null-byte, dotdot), regex-busting payloads, open-redirect/CRLF payloads, and unit tests for `_validate_job_id()`
- OWASP regression tests in `tests/test_owasp.py` cover A03 XSS (markdown rendering via `_sanitize_html` + endpoint integration), A05 security response headers (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Content-Security-Policy`), and A09 no-stack-trace-in-error-response
- `tests/test_debug_log.py` covers `Logger` (file mode, verbose mode, combined), `setup_logging()`, singleton lifecycle, and `WISPER_DEBUG` env side-effect
- `tests/conftest.py` provides an `autouse` fixture that patches `wisper_transcribe.pipeline.load_config` with a safe baseline config (prevents real user config — e.g. `parallel_stages=True` — from leaking into tests that don't explicitly patch it)
- `tests/test_time_utils.py` covers shared `format_timestamp()` and `format_duration()` helpers
- `tests/test_noise_suppress.py` covers warning filters, logger levels, `WISPER_DEBUG` bypass, missing absl, speechbrain deprecations, checkpoint upgrade warnings, and module-level suppress placement in `diarizer.py` and `speaker_manager.py`
- `tests/test_refine.py` covers `parse_transcript` (frontmatter / no-frontmatter / invalid YAML), vocabulary edit-distance guard, `apply_edits` idempotency, unknown-speaker confidence filter + hallucinated-name rejection, `render_diff` plain/coloured, and `refine_transcript` frontmatter preservation
- `tests/test_summarize.py` covers structured-output parsing, enrolled-player NPC filtering, `render_markdown` section presence + placeholders, `[[wiki-link]]` rules (enrolled-only, whole-word, idempotent), `unresolved_speakers` section, and the `sections` filter
- `tests/test_llm_clients.py` mocks httpx for Ollama and injects fake `anthropic` / `openai` / `google.genai` modules via `sys.modules` to cover the lazy-import path; each client's `complete()` and `complete_json()` are tested for happy path + SDK error + missing-SDK → `LLMUnavailableError`; `ConnectError` raises with a "daemon not running" message; a 404 `HTTPStatusError` raises with a "not found in Ollama" message; a non-404 `HTTPStatusError` (e.g. 500) raises with a generic "Ollama request failed" message
- `tests/test_web_routes.py` covers web routes including refine/summarize job submission, summary sidecar rendering, summary download, summary-badge logic on the transcript list, deletion of summary sidecars alongside transcripts, LLM config field rendering, LLM config save (provider/model/temperature), non-empty API key save, empty API key not overwriting an existing key, Config nav link presence on the job detail page, and the `/config/ollama-status` endpoint (running + models, running + empty model list, not reachable, custom endpoint forwarded)
- `tests/test_config.py` covers `get_hf_token()` accepting `HF_TOKEN` as an alias for `HUGGINGFACE_TOKEN` and propagating whichever is set to both env vars
- `tests/test_web_jobs.py` covers job queue CRUD, tqdm patch/restore, error recording, cancellation, and a regression test that `job.status = COMPLETED` is not set until after `_run_post_process()` finishes
- Test count: 411 (all mocked, all passing)

**CI matrix** (`.github/workflows/ci.yml`):
- Runs on every push/PR: Python 3.10, 3.11, 3.12, 3.13 (blocking) + 3.14 (non-blocking, `continue-on-error: true`)
- Weekly cron (Monday): same matrix + `latest-deps` job (`pip install --upgrade`) to detect forward-compatibility breakage before it hits PRs
- `allow-prereleases: true` on `setup-python` so 3.14 resolves even if still in pre-release
- Dependabot monitors `pip`, `docker`, and `github-actions` ecosystems weekly

---

## Known Constraints

| Constraint | Detail |
|-----------|--------|
| torchcodec on Windows | Requires `Gyan.FFmpeg.Shared` (full-shared build). Currently bypassed by scipy waveform pre-loading in `diarize()` and `extract_embedding()`; torchcodec would work after installing the shared build and restarting |
| MPS on Apple Silicon | faster-whisper (CTranslate2) has no MPS backend. With `mlx-whisper` installed (`pip install 'wisper-transcribe[macos]'`), transcription uses the Apple Silicon GPU/ANE via MLX Whisper. Without it, transcription falls back to CPU. pyannote diarization and speaker embeddings always run on MPS when available. |
| Thread safety | `_model` and `_pipeline` globals are not thread-safe; parallel folder processing uses `ProcessPoolExecutor` so each worker is a separate process with isolated module state |
| pyannote license | HuggingFace token + one-time model license acceptance required (free) |

---

## Web Interface (Phase 11)

### Stack
FastAPI + Jinja2 + HTMX + Tailwind CSS. All assets served locally — no CDN or internet required at runtime.

| Layer | Choice | Notes |
|-------|--------|-------|
| Backend | FastAPI (uvicorn) | `wisper server` command; single-file app factory |
| Templates | Jinja2 (server-side) | Rendered HTML; HTMX handles partial updates |
| Reactive UI | HTMX 1.9 (vendored) | `static/htmx.min.js` committed; polled job updates |
| Styling | Tailwind CSS (compiled) | `static/tailwind.min.css` pre-built; regenerate with `pytailwindcss` |
| Icons | Heroicons (inline SVG) | Embedded in templates — no external load |

### Job Queue
`web/jobs.py` — `JobQueue` class with in-memory `dict[str, Job]` and an `asyncio.Queue` drain loop.
- One background asyncio task consumes the queue; each job runs `process_file()` via `asyncio.to_thread()`.
- One job at a time (GPU-safe) — the module-level `_model`/`_pipeline` globals are not thread-safe.
- Progress: `tqdm.write` is monkey-patched per-job to capture log lines into `job.log_lines`; `tqdm.__init__` is also patched to redirect the progress bar to `job.progress`; both are restored after completion. In parallel mode, `capturing_write` also detects `[progress:channel]` prefixed messages forwarded by the drain thread and routes them to `job.progress_channels[channel]` (a `dict[str, str]` keyed by `"transcribe"` / `"diarize"`) rather than `log_lines`.
- SSE endpoint (`GET /transcribe/jobs/{id}/stream`) streams `job.log_lines`, `job.progress`, `job.progress_channels` (as `channel_progress` events), and status to the browser.
- Job `name` is set to the uploaded file's stem so the UI displays a meaningful name instead of a temp-file UUID.
- Output is always written to the configured output directory (`./output` or `data_dir/output`) so the Transcripts page can find it.
- Cancel: `POST /transcribe/jobs/{id}/cancel` calls `JobQueue.cancel()`. Pending jobs are immediately marked failed. Running jobs set a `threading.Event` (`_cancel_event`) that is checked in the `tqdm.write` patch; when set, `InterruptedError` is raised to abort the pipeline thread cleanly.

### Speaker Enrollment Web Flow
Interactive CLI enrollment (TTY prompts) is replaced by a post-job wizard:
1. Transcription completes with `enroll_speakers=False`; detected speakers appear in transcript as `SPEAKER_XX` labels.
2. After `process_file()` returns, `_extract_speaker_excerpts()` parses the output markdown for each speaker's first timestamp and cuts a ~12s audio clip via ffmpeg, stored in `job.speaker_excerpts[speaker_name]`.
3. Dashboard shows "Name Speakers" button for completed jobs.
4. `GET /transcribe/jobs/{id}/enroll` renders a wizard page with each detected label, a name input (plus existing profiles as click-to-fill options), and a Play/Stop button if an audio excerpt is available.
5. `GET /transcribe/jobs/{id}/excerpt/{speaker_name}` serves the audio clip as `audio/mpeg`.
6. `POST /transcribe/jobs/{id}/enroll` applies speaker name renames via `formatter.update_speaker_names()`.

### Web Route Security

All web route handlers follow a consistent two-layer defence pattern enforced by CodeQL scanning on every PR:

**Path traversal (CWE-22) — transcript and speaker clip routes:**
1. `os.path.basename(user_input)` strips leading path components and is recognised by CodeQL as a path sanitiser.
2. `os.path.abspath(os.path.join(base, safe_name)).startswith(base + os.sep)` confirms the resolved path stays within the intended directory.
`Path.resolve()` on tainted input is **not** used — CodeQL does not recognise it as a sanitiser.

**Open redirect (CWE-601) — job ID routes (`cancel_job`, `enroll_form`, `enroll_submit`):**
`_validate_job_id(job_id)` in `transcribe.py` applies both layers:
1. `re.match(r"^[\w\-]+$", job_id)` rejects everything except alphanumeric/hyphen.
2. `os.path.basename(os.path.abspath(os.path.join("_guard", job_id)))` round-trip produces a string CodeQL's taint tracker treats as clean. `re.match().group(1)` alone is **still considered tainted** by CodeQL even after format validation; the `os.path` round-trip is required.

**Error messages:** Internal exception text is never placed in redirect URLs or error responses. Routes use generic error codes (e.g. `?error=enroll_failed`).

**Output directory:** The `start_transcribe` form handler ignores any user-supplied `output_dir` and always writes to `_default_output_dir()`. Accepting arbitrary paths from form data would allow writing outside the data directory.

**XSS (A03) — markdown rendering:**
`transcript_detail` renders transcript markdown to HTML and injects it with Jinja's `| safe` filter. Before injection, `_sanitize_html()` in `transcripts.py` strips `<script>` elements and `on*` event-handler attributes from the rendered HTML. This defends against a `fix-speaker` payload where a malicious speaker name containing raw HTML ends up in a transcript file on disk.

**Security response headers (A05):**
`_SecurityHeadersMiddleware` in `app.py` attaches the following headers to every response:
- `X-Content-Type-Options: nosniff` — prevents MIME-type sniffing
- `X-Frame-Options: SAMEORIGIN` — clickjacking protection
- `Referrer-Policy: strict-origin-when-cross-origin` — limits referrer leakage
- `Content-Security-Policy` — restricts resource origins; `script-src` currently includes `'unsafe-inline'` because several templates contain inline `<script>` blocks. Migrating those to `app.js` and switching to a nonce-based policy is a tracked hardening task.

### Transcript Filename Handling
Transcript filenames may contain arbitrary Unicode characters (spaces, em-dashes, parentheses, etc.). All URL path parameters that correspond to filenames use the **two-layer path guard** (basename + abspath/startswith) rather than an allowlist regex — allowlist regex would block valid unicode filenames. This allows episode titles like "Episode 2 – O Captain! My (Dead) Captain!" to work correctly.

URL-encoding is applied at every point where a filename is embedded in a URL or HTTP header:
- Templates use the `urlencode` Jinja2 filter (`routes/__init__.py`) for all `<a href>` links that include a file stem.
- Redirect `Location` headers are built with `urllib.parse.quote(name)` so latin-1 codec is never violated.
- JavaScript in `job_detail.html` uses `encodeURIComponent(stem)` when constructing the post-SSE transcript link.

### Progress Display (Web)
The job detail page shows a unified progress bar and step pills driven by SSE events from `GET /transcribe/jobs/{id}/stream`.

**Step pills:** Each active step type gets a colored pill (gray=pending, indigo+pulse=active, green=done). Steps shown depend on job type:
- Transcription-only: T → D → F
- Transcription with post-processing: T → D → F → R (if `post_refine`) → S (if `post_summarize`)
- Standalone refine: R only
- Standalone summarize: S only

**Single bar:** The bar is divided into equal slices, one per step. As each step's tqdm percentage arrives, it fills within that step's slice. Phase is detected from log keywords (`transcrib`, `diariz`, `format`, `refine`, `summariz`) to activate the correct step. For parallel mode (`channel_progress` events), each channel maps to its step slice.

**ETA and rate:** Parsed from tqdm progress strings and shown live below the bar. When no tqdm data arrives for ≥5 s (e.g. during LLM steps which have no tqdm), an estimator ticks the bar forward ~1% every 5 s up to 90% of the current step's slice, providing visual feedback until the `done` event fires.

**Parallel mode** (`parallel_stages=True`, `channel_progress` SSE events): T and D slices update from their respective channels concurrently. The bar shows whichever channel is further ahead.

### Transcript Management (Web)
- Transcripts list page: each card is fully clickable via the **overlay link pattern** (card is a `div` with an `absolute inset-0` `<a>` underneath, action buttons use `relative z-10` to sit above). This avoids the invalid-HTML problem of nesting `<form>` inside `<a>`.
- Summary sidecars (`.summary.md`) are **filtered out of the transcript list** — they appear only as a green notes icon and "Campaign notes available" label on their parent transcript's card.
- Delete: `POST /transcripts/{name}/delete` removes both the `.md` file and its `.summary.md` sidecar (if present) and redirects to `/transcripts`.
- Dashboard stat cards link to their respective sections (Active Jobs → `/transcribe`, Transcripts → `/transcripts`, Enrolled Speakers → `/speakers`).

### LLM Post-processing (Web)
- **Inline after transcription**: the `/transcribe` form has a "LLM Post-processing" checkbox group (`post_refine`, `post_summarize`). When checked, the options are stripped from kwargs before `process_file()` and stored as `Job.post_refine` / `Job.post_summarize`; after transcription completes, `_run_post_process()` chains into `_do_llm_work()` in the same job thread. LLM status messages (Ollama streaming output) are captured into `job.log_lines` via `_StderrCapture` (redirects `sys.stderr` for the job thread duration — safe because the queue runs one job at a time).
- **Standalone from transcript detail**: `POST /transcripts/{name}/refine` and `POST /transcripts/{name}/summarize` call `queue.submit_llm()`, which enqueues a `Job` with `job_type="refine"` or `"summarize"`. The browser is redirected to `/transcribe/jobs/{id}` — the same job detail / SSE streaming page used for transcription jobs. The job detail page suppresses the T/D/F step indicators for LLM jobs and shows a single step dot (R or S).
- **Campaign Notes page**: `GET /transcripts/{name}/summary` renders `.summary.md` as HTML with a metadata card (LLM provider/model, generated date, NPC chips) and a "Regenerate" button. `GET /transcripts/{name}/summary/download` serves the raw `.summary.md` file.
- **Job completion actions**: the SSE `done` event now includes `summary_path` and `job_type`; the JS in `job_detail.html` conditionally shows "View Campaign Notes" when `summary_path` is set and hides "Name Speakers" for non-transcription jobs.
- **LLM config page**: `GET/POST /config` exposes a dedicated "LLM Post-processing" card. Fields: `llm_provider` (select: ollama/anthropic/openai/google), `llm_model` (text), `llm_endpoint` (text, Ollama only), `llm_temperature` (number). Three secret fields (`anthropic_api_key`, `openai_api_key`, `google_api_key`) are password inputs that are **never overwritten with an empty submission** — leaving a key blank preserves the existing stored value. A JS snippet hides/shows the endpoint and cloud API key rows based on the selected provider. A note reminds users that env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`) take precedence over config-stored keys.
- **Ollama status + model picker**: `GET /config/ollama-status?endpoint=<url>` queries Ollama's `/api/tags` endpoint (3 s connect timeout) and returns `{"running": bool, "models": [{"name", "size"}]}`. When `ollama` is selected on the config page, JS calls this endpoint and renders a status badge ("✓ Ollama running · N models installed") and a `<select>` populated with installed models; choosing one fills the `llm_model` text input. A ↻ Refresh button and a `change` listener on the endpoint field trigger a re-check. When Ollama is unreachable the badge shows the error and the text input remains editable for manual entry.
- **Ollama error messages**: `OllamaClient._post_chat` distinguishes three failure modes: `httpx.ConnectError` → "Cannot connect to Ollama … daemon running?" message; `httpx.HTTPStatusError` 404 → "Model '…' not found in Ollama. Run: `ollama pull …`"; other `httpx.HTTPError` → generic failed message without the misleading daemon hint.

### Navigation Styling
Nav link styles (`nav-link`, `nav-active`, `nav-divider`, `mobile-nav-link`) are defined in `static/input.css` as Tailwind component-layer classes, not in an inline `<style>` block in `base.html`. This ensures they are included in the compiled `tailwind.min.css` and benefit from purging. Links display as pill buttons: transparent border at rest, `border-green-500 bg-green-800` on hover, `border-green-400 bg-green-800` for the active page. The nav bar uses `sticky top-0 z-50` so it remains visible when the user scrolls down on long pages (e.g. the job detail page with a full log terminal).

### Offline Assets
- `static/htmx.min.js`: placeholder committed to repo; real file downloaded during `docker build` via `curl`. For local use: `curl -sL https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js -o src/wisper_transcribe/static/htmx.min.js`
- `static/tailwind.min.css`: rebuilt automatically on server startup by `app._build_tailwind()` (mtime-checked; skips if already current). `pytailwindcss` is a main dependency (no Node.js required). Docker builds also invoke the build step so images are self-contained. Manual rebuild: `python -m pytailwindcss -i ./src/wisper_transcribe/static/input.css -o ./src/wisper_transcribe/static/tailwind.min.css --minify`

### Docker Web Services
`docker-compose.yml` defines four services: `wisper` / `wisper-cpu` (CLI) and `wisper-web` / `wisper-cpu-web` (web UI, port 8080). All services share a common YAML anchor (`x-volumes`, `x-env`) so volume mounts and environment variables are declared once. Environment variables (`HF_TOKEN`, `HUGGINGFACE_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`) are read from a `.env` file (copy `.env.example → .env`). `Makefile` provides `make start` / `make start-gpu` / `make stop` / `make logs` / `make build` targets as a convenience layer over `docker compose`.

### Distribution Launchers
Three double-click launcher scripts handle first-time setup and server start for end users:
- `start.command` (macOS) — `.command` extension opens Terminal on double-click; checks for `.venv`, calls `bash setup.sh` on first run, then starts `wisper server` and opens the browser.
- `start.bat` (Windows) — double-click batch file; calls `setup.ps1` on first run via `powershell -ExecutionPolicy Bypass`, then launches the server and opens `http://localhost:8080`.
- `start.sh` (Linux) — equivalent for Linux desktops with `xdg-open` for browser launch.
Both `start.command` and `start.sh` are committed with the execute bit set (`git update-index --chmod=+x`).

---

## HuggingFace Models

Downloaded once on first use, cached to `~/.cache/huggingface/hub/`. All subsequent runs are offline.

| Model | Purpose | Cache Size |
|-------|---------|-----------|
| `openai/whisper-*` (via faster-whisper) | Transcription | 75 MB – 1.5 GB |
| `pyannote/speaker-diarization-3.1` | Speaker diarization pipeline | ~400 MB |
| `pyannote/embedding` | Voice fingerprint extraction | ~200 MB |
| `pyannote/segmentation-3.0` | Voice activity detection | ~100 MB |

Required license agreements (free, one-time):
- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/embedding
- https://huggingface.co/pyannote/segmentation-3.0
