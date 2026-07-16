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
| Audio conversion | ffmpeg (streaming) + `pydub` (`get_duration` fallback only) | Convert any format → 16kHz mono WAV; `get_duration()` tries `_probe_duration()` (ffprobe, header-only) first, then the stdlib `wave` module for `.wav` files, and only falls back to pydub (full decode) as a last resort |
| CLI | `click` | Command groups: `setup`, `transcribe`, `enroll`, `speakers`, `config`, `fix` |
| Config/storage | `platformdirs` + TOML | OS-native user data dirs, never hardcoded paths |
| Progress display | `tqdm` | Nested bars: folder-level (position=0), transcription (position=1) |
| GPU detection | `torch.cuda / torch.backends.mps` | Auto-selects CUDA → MPS → CPU at runtime |

---

## Module Map

```
src/wisper_transcribe/
├── cli.py              Click entry points — no business logic, delegates to pipeline/manager; includes setup wizard, server command; --debug and --verbose flags; wisper transcripts command group (list, move)
├── pipeline.py         Main orchestrator: process_file(), process_folder(); enrollment logic extracted into _interactive_enroll() and _prompt_speaker_name()
├── transcriber.py      faster-whisper wrapper, lazy model cache (_model), CUDA DLL path fix, MLX dispatch
├── diarizer.py         pyannote pipeline wrapper, lazy cache (_pipeline), uses load_wav_as_tensor() from audio_utils
├── _noise_suppress.py  Centralised third-party warning/logging suppression (Lightning, pyannote, speechbrain); safe to call from subprocesses
├── debug_log.py        Centralized logging controller (Logger class); activated by --debug (file) and/or --verbose (console); tees tqdm.write() + Python logging to ./logs/wisper_<ts>.log
├── aligner.py          Merge transcription segments with diarization labels; word-level splitting at turn boundaries when word timestamps are available, whole-segment max-overlap fallback otherwise (F8); micro-run smoothing pass (F13, `_smooth_word_speakers()`) absorbs a sandwiched run of ≤2 words or <1.0s between two same-speaker neighbors before grouping, so a jittered diarization boundary can't split one sentence across speaker blocks
├── speaker_manager.py  Profile CRUD, embedding extraction, cosine-similarity matching, EMA updates; enroll_speaker_from_audio_dir() for per-user track enrollment (Phase 6); enroll_speaker() accepts an optional precomputed embedding= (skips its internal extract_embedding() call) so callers can average embeddings from multiple raw diarization labels before saving; _select_embedding_segments() (F10b) picks up to 5 segments preferring non-overlapping ("solo") 2-20s spans over the raw-longest segments, falling back when nothing fits that profile
├── formatter.py        Markdown output, YAML frontmatter, dynamic version from __version__; parse_transcript_blocks() and rewrite_transcript_blocks() for per-line speaker editing (both match the timestamped `**Speaker** *(ts)*: text` and timestamp-free `**Speaker**: text` forms — the latter is what `include_timestamps=False` renders); rewrite_frontmatter_speakers() (F11) parses/re-dumps the YAML `speakers:` list for renames instead of regex, avoiding prefix collisions and quoted-name mismatches
├── audio_utils.py      validate_audio(), convert_to_wav(), get_duration(), load_wav_as_tensor(); VIDEO_EXTENSIONS / AUDIO_EXTENSIONS sets
├── time_utils.py       Shared time formatting: format_timestamp(), format_duration()
├── path_utils.py       Shared path utilities: validate_path_component() — four-step CodeQL-safe guard; all per-module validators delegate here. get_output_dir() — single canonical output directory resolver (replaces duplicates in transcribe + transcripts routes)
├── config.py           load_config(), save_config(), get_device(), get_hf_token(), get_llm_api_key(), resolve_llm_model(), check_ffmpeg()
├── campaign_manager.py Campaign CRUD: load/save/create/delete campaigns, add/remove roster members, bind_discord_id() / lookup_profile_by_discord_id() Discord ID binding, _validate_campaign_slug() / _validate_profile_key() delegate to path_utils
├── recording_manager.py Recording CRUD: load/save/create/delete recordings, append_segment() with per-recording mutex, reconcile_on_startup() crash recovery, _validate_recording_id() delegates to path_utils
├── web/discord_bot.py  BotManager: start()/stop() lifecycle, start_session()/stop_session(), _session_loop with auto-rejoin (backoff [2,5,15,30,60]s), _route_frame → SegmentedOggWriter + RealtimePCMMixer + auto-tag via lookup_profile_by_discord_id() + unbound_speakers tracking, _handle_disconnect (transient/permanent close code split), _finalise; injectable audio_source_factory for testing; _unix_socket_source launches JDA sidecar subprocess (Java 25, JDAVE 0.1.8), _find_sidecar_jar() for JAR discovery, _read_frame() parses length-prefixed wire protocol over Unix socket
├── web/routes/record.py Full recording UI: JSON API /api/record/{start,stop,status} + /api/record/channels (Discord REST proxy — lists guilds + voice channels via bot token), /api/recordings (list), /api/recordings/{id} (detail/transcribe/delete stubs) + HTML routes GET /record (includes channel-browser panel), POST /record/{start,stop}, GET /record/sse (SSE stream), GET /recordings (campaign-grouped list), GET /recordings/{id}, POST /recordings/{id}/enroll, POST /recordings/{id}/transcribe, POST /recordings/{id}/delete, GET /recordings/{id}/live (501 placeholder); _validate_recording_id() + _uid_guard path-traversal guards; Discord preset quick-select dropdown; pre-fills default guild/channel from config
├── models.py           Dataclasses: Word, TranscriptionSegment (+ words: Optional[list[Word]]), DiarizationSegment, AlignedSegment, SpeakerProfile, CampaignMember (+ discord_user_id), Campaign, Recording (+ unbound_speakers), SegmentRecord, RejoinAttempt, Edit, SpeakerSuggestion, LootChange, NPCMention, SummaryNote
├── refine.py           LLM-driven transcript refinement: vocabulary correction + unknown-speaker ID (edit-distance guarded, frontmatter-preserving)
├── summarize.py        Campaign-notes generation (session recap, loot, NPCs, follow-ups) → Obsidian-ready sidecar markdown
├── llm/                Provider-agnostic LLM client package (Ollama, LM Studio, Anthropic, OpenAI, Google)
│   ├── base.py         LLMClient ABC: complete() + complete_json(schema)
│   ├── errors.py       LLMUnavailableError (soft-fail) / LLMResponseError
│   ├── ollama.py       httpx streaming REST wrapper for local Ollama (/api/chat, NDJSON)
│   ├── lmstudio.py     httpx streaming REST wrapper for LM Studio (/v1/chat/completions, SSE)
│   ├── anthropic.py    Anthropic SDK; JSON via forced tool_use
│   ├── openai.py       OpenAI SDK; JSON via response_format json_schema strict mode
│   └── google.py       google-genai SDK; JSON via response_schema
├── static/             Vendored web assets: htmx.min.js, tailwind.min.css (pre-built), wisp.svg, app.js (global JS: wisperUpdateMeters, wisperTickerAppend, wisperPlayExcerpt inline audio player shared across Speakers and enrollment wizard pages)
└── web/                FastAPI web UI + Discord recording bot infrastructure
    ├── app.py          FastAPI application factory (create_app()), module-level app instance for uvicorn
    ├── jobs.py         In-memory job queue, JobQueue class, asyncio background worker, SSE progress via tqdm.write patch; LLM job types (refine/summarize) with stderr capture; on completion, moves a wisper_upload_* temp file next to the transcript as <stem><suffix> (durable across restarts, deletes it instead when there's no diarization data or the job fails/is cancelled — F5); _write_enrollment_sidecar() persists diarization segments + speaker_map (F7) + the (now durable) audio path to <stem>_diar.json alongside the transcript for restart-safe enrollment
    ├── audio_writer.py SegmentedOggWriter (rotating 60-s self-contained Ogg/Opus segments, packet-count rotation, crash-safe EOS pages), RealtimePCMMixer (48 kHz stereo → 16 kHz mono, clip-on-overflow)
    ├── enroll_shared.py Shared enrollment-wizard logic used by both routes/transcribe.py and routes/transcripts.py: resolve_current_names() (single entry point — sidecar speaker_map, falling back to build_legacy_label_map()'s interval match for legacy sidecars), template_current_names() (filters raw-label-valued entries for form prefill), apply_renames() (single-pass block-level rename, F6/F7) + enroll_profiles() (WAV convert + embed + campaign add) as the Phase 2.5 fast/slow split; find_excerpt_clip() (R24 — the one excerpt-clip disk lookup + CodeQL path guard both excerpt routes call)
    ├── _responses.py   Shared HTTP response helpers: invalid_input_response() (400 plain-text), error_redirect() (303 ?error= redirect); used by all route modules
    └── routes/
        ├── __init__.py     Jinja2 templates setup, shared get_queue() helper, urlencode filter
        ├── dashboard.py    GET /, GET /jobs (HTMX partial)
        ├── transcribe.py   GET/POST /transcribe (+ post_refine/post_summarize flags), GET /transcribe/jobs/{id}, SSE /jobs/{id}/stream, enrollment wizard
        ├── transcripts.py  GET/POST /transcripts (grouped by campaign), transcript detail (with campaign assignment dropdown), download, delete, fix-speaker; GET/POST /transcripts/{name}/edit (per-line speaker rename page); GET/POST /transcripts/{name}/enroll (transcript-centric enrollment wizard — restart-safe, reads _diar.json sidecar); GET /transcripts/{name}/excerpt/{speaker} (serves on-disk clip for enrollment wizard); POST /transcripts/{name}/refine, POST /transcripts/{name}/summarize; GET /transcripts/{name}/summary, GET /transcripts/{name}/summary/download; POST /transcripts/{name}/campaign (move/remove campaign association)
        ├── speakers.py     GET/POST /speakers, enroll (enqueues a standalone JOB_ENROLL job — R6), rename (rekey via speaker_manager.rename_profile — R31), remove; _waveform_bars() generates per-speaker deterministic pseudo-random waveform bar heights (LCG seeded from md5(key))
        ├── campaigns.py    GET/POST /campaigns, campaign detail, roster add/remove, delete; POST /campaigns/{slug}/transcripts/remove (unlink transcript from campaign without deleting the file)
        ├── config.py       GET/POST /config (+ Discord bot token, default guild/channel, presets management), POST /config/presets/add (inline preset save from Record page; validates guild/channel as snowflakes), GET /config/ollama-status, GET /config/lmstudio-status, GET /config/ollama-cloud-catalog, POST /config/{anthropic,openai,google}-models (cloud model discovery via SDK), POST /config/open-data-dir (opens data directory in OS file manager via `open`/`xdg-open`/`explorer`; POST because it is state-changing — R16)
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
   • All non-WAV inputs (audio + video): ffmpeg subprocess with
     -map 0:a:0 -ac 1 -ar 16000 -vn → first audio track only, 16kHz mono WAV
   • Streamed conversion avoids pydub's "Unable to process >4GB files"
     limit on long-form audio (multi-hour audiobooks, m4b)
   • 16kHz mono WAV inputs returned unchanged (header-only check via
     stdlib `wave`, never loads PCM data); other-rate WAVs re-encoded
   • Input file is never modified
    │
    ├──────────────────────────────────────────────────────┐
    ▼                                                      ▼
3. TRANSCRIBE       transcriber.transcribe()       4. DIARIZE   diarizer.diarize()
   • faster-whisper (CTranslate2) by default          • pyannote Pipeline (lazy-loaded, cached)
   • MLX Whisper on Apple Silicon if available        • Audio loaded via scipy.io.wavfile
     (use_mlx=auto + macOS + MPS + mlx-whisper)       • Passed as tensor dict to pipeline
   • word_timestamps=True on both backends            • Returns List[DiarizationSegment]
   • Returns List[TranscriptionSegment]                • Each segment: start, end, speaker label
   • Each segment: start, end, text, words (list[Word])
   Steps 3+4 run concurrently when parallel_stages=True (ProcessPoolExecutor; each subprocess isolates model globals)
    │                                                      │
    └──────────────────┬───────────────────────────────────┘
                       ▼
               5. ALIGN          aligner.align()
                  • Word-level strategy (F8): each word is assigned to the
                    diarization turn with max time overlap (nearest turn by
                    word-midpoint distance if none overlaps)
                  • Micro-run smoothing (F13, `_smooth_word_speakers()`):
                    diarization boundaries jitter by a word or two, so before
                    grouping, a run sandwiched between two SAME-speaker runs
                    (different from the run's own speaker) is absorbed into
                    them when it's short — ≤2 words OR <1.0s span (either
                    condition alone is enough; `_MICRO_RUN_MAX_WORDS` /
                    `_MICRO_RUN_MAX_SECONDS`). Runs at a segment's start/end
                    are never absorbed (no sandwich possible), and a run
                    between two DIFFERENT speakers always survives (genuine
                    interjection). Repeats to a fixpoint since absorbing one
                    run can expose another (e.g. `A B A B A` with tiny B runs
                    collapses to one A run)
                  • Consecutive same-speaker words (post-smoothing) are
                    grouped into one AlignedSegment per run, so a segment
                    spanning multiple speaker turns splits at the turn
                    boundary instead of being attributed wholesale to the
                    majority speaker, without fragmenting mid-sentence on
                    one- or two-word jitter
                  • Whole-segment max-overlap fallback for segments without
                    word data (None/empty `words` — legacy callers, mocks);
                    the smoothing pass only touches per-word speaker lists,
                    so this fallback is unaffected
                  • Unmatched words/segments labeled "UNKNOWN"
                  • Returns List[AlignedSegment]
                       │
                       ▼
               6. IDENTIFY       speaker_manager.match_speakers()
                  • Extract per-speaker voice embeddings from WAV
                  • Cosine similarity vs enrolled profiles, scored per (label, profile) pair
                  • Greedy assignment over all pairs, highest similarity first — a label
                    whose top choice is already claimed falls back to its next-best
                    unused profile above threshold (F4)
                  • allow_many_to_one=True (when num_speakers wasn't pinned) lets a
                    second label also claim an already-used profile, for pyannote
                    over-segmentation of one real speaker into two labels
                  • Below threshold → "Unknown Speaker N", numbered by sorted label order
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
6. For new speakers: `speaker_manager.extract_embedding()` slices the WAV to that speaker's segments and runs pyannote's embedding model; 512-dim numpy vector saved to `profiles/embeddings/<name>.npy`; metadata saved to `profiles/speakers.json`. Segment choice is delegated to `_select_embedding_segments()` (F10b): prefer up to 5 "solo" segments (no strict time-overlap with any other speaker's segment) in the 2.0-20.0s range, sorted longest-first; if none fall in that band, fall back to all solo segments longest-first; if there are no solo segments at all (constant cross-talk), fall back to the plain longest `speaker_segs` regardless of overlap. This avoids averaging in segments where tabletop cross-talk or background music bleeds into a diarization turn — previously the 5 longest segments were used unconditionally, which favoured exactly the segments most likely to contain that noise.

`_interactive_enroll()` (pipeline.py) caches each label's `extract_embedding()` result (up to 5 pyannote forward passes) in a per-call dict keyed by raw label, since the same label can be extracted up to three times in one wizard pass — once for step 3's ranking, again if the user updates an existing profile's embedding (step 4), and again if `enroll_speaker()` would otherwise re-extract for a brand-new profile. All three reuse the cached result when available.

`profiles/embeddings/<name>.mp3` — a short (~12s) reference clip saved alongside the embedding (`speaker_manager._save_reference_clip()`) for web playback on the Speakers page. `speaker_manager.remove_profile_files()`/`rename_profile_files()` delete/rekey both the `.npy` and `.mp3` together — used by `wisper speakers remove`/`rename` (CLI) and the web `/speakers/{name}/remove` route, so the play button never dangles after a removal or rename. `reset_profiles()` also globs `*.mp3` (not just `*.npy`) in the embeddings dir so a full `wisper speakers reset` doesn't leave every enrolled speaker's clip behind.

**Unified rename (R31):** `speaker_manager.rename_profile(old_key, new_name)` is the single rename implementation behind both `wisper speakers rename` (CLI) and `POST /speakers/{name}/rename` (web — which previously changed `display_name` only, so the same action did two different things depending on entry point). It (1) derives the new key via the standard convention and validates it with `validate_path_component` (the key becomes a filename and URL slug, and — for the web route — form data flows into file paths, so this also breaks the CodeQL taint chain), (2) enforces the CLI's collision guard (renaming onto a different existing key raises), (3) rekeys the profiles dict entry, updates `name`/`display_name`/`embedding_path`, and moves the `.npy`/`.mp3` files, and (4) rekeys campaign membership via `campaign_manager.rekey_member()` so roster entries — including per-campaign role/character overrides and Discord ID bindings — follow the profile instead of dangling. A same-key rename (display-name case tweak) skips the file move and campaign rekey. The web route maps `KeyError`/`ValueError` to a generic `?error=rename_failed` redirect, never reflecting the submitted name. Known remaining ripple (deliberate, matching prior CLI behavior): `recordings.json`'s per-recording `discord_speakers` values and already-written transcript display names are not rekeyed.

### Matching flow (subsequent runs)
1. Extract embedding for each detected speaker label
2. Score every (label, profile) pair via cosine similarity
3. Exclusive pass: consume pairs highest-similarity first, assigning whenever both the label and the profile are still free and similarity clears threshold — a label whose top choice was already claimed falls back to its next-best *unused* profile above threshold instead of skipping straight to Unknown (F4)
4. Many-to-one pass (`allow_many_to_one=True`, only when `num_speakers` wasn't pinned): any label still unassigned claims its single best profile even if another label already has it, still threshold-gated — for pyannote over-segmenting one real speaker into two labels
5. Similarity below threshold (default 0.65), or still unassigned after both passes → label kept as `"Unknown Speaker N"`, numbered by sorted label order

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

### Config sentinel-defaults (`process_file()`)
`process_file(model_size, language, include_timestamps, min_speakers, max_speakers, ...)` uses `None` as the *only* "unset — fall through to config, else a hardcoded fallback" marker: `model_size=None → config["model"] → "large-v3-turbo"`, `language=None → config["language"] → "en"`, `include_timestamps=None → config["timestamps"] → True`. A caller-supplied value — including one that happens to equal a hardcoded fallback (e.g. an explicit `model_size="medium"`) — always wins over config and is never re-overridden. This replaced an earlier bug where `"medium"`/`"en"` (the CLI's and web form's own literal defaults) doubled as the sentinel, which meant the `model`/`language` config keys were dead on the default path (CLI/web never actually passed `"medium"`), and a user who explicitly chose `medium` got silently overridden by config.

`language` has one extra wrinkle: the string `"auto"` is a distinct, explicit "auto-detect" marker — not the `None` sentinel — used by the CLI's `-l auto`/`--language auto` and passed through unresolved. It is only interpreted *after* the `None` → config resolution (so config's own `language` value may itself legitimately be `"auto"`), and always collapses to `None` right before `transcribe()`, which already treats a falsy language as auto-detect. The CLI itself no longer does the `"auto"` → `None` translation — it forwards whatever the user typed (or `None` if `--language` was omitted) straight through.

When diarization is enabled and the caller passes neither `num_speakers` nor `min_speakers`/`max_speakers`, `process_file()` falls back to config's `min_speakers`/`max_speakers` as a constraint on the diarizer call — previously dead keys. An explicit `num_speakers` (pinned count) suppresses this fallback entirely, since the user has already asserted an exact speaker count.

`device="auto"` and `compute_type="auto"` keep their own pre-existing, unrelated sentinel semantics (`"auto"` triggers device autodetection / compute-type resolution, not a config lookup) — they were already honest, since the CLI and web form always pass `"auto"` as their own literal default, so config's `compute_type` key was never actually dead. `vad_filter=None` also keeps its pre-existing "use config" semantics unchanged.

The web upload form (`routes/transcribe.py`) has no UI control for `language`/`device`/`compute_type`/timestamps — only `model_size` has a real (radio-button) control, which always submits an explicit value that wins over config by construction. The GET route preselects that radio from `config["model"]` (falling back to `large-v3-turbo` if the configured model isn't one of the three exposed choices). `device`/`compute_type` stay literal `"auto"` Form() defaults (matching `process_file()`'s own `"auto"` sentinel); `language`/`include_timestamps` default to `None` so the absent-field case still resolves through config.

### VAD filter via faster-whisper built-in
`transcribe()` passes `vad_filter=True/False` directly to `_model.transcribe()`. faster-whisper bundles Silero VAD internally; when enabled it skips silence/non-speech frames before feeding audio to Whisper. This is "Option A" from the plan — no separate audio stripping step, no timestamp remapping required. Timestamps in the output remain original-audio-relative. Controlled via `--vad/--no-vad` CLI flag (default: on, from config). `process_file()` uses `vad_filter: Optional[bool] = None` as a sentinel so an unset flag falls through to the config value rather than hard-coding True — see "Config sentinel-defaults" above for the full scheme this is part of.

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
`_model` (transcriber), `_pipeline` (diarizer), and `_embedding_model` (speaker_manager) are module-level globals. This avoids reloading multi-GB models between files when processing a folder. The caches are intentionally reset to `None` in tests.

**Cache keys (R4):** each cache records the parameters its cached object was loaded with, and the entry point reloads on mismatch — without this, the first job's model in the long-running web server would silently stick for every later job regardless of the model/device chosen in the upload form or config:
- `transcriber._model_key = (model_size, device, compute_type)` — the raw params as passed by the caller (`"auto"` resolves deterministically per device, so raw params are a stable key). `transcribe()` reloads when `_model is None or _model_key != key`.
- `diarizer._pipeline_device = device` — `diarize()` reloads when the requested device differs.
- `speaker_manager._embedding_device = device` — same for `_load_embedding_model()`.

**No poisoned cache on failure (R4):** all three loaders build the new object into a local variable and publish it to the module-level cache **only after** every step (device availability checks, the `.to(device)` move) has succeeded. `diarizer.load_pipeline()` previously assigned the global before the CUDA/MPS availability checks, so a failed load left a half-initialised CPU-placed pipeline cached, and the next `diarize()` silently ran on the wrong device. On reload, the old reference is dropped *before* constructing the replacement so peak memory never holds two models at once.

### Parallel folder processing (`--workers N`)
`process_folder()` accepts a `workers` parameter (default 1). When `workers > 1`, it uses `concurrent.futures.ProcessPoolExecutor` — **not** `ThreadPoolExecutor` — because `_model` and `_pipeline` are module-level globals that are not thread-safe. Each subprocess gets its own copy of the module, so globals are isolated. Guard: if the effective device resolves to anything other than `"cpu"` (after resolving `"auto"`), `workers` is clamped to 1 with a warning, because GPU memory cannot be shared across processes. CPU-only deployments (e.g. a batch server) can safely use multiple workers. `ProcessPoolExecutor` is imported at module level in `pipeline.py` so tests can patch it at `wisper_transcribe.pipeline.ProcessPoolExecutor`.

`process_folder()` returns `(successes, skipped, errors)` — a 3-tuple. Skip detection happens once, up front, via `_folder_output_path()` (mirrors `process_file()`'s own `out_dir / (stem + ".md")` logic): files whose output already exists (and `overwrite` is not set) are collected into `skipped` and never submitted to `process_file()`, sequentially or via the worker pool. This replaced a bug where the `workers > 1` path submitted every file regardless of existing output — `process_file()`'s own internal skip-check would then return the existing path without raising, which the caller's `successes` list happily accepted, silently miscounting a skip as a success. The CLI's `transcribe` command just prints `len(skipped)` — the fragile set-arithmetic that used to re-derive the skipped count from `path.iterdir()` minus `successes` minus files mentioned in `errors` is gone.

### pyproject.toml torch version
`torch>=2.8.0` is required because `pyannote-audio 4.x` declares this minimum. The CUDA build must be installed from `https://download.pytorch.org/whl/cu126` — PyPI only ships the CPU-only build. The `setup.ps1` script handles this automatically on Windows.

