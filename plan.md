# wisper-transcribe — Open Items

Active plans, open bugs, and parked designs. Shipped work is removed; its design lives in `architecture.md` and its history in git.

---

## Open bugs

### Missing transcript file after a successful Transcribe job

A local-recording transcription once reported COMPLETED ("Wrote `<id>.md`") but the file never existed; root cause unknown. **Detection is in place:** the job fails with "Transcript file missing after write" and logs the transcripts folder, and job history keeps that log across restarts. **Next step:** if it recurs, check `/jobs/history` for the job's log and output root; run with `WISPER_DEBUG=1` to capture more (`LIVE_AUDIO_TEST_PLAN.md` §4a). The recording hand-off no longer copies `combined.wav` into the output dir (it reads it in place), so the job's input is `recordings/<id>/combined.wav` and the output is named by `output_stem`.

### Docker Desktop + native CLI on one data dir can corrupt the DB

With the web server in Docker Desktop (Mac or Windows) and `./data` bind-mounted, a native `wisper` command pointed at the same `./data` (via `WISPER_DATA_DIR`) and writing at the same time can corrupt `wisper.db`: file locks don't cross the Docker Desktop VM boundary. Reproduced 2026-09-30 (one host writer plus one container writer gave `database disk image is malformed` and lost updates). Container + container is fine (15,000/15,000 writes), and native Linux Docker is unaffected.

**Status:** guarded, not fixed. The runtime lease (`db.py`, `runtime_leases`) makes the second runtime refuse to start when the container is inside Docker Desktop's VM; it is advisory (two processes starting in the same second could both pass). Documented in `docs/docker.md` ("One way of running at a time"). A real fix would need a lock that crosses the VM (e.g. routing host CLI writes through the container's server API).

### Ollama: empty responses from reasoning models

`ollama.py`'s `_post_chat` reads only `message.content` from the stream. A reasoning model can spend its whole budget on the `thinking` field and return no content, and a streamed `"error"` field is never checked. Both cases surface as `Ollama JSON response did not parse: ... Raw: ''`, which blames the parse layer. Seen intermittently when summarizing a ~150k-char transcript with `ollama-cloud`; a plain retry succeeded. A non-thinking local model instead ignored the `format` schema and returned prose.

**Decision needed:** add retry-on-empty-content and/or surface a streamed error distinctly. Either change touches the shared client code used by every provider.

---

## Manual verification owed

