# Wisper-Transcribe: Podcast Transcription with Speaker Diarization

## Context

The user runs tabletop RPG actual-play podcasts (D&D-style) with 5-8 speakers (GM + players). They want to transcribe sessions into markdown transcripts with consistent speaker labeling across files. The transcripts will be fed into a NotebookLM-style system for querying game events and tracking stats.

**Hardware**: NVIDIA RTX 3090 (24GB VRAM) on Windows, Apple M5 Mac. Both platforms must be supported.
**Processing**: All local, no cloud APIs. CLI-driven.

## Technical Stack Decision

**Custom pipeline (faster-whisper + pyannote-audio) over WhisperX.**

- WhisperX wraps both but has chronic dependency pinning issues (torch/pyannote/ctranslate2 conflicts)
- Custom pipeline gives direct control over speaker embedding extraction (critical for cross-file speaker ID)
- WhisperX's word-level alignment (via wav2vec2) is unnecessary for markdown transcripts — segment-level timestamps suffice
- Both faster-whisper and pyannote-audio are actively maintained with cleaner dependency stories

**Key dependency: HuggingFace token** (free) required for pyannote models. One-time setup.

**System requirement: ffmpeg** for audio format conversion.

## Project Structure

```
wisper-transcribe/
├── pyproject.toml
├── README.md
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
```

User data (config + speaker profiles) stored via `platformdirs`:
- Windows: `%APPDATA%/wisper-transcribe/`
- Mac: `~/Library/Application Support/wisper-transcribe/`

```
wisper-transcribe/          # user data dir
├── config.toml
└── profiles/
    ├── speakers.json       # name -> metadata mapping
    └── embeddings/         # .npy voice fingerprint files
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

## Speaker Labeling: Full Lifecycle

### Where state lives

All speaker state persists in the user data directory (survives across runs, projects, and sessions):

```
%APPDATA%/wisper-transcribe/profiles/
├── speakers.json           # Central registry
└── embeddings/
    ├── alice.npy           # 512-dim voice fingerprint
    ├── bob.npy
    └── charlie.npy