### LLM post-processing (`refine.py`, `summarize.py`, `llm/`)
Post-processing of an already-written transcript is split into two shapes:

- **`refine.py` — surgical.** `fix_vocabulary()` asks the LLM for `{original, corrected}` pairs in batches of ~25 lines, then validates each proposed substitution against the known hotwords + enrolled character names via `difflib.get_close_matches(..., cutoff=0.7)`. Freeform rewrites ("The party stepped in" → "The heroes proceeded") are rejected with a `UserWarning`. `identify_unknown_speakers()` runs a 20-line sliding window with 5-line overlap and only keeps suggestions with confidence ≥ 0.75 **and** a `suggested_name` that matches an enrolled `SpeakerProfile.display_name` — so the LLM cannot hallucinate new identities. Unknown-speaker suggestions are **never auto-applied**; they surface as rendered output only.
- **`summarize.py` — generative.** One structured-JSON call produces a `SummaryNote` (summary paragraph, loot list, NPC list, follow-ups). `render_markdown()` emits an Obsidian-compatible sidecar: YAML frontmatter (`type: session-summary`, `provider`, `model`, `refined`), then `## Summary / ## Loot & Inventory / ## NPCs / ## Follow-ups`. Names are wrapped in `[[wiki-links]]` only when they match an enrolled speaker's `display_name` or a name listed in their `notes` — unknown names stay plain to avoid creating orphan vault pages.

