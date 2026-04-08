# wisper-transcribe — Architecture Reference

> **Keep this file current.** Update it whenever a new module is added, a key design decision changes, or the processing pipeline is modified.

---

## Tech Stack

| Component | Library | Purpose |
|-----------|---------|---------|
| Transcription | `faster-whisper` (CTranslate2) | 4× faster than openai/whisper, lower VRAM, lazy model caching; supports `hotwords` and `initial_prompt` for vocabulary guidance |
| Transcription (macOS) | `mlx-whisper` (optional) | Apple Silicon GPU/ANE backend; dispatched automatically when `use_mlx=auto` and `mlx-whisper` is installed on MPS devices |
| Diarization | `pyannote-audio 4.x` | Speaker segmentation + voice embeddings |
| Audio loading (diarizer) | `scipy.io.wavfile` | Bypasses `torchcodec` (see [Known Constraints](#known-constraints)) |
| Audio conversion | `pydub` + ffmpeg | Convert any format → 16kHz mono WAV |
| CLI | `click` | Command groups: `setup`, `transcribe`, `enroll`, `speakers`, `config`, `fix` |
| Config/storage | `platformdirs` + TOML | OS-native user data dirs, never hardcoded paths |
| Progress display | `tqdm` | Nested bars: folder-level (position=0), transcription (position=1) |
| GPU detection | `torch.cuda / torch.backends.mps` | Auto-selects CUDA → MPS → CPU at runtime |

---

## Module Map

```
src/wisper_transcribe/
├── cli.py              Click entry points — no business logic, delegates to pipeline/manager; includes setup wizard, server command
├── pipeline.py         Main orchestrator: process_file(), process_folder() (supports --workers N via ProcessPoolExecutor)
├── transcriber.py      faster-whisper wrapper, lazy model cache (_model), CUDA DLL path fix
├── diarizer.py         pyannote pipeline wrapper, lazy cache (_pipeline), scipy audio loading
├── aligner.py          Merge transcription segments with diarization labels (max-overlap)
├── speaker_manager.py  Profile CRUD, embedding extraction, cosine-similarity matching, EMA updates
├── formatter.py        Markdown output, YAML frontmatter, timestamp formatting
├── audio_utils.py      validate_audio(), convert_to_wav(), get_duration()
├── config.py           load_config(), save_config(), get_device(), get_hf_token(), check_ffmpeg()
├── models.py           Dataclasses: TranscriptionSegment, DiarizationSegment, AlignedSegment, SpeakerProfile
├── static/             Vendored web assets: htmx.min.js, tailwind.min.css (pre-built), wisp.svg, app.js
└── web/                Phase 11: FastAPI web UI
    ├── app.py          FastAPI application factory (create_app()), module-level app instance for uvicorn
    ├── jobs.py         In-memory job queue, JobQueue class, asyncio background worker, SSE progress via tqdm.write patch
    └── routes/
        ├── dashboard.py    GET /, GET /jobs (HTMX partial)
        ├── transcribe.py   GET/POST /transcribe, GET /transcribe/jobs/{id}, SSE /jobs/{id}/stream, enrollment wizard
        ├── transcripts.py  GET/POST /transcripts, transcript detail, download, delete, fix-speaker
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
pyannote-audio 4.x uses `torchcodec` for audio I/O by default. On Windows, torchcodec requires FFmpeg's "full-shared" build (`winget install Gyan.FFmpeg.Shared`) to load its native DLLs. Rather than make the full-shared build a hard requirement, `diarize()` and `extract_embedding()` pre-load the WAV file with `scipy.io.wavfile` and pass a `{'waveform': tensor, 'sample_rate': int}` dict to pyannote. When the dict contains `"waveform"`, pyannote's `Audio.__call__()` and `Audio.crop()` skip torchcodec entirely and operate on the tensor directly. The input is always a 16kHz mono WAV produced by `convert_to_wav()`.

### speechbrain LazyModule shim (Windows path bug)
speechbrain 1.0 lazy-loads optional integrations (k2, transformers, spacy, numba) via `LazyModule.ensure_module()`. The guard that suppresses lazy loads triggered by `inspect.getmembers()` checks for `"/inspect.py"` — a forward-slash check that never matches on Windows (which uses backslash). As a result, every missing optional integration raises `ImportError` instead of silently no-oping. `diarizer.py` patches `LazyModule.ensure_module` at import time to catch these `ImportError`s and return empty stub modules. This is the only compatibility shim remaining; it is in speechbrain itself, not pyannote.

### Module-level imports for mock patching
`pyannote.audio.Pipeline` is imported at the top of `diarizer.py` (not inside the function). `pydub.AudioSegment` is imported at the top of `audio_utils.py`. This is required so `unittest.mock.patch("wisper_transcribe.diarizer.Pipeline", ...)` resolves correctly in tests. Lazy imports inside functions cannot be patched at the module path.

### CUDA DLL path resolution (Windows)
`transcriber.load_model()` searches for `cublas64_12.dll` in PyTorch's `nvidia-cublas` site-packages directory and the system CUDA Toolkit before loading `WhisperModel`. CTranslate2 on Windows requires this DLL to be on `PATH` or added via `os.add_dll_directory()`.

### Third-party warning suppression (`WISPER_DEBUG`)
`diarizer.py` and `cli.py` suppress several classes of non-actionable warnings from speechbrain, pyannote, Lightning, and torch at module import time using `warnings.filterwarnings()`. All suppressions are gated on `not os.environ.get("WISPER_DEBUG")` so a developer can set `WISPER_DEBUG=1` in their shell to restore raw output for debugging. Suppressed warnings:
- speechbrain module-redirect deprecations (inspect.getmembers side effect)
- pyannote TF32 ReproducibilityWarning
- pyannote pooling std() UserWarning on short/silent segments
- Lightning migration shim: "Redirecting import of pytorch_lightning..." (checkpoint saved under old namespace)
- Lightning checkpoint auto-upgrade notification (v1.x → v2.x format)
- Lightning multiple ModelCheckpoint callback states in old checkpoint
- pyannote embedding model task-dependent loss function UserWarning (not used during inference)
- Lightning state dict missing keys warning (`loss_func.W` in checkpoint but not in inference model)

The absl "triton not found" flop-counter log is suppressed via `absl.logging.set_verbosity(ERROR)` — absl-py has its own logging system separate from Python's `logging` hierarchy, so `logging.getLogger("absl")` has no effect; must use `absl.logging` directly.

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
When `parallel_stages=True` in config (default `False`), `process_file()` runs transcription and diarization concurrently via `ProcessPoolExecutor(max_workers=2)`. The two stages are independent: both take the same converted WAV file as input and produce outputs that are combined in the `align()` step. Each subprocess gets its own copy of the module-level `_model`/`_pipeline` globals, so there are no thread-safety concerns.

Interaction with `--workers N` folder mode: when both `parallel_stages=True` and `workers>1` are active, the total process count is N×2. This is documented; users with high `--workers` values can set `parallel_stages=False` to avoid contention. The web job queue's one-job-at-a-time guarantee is unaffected because the inner `ProcessPoolExecutor` runs inside the `asyncio.to_thread()` call and does not interact with the event loop.

The helper function `_run_parallel_transcribe_diarize()` wraps the executor logic and is a module-level target for test mocking (`@patch("wisper_transcribe.pipeline._run_parallel_transcribe_diarize")`). `_transcribe_worker` and `_diarize_worker` are module-level (not closures) so they can be pickled by the executor.

### Module-level model caches
`_model` (transcriber) and `_pipeline` (diarizer) are module-level globals. This avoids reloading multi-GB models between files when processing a folder. The caches are intentionally reset to `None` in tests.

### Parallel folder processing (`--workers N`)
`process_folder()` accepts a `workers` parameter (default 1). When `workers > 1`, it uses `concurrent.futures.ProcessPoolExecutor` — **not** `ThreadPoolExecutor` — because `_model` and `_pipeline` are module-level globals that are not thread-safe. Each subprocess gets its own copy of the module, so globals are isolated. Guard: if the effective device resolves to anything other than `"cpu"` (after resolving `"auto"`), `workers` is clamped to 1 with a warning, because GPU memory cannot be shared across processes. CPU-only deployments (e.g. a batch server) can safely use multiple workers. `ProcessPoolExecutor` is imported at module level in `pipeline.py` so tests can patch it at `wisper_transcribe.pipeline.ProcessPoolExecutor`.

### pyproject.toml torch version
`torch>=2.8.0` is required because `pyannote-audio 4.x` declares this minimum. The CUDA build must be installed from `https://download.pytorch.org/whl/cu126` — PyPI only ships the CPU-only build. The `setup.ps1` script handles this automatically on Windows.

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

Config keys: `model`, `language`, `device`, `compute_type`, `vad_filter`, `timestamps`, `similarity_threshold`, `min_speakers`, `max_speakers`, `hf_token`, `hotwords`, `use_mlx`, `parallel_stages`.

---

## Test Strategy

- All tests in `tests/`, mirroring `src/wisper_transcribe/`
- **No GPU, no network, no real audio required.** All ML calls (WhisperModel, pyannote Pipeline, embedding extraction) are mocked with `unittest.mock.MagicMock`
- `scipy.io.wavfile.read` patched in diarizer tests to return a fake `(16000, np.zeros(...))` tuple
- `tqdm.write` used throughout production code so test output is not polluted by progress bars
- Enrollment tests patch `wisper_transcribe.speaker_manager.load_profiles` to return `{}` (no existing profiles) to prevent tests from seeing real profiles on the developer's machine
- Coverage: run `pytest tests/ -v --cov --cov-report=term-missing`
- Web tests use `fastapi.testclient.TestClient`; routes are tested via HTTP with all ML calls mocked — no GPU/network needed
- Security tests in `tests/test_path_traversal.py` cover path traversal (null-byte, dotdot), regex-busting payloads, open-redirect/CRLF payloads, and unit tests for `_validate_job_id()`
- Test count: 203 (all mocked, all passing)

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
- Progress: `tqdm.write` is monkey-patched per-job to capture log lines into `job.log_lines`; `tqdm.__init__` is also patched to redirect the progress bar to `job.progress`; both are restored after completion.
- SSE endpoint (`GET /transcribe/jobs/{id}/stream`) streams `job.log_lines`, `job.progress`, and status to the browser.
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

### Transcript Filename Handling
Transcript filenames may contain arbitrary Unicode characters (spaces, em-dashes, parentheses, etc.). All URL path parameters that correspond to filenames use the **two-layer path guard** (basename + abspath/startswith) rather than an allowlist regex — allowlist regex would block valid unicode filenames. This allows episode titles like "Episode 2 – O Captain! My (Dead) Captain!" to work correctly.

URL-encoding is applied at every point where a filename is embedded in a URL or HTTP header:
- Templates use the `urlencode` Jinja2 filter (`routes/__init__.py`) for all `<a href>` links that include a file stem.
- Redirect `Location` headers are built with `urllib.parse.quote(name)` so latin-1 codec is never violated.
- JavaScript in `job_detail.html` uses `encodeURIComponent(stem)` when constructing the post-SSE transcript link.

### Progress Display (Web)
The job detail page shows a live progress bar driven by SSE events from `GET /transcribe/jobs/{id}/stream`:
- Three step indicators (T / D / F) advance sequentially as phases complete.
- The overall bar maps per-phase tqdm percentages: transcription → 0–60%, diarization → 60–90%, formatting → 100% on done.
- ETA and speed counter (`5.2s/s`, `0.39chunk/s`) are parsed from the tqdm string and shown below the bar while a job is active, hidden on completion.
- Diarization bars now include `{rate_fmt}` (chunk/s) to match transcription output.

### Transcript Management (Web)
- Transcripts list page: each card is fully clickable via the **overlay link pattern** (card is a `div` with an `absolute inset-0` `<a>` underneath, action buttons use `relative z-10` to sit above). This avoids the invalid-HTML problem of nesting `<form>` inside `<a>`.
- Delete: `POST /transcripts/{name}/delete` removes the `.md` file and redirects to `/transcripts`.
- Dashboard stat cards link to their respective sections (Active Jobs → `/transcribe`, Transcripts → `/transcripts`, Enrolled Speakers → `/speakers`).

### Navigation Styling
Nav link styles (`nav-link`, `nav-active`, `nav-divider`, `mobile-nav-link`) are defined in `static/input.css` as Tailwind component-layer classes, not in an inline `<style>` block in `base.html`. This ensures they are included in the compiled `tailwind.min.css` and benefit from purging. Links display as pill buttons: transparent border at rest, `border-green-500 bg-green-800` on hover, `border-green-400 bg-green-800` for the active page.

### Offline Assets
- `static/htmx.min.js`: placeholder committed to repo; real file downloaded during `docker build` via `curl`. For local use: `curl -sL https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js -o src/wisper_transcribe/static/htmx.min.js`
- `static/tailwind.min.css`: rebuilt automatically on server startup by `app._build_tailwind()` (mtime-checked; skips if already current). `pytailwindcss` is a main dependency (no Node.js required). Docker builds also invoke the build step so images are self-contained. Manual rebuild: `python -m pytailwindcss -i ./src/wisper_transcribe/static/input.css -o ./src/wisper_transcribe/static/tailwind.min.css --minify`

### Docker Web Services
`docker-compose.yml` defines `wisper-web` (GPU) and `wisper-cpu-web` (CPU), both exposing port 8080. Same image as CLI services, different `command: ["server", "--host", "0.0.0.0", "--port", "8080"]`.

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