```

**speakers.json** example:
```json
{
  "alice": {
    "display_name": "Alice",
    "role": "DM",
    "embedding_file": "embeddings/alice.npy",
    "enrolled_date": "2026-04-05",
    "enrollment_source": "session01.mp3",
    "notes": "Game Master, distinctive low voice"
  },
  "bob": {
    "display_name": "Bob",
    "role": "Player",
    "embedding_file": "embeddings/bob.npy",
    "enrolled_date": "2026-04-05",
    "enrollment_source": "session01.mp3",
    "notes": "Plays Theron the fighter"
  }
}
```

### Run 1: First file, no profiles exist yet

```
$ wisper transcribe session01.mp3 --enroll-speakers --num-speakers 6
```

1. Transcription + diarization runs → produces anonymous labels (SPEAKER_00 through SPEAKER_05)
2. For each detected speaker, the system shows a sample transcript line and timestamp:
   ```
   Speaker 1 of 6 (heard at 00:00:12 - 00:00:45):
     "Welcome back everyone. Last session you had just entered the ruins..."
   Who is this? > Alice
   Role (DM/Player/Guest, optional)? > DM
   Notes (optional)? > Game Master
   ```
3. System extracts a voice embedding for that speaker from their longest segments
4. Saves embedding as `alice.npy`, adds entry to `speakers.json`
5. Repeats for all 6 speakers
6. Writes the final .md transcript with real names

**Result**: 6 speaker profiles now exist. All future files will match against them automatically.

### Run 2+: Subsequent files, profiles exist

```
$ wisper transcribe session02.mp3 --num-speakers 6
```

1. Transcription + diarization runs → anonymous labels SPEAKER_00 through SPEAKER_05
2. **Automatic matching**:
   - For each anonymous speaker, extract voice embedding from their segments
   - Compare each embedding against all enrolled profiles using cosine similarity
   - Use greedy best-match assignment (highest similarity first) with a minimum threshold (default 0.65)
   - Example match results:
     ```
     SPEAKER_00 → Alice  (similarity: 0.91)
     SPEAKER_01 → Bob    (similarity: 0.84)
     SPEAKER_02 → Charlie (similarity: 0.78)
     SPEAKER_03 → Diana  (similarity: 0.72)
     SPEAKER_04 → Unknown Speaker 1 (best match: 0.43, below threshold)
     SPEAKER_05 → Eve    (similarity: 0.80)
     ```
3. Writes .md transcript with matched names
4. Unknown speakers get labeled "Unknown Speaker N"
5. Console reports the matching results so user can verify

### Handling edge cases

**New player joins (not enrolled):**
- They appear as "Unknown Speaker N" in the output
- User fixes and enrolls them:
  ```
  $ wisper fix session05.md --speaker "Unknown Speaker 1" --name "Frank" --re-enroll
  ```
  This updates the transcript AND saves Frank's voice embedding for future matching.

**Player absent from a session:**
- No problem — their profile simply won't match any speaker in that file. Unused profiles are ignored.

**Speaker sounds different (sick, new mic, remote vs in-person):**
- The 0.65 threshold accommodates moderate variation
- If matching fails, user can re-enroll to update the embedding:
  ```
  $ wisper enroll --name "Alice" --audio session08.mp3 --segment "0:30-1:15" --update
  ```
  The `--update` flag averages the new embedding with the existing one (exponential moving average), making the profile more robust over time rather than replacing it.

**Two speakers sound similar:**
- The greedy assignment ensures each enrolled profile maps to at most one anonymous speaker
- If truly ambiguous, the lower-confidence match may fall below threshold → "Unknown Speaker N"
- User corrects with `wisper fix`

**Wrong match (system assigns wrong name):**
- `wisper fix session03.md --speaker "Alice" --name "Diana"` swaps the labels in the transcript
- Add `--re-enroll` to also update the embeddings if the profiles need correction

### Enrollment options summary

| Method | When to use |
|--------|-------------|
| `wisper transcribe --enroll-speakers` | First run, name everyone interactively |
| `wisper enroll --name X --audio file` | Add a single speaker from a clean reference clip |
| `wisper fix --re-enroll` | Fix a wrong match and save the correct embedding |
| `wisper enroll --name X --audio file --update` | Improve an existing profile with additional voice samples |

## User Interaction: CLI-First (GUI Future)

The MVP is entirely CLI-driven. All modules are designed as importable libraries with clean function signatures — the CLI is a thin layer on top. This means a GUI (Phase 6) can call the same functions without refactoring.

### Typical workflow (what you'll actually type)

**One-time setup:**
```
$ pip install -e .                          # install the tool
$ wisper config set hf_token hf_abc123...   # set HuggingFace token
```

**First session (enroll your players):**
```
$ wisper transcribe session01.mp3 --enroll-speakers --num-speakers 6
  Transcribing session01.mp3... [=========>       ] 45%
  Diarizing speakers... done (6 speakers found)

  Speaker 1 of 6 (heard at 00:00:12):
    "Welcome back everyone..."
  Who is this? > Alice
  Role? > DM

  Speaker 2 of 6 (heard at 00:00:18):
    "Right, I want to check for traps..."
  Who is this? > Bob
  Role? > Player
  ...

  ✓ Enrolled 6 speakers
  ✓ Wrote session01.md
```

**All future sessions (fully automatic):**
```
$ wisper transcribe session02.mp3 --num-speakers 6
  Transcribing... done
  Diarizing... done
  Matching speakers:
    SPEAKER_00 → Alice  (0.91)
    SPEAKER_01 → Bob    (0.84)
    SPEAKER_02 → Charlie (0.78)
    ...
  ✓ Wrote session02.md

# Or process an entire folder at once:
$ wisper transcribe ./recordings/ --num-speakers 6
  Processing 12 files...
  [1/12] session01.mp3 → session01.md (skipped, already exists)
  [2/12] session02.mp3 → session02.md ✓
  ...
  Done. 11 transcribed, 1 skipped, 0 errors.