The `llm/` package wraps each provider behind a single `LLMClient` ABC with `complete(system, user)` and `complete_json(system, user, schema)`. Provider differences (Anthropic's forced `tool_use`, OpenAI's `response_format={"type": "json_schema", "strict": true}`, Google's `response_schema`, Ollama's `format="json"`) are entirely internal. SDKs are **lazy-imported inside each client class** so a user with only Ollama installed never hits an `anthropic`/`openai`/`google-genai` import error — missing package raises `LLMUnavailableError` with an install hint.

**Ollama streaming.** `OllamaClient._post_chat()` uses `httpx.stream()` with `read=None` (no per-chunk read timeout) so long transcripts never hit a read deadline mid-generation. A connect/write timeout (`self.timeout`, default 30 s) still guards against Ollama not being reachable. While streaming, a live dot-progress line is written to stderr (one `·` per 50 tokens) so the user can see the model is working. `wisper config llm` and `wisper setup` call `ollama list` via subprocess and display a numbered model picker when Ollama is running — falls back to a plain text prompt if the command is unavailable.

### Shared voice embeddings + per-campaign rosters
Full directory isolation per campaign would break cross-campaign recognition (re-enroll the same person for every game). Instead, campaigns are an **additive roster layer** over the global profile store. A `Campaign` holds a set of `profile_key` → `CampaignMember` entries; the voice embeddings in `profiles/embeddings/` remain global and are reused automatically. When `campaign=<slug>` is provided to `process_file()` or `match_speakers()`, a `profile_filter` set is computed via `get_campaign_profile_keys()` and passed to `match_speakers()`, which filters candidates before cosine-similarity scoring. `profile_filter=None` (default, when no campaign is specified) preserves the existing global-match behavior. Deleting a campaign never touches profiles or embeddings.

