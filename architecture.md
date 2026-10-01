# wisper-transcribe — Architecture Reference

Current-state technical reference: what each part does and why it is built that way. History lives in git; open work lives in `plan.md`.

---

## Tech Stack

| Component | Library | Purpose |
|-----------|---------|---------|
| Transcription | `faster-whisper` (CTranslate2) | Lazy-cached Whisper model; `hotwords` and `initial_prompt` for vocabulary guidance; default model `large-v3-turbo` |
| Transcription (macOS) | `mlx-whisper` (optional `[macos]` extra) | Apple Silicon GPU/ANE backend, dispatched on MPS when `use_mlx` allows |
| Diarization | `pyannote-audio 4.x` | Speaker segmentation + voice embeddings |
| Word alignment | `transformers` (Qwen3-ForcedAligner-0.6B) | Re-times Whisper's words against the audio before speaker assignment |
| Audio loading (diarizer) | `scipy.io.wavfile` via `load_wav_as_tensor()` | Bypasses `torchcodec` (see [Known Constraints](#known-constraints)) |
| Audio conversion | ffmpeg (streaming) | Any audio/video → 16 kHz mono WAV |
| CLI | `click` | `setup`, `server`, `transcribe`, `enroll`, `speakers`, `campaigns`, `transcripts`, `search`, `fix`, `refine`, `summarize`, `record`, `config`, `db` |
| Web UI | FastAPI + Jinja2 + HTMX + Tailwind v4 | See [Web Interface](#web-interface) |
| LLM post-processing | `llm/` package (httpx + optional provider SDKs) | Ollama, Ollama Cloud, LM Studio, Anthropic, OpenAI, Google |
| Live capture | `soundcard` (optional `[live]` extra) | Local mic + system-audio loopback |
| Config | `platformdirs` + TOML | OS-native user data dir, overridable with `WISPER_DATA_DIR` |
| Storage | stdlib `sqlite3` (≥ 3.43, FTS5) | `wisper.db`: profiles, campaigns, transcript registry, speakers, journals, recordings, job history, search index (see [Database](#database-dbpy)) |
| Progress display | `tqdm` | Nested bars: folder (position 0), transcription (position 1) |
| Device detection | `torch.cuda` / `torch.backends.mps` | Auto-selects CUDA → MPS → CPU |

---

## Module Map

```
src/wisper_transcribe/
├── cli.py               Click entry points; delegates to pipeline/managers. Setup wizard, server command, --debug/--verbose
├── pipeline.py          Orchestrator: process_file(), process_folder(), CLI enrollment prompts
├── transcriber.py       faster-whisper wrapper, lazy model cache, CUDA DLL path fix, MLX dispatch
├── diarizer.py          pyannote pipeline wrapper, lazy pipeline cache
├── word_alignment.py    Forced word alignment: re-time Whisper words with Qwen3-ForcedAligner (see "Forced word alignment")
├── aligner.py           Merge transcription words with diarization turns (see "Alignment")
├── speaker_manager.py   Profile CRUD (table profiles, embeddings as BLOBs), embedding extraction, cosine matching, EMA updates, rename, doctor checks
├── speaker_registry.py  Campaign-wide relabel pass from per-transcript sidecar embeddings
├── formatter.py         Markdown + YAML frontmatter output; per-block parse/rewrite for speaker edits
├── audio_utils.py       validate_audio(), convert_to_wav(), get_duration(), load_wav_as_tensor()
├── time_utils.py        format_timestamp(), format_duration(), parse_timestamp()
├── path_utils.py        validate_path_component() (CodeQL-safe guard), get_output_dir()
├── transcript_store.py  Transcript registry rows, delete_transcript() (the only delete path), atomic_write_text(), save_transcript()/save_summary()
├── search_index.py      Full-text search: FTS5 index over transcript blocks and summary sections, freshness, backfill worker, search() (see "Search index")
├── legacy_import.py     Frozen importers for the JSON-era stores (run inside their migration)
├── db.py                SQLite: connect()/transaction(), migrations, capability/dev-data/runtime guards, to_rel()/from_rel() (see "Database")
├── config.py            Config load/save, device detection, HF token and LLM key lookup, provider metadata
├── models.py            Dataclasses shared across modules (segments, profiles, campaigns, recordings, LLM results)
├── campaign_manager.py  Campaign CRUD, rosters, Discord ID binding, transcript association and order (tables campaigns, campaign_members, campaign_transcripts)
├── recording_manager.py Recording CRUD, segment manifest, markers, crash recovery
├── refine.py            LLM vocabulary correction + unknown-speaker suggestions
├── summarize.py         LLM session notes → Obsidian-ready `.summary.md` sidecar
├── journal.py           Rolling campaign journal; whole-campaign rebuild
├── debug_log.py         Logger for --debug (file tee) and --verbose (console)
├── _noise_suppress.py   Third-party warning/logging suppression; safe to call in subprocesses
├── tailwind.py          Pinned Tailwind build (TAILWIND_VERSION, build_css()); `python -m wisper_transcribe.tailwind`
├── llm/                 Provider-agnostic LLM clients
│   ├── base.py          LLMClient ABC: complete() + complete_json(schema)
│   ├── errors.py        LLMUnavailableError (soft-fail) / LLMResponseError
│   ├── ollama.py        httpx streaming client for Ollama (/api/chat, NDJSON)
│   ├── ollama_cloud.py  OllamaClient subclass for ollama.com with Bearer auth
│   ├── lmstudio.py      httpx streaming client for LM Studio (/v1/chat/completions, SSE)
│   ├── anthropic.py     Anthropic SDK; JSON via forced tool_use
│   ├── openai.py        OpenAI SDK; JSON via strict json_schema
│   └── google.py        google-genai SDK; JSON via response_schema
├── static/              Vendored assets (htmx, fonts, tailwind.min.css), app.js, logo
└── web/
    ├── app.py           App factory, lifespan (managers, cleanup, Tailwind rebuild), security headers
    ├── jobs.py          In-memory JobQueue and all job runners (see "Job Queue")
    ├── enroll_shared.py Enrollment-wizard logic shared by the transcript and job routes
    ├── audio_writer.py  SegmentedWavWriter, resampling helpers, WAV concatenation
    ├── discord_bot.py   BotManager: Discord recording sessions via the JDA sidecar
    ├── local_capture.py LocalCaptureManager: local mic + system-audio recording
    ├── live_transcribe.py Near-real-time transcription for local sessions
    ├── _responses.py    Shared 400 / error-redirect helpers
    └── routes/
        ├── __init__.py      Jinja setup, urlencode filter, static_mtime(), app.state accessors
        ├── dashboard.py     Dashboard, jobs partial, sidebar status
        ├── transcribe.py    Upload form, job pages, job SSE, job-based enrollment wizard
        ├── transcripts.py   Transcript list/detail/edit/delete, enrollment wizard, LLM actions, awaiting-transcription list
        ├── speakers.py      Speaker list, enroll, rename, remove
        ├── campaigns.py     Campaign CRUD, rosters, transcript order, journal
        ├── record.py        Record page, recordings list/detail, recording JSON API, transcribe hand-off
        ├── search.py        /search page (see "Search index")
        └── config.py        Settings page, provider status and model discovery
```

Outside the package, `scripts/` holds maintainer tools: `vendor.py` (refresh vendored web assets) and `alignment_eval.py` (measure forced alignment on real audio; see "Forced word alignment" and `docs/scenarios.md`).

---

## Processing Pipeline

```
Audio file
    │
    ▼
1. VALIDATE    audio_utils.validate_audio() — exists + supported extension, else ValueError
    │
    ▼
2. CONVERT     audio_utils.convert_to_wav()
               • ffmpeg -map 0:a:0 -ac 1 -ar 16000 -vn → first audio track, 16 kHz mono WAV
               • Streamed, so multi-hour inputs avoid pydub's 4 GB limit
               • A 16 kHz mono WAV is returned unchanged (header check only)
               • The input file is never modified
    │
    ├───────────────────────────────────────────────┐
    ▼                                               ▼
3. TRANSCRIBE  transcriber.transcribe()      4. DIARIZE   diarizer.diarize()
   • faster-whisper, or MLX on Apple Silicon    • pyannote Pipeline (lazy, cached)
   • word_timestamps=True on both backends      • Audio passed as a preloaded tensor dict
   • → list[TranscriptionSegment] (+ words)     • → list[DiarizationSegment]
   Steps 3 and 4 run concurrently when parallel_stages=True
    │                                               │
    └──────────────────┬────────────────────────────┘
                       ▼
               5. ALIGN      word_alignment.align_words() — re-time words (when forced_alignment
                             resolves on; see "Forced word alignment")
                             aligner.align() — words → speaker runs (see "Alignment")
                       ▼
               6. IDENTIFY   speaker_manager.match_speakers() — labels → names
                       ▼
               7. FORMAT     formatter.to_markdown() — frontmatter, merged speaker lines, timestamps
                       ▼
               8. WRITE      <stem>.md in the output dir (atomic), then register it: registry row,
                             campaign link, search index (transcript_store.register)
```

`pipeline.process_file()` deletes the converted WAV in a `finally` once it is no longer needed (never when it is the original input).

### Optional LLM post-processing

```
<stem>.md ──► wisper refine              ──► dry-run diff, or <stem>.md.bak + updated <stem>.md
          ──► wisper summarize           ──► <stem>.summary.md
          ──► wisper summarize --refine  ──► refine in place, then summarize
```

---

## Alignment

`aligner.align()` produces one `AlignedSegment` per same-speaker run of words.

- **Word-level assignment.** Each word takes the diarization turn with the greatest time overlap, or the nearest turn by midpoint distance if none overlaps. A whisper segment spanning a speaker change therefore splits at the turn boundary.
- **Micro-run smoothing** (`_smooth_word_speakers()`). A run of ≤2 words or <1.0 s sandwiched between two runs of the same *other* speaker is absorbed into them. Re-checked with forced-aligned words: it still mostly fixes mid-phrase flips caused by diarization-boundary jitter, so the thresholds stay (they're parameters so `scripts/alignment_eval.py` can compare variants). Diarization boundaries jitter by a word or two, and without this a single sentence splits across speakers. Runs at a segment edge, or between two *different* speakers, are kept (they are real interjections). Repeats to a fixpoint.
- **Sweep, not brute force.** `_assign_word_speakers()` sorts turns once and sweeps words against an active window, avoiding an O(words × turns) scan (~60M overlap checks for a 3-hour session). The sweep requires time-ordered words; out-of-order input is routed to `_assign_word_speakers_bruteforce()`. Ties break on original turn order, so both paths return identical output.
- **Fallback.** Segments with no word data use whole-segment max overlap (`_best_overlap_speaker()`).
- Unmatched words or segments are labelled `UNKNOWN`.

### Forced word alignment (`word_alignment.py`)

Whisper's word timestamps are a by-product of decoding, and at speaker changes they drift into the neighbouring turn. `align_words()` re-times each already-transcribed word with Qwen3-ForcedAligner-0.6B (`config.FORCED_ALIGNMENT_MODEL`, Apache-2.0, ungated) through native `transformers`. Only `Word.start`/`Word.end` change; the text is Whisper's.

- **One crop per segment, no padding.** Each segment is aligned against exactly `[seg.start, seg.end]`. The model places words anywhere in its crop, so padding lets a segment's words claim its neighbour's audio (measured: more order inversions, known words moved early).
- **Exact word mapping.** Each Whisper word is split with the processor's own `split_words_for_alignment()` (letters, digits and `'` kept; CJK per character), so every alignment item belongs to a known word. A word with no alignable characters keeps its Whisper times, clamped between its re-timed neighbours. If the processor's split of the joined tokens doesn't reproduce the per-word split, that segment keeps Whisper times.
- **Batching.** Segments are sorted by length and batched (8 on CUDA/MPS, 4 on CPU). An out-of-memory batch is split in half and retried.
- **Never fails the job.** Any failure (model load, OOM at batch 1, a crop over the model's 180 s limit, an unsupported language, a non-16 kHz WAV) keeps Whisper times for the affected segments. `InterruptedError` is re-raised so web cancellation still works; the "Aligning" tqdm bar is what gives cancellation a check point.
- **Ordering.** Items within a crop are monotonic (the processor repairs out-of-order bins); across segments, a segment's first word is clamped to start no earlier than the previous word's end.
- **Dtype.** bf16 on CUDA, fp16 on MPS (matched fp32 exactly and ran faster than bf16), fp32 on CPU.
- **When it runs.** `process_file()` calls it right before `align()`, in the main process on both the sequential and `parallel_stages` paths (it needs only the WAV and the segments). `config.forced_alignment_enabled()` resolves the `forced_alignment` setting: `auto` = CUDA or MPS, since CPU costs ~9–20 min per 2.5 h session. Never without diarization; timing only matters for speaker attribution.
- **Measurement.** `scripts/alignment_eval.py` runs the production path on an excerpt and compares arms (Whisper vs aligned timing, smoothing variants, regular vs exclusive turns) by an automatic proxy, a re-transcription audit of >1 s shifts, and a blind labelling sheet scored per arm.
- **Language.** The model never sees the language; it only picks the word splitter. `auto`/empty uses the default splitter; an explicit language outside the 11 supported ones skips alignment. Japanese and Korean need `nagisa`/`soynlp`; without them alignment is skipped for that job.

---

## Speaker Identification

### Matching

`speaker_manager.match_speakers()`:

1. Extract an embedding for each diarization label.
2. Cosine-score every (label, profile) pair.
3. **Exclusive pass:** consume pairs highest-first, assigning when both the label and the profile are free and the score clears `similarity_threshold` (default `DEFAULT_SIMILARITY_THRESHOLD`, 0.55). A label whose first choice is taken falls through to its next-best unused profile.
4. **Many-to-one pass** (`allow_many_to_one=True`, used when `num_speakers` isn't pinned): a still-unassigned label may claim an already-used profile above threshold. This absorbs pyannote splitting one person into two labels. Pinning `num_speakers` asserts one label per person, so this pass is skipped.
5. Anything left becomes `Unknown Speaker N`, numbered by sorted label order so numbering is deterministic.

Ties sort by label then profile name for determinism.

- **Why 0.55:** on real sessions in the WeSpeaker space, cross-session same-person pairs scored 0.65–0.95 and different people at most 0.50. `load_config()` migrates a saved `0.65` (the old model's default, persisted verbatim by `save_config()`) to the new default.
- **Scores are surfaced:** `match_speakers(scores=...)` fills `label -> (closest profile, similarity)`. The job log and `wisper speakers test` print the score of each match, or the closest profile for a miss, so near-misses are visible when tuning.

### Campaign scoping

Campaigns are an **additive roster layer** over one global profile store. Embeddings (the `profiles.embedding` column) stay global so the same person is recognized across campaigns without re-enrolling. Passing `campaign=<slug>` restricts `match_speakers()` candidates to that roster via `profile_filter`; `None` matches globally. Deleting a campaign never touches profiles or embeddings.

### Campaign relabel (`speaker_registry.py`)

Diarization labels are scoped to one run, so cross-session identity comes only from embeddings. Web transcription jobs store each label's embedding and each name's provenance (`auto` from matching, `manual` from the wizard) as `transcript_speakers` rows. `relabel_campaign()`:

1. Loads every campaign transcript's diarization data (`read_sidecar()`). Missing or old-space embeddings are backfilled from the durable source audio when it still exists (`backfill=True`) and saved with `set_speaker_embeddings()`; otherwise the transcript is skipped.
2. Re-matches each transcript's labels against the campaign roster with `assign_labels()` (many-to-one on).
3. Clusters the still-unknown labels across transcripts (greedy centroid, same threshold). A cluster heard in two or more transcripts becomes `Recurring Speaker N`; single-session voices keep per-transcript `Unknown Speaker N` numbering.
4. Renames only labels that are relabelable, through `apply_renames(source="auto")`, the same single-pass block rewrite the wizard uses.

- **Manual names are never overwritten.** A label with a recorded provenance follows it. Labels imported without one (older runs) treat only pipeline-shaped names (`AUTO_NAME_RE`: raw labels, `Unknown/Recurring Speaker N`) as automatic, since a real name there may have been typed.
- **Pipeline-shaped names never become profiles.** `apply_renames()` excludes `AUTO_NAME_RE` names from enrollment, so a prefilled `Unknown Speaker 1` submitted unchanged can't create a junk profile that competes in every match.
- **Propagation:** a wizard enroll job for a campaign transcript runs `relabel_campaign(backfill=False)` after enrolling, so a newly named person is renamed in the campaign's other sessions from stored embeddings without re-reading audio. The full pass (with backfill) is `wisper campaigns relabel` or the campaign page's **Re-match speakers** (`JOB_SPEAKER_RELABEL`).
- **Profiles aren't updated from auto matches**, only from wizard or explicit enrollment, so a wrong match can't reinforce itself.
- `speaker_registry` imports `apply_renames`/`AUTO_NAME_RE` lazily from `web/enroll_shared.py` (pure Python, no FastAPI) rather than duplicating the block-rewrite logic.

### Embedding extraction

`extract_embedding()` slices the WAV to a speaker's segments and runs the WeSpeaker ResNet34 model bundled in the diarization repo (`DIARIZATION_MODEL`, subfolder `EMBEDDING_SUBFOLDER`; 256-dim). Each segment's embedding is L2-normalized before averaging and the mean is normalized again, so long segments don't dominate and stored vectors are unit length. Segments are chosen by `_select_embedding_segments()` with `max_count=EMBEDDING_SEGMENTS` (30):

1. Up to 30 **solo** segments (no overlap with another speaker) of 2–20 s, longest first.
2. Else all solo segments, longest first.
3. Else the plain longest segments.

Solo segments are preferred because cross-talk and background music bleed into the longest turns. Excerpt clips reuse the same selector with `max_count=1`.

- **Why the diarizer's own embedding model:** profiles and diarization clusters share one embedding space and one gated license. Measured on real sessions, it separated same-person from different-person pairs by 0.34 cosine vs 0.25 for the old `pyannote/embedding`.
- **Why 30 segments:** raised same-person similarity across sessions by 0.05–0.10 over 5, at a few extra seconds of GPU time per speaker.

### Embedding spaces

Embeddings from different models aren't comparable (and differ in dimension), so each profile records `embedding_space`. `config.EMBEDDING_SPACE` is the current tag; an empty tag means the old 512-dim `pyannote/embedding`. A profile with no embedding at all loads with an empty tag too, so it is listed as needing re-enrollment.

- `load_profile_embedding()` returns `None` for any profile not in the current space; `match_speakers()` and the CLI ranking only compare through it.
- `stale_profile_keys()` lists old-space profiles. The pipeline logs them as skipped, the Speakers page badges them, and the CLI ranking lists them unscored.
- `update_embedding()` replaces an old-space vector outright and retags the profile instead of averaging, so any re-enroll path (wizard, standalone upload, `wisper enroll`, CLI pick) migrates a profile.

### CLI enrollment (`--enroll-speakers`)

1. After diarization, each `SPEAKER_XX` label is shown with a sample quote; `--play-audio` plays a clip via `ffplay` (ships with ffmpeg; no Python audio backend needed). `r` replays.
2. Existing profiles are ranked by cosine similarity to this label, with `★` above threshold.
3. The user picks a number to reuse a profile (optionally blending this episode in via EMA) or types a new name.
4. New profiles are one `profiles` row (embedding included) plus a `profiles/embeddings/<key>.mp3` reference clip.

`_interactive_enroll()` caches each label's embedding for the pass, since ranking, EMA update, and new-profile enrollment would otherwise extract it up to three times.

### Profiles on disk

- A profile is one `profiles` row: display name, role, notes, enrollment metadata, and the embedding as a float32 BLOB (~1 KB) with its `embedding_space`. `load_profiles()` returns them in one query, so `SpeakerProfile.embedding` is always loaded and `load_profile_embedding()` does no I/O. The Speakers page's Sessions and Last heard come from `profile_activity()`: transcripts (with their `.md` present) that have a `transcript_speakers` row carrying the profile's display name.
- `profiles/embeddings/<key>.mp3` is the ~12 s reference clip for playback on the Speakers page (`reference_clip_path()`). It is the only per-profile file; the folder name predates the database.
- Removal and reset delete the row(s) first, then the clip, so the play button never dangles. Memberships cascade.
- **EMA update** (`--update`): `stored = unit(0.7 * unit(stored) + 0.3 * unit(new))`, a read-blend-write in one transaction because it may also retag the profile.
- Profile key is `name.lower().replace(" ", "_")` — both clip filename and URL slug. It is an alternate key; campaign memberships reference the row id.
- `wisper speakers doctor` reports likely duplicates (`find_duplicate_profiles()`, cosine above 0.95), old-model profiles, and placeholder-named profiles (`AUTO_NAME_RE`). It changes nothing.

### Rename

`speaker_manager.rename_profile(old_key, new_name)` is the single implementation behind `wisper speakers rename` and `POST /speakers/{name}/rename`:

1. Derives the new key and validates it with `validate_path_component()` (it becomes a filename; this also breaks the CodeQL taint chain for the web route).
2. Rejects a collision with a different existing key.
3. One `UPDATE` of the key and display name. Campaign memberships reference the profile id, so roles, characters, and Discord bindings follow without being touched.
4. After the commit, moves the `.mp3` clip (row first, then file). A same-key rename (case tweak) moves nothing.

Recording speakers reference the profile id too, so they follow a rename. Not rekeyed: display names already written into transcripts.

---

## Key Design Decisions

### scipy audio pre-loading (torchcodec bypass)
pyannote 4.x reads audio via `torchcodec`, which on Windows needs FFmpeg's full-shared build. Instead, `diarize()` and `extract_embedding()` pass `audio_utils.load_wav_as_tensor()`'s `{'waveform', 'sample_rate'}` dict; pyannote skips torchcodec when a waveform is supplied. Input is always the 16 kHz mono WAV from `convert_to_wav()`.

### speechbrain LazyModule shim
speechbrain 1.0's guard against lazy-loading optional integrations checks for `"/inspect.py"`, which never matches Windows backslash paths, so every missing integration raises `ImportError`. `diarizer.py` patches `LazyModule.ensure_module` at import to return stub modules instead. This is the only remaining compatibility shim.

### Module-level imports for mock patching
`pyannote.audio.Pipeline` (in `diarizer.py`) and `pydub.AudioSegment` (in `audio_utils.py`) are imported at module top so tests can patch `wisper_transcribe.diarizer.Pipeline` etc. Imports inside functions can't be patched at the module path.

### CUDA DLL path (Windows)
`transcriber.load_model()` finds `cublas64_12.dll` in PyTorch's `nvidia-cublas` package or the CUDA Toolkit and adds it via `os.add_dll_directory()`; CTranslate2 can't load without it.

### UTF-8 stdio
`cli.py` (at import) and `web/app.py` reconfigure `sys.stdout`/`stderr` to UTF-8 with `errors="replace"`. Legacy Windows code pages can't encode the box-drawing and arrow characters written via `tqdm.write()`, which otherwise crashes a successful job. `app.py` repeats it because `uvicorn --reload` imports the app in a fresh subprocess that never runs `cli.py`.

### tqdm monitor thread
`tqdm.monitor_interval = 0` is set in `app.py` and per job in `jobs.py` (`--reload` imports the app in a fresh subprocess). Otherwise tqdm's `TMonitor` daemon thread hangs Ctrl+C on Python 3.14.

### Third-party warning suppression
`_noise_suppress.suppress_third_party_noise()` must run before pyannote/Lightning are imported in any process. It is called at the top of `diarizer.py`, the top of `speaker_manager.py` (`wisper enroll` never imports `diarizer.py`), and as the first line of `_diarize_worker()` (fresh subprocess interpreters).

It uses two mechanisms:
- `warnings.filterwarnings("ignore", ...)` for `warnings.warn()` messages, with no category restriction.
- `_silence_logger(name)` for `logging` messages: an always-false `logging.Filter` plus `propagate=False`. `setLevel(ERROR)` alone doesn't hold because Lightning resets its loggers to INFO during import, and torch's import resets `torch`. So every call reasserts the level and `propagate` (children such as `torch.utils.flop_counter` inherit it), and only the filter and handler are added once.

Silenced: the `lightning`/`pytorch_lightning` logger family and `torch`. Also sets `HF_HUB_DISABLE_SYMLINKS_WARNING=1` and `absl.logging.set_verbosity(ERROR)` (absl has its own logging system). Everything is skipped when `WISPER_DEBUG` is set.

### Logging (`--debug` / `--verbose`)
`debug_log.Logger`, created once by `setup_logging(debug=, verbose=)`:
- **`--debug`** (`transcribe`, `server`): sets `WISPER_DEBUG=1`, creates `./logs/wisper_<ts>.log`, tees `tqdm.write()` into it, and routes root-logger records through the same file handle via `_LoggingBridge` (two independent handles on one file interleave writes).
- **`--verbose`** (`transcribe`): a console DEBUG handler on the root logger; no file, no `WISPER_DEBUG`.

### tqdm patching is load-bearing in three layers
`tqdm.write`/`tqdm.__init__` are process-global and patched in three unrelated places:

1. **`debug_log.Logger._patch_tqdm`** — permanent tee into the `--debug` log. Idempotent: re-patching wraps the true original, so wrappers never stack.
2. **`jobs._run_transcription_job`** — per-job capture into `job.log_lines` for SSE, restored afterwards. This is also where cancellation is checked, so **cancellation only fires when tqdm writes**; a job inside a long silent call can't be interrupted until the next write.
3. **`pipeline._patch_tqdm_for_queue`** (`parallel_stages=True`) — per-subprocess redirect into the IPC queue; never touches the parent's tqdm.

They coexist only because of the one-job-at-a-time invariant: layer 2 wraps one job at a time and chains at most one level over layer 1. Check all three before changing any of them.

### Config resolution (`process_file()`)
`None` is the only "unset" marker: `model_size`, `language`, `include_timestamps`, and `vad_filter` fall through `None` → config → hardcoded fallback. An explicit value always wins, even one equal to the fallback.

- `language="auto"` is a separate explicit marker (auto-detect). It is resolved after the config lookup (config may itself say `auto`) and becomes `None` just before `transcribe()`.
- With diarization on and no `num_speakers`/`min_speakers`/`max_speakers`, config `min_speakers`/`max_speakers` constrain the diarizer. A pinned `num_speakers` disables this.
- `device="auto"` and `compute_type="auto"` mean hardware detection / dtype resolution, not config lookup.
- The web form only exposes `model_size` (preselected from config); `language`/`include_timestamps` default to `None` so config applies.

### Transcription options
- **VAD:** `vad_filter` goes straight to faster-whisper's bundled Silero VAD. Timestamps stay relative to the original audio.
- **Vocabulary:** `hotwords` (boosted tokens) and `initial_prompt` (fake prior context). `--vocab-file` (one word per line, `#` comments) overrides config `hotwords`.
- **Compute type:** `resolve_compute_type()` maps `auto` to `float16` on CUDA and `int8` on CPU; explicit values pass through.
- **MLX (Apple Silicon):** `use_mlx` = `auto` (use MLX if `mlx-whisper` imports), `true` (require it), `false` (faster-whisper CPU). Models come from `mlx-community/whisper-*-mlx` via `_MLX_MODEL_MAP`. MLX has no hotwords param, so hotwords are prefixed into `initial_prompt`; `vad_filter` is ignored. `verbose=False` is passed for its progress bar (see "Job progress display").

### Module-level model caches
`transcriber._model`, `diarizer._pipeline`, `speaker_manager._embedding_model`, and `word_alignment._fa_model`/`_fa_processor` are module globals so folder runs and the web server don't reload multi-GB models per file. Tests reset them to `None`.

- **Cache keys:** each cache records its load parameters and reloads on mismatch (`_model_key = (model_size, device, compute_type)`, `_pipeline_device`, `_embedding_device`, `_fa_device`). Without this, the web server would keep the first job's model forever.
- **No poisoned cache:** loaders build into a local and publish to the global only after device checks and `.to(device)` succeed. The old reference is dropped before loading the replacement so two models are never resident at once.
- These globals are not thread-safe, which is why the web queue runs one job at a time.

### Parallel stage processing (`parallel_stages`)
With `parallel_stages=True` (default off), transcription and diarization run in `ProcessPoolExecutor(max_workers=2)`; each subprocess has its own model globals.

Progress reaches the web UI through IPC:
1. A `multiprocessing.Manager().Queue()` is passed to each worker. A plain `multiprocessing.Queue` can't be pickled under macOS's spawn start method.
2. Workers call `_patch_tqdm_for_queue(queue, channel)` before any ML import, sending `(channel, "log"|"bar", message)`.
3. A drain thread in the parent sends `"log"` through `tqdm.write()` (so the debug tee and job capture see it) and writes `"bar"` frames to stderr, de-duplicated per channel.

Worker functions are module-level so they pickle. With `--workers N`, total processes are N×2.

### Parallel folder processing (`--workers N`)
`process_folder()` uses `ProcessPoolExecutor` (model globals aren't thread-safe). `workers` is clamped to 1 unless the resolved device is `cpu`, since GPU memory can't be shared across processes. Outputs that already exist are detected up front and returned in `skipped`; the function returns `(successes, skipped, errors)`.

### torch version
`torch>=2.8.0` (pyannote 4.x minimum). The GPU image takes CUDA builds from `https://download.pytorch.org/whl/cu126`. The CPU image installs torch/torchaudio from `https://download.pytorch.org/whl/cpu` *before* the package: PyPI's Linux torch wheels depend on ~3 GB of `nvidia-*` CUDA libraries (content size 3.9 GB → 0.9 GB with the CPU wheels).

### Database (`db.py`)
`<data dir>/wisper.db`, stdlib `sqlite3`, no ORM. It holds everything that used to be JSON files: profiles, campaigns, the transcript registry and per-transcript speakers, journal entries, recordings, job history, and the search index. Transcripts, summaries, journals, and audio stay files (they're the product; users edit them in Obsidian). Every change is one `db.transaction()`, which also serializes other processes, so there is no file locking.
- **One way in.** Every connection comes from `db.connect()`, which sets `foreign_keys=ON` (off by default, per connection), `busy_timeout=5000`, and the rollback journal (`journal_mode=DELETE`, which works on any filesystem including Docker Desktop bind mounts, where WAL misbehaves). A test fails if any other module calls `sqlite3.connect`.
- **Connection per unit of work.** `with db.transaction() as conn:` opens, runs `BEGIN IMMEDIATE`, commits or rolls back, and closes. Connections are `autocommit=True`, so nothing commits implicitly. `BEGIN IMMEDIATE` takes the write lock up front: a deferred read-then-write gets `SQLITE_BUSY` on lock upgrade without waiting. No connection is cached, so `WISPER_DATA_DIR` changes (tests) just work. Never hold a transaction across ML or LLM work. The capture hot path passes a 500 ms busy timeout (see "Recording layer").
- **Migrations** run on first `connect()` when `PRAGMA user_version` is behind. Each is DDL plus an optional legacy-import step, applied with the version bump in one `BEGIN IMMEDIATE` transaction with `defer_foreign_keys=ON`; `PRAGMA foreign_key_check` must be empty or everything rolls back. A second process waits on the lock, then sees the new version and does nothing. Upgrading an existing DB first snapshots it to `backups/wisper-v<N>-<time>.db` (backup API on a second connection, after the migrator holds the lock). An import step copies its legacy files to `backups/pre-sqlite-v<N>-<time>/` first and writes `import-report.txt` there. **Shipped migrations are frozen:** a schema change is a new version.
- **Guards**, each refusing with a user-facing `db.DatabaseError` (the CLI prints it without a traceback; the server refuses to start):
  - SQLite ≥ 3.43 with FTS5 contentless-delete, checked once per process.
  - Downgrade: a DB newer than the code refuses; an old build must not write a newer schema.
  - Dev data: a branch that has to reshape unreleased migrations sets `SCHEMA_FROZEN = False`, and such a build refuses to migrate the platform-default data dir (work on a copied `WISPER_DATA_DIR`). `test_schema_frozen_on_main` fails on `main` or a PR into it unless the flag is `True`.
  - Runtime lease (`runtime_leases`): a host process and a Docker Desktop container writing one DB corrupts it (file locks don't cross the VM). `connect()` records a `host` or `container` lease (container = `/.dockerenv`; `crosses_vm` = `/proc/version` contains `linuxkit`) and refreshes it at most every 30 s; the server also runs `db.Heartbeat` every 60 s. Startup refuses only when the *other* runtime's lease is under 2 minutes old and the container lease crosses the VM, so native Linux Docker is never blocked. Advisory: two processes starting in the same second could both pass.
- **Schema by version** (DDL is the `_V<N>_DDL` strings in `db.py`):
  - **v1** `migrations` (append-only log; `user_version` is authoritative), `runtime_leases`. Pins an existing install's CWD `./output` into `output_dir`.
  - **v2** `profiles`, `campaigns`, `campaign_members`, `transcripts` (the registry: one row per NFC stem under the output root, `missing_since` when its `.md` is absent, `audio_rel_path`), `campaign_transcripts` (one campaign per transcript, `UNIQUE(campaign_id, position)`; reordering writes positions in two steps, above the max and then final, because `UNIQUE` is checked row by row). Imports `speakers.json`, `.npy` embeddings, and `campaigns.json`.
  - **v3** `journal_entries` and the stale-journal trigger (see "LLM Post-processing").
  - **v4** `transcript_speakers` (label, display name, `source` auto|manual, embedding + space). Imports every `_diar.json` (backed up under `backups/pre-sqlite-v4-*/output/`, then slimmed to its segments; an `input_path` from another machine or a host path seen from Docker is matched by file name in the output root; audio outside it becomes NULL and is reported; a sidecar `campaign` whose transcript has none associates it).
  - **v5** recordings and their subtypes and children (see "Recording layer"). Imports `recordings.json` and each `metadata.json`.
  - **v6** `jobs` (see "Job Queue").
  - **v7** `search_index_state`, `search_blocks`, `search_fts` (see "Search index").
- **Schema principles** (tested in `test_schema.py`):
  - Normalized to 3NF/BCNF; derived values aren't stored. A recording's `transcribing`/`transcribed` status, segment and `combined.wav` paths, marker `elapsed_s`, and unbound Discord speakers are all computed.
  - Every ordinary table is `STRICT` (FTS5's virtual and shadow tables can't be; `test_every_table_is_strict` tells them apart with `PRAGMA table_list`). Integrity lives in the DB: `NOT NULL`, `CHECK` on every enum and paired-nullable column (embedding + space), and `UNIQUE` for every one-to-one rule (one Discord account per member per campaign, one campaign per transcript, one recording per transcript, campaign order positions). Discord ids are digits only.
  - Subtypes are enforced by composite FKs: Discord-only rows (`recording_discord`, speakers, rejoins) and local devices (`recording_devices`) reference `recordings(id, source)`, so a local recording can't carry Discord rows and vice versa.
  - Every FK child column is indexed (an unindexed child makes a cascade a full scan). Surrogate integer ids for everything user-renamable, so a rename is one `UPDATE`; UUIDs for recordings and jobs, which are in URLs.
  - Redundancy only where a constraint pins it: `journal_entries.campaign_id` (its composite FK to `campaign_transcripts` makes it equal the transcript's current campaign), and the subtype tables' constant `source` columns.
  - Deliberate non-derivations: `profiles.key` (alternate key: URL slug and clip filename), `transcript_speakers.display_name` (the name as written in that `.md`, which a profile rename doesn't rewrite), `recording_speakers.profile_id` (who that Discord user was when captured), and the search index.
  - `jobs` subject columns record only the job's direct subject, `ON DELETE SET NULL`, so history survives deletes. `jobs.params_json` is the one multi-valued column (an audit snapshot, never queried by field, constrained to a JSON object).
  - Timestamps are ISO-8601 UTC `TEXT` (recordings carry microseconds); booleans are `INTEGER CHECK IN (0,1)`; enum `CHECK`s mirror the Python constants (`test_enum_checks_mirror_python_constants`).
- **Legacy import** (`legacy_import.py`, frozen): files are copied to the backup dir, imported, and deleted after the commit (a leftover after a crash is deleted on the next start). Dirty data is repaired or dropped and listed in `import-report.txt`: members without a profile, duplicate or non-numeric Discord ids, a stem in two campaigns or with a path separator, missing embeddings, bad dates. An unreadable top-level file aborts the migration, naming the file.
- **File/row ordering rule:** the `.md` is the existence marker, the row follows it, and companion files follow the row. See "Output directory" for how `transcript_store` applies it.
- **Stored paths** are relative to the output root and POSIX-separated. `db.to_rel()`/`from_rel()` are the only converters; they resolve symlinks on both sides and reject absolute paths, backslashes, and `..`.
- **Inspection:** `wisper db status` (read-only: never migrates or claims a lease) reports version, `integrity_check`, `foreign_key_check`, leases, and schema drift (the live schema compared with this build's DDL replayed in memory; a DB made by an unmerged branch build shows as drift). `wisper db backup`, `wisper db dump`, `wisper db reindex`.

---

## LLM Post-processing

### Shapes
- **`refine.py` — surgical.** `fix_vocabulary()` asks for `{original, corrected}` pairs in ~25-line batches and accepts only substitutions close to a known hotword or enrolled name (`difflib.get_close_matches`, cutoff 0.7); freeform rewrites are rejected with a `UserWarning`. `identify_unknown_speakers()` uses a 20-line window with 5-line overlap and keeps suggestions with confidence ≥ 0.75 that name an enrolled profile — never auto-applied.
- **`summarize.py` — generative.** One structured-JSON call → `SummaryNote` (summary, loot, NPCs, follow-ups), rendered as an Obsidian sidecar with frontmatter (`type: session-summary`, provider, model, `refined`). Names become `[[wiki-links]]` only when they match an enrolled display name or a name in its notes, to avoid orphan vault pages.
- **`journal.py` — campaign-scoped, incremental.** `update_journal()` folds **one** session at a time: `[current journal] + [one .summary.md]` → rewritten journal. Prompt size is independent of campaign length. Folded sessions are `journal_entries` rows (migration v3, imported from the old `journaled_sessions:` frontmatter, which new folds no longer write; `export_journal()` adds it back for downloads and `--export`). `unjournalled_sessions()` returns campaign transcripts with a summary and no entry. A fold never modifies session summaries.
- **Journal write rule.** Body (file) and entries (rows) are two stores. A fold runs the LLM outside any transaction, writes `journal.md.wisper-pending`, then in one transaction inserts the entry and sets `campaigns.journal_sha256` to the pending file's hash (the commit point), then `os.replace`s it into place. The transaction also checks that `journal_sha256` still equals the hash the fold started from, so two concurrent folds can't silently drop one. `sync_journal()` runs before every read: a pending file whose hash matches is a committed fold that crashed before its move (moved into place); any other pending file is deleted; a missing `journal.md` resets the entries (fresh start); a different hash means the user edited it, which is adopted.
- **Stale journal.** `journal_entries` has a composite FK to `campaign_transcripts(campaign_id, transcript_id)`, so an entry always belongs to the campaign that holds the transcript. Moving, unassigning, or deleting a folded transcript deletes its entry, and a trigger on that delete (it also fires on cascades) sets `campaigns.journal_stale_since`. `register(origin="job")` on a folded transcript (overwrite, re-transcribe) sets it too. The journal text is never edited automatically; the Campaign and Journal pages show a banner and `wisper campaigns show` prints it. `campaign_manager._write_order()` updates positions in place for rows already in the campaign and delete-inserts rows moving in, because the composite FK refuses an in-place `campaign_id` change — so reorders don't mark anything stale.
- **Rebuilds.** Both reset the journal (`reset_journal()`: delete file and entries, then clear the stale flag the trigger just set) and fold in campaign order; a missing transcript or LLM error skips that session (`RebuildResult.skipped`). `refold_campaign()` (default; web **Rebuild journal**, CLI `--rebuild`) folds each session's existing `.summary.md`, summarizing only sessions without one — one call per session, and summary edits survive. `rebuild_campaign()` (web **Rebuild from transcripts**, CLI `--rebuild --resummarize`) re-summarizes everything first — two calls per session. Both run as `JOB_CAMPAIGN_JOURNAL` (`rebuild`/`resummarize` kwargs) behind a confirmation showing the call count.

Campaign fold order is `Campaign.transcripts` order — **insertion order**, not a date. `reorder_campaign_transcript()` (swap up/down) and `set_campaign_transcript_order()` (full permutation, else `ValueError`) fix it.

### Clients (`llm/`)
- One `LLMClient` ABC (`complete()`, `complete_json(schema)`); provider JSON mechanics are internal.
- SDKs are imported inside each client, so an Ollama-only install never hits an SDK import error; a missing SDK raises `LLMUnavailableError` with an install hint.
- **Ollama streaming:** `httpx.stream()` with no read timeout (long generations) but a connect/write timeout; prints a `·` per 50 tokens to stderr. Errors are split into connect failure ("daemon running?"), 404 ("model not found — `ollama pull`"), and other HTTP errors.
- **Ollama Cloud:** either keep `llm_provider = "ollama"` and pick a `-cloud` model (the local daemon proxies with `ollama signin` credentials), or use `llm_provider = "ollama-cloud"` with `OLLAMA_API_KEY`, which calls `https://ollama.com/api/chat` directly.
- **LM Studio:** OpenAI-compatible API on `:1234`, SSE streaming, `response_format: json_object`.

### Invariants
1. YAML frontmatter is never sent to the LLM and is preserved byte-for-byte (`parse_transcript()` keeps the raw string).
2. `refine` is dry-run by default; `--apply` writes `<stem>.md.bak` first. `summarize` won't overwrite a sidecar without `--overwrite`.
3. Cloud providers are opt-in; the default is local Ollama.
4. Soft-fail: an unreachable provider, 429/500, or missing SDK warns and returns early. `summarize --refine` still produces a summary (with `refined: false`) if refine fails.
5. API keys: env var first, then config; masked as `***` in `wisper config show`.
6. The CLI runs refine/summarize synchronously with a dry-run preview; the web runs them as queued jobs with no dry-run. The difference follows the surface, not a missing feature.

### Single sources of truth
- `config.py`'s `LLM_PROVIDERS`, `_LLM_DEFAULT_MODELS`, `_LLM_DEFAULT_ENDPOINTS`, and `_LLM_API_KEY_ENV` are the only provider tables. The CLI wizards, `llm.get_client()`, and the `--provider` choice list all derive from them.
- `path_utils.get_output_dir()` is the only output-dir resolver (via `config.get_output_root()`: `WISPER_OUTPUT_DIR`, then the `output_dir` setting, then `<data dir>/output`). It never looks at the working directory.
- `config.MODEL_SIZES`, `DEVICES`, and `COMPUTE_TYPES` back both the CLI `click.Choice` lists and web-form validation.

---

## Data Storage

All user data lives in the OS user data dir unless `WISPER_DATA_DIR` is set. `config.get_data_dir()` is the only resolver.

| Context | Path |
|---------|------|
| Windows | `%APPDATA%\wisper-transcribe\` |
| macOS | `~/Library/Application Support/wisper-transcribe/` |
| Linux | `~/.local/share/wisper-transcribe/` |
| Docker | `/data` (`WISPER_DATA_DIR=/data`) |

```
<data dir>/
├── config.toml
├── wisper.db                        SQLite database (see "Database")
├── backups/                         pre-migration DB snapshots and legacy-file copies; `wisper db backup` default
├── server.json                      bind address of a running `wisper server` (read by `wisper record`)
├── profiles/
│   └── embeddings/
│       └── <key>.mp3                ~12 s reference clip (profiles themselves are in wisper.db)
├── campaigns/
│   └── <slug>/journal.md            rolling campaign journal
├── recordings/
│   └── <recording_id>/                recording rows are in wisper.db; paths below are fixed
│       ├── per-user/<track>/NNNN.wav  60 s segments per Discord user id, or `mic` / `system`
│       ├── combined/NNNN.wav        60 s segments of the mixed track
│       ├── combined.wav             concatenated at session end
│       └── live_transcript.md       live draft (local sessions)
└── output/                          transcripts (default output root; see below)
```

### Output directory

The output root is `WISPER_OUTPUT_DIR`, else the `output_dir` setting (relative values are relative to the data dir; `wisper config set` stores an absolute path), else `<data dir>/output`. It used to be `./output` in the working directory when that existed; the database records which transcripts exist, so the root must not depend on where wisper was launched. On upgrade, migration v1 pins an existing install's working-directory `./output` into `output_dir` (only when the data dir already has a config or legacy store, and neither the env var nor the setting is set). Docker sets `WISPER_OUTPUT_DIR=/app/output`.

```
output/
├── <stem>.md                        transcript
├── <stem>.summary.md                LLM session notes (optional)
├── <stem><suffix>                   durable copy of a web-uploaded source (only when diarization ran)
├── <stem>_diar.json                 diarization segments (speaker data is in wisper.db)
├── <stem>_excerpt_<label>.mp3       ≤12 s clip per detected speaker
└── <stem>_excerpt_<label>.txt       words audible in that clip
```

**Diarization data** makes the transcript-centric enrollment wizard and the campaign relabel pass work after restarts. It is split (migration v4): `<stem>_diar.json` holds only `diarization_segments` (hundreds of KB, only ever read whole); each raw label's display name, provenance (`auto`/`manual`), and unit-length embedding are `transcript_speakers` rows, and the durable audio copy is `transcripts.audio_rel_path` (relative to the output root; audio anywhere else isn't tracked). The campaign is the transcript's `campaign_transcripts` row, so a reassigned transcript enrolls into its current campaign. Code reads it as a dict through `transcript_store.read_sidecar()` (keys `diarization_segments`, `speaker_map`, `speaker_map_source`, `speaker_embeddings`, `embedding_space`, `input_path`, `campaign`). Writes are narrow: a transcription job stores everything once with `write_sidecar()`; wizard renames change only names and provenance (`set_speaker_names()`); and the relabel backfill adds only embeddings (`set_speaker_embeddings()`). A sidecar that still carries the old fields is read as a fallback when the database has nothing for that transcript. This is deliberate, not leftover: it is the only copy of that data when the v4 migration ran while the transcripts drive was unmounted (or the sidecar was synced from an older install), and the next write moves it into the database. `_companion_paths()` keeps the same fallback for an old sidecar's `input_path`. Paths built from a stored stem go through `existing_form()`, which falls back to the NFD spelling when only that exists (files synced from a Mac onto ext4). Labels imported without provenance get it by the same rule `is_relabelable()` applies (pipeline-shaped names `auto`, anything else `manual`). Re-writing with a different audio copy deletes the old one; `register(origin="job")` on an existing transcript clears its speaker rows and sidecar, since they describe the old text (a web job writes fresh ones straight after).

- **The stored speaker map is authoritative** (`transcript_speakers`, `speaker_map` in `read_sidecar()`'s dict) for "what does raw label X display as": it is exactly what the formatter used, and every wizard rename updates it (the `.md` is rewritten first, then the rows; interval matching repairs a crash in between). Never reconstruct it from the rendered markdown when the database has it. Transcripts without it fall back to interval matching.
- **Every transcript delete goes through `transcript_store.delete_transcript()`** (single and bulk delete on `/transcripts`, and purging a recording). It follows the ordering rule: read the companion paths (the audio copy's name comes from `transcripts.audio_rel_path`, or an old sidecar's `input_path`, since collision suffixes mean it can't be derived), unlink the `.md`, delete the registry row in one transaction (cascading to its campaign position), then unlink the summary, sidecar, excerpt clips, and the audio copy — only a path inside the output dir, so old sidecars pointing at a tempdir are left alone. If the `.md` can't be removed, the row stays. A recording that produced the transcript goes back to `completed`, because `recordings.transcript_id` is `ON DELETE SET NULL`. `test_only_transcript_store_deletes_transcripts` flags any other `.unlink()` of a transcript.
- **Transcript and summary rewrites go through `transcript_store.save_transcript()` / `save_summary()`**, which write atomically (below) and then reindex that transcript for search. `test_transcript_writes_reindex` lists every other `atomic_write_text()` call by target (journal, `.bak` backups, excerpt text, sidecars, and the pipeline's new `.md`, which `register()` indexes).
- **Every transcript, summary, sidecar, and journal write goes through `transcript_store.atomic_write_text()`**: a temp file (`.wisper-tmp-<name>.<pid>-<thread>`) in the same dir, fsync, `os.replace()`. A crash leaves the old file or the new one, never a truncated `.md`. On Windows, `os.replace()` fails with WinError 5/32/33 while Obsidian, antivirus, or the indexer holds the target open; the helper retries 8 times with doubling backoff from 10 ms (about 1.3 s), then writes in place and logs a warning. `test_file_writes_are_atomic` flags any new direct `write_text()` (allowlist: `server.json`, the append-only live draft, the migration report).
- **Reconcile** (`transcript_store.reconcile()`): the registry vs. the `.md` files in the output root. Runs at server startup with `sweep=True` and cheaply (stat, insert, flag — no parsing) on `/transcripts`, the Campaign page, and `wisper transcripts list`. An unregistered `.md` gets a row; a row whose file is gone gets `missing_since` but keeps its campaign position, journal entry, and companions (the file may come back); a returning file clears the flag. Rows are never deleted automatically. On a case-insensitive filesystem (probed once per directory) a file differing from a missing row only in case renames that row. Stems are NFC-normalized, so NFD filenames (macOS) map to the same row. The startup sweep deletes atomic-write temp files older than 10 minutes and pattern-identifiable companions (`.summary.md`, `_diar.json`, `_excerpt_*`) that have neither a `.md` nor a row — never a generic audio file.
- **Relink** (`transcript_store.relink()`, Campaign page dropdown next to a MISSING entry): the missing row takes the new file's name, keeping its links; the new name's own row (created by reconcile) is dropped. Refused if the target is already in a campaign or journal. `POST /campaigns/{slug}/transcripts/relink` path-guards both stems at the route and again in the store.
- **Name collisions:** web uploads never replace or reuse a transcript silently. `GET /transcribe/name-check` tells the page when you pick a file, which offers **Overwrite** / **Cancel** before uploading; `POST /transcribe` refuses a taken name without `overwrite=on` (`?error=name_exists`). Web jobs call `process_file(skip_existing=False)`, which raises `TranscriptExistsError` instead of the CLI's skip, and every run re-checks immediately before writing. After `process_file()` returns, the job checks the `.md` exists, else fails with "Transcript file missing after write" and logs the transcripts folder. Recording hand-offs pass `overwrite=True` (the output is `<recording-id>.md`, only ever written by that recording) behind a confirmation. The CLI skip message names the existing transcript's campaign.
- **Registration:** `process_file()` writes the `.md`, then calls `transcript_store.register(stem, origin="job")` when the output is in the output root. An existing row keeps its id and campaign position (overwrite, re-transcribe); a row flagged missing is un-flagged. `--campaign` with output elsewhere prints a note and skips the association, since the web UI only sees the output root.
- Excerpt clip globs are `glob.escape()`-d so a stem like `mix*` can't match another transcript's clips.

### Search index (`search_index.py`)
Full-text search over every transcript and its `.summary.md`. The index is derived from the files and disposable: `wisper db reindex` drops and rebuilds it.
- **Tables (v7).** `search_index_state` has one row per indexed file, keyed `(transcript_id, kind)` with `kind` `transcript` or `summary`, and holds the `mtime_ns` and size the file had when indexed. No `transcript` row means the transcript is unindexed or stale. `search_blocks` has one row per speaker block (`block_idx`, `speaker`, `start_s`) or summary section. Its composite FK to the state row means deleting a transcript's state rows removes its blocks, which is how "mark stale" works. `search_fts` is contentless FTS5 (`content=''`, `contentless_delete=1`, porter + `unicode61 remove_diacritics 2`) over the block text, with `rowid = search_blocks.id`. FTS tables can't hold foreign keys, so the `search_blocks_ad` trigger deletes the FTS row, and it fires on cascades: deleting a transcript clears its index.
- **Blocks.** Transcripts use `formatter.searchable_blocks()`, the numbering of `parse_transcript_blocks()`. A transcript with neither speakers nor timestamps falls back to one block per text line. Summaries use one section per `## ` heading, with section 0 as everything before the first heading. Frontmatter is never indexed.
- **No prefix indexes.** Measured on 21 MB of real transcript text: `prefix='2 3'` made the index 2.3× larger (1.31× vs 0.57× of the text) and saved about 10 ms on a two-letter prefix query (15 vs 26 ms). `Stra*` still works without them.
- **Freshness.**
  - The app's own writes reindex immediately: `save_transcript()`/`save_summary()`, `register(origin="job")` (new transcripts and overwrites), and `relink()`. `reindex()` stats each file *before* reading it and stores that stat, so a write landing between the two leaves a mismatch rather than fresh-looking state over old blocks. It is one short transaction: delete the state rows (cascading to the blocks), insert the new state, then the blocks.
  - Edits made elsewhere (Obsidian, sync) are caught by `check_freshness()`, which reconcile calls. It compares only `stat()`s: a changed mtime or size, a deleted summary, or a summary that appeared since indexing marks the transcript stale. A search result whose file no longer matches its state row is also shown as "changed — reindexing" and marked stale.
  - Marking stale only deletes state rows. The **backfill worker** (a daemon thread started in the server lifespan, woken by `request_backfill()`) indexes every present transcript without a `transcript` state row, one transaction each with a 50 ms pause between them (so a capture hot-path write, which waits at most 500 ms, always gets the lock), so no request parses a transcript, a big archive doesn't delay startup, and an interrupted build resumes. Undecodable bytes are replaced and an unreadable file is indexed as empty, so a bad file is indexed once rather than retried forever (and "Indexing N of M" can finish). Tests set `search_index.AUTOSTART_WORKER = False` (conftest) and call `run_backfill()` directly.
  - A reconcile-origin registration doesn't index; the backfill does. Case-fold renames in reconcile mark stale. Missing transcripts keep their index but are left out of results.
- **Queries.** `search()` quotes every word or `"phrase"` before `MATCH` (a trailing `*` stays a prefix search), so FTS5 syntax is inert, and a query FTS5 still rejects returns a generic message. The `hits` CTE applies the campaign, speaker, and kind filters before ranking and is `MATERIALIZED`, because when it is flattened `bm25()` loses the FTS context and SQLite rejects it. Results are grouped by transcript (best `bm25()` first, `transcript_id` as tie-break), paged 20 transcripts at a time, with the top 3 hits each.
- **Web** (`web/routes/search.py`, `search.html`, the sidebar form): filters are accepted only when they exactly match a dropdown value. Transcript pages wrap each block in `<span id="b-<index>" class="block-anchor">` (`_anchor_blocks()`, numbered by `searchable_blocks()`); summary pages give each `<h2>` `id="s-<n>"`, with `s-0` at the top (`_anchor_sections()`). With `?q=`, `partials/search_highlight.html` gets `highlight_pattern(q).pattern` (word characters and `|` only) through `tojson`, and marks the matches inside the target block or section.
- **Snippets** are rebuilt from the current file by `block_idx`, after checking its stat against the state row (and again after the read). Each piece of text is HTML-escaped and only the `<mark>` tags are markup. Highlighting is approximate: porter can't be reproduced in Python, so each query word is matched by prefix after dropping `-ing`, `-es`, `-ed`, or `-s`. A hit with nothing highlighted is acceptable.

### Config keys
`model`, `language`, `device`, `compute_type`, `vad_filter`, `timestamps`, `similarity_threshold`, `min_speakers`, `max_speakers`, `hf_token`, `hotwords`, `use_mlx`, `forced_alignment`, `parallel_stages`, `llm_provider`, `llm_model`, `llm_endpoint`, `llm_temperature`, `anthropic_api_key`, `openai_api_key`, `google_api_key`, `ollama_cloud_api_key`, `discord_bot_token`, `discord_default_guild`, `discord_default_channel`, `discord_presets`, `output_dir`.

`default_mic_profile_key` is also stored, written by the Record page (not in `DEFAULTS`, so not settable via `config set`).

`wisper config set` rejects keys not in `DEFAULTS` and coerces values to the default's type: bool → int → float → comma-list → string (bool first, since `bool` subclasses `int`). Keys in `config.CONFIG_CHOICES` must be one of their values; the web Config page applies the same rule to its `str` fields with options.

`omegaconf` is declared explicitly in `pyproject.toml`: pyannote imports it but doesn't list it.

---

## Recording

Two managers write the same on-disk layout: `BotManager` (Discord, via a Java sidecar) and `LocalCaptureManager` (local mic + system audio). Only one session may be active across both (`_other_session_active()` → 409).

### Recording layer (`recording_manager.py`)
- **Tables** (migration v5): `recordings` (id, source, name, notes, campaign, transcript, `capture_status`, times, `recovered_at`); the subtypes `recording_discord` (guild, channel) and `recording_devices` (local mic/system names) with a composite FK to `recordings(id, source)`, so a local recording can't get Discord rows and vice versa; `recording_speakers` (Discord user → profile id, NULL = unbound), `recording_segments`, `recording_markers`, `recording_rejoins`. Times are ISO-8601 UTC with microseconds, so two markers in one second are two rows.
- **Derived, not stored:** `combined_path` (`recordings/<id>/combined.wav` if it exists), `per_user_dir`, segment paths (`combined/NNNN.wav`; only the mixed stream is tracked), marker `elapsed_s` (`marked_at − started_at`), `unbound_speakers` (speakers with no profile), `recoverable`, and **status**: `transcribed` = has a transcript (`transcript_id`, `ON DELETE SET NULL`, so deleting the transcript makes the recording transcribable again), `transcribing` = the job queue has a pending or running job for it (`set_job_lookup()`, registered by `JobQueue`; `Job.recording_id`), else the stored capture state. A restart has no active job, so nothing can be stuck in `transcribing`, and a failed or cancelled job needs no status revert. `save_recording()` raises `ValueError` for a `combined_path` or segment path off the layout, and ignores `transcribing`/`transcribed` (the stored capture state stays).
- **No lost updates:** the managers hold one long-lived `Recording` per session, whose name and notes can go stale while the user edits them. So capture code never writes the whole object: it uses targeted writers, `update_recording_status()`, `bind_recording_speaker()` (a binding is never undone), `append_rejoin()`, `append_segment()`, and `append_marker()`. `test_capture_code_uses_targeted_writers` keeps `save_recording()` out of `discord_bot.py`, `local_capture.py`, and `jobs.py`. `save_recording()` is for creating a recording and for edits made from a freshly loaded object. It updates the recording's own fields and only *inserts* missing segment/marker/rejoin rows (a segment's `finalized` can only go up).
- **Capture hot path:** `record_completed_wav_segment()` runs whenever the combined writer rotates and once at finalise, with a 500 ms busy timeout instead of 5 s. Zero-frame or unreadable segments are skipped; `duration_s` is wall-clock-derived. It never raises: if the database stays busy the row is skipped and logged, and startup restores it. `bind_recording_speaker()` and `append_rejoin()` use the same short timeout. `append_marker()` doesn't: it runs from a web request, and a dropped marker can't be restored, so it waits the normal 5 s.
- **Crash recovery:** `reconcile_on_startup()` marks `recording`/`degraded` sessions `failed` (audio stays on disk) and restores segment rows missing for `combined/NNNN.wav` files. A failed session with segments and no `combined.wav` is `recoverable`: **Recover recording** on its page (or `wisper record recover <id>`, via `POST /api/recordings/{id}/recover`) joins the segments with `concat_wav_segments()` off the request thread (segments are self-contained WAVs, so no repair step) and marks it `completed` with `recovered_at`; the page then notes the last partial minute may be missing.
- **Import** (v5): `recordings.json` + each `metadata.json`, backed up and deleted after commit. `transcribing`/`transcribed` import as `completed`, active states as `failed`; `transcript_path` links by stem only under the output root (first recording keeps a shared one); an unknown campaign, a missing profile, Discord speakers on a local session, non-numeric ids, and off-layout segments are repaired or dropped and reported.
- **Ids:** `_validate_recording_id()` uses the four-step CodeQL pattern.
- **Fields:** `source` (`discord`|`local`) and `devices` (display names only, never paths). `name` is a display-only session title (trimmed, ≤200 chars); `id` (uuid4) backs the directory, so `name` needs no path guard.
- `delete_recording()` removes only the rows (children cascade); route-level `_purge_recording_files()` removes files (see "Record routes").

### Audio writer (`web/audio_writer.py`)
- **`SegmentedWavWriter`:** rotating self-contained 16 kHz mono 16-bit WAVs via stdlib `wave`. Rotation is by sample count (media time), so faster-than-real-time tests work. Resumes at the next index on construction. `writeframes()` rewrites header sizes on each call and `write()` flushes, so a crash leaves a readable segment.
- **`downsample_48k_stereo_to_16k_mono()`:** Discord's fixed 48 kHz stereo → average L+R, 3-tap low-pass, decimate by 3. Pure NumPy (`audioop` is gone in 3.13). ~6× less disk than raw.
- **`resample_to_16k_mono()`:** local capture's arbitrary rate (e.g. 44.1 kHz) via `scipy.signal.resample_poly` at a gcd-reduced ratio. Scales float [-1, 1] to int16 before rounding.
- **`concat_wav_segments()`:** frame-concatenates a writer's segments, skipping unreadable/empty ones; `None` if nothing was written.

### File-format invariants
1. Each segment is a self-contained WAV whose header sizes match its data.
2. Segment rows are append-only (`append_segment()`), one short transaction each.
3. Segments are ≤60 s.
4. Layout is `recordings/<id>/per-user/<track>/NNNN.wav`.
5. `Recording.status` has a distinct `recording` state; live consumers watch only `recording`/`degraded`.

### Discord (`web/discord_bot.py`)
- `_unix_socket_source` launches the JDA sidecar (Java 25, JDAVE for DAVE decryption; `_find_sidecar_jar()` locates the JAR). `_read_frame()` parses length-prefixed user_id + 48 kHz stereo PCM.
- `_route_frame` downsamples and writes per-user tracks. `__mixed__` (JDA's pre-mixed track, one frame per real 20 ms tick) goes straight to the combined writer, so combined duration tracks wall-clock time regardless of speaker count.
- Known Discord ids auto-tag to profiles via `lookup_profile_by_discord_id()`; unknown ids go to `unbound_speakers`.
- `_session_loop` auto-rejoins with backoff `[2, 5, 15, 30, 60]` s; `_handle_disconnect` treats transient close codes as retryable and permanent ones (e.g. 4014) as failed.
- `_finalise()` closes writers and writes `combined.wav` with `concat_wav_segments()` (a no-audio session writes none, so the derived `combined_path` is `None`), then records only the status change.
- `audio_source_factory` is injectable for tests.

### Local capture (`web/local_capture.py`)
- Thread-based, synchronous API mirroring `BotManager`; async callers use `asyncio.to_thread()`.
- Two capture threads only fill per-track byte FIFOs. One tick thread every ~20 ms drains a chunk from each (silence-padding a starved track; draining surplus when a FIFO passes ~2 s), writes both tracks, and int32-sums them into the combined writer.
- `stop_session()` is the only finalizer, so a finished ticker and an external stop can't both finalize.
- `enumerate_devices()` returns `available: False` rather than raising when `soundcard` is missing or fails, and reports `default_microphone_id`/`default_loopback_id` so the Record page preselects the OS defaults.
- A dead capture thread (e.g. unplugged USB mic) marks the session `degraded`.
- `set_live_sink(callback)` taps the mixed chunks for live transcription; a failing sink disables itself.
- Level gauge: per-track peak RMS since the last read (`get_and_reset_levels()`), so transients between ~1 s polls aren't missed.
- `_active_recording` is not cleared after a session; check `.status`.
- `capture_factory` and `ticker` are injectable for tests.

### Live transcription (`web/live_transcribe.py`)
- `LiveRingBuffer` accumulates aligned mic/system/mixed PCM from the live sink.
- `find_commit_boundary()` scans only the last ~1.6 s with Silero VAD (avoids O(n²) rescans). It cuts once ≥0.6 s of silence follows speech, or force-cuts at 15 s.
- `attribute_speaker()` labels by louder track (`You`/`Other`, or the "This is me" profile name). If neither track clears the noise floor (`NOISE_FLOOR_RMS`, default 300), the chunk is dropped, since Whisper hallucinates text over room tone.
- `transcribe_array()` reuses `transcriber._model` on an in-memory array; safe because `JOB_LIVE` holds the only queue slot.
- `get_noise_floor` is re-read every iteration so the slider applies to the next chunk.
- `initial_prompt` is never chained from previous output; chaining causes repetition loops.
- Per-chunk errors go to `on_warning` (the job log) and don't end the loop.
- No pyannote here: the post-session Transcribe pass is the authoritative transcript.

### Record routes (`web/routes/record.py`)

**JSON API**
- `GET /api/record/status` — `{"active": false}` or the active recording + `"active": true`; backs the global banner.
- `GET /api/record/devices`, `POST /api/record/start-local` / `stop-local` — device ids resolve to names server-side.
- `POST /api/record/start` / `stop` — Discord sessions.
- `GET /api/record/channels` — guilds and voice channels the bot can see. No UI uses it yet.
- `POST /api/record/live-noise-floor` — updates the running `JOB_LIVE` job's noise floor.
- `/api/recordings/*` (list, detail, transcribe, delete) back `wisper record list/show/transcribe/delete`. `_recording_to_dict()` never includes filesystem paths.
- `POST /api/recordings/{id}/delete` removes only the database rows unless `?purge=true`, because it has no confirmation step. The CLI passes `purge=true` after its own prompt.
- `_current_active_recording()` is the one resolver for "which manager has the active session".

**HTML**
- `GET /record` — start forms, active-session toolbar, live ticker, noise-floor slider, level gauges (0–2000 RMS; the slider is 0–1000 so speech doesn't pin the bar). Speaker meters render only for Discord (local sessions have no per-participant data).
- `GET /record/sse` — status stream; local sessions include `mic_rms`/`system_rms`.
- `POST /record/marker` — `fetch()`, not a form, so the ticker keeps its scroll position.
- `GET /recordings` — list with bulk-select delete. Active and degraded rows have no checkbox. Bulk submit uses a separate hidden form because each row already contains its own form.
- `GET /recordings/{id}` — detail; for a stopped local session without a transcript, renders `live_transcript.md` server-side as a static draft.
- `GET /recordings/{id}/live` — SSE of live lines with index resume; caches the last-seen job so a line committed as the job ends still streams; ends with a bare `{"type": "snapshot"}` when the job is gone.
- Delete routes call `_purge_recording_files()` first: removes `recordings/<id>/` and, if transcribed, the transcript and its sidecars (reusing `transcripts.py` helpers). Best-effort, and refuses while the session is active (writers still hold file handles).
- "This is me" choice is remembered as `default_mic_profile_key`.
- `_validate_recording_id()` plus a `_uid_guard` round-trip gate every id used in a path or redirect.

**Template rules**
- `{% block extra_scripts %}` must be a top-level sibling of `{% block page %}`. Nested, Jinja renders it twice under `extends`, opening duplicate SSE connections.
- An `{% if %}` around a `{% block %}` tag does not make the block conditional; put the condition inside the block.
- Inline calls into `app.js` run inside a `DOMContentLoaded` listener, because `defer`red `app.js` executes after inline scripts.

### Transcribe hand-off
`POST /recordings/{id}/transcribe` copies `combined.wav` to the output dir and submits a normal transcription job with `original_stem=recording.id`, `title=recording.name`, and the recording's campaign. `title` is separate from the stem because a free-text name must never become a filename.

- The job carries `recording_id`, which is what makes the recording read as `transcribing` while it is pending or running. `process_file()` associates the transcript with the recording's campaign; `on_complete` links it (`link_transcript()`), so the recording reads as `transcribed`. Nothing needs undoing on failure or cancellation.
- `overwrite=True`: the output is `<recording-id>.md`, only ever written by that recording, so Re-transcribe (behind a confirmation) replaces it and keeps its campaign place.
- Stopping a session never auto-queues a transcribe.

`/transcripts` lists `completed` recordings with audio on disk under "Awaiting transcription", each with a Transcribe button.

---

## Web Interface

### Stack

| Layer | Choice | Notes |
|-------|--------|-------|
| Backend | FastAPI (uvicorn) | `wisper server`; binds `127.0.0.1` unless `--host` is given |
| Templates | Jinja2 | Server-rendered; HTMX for partial updates |
| Reactive UI | HTMX 1.9 (vendored) | `static/htmx.min.js` |
| Styling | Tailwind CSS v4 | `static/tailwind.min.css` prebuilt; tokens in `static/input.css` `@theme` |
| Fonts | Newsreader, Geist, JetBrains Mono, Instrument Serif | Self-hosted woff2 (SIL OFL) |
| Icons | SVG macros | `partials/icons.html` |

No CDN or network access is needed at runtime.

### Design system
"Studio": 204 px left sidebar, near-black background (`#0b0f17`), paper-cream text (`#f3ead8`), cyan (`#5fd4e7`) reserved for live/active states.

Tokens in `input.css` `@theme`: `--color-ink-*` backgrounds, `--color-paper*` text, `--color-rule*` borders, `--color-accent*`, `--color-signal-{green,amber,rose}`, `--font-{serif,sans,mono}`. Component classes (`sidebar`, `toolbar`, `pill-*`, `btn`, …) live in `@layer components`.

### Templates and assets
- All pages extend `base.html`: sidebar, `#global-recording-banner`, and `toolbar`/`page`/`extra_scripts` blocks.
- The sidebar polls `/api/sidebar-status` every 5 s.
- `app.js` and `tailwind.min.css` are cache-busted with `?v={{ static_mtime(...) }}`, which stats the file on every render. The package version can't be used because static edits don't restart a `--reload` server.
- Transcript stems in URLs use the `urlencode` filter; redirect `Location` headers use `urllib.parse.quote()` (latin-1 headers); JS uses `encodeURIComponent()`.

### Global JS (`static/app.js`)
- `wisperConnectLiveStream(url, onLine, onSnapshot)` — shared SSE connector for live lines. De-dupes on `start_s|speaker|text` because the server's resume cursor is per connection, so a reconnect replays everything.
- `wisperTickerAppend()` / `wisperTickerAppendMarker()` — Record page ticker rows (markers are rose, italic, speaker-less). The ticker scrolls and never drops lines.
- `wisperPlayExcerpt` — inline audio player for the Speakers page and enrollment wizard.
- Global recording banner — polls `/api/record/status` every 4 s (except on `/record`) and shows an elapsed timer and a Stop button. The session name is HTML-escaped before `innerHTML`.

### Job Queue (`web/jobs.py`)
In-memory `dict[str, Job]` drained by one asyncio task; each job runs via `asyncio.to_thread()`. **One job at a time** for every type, because the model globals aren't thread-safe. The queue itself isn't persisted: after a restart nothing resumes, but every job's record survives in job history (below).

**Job history** (`job_history.py`, migration v6 `jobs`): every job is written through at submit (`JobQueue._enqueue()`), when the worker starts it, at its terminal state, and on a pending cancel. Rows keep the generic error text (never exception text), the last 200 log lines, an allowlisted `params_json` (no paths, secrets, or free text; transcription jobs add the resolved `output_root`), and the job's direct subject (`transcript_id`, `campaign_id`, `recording_id`, all `ON DELETE SET NULL`, linked only to rows that exist). At startup, rows left `pending`/`running` become `failed` / "Interrupted by restart"; jobs are never resumed. A clean shutdown records the running job the same way as it happens: `stop()` cancels the worker, and `_worker` catches `asyncio.CancelledError` (a `BaseException`, so `except Exception` misses it) to mark the job failed and delete its temp upload, instead of letting the `finally` call it completed. The in-memory queue keeps its 50-job cap; the database keeps every job. The dashboard merges live jobs with history (20 newest); `/jobs/history` pages all of it (50 per page, filter by type/status, or `?transcript=`/`?campaign=` from the "Jobs" links on those pages); `/transcribe/jobs/{id}` falls back to the stored record when the job is no longer in memory. A recording's `transcribing` state and job link come from `jobs.recording_id`. `test_enum_checks_mirror_python_constants` fails if a `JOB_*` type, status, capture state, or speaker source is added without a migration.

Job types:
- **Transcription** — `process_file()`, optionally chaining refine/summarize (`post_refine`/`post_summarize`) in the same thread.
- **`refine` / `summarize`** — `submit_llm()`. Provider output is captured by redirecting `sys.stderr` for the job thread (safe under one-job-at-a-time).
- **`JOB_CAMPAIGN_JOURNAL`** — `submit_journal()`: fold next, fold all, or rebuild.
- **`JOB_SPEAKER_RELABEL`** — `submit_relabel()`: `relabel_campaign()` with audio backfill; logs renames and skipped sessions.
- **`JOB_ENROLL`** — `enroll_mode` selects:
  - `wizard` — embeddings for renames already applied by the wizard. Carries only the transcript path, rename groups, and device; re-reads the sidecar. `output_path` is set at submit so "View transcript" works immediately. For a campaign transcript it then propagates names with `relabel_campaign(backfill=False)`; a propagation failure is logged and doesn't fail the job.
  - `standalone` — `/speakers/enroll` upload: convert → diarize → pick the speaker with the most speech → enroll or EMA-update. The upload is renamed to `wisper_enrollsrc_<job-id>` at submit and deleted in a `finally`.
  - `recording` — enroll an unbound Discord speaker from their per-user track, then bind the id in the recording and campaign (best-effort follow-ups). Never deletes recording audio.
- **`JOB_LIVE`** — open-ended; runs `run_live_loop()` until `stop_live()` sets `live_stop_event`, which is normal completion, not cancellation. It holds the only worker slot for the whole session. Lines go to `job.live_lines` and `recordings/<id>/live_transcript.md`.
  - `noise_floor` lives in `job.kwargs` and can be changed while running (`set_live_noise_floor()`).
  - `find_live_job_for_recording()` scans pending/running jobs instead of storing the job id on the recording.
  - If `live_stop_event` is already set when the worker reaches a `JOB_LIVE` job (the session ended while it waited behind another job), the job fails with "Live transcript never started — the job queue was busy for the whole session". `_start_live_transcription()` also warns up front (`?error=live_delayed`) when another job is active.
  - `stop_all_live()` runs on shutdown before `job_queue.stop()`: a running `to_thread` can't be cancelled, and an unstopped live thread would block exit.
  - The generic cancel button routes `JOB_LIVE` through `_stop_live_transcription()` (clear the sink, then `stop_live()`); the normal cancel event is never checked by the live loop.

**Errors.** `_set_job_error()` maps exceptions to generic strings — `InterruptedError` → `"Cancelled"`, `FileNotFoundError` → `"Input file not found"`, else a per-type "see server logs" message — and logs the real traceback. `job.error` renders into HTML, so exception text (which can contain paths) is never shown.

**Progress.** Per-job `tqdm.write`/`tqdm.__init__` patches feed `job.log_lines`/`job.progress`. In parallel mode, `[progress:<channel>]` messages go to `job.progress_channels`. `GET /transcribe/jobs/{id}/stream` streams all of these plus status.

**Cancel.** `POST /transcribe/jobs/{id}/cancel` fails a pending job immediately; for a running job it sets `_cancel_event`, which the tqdm patch turns into `InterruptedError` on the next write.

**Web uploads.** `JobQueue.submit()` renames the `wisper_upload_*` temp file to `<stem><suffix>` immediately and records `Job.is_web_upload` from the original name. On success with diarization data, the file moves next to the transcript (collision-safe `_1`, `_2`, …) and `job.input_path` is updated before the sidecar is written. Without diarization data, or on failure/cancel, it is deleted. `is_web_upload` is then cleared so later post-processing failures can't delete the durable copy.

**Retention.**
- At most 50 terminal jobs are kept (`_MAX_RETAINED_JOBS`, oldest pruned first); pending/running jobs are never pruned.
- `log_lines` are capped at 1000 and `live_lines` at 2000, trimming from the front and counting drops.
- `_append_capped()` and `resume_slice()` are the shared implementations for capping and SSE resume.
- These are internal constants, not config.

### Upload progress
`transcribe.html` posts via `XMLHttpRequest` for byte-level progress on large files; the route is unchanged.
- Upload `progress` drives the bar; upload `load` switches to "Processing…" while the server spools the file.
- On completion the page navigates to `xhr.responseURL`. The route always 303s, so this covers both success and validation errors.
- Network errors re-enable the buttons and show an inline error. Both submit buttons are disabled during the request.
- `start_transcribe` validates `model_size`/`device`/`compute_type` before spooling the upload; invalid values redirect to `?error=invalid_option` without echoing the value.

### Speaker enrollment (web)
A post-job wizard replaces interactive CLI prompts. There are two entry points, both backed by `web/enroll_shared.py` so they can't drift:
- **Transcript-centric** (`/transcripts/{name}/enroll`) — reads `_diar.json`; restart-safe; preferred.
- **Job-based** (`/transcribe/jobs/{id}/enroll`) — uses in-memory job state; only while the server session lives.

On transcription completion, `jobs.py`:
1. Moves web-upload audio next to the transcript (see "Web uploads").
2. `_extract_speaker_excerpts()` cuts each label's clip from its longest solo diarization turn (same selection as embedding extraction), clamped to `min(12 s, turn length)` so the clip doesn't bleed into another speaker. The `.txt` holds every aligned word run overlapping the clip. A label with no usable turn falls back to its longest aligned segment.
3. `_write_enrollment_sidecar()` stores the segments, the `speaker_map` the formatter used (provenance `auto`), per-label embeddings, and the audio copy's path via `transcript_store.write_sidecar()`.

`enroll_shared.py`:
- **`resolve_current_names()`** — the one answer to "what does this raw label display as": the stored speaker map (`transcript_speakers`), else `build_legacy_label_map()` (interval-matching timestamps against diarization spans).
- **`template_current_names()`** — drops entries whose value is still a raw `SPEAKER_XX` label, so untouched fields start empty and can't create junk "SPEAKER_03" profiles.
- **`apply_renames()`** — fast, runs in the request. Ignores raw-label-shaped new names. Rewrites the transcript in **one pass**: each block is attributed to a raw label once: by its displayed name when exactly one label currently has that name, otherwise by timestamp among the labels sharing the name (or all labels if the name isn't in the map). The name comes first because block timestamps are whole seconds and overlapping turns put many of them inside another speaker's turn; timestamp-first attribution silently skipped those blocks. Only renamed labels' blocks change. Changed labels get `speaker_map_source` = the caller's `source` (`manual` from the wizard); pipeline-shaped names (`AUTO_NAME_RE`) are never grouped for enrollment. This handles swaps (Alice↔Bob) and two labels sharing a display name. Frontmatter `speakers:` is rewritten via YAML round-trip (`formatter.rewrite_frontmatter_speakers()`), skipping names shared by several labels. The `.md` is written with `save_transcript()` (reindexed for search), then the names with `set_speaker_names()`. Returns eligible renames grouped by target name.
- **`enroll_profiles()`** — slow, runs in `JOB_ENROLL`. Converts once. Existing profiles get EMA updates (`update_embedding()`), never `enroll_speaker()`, which would overwrite metadata. New profiles get `enroll_speaker()` with a pre-averaged `embedding=` when several labels map to one name. Adds each profile to the campaign if it isn't already a member.
- **`find_excerpt_clip()`** — the one excerpt disk lookup + CodeQL guard, scoped to a single transcript stem (every transcript reuses `SPEAKER_00`…). The job-based excerpt route 404s once the job is gone instead of guessing.

Submit flow: rename synchronously; if nothing is eligible, redirect back; if the source audio is gone, redirect with `?notice=enroll_audio_missing` and no job; else `submit_enroll()` and redirect to the job page via `job.id`. The wizard shows an "audio missing" banner before submit when `input_path` doesn't exist.

### Startup cleanup
`app._cleanup_orphaned_uploads()` deletes `wisper_upload_*`, `wisper_enroll_*`, and `wisper_enrollsrc_*` from the temp dir at startup. Running jobs never match these globs: uploads are renamed at submit (`<stem><suffix>`, `wisper_enrollsrc_<job-id>`) and jobs clean up their own files. The sweep only covers the crash window before submit, and at startup the in-memory queue is empty.

### Route security
CodeQL scans every PR. The patterns (see also CLAUDE.md):
- **Path traversal (CWE-22):** `os.path.basename()` then `os.path.abspath(join(base, name)).startswith(base + os.sep)`. Not `Path.resolve()` — CodeQL doesn't treat it as a sanitizer. Filenames use this guard rather than an allowlist so Unicode titles work.
- **Open redirect (CWE-601):** `_validate_job_id()` (regex + `os.path` round-trip), then redirect using the server-generated `job.id`, never the user value.
- **Errors:** generic codes (`?error=enroll_failed`), never `str(exc)`.
- **No client-supplied paths:** the output dir is always `path_utils.get_output_dir()`.
- **XSS:** rendered transcript markdown passes through `_sanitize_html()` before `| safe`. It strips `<script>`/`<iframe>`/`<object>` with content, `<embed>`, `on*` attributes, and `javascript:`/`data:`/`vbscript:` URLs (after removing whitespace/control characters and lowercasing).
- **Headers** (`_SecurityHeadersMiddleware`): `nosniff`, `X-Frame-Options: DENY`, `strict-origin-when-cross-origin`, and a CSP that still allows `'unsafe-inline'` scripts because templates contain inline `<script>` blocks.
- **Trust model:** no auth or CSRF (single-user tool). The server binds `127.0.0.1` by default; Docker passes `--host 0.0.0.0`. State-changing endpoints are POST-only.
- **Model menus:** provider-returned model names are rendered with `textContent`, never `innerHTML`.
- **Cloud model discovery** accepts an optional typed key via POST (never in a URL), capped at 512 chars. Errors return a generic message, never exception text that could contain key fragments.

### Job progress display
The job page shows step pills and one bar split into equal per-step slices:
- Transcription: T → D (→ A) → F (→ R → S with post-processing). Refine: R. Summarize: S. Enroll: E. Journal: J.
- **Align step:** shown when `Job.will_align` is true, which mirrors `process_file()` (diarization on, a HuggingFace token, `forced_alignment_enabled()` for the job's device). `JobQueue.submit()` freezes the `forced_alignment` setting into the job's kwargs so the pill and the run agree if the config changes while queued. The "Aligning" bar and the "Aligned words" log line drive it.
- The active step is detected from log keywords; tqdm percentages fill its slice. Parallel mode fills T and D from their channels.
- With no tqdm update for ≥5 s (LLM steps, enrollment), the bar creeps ~1 %/5 s up to 90 % of the slice. The ETA and rate clear when a step starts, so a finished step's `0:00` doesn't linger.
- MLX transcription reports real progress: `_transcribe_mlx()` passes `verbose=False`, which enables mlx-whisper's frame-based tqdm bar (the default `None` disables it). It advances once per 30 s decoding window.
- The `done` event carries `summary_path` and `job_type` so the page shows the right follow-up links.

### Transcripts and dashboard
- `.summary.md` files are hidden from the list and shown as a notes icon on their transcript.
- Bulk actions: tick rows on `/transcripts` to delete them (`/transcripts/bulk-delete`, through `delete_transcript()`) or move them to a campaign (`/transcripts/bulk-campaign`).
- Missing transcripts (`missing_since`) are flagged on the Campaign page, with **Relink** and **Remove**.
- The Dashboard System card shows Whisper state and LLM readiness. For cloud providers it shows only a boolean and a generic hint; keys never reach templates.

### Settings page (LLM)
- Provider, model, endpoint, temperature, and API keys. A blank key field never overwrites a stored key. Env vars take precedence.
- A custom model combobox is used because `<datalist>` behaves inconsistently across browsers. Options come from `/config/ollama-status`, `/config/lmstudio-status`, `/config/ollama-cloud-catalog`, and `POST /config/{anthropic,openai,google}-models` (filtered to chat models). Local endpoints are read from saved config, never from request input. Selection uses `mousedown` so it fires before `blur` closes the menu.
- `POST /config/open-data-dir` opens the data dir in the OS file manager.

### Offline assets and CI
- `static/htmx.min.js`, fonts, and `tailwind.min.css` are committed.
- Every Tailwind build goes through `tailwind.build_css()`, which pins the binary to `TAILWIND_VERSION`. Different Tailwind releases emit different CSS for the same source; with one version, Windows and Linux output is byte-identical. `pytailwindcss` defaults to "latest" and caches whatever that was on first download, so an unpinned call drifts per machine. `tests/test_tailwind.py` fails if any caller bypasses the module.
- `app._build_tailwind()` rebuilds CSS at startup when `input.css` is newer than the output (mtime check); `pytailwindcss` needs no Node.
- Tailwind v4 scans every tracked text file (Markdown, docstrings, tests), not just templates, so a class-like word anywhere can change the output.
- `scripts/vendor.py` (`--check` to audit) re-downloads HTMX/fonts and rebuilds Tailwind.
- CI rebuilds Tailwind and fails on `git diff --exit-code` if the committed CSS is stale. The Claude Code pre-commit hook (`.claude/hooks/pre_commit.py`) runs the same rebuild before each commit Claude makes.

### Docker and launchers
- `docker-compose.yml`: `wisper`/`wisper-cpu` (CLI) and `wisper-web`/`wisper-cpu-web` (port 8080), sharing `x-volumes`/`x-env` anchors (`WISPER_DATA_DIR=/data`, `WISPER_OUTPUT_DIR=/app/output`); secrets come from `.env`. CLI and web containers can share `./data` safely (verified); a native CLI alongside a running Docker Desktop container can't (see "Database"). The `Makefile` wraps common `docker compose` commands.
- `start.command` (macOS), `start.bat` (Windows), `start.sh` (Linux) run setup on first launch, then start the server and open the browser. The shell launchers are committed executable.
- **Dependency refresh:** setup stamps `.venv/.wisper-deps` after `pip install -e .`. The launchers reinstall whenever `pyproject.toml` is newer than the stamp (or it's missing), so updating an existing install picks up new dependencies. A failed reinstall leaves the stamp stale and the server starts anyway. `word_alignment` also names the fix when `transformers` is missing, since that's how a stale install shows up.
- `setup.sh`/`setup.ps1` check Python ≥ 3.13 and then SQLite ≥ 3.43 with FTS5 contentless delete (the same probe as `db.check_sqlite_capabilities()`, inlined since the package isn't installed yet), so an old system Python fails before ~2 GB of models download rather than at first server start.
- `setup.sh`/`setup.ps1` probe Ollama (`:11434`) and LM Studio (`:1234`) and offer a model picker, and show progress for long installs.
- `setup.ps1` installs CUDA `torch`/`torchaudio` **before** `pip install -e .`. Otherwise pip resolves the CPU `torch` first and dependent packages bind to the wrong build (`torch has no attribute _utils`).

---

## Test Strategy

- Tests live in `tests/`, one `test_<module>.py` per module (routes are grouped in `test_web_routes.py`, `test_record_routes.py`, and `test_record_live_routes.py`).
- **No GPU, network, or real audio.** `WhisperModel`, pyannote `Pipeline`, and embedding extraction are mocked; `load_wav_as_tensor` returns a fake tensor dict.
- `tests/conftest.py` autouse-patches `pipeline.load_config` with a safe baseline (including `forced_alignment = false`, so no test loads the real aligner on a GPU machine) so a developer's real config can't leak in, and points `WISPER_DATA_DIR` at a fresh temp dir (and clears `WISPER_OUTPUT_DIR`) so no test reads or writes the developer's real campaigns, profiles, config, or database. Each test therefore gets its own `wisper.db`, migrated on first connect. That costs about 1.7 ms, so there is no session-scoped template DB (measured: it would save under 2 s of a 25 s suite). Enrollment tests patch `speaker_manager.load_profiles`.
- **Seeding:** `tests/_seed.py` stores data the way the app does. `seed_profile(s)`, `seed_recording`, and `seed_job` cover profiles, recordings, and jobs. `seed_sidecar` stores diarization data through `write_sidecar()`, so tests use the database path rather than the legacy sidecar fallback. `save_profiles`/`save_campaigns` are whole-store replaces used by tests only. `tests/_legacy_store.py` holds frozen JSON-era writers, used only by importer tests.
- **`test_schema.py`** pins the database itself: one violating statement per `CHECK`/`UNIQUE`/FK/subtype rule against a valid baseline, every cascade and trigger, and that every FK child column is indexed.
- **`test_e2e.py`** runs three flows through `TestClient` and the real job queue, pipeline, store, and search index, with only the ML/LLM/ffmpeg boundaries mocked: upload → transcribe → campaign → summarize → journal → rename → search → delete; a JSON-era data dir imported on first start, then used; and Discord recording → transcribe → delete the transcript → the recording is transcribable again.
- The aligner's tests (`test_word_alignment.py`) use a fake processor/model but the real `split_words_for_alignment()` from transformers, so word-mapping rules are tested against the actual splitter. `scripts/alignment_eval.py`'s pure logic is tested in `test_alignment_eval.py`.
- Real LLM HTTP calls are blocked for the whole suite; clients are tested with mocked httpx and fake SDK modules injected via `sys.modules`.
- Web tests use `TestClient`. Live-recording tests use a `JobQueue` that is never started, and inject jobs directly, so no real worker loads a model.
- Infinite SSE endpoints are tested by pulling one chunk from the `StreamingResponse` body iterator, not over HTTP.
- Recording managers are tested through injected fake sources (`tests/_discord_fakes.py`) and a scripted `capture_factory` + instant `ticker`.
- `test_db.py` covers the migration runner (rollback, `foreign_key_check`, snapshot, cross-process race under spawn), every guard, lease scenarios with `detect_runtime` mocked, and Windows path conversion via `PureWindowsPath`. Thread and process tests use `join(timeout)` so a lock bug fails instead of hanging the suite.
- Security controls have regression tests in `test_path_traversal.py` (null bytes, regex-busting ids, open-redirect/CRLF) and `test_owasp.py` (XSS sanitizer, headers, no stack traces).
- There is no JS test harness; client-only behavior (e.g. ticker de-dupe) is verified manually via `LIVE_AUDIO_TEST_PLAN.md`.

**CI** (`.github/workflows/ci.yml`):
- Python 3.13 and 3.14, both blocking — the versions shipped (Docker `python:3.14-slim`; `requires-python >= 3.13`).
- A `windows-latest` job (3.13) runs the storage tests (`test_db.py`, `test_path_utils.py`, `test_legacy_import.py`, `test_speaker_manager.py`, `test_campaign_manager.py`, `test_transcript_store.py` with a real locked-file `os.replace`, `test_journal.py`, `test_recording_manager.py`, `test_job_history.py`, `test_search_index.py`, `test_schema.py`, `test_e2e.py`) on a real Windows filesystem.
- Weekly cron adds a `latest-deps` job (`pip install --upgrade`, 3.14) to catch upstream breakage early.
- Tailwind staleness check; CodeQL. The Docker CPU image smoke build (`docker.yml`) is currently disabled on GitHub, so image builds are verified manually.
- Dependabot watches `pip`, `docker`, and `github-actions` weekly.

---

## Known Constraints

| Constraint | Detail |
|-----------|--------|
| torchcodec on Windows | Needs FFmpeg's full-shared build; bypassed by scipy pre-loading |
| PyAV pinned `<19` | faster-whisper 1.2.1 decodes audio with `av.open(metadata_errors=…)`, which PyAV 19 removed; a fresh install without the pin fails every CTranslate2 transcription (MLX on Apple Silicon doesn't use it). Drop the pin once faster-whisper releases a fix |
| MPS on Apple Silicon | CTranslate2 has no MPS backend. With `[macos]`, transcription uses MLX; otherwise CPU. Diarization, embeddings, and word alignment use MPS |
| Forced alignment scope | 11 languages (others keep Whisper times); `auto` skips CPU-only machines (~9–20 min per 2.5 h session); no confidence score, so a misplaced word can't be filtered; one timeline can't represent overlapped speech |
| Thread safety | Model globals aren't thread-safe: the web queue runs one job at a time; folder mode uses processes |
| pyannote license | HF token + one-time model license acceptance |
| No web auth | Recording and all other endpoints are unauthenticated; the server assumes localhost or a trusted network |
| Long recording sessions | Sessions run until stopped; disk grows ~1.9 MB/min per track (16 kHz mono 16-bit) |
| Local recording is native-only | `soundcard` needs host audio devices, so it never works in Docker; the Local capture card hides when unavailable |
| Live session holds the queue | `JOB_LIVE` occupies the only worker slot for the session. A job already running when a session starts delays live transcription for the whole session (the Record page warns) |
| Cooperative cancellation | Cancel is checked on tqdm writes; the GPU finishes its current batch |
| Host + Docker Desktop container on one DB | File locks don't cross Docker Desktop's VM, so both writing `wisper.db` at once corrupts it. The runtime lease refuses the second one; use Docker for everything or the native CLI with a local `wisper server`. Container + container is safe; native Linux Docker is unaffected |
| SQLite requirement | SQLite ≥ 3.43 with FTS5 (contentless delete). Shipped Pythons and Docker have it; an old Linux system Python (e.g. Ubuntu 22.04's 3.37) is refused at startup |
| Search highlighting | Matching uses porter stemming; highlighting approximates it by prefix, so a hit can show no highlighted word |
| Markers after reload | Markers aren't replayed into the live ticker on page reload (they're on the detail page) |

---

## HuggingFace Models

Downloaded on first use to `~/.cache/huggingface/hub/`; later runs are offline.

| Model | Purpose | Size |
|-------|---------|------|
| `openai/whisper-*` (via faster-whisper) | Transcription | 75 MB – 1.5 GB |
| `Qwen/Qwen3-ForcedAligner-0.6B-hf` | Forced word alignment (Apache-2.0, ungated) | ~1.7 GB |
| `pyannote/speaker-diarization-community-1` | Diarization pipeline (segmentation + WeSpeaker embedding + VBx clustering bundled); its `embedding/` subfolder also produces profile embeddings | ~32 MB |

License acceptance (free, one-time): [speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1). The model id lives in `config.DIARIZATION_MODEL`.

- **Why community-1 over 3.1:** on multi-hour 5–8-speaker sessions 3.1 produced one catch-all cluster plus several fragments of the same person; community-1 produced one cluster per person at the same runtime.
- **`diarize()` uses `speaker_diarization`, not `exclusive_speaker_diarization`:** solo-segment selection for embeddings and excerpts needs the overlap information the exclusive view removes.
- **Gated-model errors:** `load_pipeline()` and `_load_embedding_model()` turn `GatedRepoError` into a message naming the terms URL, since existing users must accept community-1 separately from 3.1.