```

**Quick fixes:**
```
$ wisper speakers list                     # see enrolled profiles
$ wisper fix session05.md --speaker "Unknown Speaker 1" --name "Frank" --re-enroll
```

### Future GUI considerations (Phase 6, not in MVP)

The clean separation between `cli.py` (thin command layer) and `pipeline.py` / `speaker_manager.py` (logic) means a GUI can call the same functions. Likely approach:
- **Textual** (terminal UI) for a rich CLI experience, or
- **tkinter** / **PyQt** for a desktop window with file picker, speaker management panel, and progress display
- The GUI would wrap the same `pipeline.process_file()` and `speaker_manager.enroll()` calls

## CLI Commands

```
wisper transcribe <path>        # file or folder
  -o, --output DIR              # output directory (default: same as input)
  -m, --model SIZE              # tiny/base/small/medium/large-v3 (default: medium)
  -l, --language LANG           # language code (default: auto)
  -n, --num-speakers INT        # expected speaker count
  --enroll-speakers             # interactive naming mode
  --no-diarize                  # skip diarization
  --timestamps / --no-timestamps
  --device cpu|cuda|auto
  --overwrite

wisper enroll <name> --audio <file>
  --segment START-END           # use specific time range
  --notes TEXT                  # e.g., "DM", "plays the fighter"

wisper speakers list|remove|rename|test

wisper config show|set|path

wisper fix <transcript.md>
  --speaker NAME --name NEW_NAME [--re-enroll]
```

## Output Format

```markdown
---
title: "Session 01 - The Dragon's Keep"
source_file: session01.mp3
date_processed: 2026-04-05
duration: "1:23:45"
speakers:
  - name: Alice
    role: DM
  - name: Bob
    role: Player
  - name: Charlie
    role: Player
---

# Session 01 - The Dragon's Keep

**Alice** *(00:00:12)*: Welcome back everyone. Last session you had just entered
the ruins of Khar'zul.

**Bob** *(00:00:18)*: Right, I want to check for traps before we go further in.

**Alice** *(00:00:23)*: Go ahead and roll a perception check.

**Bob** *(00:00:26)*: That's a seventeen.

**Alice** *(00:00:28)*: You notice a thin tripwire stretched across the corridor
about ankle height.

**Charlie** *(00:00:35)*: Can I see what it's connected to? I have darkvision.

