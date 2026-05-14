# wisper-transcribe — Open Items

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

## Pycord / DAVE Sidecar Migration

**Issue:** [#39](https://github.com/brandonfhall/wisper-transcribe/issues/39) — DAVE (Discord Audio/Video E2EE) blocking voice bot audio receive — **OPEN**

**Background:** Discord enforced DAVE E2EE for non-stage voice calls on March 2, 2026. The Java JDA sidecar continues to work (JDA 6.x has DAVE support). The Python side of the codebase has no DAVE implementation yet.

**Blockers being watched:**
- **Pycord PR #3159** — DAVE receive for pycord. Still open as of late April 2026.
- **discord.py PR #10300** — DAVE via the `davey` (OpenMLS) dependency. Actively in progress; issue #9948 tracks it.

**Migration path** (when a Python library ships stable DAVE receive):
1. Delete `discord-bot/` (the Gradle/Java project)
2. Write ~100-line Python replacement emitting the same length-prefixed PCM wire format over the existing Unix socket
3. Update `BotManager` to launch the Python script instead of the JAR
4. Remove the Java builder stages from `Dockerfile` and the Java 25 requirement from launchers + README

Nothing else changes — `SegmentedOggWriter`, the web UI, campaigns, CLI, and all tests remain unaffected.


## Ideas ##

* Add per line rename speaker in the web UI — **OPEN** 

* Suppress webserver output on the termainl when running in web UI mode — **OPEN** (currently the server logs are mixed with the job progress logs, which is noisy and can be confusing)



## UI Bugs ##

* on the transcript results page the "Generate Summary" button is shown even after a summary has been generated. Additionally the summary is not shown on the transcript results page after it has been generated. — **OPEN** (template logic is correct; suspected CWD mismatch between server launch dir and where the summary was written — reproduce needed)

* Summary generation is not properly working for at least ollama-cloud it could affect other providers as well. It appears that it is coming back but it's not being properly detected.
    - "LLM: ollama-cloud / glm-5.1
    Generating campaign summary for Impossible Landscapes S1 E1 — Remove Your Mask.md ...
    Connecting to Ollama (https://ollama.com)...
    LLM: ollama-cloud / glm-5.1
    Generating campaign summary for Impossible Landscapes S1 E1 — Remove Your Mask.md ...
    Connecting to Ollama (https://ollama.com)...
    Waiting for glm-5.1 to start generating...
    Generating (glm-5.1): ···········
    Summarize failed: Ollama JSON response did not parse: Expecting value: line 1 column 1 (char 0). Raw: '```json\n{\n "summary": "The season four premiere of Get in the Trunk opens with a cinematic cold read performed by Troy, featuring his returning character Roger Cummestone aboard a flight into Baltimo'"


