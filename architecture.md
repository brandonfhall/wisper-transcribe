# wisper-transcribe — Architecture Reference

> **Keep this file current.** Update it whenever a new module is added, a key design decision changes, or the processing pipeline is modified.

---

## Tech Stack

| Component | Library | Purpose |
|-----------|---------|---------|
| Transcription | `faster-whisper` (CTranslate2) | 4× faster than openai/whisper, lower VRAM, lazy model caching |
| Diarization | `pyannote-audio 4.x` | Speaker segmentation + voice embeddings |
| Audio loading (diarizer) | `scipy.io.wavfile` | Bypasses `torchcodec` (see [Known Constraints](#known-constraints)) |
| Audio conversion | `pydub` + ffmpeg | Convert any format → 16kHz mono WAV |
| CLI | `click` | Command groups: `transcribe`, `enroll`, `speakers`, `config`, `fix` |
| Config/storage | `platformdirs` + TOML | OS-native user data dirs, never hardcoded paths |
| Progress display | `tqdm` | Nested bars: folder-level (position=0), transcription (position=1) |
| GPU detection | `torch.cuda.is_available()` | Auto-selects CUDA or CPU at runtime |

---

## Module Map

```
src/wisper_transcribe/
├── cli.py              Click entry points — no business logic, delegates to pipeline/manager
├── pipeline.py         Main orchestrator: process_file(), process_folder()
├── transcriber.py      faster-whisper wrapper, lazy model cache (_model), CUDA DLL path fix
├── diarizer.py         pyannote pipeline wrapper, lazy cache (_pipeline), scipy audio loading
├── aligner.py          Merge transcription segments with diarization labels (max-overlap)
├── speaker_manager.py  Profile CRUD, embedding extraction, cosine-similarity matching, EMA updates
├── formatter.py        Markdown output, YAML frontmatter, timestamp formatting
├── audio_utils.py      validate_audio(), convert_to_wav(), get_duration()
├── config.py           load_config(), save_config(), get_device(), get_hf_token(), check_ffmpeg()
└── models.py           Dataclasses: TranscriptionSegment, DiarizationSegment, AlignedSegment, SpeakerProfile
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
   • faster-whisper model (lazy-loaded, cached)       • pyannote Pipeline (lazy-loaded, cached)
   • Returns List[TranscriptionSegment]               • Audio loaded via scipy.io.wavfile
   • Each segment: start, end, text                   • Passed as tensor dict to pipeline
   • tqdm progress bar on transcription time          • Returns List[DiarizationSegment]
                                                        • Each segment: start, end, speaker label
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
2. `speaker_manager.extract_embedding()` slices the WAV to that speaker's segments and runs pyannote's embedding model
3. 512-dim numpy vector saved to `profiles/embeddings/<name>.npy`
4. Metadata (display name, role, notes, date) saved to `profiles/speakers.json`

### Matching flow (subsequent runs)
1. Extract embedding for each detected speaker label
2. Build cosine similarity matrix: `(n_detected × n_enrolled)`
3. Greedy assignment: highest-similarity pair first, each profile assigned at most once
4. Similarity below threshold (default 0.65) → label kept as `"Unknown Speaker N"`

### EMA updates (`wisper enroll --update`)
New embedding blended with existing: `stored = 0.7 * stored + 0.3 * new`

---

## Key Design Decisions

### pyannote-audio pinned to 3.x, torchaudio compatibility shims
`pyannote-audio` is pinned to `>=3.3,<4.0`. pyannote 4.x depends on `torchcodec` for audio I/O, which requires FFmpeg's "full-shared" build on Windows (not provided by `winget install Gyan.FFmpeg`).

pyannote 3.x uses `soundfile` for audio loading, but its `core/io.py` references two torchaudio API symbols that were removed in torchaudio 2.x: `torchaudio.AudioMetaData` (namedtuple) and `torchaudio.list_audio_backends()` (function). Both are patched back in at the top of `diarizer.py` before `from pyannote.audio import Pipeline` so the import succeeds.

At runtime, `diarize()` pre-loads the WAV file with `scipy.io.wavfile` and passes a `{'waveform': tensor, 'sample_rate': int}` dict to the pipeline. When the dict contains `"waveform"`, pyannote's `Audio.__call__()` and `Audio.crop()` both operate directly on the tensor without ever calling `torchaudio.load()`, so the missing torchaudio audio I/O is never exercised.

### Module-level imports for mock patching
`pyannote.audio.Pipeline` is imported at the top of `diarizer.py` (not inside the function). `pydub.AudioSegment` is imported at the top of `audio_utils.py`. This is required so `unittest.mock.patch("wisper_transcribe.diarizer.Pipeline", ...)` resolves correctly in tests. Lazy imports inside functions cannot be patched at the module path.

### CUDA DLL path resolution (Windows)
`transcriber.load_model()` searches for `cublas64_12.dll` in PyTorch's `nvidia-cublas` site-packages directory and the system CUDA Toolkit before loading `WhisperModel`. CTranslate2 on Windows requires this DLL to be on `PATH` or added via `os.add_dll_directory()`.

### Module-level model caches
`_model` (transcriber) and `_pipeline` (diarizer) are module-level globals. This avoids reloading multi-GB models between files when processing a folder. The caches are intentionally reset to `None` in tests.

### pyproject.toml torch version
`torch>=2.8.0` is required because `pyannote-audio 4.x` declares this minimum. The CUDA build must be installed from `https://download.pytorch.org/whl/cu126` — PyPI only ships the CPU-only build. The `setup.ps1` script handles this automatically on Windows.

---

## Data Storage

All user data lives **outside the repo** in the OS-native user data directory:

| Platform | Path |
|----------|------|
| Windows  | `%APPDATA%\wisper-transcribe\` |
| Mac      | `~/Library/Application Support/wisper-transcribe/` |

```
wisper-transcribe/       ← platformdirs.user_data_dir("wisper-transcribe")
├── config.toml
└── profiles/
    ├── speakers.json    name → SpeakerProfile metadata
    └── embeddings/
        └── <name>.npy   512-dim float32 voice embeddings (gitignored)
```

Config keys: `model`, `language`, `device`, `timestamps`, `similarity_threshold`, `min_speakers`, `max_speakers`, `hf_token`.

---

## Test Strategy

- All tests in `tests/`, mirroring `src/wisper_transcribe/`
- **No GPU, no network, no real audio required.** All ML calls (WhisperModel, pyannote Pipeline, embedding extraction) are mocked with `unittest.mock.MagicMock`
- `scipy.io.wavfile.read` patched in diarizer tests to return a fake `(16000, np.zeros(...))` tuple
- `tqdm.write` used throughout production code so test output is not polluted by progress bars
- Coverage: run `pytest tests/ -v --cov --cov-report=term-missing`

---

## Known Constraints

| Constraint | Detail |
|-----------|--------|
| torchcodec on Windows | Requires FFmpeg "full-shared" build; avoided by scipy audio loading in diarizer |
| MPS on Apple Silicon | faster-whisper (CTranslate2) has no MPS backend — transcription always uses CPU on Mac. pyannote diarization and speaker embeddings run on MPS when available (auto-detected). |
| Thread safety | `_model` and `_pipeline` globals are not thread-safe; parallel folder processing would require per-worker instances |
| pyannote license | HuggingFace token + one-time model license acceptance required (free) |

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