**`match_speakers()` assignment algorithm (F4).** Every (diarization label, enrolled profile) pair is cosine-scored up front — not just each label's single best profile. Pairs are sorted by similarity descending (ties broken by label then profile name for determinism) and consumed greedily in an **exclusive pass**: a pair is assigned when both the label and the profile are still free and its similarity clears `threshold`. Because the full pair list is available, a label whose top choice was already claimed by a higher-scoring label naturally falls through to its next-best *unused* profile, instead of going straight to "Unknown" (the old bug: each label recorded only its single best profile). An optional **many-to-one pass** (`allow_many_to_one: bool`) then lets any label still unassigned after the exclusive pass claim its single best profile even if another label already has it, still gated on `threshold` — this models pyannote over-segmenting one real speaker into two labels (e.g. `SPEAKER_00` + `SPEAKER_02`), which is common when `num_speakers` isn't pinned. Both call sites (`pipeline.process_file()`, `cli.py`'s `speakers test`) pass `allow_many_to_one=(num_speakers is None)` — pinning the count is the user asserting one label per person, so exclusivity is kept in that case. Labels still unassigned after both passes (failed embedding extraction, below threshold, or an exclusivity loser with many-to-one off) become `"Unknown Speaker N"`, numbered by sorted label order rather than similarity order, so numbering is deterministic regardless of scoring.

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
├── profiles/
│   ├── speakers.json    name → SpeakerProfile metadata (global — one entry per person)
│   └── embeddings/
│       └── <name>.npy   512-dim float32 voice embeddings (gitignored)
├── campaigns/
│   └── campaigns.json   slug → Campaign metadata + per-campaign roster (additive layer)
└── recordings/
    ├── recordings.json              index: recording_id → true (presence marker)
    └── <recording_id>/
        ├── metadata.json            full Recording dataclass + segment manifest (append-only)
        ├── per-user/
        │   └── <discord_user_id>/
        │       ├── 0000.opus        60-s self-contained Ogg/Opus segment (v1 file-format invariant 4)
        │       └── 0001.opus
        └── final/
            └── combined.wav         16 kHz mono PCM mix, written post-stop (copied to output/ before JobQueue submit)
```

### Recording layer

`recording_manager.py` mirrors `campaign_manager.py` in structure. Key design points:

- **Atomic saves** — `save_recording()` writes to a `NamedTemporaryFile` in the same directory then calls `os.replace()` (atomic on POSIX and Windows NTFS). Each call gets a unique temp filename to avoid collision under concurrent threads.
- **Per-recording mutex** — `append_segment()` acquires a `threading.Lock` keyed by `recording_id` so concurrent mixed + per-user writers cannot produce lost-update races on the manifest.
- **Crash recovery** — `reconcile_on_startup()` is called from `app.py` lifespan on server start. Any recording in `"recording"` or `"degraded"` status was active when the server crashed; it is marked `"failed"` with `ended_at = now`. Audio segments on disk are preserved.
- **Security** — `_validate_recording_id()` follows the four-step CodeQL Pattern 2: null-byte check → `os.path.basename` strip → regex `^[\w\-]+$` → `os.path.abspath` round-trip to break the taint chain.

`web/audio_writer.py` provides:
- **`SegmentedOggWriter`** — writes Opus packets into rotating self-contained Ogg files. Rotation is triggered by packet count (media time) rather than wall-clock time, so tests that feed packets faster than real-time work correctly. On construction, it scans the target directory for existing `*.opus` files and starts at the next index, enabling crash recovery by a new writer instance.
- **`RealtimePCMMixer`** — accumulates 48 kHz stereo 16-bit PCM frames from multiple Discord users and mixes them to 16 kHz mono 16-bit output suitable for Whisper.

**Five v1 file-format invariants (versioned contract for future live transcription in v2):**
1. Each segment file is a self-contained Ogg/Opus container with a valid EOS page.
2. Segment manifest is append-only and atomic (per-recording mutex + atomic file replace).
3. Segment length ≤ 60 s (3000 packets × 20 ms).
4. Per-user directory layout `recordings/<id>/per-user/<discord_id>/NNNN.opus` is fixed.
5. `Recording.status` has a distinct `"recording"` state (v2 live ticker watches for new segments only while status is `"recording"` or `"degraded"`).

**Transcribe hand-off (Phase 7):** `POST /recordings/{id}/transcribe` copies `combined.wav` into the output directory and calls `job_queue.submit()` with `original_stem=recording_id`, `campaign=recording.campaign_slug`. A post-completion callback (`on_complete`) sets `Recording.status` to `"transcribed"`, records the transcript path, and calls `move_transcript_to_campaign()` to auto-associate the output with the campaign. `Recording.job_id` tracks the corresponding `Job.id` for the UI to link to job status. `Recording` statuses now include `"transcribing"` and `"transcribed"`.

**Campaign data model:** Campaigns hold rosters of `profile_key` references to the global `speakers.json`. Voice embeddings remain global — adding a speaker to a second campaign reuses their existing `.npy` automatically (voice transfer). Deleting a campaign never touches profiles or embeddings. `campaigns.json` absent on first run → `load_campaigns()` returns `{}`.

**Transcript output directory** (`data_dir/output/` or `./output/`):
```
output/
├── <stem>.md                       transcript with YAML frontmatter
├── <stem><suffix>                  durable copy of a web-uploaded source file (F5; mp3/wav/m4a/… — only present when the job had diarization data), moved here from the tempdir at job completion
├── <stem>.summary.md               LLM campaign-notes sidecar (optional)
├── <stem>_diar.json                enrollment sidecar: diarization_segments, speaker_map, input_path (now the durable <stem><suffix> path above), campaign slug
├── <stem>_excerpt_<speaker>.mp3    up to ~12 s audio clip per detected speaker, clamped to their longest solo diarization turn (F12; for enrollment wizard)
└── <stem>_excerpt_<speaker>.txt    that speaker's aligned word-runs overlapping the clip window, joined in time order (F12; shown in enrollment wizard)
```
The `_diar.json` sidecar is written by `_write_enrollment_sidecar()` in `jobs.py` when a transcription job completes with diarization results. It is the key artifact that makes `GET/POST /transcripts/{name}/enroll` restart-safe — the transcript-centric enrollment wizard reads it instead of relying on in-memory job state. Since F5, `input_path` in the sidecar always points at the durable `<stem><suffix>` copy (when the job came from a web upload) rather than a tempdir path that a later server restart could sweep away. Since F7 (phase 3), the sidecar also carries `speaker_map` — the exact raw `SPEAKER_XX` → display-name mapping `pipeline.process_file()` handed the formatter when it wrote the transcript (round-tripped through `Job.speaker_map`, populated from `_result_store["speaker_map"]`). This is the *authoritative* source for "what does this raw label currently display as" — see `enroll_shared.resolve_current_names()`. Sidecars written before this key existed simply lack it; callers fall back to the older interval-matching heuristic in that case. `apply_renames()` updates `speaker_map` in place after every successful rename, so it stays authoritative across repeated wizard visits. Deleting a transcript (`POST /transcripts/{name}/delete`, and the bulk-delete route) also deletes `<stem>_diar.json` and the `<stem><suffix>` audio file it references — that audio exists only to back the now-deleted transcript's enrollment wizard, so leaving it in the output dir would be a permanent leak. The deletion only ever targets a path that resolves inside the output dir (an `os.path.abspath` + `startswith` guard), so a legacy pre-F5 sidecar still pointing at a tempdir path is left alone.

Config keys: `model`, `language`, `device`, `compute_type`, `vad_filter`, `timestamps`, `similarity_threshold`, `min_speakers`, `max_speakers`, `hf_token`, `hotwords`, `use_mlx`, `parallel_stages`, `llm_provider`, `llm_model`, `llm_endpoint`, `llm_temperature`, `anthropic_api_key`, `openai_api_key`, `google_api_key`, `discord_bot_token`, `discord_default_guild`, `discord_default_channel`, `discord_presets`.

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
- `tests/test_lmstudio_client.py` covers `LMStudioClient` happy paths (`complete`, `complete_json`, SSE token accumulation, `json_object` response format), all three error branches (ConnectError, 404, non-404), bad JSON → `LLMResponseError`, and non-SSE line filtering; also tests `get_client("lmstudio")` wiring and default endpoint
- `tests/test_audio_utils.py` covers `validate_audio` (missing file, unsupported extension, all supported extensions including all video formats, case-insensitive), `convert_to_wav` (already-correct 16 kHz mono WAV passthrough via stdlib `wave`, wrong-rate WAV re-encoded via ffmpeg, mp3 routed through streaming ffmpeg, all 10 video extensions trigger the ffmpeg Popen path with correct `-map 0:a:0 -progress pipe:1` args, progress lines drive tqdm bar, ffmpeg failure → `ValueError` and cleans up any partial output WAV [R9-3], missing ffmpeg → `RuntimeError`), `_probe_duration` (ffprobe mock), `get_duration` (R26: prefers ffprobe, falls back to the `wave` header for `.wav`, falls back to pydub last — including an unreadable/non-PCM `.wav`), and `load_wav_as_tensor` (mono/stereo/float32); tqdm output suppressed via `TQDM_DISABLE` autouse fixture
- `tests/test_web_routes.py` covers web routes including video file uploads (mp4, mkv, mov, webm accepted and queued), refine/summarize job submission, summary sidecar rendering, summary download, summary-badge logic on the transcript list, deletion of summary sidecars alongside transcripts (plus R9-4: deletion of `<stem>_excerpt_*.mp3`/`.txt` clips in both single and bulk delete, scoped so a similarly-named transcript's clips survive, and a regression test that a stem containing glob metacharacters — e.g. an upload literally named `mix*.mp3` — is `glob.escape()`-d rather than treated as a wildcard that would also match an unrelated transcript's clips), LLM config field rendering, LLM config save (provider/model/temperature), non-empty API key save, empty API key not overwriting an existing key, Config nav link presence on the job detail page, the `/config/ollama-status` and `/config/lmstudio-status` endpoints, full campaign CRUD routes (`/campaigns`, `/campaigns/{slug}`, member add/remove, campaign delete) including create-then-redirect via server-generated slug and transcribe form campaign select, R10's chunked upload streaming (large payload spanning multiple chunks lands byte-for-byte; empty upload still queues), the R6 standalone-enroll hand-off (`/speakers/enroll` enqueues a JOB_ENROLL job, upload renamed to `wisper_enrollsrc_<job-id>` and cleaned up by the job — runner-level success/failure/cleanup covered in `tests/test_web_jobs.py`), the startup sweep recognizing `wisper_upload_*`/`wisper_enroll_*`/`wisper_enrollsrc_*`, R9-5's `.mp3` reference-clip deletion alongside `.npy` on speaker removal, R31's web rename rekey semantics (file moves, campaign membership, collision/invalid-name generic errors), and R14's SSE stream index translation after `log_lines` trimming
- `tests/test_config.py` covers `get_hf_token()` accepting `HF_TOKEN` as an alias for `HUGGINGFACE_TOKEN` and propagating whichever is set to both env vars
- `tests/test_web_jobs.py` covers job queue CRUD, tqdm patch/restore, error recording, cancellation, a regression test that `job.status = COMPLETED` is not set until after `_run_post_process()` finishes, and R14's retention/log-line caps (`append_log()` trims oldest lines past `_MAX_LOG_LINES` and tracks `log_lines_dropped`, `_prune_finished_jobs()` caps `COMPLETED`/`FAILED` jobs at `_MAX_RETAINED_JOBS` oldest-first while never touching `PENDING`/`RUNNING`, and cancelling a `PENDING` job also triggers a prune)
- `tests/test_campaign_manager.py` covers load/save roundtrip, `create_campaign` (slug generation, duplicate rejection, empty-name rejection), `delete_campaign` (profile files untouched), `add_member` / `remove_member`, `get_campaign_profile_keys`, `_make_slug` punctuation stripping, `_validate_campaign_slug` (parametrized accept/reject with null-byte, dotdot, slash, CRLF payloads), `bind_discord_id` persistence + one-to-one overwrite enforcement, and `lookup_profile_by_discord_id` (known ID returns profile key, unknown ID returns None)
- `tests/test_path_traversal.py` covers path traversal (null-byte, dotdot), regex-busting payloads, open-redirect/CRLF payloads, unit tests for `_validate_job_id()`, recording-ID path traversal for JSON API + HTML routes, `_validate_recording_id()` unit tests, campaign-slug path traversal, and `_validate_campaign_slug()` unit tests
- `tests/test_recording_manager.py` covers load/save roundtrip, UUID generation, corrupt index handling, missing metadata skip, status updates, concurrent `append_segment` (threading), crash recovery via `reconcile_on_startup`, and `_validate_recording_id` (parametrized accept/reject with null-byte, dotdot, slash, wildcard payloads)
- `tests/test_audio_writer.py` covers `SegmentedOggWriter` rotation at 60 s, three-segment sessions, EOS page flag verification, crash-recovery (second writer resumes from next index), write return values, and `RealtimePCMMixer` (single user, clear-after-mix, clip-on-overflow, silence)
- `tests/test_record_routes.py` covers start (201 + recording_id), missing voice_channel_id (400), stop with no session (400), path-traversal rejection, server.json lifecycle (written on startup, deleted on shutdown), GET /record returns 200, GET /recordings empty state + campaign-grouped list, GET /recordings/{id} detail + unknown-id 303 redirect, POST /recordings/{id}/delete removes entry, GET /recordings/{id}/live returns 501, POST /recordings/{id}/enroll (valid → 303 + profile created, invalid discord_user_id → 400, user not in unbound_speakers → 409)
- `tests/test_record_cli.py` covers "server not running" error, server.json discovery → HTTP POST, WISPER_SERVER_URL env var override, list output, recording_id validation, stop → server POST, transcribe with path-traversal guard, delete with path-traversal guard, start missing --voice-channel error, show/transcribe valid IDs request server, token masking in config show, config discord wizard prompts, empty-input preserves existing token (14 tests)
- `tests/test_discord_bot.py` covers BotManager start/stop lifecycle, start_session recording persisted, per-user .opus files written from PCM frames, transient 4015 rejoin logged, exhausted retries → degraded, permanent 4014 → failed (no retry), stop_session → completed, known Discord ID auto-tagged to profile key on first frame, unknown Discord ID gets empty string, unknown Discord ID added to unbound_speakers, known Discord ID NOT added to unbound_speakers, 3-user simultaneous interleaved frames → all per-user dirs populated, 3 unknown speakers all in unbound list (no duplicates), simultaneous known+unknown speakers (tagged vs unbound split); all via injected fake audio sources (no real JDA/Discord) (14 tests)
- `tests/_discord_fakes.py`: scripted_source, multi_attempt_source, infinite_disconnect_source, blocking_source factories + make_pcm_frame / make_disconnect_frame helpers
**CI matrix** (`.github/workflows/ci.yml`):
- Runs on every push/PR: Python 3.13 and 3.14, both blocking — the only versions the project ships on (Docker `python:3.14-slim`; local-`.venv` install floor 3.13 per `requires-python`). We deliberately do not fan out across versions we don't ship.
- Weekly cron (Monday): same matrix + `latest-deps` job (`pip install --upgrade`, 3.14) to detect forward-compatibility breakage before it hits PRs
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
| Unauthenticated recording API | `POST /api/record/start` and `/stop` have no auth layer. v1 deployment assumes local/trusted network access only. A single-recording lock prevents concurrent abuse; Discord enforces channel join permissions server-side. Web auth is deferred to v2. |
| Unbounded recording sessions | `BotManager._session_loop()` runs until explicitly stopped — sessions routinely last multiple hours. Disk usage scales linearly (~60 Ogg segments/hour/user). Operator is responsible for stopping sessions. |

---

## Web Interface (Phase 11)

### Stack
FastAPI + Jinja2 + HTMX + Tailwind CSS v4. All assets served locally — no CDN or internet required at runtime.

| Layer | Choice | Notes |
|-------|--------|-------|
| Backend | FastAPI (uvicorn) | `wisper server` command; single-file app factory |
| Templates | Jinja2 (server-side) | Rendered HTML; HTMX handles partial updates |
| Reactive UI | HTMX 1.9 (vendored) | `static/htmx.min.js` committed; polled job updates |
| Styling | Tailwind CSS v4 (compiled) | `static/tailwind.min.css` pre-built; design tokens in `static/input.css` via `@theme` |
| Fonts | Newsreader, Geist, JetBrains Mono | Self-hosted woff2 in `static/fonts/` (SIL OFL); downloaded once at setup |
| Icons | Custom SVG macro set | `partials/icons.html` Jinja2 macros — no external load |

### Studio Design System

The UI uses the **Studio** design direction: persistent 204 px left sidebar, dark near-black background (`#0b0f17`), paper-cream text (`#f3ead8`), and cyan (`#5fd4e7`) as the sole saturated accent reserved for live/active states.

Design tokens are defined in `static/input.css` under `@theme` (Tailwind v4 CSS-first approach):
- **Backgrounds**: `--color-ink-900` (bgSunken) → `--color-ink-600` (bgRaised2)
- **Text**: `--color-paper` / `--color-paper-dim` / `--color-paper-faint`
- **Borders**: `--color-rule` (rgba hairline) / `--color-rule-strong`
- **Accent**: `--color-accent` (cyan) / `--color-accent-deep`
- **Signals**: `--color-signal-green` / `--color-signal-amber` / `--color-signal-rose`
- **Fonts**: `--font-serif` (Newsreader) / `--font-sans` (Geist) / `--font-mono` (JetBrains Mono)

Component classes (`sidebar`, `toolbar`, `section-head`, `hairline-*`, `pill-*`, `btn`, `filter-pill`, `tab-item`, etc.) are defined in `@layer components` in `input.css`.

**Template structure**: all pages extend `base.html` which provides the sidebar and a `{% block toolbar %}` + `{% block page %}` slot. The sidebar (`partials/sidebar.html`) determines the active nav item from `request.url.path` and polls `/api/sidebar-status` every 5 seconds for live device/job counts.

### Job Queue
`web/jobs.py` — `JobQueue` class with in-memory `dict[str, Job]` and an `asyncio.Queue` drain loop.
- One background asyncio task consumes the queue; each job runs `process_file()` (transcription), the LLM pipeline (`JOB_REFINE`/`JOB_SUMMARIZE`), or `enroll_shared.enroll_profiles()` (`JOB_ENROLL`) via `asyncio.to_thread()`.
- One job at a time (GPU-safe) — the module-level `_model`/`_pipeline` globals are not thread-safe.
- **`JOB_ENROLL` (Phase 2.5 + R6):** all speaker-enrollment work runs as jobs. `Job.enroll_mode` selects one of three runners in `_run_enroll_job()`:
  - **`"wizard"`** (the original Phase 2.5 flow) — the enrollment wizard's slow half. `submit_enroll(md_path, transcript_name, groups, device)` carries only the transcript path, the validated rename groups (`display_name -> [raw_label, ...]`, from `enroll_shared.apply_renames()`), and the device — deliberately not the diarization segments or source audio path, to keep the job payload small. `output_path` is set to `md_path` immediately at submit time (not just on completion, unlike the LLM jobs) so "View transcript" works while the job is still running — the rename already happened synchronously in the route; only embedding extraction is pending. `_run_wizard_enroll()` re-reads the transcript's `<stem>_diar.json` sidecar for `segments`/`input_path`/`campaign` (restart-irrelevant since the queue itself is in-memory anyway) and calls `enroll_profiles()` with a `progress` callback that appends straight into `job.log_lines` (no tqdm/stderr capture needed, unlike transcription/LLM jobs).
  - **`"standalone"`** (R6) — the `/speakers/enroll` upload flow, which previously ran `convert_to_wav` + `diarize` + `extract_embedding` synchronously inside the `async def` route: that blocked the entire event loop (UI, SSE, everything) for minutes AND mutated the module-level ML caches concurrently with any running job's worker thread, violating the one-job-at-a-time invariant. `submit_standalone_enroll(upload_path, profile_key=…, display_name=…, role=…, notes=…, update=…)` renames the `wisper_enroll_*` temp upload to `wisper_enrollsrc_<job-id>` at submit time (see Startup Cleanup below) and `_run_standalone_enroll()` does the full convert → diarize → primary-speaker pick (most total speech time) → enroll/EMA-update sequence in the job thread, deleting the upload and converted WAV in a `finally` — success or failure (R9-1's cleanup obligation moved into the job with the work).
  - **`"recording"`** (R6) — the `/recordings/{id}/enroll` unbound-Discord-speaker flow (previously synchronous pydub decode + embedding extraction in the route). `submit_recording_enroll(recording_id=…, discord_uid=…, per_user_dir=…, profile_key=…, display_name=…)` carries only strings; `_run_recording_enroll()` calls `speaker_manager.enroll_speaker_from_audio_dir()` and, on success, applies the recording-state updates the route used to do inline (remove the uid from `unbound_speakers`, bind it in `discord_speakers`, add/bind the profile in the recording's campaign — those follow-ups are best-effort and logged, never failing an already-successful enrollment). The per-user audio dir is durable recording storage and is never deleted by the job.
  All three routes redirect (303) to `/transcribe/jobs/{job.id}` — the job detail page already renders `job_type == "enroll"` with a single E step.
- **Enroll-job error policy:** on missing source audio, or on any exception, an enroll job fails with a **generic** error string (`"Source audio not available"` / `"No speech detected in the uploaded audio"` / `"Enrollment failed"`) — never `str(exc)`, since `job.error` renders directly into the job detail page's HTML and a WAV-conversion exception message can contain a filesystem path. Unlike `_run_llm_job`, the exception is **not** re-raised after being caught (the real exception is logged server-side).
- **Generic job errors for ALL job types (R13):** the enroll-job policy above now applies everywhere `job.error` is set. `_set_job_error(job, exc)` maps exceptions to user-facing strings — `InterruptedError` → `"Cancelled"` (literal preserved; other code and the template check for it), `FileNotFoundError` → `"Input file not found"`, everything else → `"Transcription failed — see server logs"` / `"Post-processing failed — see server logs"` per job type — and logs the real exception with its traceback via `logging`. The worker loop's own except-block keeps a runner-set generic message rather than overwriting it. The `_run_post_process` catch-all log line is generic for the same reason (`job.log_lines` also renders in the UI).
- Progress: `tqdm.write` is monkey-patched per-job to capture log lines into `job.log_lines`; `tqdm.__init__` is also patched to redirect the progress bar to `job.progress`; both are restored after completion. In parallel mode, `capturing_write` also detects `[progress:channel]` prefixed messages forwarded by the drain thread and routes them to `job.progress_channels[channel]` (a `dict[str, str]` keyed by `"transcribe"` / `"diarize"`) rather than `log_lines`.
- SSE endpoint (`GET /transcribe/jobs/{id}/stream`) streams `job.log_lines`, `job.progress`, `job.progress_channels` (as `channel_progress` events), and status to the browser.
- Job `name` is set to the uploaded file's stem so the UI displays a meaningful name instead of a temp-file UUID.
- Output is always written to the configured output directory (`./output` or `data_dir/output`) so the Transcripts page can find it.
- Cancel: `POST /transcribe/jobs/{id}/cancel` calls `JobQueue.cancel()`. Pending jobs are immediately marked failed. Running jobs set a `threading.Event` (`_cancel_event`) that is checked in the `tqdm.write` patch; when set, `InterruptedError` is raised to abort the pipeline thread cleanly.
- **Durable audio (F5):** `Job.is_web_upload` is set in `JobQueue.submit()` from the *original* `wisper_upload_*` basename, before the friendly-name rename (`<original_stem><suffix>`, still in the tempdir) strips that prefix — the flag, not the current basename, is what later code checks. On successful completion with diarization data, `_run_transcription_job` moves that file into the transcript's output directory as `<stem><suffix>` (collision-safe: appends `_1`, `_2`, … before the suffix) and updates `job.input_path` to the durable path before `_write_enrollment_sidecar()` runs. If the job completed with **no** diarization data (e.g. `--no-diarize` uploads), or the job **fails or is cancelled**, the temp file is deleted instead — there's no `_diar.json` in the no-diarize case to ever record a moved file's path, so moving it would just leak it in the output dir forever. `job.is_web_upload` is cleared immediately after this decision so a later failure (e.g. in chained LLM post-processing) can never delete the now-durable, transcript-adjacent audio copy. Recording-sourced and other non-tempdir inputs are never touched.
- **Converted WAV cleanup (R9-2):** `pipeline.process_file()` wraps everything after `convert_to_wav(path)` in a `try`/`finally` that unlinks the converted WAV (only when it differs from the original input — a passthrough WAV is never deleted) once transcription/diarization/enrollment no longer need it, success or failure. This is process_file's own local temp file, never referenced by the `_diar.json` sidecar (which always records the *original* input path via `job.input_path`, not the converted copy).
- **Retention caps (R14):** `_jobs` is an in-memory `dict` that would otherwise grow forever. `JobQueue._prune_finished_jobs()` runs after every job leaves `RUNNING` (in `_worker()`'s `finally`) and after `cancel()` fails a `PENDING` job directly; it caps retained `COMPLETED`/`FAILED` jobs at `_MAX_RETAINED_JOBS` (50), dropping the oldest terminal jobs first — `PENDING`/`RUNNING` jobs are never pruned regardless of the terminal-job count. A pruned job simply disappears from the dashboard/jobs list. Separately, `Job.append_log()` caps `log_lines` at `_MAX_LOG_LINES` (1000), trimming from the front and counting drops in `log_lines_dropped` — every `job.log_lines.append(...)` call site goes through this method instead. The SSE stream (`GET /transcribe/jobs/{id}/stream`) translates its absolute line-index bookkeeping (`last_line_idx`) against `log_lines_dropped` so trimming never desyncs the stream; a client that fell more than 1000 lines behind just resumes from whatever's still retained. Both caps are module-level constants, not config keys — they're internal resource limits, not something a user needs to tune.

### Upload Progress (Web)
`transcribe.html` submits via `XMLHttpRequest` instead of a native form POST so it can show byte-level progress for large source files (multi-GB video/audio) — the server-side route (`start_transcribe` in `transcribe.py`) is unchanged; only the client-side transport differs.
- `xhr.upload.addEventListener('progress', ...)` drives a `#upload-progress-bar` fill and `N%` label while bytes are in flight (this tracks the HTTP request body, not job/transcription progress — that's the separate SSE-driven bar described below, which only starts once the job exists).
- `xhr.upload`'s `load` event (all bytes sent, response not yet received) switches the label to "Processing…" so the UI doesn't look stalled while the server spools the temp file and creates the job — this window can be several seconds for very large uploads.
- On the request's own `load` event, the client navigates via `window.location.href = xhr.responseURL`. Because `start_transcribe` always responds with a 303 (success → `/transcribe/jobs/{id}`, validation failure → `/transcribe?error=...`) and XHR follows redirects itself, this one line reproduces what a plain form submission would have done for both outcomes — no separate error branch needed.
- `xhr.addEventListener('error', ...)` (network-level failure, no response at all) is the one case with no page to navigate to: it re-enables the submit buttons and shows an inline error, rather than leaving the user stuck on "Uploading…".
- Both submit buttons (`#run-job-btn-toolbar` in the page toolbar, `#run-job-btn-inline` in the settings panel — the toolbar one references the form via the `form=` attribute rather than being nested inside it) are disabled for the duration of the request to prevent duplicate submissions.

### Speaker Enrollment Web Flow
Interactive CLI enrollment (TTY prompts) is replaced by a post-job wizard. The wizard has two entry points — a **transcript-centric path** (preferred, restart-safe) and a **legacy job path** (still works while the server hasn't restarted). Both now delegate their submit and current-name-resolution logic to a single shared module, `web/enroll_shared.py`, so the two paths cannot drift apart (audit finding F1).

**On job completion (jobs.py):**
1. Transcription completes with `enroll_speakers=False`; detected speakers appear in transcript as `SPEAKER_XX` labels.
2. **F5:** if the job's input came from a web upload (`Job.is_web_upload`) and diarization data exists, the temp file is moved from the tempdir into the output directory as `{stem}{suffix}` and `job.input_path` is updated to that durable path — see "Job Queue" above for the full move/cleanup decision tree.
3. `_extract_speaker_excerpts()` (F12, phase 7; supersedes F10a) chooses each raw speaker label's clip window from their longest **solo diarization turn** — `speaker_manager._select_embedding_segments(diarization_segments, label, max_count=1)`, reusing F10b's solo-preferred / 2-20s-band / graceful-fallback policy so the clip is, as much as diarization allows, audio of only that speaker. The ffmpeg clip's `-t` is strictly clamped to `min(_EXCERPT_SECONDS, turn length)` — no padding floor when the turn is shorter than 12s, since a short clip of only the target speaker beats 12s that bleeds into someone else's turn. Saved to `{stem}_excerpt_{speaker}.mp3` alongside the transcript. The `.txt` sidecar is built from ALL of that label's aligned word-runs (post-F8, one whisper segment can yield several) that overlap the clip window, joined in time order, so the displayed text matches exactly what's audible — not just one word-run that can be a mid-sentence fragment. A label with no diarization segments (or none for that label — `_select_embedding_segments` raises `ValueError`) falls back per-label to the pre-F12 behavior: cut at that label's longest ALIGNED segment with the fixed 12s window and that single segment's text; one label's fallback never affects another's extraction. Both files survive server restarts.
4. `_write_enrollment_sidecar()` writes `{stem}_diar.json` alongside the transcript containing the raw `diarization_segments`, `speaker_map` (F7 — the authoritative raw label → display name map, carried on `Job.speaker_map` from `_result_store["speaker_map"]`), the (now durable, per step 2) `input_path`, and `campaign` slug. This is the key artifact that makes the transcript-centric wizard restart-safe.
5. `pipeline.process_file()` writes `job_id` to the transcript's YAML frontmatter for legacy linking, and (F7) exports the same `speaker_map` local it hands to `to_markdown()` into `_result_store["speaker_map"]` (defaulting to `{}` when diarization was skipped) for step 4 above to persist.

**Shared enrollment logic (`web/enroll_shared.py`):**
- **`resolve_current_names(md_path, diar, segments)`** (F7) — the single entry point every caller (both wizard GET routes' prefill, and `apply_renames`'s old-name resolution) goes through to answer "what does this raw pyannote label currently display as". Resolution order: (1) the sidecar's persisted `speaker_map`, if the loaded `diar` dict has one — exactly what the formatter used, no reconstruction; (2) `build_legacy_label_map()`'s interval-matching heuristic, only when the sidecar predates the `speaker_map` key (or no sidecar dict at all).
- `build_legacy_label_map(md_path, segments)` — **legacy fallback only** since F7. Maps raw pyannote label → *current* display name in the transcript body, by interval-matching markdown timestamps against diarization segment `[start, end]` spans (falls back to nearest-start-time when no interval contains the timestamp — whisper segment starts routinely fall just outside pyannote turns, and rendered timestamps are truncated to whole seconds). First-write-wins (`setdefault`) across the whole transcript, which is fragile if an early block is misattributed — this is why `resolve_current_names` prefers the persisted map whenever one exists, and why `apply_renames`'s own per-block attribution (below) doesn't reuse this first-write-wins strategy.
- `template_current_names(current_names)` filters that map for template prefill: entries whose *value* is itself raw-label-shaped (`SPEAKER_00 -> "SPEAKER_00"` — true on a first pass, before any rename has happened) are dropped so the wizard input starts empty instead of prefilled with the raw label. The `speaker_enroll.html` template also re-checks this itself (`_known and _known != speaker_name`) as a second line of defense. Submitting an untouched, prefilled-with-the-raw-label field used to silently create a junk `speaker_03` voice profile named "SPEAKER_03" that then competed in every future `match_speakers` call (F2).
- **Phase 2.5 split** — what used to be one synchronous function (`apply_enrollment_submit`: rename + WAV convert + embedding extraction, all inline in the HTTP request) is now two, so the slow half can run in a `JOB_ENROLL` job instead of blocking the browser tab for 30–120s:
  1. **`apply_renames(md_path, segments, renames)`** — fast, synchronous, stays inline in the route. Resolves `current_names` via `resolve_current_names` (loading the sidecar itself), drops any submission whose *new* name is raw-label-shaped (`^SPEAKER_\d+$`) (F2), then rewrites the transcript in a **single pass over the original content** (F6): each markdown block is attributed to a raw pyannote label exactly once — via `_attribute_block_to_label()` (interval containment against the block's timestamp; falls back to the nearest segment by start time, marked *not confident*, when no interval contains it) — with an unambiguous-name-match fallback for low-confidence blocks (if exactly one raw label currently displays that block's speaker name, trust the name over the coarse timestamp guess; if the name is itself shared by more than one raw label, keep the timestamp guess — it's the best signal available and, being per-block, a bad guess here can't poison anything beyond that one block, unlike F7's old per-label `setdefault`). Only blocks whose attributed raw label was actually renamed are rewritten, via `formatter.rewrite_transcript_blocks()`. This is what makes a same-submit swap (Alice↔Bob) and a shared-display-name rename (two raw labels both currently "Dan", only one renamed) both come out correct — neither is representable as a single global find/replace. A block rendered without a timestamp at all (`include_timestamps=False`) skips interval attribution entirely and goes straight to the name-based fallback, since there's no timing signal to attribute from — this only fully resolves renames when `current_names` came from the persisted `speaker_map` (guaranteed for any transcript produced after this fix); a legacy sidecar that is *also* missing timestamps has no reliable signal at all and may leave such blocks unrenamed. The YAML frontmatter `speakers:` list is rewritten via `formatter.rewrite_frontmatter_speakers()` (F11) — parses the frontmatter as YAML and matches/replaces `name` values exactly (no prefix-collision risk, no quoted-name mismatch, since it round-trips through `yaml.safe_load`/`yaml.dump` rather than regex), applying every rename in `frontmatter_renames` in one simultaneous pass against the parsed values — but only for old names that aren't shared by more than one raw label — a shared name in the frontmatter list can't be split between two people, so it's left alone rather than guessed at. After writing, the sidecar's `speaker_map` is updated in place with every submitted raw label's now-current name, keeping it authoritative across repeat wizard visits. Remaining *eligible* renames (F2's raw-label guard and F3's unchanged-name-with-existing-profile skip both applied) are grouped by *target display name* into a `RenameResult(current_names, groups)` — `groups` maps display name → `[raw_label, ...]`, handling two raw labels assigned the same name in one submit (common pyannote over-segmentation). `groups` is empty when nothing submitted was eligible.
  2. **`enroll_profiles(input_path, segments, groups, campaign_slug, device, progress=None)`** — slow, runs off the request thread. Converts to WAV once, then per group: if the profile exists, extracts embeddings per raw label (averaging when there's more than one) and merges via `speaker_manager.update_embedding()` (EMA, alpha=0.3) — never `enroll_speaker()`, which would overwrite the embedding and clobber `display_name`/`enrolled_date`/etc; if new, calls `enroll_speaker()`, passing a pre-averaged `embedding=` when the group has more than one raw label (F3). Adds the profile to `campaign_slug` (if any) after each successful enroll/update, same `add_member`-if-not-already-a-member guard as before. `progress`, if given, is called with human-readable status lines ("Converting audio…", "Extracting embedding for Alice (2/5)…") — the `JOB_ENROLL` runner wires this straight into `job.log_lines`.
  `speaker_manager.enroll_speaker()` grew an optional `embedding: Optional[np.ndarray]` parameter for this — when provided it's saved as-is instead of calling `extract_embedding()` internally, which is what lets the caller average across multiple raw labels before persisting.

**Transcript-centric enrollment wizard (transcripts.py) — preferred path:**
6. The transcript detail sidebar shows "Name speakers" linking to `GET /transcripts/{name}/enroll` when `{stem}_diar.json` exists, or falls back to the job-based URL when only `job_id` is in the frontmatter, or falls back to `/speakers` for pre-sidecar transcripts.
7. `GET /transcripts/{name}/enroll` reads speaker labels from the sidecar (always the original `SPEAKER_XX` labels regardless of whether the transcript has already been renamed), locates clip files on disk, and renders the wizard. This works after any server restart. The route builds `current_names` via `resolve_current_names()` (filtered through `template_current_names()`) and passes it to the template so the input fields pre-fill with names the user previously applied — letting them re-enter the wizard to correct a typo without retyping every name, while an untouched field stays empty. Excerpt-clip lookup for legacy (pre-fix) display-name-keyed files still uses `build_legacy_label_map()` directly — that's a different, best-effort "find this file on disk" use, not name resolution for renaming. **F5:** the route also checks whether the sidecar's `input_path` exists on disk and passes `audio_missing` to the template; `speaker_enroll.html` renders a warning banner ("Voice enrollment unavailable — source audio missing. Name changes will still apply.") above the form when true, so the user knows *before* submitting rather than being surprised after. The job-centric `GET /transcribe/jobs/{id}/enroll` does the same check against `job.input_path`.
8. `GET /transcripts/{name}/excerpt/{speaker_name}` serves the audio clip from disk.
9. **`POST /transcripts/{name}/enroll`** (Phase 2.5): reconstructs `DiarizationSegment` objects from the sidecar and calls `enroll_shared.apply_renames()` synchronously — the rename always happens before the response is sent. If `rename_result.groups` is empty (nothing eligible: all skipped/unchanged/refused), it redirects straight back to the transcript, same as before. Otherwise it checks whether the sidecar's `input_path` still exists (F5's pre-check, unchanged) — if not, it redirects with the generic `?notice=enroll_audio_missing` flag **without enqueueing a job** (a job that can only ever fail is pointless); if the audio exists, it calls `queue.submit_enroll(md_path, transcript_name, groups, device)` and redirects (303) to `/transcribe/jobs/{job.id}` — the wizard's own job progress page, using the server-generated `job.id` per the redirect security rules. `transcript_detail.html` still renders the "voice enrollment was skipped" banner when the notice flag is present. The job-centric `POST /transcribe/jobs/{id}/enroll` mirrors this exactly.

**Legacy job-based wizard (transcribe.py) — available while server session is live:**
- `GET /transcribe/jobs/{id}/enroll` renders the same wizard using in-memory `job.diarization_segments` (prefers these over frontmatter, so re-opening after a rename still shows original `SPEAKER_XX` labels). Falls back to frontmatter if segments are absent. Also builds `current_names` via `resolve_current_names()`/`template_current_names()`, loading the transcript's `_diar.json` sidecar (via `_load_diar_sidecar()`) for the authoritative `speaker_map` when one exists — this path previously never resolved current names at all, which was the direct cause of F1 (every rename after the first session silently no-op'd).
- `GET /transcribe/jobs/{id}/excerpt/{speaker_name}` serves the clip from `job.speaker_excerpts`; if that's missing/stale, falls back to disk but ONLY within this job's own transcript stem (F9) — `{stem}_excerpt_{speaker}.mp3` where `stem = Path(job.output_path).stem`, never a bare `*_excerpt_{speaker}.mp3` glob across the whole output directory (which could serve a *different* transcript's same-labelled clip, since every transcript reuses `SPEAKER_00`, `SPEAKER_01`, …). The on-disk lookup — including its CodeQL guard — lives in **`enroll_shared.find_excerpt_clip(out_dir, stem, candidates)`** (R24), shared with `transcripts.py`'s excerpt route so the two can't drift: it whitelists each candidate label via `re.sub(r"[^\w\-]", "_", …)` AND runs the `os.path.abspath` + `startswith` round-trip (a direct path join from a sanitised-but-still-tainted label needs it; CodeQL doesn't recognise the regex substitution alone as a sanitiser). If the in-memory job itself is gone (server restarted), there's no stem to scope to, so this route 404s rather than guessing — after a restart, `/transcripts/{name}/excerpt/{speaker_name}` (transcripts.py) is the route that actually serves excerpts, and it's transcript-scoped by construction (derives `stem` from the transcript's own path, not from job state); it tries the raw label first, then the legacy display-name key, passing both as `candidates` to the same helper.
- `POST /transcribe/jobs/{id}/enroll` calls `enroll_shared.apply_renames()` with `job.diarization_segments`, same as the transcript-centric path, then (when eligible and audio exists) `queue.submit_enroll()` using `job.output_path` as the transcript path — the `JOB_ENROLL` runner re-reads that transcript's sidecar rather than relying on the (still completed, but separate) transcription job's in-memory state.

### Startup Cleanup
`app._cleanup_orphaned_uploads()` runs in the FastAPI lifespan on every startup. It deletes `wisper_upload_*`, `wisper_enroll_*`, **and** `wisper_enrollsrc_*` temp files left in `tempfile.gettempdir()` from requests or jobs that crashed mid-flight. The files are only needed for the duration of a single job/request, so anything on disk at boot time is safe to remove.

**F5 note:** this sweep only ever matches the unrenamed `wisper_upload_*` prefix — `JobQueue.submit()` renames the temp file to `<original_stem><suffix>` synchronously before the job is even enqueued, so by the time a job is pending/running the file already has a friendly name and would *not* match this glob. That's exactly why the durable-audio fix (see "Job Queue" above) tracks the web-upload origin on `Job.is_web_upload` at submit time rather than re-deriving it from the current basename, and why the completed/failed/cancelled paths now handle cleanup explicitly instead of relying on this sweep. This startup sweep still matters for the narrow crash window between the temp file's creation and the rename (or a crash before `submit()` runs at all) — it is not redundant, just no longer the only cleanup path.

**R9-1/R6 (`wisper_enroll_*` → `wisper_enrollsrc_*`):** since R6 the standalone speaker-enroll route (`POST /speakers/enroll` in `routes/speakers.py`) hands its upload to a background JOB_ENROLL job, so the temp-file cleanup obligation moved into the job with the work. `JobQueue.submit_standalone_enroll()` immediately renames the `wisper_enroll_*` upload to `wisper_enrollsrc_<job-id>` (the same rename-at-submit pattern as F5), so a pending job's file can never match the `wisper_enroll_*` glob; `_run_standalone_enroll()` deletes both the upload and the converted WAV in a `finally`, success or failure. The sweep also clears `wisper_enrollsrc_*` orphans — safe because it runs at startup, when the in-memory queue is necessarily empty, so any such file on disk belongs to a job that died with the previous process. If the route's job hand-off itself fails, the route deletes the still-`wisper_enroll_*`-named upload before redirecting (ownership never transferred).

### Web Route Security

All web route handlers follow a consistent two-layer defence pattern enforced by CodeQL scanning on every PR:

**Path traversal (CWE-22) — transcript and speaker clip routes:**
1. `os.path.basename(user_input)` strips leading path components and is recognised by CodeQL as a path sanitiser.
2. `os.path.abspath(os.path.join(base, safe_name)).startswith(base + os.sep)` confirms the resolved path stays within the intended directory.
`Path.resolve()` on tainted input is **not** used — CodeQL does not recognise it as a sanitiser.

**Open redirect (CWE-601) — job ID routes (`cancel_job`, `enroll_form`, `enroll_submit`):**
`_validate_job_id(job_id)` in `transcribe.py` gates access with two layers:
1. `re.match(r"^[\w\-]+$", job_id)` rejects everything except alphanumeric/hyphen.
2. `os.path.basename(os.path.abspath(os.path.join("_guard", job_id)))` round-trip. `re.match().group(1)` alone is **still considered tainted** by CodeQL even after format validation; the `os.path` round-trip is required.

After validation, redirect URLs use **`job.id`** (the server-generated `uuid4` string stored on the `Job` object) rather than `safe_id` (the validated but still-tainted user value). Because `job.id` is set at job creation from `uuid.uuid4()` — never from request data — CodeQL's taint tracker sees no user-controlled data flowing into the `Location` header, fully resolving the `py/url-redirection` alerts.

**Error messages:** Internal exception text is never placed in redirect URLs or error responses. Routes use generic error codes (e.g. `?error=enroll_failed`).

**Output directory:** The `start_transcribe` form handler ignores any user-supplied `output_dir` and always writes to `_default_output_dir()`. Accepting arbitrary paths from form data would allow writing outside the data directory.

**XSS (A03) — markdown rendering:**
`transcript_detail` renders transcript markdown to HTML and injects it with Jinja's `| safe` filter. Before injection, `_sanitize_html()` in `transcripts.py` strips (R17): `<script>`/`<iframe>`/`<object>` elements *with their content*; `<embed>` tags (dropped without depth tracking — it's a void tag, so an unclosed one must not swallow the rest of the document); `on*` event-handler attributes; and `href`/`src` attributes whose value carries a `javascript:`/`data:`/`vbscript:` scheme. The scheme check conservatively removes every character ≤ 0x20 (whitespace/control) anywhere in the value and lowercases before the `startswith` — browsers tolerate obfuscations like `java\tscript:`, and `convert_charrefs=True` means `&#106;avascript:` is already decoded by the time attribute values reach the check. This defends against a `fix-speaker` payload where a malicious speaker name containing raw HTML ends up in a transcript file on disk.

**Security response headers (A05):**
`_SecurityHeadersMiddleware` in `app.py` attaches the following headers to every response:
- `X-Content-Type-Options: nosniff` — prevents MIME-type sniffing
- `X-Frame-Options: DENY` — clickjacking protection (R18: DENY, matching the CSP's `frame-ancestors 'none'`; the previous `SAMEORIGIN` contradicted it)
- `Referrer-Policy: strict-origin-when-cross-origin` — limits referrer leakage
- `Content-Security-Policy` — restricts resource origins; `script-src` currently includes `'unsafe-inline'` because several templates contain inline `<script>` blocks. Migrating those to `app.js` and switching to a nonce-based policy is a tracked hardening task.

**Network trust model (R16):**
The web UI has no authentication and no CSRF tokens (an explicit non-goal for a single-user tool). `wisper server` therefore binds `127.0.0.1` by default; `--host 0.0.0.0` is an explicit opt-in for trusted networks, and the Docker web services pass it explicitly in `docker-compose.yml` (the container must bind all interfaces for Docker's port mapping to work). State-changing endpoints are POST-only — `GET /config/open-data-dir` (which spawns an OS file-manager process) was converted to POST because a state-changing GET is triggerable cross-site via a simple `<img>` tag.

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
- Enrollment (`JOB_ENROLL`): E only — progress comes from `enroll_profiles()`'s `progress` callback lines in the log stream (no tqdm), so the ≥5 s estimator below carries the bar between per-speaker updates

**Single bar:** The bar is divided into equal slices, one per step. As each step's tqdm percentage arrives, it fills within that step's slice. Phase is detected from log keywords (`transcrib`, `diariz`, `format`, `refine`, `summariz`; for enroll jobs, the `progress` callback's own phrases `converting audio` / `extracting embedding`) to activate the correct step. For parallel mode (`channel_progress` events), each channel maps to its step slice.

**ETA and rate:** Parsed from tqdm progress strings and shown live below the bar. When no tqdm data arrives for ≥5 s (e.g. during LLM steps which have no tqdm), an estimator ticks the bar forward ~1% every 5 s up to 90% of the current step's slice, providing visual feedback until the `done` event fires.

**Parallel mode** (`parallel_stages=True`, `channel_progress` SSE events): T and D slices update from their respective channels concurrently. The bar shows whichever channel is further ahead.

### Transcript Management (Web)
- Transcripts list page: each card is fully clickable via the **overlay link pattern** (card is a `div` with an `absolute inset-0 z-10` `<a>` covering the whole card; non-interactive content divs carry no z-index so the overlay sits above them; action buttons use `relative z-50` to sit above the overlay). This avoids the invalid-HTML problem of nesting `<form>` inside `<a>`.
- Summary sidecars (`.summary.md`) are **filtered out of the transcript list** — they appear only as a green notes icon and "Campaign notes available" label on their parent transcript's card.
- Delete: `POST /transcripts/{name}/delete` removes both the `.md` file and its `.summary.md` sidecar (if present) and redirects to `/transcripts`.
- Dashboard stat cards link to their respective sections (Active Jobs → `/transcribe`, Transcripts → `/transcripts`, Enrolled Speakers → `/speakers`).
- Dashboard System card surfaces Whisper-side state (device, default model, HF token status) and LLM-side state (provider, resolved model via `resolve_llm_model()`, ready-vs-needs-config status). Ready is `True` for `ollama`/`lmstudio` (no key required) and reflects `get_llm_api_key()` for cloud providers — the actual key is never read into the template context, only the boolean and a generic "API key missing" hint, so the dashboard never leaks key material.

### LLM Post-processing (Web)
- **Inline after transcription**: the `/transcribe` form has a "LLM Post-processing" toggle group (`post_refine`, `post_summarize`). HTML checkboxes submit `value="on"` when checked; the route handler uses `bool(post_refine)` (truthy for `"on"`, falsy for `None` when unchecked) before storing as `Job.post_refine` / `Job.post_summarize`. After transcription completes, `_run_post_process()` chains into `_do_llm_work()` in the same job thread. LLM status messages (Ollama streaming output) are captured into `job.log_lines` via `_StderrCapture` (redirects `sys.stderr` for the job thread duration — safe because the queue runs one job at a time).
- **Standalone from transcript detail**: `POST /transcripts/{name}/refine` and `POST /transcripts/{name}/summarize` call `queue.submit_llm()`, which enqueues a `Job` with `job_type="refine"` or `"summarize"`. The browser is redirected to `/transcribe/jobs/{id}` — the same job detail / SSE streaming page used for transcription jobs. The job detail page suppresses the T/D/F step indicators for LLM jobs and shows a single step dot (R or S).
- **Campaign Notes page**: `GET /transcripts/{name}/summary` renders `.summary.md` as HTML with a metadata card (LLM provider/model, generated date, NPC chips) and a "Regenerate" button. `GET /transcripts/{name}/summary/download` serves the raw `.summary.md` file.
- **Job completion actions**: the SSE `done` event now includes `summary_path` and `job_type`; the JS in `job_detail.html` conditionally shows "View Campaign Notes" when `summary_path` is set and hides "Name Speakers" for non-transcription jobs.
- **LLM config page**: `GET/POST /config` exposes a dedicated "LLM Post-processing" card. Fields: `llm_provider` (select: ollama/anthropic/openai/google), `llm_model` (text), `llm_endpoint` (text, Ollama only), `llm_temperature` (number). Three secret fields (`anthropic_api_key`, `openai_api_key`, `google_api_key`) are password inputs that are **never overwritten with an empty submission** — leaving a key blank preserves the existing stored value. A JS snippet hides/shows the endpoint and cloud API key rows based on the selected provider. A note reminds users that env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`) take precedence over config-stored keys.
- **Local-provider model combobox**: `GET /config/ollama-status` and `GET /config/lmstudio-status` query `/api/tags` / `/v1/models` respectively (3 s connect timeout, endpoint read from saved config — no user input reaches `httpx`) and return `{"running": bool, "models": [{"name", "size"}]}`. The config page renders an `llm_model` text input with a sibling chevron button and a `<ul id="llm_model_menu">` popup — a small custom combobox. The browser-native `<datalist>` was rejected because its chevron behavior is inconsistent across browsers (Firefox/Safari don't show one, Chromium's only opens on type/arrow-key, not always on click). JS holds the discovered model list in `_modelOptions`, renders filtered `<li>` entries with `textContent` (so provider-returned names cannot inject HTML), and uses `mousedown` (not `click`) so the option selection fires before input `blur` would close the menu. A status line ("✓ Ollama running · N models available") and ↻ Refresh button live in `#llm_local_status_row`, shown only for local providers. A shared `_LOCAL_PROVIDERS` JS map drives both Ollama and LM Studio with one `loadLocalProviderStatus()` function; discovered models are cleared on provider switch so stale suggestions never appear.
- **LM Studio support**: `LMStudioClient` (`llm/lmstudio.py`) uses the OpenAI-compatible API at `http://localhost:1234` (default). It streams via SSE (`data: {...}` lines), accumulates tokens with dot-progress output identical to `OllamaClient`, and uses `response_format: {"type": "json_object"}` for structured output. The endpoint field placeholder switches between `:11434` and `:1234` based on the selected provider. `wisper config llm` prompts for endpoint first (defaults to `:1234`), then lists loaded models for selection. `_LLM_DEFAULT_ENDPOINTS` in `config.py` holds per-provider endpoint defaults.
- **Ollama error messages**: `OllamaClient._post_chat` distinguishes three failure modes: `httpx.ConnectError` → "Cannot connect to Ollama … daemon running?" message; `httpx.HTTPStatusError` 404 → "Model '…' not found in Ollama. Run: `ollama pull …`"; other `httpx.HTTPError` → generic failed message without the misleading daemon hint.
- **Local-provider combobox XSS prevention**: the JS that builds the model menu uses `document.createElement('li')` with `.textContent` assignment (never `innerHTML` or string-built HTML) so model names returned by Ollama or LM Studio cannot inject markup.
- **Cloud-provider model discovery**: `POST /config/anthropic-models`, `POST /config/openai-models`, `POST /config/google-models` accept an optional `api_key` form field and call the respective SDK's `models.list()`. Resolution order is form `api_key` (capped at 512 chars) > env var > saved config — handled by `_resolve_form_api_key()`. POST is required so the freshly-typed key never appears in a URL or in server logs. OpenAI results are filtered through `_is_openai_chat_model()` (allows `gpt-*`, `chatgpt-*`, `o<digit>*`; denies `instruct`, `audio`, `realtime`, `search`, `transcribe`, `image`, `tts`, `dall-e`, `whisper`, `embedding`, `moderation`, `davinci`, `babbage`, `vision`). Google results are filtered to `gemini`-containing names with the `models/` prefix stripped, excluding `embedding`, `aqa`, `imagen`. Anthropic returns all chat models with `display_name` surfaced as the `size` field for combobox labelling. Failure modes — missing SDK, missing key, API exception — return `{running: false, models: [], error: "<generic message>"}`; raw exception text is **never reflected** to the client to avoid leaking key fragments embedded in error messages. The frontend combobox auto-fires the listing on provider change AND on `change` of the matching API-key field (so typing a key then tabbing out triggers discovery without a manual Refresh click).
- **Campaign auto-association on enrollment**: when a transcribe job is submitted with `campaign=<slug>`, the slug rides on `job.kwargs["campaign"]`. The post-job enrollment wizard (`POST /transcribe/jobs/{id}/enroll`) reads it and, after each successful `enroll_speaker()`, calls `add_member(slug, profile_key)` so the new profile shows up in that campaign's roster. To match `record.py`'s defensive pattern, membership is checked first via `load_campaigns()` — `add_member` is only invoked when the profile is NOT already in the roster, so existing role/character entries are never clobbered. Campaign failures are logged and never break the enrollment HTTP response.
- **Ollama Cloud — two routing paths**: a new `ollama-cloud` provider was added to `LLM_PROVIDERS` alongside an `OllamaCloudClient` (a thin `OllamaClient` subclass with `endpoint=https://ollama.com` and a required Bearer token in the `Authorization` header). `OllamaClient` itself grew an optional `api_key` constructor parameter so the cloud subclass reuses the streaming logic verbatim. (Path A) Users can keep `llm_provider = "ollama"` and pick a cloud model with `-cloud` suffix (e.g. `gpt-oss:120b-cloud`); the local daemon recognises the suffix and proxies to ollama.com using `ollama signin` credentials — wisper code is unchanged. (Path B) Users can switch to `llm_provider = "ollama-cloud"` and supply `OLLAMA_API_KEY` / `ollama_cloud_api_key`; `OllamaCloudClient` then calls `https://ollama.com/api/chat` directly with no local daemon. `GET /config/ollama-cloud-catalog` is a single public endpoint that fetches `https://ollama.com/api/tags` (5 s timeout, no auth header sent) and is used by both paths. The web combobox fetches local `/api/tags` and the cloud catalog in parallel when `ollama` is selected, dedupes by name, and tags cloud entries with `-cloud` suffix plus a `☁` label. For `ollama-cloud` provider the catalog is shown with bare names. Hardcoded endpoint in `_LLM_DEFAULT_ENDPOINTS["ollama-cloud"] = "https://ollama.com"` (no user override exposed in UI — there is one cloud endpoint).
- **LLM provider metadata — single source of truth**: `config.py`'s `_LLM_DEFAULT_MODELS`, `_LLM_DEFAULT_ENDPOINTS`, and `_LLM_API_KEY_ENV` are the only copies. `cli.py`'s `setup()` and `config_llm()` wizards, and `llm/__init__.py`'s `get_client()`, all import and read these tables instead of keeping their own inline dicts — a prior duplication had let `cli.py`'s copies drift (missing `ollama-cloud`, stale model IDs). `cli.py`'s `_LLM_PROVIDER_CHOICE` (the `--provider` click.Choice for `refine`/`summarize`) is derived from `config.LLM_PROVIDERS` rather than a hand-maintained list, so it can't silently omit a provider (it previously omitted `lmstudio` and `ollama-cloud`). `_get_llm_client()`'s `--endpoint` override now applies to both `ollama` and `lmstudio` (previously `ollama`-only).
- **Output-dir resolution — single source of truth**: `path_utils.get_output_dir()` (checks `./output` relative to cwd first, falls back to `get_data_dir() / "output"`, creates it if missing) is the only implementation. `cli.py`'s `transcripts_list` and `web/routes/dashboard.py`'s transcript-count both call it instead of re-deriving the same path inline. The dashboard's transcript count also now excludes `*.summary.md` sidecars — it previously counted them, disagreeing with the Transcripts page's own `*.md`-minus-`.summary` filter.
- **`wisper config set` validation**: rejects any key not present in `config.DEFAULTS` with a short `ClickException` pointing at `wisper config show` (previously silently wrote unknown keys as junk config). Type coercion now also handles `int`-typed defaults (e.g. `min_speakers`, `max_speakers` — previously stored as strings, a booby trap once R5 started actually reading them); `bool` is checked before `int` since `bool` is an `int` subclass in Python and would otherwise be coerced by the int branch.
- **`wisper record start` config fallbacks**: after preset resolution, falls back to `discord_default_guild`/`discord_default_channel` from config (the same keys `wisper config discord` and the web recording form already read) before raising the "required" `ClickException` — previously only `--guild`/`--voice-channel`/`--preset` were honored, so a configured default was silently ignored on the CLI.
- **Web form enum validation (R33)**: `routes/transcribe.py`'s `start_transcribe` validates `model_size`/`device`/`compute_type` against `config.MODEL_SIZES`/`config.DEVICES`/`config.COMPUTE_TYPES` (new tuples next to the pre-existing `COMPUTE_TYPES`, also now used by `cli.py`'s `click.Choice` lists) before any file I/O — an invalid value redirects to `/transcribe?error=invalid_option` (generic code, the bad value is never echoed) rather than reaching `process_file()` with a value it doesn't understand. Validation runs before the uploaded file is spooled to a `wisper_upload_*` temp file, so a rejected request never orphans one.

### Offline Assets
All web UI assets are committed directly — no network access required at runtime:
- `static/htmx.min.js`: real HTMX 1.9.12 (48 KB, ISC) committed in full. No placeholder, no download step needed for local dev or Docker.
- `static/tailwind.min.css`: rebuilt automatically on server startup by `app._build_tailwind()` (mtime-checked; skips if already current). `pytailwindcss` is a main dependency (no Node.js required). Manual rebuild: `.venv/bin/python -m pytailwindcss -i src/wisper_transcribe/static/input.css -o src/wisper_transcribe/static/tailwind.min.css --minify`
- `static/fonts/`: self-hosted woff2 files for Newsreader, Geist, JetBrains Mono, and Instrument Serif (latin + latin-ext subsets). All licensed under SIL OFL 1.1. `@font-face` declarations live in `static/input.css`.

**Vendor management** — `scripts/vendor.py` re-downloads all vendored assets and rebuilds Tailwind in one command:
- `python scripts/vendor.py --check` — audit current state (shows ✓/✗ per asset)
- `python scripts/vendor.py` — re-download htmx + font woff2s and rebuild Tailwind CSS

Run this when bumping HTMX versions or adding font weights/subsets, then commit the changed files in `static/`.

### Tailwind CSS Staleness Check (CI)
CI runs `python -m pytailwindcss ... --minify` then `git diff --exit-code -- static/tailwind.min.css`. If the committed CSS differs from what a fresh build would produce (because a template or `input.css` changed without rebuilding), the diff step fails and blocks merge. The error message tells the developer exactly which command to run to fix it.

### Docker Web Services
`docker-compose.yml` defines four services: `wisper` / `wisper-cpu` (CLI) and `wisper-web` / `wisper-cpu-web` (web UI, port 8080). All services share a common YAML anchor (`x-volumes`, `x-env`) so volume mounts and environment variables are declared once. Environment variables (`HF_TOKEN`, `HUGGINGFACE_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`) are read from a `.env` file (copy `.env.example → .env`). `Makefile` provides `make start` / `make start-gpu` / `make stop` / `make logs` / `make build` targets as a convenience layer over `docker compose`.

### Distribution Launchers
Three double-click launcher scripts handle first-time setup and server start for end users:
- `start.command` (macOS) — `.command` extension opens Terminal on double-click; checks for `.venv`, calls `bash setup.sh` on first run, then starts `wisper server` and opens the browser.
- `start.bat` (Windows) — double-click batch file; calls `setup.ps1` on first run via `powershell -ExecutionPolicy Bypass`, then launches the server and opens `http://localhost:8080`.
- `start.sh` (Linux) — equivalent for Linux desktops with `xdg-open` for browser launch.
Both `start.command` and `start.sh` are committed with the execute bit set (`git update-index --chmod=+x`).

**Setup script LLM provider detection.** `setup.sh` and `setup.ps1` probe `localhost:11434` (Ollama `/api/tags`) and `localhost:1234` (LM Studio `/v1/models`) with a 2 s timeout before showing the LLM provider menu. When a local provider is running, the script lists installed/loaded models and lets the user pick by number — the choice is persisted via `wisper config set llm_provider …` / `llm_model …`. When neither is running, install/start hints are shown and the user can defer with `s` (skip → run `wisper config llm` later). Cloud SDK extras (`a/b/c/d` for anthropic / openai / google / all) are also offered. This eliminates a manual `wisper config llm` round-trip for the common case where Ollama or LM Studio is already running.

**Setup script install order (Windows GPU).** `setup.ps1` detects an NVIDIA GPU via `nvidia-smi` and installs the CUDA build of `torch` / `torchaudio` from `https://download.pytorch.org/whl/cu126` **before** running `pip install -e .`. If the project install ran first, pip would resolve the CPU-only PyPI `torch` as a transitive dependency, and the later CUDA install would replace `torch` itself but leave `faster-whisper` / `torchaudio` linked to the CPU build's internal layout — surfacing as `torch has no attribute _utils` at transcription time. With the CUDA wheels installed first, pip reuses them when resolving the project's deps, so all ML packages bind to the same build from the start. `--force-reinstall` is no longer needed.

**Setup script progress indicators.** Long-running pip installs (project, PyTorch, LLM extras) display a progress bar (PowerShell: `Write-Progress` driven by a background `Start-Process`) or a spinner (bash: background pip + spinning cursor) so the user has live feedback that setup is working.

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