---
*Transcribed by wisper-transcribe v0.1.0*
```

YAML frontmatter includes structured speaker metadata (name + role like DM/Player) — useful for downstream NotebookLM ingestion and stat tracking. Bold speaker names, italic timestamps. Consecutive same-speaker lines merged.

## Dependencies

```toml
requires-python = ">=3.10"
dependencies = [
    "faster-whisper>=1.1.0",
    "pyannote-audio>=3.3",
    "torch>=2.1",
    "torchaudio>=2.1",
    "click>=8.1",
    "platformdirs>=4.0",
    "numpy>=1.24",
    "scipy>=1.10",
    "tqdm>=4.65",
    "pydub>=0.25",
    "pyyaml>=6.0",
]
```

System requirement: `ffmpeg` (brew/choco/winget/apt).

## Cross-Platform Notes

- Always use `pathlib.Path` for all file ops
- `platformdirs` for config/data directory resolution
- **Windows (3090)**: CUDA auto-detect, use `large-v3` model (24GB VRAM handles it easily). Default device=cuda.
- **Mac (M5)**: CPU-only for pyannote (MPS unreliable for these models). faster-whisper works on CPU. Default device=cpu. Consider `medium` model on Mac for speed.
- ffmpeg check on startup with platform-specific install instructions
- 5-8 speakers: always recommend `--num-speakers` flag for better diarization accuracy with larger groups

## Implementation Phases (Build Order)

Build all phases sequentially. Each phase should be fully working before moving to the next.

### Phase 1: Project Skeleton + Basic Transcription

**Create these files:**

1. `pyproject.toml` — Python project with `[project.scripts] wisper = "wisper_transcribe.cli:main"`. Include all dependencies listed in Dependencies section. Use `src` layout.
2. `src/wisper_transcribe/__init__.py` — version string only
3. `src/wisper_transcribe/__main__.py` — `from .cli import main; main()`
4. `src/wisper_transcribe/models.py` — dataclasses:
   - `TranscriptionSegment(start: float, end: float, text: str)`
   - `DiarizationSegment(start: float, end: float, speaker: str)`
   - `AlignedSegment(start: float, end: float, speaker: str, text: str)`
   - `SpeakerProfile(name: str, display_name: str, role: str, embedding_path: Path, enrolled_date: str, enrollment_source: str, notes: str)`
5. `src/wisper_transcribe/config.py`:
   - Use `platformdirs.user_data_dir("wisper-transcribe")` for data path
   - Load/save `config.toml` with defaults: model="medium", language="en", device="auto", timestamps=True, similarity_threshold=0.65, min_speakers=2, max_speakers=8
   - `check_ffmpeg()` — run `ffmpeg -version`, raise clear error with install instructions if missing
   - `get_device()` — return "cuda" if `torch.cuda.is_available()`, else "cpu"
6. `src/wisper_transcribe/audio_utils.py`:
   - `validate_audio(path)` — check file exists, extension in {.wav, .mp3, .m4a, .flac, .ogg, .mp4}
   - `convert_to_wav(path) -> Path` — use pydub to convert to 16kHz mono WAV in temp dir. Return original path if already WAV 16kHz mono.
   - `get_duration(path) -> float` — return duration in seconds
7. `src/wisper_transcribe/transcriber.py`:
   - Module-level `_model = None` for lazy loading
   - `load_model(model_size, device)` — load faster-whisper WhisperModel, cache in `_model`
   - `transcribe(audio_path, model_size="medium", device="auto", language="en") -> list[TranscriptionSegment]`
8. `src/wisper_transcribe/formatter.py`:
   - `to_markdown(segments, speaker_map, metadata) -> str` — produce the markdown format shown in Output Format section
   - `metadata` is a dict with keys: title, source_file, date_processed, duration, speakers
   - If `speaker_map` is None, omit speaker names (single-speaker mode)
   - Merge consecutive segments from same speaker into one block
9. `src/wisper_transcribe/pipeline.py`:
   - `process_file(path, output_dir, model_size, device, language, ...) -> Path` — run full pipeline, return output .md path
   - Phase 1: transcribe only, no diarization
10. `src/wisper_transcribe/cli.py`:
    - Use Click. Command group `wisper`.
    - `wisper transcribe <path>` with flags: `-o`, `-m`, `-l`, `--device`, `--overwrite`, `--timestamps/--no-timestamps`
    - Phase 1: single file only, no diarization flags yet

**Verify:** `pip install -e .` then `wisper transcribe somefile.mp3` produces a `.md` file with timestamped text (no speaker labels yet).

### Phase 2: Speaker Diarization

**Add/modify these files:**

1. `src/wisper_transcribe/diarizer.py`:
   - Module-level `_pipeline = None` for lazy loading
   - `load_pipeline(hf_token, device)` — load `pyannote/speaker-diarization-3.1` from HuggingFace
   - `diarize(audio_path, hf_token, device, num_speakers=None, min_speakers=None, max_speakers=None) -> list[DiarizationSegment]`
2. `src/wisper_transcribe/aligner.py`:
   - `align(transcription: list[TranscriptionSegment], diarization: list[DiarizationSegment]) -> list[AlignedSegment]`
   - For each transcription segment, find the diarization segment with maximum time overlap. Assign that speaker.
   - Handle edge case: if no diarization segment overlaps, assign "UNKNOWN"
3. **Update `config.py`**: Add HuggingFace token to config. On first run, if no token found, prompt user to enter it and save to config.toml. Also check `HUGGINGFACE_TOKEN` env var.
4. **Update `pipeline.py`**: After transcription, run diarization and alignment. Pass speaker labels to formatter.
5. **Update `formatter.py`**: Use speaker labels (SPEAKER_00 etc.) when available.
6. **Update `cli.py`**: Add `--num-speakers`, `--min-speakers`, `--max-speakers`, `--no-diarize` flags.

**Verify:** `wisper transcribe multi_speaker.mp3 --num-speakers 4` produces `.md` with `**SPEAKER_00**`, `**SPEAKER_01**` etc.

### Phase 3: Speaker Profiles & Cross-File Identification

**Add/modify these files:**

1. `src/wisper_transcribe/speaker_manager.py`:
   - `load_profiles(data_dir) -> dict[str, SpeakerProfile]` — read speakers.json
   - `save_profiles(data_dir, profiles)` — write speakers.json
   - `extract_embedding(audio_path, segments: list[DiarizationSegment], speaker_label: str, device) -> np.ndarray` — use `pyannote/embedding` model (wespeaker-voxceleb-resnet34-LM) to extract 512-dim embedding. Average embeddings from the 3-5 longest segments for that speaker.
   - `enroll_speaker(name, display_name, role, audio_path, segments, speaker_label, device, data_dir, notes="")` — extract embedding, save .npy, update speakers.json
   - `match_speakers(audio_path, diarization_segments, data_dir, device, threshold=0.65) -> dict[str, str]` — for each unique speaker label in diarization, extract embedding, compare via cosine similarity against all enrolled profiles, return mapping like `{"SPEAKER_00": "Alice", "SPEAKER_01": "Unknown Speaker 1"}`
   - `update_embedding(name, new_embedding, data_dir, alpha=0.3)` — exponential moving average: `new = alpha * new_sample + (1-alpha) * existing`
2. **Update `pipeline.py`**: After alignment, call `match_speakers()` if profiles exist. Pass name mapping to formatter.
3. **Update `cli.py`**:
   - Add `--enroll-speakers` flag to `transcribe` command. When set, after diarization, interactively prompt for each speaker's name/role, call `enroll_speaker()`.
   - Add `wisper enroll <name> --audio <file>` command with `--segment`, `--notes`, `--update` flags
   - Add `wisper speakers` command group: `list`, `remove <name>`, `rename <old> <new>`, `test <audio>`
   - Add `wisper fix <transcript.md> --speaker NAME --name NEW_NAME` with optional `--re-enroll`

**Verify:** Enroll speakers from session01, then transcribe session02 — speakers should be auto-named.

### Phase 4: Batch Processing & Polish

**Modify these files:**

1. **Update `pipeline.py`**:
   - `process_folder(path, output_dir, ...) -> list[Path]` — iterate audio files, call `process_file()` for each
   - Skip files that already have `.md` output (unless `--overwrite`)
   - Wrap each file in try/except — log error, continue to next file
   - Report summary at end: N transcribed, N skipped, N errors
2. **Update `cli.py`**:
   - `wisper transcribe` should detect if path is a file or directory and call accordingly
   - Add `tqdm` progress bars
   - Add `wisper config show|set|path` commands
   - Add `--verbose` flag
3. **Update `formatter.py`**: The `wisper fix` command needs to parse an existing .md, update speaker names, and rewrite it.

**Verify:** `wisper transcribe ./recordings/` processes all files, skips existing, reports summary.

### Phase 5: Tests & README
- Unit tests in `tests/` for each module (mock the ML models — don't require GPU in tests)
- `README.md` with install instructions, quick start, and full CLI reference

### Phase 6 (Future, NOT in MVP): Optional GUI
- Terminal UI via Textual or desktop UI via tkinter/PyQt
- Wraps the same `pipeline.process_file()` and `speaker_manager` calls
- Not to be built now — just keep the library/CLI separation clean

## Verification Checklist

- [ ] `pip install -e .` succeeds
- [ ] `wisper transcribe single_speaker.mp3` → readable .md without speaker labels
- [ ] `wisper transcribe multi_speaker.mp3 --num-speakers 4` → .md with SPEAKER_XX labels
- [ ] `wisper transcribe session01.mp3 --enroll-speakers --num-speakers 6` → interactive enrollment + .md with real names
- [ ] `wisper transcribe session02.mp3 --num-speakers 6` → automatic speaker matching from profiles
- [ ] `wisper speakers list` → shows enrolled profiles
- [ ] `wisper fix session.md --speaker "Unknown Speaker 1" --name "Frank" --re-enroll` → updates transcript + creates profile
- [ ] `wisper transcribe ./recordings/` → batch processing with progress, skip existing, error recovery


## Manual Testing Notes
- Add a progress bar per file and per group of files. 
- Clarify if the model downloads from Hugging face are one time. 
- Clarify how to check which models are here. 
- Clarify which model is running by default. 
- Add logic to detect if there is a CUDA device and to select the optimal model for the hardware. 
- (.venv) PS C:\vscode\wisper-transcribe> wisper transcribe '.\example-file\Episode 1 – Introducing Tom Exposition.mp3'     --enroll-speakers --device cuda
    Transcribing Episode 1 – Introducing Tom Exposition.mp3...
    Error: Library cublas64_12.dll is not found or cannot be loaded
    - May be fixed now by including 
      - "nvidia-cublas-cu12; sys_platform == 'win32'",
      - "nvidia-cudnn-cu12; sys_platform == 'win32'",
- Parallel processing for folders? 
- expose default save directory in wisper config
- expose default save directory for speaker profiles in config 

- May be add dependency checking or a "setup.sh" style script to initilitze everything. 

-
(.venv) PS C:\vscode\wisper-transcribe> wisper transcribe '.\example-file\Episode 1 – Introducing Tom Exposition.mp3' --enroll-speakers
  Transcribing Episode 1 – Introducing Tom Exposition.mp3...
C:\vscode\wisper-transcribe\.venv\Lib\site-packages\pyannote\audio\core\io.py:47: UserWarning:
torchcodec is not installed correctly so built-in audio decoding will fail. Solutions are:
* use audio preloaded in-memory as a {'waveform': (channel, time) torch.Tensor, 'sample_rate': int} dictionary;
* fix torchcodec installation. Error message was:

Could not load libtorchcodec. Likely causes:
          1. FFmpeg is not properly installed in your environment. We support
             versions 4, 5, 6, 7, and 8, and we attempt to load libtorchcodec
             for each of those versions. Errors for versions not installed on
             your system are expected; only the error for your installed FFmpeg
             version is relevant. On Windows, ensure you've installed the
             "full-shared" version which ships DLLs.
          2. The PyTorch version (2.11.0+cpu) is not compatible with
             this version of TorchCodec. Refer to the version compatibility
             table:
             https://github.com/pytorch/torchcodec?tab=readme-ov-file#installing-torchcodec.
          3. Another runtime dependency; see exceptions below.

        The following exceptions were raised as we tried to load libtorchcodec:

[start of libtorchcodec loading traceback]
FFmpeg version 8:
Traceback (most recent call last):
  File "C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torch\_ops.py", line 1503, in load_library
    ctypes.CDLL(path)
  File "C:\Users\brand\AppData\Local\Programs\Python\Python312\Lib\ctypes\__init__.py", line 379, in __init__
    self._handle = _dlopen(self._name, mode)
                   ^^^^^^^^^^^^^^^^^^^^^^^^^
FileNotFoundError: Could not find module 'C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torchcodec\libtorchcodec_core8.dll' (or one of its dependencies). Try using the full path with constructor syntax.

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torchcodec\_internally_replaced_utils.py", line 93, in load_torchcodec_shared_libraries
    torch.ops.load_library(core_library_path)
  File "C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torch\_ops.py", line 1505, in load_library
    raise OSError(f"Could not load this library: {path}") from e
OSError: Could not load this library: C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torchcodec\libtorchcodec_core8.dll

FFmpeg version 7:
Traceback (most recent call last):
  File "C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torch\_ops.py", line 1503, in load_library
    ctypes.CDLL(path)
  File "C:\Users\brand\AppData\Local\Programs\Python\Python312\Lib\ctypes\__init__.py", line 379, in __init__
    self._handle = _dlopen(self._name, mode)
                   ^^^^^^^^^^^^^^^^^^^^^^^^^
FileNotFoundError: Could not find module 'C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torchcodec\libtorchcodec_core7.dll' (or one of its dependencies). Try using the full path with constructor syntax.

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torchcodec\_internally_replaced_utils.py", line 93, in load_torchcodec_shared_libraries
    torch.ops.load_library(core_library_path)
  File "C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torch\_ops.py", line 1505, in load_library
    raise OSError(f"Could not load this library: {path}") from e
OSError: Could not load this library: C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torchcodec\libtorchcodec_core7.dll

FFmpeg version 6:
Traceback (most recent call last):
  File "C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torch\_ops.py", line 1503, in load_library
    ctypes.CDLL(path)
  File "C:\Users\brand\AppData\Local\Programs\Python\Python312\Lib\ctypes\__init__.py", line 379, in __init__
    self._handle = _dlopen(self._name, mode)
                   ^^^^^^^^^^^^^^^^^^^^^^^^^
FileNotFoundError: Could not find module 'C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torchcodec\libtorchcodec_core6.dll' (or one of its dependencies). Try using the full path with constructor syntax.

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torchcodec\_internally_replaced_utils.py", line 93, in load_torchcodec_shared_libraries
    torch.ops.load_library(core_library_path)
  File "C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torch\_ops.py", line 1505, in load_library
    raise OSError(f"Could not load this library: {path}") from e
OSError: Could not load this library: C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torchcodec\libtorchcodec_core6.dll

FFmpeg version 5:
Traceback (most recent call last):
  File "C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torch\_ops.py", line 1503, in load_library
    ctypes.CDLL(path)
  File "C:\Users\brand\AppData\Local\Programs\Python\Python312\Lib\ctypes\__init__.py", line 379, in __init__
    self._handle = _dlopen(self._name, mode)
                   ^^^^^^^^^^^^^^^^^^^^^^^^^
FileNotFoundError: Could not find module 'C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torchcodec\libtorchcodec_core5.dll' (or one of its dependencies). Try using the full path with constructor syntax.

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torchcodec\_internally_replaced_utils.py", line 93, in load_torchcodec_shared_libraries
    torch.ops.load_library(core_library_path)
  File "C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torch\_ops.py", line 1505, in load_library
    raise OSError(f"Could not load this library: {path}") from e
OSError: Could not load this library: C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torchcodec\libtorchcodec_core5.dll

FFmpeg version 4:
Traceback (most recent call last):
  File "C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torch\_ops.py", line 1503, in load_library
    ctypes.CDLL(path)
  File "C:\Users\brand\AppData\Local\Programs\Python\Python312\Lib\ctypes\__init__.py", line 379, in __init__
    self._handle = _dlopen(self._name, mode)
                   ^^^^^^^^^^^^^^^^^^^^^^^^^
FileNotFoundError: Could not find module 'C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torchcodec\libtorchcodec_core4.dll' (or one of its dependencies). Try using the full path with constructor syntax.

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torchcodec\_internally_replaced_utils.py", line 93, in load_torchcodec_shared_libraries
    torch.ops.load_library(core_library_path)
  File "C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torch\_ops.py", line 1505, in load_library
    raise OSError(f"Could not load this library: {path}") from e
OSError: Could not load this library: C:\vscode\wisper-transcribe\.venv\Lib\site-packages\torchcodec\libtorchcodec_core4.dll
[end of libtorchcodec loading traceback].
  warnings.warn(
  Diarizing speakers...
Error: Pipeline.from_pretrained() got an unexpected keyword argument 'use_auth_token'