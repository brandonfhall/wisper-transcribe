`# wisper-transcribe — Open Items

---

# Code Review Findings — 2026-07-15 (senior review)

Full-codebase audit on branch `fix/enrollment-audit`. All of `src/` was read end-to-end; every finding cites file:line and the load-bearing ones were grep-verified. Findings are the actionable work queue for the implementing engineer; suggested execution order at the end.

**Baseline note:** `.venv` currently has **no pytest installed** — `.venv/bin/pytest` (the CLAUDE.md canonical command) fails with "No module named pytest". Reinstall dev deps and establish a green baseline before touching anything (see R35).

## CRITICAL — broken features / guaranteed runtime errors

### R1 — `NameError`: missing `import os` in `speaker_manager.py`
`src/wisper_transcribe/speaker_manager.py:291-296` uses `os.path.abspath` / `os.sep` inside `enroll_speaker_from_audio_dir()`, but the module never imports `os`. Every call raises `NameError`. The only caller is the web route `record.py:394` (`recording_enroll_html`), whose blanket `except Exception` swallows it into `?error=enroll_failed` — so **enrolling a Discord speaker from a recording is 100% broken** and looks like a generic failure. The test suite never catches it because `tests/test_record_routes.py:290` mocks the entire function.
**Fix:** add `import os` to module imports. Add a real (non-mocked) unit test for `enroll_speaker_from_audio_dir` path validation that would have caught this.

### R2 — Recording → transcription hand-off can never succeed (`combined_path` never set)
`Recording.combined_path` is assigned exactly once, to `None` (`recording_manager.py:259`), and never populated anywhere (grep-verified). `record.py:452` (`recording_transcribe_html`) requires `recording.combined_path` to exist → always redirects `?error=no_audio`. `BotManager._finalise` (`discord_bot.py:458`) closes the mixed-track segment writer but never merges `recordings/<id>/combined/*.opus` into a single file nor sets `combined_path`.
**Fix:** in `_finalise`, concatenate/transcode the combined-track segments to `recordings/<id>/combined.wav`, set `recording.combined_path`, save. Or change the hand-off to consume the segment directory directly.

### R3 — Cancelling a PENDING job doesn't cancel it; the worker revives and runs it
`web/jobs.py:574-587`: `cancel()` on a PENDING job sets `status=FAILED`, but the job id is still in the asyncio queue. `_worker()` (`jobs.py:593-607`) dequeues it and unconditionally sets `status = RUNNING` and runs it — the "cancelled" job runs to completion anyway, and its status lies the whole time.
**Fix:** in `_worker`, skip jobs whose status is not PENDING at dequeue time (`if job is None or job.status != PENDING: continue`).

### R4 — Model caches ignore parameter changes; failed pipeline load leaves poisoned cache
- `transcriber.py:198`: `if _model is None: load_model(...)` — the cache key ignores `model_size`/`device`/`compute_type`. In the long-running web server, the **first job's model is silently reused for every later job** regardless of the model chosen in the upload form or config.
- `diarizer.py:91-121`: `load_pipeline()` assigns `_pipeline` *before* the CUDA/MPS availability checks that can raise. On failure the half-initialised (CPU-placed) pipeline stays cached; the next `diarize()` call sees `_pipeline is not None`, skips `load_pipeline`, and silently runs on the wrong device.
**Fix:** cache alongside a key tuple `(model_size, device, compute_type)` and reload on mismatch; in `load_pipeline`, assign the global only after all checks pass (build into a local first), and reset to `None` on exception.

### R5 — Config sentinel-defaults are wrong in both directions; several config keys are dead
`pipeline.process_file()` (`pipeline.py:375-387`) applies config with `if model_size == "medium": model_size = config.get("model", ...)` — but both the CLI (`cli.py:27`) and the web form (`routes/transcribe.py:44`) default to `"large-v3-turbo"`. Consequences:
- The `model` config key is **ignored** on the default CLI/web path (`wisper config set model small` does nothing).
- A user who *explicitly* selects `medium` gets it silently **overridden** by the config value.
- Same pattern for `language == "en"`.
- Additionally the `min_speakers`, `max_speakers`, and `timestamps` config keys are **never read by the pipeline at all** (grep-verified) despite being editable in the web Config UI and having DEFAULTS entries — dead settings that mislead users.
**Fix:** use `None` as the only sentinel through the whole stack (CLI/web pass `None` unless the user chose something; pipeline resolves `None → config → hardcoded default`). Wire `min_speakers`/`max_speakers`/`timestamps` into `process_file`'s defaults or remove them from config + UI.

### R6 — Two web routes run minutes of ML work synchronously on the event loop, racing the job worker
- `routes/speakers.py:90-164` (`enroll_submit`, standalone enroll): calls `convert_to_wav`, `diarize`, `extract_embedding` directly inside the `async def` route — **blocks the entire event loop** (UI, SSE, everything) for the duration, and mutates the module-level `_pipeline`/`_embedding_model` globals **concurrently with a running transcription job's thread**, violating the documented one-job-at-a-time invariant (CLAUDE.md).
- `routes/record.py:394` (`recording_enroll_html`): same pattern with pydub decode + embedding extraction (currently masked by R1).
**Fix:** route both through the JobQueue (a JOB_ENROLL-style job, exactly like the wizard's Phase 2.5 split in `enroll_shared.py`), or minimally `asyncio.to_thread` + a queue-level lock. The JobQueue path is the right one — the plumbing already exists.

### R7 — `wisper record list/show/transcribe/delete` CLI commands call 501 stubs
`routes/record.py:166-193`: `/api/recordings` (+ detail/transcribe/delete) return `_NOT_IMPLEMENTED` (501). The CLI (`cli.py:1380-1422`) calls exactly these endpoints, so four documented `wisper record` subcommands always fail with "Server returned 501". Working HTML equivalents exist (`/recordings`, `/recordings/{id}/transcribe`, …).
**Fix:** implement the JSON API by delegating to the same code the HTML routes use, or remove the CLI subcommands until it exists. Update `docs/cli-reference.md` accordingly.

## HIGH — correctness bugs and resource leaks

### R8 — `recordings_list_html` sort crashes on `started_at=None` and contains a no-op expression
`routes/record.py:297-301`: `key=lambda r: r.started_at or r.started_at` — `x or x` is `x`; comparing `None` with `datetime` raises `TypeError`, taking down the whole recordings page if any recording has a null `started_at` (possible via `_str_to_dt(None)` on legacy/corrupt metadata).
**Fix:** `key=lambda r: r.started_at or datetime.min.replace(tzinfo=timezone.utc)`.

### R9 — Temp/orphan file leaks (five distinct ones)
1. `routes/speakers.py:117`: `wisper_enroll_*` temp uploads are never deleted (success or failure), and the startup sweep (`app.py:63`) only globs `wisper_upload_*`.
2. `audio_utils.convert_to_wav` writes a converted WAV to the OS tempdir; **neither the CLI pipeline nor the web transcription job ever deletes it** — every non-16k-WAV transcription leaks a full-length WAV. (`enroll_shared.enroll_profiles` cleans up its own — lines 501-503 — proving the pattern; the main path doesn't.)
3. `audio_utils._extract_first_audio_track:145-150`: on ffmpeg failure the partial `out_path` is left behind.
4. Deleting a transcript leaves its `*_excerpt_*.mp3`/`.txt` clips forever (`routes/transcripts.py:124-137` explicitly punts on this).
5. Speaker profile removal/rename (both CLI `cli.py:643-690` and web `routes/speakers.py:167-190`) leaves the `embeddings/<key>.mp3` reference clip behind; CLI rename moves the `.npy` but not the `.mp3`, so the Speakers-page play button silently breaks after rename.
**Fix:** (1) reuse the `wisper_upload_` prefix or add the enroll prefix to the sweep + delete in a `finally`; (2) delete `wav_path` in `process_file`'s completion path when `wav_path != path`; (3) unlink on the error branch; (4) glob-delete `<stem>_excerpt_*` in `delete_transcript`/`bulk_delete`; (5) delete/rename the clip alongside the `.npy`.

### R10 — Whole-file upload buffering in RAM
`routes/transcribe.py:67` (`content = await file.read()`) and `routes/speakers.py:119` read the entire upload into memory. This is a tool whose primary input is multi-hour (potentially multi-GB) session audio.
**Fix:** stream to the temp file in chunks (`while chunk := await file.read(1 << 20): tmp.write(chunk)`).

### R11 — `refine.apply_edits` does global substring replacement
`refine.py:189-201`: each accepted edit runs `body.replace(original, corrected)` over the whole body — a short `original` ("Dan" → "Don") also rewrites every occurrence inside longer words ("Dandy" → "Dondy") and inside `**Speaker**` labels. The edit-distance guard validates the *target*, not the *blast radius*.
**Fix:** word-boundary regex replacement (`re.sub(rf"\b{re.escape(original)}\b", ...)`), and skip lines that are speaker-label positions.

### R12 — Discord audio path: the same bytes are treated as both Opus and PCM
`discord_bot._route_frame` (`discord_bot.py:387-416`) takes each sidecar frame and (a) writes it to `SegmentedOggWriter` as an **Opus packet**, and (b) feeds it to `RealtimePCMMixer` as **48 kHz stereo PCM**. These interpretations are mutually exclusive — one of the two consumers is processing garbage. Compounding issues in `audio_writer.py`:
- Ogg page CRC is always 0 (`audio_writer.py:74`) — ffmpeg/pydub validate CRCs; decode of these files is at best warning-laden, at worst rejected (this feeds R1's enrollment path: `PydubSegment.from_file(..., format="opus")`).
- Lacing bug: a packet whose length is an exact multiple of 255 is missing the terminating 0-lace (`audio_writer.py:53-58`) → malformed page.
- `OpusHead` hardcodes mono/48 kHz (`audio_writer.py:83`) regardless of what's actually written; the mixed track writes 16 kHz mono PCM into it.
- `mix()` is called once per **incoming** frame (`discord_bot.py:415`), so with N concurrent speakers the combined track advances N×20 ms per real 20 ms — combined-track duration scales with speaker count.
**Fix:** decide the wire format once (plan.md says the sidecar sends PCM). Then: per-user writers should encode PCM→Opus (or store WAV segments), compute real Ogg CRCs, fix the lacing terminator, and drive `mix()` off a 20 ms clock (or per unique-frame-set), not per frame. This subsystem needs a focused pass with a real end-to-end decode test.

### R13 — Transcription job errors leak raw exception text (inconsistent with the project's own rule)
`jobs.py:604` and `jobs.py:719-721` put `str(exc)` into `job.error`, which the job-detail page and SSE stream render. `_run_enroll_job` (`jobs.py:775-835`) was deliberately written to use generic messages *because* exception text can contain filesystem paths (its docstring says so, and CLAUDE.md forbids reflecting exception text). Transcription/LLM jobs violate the same rule.
**Fix:** map exceptions to generic user-facing messages on all job types; log the real exception server-side.

### R14 — Unbounded memory growth in the job queue
`JobQueue._jobs` is never pruned and each `Job.log_lines` grows without limit (LLM stderr capture can be large). A server left running for weeks accumulates every job ever run.
**Fix:** cap retained finished jobs (e.g. keep last 50) and/or cap `log_lines` length per job.

### R15 — `speakers rename` (CLI) silently overwrites an existing profile on key collision
`cli.py:663-690`: `wisper speakers rename A B` where `B`'s key already exists replaces B's entry and renames A's embedding over B's `.npy`. Data loss with no warning.
**Fix:** refuse when `new_key in profiles`.

## MEDIUM — security posture

### R16 — Default bind `0.0.0.0` + zero auth + zero CSRF protection
`cli.py:139` defaults `wisper server` to `0.0.0.0`. Every state-changing endpoint (bulk-delete transcripts, config save **including API keys and tokens**, start/stop Discord recordings, job cancel) is an unauthenticated POST with no CSRF token; `open_data_dir` (`routes/config.py:414`) is a state-changing **GET**, triggerable cross-site by an `<img>` tag. On a home LAN this is a full read-write surface for anyone on the network.
**Fix (pragmatic for a single-user tool):** default `--host` to `127.0.0.1` (keep `0.0.0.0` opt-in for Docker, which can pass it explicitly); document the trust model in `docs/web-ui.md`; make `open_data_dir` a POST. Full CSRF tokens optional beyond that.

### R17 — `_HtmlSanitizer` gaps
`routes/transcripts.py:32-81` strips `<script>` and `on*` attributes but allows `javascript:` URLs in `href`/`src` and doesn't strip `<iframe>/<object>/<embed>`. Transcript bodies are mostly self-generated, but LLM refine output is written into the same files and re-rendered.
**Fix:** drop `href`/`src` attributes whose value (case/whitespace-normalised) starts with `javascript:`/`data:`; add iframe/object/embed to `_STRIP_TAGS`.

### R18 — Header contradiction
`app.py:104-121`: CSP says `frame-ancestors 'none'` while `X-Frame-Options: SAMEORIGIN`. Cosmetic (CSP wins) but pick one story — `DENY` matches the CSP.

## MEDIUM — redundancy / spaghetti (same knowledge in N places)

### R19 — LLM provider metadata exists in four places
Default models: `config._LLM_DEFAULT_MODELS`, `cli.setup` `provider_defaults` (`cli.py:267-273` — this copy is **missing `ollama-cloud`**), `cli.config_llm` `provider_defaults` (`cli.py:490-497`). Env/config key maps: `config._LLM_API_KEY_ENV`, `cli.setup` `env_map` (`cli.py:310-315`), `cli.config_llm` maps (`cli.py:535-546`), `llm/__init__.get_client` (`llm/__init__.py:51-62`).
**Fix:** single source of truth in `config.py`; everything else imports it.

### R20 — CLI `--provider` choice list omits valid providers
`cli.py:1027`: `_LLM_PROVIDER_CHOICE = click.Choice(["ollama", "anthropic", "openai", "google"])` — `lmstudio` and `ollama-cloud` are fully supported (config UI, `get_client`) but unreachable via `wisper refine/summarize --provider`.
**Fix:** derive the Choice from `config.LLM_PROVIDERS`.

### R21 — Output-dir resolution logic duplicated three times
`path_utils.get_output_dir()`, `cli.transcripts_list` (`cli.py:924-926`), `routes/dashboard.py:33` all reimplement "CWD `./output` else `data_dir/output`". The dashboard copy also **counts `.summary.md` files as transcripts** (`dashboard.py:34`), so the dashboard count disagrees with the Transcripts page.
**Fix:** everyone calls `get_output_dir()`; dashboard excludes `.summary.md`.

### R22 — Skip-already-processed logic implemented three times
`process_file` (`pipeline.py:399-401`), `process_folder`'s sequential loop (`pipeline.py:664-667`), and the CLI's post-hoc "skipped" count (`cli.py:116-121`, reconstructed by set arithmetic over names — fragile).
**Fix:** have `process_file` return a status (`written|skipped`) and count from that.

### R23 — `wisper config set` validates nothing
`cli.py:392-410`: unknown keys are silently written (typos become junk config); coercion handles bool/float/list but **not int** (`min_speakers` etc. would be stored as strings — currently masked by R5's dead keys, but a booby trap for the R5 fix).
**Fix:** reject keys not in `DEFAULTS`; add int coercion.

### R24 — Excerpt-serving fallback duplicated between the two wizards
`routes/transcribe.py:337-386` and `routes/transcripts.py:789-832` carry near-identical excerpt-lookup + CodeQL-guard blocks (the comments even cross-reference each other).
**Fix:** extract one helper into `enroll_shared.py` (e.g. `find_excerpt_clip(out_dir, stem, speaker, legacy_map) -> Path|None`).

### R25 — `_get_safe_content_path(request, …)` takes an unused `request` parameter
`routes/transcripts.py:99` — every one of ~15 call sites threads `request` through for nothing. Remove the parameter.

## LOW — smaller bugs, efficiency, style

### R26 — `get_duration` loads the entire file through pydub
`audio_utils.py:181-184`. Contradicts the module's own >4 GB rationale for avoiding pydub, and wastes seconds/GBs just to read a duration. `_probe_duration` (ffprobe, same file) already exists 140 lines up.
**Fix:** use `_probe_duration`, falling back to the `wave` header for WAVs.

### R27 — `aligner._assign_word_speakers` is O(words × turns)
`aligner.py:57-82`: linear scan of all diarization turns per word. A 3-hour session (~30k words × ~2k turns) is ~60M overlap computations in pure Python.
**Fix:** sort turns once, walk with a two-pointer/bisect. Keep `_best_overlap_speaker` for the no-words fallback.

### R28 — `_interactive_enroll` extracts the same embedding twice per speaker
`pipeline.py:251` (ranking) and `pipeline.py:285` (profile update) both call `extract_embedding` for the same label. Each is up to 5 pyannote forward passes. Cache the first result.

### R29 — `transcript_enroll_submit` KeyErrors on legacy sidecars
`routes/transcripts.py:744` indexes `diar["input_path"]` directly (500 on absence) while the sibling GET route uses `.get(...)` (`transcripts.py:673`). Use `.get` + the existing `enroll_audio_missing` notice.

### R30 — `wisper record start` ignores `discord_default_guild`/`discord_default_channel`
`cli.py:1339-1362` requires `--guild`/`--voice-channel` or a preset, never falling back to the config defaults that `wisper config discord` (and the web form) maintain. Wire in the fallback or stop collecting the defaults.

### R31 — Web speaker "rename" and CLI "rename" do different things
Web (`routes/speakers.py:179-190`) changes `display_name` only; CLI (`cli.py:663`) rekeys profile + embedding file. After a web rename, the key≠name convention (`name.lower().replace(" ","_")`, CLAUDE.md) silently breaks for that profile. Pick one semantic (suggest: web adopts CLI's rekey behavior via a shared function in `speaker_manager`), and fix the `.mp3` clip rename per R9-5.

### R32 — Minor per-module nits (batch these)
- `speaker_manager.py:128`: `except (RuntimeError, Exception)` — just `Exception`.
- `speaker_manager.py:423`: `None` stored into `dict[str, np.ndarray]` (typed lie; use a separate `failed: set[str]`).
- `transcriber.py:56`: MLX repo fallback f-string will 404 for unmapped sizes — raise a clear error instead.
- `transcriber.py:236`: `pbar.update(seg.end - pbar.n)` can go negative on non-monotonic segments — clamp.
- `routes/dashboard.py:28-29`: `__import__("os")` inline — import normally.
- `routes/speakers.py:20-21`: redundant `import os` inside `_clip_path` (already module-level).
- `formatter.update_speaker_names` (`formatter.py:247`): `**Old**` regex also rewrites matching bold text in the body — worth a docstring warning.
- `app.py:41-43`: `_INPUT_CSS.stat()` raises uncaught `FileNotFoundError` at startup if `input.css` is missing — guard it.
- `web/jobs.py:566`: `sorted(list(...)[::-1], key=..., reverse=True)` — replace with an explicit `(created_at, seq)` key or at least comment the reverse-then-stable-sort trick.
- `debug_log.Logger._patch_tqdm`: repeated `setup_logging()` calls stack tee-wrappers — make idempotent.
- `summarize._linkify`'s `(?<!\[)`/`(?!\])` guards only check one bracket char — double-wrap possible in edge cases.

### R33 — Web form enums unvalidated
`routes/transcribe.py:44-51`: `model_size`, `device`, `compute_type` accepted as free strings and passed to the ML stack (failure surfaces later as a cryptic job error). Validate against the same choice lists the CLI uses.

### R34 — Anthropic default model id should be verified
`config.py:60`: `"claude-sonnet-4-6"`. Verify against the current Anthropic models list and update `_LLM_DEFAULT_MODELS` if stale.

## PROCESS / environment

### R35 — Dev environment broken vs CLAUDE.md
`.venv` lacks pytest; the documented `.venv/bin/pytest tests/ -v` fails. Reinstall dev deps; consider a `make test` or setup script that pins this.

### R36 — tqdm monkey-patching is load-bearing in three layers (accepted; document it)
`debug_log.Logger._patch_tqdm` (permanent tee), `jobs._run_transcription_job` (per-job capture + restore), and `pipeline._patch_tqdm_for_queue` (per-subprocess) all patch process-global tqdm state. It works because of the one-job-at-a-time invariant, but any concurrency change breaks all of it, and job cancellation only fires when tqdm writes (already noted elsewhere in plan.md). Action: `architecture.md` note tying the three together; revisit if R6's fix or multi-worker lands.

### R37 — Unlocked read-modify-write on shared JSON stores
`campaign_manager`/`speaker_manager` do unlocked load→modify→save of shared JSON (`recording_manager` got per-record locks; the others didn't). Two simultaneous wizard submits or campaign edits can lose writes. Low likelihood single-user; fix opportunistically by mirroring `recording_manager`'s lock pattern.

### R38 — Docs drift to fix alongside the above
When fixing: `docs/configuration.md` (R5 dead keys), `docs/cli-reference.md` (R7, R20, R30), `docs/web-ui.md` (R16 trust model), `architecture.md` (R4 cache keys, R12 audio format contract, R36 tqdm layers), CLAUDE.md Non-Obvious Gotchas if invariants change.

## Suggested execution order

1. **Phase A (small, surgical, high value):** R1, R3, R8, R15, R29 + env fix R35. Each is a few lines + a test.
2. **Phase B (config/CLI coherence):** R5, R19, R20, R21, R22, R23, R30, R33, R34.
3. **Phase C (leaks + memory):** R9 (all five), R10, R14, R26, R28.
4. **Phase D (web correctness/security):** R4, R6, R13, R16, R17, R18, R24, R25, R31.
5. **Phase E (Discord audio subsystem):** R2 + R12 together — needs a wire-format design decision first; do not start piecemeal.
6. **Phase F (nits):** R11, R27, R32, R36–R38 opportunistically.

Each phase = one PR-sized branch, tests green + docs synced per Definition of Done, pause for user review between phases.

---

## Deferred parity gaps

### D5 — Refine/summarize CLI vs web asymmetry
CLI runs these synchronously with `--dry-run` preview. Web runs them as async JobQueue jobs with no dry-run. Both work; the asymmetry reflects the surface (terminal vs. browser), not a missing feature.

---

## Job cancellation — best-effort GPU stop

**Observed (2026-05-11):** clicking Stop on an in-flight transcribe job in the web UI marks the job `Failed` in the queue, but the GPU keeps running hard for the duration of the in-flight CTranslate2 batch. The Python worker exits on the next tqdm tick (cooperative cancel via `job._cancel_event` in `web/jobs.py`), but in-flight inference inside faster-whisper's internal thread pool continues until the batch finishes.

**Why the current mechanism is cooperative-only:**
- `cancel_event.is_set()` is checked inside `capturing_write()` and `ProgressCatcher.write()` — both only fire when tqdm emits output.
- Between tqdm ticks the worker thread is blocked inside CTranslate2's C++ code, which has no Python yield points and no public cancel hook.
- `pipeline.py` itself has no awareness of the job's cancel event.

**Options for true interrupt:**
1. **Run transcription in a subprocess and SIGTERM on cancel.** The `parallel_stages = true` config already does this for the transcribe+diarize concurrency path. Generalising it to single-stage mode would mean every job spawns a subprocess (small startup cost, ~1–2 s) but gives clean GPU release on cancel.
2. **Plumb the cancel event into `pipeline.process_file()`** so it's checked between segments inside the generator loop. Faster than (1) for very short batches; doesn't help mid-batch on the GPU.
3. **Document cancel as best-effort** and add a "Force-quit" button that issues the OS-level termination (Windows-aware, no JVM-style hard kill on POSIX).

Recommendation: option (1) — reuse the parallel-stages subprocess plumbing for the single-stage path too. Tracked here until a user explicitly cancels often enough to justify the work.

---

## DAVE Sidecar → Python migration (parked; not yet viable)

**Issue #39 (DAVE blocking audio receive) is CLOSED** — the original "bot is broken" premise is resolved. The Java JDA 6.3.0 + JDAVE 0.1.8 sidecar receives and decrypts DAVE-encrypted audio today and works end-to-end. DAVE itself is mandatory and unavoidable (Discord enforced E2EE for non-stage voice on March 2, 2026; there is no per-channel opt-out), so the only open question is *where* DAVE is implemented, not *whether*.

**Key fact:** DAVE is MLS over OpenMLS — there is no pure-Python implementation and never will be. Every path depends on a native (Rust/JNI) MLS binding. The choice is which language wraps that binding, not Java-native vs. Python-pure.

**Python DAVE-receive readiness (as of 2026-06-15):**
- **pycord PR #3159** — DAVE *receive* for pycord. Approved by 2 reviewers but still a **draft**, milestoned for **2.9.0rc1** (last activity 2026-06-08). pycord has native voice receive, so this is the right target — but it is **unreleased**.
- **discord.py PR #10300** — **merged 2026-01-07**, shipped in discord.py **2.7.0 / 2.7.1** (2026-03-03), but flagged *"tentative"*. discord.py has **no first-class voice receive**, so it is not a fit for a recording bot regardless.
- **`davey`** (Snazzah's OpenMLS binding, the Rust native lib both discord.py and pycord use) — **v0.1.5, beta, 2026-03-29**, with "proper usage documentation does not exist yet."

**Verdict:** Migrating now would trade a working Java sidecar for an unreleased Python one on a beta native lib. **Keep the sidecar.** Revisit when **pycord 2.9 ships #3159 as a stable release**.

**Migration path** (execute once pycord 2.9 stable lands):
1. Delete `discord-bot/` (the Gradle/Java project)
2. Write ~100-line Python replacement emitting the same length-prefixed PCM wire format over the existing Unix socket
3. Update `BotManager` to launch the Python script instead of the JAR
4. Remove the Java builder stages from `Dockerfile` and the Java 25 requirement from launchers + README

Nothing else changes — `SegmentedOggWriter`, the web UI, campaigns, CLI, and all tests remain unaffected.

**Structural fallback (Strategy B), if the native-binding ecosystem stalls:** both JDAVE and `davey` are small-maintainer libraries tracking a protocol Discord controls and can change. The only DAVE-churn-immune approach is to *not* implement DAVE at all — run a real Discord client joined to the channel and capture its client-side-decrypted audio via a virtual audio (loopback) device. Heavier operationally and loses per-speaker SSRC separation, so not worth building now — documented as the escape hatch if jdave/davey break on a future protocol bump.

---

---

## Storage architecture — SQLite full migration (future consideration)

**Context (2026-05-14):** The job queue is in-memory only. When the server restarts, in-progress enrollment wizards break because `diarization_segments` and `input_path` are lost. The immediate fix is JSON sidecars written alongside the transcript (Option 2, implemented). This section records the case for a full SQLite migration if the app grows.

**Current storage model — "files are the database":**
- `speakers.json` + `.npy` embedding files
- `campaigns.json`
- `.md` transcript files + `.summary.md` sidecars
- `_diar.json` enrollment sidecars (added by Option 2)
- Job queue: in-memory only (ephemeral)

**Why full SQLite would be worth doing at some future point:**
- Transactional writes across related data (e.g., add campaign member + transcript association atomically) — currently `campaigns.json` and `speakers.json` can drift if a crash happens mid-write
- Persistent job history across restarts — past transcription runs, their logs, and enrollment data would all survive
- Relational queries if features grow (e.g., "all transcripts for a speaker", "jobs by campaign")
- Eliminates the proliferating sidecar pattern (`_diar.json`, `.summary.md`, `_excerpt_*.mp3`, `_excerpt_*.txt`) in favour of a single source of truth

**Why we're not doing it now:**
- Requires migrating existing installs (`campaigns.json`, `speakers.json` → tables) with a one-time migration script
- Embedding `.npy` files still live on disk regardless — SQLite would store the path, not the blob
- Loses "just open the file" inspectability; needs `sqlite3` CLI or a viewer
- Schema migrations become a maintenance burden as the codebase evolves (would want `peewee` or similar rather than raw `sqlite3`)
- "Jobs-only SQLite + JSON for everything else" was considered and rejected — the hybrid model is the worst of both worlds, creating two storage patterns to reason about

**Trigger conditions** — revisit when any of these are true:
- Multi-user or networked deployments are needed (SQLite WAL mode handles concurrent reads but not concurrent writes from multiple processes)
- Job history browsing across restarts becomes a user need
- A third JSON file with cross-cutting relationships appears (campaigns.json + speakers.json are already two; a third is the smell)

---

## UI Bugs

---

## Campaign-level LLM summaries (DM tools)

**Context (2026-05-14):** Per-session `wisper summarize` already produces `.summary.md` sidecars with recap, loot, NPCs, and follow-ups. These are session-scoped. The next level is campaign-scoped documents — aggregations across sessions that are most useful to the DM managing an ongoing story.

Four distinct features share the same infrastructure (reading multiple `.summary.md` files, writing a campaign-level output, running through the LLM pipeline):

---

### 1. Rolling campaign journal (incremental, bounded context)

A living document that grows with each new session. On each run the LLM receives `[current journal.md] + [new session.summary.md]` and rewrites the journal to incorporate the new session.

**Why this is the right default:** Context stays bounded — even session 50 only sends one session's worth of new material plus the current journal (~2–5 k tokens each). The journal acts as a compressed campaign memory.

**What it tracks across sessions:**
- Story arc progression and where each thread stands
- Active plot hooks (opened vs resolved)
- NPC roster: who appeared, what role they played, how the relationship evolved
- PC decisions that had lasting consequences
- Running loot/resource ledger (net gains/losses per session)

**Storage:** `data_dir/campaigns/<slug>/journal.md` — a single file that gets overwritten each time a new session is folded in. The individual session `.summary.md` files are never touched; they remain the source of truth.

**Entry point:** "Update journal" button on the Campaign page, enabled when new sessions exist that have not yet been folded in. Track this via a `journal_through: <session_stem>` frontmatter key in `journal.md` — compare against the campaign transcript list to know what's new.

**CLI:** `wisper campaign journal <slug> [--session <stem>]` — folds one session (default: latest un-journalled) into the journal.

---

### 2. Combined summary (batch, full campaign)

Takes all session summaries for a campaign in one LLM call and produces a single consolidated document. Useful for retrospectives, onboarding a returning/new player, or a campaign wiki entry.

**Context ceiling:** A 20-session campaign with typical summaries (~1 k tokens each) is ~20 k tokens of input. Most providers handle this fine. At 50+ sessions it starts to strain context limits — the rolling journal (above) is the better choice at that scale.

**Output:** `data_dir/campaigns/<slug>/combined_summary.md`

**Entry point:** "Generate combined summary" button on the Campaign page. Warn the user if session count is high.

---

### 3. "Previously on..." recap (player-facing, one-pager)

A short (200–400 word) player-facing doc generated before each session. Different tone from the DM journal — no spoilers, no DM-only info, focused on what the players experienced and remember.

**Input:** The most recent 1–3 session summaries (not the full journal).

**Output:** Displayed inline on the Campaign page or exported as a `.recap.md`. Shareable with players — could also be posted to a campaign Discord.

**Distinction from the journal:** The journal accumulates everything (DM view); the recap is a short selective retelling (player view) of the last session or two.

---

### 4. Hierarchical summaries (arc → campaign, scales to any length)

For very long campaigns (30+ sessions), group sessions into arcs, summarize each arc, then combine arc summaries into a campaign overview. Two-level LLM pipeline.

**When to build this:** Only if the rolling journal hits context limits in practice. The journal's incremental design means this is unlikely to be needed for typical campaigns. Defer indefinitely.

---

### Shared implementation notes

- All four read from the same `.summary.md` sidecar files written by `wisper summarize`
- Campaigns without any summarized sessions silently show nothing (the buttons are disabled or hidden)
- The `summarize.py` `SummaryNote` dataclass already captures loot, NPCs, follow-ups — the campaign-level LLM just needs to receive multiple of these and synthesize
- The rolling journal is the highest-value, most technically tractable feature — build it first; the others follow naturally from the same infrastructure
- All three non-hierarchical features fit into the existing `JobQueue` as new `JOB_CAMPAIGN_*` types, giving them the same SSE progress page as transcription and summarize jobs

---