- **Live recording + campaign journal:** `LIVE_AUDIO_TEST_PLAN.md` — real-device capture, live transcript, journal browser flows, bulk delete, busy-queue notice.
- **Live Discord acceptance test.** The recording pipeline (WAV segments, `__mixed__` combined track, `combined_path` hand-off) is covered by synthesized-PCM tests, but the JDA → socket → Python path needs one real session: record a few minutes with 2+ speakers, play the per-user WAVs before binding any speaker (a bound user's track is deleted when the session ends or on binding), and run Transcribe.
- **Windows launcher dependency refresh.** `start.bat` reinstalls dependencies when `pyproject.toml` is newer than `.venv\.wisper-deps`. The one-time reinstall after a `git pull` is confirmed on Windows (2026-10-03); still owed: a second launch skips it.
- **GPU Docker image on an NVIDIA host.** The "Docker Build" workflow is disabled on GitHub, and the GPU image can only be built (not run) without NVIDIA hardware. Confirm a diarized job on the GPU image downloads the alignment model into `./cache/` and logs "Aligned words".
- **SQLite storage in a browser and on real capture** (automated coverage: `test_e2e.py`, `test_schema.py`). With `WISPER_DATA_DIR` pointing at a copy of real data:
  - Journal: the stale-journal notice (move a folded session to another campaign) on the Campaign and Journal pages; **Rebuild journal** vs **Rebuild from transcripts** confirmations and their call counts; journal **Download** includes `journaled_sessions`.
  - Job history page, filters, paging, and a historical job's page after a restart.
  - Recording end to end: record a few minutes, local and Discord. Add markers, including two in quick succession, and edit the notes mid-session, then stop. After stop, only `combined.wav` and (local) `live_transcript.md` remain; Discord also keeps unbound users' `per-user/<uid>/`. Transcribe it, play it back, and jump to a marker.
  - Search: edit a transcript in Obsidian while the server runs and search for the new words (the result shows "changed — reindexing", then matches after a reload).
- **macOS loopback.** Record page on a Mac with BlackHole installed: BlackHole appears under System Audio and captures audio.

---

## Dependency pins

- **Drop `av<19`** (pyproject) once a faster-whisper release stops passing `metadata_errors=` to `av.open()`; check with a CPU Docker build and one transcription.

---

## Storage — open

Storage trim shipped on `feat/storage-trim`: file registry (v9/v10), rename-following, Needs attention, FLAC-only upload audio, recording trim, `wisper storage trim`, playback, and Re-transcribe. Its design is in `architecture.md`.

- **Windows rehearsal before the next `start.bat` launch:** run the merged build on the Windows PC against a *copy* of the Windows data dir and output folder (`WISPER_DATA_DIR`, `WISPER_OUTPUT_DIR`, `TEMP`/`TMP` pointed at scratch). Confirm v8 → v10 (`wisper db status`: integrity ok, no drift). Run `wisper storage trim`, then `--apply`; a re-run finds nothing. Expect the Hanataz `.mp4`s to become `<stem>.flac` after their voices are backfilled, the orphan `<recording-id>.wav` hand-off copies to be deleted, and the recordings to be trimmed. Then delete the copy.
- **Windows-only paths are tested only in CI:** `db.ServerLock`'s `msvcrt` lock, and the `PermissionError`/`_replace` retry when a file is open in Obsidian, Explorer, or a player. Watch the first real use.
- **Already-compressed audio grows on conversion:** `wisper storage trim` turns an audio-only MP3/M4A/Opus into a 16 kHz mono FLAC, about 3× a 64 kbps MP3 (Brandon chose one uniform format). On the Mac data, `--apply` used 460 MB more.
- **Store `combined.wav` as FLAC** (about half the size)? It touches the fixed `recordings/<id>/combined.wav` layout and every reader of it.
- **Prune old `backups/` snapshots** (keep the newest N)? Small today; it grows with each migration.
- **Minor:** with an unfrozen schema and `WISPER_OUTPUT_DIR` unset, a CLI command creates the configured output folder (empty) before the dev guard refuses (`path_utils.get_output_dir` mkdir). The server path creates nothing.
- **Seen, unrelated:** speaker matching printed a `nan` similarity for a speaker with a very short excerpt (`SPEAKER_00 → Announcer (nan)`). `docs/docker.md`'s `docker compose run wisper nvidia-smi` runs `wisper nvidia-smi`; use `--entrypoint nvidia-smi`.

---

## Campaign folders — transcripts in a folder per campaign (`feat/campaign-folders`, after storage trim)

**Status:** design decisions taken (Brandon, 2026-10-03). It builds on storage trim's `rename_companions`, `audio_path`, and Needs-attention panel.

**Before any work starts (gate, in order):**
1. **Re-review every decision below against the code as merged** after storage trim: helpers, schema, routes, and anything storage trim changed or learned. Update or drop decisions that no longer fit, and confirm changes with Brandon.
2. **Write the detailed, worker-ready design**, like storage trim's (`plan.md` at `dc62163`): phases with Read first, Steps, the existing tests to rewrite, new tests, Docs, and Done when.
3. **Run the three reviews again** on that design and fold in the results:
   - an Opus reviewer checking correctness and safety against the code;
   - a Sonnet reviewer walking each phase as the worker who will build it;
   - a database reviewer checking the migration and schema.
4. **Brandon approves** the design. Only then does Phase 1 go to a worker.

**Decisions:**
- A transcript in a campaign lives in `<output root>/<campaign folder>/`. A transcript in no campaign stays in the output root.
- The campaign folder is named after the campaign's display name. Renaming a campaign renames its folder and updates the database.
- Moving a transcript to another campaign (or out of all of them) moves its `.md` and every companion file: summary, sidecar, excerpts, and audio.
- The same transcript name may exist in two campaigns (`A/Session 1.md` and `B/Session 1.md`).
- A file's location is campaign folder + transcript name. The `files` registry (storage trim Phase 1) stores each file's path, and campaign moves update it through `file_registry.move`.
- **Dupes:** any move, rename, or upload that would land on an existing file prompts Keep or Overwrite, showing that file's last-modified time.
- **Renames:** a Rename action in wisper renames the `.md` and its companions together (`rename_companions`), with the same clash prompt.

**Known consequences, for the design pass:**
- `transcripts.stem` is `UNIQUE` across all transcripts (`db.py:323`), and its CHECK forbids path separators. Uniqueness becomes per campaign, while the campaign lives in `campaign_transcripts`, a separate table. One option is moving `campaign_id` onto `transcripts` with a unique index on `(coalesce(campaign_id, 0), stem)`. Either way it needs a new migration that rebuilds `transcripts`; every table that references it needs care.
- URLs identify a transcript by name (`/transcripts/{name}`, campaign and journal routes, search deep links). With duplicate names they need the campaign or the row id.
- 28 path builders in 11 files assume `<output root>/<stem><suffix>`. They go through one location helper.
- `reconcile()` and `_transcript_files` scan only the output root. They scan campaign folders too, and a file found in a campaign folder that doesn't match its row's campaign is a move to resolve, or a Needs-attention item.
- Display names may contain characters Windows forbids in folder names, and two display names may map to the same folder name. Sanitizing and uniqueness rules are needed.
- Obsidian links Brandon typed by name keep working after a move. Links written with a path, or ambiguous names shared by two campaigns, may not.
- The existing flat output root needs a one-time migration of files into campaign folders: a CLI command with a dry run, like `wisper storage trim`.
**Database review input (2026-10-03, against v1–v8 plus storage trim's planned v9 `files` table).** Re-check each item at the gate.
- **Migration number:** v11 (storage trim adds v9, the `files` registry, and v10, which drops `audio_rel_path`).
- **Foreign keys off during migrations:** storage trim Phase 1 adds `PRAGMA foreign_keys=OFF` to `migrate()`. Without it, rebuilding `transcripts` would cascade-delete its speakers, search data, campaign places, journal entries, and `files` rows, and the final `foreign_key_check` would still pass. The v8→v10 upgrade test must seed every child table and assert their row counts are unchanged.
- **Per-campaign uniqueness:** move `campaign_id` and `position` onto `transcripts` and drop `campaign_transcripts`.
  - `UNIQUE (campaign_id, stem)`, plus a partial unique index on `stem` for the root (`WHERE campaign_id IS NULL`).
  - `CHECK ((campaign_id IS NULL) = (position IS NULL))`, `UNIQUE (campaign_id, position)`, and `UNIQUE (campaign_id, id)` as the journal's FK target.
  - `campaign_id REFERENCES campaigns(id)` with no action (RESTRICT): a campaign's transcripts move out (with the clash prompt) before it's deleted.
  - Rebuild `journal_entries` to reference `transcripts(campaign_id, id)`. A `BEFORE UPDATE OF campaign_id` trigger deletes the journal entry on a move, which marks the journal stale via `journal_entries_ad`. Pin its ordering with a test.
- **`campaigns.folder` is stored:** `TEXT NOT NULL COLLATE NOCASE UNIQUE`, with a CHECK forbidding `/ \ : * ? " < > |`, a leading dot, and trailing spaces or dots.
  - Rename order: `display_name` changes at once; `folder` changes only after the directory rename succeeds (Obsidian or Explorer may hold it open).
  - v11 seeds `folder` from `slug`. Its import step sets sanitized display names with a frozen sanitizer, de-duplicated with `casefold()`.
- **Rebuild procedure:** create `X_new`, copy (keeping `id`), `DROP X`, `ALTER TABLE X_new RENAME TO X`. Never rename the old table first: SQLite then rewrites child FKs to point at it.
  - Recreate the `transcript_titles_*` triggers and run `INSERT INTO transcript_titles(transcript_titles) VALUES('rebuild')`.
  - All structural changes go in the DDL string, because `expected_schema()` replays only DDL.
- **`transcripts.id` becomes `INTEGER PRIMARY KEY AUTOINCREMENT`,** so ids are never reused, and URLs key transcripts by id.
- `transcripts.audio_rel_path` is already gone (v10).
- **`legacy_root` (a flag for files not yet moved into their campaign folder)** is unnecessary only if every path lookup resolves through `files.rel_path` (the `transcript` row is the authoritative `.md` location; storage trim Phase 1). If any helper builds paths from campaign folder + stem instead, it comes back. Decide at the gate: one source of truth for locations.
- **Code that ships with v11:**
  - every `campaign_transcripts` query (`campaign_manager`, `journal`, `search_index`, `transcript_store`, `job_history`);
  - `_purge_recording_files` (`record.py:526`) and every other output-root-only path check: `recording_manager._transcript_id` (`:256`), `search_index.reindex_path` (`:215`), `job_history._subject_ids` (`:82`), and `JobRecord.output_path`. These move to the registry.
  - `recording_manager._load`'s join on `campaign_transcripts` (storage trim Phase 8) becomes `transcripts.campaign_id`.
  - A campaign-folder rename rewrites `files.rel_path` by prefix with `substr(rel_path, 1, length(:old) + 1) = :old || '/'`, not GLOB (folder names can contain `[`, `*`, `?`).

**Questions for the gate (second review round, 2026-10-03):**
1. **One source of truth for locations:** `files.rel_path`, or campaign folder + stem (see `legacy_root` above)?
2. **Campaign delete:**
   - `files.campaign_id` (v9) cascades, but v11 makes `transcripts.campaign_id` RESTRICT; reconcile the two;
   - storage trim Phase 2's Delete everything / Keep the files choice must also move or delete the campaign folder;
   - does the journal live in the data dir or in the campaign folder?
3. **URLs:** `/transcripts/{id}`; what happens to old stem URLs, search deep links, and Obsidian links.
4. **`process_file` must know the campaign before writing,** so the `.md` lands in the right folder. Define the CLI behaviour too.
5. **Folder names:**
   - Windows rules: reserved names (CON, PRN, AUX, NUL, COM1–9, LPT1–9), trailing dots and spaces, MAX_PATH (260) with long names;
   - Unicode case folding (`COLLATE NOCASE` folds ASCII only);
   - what to do when the output root already holds a user folder with that name. Never adopt or rename a folder wisper didn't create.
6. **Folder renames on Windows** fail while any file inside is open, including by the search indexer. They need retry, plus a crash rule between the directory rename and the `rel_path` update. A per-file campaign move isn't atomic across N files either.
7. **Refuse moves** while a job targets that transcript.
8. **"Overwrite" on a clash** never replaces a file wisper doesn't own (no `files` row).
9. **Vault clutter:** companions (excerpts, `_diar.json`, `.flac`, `.bak`) sit in each campaign folder. Consider a hidden `.wisper/` subfolder.
10. **Scanning:** reconcile's scanning depth; whether the size/mtime rename match also detects cross-folder moves.
11. **Moving existing files:** the flat-to-folders command (dry run, rollback, and its order relative to `wisper storage trim`).
12. **Every flat-root check** must move to the location helper: `pipeline._under_output_root` (707–712), `campaigns.py` `_transcript_missing`/`summarized` (~89–104), `transcribe._existing_transcript`/`name_check`, `search_index.check_freshness`/`_paths`, `find_excerpt_clip`, `link_transcript`/`_transcript_id`, and every stem-keyed API.
13. **Case sensitivity across moves:** a data dir moved between case-insensitive (Windows, macOS) and case-sensitive (Linux) filesystems. Should per-campaign stem uniqueness ignore case?
14. **Storage trim's flat-root assumptions** must all change: `_v9_import`'s "contains `/`" skip; `sync`'s top-level-only scan; the basename-only Needs-attention routes and `_companion_stem`; Phase 6's orphan rule; Phase 7's audio-route guard.
15. **`files` CHECKs:** `(root = 'output') = (transcript_id IS NOT NULL)` and the `journal` path pin forbid a journal inside a campaign folder. Changing them means rebuilding `files` in v11 (Q2 decides).
16. **`campaigns.folder` renames:** rewrite `files.rel_path` by trigger (like `files_profile_key_au`) or by code?
17. **CLI:** do `wisper transcribe --campaign` and folder runs (`_folder_output_path`, `pipeline.py:717`) write into campaign folders?

---

## Forced word alignment — follow-ups

Design in `architecture.md` ("Forced word alignment"). The `forced_alignment = auto` default rests on spot-check evidence, not a full labelled set: on a 2 h episode, aligned and unaligned runs disagreed on 155 of 20,675 words; one-word "islands" inside another speaker's run were 10 (unaligned) vs 3 (aligned); 4 of 4 hand-checked disputed words were right with alignment.

- **If attribution at speaker changes regresses, or before changing the aligner, smoothing thresholds, or the default,** run `scripts/alignment_eval.py` (see `docs/scenarios.md`) on a live-table excerpt and label its sheet. Arms already exist for the open questions: `aligned-guard-1s` (on edited podcast audio, words moved >1 s favoured Whisper 8 vs 1, the opposite of the Hanataz spike's 28 vs 1), `aligned-smooth-1w` / `aligned-nosmooth`, and `aligned-exclusive`.
- **Fallback engine:** `torchaudio` MMS_FA + star token (no new dependency, but CC-BY-NC weights and weaker on crosstalk). Only if the `transformers` dependency becomes a problem; recipe in git history (`6286e21`).
- **Watch:** `Qwen3ASR*` is new in transformers 5.x and may be renamed; the calls are covered by tests.

---

## Speaker consistency — remaining

Design in `architecture.md`. Open items:

- **Diarization measurement set.** 8–12 hand-corrected excerpts of 2–3 min, stratified by speaker count (2–3 / 4–5 / 6–8), in-room vs remote, low vs high overlap. Report DER split into missed / false alarm / confusion (`pyannote.metrics`) plus JER, and compare configs with a paired bootstrap over recordings (B ≥ 1000). The 0.55 threshold and the community-1 choice rest on one session pair until this exists.
- **Ruled out for now:** Sortformer (4-speaker cap), DiariZen (CC-BY-NC), NVIDIA Nemotron diarization (no independent validation). Revisit only if community-1 plateaus on the measurement set.

---

## Live recording — feature requests

- **Change input devices mid-session.** `LocalCaptureManager.start_session()` binds both capture threads to fixed device IDs; switching today means Stop + Start (a new `Recording` and a gap in the transcript). Needs capture threads that can restart against a new device while the tick thread, segment writers, and `Recording` keep running. Needs a design pass first.
- **Per-speaker diarization on the live system track.** The system track is a single RMS-attributed "Other", because OS loopback is already a mixed-down stream. pyannote only gives consistent labels across a whole-file pass; live use would need an incremental layer (embed each turn, match against a running per-session speaker pool) plus added latency in the live loop. Revisit if the You/Other split becomes limiting.
- **Channel picker on the Record page.** `GET /api/record/channels` already lists the guilds and voice channels the bot can see; the Record page still takes raw IDs or a preset.
- **Replay markers into the ticker on reload.** Markers persist (`recording_markers`) and show on the detail page, but a page reload doesn't re-insert them into the live ticker (only transcript lines come back through SSE). For finished sessions, the transcript-page player lists markers ("Storage trim" Phase 7); this item is the live ticker only.

---

## Intel Arc GPU support (planned — not started)

Design only; no code written. File:line references date from the original design spike — re-locate them before implementing.

### Context

wisper-transcribe today accelerates on **NVIDIA (CUDA)** and **Apple Silicon (MPS)**. We want **Intel discrete Arc GPUs** (Alchemist + Battlemage, validated on an **A310**) to be a first-class target — "as flawless as CUDA."

The hard constraint that shapes everything: **faster-whisper / CTranslate2 has no Intel backend** — it runs CPU or CUDA only. So transcription on an Arc card requires a *second inference engine*, not a device-string tweak. Diarization/embedding (pyannote on PyTorch) **can** run on Intel via the PyTorch `xpu` device, so those are mostly plumbing.

**The codebase already has the pattern we need.** The MLX path for Apple Silicon (transcriber.py:171-186) dispatches to an alternate backend (`_transcribe_mlx`) based on device, returning the same `list[TranscriptionSegment]`. The OpenVINO backend mirrors this almost 1:1.

### Locked decisions (from PM)

| Decision | Choice | Implication |
|---|---|---|
| User-facing device token | **`intel`** | `--device intel`, `device = "intel"`. Translated internally: torch `xpu`, OpenVINO `GPU`. |
| `device=auto` behavior | **Auto-select** | Resolve order: CUDA → **intel** → MPS → CPU. No flags needed when an Arc card is present. |
| XPU diarization op-gap | **Warn loudly, continue on CPU** | Transcription stays on the Arc GPU; diarization/embedding retry on CPU with a prominent per-run warning. |

### Architect-level calls (documented for the record)

- **Transcription engine = `optimum-intel` (`OVModelForSpeechSeq2Seq`).** Auto-converts HF Whisper models to OpenVINO IR on first run and caches them (mirrors MLX's auto-download UX). `openvino-genai`'s `WhisperPipeline` is faster but lower-level — a **future perf lever**, not v1.
- **OpenVINO uses HF-format Whisper models** (e.g. `openai/whisper-large-v3-turbo`), a *separate download* from faster-whisper's CTranslate2 models. Documented; not a blocker.
- **`compute_type` does not apply to the OpenVINO path.** CT2 quant types are CT2-only. On `intel`, OpenVINO defaults to FP16 on GPU; INT8 (NNCF) is future. The intel path **skips `resolve_compute_type()`**.
- **Optional dependency, mirroring `[macos]`/mlx.** Core install unchanged; Intel is `pip install "wisper-transcribe[intel]"` + a torch XPU-index install (handled by Docker/setup scripts, like cu126).

### Device translation model (the heart of the change)

One user token (`intel`) fans out to two frameworks. Centralize the mapping in one place.

```
user "intel"
   ├─ transcription  → OpenVINO  device="GPU"   (optimum-intel)
   └─ diarization /  → PyTorch   torch.device("xpu")
      embedding
```

**Add to `config.py`:**

```python
def torch_device_string(device: str) -> str:
    """Map the user-facing device token to a torch device string.
    'intel' is exposed to users but PyTorch/IPEX call Intel GPUs 'xpu'.
    Everything else passes through unchanged.
    """
    return "xpu" if device == "intel" else device
```

**Extend `get_device()` (config.py:148) — the order encodes the auto-select decision:**

```python
def get_device() -> str:
    """Return 'cuda', 'intel', 'mps', or 'cpu' based on available hardware."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch, "xpu") and torch.xpu.is_available():
            return "intel"          # ← Intel Arc, auto-selected after CUDA
        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    except ImportError:
        return "cpu"
```

> **Builder note:** `torch.xpu` is provided natively by the **PyTorch XPU wheel** (`https://download.pytorch.org/whl/xpu`, the Intel analog of cu126). IPEX adds extra op coverage/perf but `torch.xpu.is_available()` works without it on the XPU build. The `hasattr(torch, "xpu")` guard keeps CPU/CUDA wheels (no `torch.xpu`) safe.

### Implementation phases

Each phase is committed separately and **pauses for PM review**. **Docs are updated in the same commit as the code** (CLAUDE.md rule). Hardware-validation milestones run on the Proxmox A310 (native Linux build or the Docker `intel` target); Windows-native is validated-by-proxy.

#### Phase 1 — Device plumbing & detection (no hardware; 100% unit-testable)

**Goal:** `intel` is a recognized device everywhere CUDA/MPS are, `auto` detects it, and selecting it without the backend gives a clean "not installed" error.

| File | Change |
|---|---|
| config.py | Add `torch_device_string()`; extend `get_device()`. `resolve_compute_type()` unchanged (CT2-only). |
| cli.py:29 | Add `"intel"` to `--device` `click.Choice`. |
| cli.py:202-208 | `setup` command: add `"intel": "Intel Arc GPU (XPU/OpenVINO)"` label + note (transcription=OpenVINO, diarization=XPU). |
| web/routes/config.py:28 | Add `"intel"` to the device choices array. |
| docs/configuration.md, docs/cli-reference.md | Document the `intel` device value. |

**Tests** (test_config.py, test_cli.py): `get_device()` → `"intel"` when `torch.xpu.is_available()` mocked True (cuda False); `torch_device_string("intel") == "xpu"` + passthrough; CLI/web accept `--device intel`.

#### Phase 2 — OpenVINO transcription backend ⭐ core feature

**Goal:** `--device intel` transcribes Whisper on the Arc GPU. **Mirror the MLX structure exactly.**

`transcriber.py` additions (model after `_MLX_MODEL_MAP` / `_is_mlx_available` / `_transcribe_mlx`):

```python
_OPENVINO_MODEL_MAP = {
    "tiny":            "openai/whisper-tiny",
    "base":            "openai/whisper-base",
    "small":           "openai/whisper-small",
    "medium":          "openai/whisper-medium",
    "large-v3":        "openai/whisper-large-v3",
    "large-v3-turbo":  "openai/whisper-large-v3-turbo",
}

def _is_openvino_available() -> bool:
    """True if optimum-intel + openvino are importable. Cheap find_spec check
    (mirrors _is_mlx_available) so it's safe to call from the uvicorn process;
    the heavy import happens inside _transcribe_openvino (worker/subprocess)."""
    import importlib.util
    return (importlib.util.find_spec("optimum") is not None
            and importlib.util.find_spec("openvino") is not None)

def _transcribe_openvino(audio_path, model_size="medium", language="en",
                         initial_prompt=None, hotwords=None):
    """Transcribe on an Intel GPU via OpenVINO (optimum-intel).
    Returns list[TranscriptionSegment] — same contract as faster-whisper/MLX."""
    from optimum.intel import OVModelForSpeechSeq2Seq
    from transformers import AutoProcessor, pipeline as hf_pipeline
    from tqdm import tqdm

    repo = _OPENVINO_MODEL_MAP.get(model_size, f"openai/whisper-{model_size}")
    tqdm.write(f"  Using OpenVINO backend ({repo}) on Intel GPU")

    # export=True converts HF → OpenVINO IR on first run and caches it.
    # device="GPU" targets the Intel Arc.
    model = OVModelForSpeechSeq2Seq.from_pretrained(repo, export=True, device="GPU")
    processor = AutoProcessor.from_pretrained(repo)
    asr = hf_pipeline(
        "automatic-speech-recognition",
        model=model, tokenizer=processor.tokenizer,
        feature_extractor=processor.feature_extractor,
        chunk_length_s=30, return_timestamps=True,
    )
    # hotwords → initial_prompt prefix, same trick as MLX (no native hotwords param).
    prompt = initial_prompt or ""
    if hotwords:
        hw = ", ".join(hotwords)
        prompt = f"{hw}. {prompt}".strip() if prompt else hw
    gen = {"language": language} if language else {}
    if prompt:
        gen["initial_prompt"] = prompt   # passed via generate_kwargs where supported
    result = asr(str(audio_path), generate_kwargs=gen)

    segs = []
    for ch in result.get("chunks", []):
        start, end = ch.get("timestamp", (None, None))
        text = (ch.get("text") or "").strip()
        if text and start is not None and end is not None:
            segs.append(TranscriptionSegment(start=float(start), end=float(end), text=text))
    return segs
```

Dispatch in `transcribe()` — add a branch beside the MLX one (transcriber.py:167-187):

```python
    if device == "auto":
        device = get_device()

    if device == "intel":
        if not _is_openvino_available():
            raise RuntimeError(
                "Intel GPU transcription needs the OpenVINO backend.\n"
                "Install it with: pip install 'wisper-transcribe[intel]'\n"
                "Or use --device cpu."
            )
        return _transcribe_openvino(audio_path, model_size=model_size,
                                    language=language, initial_prompt=initial_prompt,
                                    hotwords=hotwords)
    # ... existing MLX (mps) branch and faster-whisper path unchanged ...
```

> **Builder notes:**
> - `load_model()` / CTranslate2 are **never touched** on the intel path — `_transcribe_openvino` returns first. Leave `load_model` as-is.
> - `compute_type` is intentionally ignored on intel. If user set non-`auto`, `tqdm.write` a one-line note it doesn't apply to OpenVINO.
> - `vad_filter` has no OpenVINO equivalent here (like MLX) — silently skipped.
> - Confirm the exact `generate_kwargs` prompt key against the installed transformers version during hardware bring-up; `initial_prompt` support varies. **Flag for hardware milestone.**

| File | Change |
|---|---|
| pyproject.toml:44 | New extra: `intel = ["optimum-intel[openvino]>=1.20", "openvino>=2024.4", "transformers>=4.45"]`. (torch XPU build is index-url — Phase 4.) |
| docs/setup.md, architecture.md | Add OpenVINO backend to the component table + a design-decision section. |

**Tests** (test_transcriber.py): patch `_is_openvino_available`→True and mock the optimum/HF pipeline factory; assert `transcribe(..., device="intel")` returns mapped `TranscriptionSegment`s; assert chunk→segment mapping (drop empty/None); assert clean RuntimeError when unavailable. **No real model load** — mock the pipeline as `test_transcriber.py` mocks `WhisperModel`.

**🔌 Hardware milestone #1 (A310):** real transcription on GPU; verify IR conversion + cache; sanity-check WER vs CPU.

#### Phase 3 — XPU diarization & embedding (warn-and-fallback)

**Goal:** pyannote diarization + speaker embeddings run on the Arc GPU via `xpu`, with a loud CPU fallback on op gaps.

`diarizer.py` — `load_pipeline()` (diarizer.py:91-121):

```python
    import torch
    from .config import torch_device_string
    if device == "intel":
        if not (hasattr(torch, "xpu") and torch.xpu.is_available()):
            raise RuntimeError("Intel XPU not available. Install the PyTorch XPU "
                               "build, or use --device cpu.")
    # ... existing cuda / mps validation ...
    _pipeline.to(torch.device(torch_device_string(device)))   # 'intel' → 'xpu'
```

`diarize()` — wrap execution with warn-and-fallback (realizes the PM decision):

```python
    try:
        diarization = _pipeline(audio_dict, hook=hook, **kwargs)
    except Exception as exc:
        if device == "intel":
            from tqdm import tqdm
            tqdm.write("⚠  INTEL GPU DIARIZATION FAILED — falling back to CPU for "
                       f"diarization (transcription stays on GPU). Reason: {exc}")
            load_pipeline(hf_token, "cpu")          # reload pipeline on CPU
            diarization = _pipeline(audio_dict, hook=hook, **kwargs)
        else:
            raise
```

`speaker_manager.py` — `_load_embedding_model()` (speaker_manager.py:101-121): mirror the translation (`device in ("cuda","mps","intel")` → `.to(torch.device(torch_device_string(device)))`) and the same try/warn/CPU-fallback around `inference.crop(...)` in `extract_embedding()`.

> **Builder notes:**
> - The fallback **reloads** the module-level `_pipeline` on CPU (can't reliably move a partially-failed pipeline). After a fallback the global stays CPU for the rest of the run — acceptable; resets next process.
> - Keep the warning prominent and **every run** (PM decision), not once-per-session.

**Tests** (test_diarizer.py, test_speaker_manager.py): mock `torch.xpu.is_available()`→True, assert `_pipeline.to` called with `torch.device("xpu")` for `device="intel"`; simulate the pipeline call raising once, assert warning + CPU reload+retry returns `DiarizationSegment`s.

**🔌 Hardware milestone #2 (A310):** diarization on GPU; deliberately exercise the fallback and confirm warning + CPU completion.

#### Phase 4 — Packaging: Docker, Linux, Windows

**Docker** — new `intel` stage in `Dockerfile` mirroring `gpu`, plus the **Intel GPU runtime** the OpenVINO GPU plugin needs (the extra step CUDA doesn't have):

```dockerfile
# ── intel target ──────────────────────────────────────────────────────────
FROM base AS intel
# Intel GPU runtime for the OpenVINO GPU plugin + level-zero (apt, from Intel repo):
#   intel-opencl-icd, libze1, libze-intel-gpu1   (package names per Intel's docs)
RUN pip install --no-cache-dir -e ".[intel]" \
 && pip install --no-cache-dir --upgrade "torch>=2.8.0" "torchaudio>=2.8.0" \
        --index-url https://download.pytorch.org/whl/xpu \
 && python -m wisper_transcribe.tailwind
ENTRYPOINT ["wisper"]
CMD ["--help"]
```

**docker-compose.yml** — `wisper-intel` + `wisper-intel-web` services with `/dev/dri` passthrough (Intel analog of the nvidia `deploy.resources` block):

```yaml
  wisper-intel-web:
    build: { context: ., target: intel }
    image: wisper-transcribe:intel
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - "render"        # host 'render' gid; may need a numeric gid on some hosts
    # ... shared volumes/env/ports as the other services ...
```

**Makefile:** `start-intel` (`docker compose up wisper-intel-web`), `build-intel`, `shell-intel`.

**setup scripts:**
- `setup.sh` (Linux): detect Arc (`lspci | grep -i 'VGA.*Intel.*Arc'` or `/dev/dri/renderD*` + `clinfo`); install torch from xpu index; `pip install -e ".[intel]"`; verify `python -c "import torch; print(torch.xpu.is_available())"`.
- `setup.ps1` (Windows): detect via `Get-CimInstance Win32_VideoController` name match `Arc|Intel`; install xpu-index torch; `[intel]` extra. **Windows = code-complete, validated-by-proxy.**

| File | Doc |
|---|---|
| docs/docker.md | Intel section: `/dev/dri` passthrough, `render` group, runtime packages, `make start-intel`, `wisper --device intel` verify. |
| docs/setup.md | Intel install paths (Docker / Linux / Windows), model-storage note (separate HF-format models), A310/4 GB model-size guidance. |

> **Builder notes / risks:**
> - Intel **compute-runtime apt package names drift** across base-image versions — confirm against Intel's current install guide during the Docker build; most failure-prone step. Pin versions once known good.
> - On Proxmox: the A310 must be **passed through to the VM/LXC** running Docker, and `/dev/dri/renderD128` visible inside the container; the container user needs the host `render` gid.
> - torch XPU wheels bundle the SYCL runtime, but the **OpenVINO GPU plugin** still needs the system `intel-opencl-icd`/level-zero — don't assume torch wheels cover OpenVINO.

**🔌 Hardware milestone #3 (A310):** build + run the Docker `intel` target on the Proxmox host end-to-end; repeat with a native Linux `setup.sh` build.

#### Phase 5 — Final docs consolidation

`architecture.md`: module-map entries for the OpenVINO backend + `torch_device_string`; a "Design Decisions" entry (engine choice, device-token mapping, warn-and-fallback); **Known Constraints** rows (Intel transcription requires OpenVINO not CT2; XPU diarization op gaps → CPU fallback; Windows validated-by-proxy). `README.md`: add Intel to the accelerator list if the quickstart mentions GPUs. Remove these Intel entries from `plan.md` as phases complete.

### Complete device-touchpoint inventory (so nothing is missed)

Representative branch points (most handled via the `auto`-resolve + `torch_device_string` helper, so few need bespoke logic):

- **Choice lists:** cli.py:29, web/routes/config.py:28 — add `"intel"`.
- **Detection/resolve:** config.py:148 (`get_device`), pipeline.py:375 & pipeline.py:594 (`auto`→`get_device`, already generic), transcriber.py:167.
- **Backend dispatch:** transcriber.py:171 (add intel branch beside MLX).
- **torch `.to(device)`:** diarizer.py:120, speaker_manager.py:118-120 — route through `torch_device_string`.
- **Validation blocks:** diarizer.py:108-119, transcriber.py:120-128 — add intel/xpu checks.
- **Parallel-workers guard** pipeline.py:596 (`workers>1 and device!="cpu"` → clamp to 1): **no change** — `intel` is non-cpu so correctly clamped to a single worker, like cuda.
- **MLX gates** pipeline.py:410, transcriber.py:171: stay `mps`-only — **no change**.

### Testing strategy (CI stays GPU-free)

All ML mocked, per CLAUDE.md. New seams are patchable like the existing ones:

- `_is_openvino_available()` and the optimum/HF pipeline factory are the mock points for transcription (parallel to `WhisperModel`).
- `torch.xpu.is_available()` is patched for detection + diarization device tests.
- Fallback paths tested by making the mocked pipeline call raise once, then asserting the warning + CPU retry.
- New/extended files: test_transcriber.py, test_diarizer.py, test_speaker_manager.py, test_config.py, test_cli.py, plus test_web_routes.py for the device choice.

**Hardware validation runbook** (A310, outside CI — Linux native or Docker `intel`):
1. `wisper setup` reports `Intel Arc GPU (XPU/OpenVINO)`.
2. `wisper transcribe sample.mp3 --device intel` → transcript; first run converts/caches IR; GPU utilized (`intel_gpu_top`).
3. `--device auto` selects intel automatically.
4. Force an XPU diarization op gap → loud warning + CPU completion (transcription still on GPU).
5. Docker `make start-intel` → web UI transcribes a file end-to-end with `/dev/dri` passthrough.

### Risks & open items

- **R1 — Intel compute-runtime packaging** (Docker apt names / Proxmox passthrough). Highest-risk; resolved empirically at Phase 4 hardware bring-up.
- **R2 — `generate_kwargs` prompt key** for OpenVINO transcription varies by transformers version (Phase 2 hardware check).
- **R3 — IPEX vs native `torch.xpu`** op coverage for pyannote; warn-and-fallback (Phase 3) is the safety net by design.
- **R4 — Windows-native is validated-by-proxy** (no A310 on the Windows box) — documented, not claimed as proven.

---

## Job cancellation — best-effort GPU stop

Stopping an in-flight transcribe job marks it Failed, but the GPU keeps running until the current CTranslate2 batch finishes.

**Why cancellation is cooperative-only:**
- `cancel_event.is_set()` is checked only inside `capturing_write()` and `ProgressCatcher.write()`, which fire when tqdm emits output.
- Between tqdm ticks the worker is inside CTranslate2's C++ code, which has no Python yield points or cancel hook.
- `pipeline.py` has no awareness of the job's cancel event.

**Options:**
1. **Run transcription in a subprocess and terminate it on cancel.** `parallel_stages = true` already does this for concurrent transcribe + diarize. Generalizing it costs ~1–2 s of startup per job but releases the GPU cleanly.
2. **Check the cancel event between segments in `pipeline.process_file()`.** Cheaper, but doesn't help mid-batch.
3. **Document cancel as best-effort** and add a force-quit button that terminates at the OS level.

**Recommendation:** option 1, reusing the parallel-stages subprocess plumbing. Deferred until cancellation is used often enough to justify it.

---

## DAVE sidecar → Python migration (parked)

The Java sidecar (JDA 6.3.0 + JDAVE 0.1.8) receives and decrypts DAVE-encrypted audio end-to-end. DAVE is mandatory for non-stage voice, so the only question is where it's implemented. DAVE is MLS over OpenMLS and every path depends on a native (Rust/JNI) binding; the choice is which language wraps it.

**Python readiness (as of 2026-06):**
- **pycord PR #3159** — DAVE receive for pycord, which has native voice receive. Approved but still a draft, milestoned for 2.9.0rc1. The right target once released.
- **discord.py PR #10300** — shipped in 2.7.x but flagged tentative. discord.py has no first-class voice receive, so it doesn't fit a recording bot.
- **`davey`** — the OpenMLS binding both use; beta (v0.1.5) with no usage docs.

**Verdict:** keep the sidecar. Revisit when pycord 2.9 ships #3159 as stable.

**Migration path:**
1. Delete `discord-bot/` (the Gradle/Java project).
2. Write a ~100-line Python replacement that emits the same wire format over the existing Unix socket: length-prefixed user_id + 48 kHz stereo PCM, including the pre-mixed `__mixed__` stream.
3. Point `BotManager` at the Python script instead of the JAR.
4. Remove the Java builder stages from `Dockerfile` and the Java 25 requirement from launchers and docs.

Nothing else changes; the wire protocol is the stable interface.

**Fallback if the native-binding ecosystem stalls:** skip DAVE entirely — run a real Discord client in the channel and capture its decrypted output through a loopback device. Heavier to operate and loses per-speaker separation; kept only as an escape hatch.

---

## Campaign-level LLM summaries (DM tools)

The rolling campaign journal sets the pattern: slug-scoped storage under `campaigns/<slug>/`, `.summary.md` discovery via `unjournalled_sessions()`, and `JobQueue.submit_journal` / `_run_journal_job` as the template for new `JOB_CAMPAIGN_*` types on the standard SSE progress page. All three features below read the same `.summary.md` sidecars (`SummaryNote` already carries loot, NPCs, and follow-ups). Campaigns with no summarized sessions hide or disable the buttons.

**Build on the database:** the transcript registry and `journal_entries`, not stem lists or frontmatter. Combined-summary and recap outputs get their own table with FKs to the campaign (and the sessions they cover), so deletes cascade; add it as a new migration and extend `test_schema.py`. The search index could cover them too (a new `search_index_state.kind`).

### 1. Combined summary

One LLM call over every session summary in a campaign → `campaigns/<slug>/combined_summary.md`. For retrospectives, onboarding a player, or a campaign wiki. ~20 sessions ≈ 20k input tokens; at 50+ the rolling journal is the better tool. Entry point: "Generate combined summary" on the Campaign page, with a warning at high session counts.

### 2. "Previously on…" recap

A 200–400 word, spoiler-free, player-facing recap built from the last 1–3 session summaries. Shown on the Campaign page or exported as `.recap.md`; shareable with players (e.g. to a campaign Discord). The journal is the DM's cumulative view; the recap is a short retelling for players.

### 3. Hierarchical summaries

Group sessions into arcs, summarize each arc, then combine arcs into a campaign overview. Only needed if the rolling journal hits context limits in practice — deferred indefinitely.
