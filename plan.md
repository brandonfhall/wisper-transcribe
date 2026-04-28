# Wisper-Transcribe: Backlog & Active Work

## Project Context

Podcast transcription tool for tabletop RPG actual-play recordings (D&D, Pathfinder, etc.) with 5–8 speakers (GM + players). Transcripts are fed into NotebookLM for querying game events and tracking stats.

**Hardware:** NVIDIA RTX 3090 (Windows), Apple M5 Mac. Both platforms supported.
**Processing:** Fully local — no cloud APIs. CLI + web UI.
**Stack:** faster-whisper + pyannote-audio. See [architecture.md](architecture.md) for full technical reference and [README.md](README.md) for user docs.

---

## Backlog

### Distribution — Tier 3: PyPI + pipx (future)

**Goal:** `pipx install wisper-transcribe` — fully isolated, one command, no venv management.

**What's needed:**
1. **Publish to PyPI** — `pyproject.toml` is already correctly structured. Steps:
   - Create a PyPI account and API token
   - Add a GitHub Actions release workflow (`.github/workflows/publish.yml`) that runs `python -m build && twine upload` on a `v*` tag push
   - `pip install build twine` (dev deps, not in `pyproject.toml`)
2. **pipx install story** — once on PyPI:
   ```bash
   pipx install wisper-transcribe           # base install (Ollama LLM)
   pipx inject wisper-transcribe anthropic  # cloud LLM extras
   ```
3. **Entry-point completeness** — `wisper server` must download `htmx.min.js` on first run if missing (Docker build does this; local pip installs do not). Add a startup check in `app.py` that downloads it if the placeholder is detected.
4. **Version pinning strategy** — ML dependencies (torch, pyannote, faster-whisper) move fast. Consider using `>=` lower bounds (as now) but adding a tested upper bound for major ML versions to prevent surprise breakage on pip installs.

**Why not now:** Requires cutting releases, managing PyPI credentials, and the htmx download story. Good to do once the tool is stable enough to version properly.

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

---


## Manual Test Plans

### LLM Post-processing CLI (T1–T5) — code complete; manual verification pending

**T1 — `wisper refine`**

T1.1 Dry-run: `wisper refine session.md` → diff printed, file unchanged.
T1.2 No terms: `wisper refine session.md` with no hotwords → skipping warning.
T1.3 Apply + backup: `wisper refine session.md --apply` → `.bak` created.
T1.4 No-color: `wisper refine session.md --no-color | cat` → no ANSI codes.
T1.5 Unknown task: `wisper refine session.md --tasks unknown` → suggestions only.
T1.6 Both tasks: `wisper refine session.md --tasks vocabulary,unknown`.
T1.7 YAML frontmatter unchanged after `--apply`.

**T2 — `wisper summarize`**

T2.1 Basic: `wisper summarize session.md` → sidecar with all sections.
T2.2 No overwrite: second run without `--overwrite` → error.
T2.3 Overwrite: `wisper summarize session.md --overwrite` → clean regeneration.
T2.4 Custom path: `wisper summarize session.md --output /tmp/recap.md`.
T2.5 Sections filter: `--sections summary,loot` → only those sections present.
T2.6 Wiki-links: enrolled speaker names become `[[Name]]` in body.
T2.7 Non-enrolled: unenrolled names stay plain text.
T2.8 Refine flag: `wisper summarize session.md --refine` → `refined: true` in frontmatter.

**T3 — Combined flow**

T3.1 Refine failure still produces summary (bad endpoint → WARN + summary written).
T3.2 `--refine-tasks vocabulary,unknown` → both passes + unresolved speakers in output.

**T4 — LLM config integration**

T4.1 Provider flag beats config.
T4.2 `wisper config llm` wizard round-trip → keys masked in `config show`.
T4.3 Env var beats config key for API access.

**T5 — Edge cases**

T5.1 No-frontmatter transcript handled gracefully.
T5.2 Empty transcript body — no crash.
T5.3 Read-only directory — clean error.

### Web UI LLM post-processing (W1–W5) — code complete; manual verification pending

W1 Post-process checkboxes on transcribe form → campaign notes appear after job.
W2 Standalone summarize from transcript detail → job progress page → notes.
W3 Campaign Notes page renders with metadata.
W4 Delete transcript removes summary sidecar.
W5 Summary badge on transcripts list page.

### Progress bar redesign (P1–P3) — code complete; manual verification pending

P1 Transcription-only job: T → D → F pills advance, bar fills across all three slices, ETA/rate shown during T and D.
P2 Transcription + post-summarize: T → D → F → S pills shown; S activates on "Summarizing…" log line; estimator creeps bar during S when no tqdm data.
P3 Standalone summarize job: only S pill shown; bar fills from 0 → 100 via estimator + done event.
